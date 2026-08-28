---
name: kanban-safety-protocols
description: Safety guardrails for kanban task execution — branch protection (prevent commits to main/wrong branches), worktree verification, and cross-cutting safety patterns that protect the repo from automation errors.
version: 2.12.0
platforms: [linux, macos, windows]
environments: [kanban]
metadata:
  hermes:
    tags: [kanban, safety, branch-protection, guardrails]
    related_skills: [kanban-worker, kanban-orchestrator]
---

# Kanban Safety Protocols

> Cross-cutting safety guardrails for kanban task execution. These patterns protect the repository from automation errors — coders committing to the wrong branch, worktree misconfiguration, and other safety violations that bypass the normal PR workflow.
>
> **Reference:** [Wrong-Base Worktree Case Study](references/branch-guardrail-case-study.md) — real-world failure mode that motivated these protocols, including the coder task t_c36027fd scenario.
> **Reference:** [Hermes Config Backup](references/hermes-config-backup.md) — portable backup/restore procedure for migrating to a new instance.
> **Reference:** [Hermes Config Auto-Sync](references/hermes-config-auto-sync.md) — automated hourly mirror of all agent config into the project repo, with `[skip ci]` to avoid triggering workflows.

## Branch Guardrail — Three-Layer Defense

### Problem
Coders applying fixes to incorrect branches or directly to `main`/`master`, bypassing the PR workflow. This can happen when:
- Worktree resolves to `main` instead of a feature branch
- Worker session lands in the wrong worktree
- Coder skips or misses the branch verification step
- Coder's branch is based on `main` instead of the target branch

### Architecture
The branch guardrail has three independent layers, each in a different file that the coder loads through a different mechanism:

| Layer | What | Where | Loaded by |
|-------|------|-------|-----------|
| 1 | Card body instruction | Every `kanban_create` body (last lines) | Coder reads the card |
| 2 | Mandatory branch check | `AGENTS.md` Tier 2, step 2 | Coder loads project context |
| 3 | Worktree guardrail | `kanban-worker/SKILL.md` workspace section | Coder loads skill |

If any one layer is missing or fails, the remaining two still catch the error.

### Layer 1 — Card Body (Orchestrator creates)

Every coder implementation card **must** have these as the last lines of the `body` parameter:

```
BASE BRANCH: <target-branch-name>
CRITICAL: Before writing code, run `git branch --show-current` and verify you are on a worktree branch derived from the base branch above. You must NOT be on main or master. If you are, block the task immediately.
```

The `BASE BRANCH:` line tells the coder which branch the worktree should be based on. The orchestrator must also pass `branch_name=<target-branch>` on the `kanban_create` call so `$HERMES_KANBAN_BRANCH` is set in the worker's environment.

**Encoding in the orchestrator's identity file (SOUL.md):**
This rule must be baked into the orchestrator's `SOUL.md` (loaded via `prefill_messages_file: SOUL.md`) so every decomposition session produces compliant cards automatically. The SOUL.md's card body format should require:
- A `Base branch:` field in the body format listing
- The `BASE BRANCH:` + guardrail as mandatory last lines
- A "Branch Specification on Card Creation" section that says: "pass `--branch <target-branch>` on every `kanban_create` call for named PR/fix branches; **omit `--branch` for feature work on main** (the dispatcher auto-derives `wt/t_<task-id>` — passing `--branch main` causes a worktree collision since `main` is already checked out at the repo root)."

Without this, the instruction exists in the skill but the orchestrator won't follow it until manually reminded.

**Implementation in orchestrator:**

```python
kanban_create(
    title="[DF-42] Fix package.json on fix/df-41-broken-thing",
    assignee="coder",
    body=(
        "Goal: Fix ERR_MODULE_NOT_FOUND...\n"
        "Files: package.json, scripts/check-deps.sh\n"
        "Verification: ./run-tests.sh frontend-unit passes\n"
        "BASE BRANCH: fix/df-41-broken-thing\n"
        "CRITICAL: Before writing code, run `git branch --show-current`. "
        "You must NOT be on main or master. If you are, block the task immediately."
    ),
    workspace="worktree",
    branch="fix/df-41-broken-thing",  # <-- sets $HERMES_KANBAN_BRANCH
)["task_id"]
```

**CLI equivalent** (when using terminal instead of the Python tool):

```bash
hermes kanban create \
  --workspace worktree \
  --branch fix/df-41-broken-thing \
  --assignee coder \
  --body "Goal: Fix ERR_MODULE_NOT_FOUND...
Files: package.json, scripts/check-deps.sh
Verification: ./run-tests.sh frontend-unit passes

BASE BRANCH: fix/df-41-broken-thing
CRITICAL: Before writing code, run git branch --show-current. You must NOT be on main or master. If you are, block the task immediately." \
  "[DF-42] Fix package.json on fix/df-41-broken-thing"
```

### Layer 2 — AGENTS.md (Coder reads at startup)

Tier 2 Coders Instructions must include `git branch --show-current` as step 2, before any implementation:

1. Orient — read card body
2. **Verify branch** — `git branch --show-current` and `echo "HERMES_KANBAN_BRANCH=$HERMES_KANBAN_BRANCH"`. Must NOT be `main` or `master`, and env var must match card body's `BASE BRANCH:` line.
3. Implement
4. Test
5. Lint
6. Commit
7. Hand off

**Three-way decision tree for the branch check (with base branch cross-check):**

```python
import os, subprocess

branch = subprocess.run(["git", "branch", "--show-current"], capture_output=True, text=True).stdout.strip()
base_branch = os.environ.get("HERMES_KANBAN_BRANCH", "")

if branch in ("main", "master"):
    kanban_block(reason="CRITICAL: worktree is on main — cannot implement on this branch. Need worktree checkout.")
    return
elif not branch.startswith(("wt/", "fix/")):  # not a known worktree prefix
    kanban_block(reason=f"WRONG BRANCH: current branch is '{branch}', expected worktree branch. Cannot implement here.")
    return
elif not base_branch:
    # Fallback: no base specified, just proceed (feature work on main base)
    pass
else:
    # Check base branch matches card body
    # (card body says BASE BRANCH: <name> — confirm it matches env var)
    print(f"Base branch from env: {base_branch}")
    # Also verify by reading the card body's BASE BRANCH: line
    card_body = os.environ.get("HERMES_KANBAN_TASK_BODY", "")
    if "BASE BRANCH:" in card_body:
        card_base = card_body.split("BASE BRANCH:")[1].split("\n")[0].strip()
        if card_base != base_branch:
            kanban_block(reason=f"WRONG BASE: card says base '{card_base}' but worktree was created from '{base_branch}'. Blocking.")
            return
    # proceed — correct branch with correct base
```

### Layer 3 — kanban-worker Skill (Coder loads)

The worker skill's workspace-handling table and Do NOT list must carry the same guardrail:

**Worktree setup must use the base branch when available:**

