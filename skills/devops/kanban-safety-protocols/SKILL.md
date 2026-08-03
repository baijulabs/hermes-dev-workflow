---
name: kanban-safety-protocols
description: Safety guardrails for kanban task execution — branch protection (prevent commits to main/wrong branches), worktree verification, and cross-cutting safety patterns that protect the repo from automation errors.
version: 2.9.0
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
- When `$HERMES_KANBAN_BRANCH` is NOT set: `git worktree add <path> wt/$HERMES_KANBAN_TASK` (creating from HEAD)

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

## Coder Review-Required Block Auto-Complete

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