- When `$HERMES_KANBAN_BRANCH` is set: `git worktree add -b wt/$HERMES_KANBAN_TASK <path> $HERMES_KANBAN_BRANCH`
- When `$HERMES_KANBAN_BRANCH` is NOT set: `git worktree add -b wt/$HERMES_KANBAN_TASK <path> HEAD` (creating from HEAD). **The `-b` flag is CRITICAL** — without it, git checks out an EXISTING branch (which doesn't exist yet) and falls through to detached HEAD. Commits in detached HEAD have no branch ref and become invisible to the consolidation script after the worktree is pruned.

This ensures the worktree branch is based on the correct target, not on `main`.

**Branch guardrail section:** "Before writing any code in a worktree, you MUST verify the current branch with `git branch --show-current` and the base branch with `echo \"$HERMES_KANBAN_BRANCH\"`."

**Do NOT:** "Commit or push to `main` or `master` — ever. This is a hard stop."

### Zero-Exemption Rule

**There is no "quick fix" exemption.** Not even if CI is green. Not even if the change is one line. Not even if the coder is "just fixing a typo." Committing to `main`/`master` is **always** wrong for a dispatched coder worker. The only exception is the orchestrator, which opens PRs after all review gates pass.

## Base Branch Specification

### Why It Matters

Without an explicit base branch, the worktree is created from `main`/HEAD. If the target branch is a PR branch (e.g., `fix/df-41-broken-thing`), the coder ends up on a branch descended from `main` — which may already have the fix. The coder "passes" tests, commits to a branch that has the fix inherited from main, but the target branch never gets the fix. The reviewer catches the discrepancy, but the cycle is wasted.

### The Fix

Three things must align:

1. **Orchestrator** passes `branch_name=<target-branch>` on `kanban_create` → `$HERMES_KANBAN_BRANCH` env var set
2. **Worktree setup** creates from that base: `git worktree add -b wt/$TASK <path> $BRANCH`
3. **Coder** verifies the env var matches the card body's `BASE BRANCH:` line

### When to Set

| Scenario | `base_branch` value |
|---|---|
| Feature work based on main | **Omit `--branch`** — dispatcher auto-derives `wt/t_<task-id>`. Never pass `main` as the branch name — it causes `git worktree add` to fail with collision since `main` is already checked out at the repo root. |
| Bug fix on existing PR branch | The PR branch name (e.g., `fix/df-42-save-values`) |
| Hotfix on a release branch | The release branch name |
| Named agent task | e.g., `agent/GH-101` |

**Real-world failure mode (Jul 24):** Three coder cards were created with `--branch main` and all three blocked immediately with `"fatal: 'main' is already used by worktree at '...'"`. The fix was to update the DB's `branch_name` column from `main` to `wt/t_<task-id>` and reset `consecutive_failures` to 0. The orchestrator's SOUL.md and the kanban-orchestrator skill both instructed passing `--branch main` for feature work — this instruction was wrong. The `--branch` parameter is the **literal worktree branch name**, not the base branch. Passing `main` tries to check out `main` in a second worktree, which is impossible.

### Pitfall: SOUL.md Overrides Skill Guidance

The `--branch main` collision happens because the orchestrator's SOUL.md (system prompt identity) contains the instruction "pass `--branch main` for feature work." This identity file is loaded before any skill and defines the card-creation template. Even when the skill says "omit `--branch` for feature work," the orchestrator follows the SOUL.md because it's the template it uses for every card creation.

**Fix:** Update BOTH the skill AND the SOUL.md/identity file. The SOUL.md's card body format must match the skill's "When to Set" table. The skill's Layer 1 "Encoding in the orchestrator's identity file" subsection documents the correct SOUL.md text — ensure it's actually applied.

**Cross-reference:** The kanban-system-health reference `recurring-corruption-jul24.md` documents the full failure chain.

## Recovering from Wrong-Branch Commits

If a coder commits to `main` despite the guardrails:

1. **Identify** the commit: `git log main --oneline -5`
2. **Undo the local commit:**
   ```bash
   git checkout main
   git reset --hard HEAD~1
   ```
3. **Force-push** (only if branch protection allows):
   ```bash
   git push origin main --force-with-lease
   ```
4. **Diagnose why the guardrail failed.** Common causes:
   - Worktree setup script resolved to `main` instead of the feature branch
   - Worker session didn't load AGENTS.md (terminal.cwd misconfiguration)
   - Card body was truncated and lost the guardrail line
   - The worker profile doesn't load the kanban-worker skill
5. **File a fix card** for the root cause — don't just undo the commit and move on.

## Adding New Safety Protocols

This skill is the umbrella for any cross-cutting safety pattern that doesn't fit neatly into kanban-worker (worker-side) or kanban-orchestrator (orchestrator-side). When adding a new protocol:

1. Document the **problem** — what can go wrong
2. Document the **multi-layer defense** — at minimum two independent layers
3. Document the **zero-exemption rule** — where the hard line is
4. Document the **recovery procedure** — how to undo if it happens anyway
5. Update all four locations: SOUL.md (orchestrator identity → card body format), AGENTS.md (coder instructions), and kanban-worker skill (worktree setup + Do NOT list)

#
## CI/Deploy YAML Change Guardrail

### Problem

Changes to CI/deploy workflow files (`.github/workflows/*.yml`, `Dockerfile`, `frontend/lighthouserc.cjs`) bypass the PR test suite entirely — the workflow YAML is never executed during the review gate. Latent bugs like wrong secret references, broken glob patterns, or upload-artifact@v6 defaults only surface on the first real run after merge.

Real failure modes caught by this guardrail:

| Gotcha | Symptom | Root Cause |
|--------|---------|------------|
| `upload-artifact@v6` silently ignores dotfiles | Artifact says "No files found" but reports exist on disk | Default `include-hidden-files: false` — `.lighthouseci/` starts with `.`, so the glob returns empty. `hashFiles()` uses the same glob pattern and also returns empty. |
| `npx lhci` resolves to wrong package | "Hello, this is AnupamAS01!" printed instead of Lighthouse audit | The npm package `lhci` is a squat/placeholder (`description: "placeholder anupamas0x1"`). The real CLI is `@lhci/cli`. |
| `environment: staging` secret scoping | Missing secret on first run | Secrets in named environments only resolve when the job has `environment:` set. |
| `github.event_name` gating | Job silently skipped on `workflow_dispatch` | Condition only allowed `pull_request_target`. Manual deploys never run the job — was correct when added but broke when deploy flow changed. |

### Three-Layer Defense

#### Layer 1 — Post-Change Validation (Orchestrator must run)

After ANY change to CI/deploy YAML or workflow-adjacent files (lighthouserc, Dockerfile, scripts invoked by CI), the orchestrator MUST:

1. **Trigger a `workflow_dispatch`** on the PR branch (or main if already merged) and verify the job completes successfully:
   ```bash
   gh workflow run deploy.yml --ref <branch> --repo <owner>/<repo>
   ```
2. **Inspect the job logs** for the changed step — do not rely on the run's green checkmark alone. A skipped job or a hidden step failure still shows a green run.
3. **Check the artifact** if the change touches upload steps — verify the artifact exists and has the expected files.

#### Layer 2 — Known-Problem Documentation (This section)

When writing new CI steps, check for these common upload-artifact@v6 pitfalls:

- **`include-hidden-files: true`** — Always set when the path is a dot-directory (`.lighthouseci/`, `.coverage/`, `.nyc_output/`). The default `false` causes both `hashFiles()` and the upload to silently return empty.
- **`if-no-files-found: error`** — Set to `error` instead of the default `warn` during development; switch to `warn` after you've confirmed the glob works. This catches glob mismatches instead of silently skipping.
- **`npx` package resolution** — Never use `npx <short-name>` for tools that have squat packages on npm. Always use the full scoped name (`npx @lhci/cli`, `npx @angular/cli`, etc.). Verify with `npm view <name>` before committing.

#### Layer 3 — Recovery (When the guardrail fails)

If a CI change hits production and fails:

1. **Immediately revert** the YAML change to the previous known-good version
2. **Create a kaban fix card** with the diagnostic findings — never cherry-pick a second fix directly to main
3. **Tag the root cause** in the `kanban-safety-protocols` skill if it's a new gotcha pattern

# Coder Review-Required Block Auto-Complete

### Problem

Coders sometimes call `kanban_block(reason="review-required: ...")` instead of `kanban_complete()`. This blocks the pipeline — the paired reviewer card never promotes because it's waiting for the coder to complete.

### Layer 1 — Instruction (kanban-worker skill)

The kanban-worker skill says: "Block only when you hit a genuine roadblock... Do NOT block for review." And explicitly: "If you call kanban_block(reason='review-required:...'), a watchdog cron will auto-complete your card within 5 minutes anyway."

### Layer 2 — Watchdog Cron (auto-complete)

A `no_agent: true` cron job running every 5 minutes queries the events table for blocked coder cards with `reason LIKE 'review-required:%'` and auto-completes them. The events table is the source of truth (not `last_failure_error` which is often empty):

```sql
SELECT DISTINCT t.id FROM tasks t
JOIN task_events e ON e.task_id = t.id
WHERE t.status = 'blocked' AND t.assignee = 'coder'
  AND e.kind = 'blocked'
  AND json_extract(e.payload, '$.reason') LIKE 'review-required:%'
```

**Script:** `scripts/coder-review-required-watch.py` at `~/.hermes/profiles/orchestrator/scripts/`

**Cron setup:**
```bash
cronjob action=create schedule="every 5m" name="coder-review-required-watch" script="coder-review-required-watch.py" deliver="telegram" no_agent=true
```

### Layer 3 — Notification

When the watchdog fires, a Telegram notification is delivered with the list of auto-completed cards.

## PR Consolidation Watchdog

### Problem

When a coder+reviewer pair completes (both `done`, reviewer `approved`), the worktree branch must be pushed and a PR created. This does NOT happen automatically. The worktree branch exists locally but is never pushed to origin. If the worktree is pruned, commits are stranded.

### The Watchdog

A `no_agent: true` cron job running every 10 minutes:

1. Queries the kanban DB for `done` coder cards with `done` reviewer children
2. Checks if the worktree branch has commits not in `origin/main` (via `git cherry`)
3. Pushes the branch to origin
4. Creates a PR via `gh pr create`

**⚠️ Query must include `archived` status.** Cards get archived by `hermes_github_sync.sh` and other processes, changing status from `done` to `archived`. A query filtering only `c.status = 'done' AND r.status = 'done'` will miss archived pairs — no PR ever gets created and the user must manually push every branch. **Fix:** Use `c.status IN ('done', 'archived') AND r.status IN ('done', 'archived')`. The `already_has_pr()` check prevents duplicates.\n\n**⚠️ `hermes_github_sync.sh` auto-closes issues prematurely.** The sync script closes `[GH-N]` issues when ANY done card matches — including orchestrator epic cards that just finished decomposition. See `references/hermes-github-sync-guards.md` for the 4-layer guard system (Guard 4 specifically prevents premature close when coder children are still in flight).\n\n**⚠️ Version bump must use local branch refs, not `origin/`.** The semver bump scans commit messages with `git log origin/main..origin/{branch}`. Coder worktree branches (`wt/t_XXXXX`) are local-only — `origin/{branch}` doesn't exist. Use `origin/main..{branch}` (local ref) instead. Also add `--allow-empty` to the bump commit and check return codes from `sync-version.sh`.

**⚠️ Version bump worktree collision — find existing worktree, don't create new.** The version bump logic creates a temp worktree with `git worktree add /tmp/wt_bump_XXX <branch>`. This fails when `<branch>` is already checked out by the coder's worktree (`fatal: '<branch>' is already used by worktree at '.worktrees/t_XXX'`). The old code silently skipped the ENTIRE card (no PR at all) when this happened. **Fix:** Scan existing worktrees with `git worktree list --porcelain` to find the coder's worktree path, and run the version bump there directly. If no existing worktree is found, fall back to creating a temp one. If BOTH fail, skip only the version bump — still push and create the PR. Never skip the PR because of a version bump failure. See `pr-consolidation-watch.py` for the implementation.

**Script:** `scripts/pr-consolidation-watch.py` at `~/.hermes/profiles/orchestrator/scripts/`

**Cron setup:**
```bash
cronjob action=create schedule="every 10m" name="pr-consolidation-watch" script="pr-consolidation-watch.py" deliver="telegram" no_agent=true
```

### Recovery from Stranded Branches

When discovering local-only branches with un-pushed commits:

```bash
# Find all local branches with commits not on origin
git branch --format='%(refname:short)' | while read branch; do
  remote=$(git branch -r --list "origin/$branch" | head -1)
  if [ -z "$remote" ]; then
    commits=$(git rev-list --count origin/main..$branch 2>/dev/null || echo 0)
    if [ "$commits" -gt 0 ]; then
      echo "$branch|$commits"
    fi
  fi
done
```

Check if commits are already in main using `git cherry`:

```bash
git cherry origin/main <branch> | head -5
# '+' = not in main, '-' = already in main
```

Then push and PR:

```bash
git push origin <branch>
gh pr create --base main --head <branch> --title "<first-commit-msg>" --body "Recovered from local-only branch."
```

### Deduplication

When the same commit appears across multiple worktree branches (common when the same fix was iterated on), use `git cherry` to identify the unique commits. Only push one representative branch per unique commit set.

## GitHub Actions Deploy Trigger Pitfall

The `deploy-to-staging` job uses `github.event.pull_request.merged` to detect PR merges on `pull_request_target` events. **This field can be the string `'true'` instead of the boolean `true`**, causing `== true` to evaluate to `false` and the deploy job to be silently skipped.

**Fix:** Use a truthy check instead of `== true`, and add an explicit `action == 'closed'` guard:

```yaml
if: github.event_name == 'pull_request_target' && github.event.action == 'closed' && github.event.pull_request.merged && github.event.pull_request.base.ref == 'main'
```

The truthy check works for both `true` (boolean) and `'true'` (string). The `action == 'closed'` guard ensures we only trigger on PR close events.

## GitHub Actions Node 24 Upgrade Reference

When upgrading GitHub Actions to Node 24, check the current version's Node runtime and the latest version's Node runtime:

```bash
# Check a specific action's Node version
curl -s "https://api.github.com/repos/<owner>/<repo>/contents/action.yml?ref=<tag>" \
  | python3 -c "import sys,json,urllib.request; d=json.load(sys.stdin); print(urllib.request.urlopen(d['download_url']).read().decode())" \
  | grep -E '^\s*using:'
```

As of Jul 2026, the following actions have Node 24 versions:

| Action | Node 20 | Node 24 |
|---|---|---|
| actions/checkout | @v4 | @v5 |
| actions/setup-node | @v4 | @v5 |
| actions/setup-python | @v5 | @v6 |
| actions/cache | @v4 | @v5 |
| actions/upload-artifact | @v4 | @v6 |
| actions/github-script | @v7 | @v8 |
| docker/login-action | @v3 | @v4 |
| google-github-actions/auth | @v2 | @v3 |
| google-github-actions/setup-gcloud | @v2 | @v3 |
| hashicorp/setup-terraform | @v3 | @v4 |
| astral-sh/setup-uv | @v5 | @v7 |
| dorny/paths-filter | @v3 | @v4 |

---

## Worktree Branch Collision Prevention (Pattern 5b)

### Problem

When creating a fix card (especially during automated review-failed resolution), the new card may be assigned a `--branch` value that's already checked out by a sibling worktree. Git refuses with: `fatal: '<branch>' is already used by worktree at '...'`. The coder spawns, fails, and the card stays blocked with `consecutive_failures >= 1`.

This happens most commonly when:
- Auto-resolution copies the original coder's `branch_name` into the new fix card (the original worktree still has it)
- Epic decomposition assigns the same agent branch name to sibling cards
- Manual re-spec reuses the old branch name out of habit

### Three-Layer Defense

#### Layer 1 — Pre-creation Branch Uniqueness Check (Prevent)

Before creating any worktree card with `--branch`, check whether the branch is already in use. The script `scripts/assert-branch-unique.sh` (from `~/.hermes/profiles/orchestrator/scripts/`) checks both:

- **Live git worktrees** (`git worktree list`) — catches branches checked out by completed tasks' worktrees still on disk
- **Kanban DB** — catches branches queued by other active (non-archived, non-done) tasks

Call this before every `kanban_create --branch <name>`:

```bash
if ! assert-branch-unique.sh "fix/gh-592-foo"; then
    echo "Collision detected. Omitting --branch (dispatcher auto-derives)."
    BRANCH_ARG=""
fi
```

#### Layer 2 — Auto-Resolution Branch Handling (Design)

When creating fix cards during review-failed auto-resolution:

- **Do NOT copy** the original coder's `branch_name` — the original worktree still has that branch checked out
- **Omit `--branch`** entirely — the dispatcher auto-derives `wt/t_<task-id>`, which is guaranteed unique
- **Or generate a fresh name** like `fix/<issue_hook>-<short-descriptor>` (no collision risk)
- The card body's `BASE BRANCH:` should reference the original coder's worktree branch (for the worker guardrail) — this is different from `--branch`

#### Layer 3 — Automated Safety Net Cron (Recover)

The `worktree-collision-watch` cron job runs every 5 minutes as a `no_agent` watchdog. It:

1. Queries the kanban DB for `blocked` coder cards with `last_failure_error` containing "already used by worktree"
2. Auto-assigns a unique branch name (`fix/<gh-part>-collision-<ts>`) that doesn't conflict with any live worktree
3. Resets `consecutive_failures` to 0 and status to `todo`
4. Delivers a Telegram notification when remediation is applied (silent when no collisions exist)

This catches any Pattern 5b collision that the pre-creation check misses (e.g., cards created by non-orchestrator processes like scripts, cron, or direct DB inserts).

**Script:** `scripts/worktree-collision-watch.py` (from `~/.hermes/profiles/orchestrator/scripts/`)

**Cron creation:**

```bash
hermes cron create \
  --name worktree-collision-watch \
  --schedule "every 5m" \
  --deliver telegram \
  --no-agent \
  --script worktree-collision-watch
```

**Note:** The script must be symlinked or copied to `~/.hermes/scripts/` for the cron system to find it.

---

## Stranded Worktree Commits (Never Pushed to Origin)

### Problem

Coders commit to local worktree branches but the orchestration layer never pushes them to origin. When the worktree is pruned (cleaned up after task completion), the commits exist only in the local git repository's object database. If the local clone is deleted or garbage-collected, the commits are permanently lost.

This happens because:
1. The coder commits to `wt/t_<task-id>` or `fix/<name>` in the worktree
2. The coder calls `kanban_complete()` — but never `git push`
3. The orchestrator doesn't push the branch either
4. The worktree is pruned, but the branch ref survives locally
5. Over time, 100+ local-only branches can accumulate with un-pushed commits

### Detection

Query for local-only branches that have commits not on main and not on origin:

```bash
git branch --format='%(refname:short)' | while read branch; do
  remote_exists=$(git branch -r --list "origin/$branch" | head -1)
  if echo "$branch" | grep -qE '^(wt/|fix/|agent/)'; then
    commits=$(git rev-list --count origin/main..$branch 2>/dev/null || echo 0)
    if [ "$commits" -gt 0 ] && [ -z "$remote_exists" ]; then
      echo "$branch ($commits commits) — LOCAL ONLY"
    fi
  fi
done
```

### Automated Prevention — PR Consolidation Watchdog

The `pr-consolidation-watch` cron job runs every 10 minutes as a `no_agent: true` script. It:

1. Queries the kanban DB for done coder cards with done reviewer children (approved reviews)
2. Pushes the worktree branch to origin if not already there
3. Creates a GitHub PR from the branch
4. Delivers a Telegram notification when PRs are created
5. Silent when nothing to do

**Script:** `~/.hermes/profiles/orchestrator/scripts/pr-consolidation-watch.py`

**Cron registration:**

```bash
cronjob action=create \
  schedule="every 10m" \
  name="pr-consolidation-watch" \
  script="pr-consolidation-watch.py" \
  deliver="telegram" \
  no_agent=true
```

### Automated Recovery — Coder Review-Required Watchdog

The `coder-review-required-watch` cron job runs every 5 minutes. It detects coder cards blocked with `review-required:` reason and auto-completes them so the paired reviewer card can promote and the PR consolidation watchdog can create the PR.

**Script:** `~/.hermes/profiles/orchestrator/scripts/coder-review-required-watch.py`

**Cron registration:**

```bash
cronjob action=create \
  schedule="every 5m" \
  name="coder-review-required-watch" \
  script="coder-review-required-watch.py" \
  deliver="telegram" \
  no_agent=true
```

### Recovery Procedure

When stranded commits are discovered:

1. **Identify** local-only branches with un-pushed commits (see detection query above)
2. **For each branch**, push and create a PR:
   ```bash
   git push origin <branch>
   gh pr create --base main --head <branch> --title "fix: <summary>" --body "Recovered from local-only branch."
   ```
3. **Verify** the PR's CI passes and the changes are correct
4. **For the general case**, rely on the `pr-consolidation-watch` cron to catch future pairs automatically

### Pitfall: Worktree Pruning Doesn't Delete the Branch

`git worktree prune` removes the worktree directory metadata but does NOT delete the branch or its commits. The branch ref and commit objects remain in the local git database. However, if the branch is force-deleted with `git branch -D` or the local clone is removed, the commits are lost (garbage-collected after 90 days in reflog).

The `pr-consolidation-watch` cron pushes the branch from the local ref — it does not need the worktree directory to exist. The commits are in the local object DB even after the worktree is pruned.

### Root Cause: Detached HEAD Worktrees (Missing `-b` Flag)

The most dangerous stranded-commit scenario is invisible to the consolidation scripts: **commits made in a detached HEAD worktree never get a branch ref at all**.

If `git worktree add` is called without `-b <branch>`, git checks out an EXISTING branch by that name. If no such branch exists (the first run for this task), the command fails and falls through to detached HEAD. The coder's commits land on the worktree's internal HEAD pointer, NOT on a named branch:

```
$ git worktree add /tmp/test wt/t_XXXXX  ← no -b flag
fatal: 'wt/t_XXXXX' is not a commit and a branch 'wt/t_XXXXX' cannot be created
$ git branch --show-current
HEAD  ← detached!
```

When the worktree is pruned, the HEAD pointer is lost. The commit objects survive in the object DB (reachable only by hash) but the consolidation script finds no branch ref — `git branch` returns nothing, `get_branch_commit_count()` returns -1, and the card is silently skipped.

**Three-layer fix:** See `kanban-worker/SKILL.md` for:
- Mandatory `-b` flag on every `git worktree add` call
- Mandatory `git push origin <branch>` before `kanban_complete()` (so even if the worktree is lost, the remote ref survives)
- The "Abandon work without pushing" Do NOT entry

### Automated Recovery: `recover_from_worktree()`

Both `build-consolidate-prs.py` and `pr-consolidation-watch.py` now include a `recover_from_worktree(branch)` function. When the consolidation script encounters a missing branch (count == -1), it scans `.worktrees/<task-id>/` for the commit before declaring the branch lost:

```python
def recover_from_worktree(branch):
    """Fallback: recover branch ref from worktree on disk."""
    task_id = branch.replace("wt/", "") if branch.startswith("wt/") else branch
    for suffix in [task_id, branch.replace("/", "_")]:
        wt_dir = os.path.join(REPO_DIR, ".worktrees", suffix)
        if not os.path.isdir(wt_dir):
            continue
        rc, out, _ = run(["git", "rev-parse", "HEAD"], cwd=wt_dir, timeout=10)
        if rc == 0 and out.strip():
            rc2, _, _ = run(["git", "branch", "--force", branch, out.strip()], timeout=10)
            if rc2 == 0:
                print(f"  ♻️  Recovered '{branch}' from {suffix}")
                return True
    return False
```

This only works if the worktree directory still exists on disk — run the consolidation script before the prune cycle (which has a 7-day staleness gate for worktrees untouched >24h and a 7-day stale deadline).

### Pitfall: Consolidation Script Only Fetched `origin main`

Until Aug 2026, `build-consolidate-prs.py` only ran:
```python
run(["git", "fetch", "--depth=100", "origin", "main"], timeout=30)
```

If a worktree branch WAS pushed to origin during the brief window before pruning, the consolidation script still couldn't see it — `origin/wt/t_XXXXX` wasn't fetched. Fixed by adding:
```python
run(["git", "fetch", "--depth=100", "origin", "refs/heads/wt/*:refs/remotes/origin/wt/*"], timeout=60)
```

This two-fetch pattern should be included in any new consolidation or merge-audit script.

---

## Active PR Guard Recovery

### Problem

After a coder completes work and opens a PR, the card may be unblocked (e.g., after a `review-required` → unblocked cycle). The dispatcher tries to re-spawn the coder but detects an active PR and guards the spawn with `respawn_guarded` (reason `active_pr`). The card stays in `ready` forever — the guard correctly prevents duplicate work, but the dispatcher keeps logging "ready queue non-empty for N ticks but 0 workers spawned" warnings.

### Diagnosis

Check event history for the pattern — repeated `respawn_guarded` (reason `active_pr`) with no intervening `claimed` or `spawned`:

```bash
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db "
SELECT kind, created_at
FROM task_events
WHERE task_id = '<id>'
ORDER BY created_at DESC
LIMIT 10;
"
```

### Automated Recovery

The `active-pr-guard-watch` cron job (every 5 min, no_agent) detects cards with 5+ consecutive `respawn_guarded` events (no intervening `claimed`/`spawned`) and moves them to `triage` for orchestrator handling.

**Script:** `scripts/active-pr-guard-watch.py`

**Cron creation:**

```bash
hermes cron create \
  --name active-pr-guard-watch \
  --schedule "every 5m" \
  --deliver telegram \
  --no-agent \
  --script active-pr-guard-watch
```

**Reference:** See `references/active-pr-guard-recovery.md` for the full walkthrough with diagnosis commands and real-world example.

### Manual fix (one-off)

```bash
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db "
UPDATE tasks
SET status = 'triage'
WHERE id = '<task-id>' AND status = 'ready';
"
```

### Distinction from Pattern 6 (stuck ready with failures)

| Aspect | active_pr guard | Pattern 6 (high failures) |
|--------|-----------------|---------------------------|
| Event pattern | `respawn_guarded` only | `spawn_failed` / `crashed` |
| Worker attempted? | Yes (successfully — PR exists) | Yes (failed) |
| PR exists? | Yes | No |
| Fix | Move to `triage` immediately | Investigate worker crash |

---

## Phantom Cards — Cancelled Cards Visible in Dashboard

### Protocol

The orchestrator detects blocked reviewer cards (`status=blocked AND assignee=code-reviewer` with reason starting `review-failed:`), then:

1. **Read the comments** via `kanban_show()` — extract files, issues, suggested fixes
2. **Create a new coder card** with the reviewer's findings baked into the body as paste-able requirements (not just "fix remaining issues")
3. **Create a paired reviewer card** with `parents=[new-coder-id]`
4. **Archive the old blocked reviewer card** with a comment linking to the new fix card

### When NOT to auto-resolve

- Unstructured comments (prose-only, no parseable findings) — escalate to human
- 3+ consecutive review-failed cycles with no progress — escalate
- Project-level decisions (API contract change, security policy) — escalate

### Example Flow

```python
reviewer = kanban_show(task_id="t_55ea20f5")
# Extract: findings["files"], findings["issues"], findings["verification"], base_branch

coder_id = kanban_create(
    title=f"[DF-X] Fix: {findings['summary']}",
    assignee="coder",
    workspace="worktree",
    # Do NOT pass branch=base_branch — the original worktree still has that
    # branch checked out, causing Pattern 5b collision. Omit --branch or
    # generate a fresh unique name.
    body=f"## Goal\n{findings['description']}\n## Reviewer findings\n{findings['details']}\n## Files\n{findings['files']}\n## Verification\n{findings['verification']}\n\nBASE BRANCH: {base_branch}\nCRITICAL: Before writing code, run git branch --show-current and verify you are on a worktree branch derived from the base branch above.",
)[Coder_id]

kanban_create(title=f"Review: {findings['summary']}", assignee="code-reviewer", parents=[coder_id], ...)
kanban_comment(task_id=reviewer["id"], body=f"Superseded by new fix card {coder_id}")
kanban_archive(task_id=reviewer["id"])
```

### Pitfalls

- **Don't re-assign the same card.** Create a NEW card. The old reviewer stays `blocked` (audit trail), the new card gets a fresh lifecycle.
- **Preserve the base branch in the card body, NOT as the worktree branch name.** The original card's `branch_name` belongs to a live worktree — reusing it causes a Pattern 5b collision (`fatal: already used by worktree at ...`). Set `BASE BRANCH: <name>` in the body (for the worker guardrail) but omit `--branch` or generate a fresh name for the fix card's own worktree.
- **Include branch guardrails** in every new fix card body.
- **Bake the exact fix** into the body. The coder won't read the review thread. Include old→new code blocks.
- **Archive, don't cancel.** Archived cards vanish from the dashboard; cancelled ones linger as phantom cards (see Phantom Cards section below).

### Triggering — review-failed-watch Cron Job

Auto-resolution is triggered by a dedicated **review-failed-watch** cron job. This job:
- Runs every 15 minutes
- Loads the `kanban-orchestrator` skill
- Queries for `status=blocked AND assignee=code-reviewer` with `review-failed:` reason
- Extracts findings from reviewer comments, creates a new fix card + paired reviewer, archives the old blocked card
- Delivers a Telegram notification when new cards are created

Without this cron job, auto-resolution only fires when the orchestrator actively processes the board — there is no automatic polling.

**Creating the cron job:**

```bash
hermes cron create \
  --name review-failed-watch \
  --schedule "every 15m" \
  --deliver telegram \
  --workdir /path/to/repo \
  --skills kanban-orchestrator \
  --prompt "Check the <board-slug> kanban board for blocked code-reviewer cards that need auto-resolution. For each card where status=blocked AND assignee=code-reviewer AND reason starts with review-failed:, follow the Automated Review-Failed Resolution playbook: read comments, extract findings, create a new fix card + paired reviewer (with branch guardrails), archive the old blocked card."
```

**Prompt details:** The cron prompt must be fully self-contained — it cannot reference this skill by saying "load the skill and follow it" because the cron runner loads skills before processing the prompt. The prompt should explicitly list the steps rather than just saying "follow the playbook."

---

## Phantom Cards — Cancelled Cards Visible in Dashboard

### Problem

Cancelled cards (`status='cancelled'`) remain visible in the dashboard's `todo` column even though they are no longer actionable. They clutter the board and create confusion about what actually needs work.

### Root Cause

The dashboard's kanban API query is `SELECT * FROM tasks WHERE status != 'archived'` — it shows everything except `archived`. The `cancelled` status is not filtered out. The column mapping treats `cancelled` as equivalent to `todo` for board layout, since the dashboard has no dedicated `cancelled` column.

### Fix — Archive Instead of Cancel

When a card is truly superseded (replacement chain exists, no longer actionable), **archive it** — not cancel:

```bash
hermes kanban comment <task-id> "Archiving — superseded by <new-task-id>"
hermes kanban archive <task-id>
```

### Detective Work — Finding Phantom Cards

When the user reports "more cards in todo than expected," check the actual DB status via the kanban board API, which uses `SELECT * FROM tasks WHERE status != 'archived'`:

```bash
# Compare dashboard count vs terminal list
hermes kanban list --json 2>/dev/null | python3 -c "
import json,sys
d=json.load(sys.stdin)
from collections import Counter
statuses = Counter(t['status'] for t in d.get('tasks',[]))
for s,c in sorted(statuses.items()): print(f'  {s}: {c}')
"

# Or query DB directly
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db \
  "SELECT status, COUNT(*) FROM tasks WHERE status NOT IN ('done','archived') GROUP BY status;"
```

Cancelled cards that should have been archived will show up with `status=cancelled`. The dashboard's column mapping puts them in the `todo` column because there's no dedicated `cancelled` column.

### Bulk Cleanup

For bulk cleanup of old cancelled cards, or archived cards that are still visible:

```bash
# Archive all cancelled cards at once
for tid in $(sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db \
  "SELECT id FROM tasks WHERE status='cancelled'"); do
  hermes kanban comment $tid "Archiving cancelled card — no longer actionable"
  hermes kanban archive $tid
done

# Or direct SQL for mass cleanup (bypasses event logging)
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db \
  "UPDATE tasks SET status='archived' WHERE status='cancelled';"
```

### Prevention

- When creating replacement coder+reviewer chains, archive the old cards (don't leave them cancelled)
- After bulk operations (ghost sweep, corruption recovery), sweep for cancelled cards
- The `review-failed-watch` cron archives old blocked reviewers automatically

---

## Reviewer Workspace Blindness — 71% of Reviewer Cards Can't Inspect Coder Files

### Problem

When the orchestrator creates a code-reviewer card without specifying `workspace_kind`, the kanban system defaults to `scratch` — a fresh empty temp directory. The reviewer has **no access to the coder's worktree** and cannot inspect actual files, verify code quality, check test results, or confirm that the coder committed their work.

The reviewer can only read the coder's `kanban_complete(summary=...)` and metadata — a self-report that may be inaccurate or incomplete.

### Evidence

As of Aug 2026, 447 out of 626 code-reviewer cards (71%) were created with `workspace_kind=scratch`. Only 179 used `worktree`.

**Production failure (GH-1731, Aug 24 2026):**
- Coder modified files but NEVER committed (`git status` showed dirty worktree)
- Coder called `kanban_complete()` — task marked `done`
- Reviewer spawned with `workspace_kind=scratch` — empty temp dir, no way to see the coder's uncommitted changes
- Reviewer completed in 30 seconds: "Code changes correctly implement LLM context injection... and pass backend tests"
- Result: Coder's work was stranded in a dirty worktree that was later pruned. Zero trace in git history. No PR was ever created. The orchestrator had to manually commit+push the work after discovery.

### Root Cause

The orchestrator's SOUL.md (card body format) and the decomposition instructions in `kanban-orchestrator` skill do not include `workspace_kind="worktree"` when creating reviewer cards:

```python
# Current — reviewer gets scratch (default):
kanban_create(
    title="Review: [GH-42] rate limiter",
    assignee="code-reviewer",
    parents=[coder_id],
)

# Correct — reviewer should inspect the coder's actual work:
kanban_create(
    title="Review: [GH-42] rate limiter",
    assignee="code-reviewer",
    workspace="worktree",                          # <-- required: makes reviewer inspectable
    branch="wt/t_<coder-task-id>",                 # <-- connects reviewer to coder's branch
    parents=[coder_id],
)
```
**Pitfall — SOUL.md text is correct but `prefill_messages_file` is empty.** The orchestrator only loads SOUL.md if `prefill_messages_file: SOUL.md` is set in the profile's `config.yaml`. If the config has `prefill_messages_file: ''` (the default), the SOUL.md sits on disk as a passive document — the orchestrator never sees it. This was the root cause for 447/626 reviewer cards being `scratch`: the SOUL.md had the correct instructions for months, but the config had the empty default. Verify with:

```bash
grep prefill_messages_file ~/.hermes/profiles/orchestrator/config.yaml
# Expected: prefill_messages_file: SOUL.md
```

Fix with:
```bash
hermes config set prefill_messages_file "SOUL.md"
```

### Three-Layer Defense

#### Layer 1 — Card Creation (Orchestrator creates correctly)

Every code-reviewer card MUST include:
1. `workspace="worktree"` — so the reviewer gets a git worktree, not a scratch dir
2. **⚠️ CRITICAL: The reviewer MUST get its OWN unique branch — NOT the coder's branch name.** Git does not allow two worktrees on the same branch simultaneously. Passing `branch=coder_branch` causes `fatal: '<branch>' is already used by worktree at '<coder-worktree>'.` The reviewer should omit `--branch` entirely (dispatcher auto-derives `wt/t_<reviewer-id>`) or use a distinct name like `review/<coder-task-id>`.
3. The card body should include the coder's branch name so the reviewer knows what to inspect

**How reviewers inspect coder files (without checking out the coder's branch):**
   a. Fetch the coder's branch from origin: `git fetch origin wt/t_<coder-id>`
   b. List changed files: `git diff --name-only origin/main..origin/wt/t_<coder-id>`
   c. Read specific file content: `git show origin/wt/t_<coder-id>:path/to/file`
   d. Read the full diff: `git diff origin/main..origin/wt/t_<coder-id>`
   e. Last resort: read the coder's local worktree filesystem at `<repo>/.worktrees/<coder-task-id>/`

```python
# Capture coder info during creation
coder_task = kanban_create(
    title="[GH-42] implement rate limiter",
    assignee="coder",
    workspace="worktree",
    # branch=... or omitted for auto-derive
)
coder_id = coder_task["task_id"]

# Retrieve the branch name from kanban_show (for the card body only)
coder_detail = kanban_show(task_id=coder_id)
coder_branch = coder_detail.get("branch_name", "")

# Create reviewer — MUST get its OWN unique branch (omit --branch or use distinct name)
kanban_create(
    title="Review: [GH-42] rate limiter",
    assignee="code-reviewer",
    workspace="worktree",
    # NO branch= parameter — auto-derives wt/t_<reviewer-id>, avoids worktree collision
    parents=[coder_id],
    body=(
        "Review implementation of [GH-42] rate limiter\n"
        f"Coder task: {coder_id}\n"
        f"Coder branch: {coder_branch} (fetch from origin to inspect)\n"
        "Files: rate_limiter.py, tests/test_rate_limiter.py\n"
        "Verification: 14 tests must pass\n"
        "---\n"
        "Inspection guide:\n"
        f"1. git fetch origin {coder_branch}\n"
        "2. git diff --name-only origin/main..origin/<coder_branch>\n"
        "3. git show origin/<coder_branch>:path/to/file\n"
    ),
)
```

**This MUST be baked into the orchestrator's SOUL.md card body format.** Without it, every decomposition session creates blind reviewers.

#### Layer 2 — Reviewer Workflow (Verify Before Approving)

The reviewer card body should instruct the reviewer to verify the coder's git state before approving:

```bash
# 1. Confirm you're on the coder's branch
git branch --show-current
# Expected: wt/t_<coder-task-id> or fix/<name>

# 2. Check that the coder actually committed
git status --short
# Expected: nothing (empty = all changes committed)

# 3. Check that the commits are not just main already
git log --oneline origin/main..HEAD
# Expected: non-empty (coder's unique commits)

# 4. Check that the branch was pushed
git branch -r --list "origin/$(git branch --show-current)"
# Expected: non-empty (branch exists on origin)

# 5. Read the diff
git diff origin/main..HEAD --stat
```

These 5 commands are the minimum verification before approving. If any step fails, the reviewer should block with `review-failed: commit-pending:` or `review-failed: not-pushed:` or `review-failed: already-on-main:`.

#### Layer 3 — Auditor Detection (Catch existing blind pairs)

Query the kanban DB for done coder+reviewer pairs where the reviewer had `workspace_kind=scratch`. These pairs completed without structural verification — their approvals are not trustworthy:

```bash
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db "
SELECT c.id as coder_id, c.title, r.id as reviewer_id
FROM tasks c
JOIN task_links l ON l.parent_id = c.id
JOIN tasks r ON r.id = l.child_id AND r.assignee = 'code-reviewer'
WHERE c.assignee = 'coder'
  AND c.status IN ('done', 'archived')
  AND r.workspace_kind = 'scratch'
  AND r.completed_at > strftime('%s', 'now', '-30 days')
ORDER BY r.completed_at DESC;
"
```

For each pair returned, the coder's work needs manual verification that it was actually committed and pushed.

---

## Worktree Deploy Guard — Prevent Deploys from Non-Main Refs

### Problem

The `deploy-to-staging` job in `.github/workflows/deploy.yml` accepts a `ref` input parameter via `workflow_dispatch`:

```yaml
- name: "Ref to deploy"
  description: "The branch, tag, or SHA to deploy (e.g., feat/new-feature). Defaults to main."
```

There is **no validation** that the supplied ref is an ancestor of `main`. Any worktree branch, experimental feature branch, or stale fork can be deployed to staging — bypassing PR review, CI testing, and the merge pipeline entirely.

### Evidence

Multiple staging deploys have been triggered from worktree branches (`wt/t_*`, `feature/*`) via `workflow_dispatch`. Staging runs code that was never merged, never reviewed, and never tested with the deploy workflow.

### Defense — Ancestry Check in Deploy Pipeline

Add a validation step in `deploy-to-staging` that checks whether the deployed ref is an ancestor of `origin/main`:

```yaml
- name: Validate ref is an ancestor of main
  if: github.event_name == 'workflow_dispatch'
  run: |
    git fetch origin main --depth=1000
    git merge-base --is-ancestor HEAD origin/main && {
      echo "✅ Ref $(git rev-parse --short HEAD) is an ancestor of origin/main"
    } || {
      echo "❌ BLOCKED: Ref $(git rev-parse --short HEAD) is NOT an ancestor of origin/main."
      echo "Staging deploys must come from main or be merged to main first."
      echo "Ref: ${{ github.ref_name }}  SHA: $(git rev-parse HEAD)"
      exit 1
    }
```

This was previously added as a step but may have been removed or commented out in deploy.yml rewrites. Verify it exists and is active.

The step should:
- **Run only on `workflow_dispatch`** — PR merge events always deploy from main (the merge base), so the check is redundant there
- **Fetch `origin/main` with sufficient depth** — `--depth=1000` ensures the merge-base computation works for branches up to 1000 commits behind
- **Fail the job** if the ref is not an ancestor — this prevents accidental worktree deploys while still allowing overrides by temporarily removing the guard

### Detection

Check the last N staging deploys and verify their refs:

```bash
gh run list --workflow deploy.yml --branch main --event workflow_dispatch \
  --limit 10 --json headBranch,databaseId,headSha --jq \
  '.[] | "\(.databaseId) \(.headBranch) \(.headSha[0:8])"'
```

Any run with `headBranch != "main"` is a worktree deploy. Cross-reference the SHA against main:

```bash
git merge-base --is-ancestor <sha> origin/main && echo "ancestor of main" || echo "NOT ancestor"
```

---

## Post-Deploy Fix Content Verification

### Problem

The QA verification pipeline (`verify-deploy-qa`) performs structural health checks — API reachability, database connectivity, browser load, version comparison. It does NOT verify that specific fixes from merged PRs are actually present in the deployed environment.

A fix can be "on main" (merged via PR) but absent from staging because:
- The deploy was cut from a stale SHA (worktree deploy, or deploy before the fix merged)
- The deploy rolled back silently
- The fix was never in the merge commit's ancestry (squash loss)

### Evidence

**GH-1660 (Aug 22 2026):** Three fixes (Vision Board 404, i18n broken, empty user name) were merged to main on Aug 23. The last staging deploy was from commit `b5380bef` on Aug 21 — both older than the fixes. QA ran, reported "all healthy," but all three findings were still present on staging. The version check passed because `pyproject.toml` said `0.57.1` both before and after.

### Defense — Per-Fix Verification After Each Deploy

After each successful staging deploy, the QA pipeline should:

1. **Read the merge commit's PR body** — parse `Closes #N` references to identify which fixes should be deployed
2. **For each closed issue, attempt fix verification:**
   - **Route fix:** `curl <endpoint>` — check for expected HTTP status and response body
   - **UI fix:** Drive the browser to the relevant page and check for expected element presence/text
   - **i18n fix:** Check `<html lang>` attribute, or fetch a route with `Accept-Language` header
   - **Data fix:** Query the staging DB for expected records
3. **Report per-fix status:** `"GH-1661 (Vision Board): ✅ route exists (HTTP 200)"` or `"GH-1662 (i18n): ❌ lang attribute still hardcoded"`

### Implementation Sketch

```python
def verify_fix(issue_num, fix_type, staging_url):
    """Verify a specific fix on staging. Returns (issue_num, passed, detail)."""
    fix_checks = {
        "GH-1661": lambda: (
            requests.get(f"{staging_url}/vision-board").status_code == 200,
            "Vision Board route"
        ),
        "GH-1662": lambda: (
            "lang=\"en\"" not in requests.get(staging_url).text,
            "Dynamic lang attribute"
        ),
    }
    if issue_num in fix_checks:
        passed, detail = fix_checks[issue_num]()
        return (issue_num, passed, detail)
    return (issue_num, None, "No verification check registered")
```

Each fix should register its verification check when the PR is created. This is the QA equivalent of a test assertion for deployments.

---

## Consolidation Script Silent Skip Notification

### Problem

The `build-consolidate-prs.py` script has several skip paths that produce **no output** — no diagnostic line, no notification, no logged event:

| Skip Condition | Output | Detection Available? |
|---------------|--------|---------------------|
| `get_branch_commit_count()` returns -1 (branch lost) | `"⚠️  Branch {branch} lost — skipping"` only when `skip_lost=False` | ❌ Silent in consolidate mode |
| `get_branch_commit_count()` returns 0 (already on main) | None | ❌ Silent |
| `get_commit_hashes()` returns empty | None | ❌ Silent |
| `dedup_key` already in `seen_commit_sets` | None | ❌ Silent |
| `is_already_in_main()` returns True | None | ❌ Silent |
| `already_has_pr()` returns True | None | ❌ Silent |

A done coder+reviewer pair can be silently skipped by every cron tick, forever, with no indication that a PR was never created. The user only discovers the gap when they notice the GH issue is still open days later.

### Evidence

**GH-1701 and GH-1731:** Both coder+reviewer pairs reached `done` status. The consolidation script ran every 5 minutes for hours, silently skipping both. No notification was ever sent. The only reason the issue was caught was the user manually asking "where is the PR."

### Defense — Counters and Summary at End of Each Run

The script should maintain counters for each skip reason and emit a summary at the end of its run:

```python
def main():
    seen_commit_sets = set()
    created = 0
    # Skip counters
    skip_counters = {
        "branch_lost": 0,
        "branch_empty": 0,
        "dedup_already_pr": 0,
        "already_on_main": 0,
        "pr_already_exists": 0,
        "recovery_succeeded": 0,
        "already_merged": 0,
    }

    # ... (existing logic, increment skip_counters at each skip) ...

    # Final summary
    if sum(skip_counters.values()) > 0 or created > 0:
        total_skipped = sum(skip_counters.values())
        parts = [f"Created {created} PRs"]
        if total_skipped > 0:
            parts.append(f"Skipped {total_skipped} pairs")
        for reason, count in skip_counters.items():
            if count > 0:
                parts.append(f"  • {reason}: {count}")
        print("; ".join(parts))
```

This already works as a `no_agent=True` script — stdout triggers notification. The current script returns silent on zero output. With a summary, every run produces at least one output line.

### Pitfall: False "lost" Warnings for Already-Merged PRs

When a PR merges with `--delete-branch`, the remote branch is deleted. The kanban card stays `done` (not `archived`). The consolidation script re-processes it every tick, fails to find the branch, and prints a misleading `"⚠️  Branch lost — skipping"` warning — even though the work is already on main.

**Fix:** Before reporting "branch lost," check if a merged PR exists for that branch:

```python
def pr_for_branch_is_merged(branch):
    """Check if a PR was already created from this branch and merged."""
    rc, out, _ = run(["gh", "pr", "list", "--state", "merged", "--head", branch,
                      "--json", "number,state", "--jq", "length"], timeout=15)
    if rc == 0 and out.strip().isdigit() and int(out.strip()) > 0:
        return True
    return False
```

If a merged PR is found, **archive the coder card** so the consolidation script never re-processes it:

```python
if pr_for_branch_is_merged(branch):
    skip_counters["already_merged"] += 1
    archive_coder_card(coder_id)
    return False  # silently handled — no "lost" warning
```

This eliminates the false alarm and cleans up stale `done` cards automatically.

### Verification

After the fix, confirm by creating a test case: a done coder+reviewer pair with a deliberately invalid branch name. The script should report:

```
⚠️  Branch wt/t_nonexistent lost — skipping
Skipped 1 pair;   • branch_lost: 1
```

Three additional edge cases are documented in `references/consolidation-fix-edge-cases.md`:
- Cards with null `branch_name` (pre-fix epoch) — auto-archived when GH issue is closed
- Branches deleted after PR merge — detected via `pr_for_branch_is_merged()` instead of falsely reporting "lost"
- Blocked consolidation groups — re-checks whether the GH issue is actually closed before waiting out the 24h cooldown
