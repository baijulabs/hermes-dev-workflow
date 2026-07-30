---
name: deploy-failure-automation
description: "Detect CI/deploy failures, auto-create kanban fix cards, and consolidate completed fixes into a PR — all without human babysitting."
version: 1.8.0
platforms: [linux, macos]
environments: [kanban, github-actions]
---

# Deploy Failure Automation

End-to-end pipeline for detecting failed CI/deploy runs, creating kanban fix cards, and consolidating completed fixes into a PR. The user should never need to check progress — the system either delivers a PR URL or an error alert.

## Architecture

Two pipelines, each running as separate cron jobs:

### Pipeline 1: Deploy-watch (workflow-run monitoring)

```
GitHub Actions run fails (workflow_dispatch or pull_request_target)
        ↓
[1] staging-deploy-watch.py (no_agent=true, polls every 10m)
        ↓ New failure detected → stdout delivered to agent
        ↓
[2] Agent session creates kanban coder+reviewer cards
    + registers PR consolidation cron job
        ↓
Dispatchers process cards → coders fix → reviewers approve
        ↓
[3] pr-consolidate.py (no_agent=true, polls every 15m)
        ↓ All coder cards done → merge worktree branches into PR
        ↓ PR URL or error delivered to user
```

### Pipeline 2: PR-check-watch (open-PR CI and Merge Conflict monitoring)

```
Open PR has a failing CI check or merge conflicts (CONFLICTING / DIRTY)
        ↓
pr-check-watch cron (agent, polls every 15m) — distinct from deploy-watch
        ↓
Detects failure/conflict → dedups against existing kanban cards → creates fix cards
        ↓
Dispatchers process cards → coders resolve/fix → reviewers approve
        ↓
No separate PR creation (the PR branch already exists — fix/conflict resolution is pushed directly to it)
```

See references/pr-check-watch-architecture.md for full detail, references/merge-conflict-auto-resolution.md for the conflict playbook, references/pr-merge-conflict-resolution-recipe.md for the automated conflict resolution recipe, and references/qa-verification-pipeline.md for the post-deploy QA verification agent (two-mode: fix verification + dogfood).

## Components

### Script: `staging-deploy-watch.py`

Path: `~/.hermes/profiles/orchestrator/scripts/staging-deploy-watch.py`

Polls GitHub Actions for the latest completed `workflow_dispatch` and `pull_request_target` runs on the deploy workflow. Tracks `last_run_id` in a state file so it only reports new failures.

**Dedup logic (in the script, before agent runs):** Before outputting failure details, the script checks the kanban board for in-flight fix cards on the same branch:
```python
result = subprocess.run(
    ["sqlite3", str(KANBAN_DB),
     f"SELECT COUNT(*) FROM tasks WHERE branch_name = '{branch}' "
     f"AND status NOT IN ('done','cancelled','archived') AND assignee = 'coder';"],
    capture_output=True, text=True, timeout=10,
)
if int(result.stdout.strip()) > 0:
    return  # silent exit — cards still in flight, let them complete
# No open cards → output failure details for a new fix cycle
```
This is the correct lifecycle: open cards → wait (do nothing). All done/cancelled + still failing → start a new cycle. No infinite re-trigger loops.

**Stale-failure detection (in the script, before agent runs):** Before reporting a `pull_request_target` failure, the script checks if a **newer successful run** exists on the same branch. This prevents false positives when a fix was pushed to the PR branch *after* the failed CI run started but *before* the deploy-watch script polls:

```python
def is_stale_failure(run):
    if run.get("event") == "workflow_dispatch":
        return False  # manual deploys never auto-re-run
    branch = run.get("headBranch", "")
    success_by_branch = get_latest_successful_runs()
    latest_success = success_by_branch.get(branch)
    if not latest_success:
        return False
    return latest_success > run.get("createdAt", "")
```

This prevents the common scenario: a coder pushes a fix to the PR branch, GitHub triggers a new CI run, but the old failed run is still the latest completed run when the deploy-watch polls. Without this check, the agent would create fix cards for an issue already resolved.

**Output behavior for `no_agent: true` cron:**
- Empty stdout = no new failures → silent exit (no delivery)
- Non-empty stdout = failure details → delivered to user as notification

**Event filtering:**
- `workflow_dispatch`: manual staging deploys → always included
- `pull_request_target`: PR CI checks → only included if the PR is still open (via `gh pr list --head <branch> --state open`). Merged/closed PRs are skipped.

**State file:** `~/.hermes/profiles/orchestrator/state/staging-deploy-watch.json`

**DDL migration pitfalls:** See `references/ddl-migration-pitfalls.md` for the `CREATE TABLE IF NOT EXISTS` trap — column constraint changes silently do nothing on existing tables, requiring explicit `ALTER TABLE` statements.

### Agent: Card creation (deploy-watch cron job)

The `staging-deploy-watch` cron job uses `no_agent: true` for the polling script but the script's output is piped into an **agent prompt** that creates the kanban cards. The agent:
1. Reads the failure output from the script
2. Identifies distinct test failures
3. Creates coder+reviewer card pairs for each fix, each with `workspace_kind=worktree` and a descriptive branch name (e.g., `fix/deploy-fail-<timestamp>`)
4. **Registers a `pr-consolidate` cron job watching the new coder cards.** Since `no_agent: true` scripts cannot accept CLI arguments, the agent must create a wrapper shell script that bakes in the card IDs:

   **Pitfall: the wrapper script path.** Cron jobs resolve `script` relative to the profile's `scripts/` directory. For the orchestrator profile, place wrapper scripts at `~/.hermes/profiles/orchestrator/scripts/`. Do NOT use `~/.hermes/scripts/` — that path is not resolved by the cron scheduler.

   ```bash
   # Write the wrapper script — at ~/.hermes/profiles/orchestrator/scripts/
   cat > ~/.hermes/profiles/orchestrator/scripts/pr-consolidate-<epic>.sh << 'WRAPPER'
   #!/bin/bash
   python3 ~/.hermes/profiles/orchestrator/scripts/pr-consolidate.py \
     --epic <epic> \
     --coder-cards <id1> <id2> <id3>
   WRAPPER
   chmod +x ~/.hermes/profiles/orchestrator/scripts/pr-consolidate-<epic>.sh

   # Register the consolidation cron
   cronjob action=create \
     name='pr-consolidate-<epic>' \
     schedule='every 15m' \
     no_agent=True \
     script='pr-consolidate-<epic>.sh' \
     workdir='/path/to/repo' \
     deliver='telegram'
   ```

   The wrapper script pattern is required because `no_agent: true` mode executes the script directly with no argument processing — the fixed args must be in the script body itself.

### Script: `pr-consolidate.py`

Path: `~/.hermes/profiles/orchestrator/scripts/pr-consolidate.py`

When all coder cards are done, consolidates their worktree commits into a PR:

1. Checks kanban DB for all coder card statuses
2. Fetches latest `origin/main`
3. Creates a fresh branch off main
4. Cherry-picks commits from each coder's local worktree branch (`git log <branch> ^main` — the `^main` MUST be a separate CLI argument, not part of the same string)
5. Runs `./run-tests.sh backend -k "test_list_quiz_attempts or test_promote_to_sop"` (or appropriate test filter)
6. Runs `./run-tests.sh frontend-all`
7. If all tests pass: pushes branch, creates PR via `gh pr create`
8. If any step fails: outputs error message (which gets delivered as notification)

**Output behavior:**
- Stdout containing `ERROR:` = failure notification delivered to user
- Stdout containing `PR created:` = success notification with URL
- Empty stdout = not ready yet (silent exit)

**State file:** `~/.hermes/profiles/orchestrator/state/pr-consolidate-<epic>.json`

## Cron Job Delivery Pattern

All cron jobs use `deliver: telegram` (not `local`) so the user actually receives notifications:

```bash
# Create a PR consolidation watcher (wrapped in a shell script)
cronjob(action='create',
  name='pr-consolidate-<epic>',
  schedule='every 15m',
  no_agent=True,
  script='pr-consolidate-<epic>.sh',  # wrapper script that calls pr-consolidate.py with args
  workdir='/path/to/repo',
  deliver='telegram')
```

**Critical: `deliver: local` saves to log only — the user will NEVER see it.** Always set `deliver` to a real messaging platform (`telegram`, `discord`, `all`) for any cron job that should notify the user. If the Telegram gateway is not configured, the delivery silently fails — verify with `hermes config get telegram.enabled` and `hermes config get telegram.allowed_chats`.

## Agent-Side Failure Analysis Flow

When the script detects a new failure (the "script output" arrives as a pre-run context block), the agent must bridge the gap between detection and card creation. This flow — checking PR status, waiting for re-trigger, categorizing root causes, and creating properly paired coder+reviewer cards — is documented in a dedicated reference:

- `references/agent-failure-analysis-flow.md` — step-by-step: reading script context, checking PR merge state, polling for re-trigger completion, the decision tree for genuine vs flake failures, categorizing distinct root causes (DDL vs npm vs missing route), confirming DDL staleness via two-dot git diff, diagnosing npm hoisting via install count, and creating fix cards with verification.

For the concrete example from this session's execution (PR #548, two root causes, two fix card pairs), see the "Example: PR #548 Full Flow" section in that reference.

## Behavior Contract

- **No silent failures.** If the consolidation script encounters a merge conflict, test failure, push error, or any other problem, it outputs `ERROR: <description>` which gets delivered as a notification. The user is never left wondering why nothing happened.
- **No overlapping PRs.** The state file marks `pr_created: true` after successful creation. Subsequent runs exit silently.
- **No duplicate notifications.** The state file's `last_run_id` prevents re-reporting the same deploy failure.
- **Self-cleaning.** The state file is deleted after successful PR creation.
- **Agent tasks register their own consolidation.** When the deploy-watch agent creates fix cards, it also registers the `pr-consolidate` cron job — no manual setup needed.

## Prerequisites

### Telegram delivery
Before any cron job can deliver to Telegram, the config must have:
```yaml
telegram:
  enabled: true
  allowed_chats: '<chat-id>'
```

Check with:
```bash
hermes config get telegram.enabled
hermes config get telegram.allowed_chats
```

If `enabled` is missing or `false`, or `allowed_chats` is empty, notifications will be silently dropped. Fix with:
```bash
hermes config set telegram.enabled true
hermes config set telegram.allowed_chats <chat-id>
systemctl --user restart hermes-gateway
```

The bot token goes in `~/.hermes/profiles/<profile>/.env`:
```
TELEGRAM_BOT_TOKEN=<token>
```

### GitHub CLI auth
The scripts use `gh` CLI for API calls. Must be authenticated:
```bash
gh auth status
```

### The `[GH-N]` pattern side effect (CRITICAL — can silently close open PRs)

The pre-existing `hermes_github_sync.sh` script (part of the `gh-issues-to-kanban` cron job) runs every 15 minutes. It scans ALL `done` kanban cards for the regex pattern `\[GH-\d+\]` in their titles. When it finds a match, it runs:

```bash
gh issue close "$ISSUE_NUM" --repo "$REPO"
```

Since GitHub treats pull requests as issues in its API, **this closes open PRs** whose number matches the `[GH-N]` pattern found in any done card's title. The close event is attributed to the authenticated user. The script also posts a comment: `✅ Automated Resolution: This task was completed by the Hermes agent pool.`

**Prevention (three layers):**

1. **Never use `[GH-N]` in auto-generated card titles.** Deploy-watch and PR-check-watch cron prompts MUST explicitly forbid the `[GH-\d+]` pattern in card titles. Use `[DF-<timestamp>]` or `[PRFIX-<timestamp>]` prefixes instead.

2. **Guard the sync script — skip PRs.** Add a PR check before `gh issue close`:
```bash
if gh pr view "$ISSUE_NUM" --repo "$REPO" --json id &>/dev/null; then
    echo "Skipping #$ISSUE_NUM — it's a pull request, not an issue."
    continue
fi
```

3. **Guard 4: check for in-flight coder children.** Orchestrator epics decompose into coder+reviewer pairs. When the orchestrator card reaches `done`, decomposition is complete — but the coder children may still be running. The sync script must query the kanban DB for child cards still in flight before closing:
```bash
PARENT_IDS=$(sqlite3 "$KANBAN_DB" \
  "SELECT id FROM tasks WHERE title LIKE '%[GH-$ISSUE_NUM]%' AND status='done' AND assignee='orchestrator';")
if [ -n "$PARENT_IDS" ]; then
  for pid in $PARENT_IDS; do
    IN_FLIGHT=$(sqlite3 "$KANBAN_DB" \
      "SELECT COUNT(*) FROM task_links l JOIN tasks c ON c.id=l.child_id
       WHERE l.parent_id='$pid' AND c.status NOT IN ('done','archived','cancelled') AND c.assignee='coder';")
    [ "$IN_FLIGHT" -gt 0 ] && echo "Skipping #$ISSUE_NUM — $IN_FLIGHT coder child(ren) in flight." && continue 2
  done
fi
```
Real-world failure: Issue #806 closed 5 seconds after orchestrator finished decomposition, before any coder started. Without Guard 4, issues close prematurely.

**Post-mortem detection:** Check close events:
```bash
gh api "repos/<owner>/<repo>/issues/<pr-number>/events" --jq '.[] | select(.event=="closed") | "\(.actor.login) at \(.created_at)"'
```

If `closed by user` at 15-minute intervals, the sync script is the culprit. Cancel the offending done card:
```bash
sqlite3 $KANBAN_DB "UPDATE tasks SET status='cancelled' WHERE title LIKE '%[GH-$PR_NUM]%' AND status NOT IN ('cancelled','archived');"
```

## Pitfalls

**`^main` must be a separate argument in subprocess calls.** When calling `git log branch ^main` from Python's `subprocess.run`, the `^main` must be a separate list element:
```python
# CORRECT — works
subprocess.run(["git", "log", "--format=%H", branch, "^main"])

# WRONG — fails with "unknown revision"
subprocess.run(["git", "log", "--format=%H", f"{branch} ^main"])
```

**Worktree branches may be local-only.** The kanban coder lifecycle commits to the worktree branch but does NOT push to origin. The `pr-consolidate.py` script fetches from origin first, then falls back to local branch refs. For this to work, the local branches must still exist (they are not garbage-collected).

**Git stash pop clobbers rebase results — recovery.** If you `git stash` during a rebase, `git stash pop` restores the pre-rebase working tree state, overwriting the rebased file content. To recover:
```bash
git checkout HEAD -- <file>   # restore committed version (drops stash-pop changes)
```
The committed version has the correct content from the rebase. Do NOT `git stash drop` until you've verified the checkout restored the right version. This happens because stash saves the working tree, and popping it after a rebase re-applies the old working tree on top of the rebased history — the files on disk look "right" to the user but don't match HEAD.

**Agent-based cron jobs can be slow to start.** An agent cron job may take 30+ seconds to load skills, model, and context. For a 10-minute schedule, the actual interval between checks can be 10+ minutes. For faster polling, use `no_agent: true` scripts.

**The kanban-orchestrator skill is protected.** It contains the PR consolidation rule ("orchestrator creates one PR per epic") but the implementation details belong in this sibling skill. Do not patch kanban-orchestrator — add to this skill instead.

**`NOW()` vs `clock_timestamp()` in PostgreSQL test suites.** When a test fixture intercepts `conn.commit()` as a no-op (like MyProject's `_SavepointSession`), the entire test runs in a single transaction. PostgreSQL's `NOW()` returns the transaction start time — all INSERTs in the same test get the identical timestamp. This breaks `ORDER BY created_at DESC` because multiple rows share the same timestamp. The fix is twofold:\n  - Use `clock_timestamp()` (statement time, not transaction time) in the INSERT statement: `INSERT INTO ... VALUES (..., clock_timestamp())`\n  - Never rely on `DEFAULT NOW()` in the column definition — `CREATE TABLE IF NOT EXISTS` doesn't alter existing tables, so a DDL change to `DEFAULT clock_timestamp()` has no effect on existing test databases. The INSERT-level change is the only reliable fix.

**Branch name collision between worktrees.** Auto-generated fix cards must use unique branch names per batch. A date-based name like `fix/deploy-fail-20260721` collides with existing worktrees from earlier runs. Use `fix/deploy-fail-<unix-timestamp>-<random-suffix>` instead. The pr-consolidate.py script creates a fresh branch off main (deleting the old one), but the dispatcher's worktree creation fails hard when the branch is already checked out by another worktree — and the card ends up `blocked` with a `spawn_failed` outcome.

**Duplicate card creation by PR-check watcher.** A separate cron job (`pr-check-watch`) monitors open PRs for failing CI checks. The deploy-watch script handles dedup at the script level (checks kanban for in-flight cards on the same branch before outputting any failure details). The PR-check-watch agent must also check for existing open PRs or kanban cards addressing the same failure. The correct dedup lifecycle: if fix cards are still in flight (status=ready/running, not done/cancelled), do nothing — let them complete. Only when all previous cards are done/cancelled and CI still fails, start a new cycle. Do NOT re-trigger CI on the same branch.

**`hermes gateway restart` times out via CLI.** The CLI command `hermes gateway restart` may hang for 180+ seconds or timeout entirely. Use systemctl instead for faster, reliable restarts:
```bash
systemctl --user restart hermes-gateway
```

**Fix-revert investigation: CI still failing despite a fix commit in the PR history.** When a CI check is still failing and the PR branch already contains a commit whose subject line matches the expected fix, the fix may have been reverted by a later commit. This happens when a squash-merge sequence or a rebase conflict resolution restores the old code. To investigate:
  1. List commits on the PR branch: `gh pr view <N> --json commits --jq '.commits[] | "\\(.oid[:8]) \\(.messageHeadline)"'`
  2. Find the fix commit and the commit after it
  3. `git diff <fix_commit>..<next_commit>` — if the diff shows the fix being undone (e.g., `INTEGER`→`INTEGER NOT NULL`), the fix was reverted
  4. Create a new kanban card to re-apply the fix, this time also checking that **all** related code paths are updated (function signatures, call sites, not just the DDL). A `CREATE TABLE IF NOT EXISTS` DDL change alone is not sufficient if the calling code also needs updating — e.g., a function signature that still requires `int` (not `int = None`) will reject the None value even if the column is nullable.

**Stale failure delivery to agent — fix already on branch.** The deploy-watch script may detect a `pull_request_target` failure where a fix commit was pushed to the branch *after* the failed run started but *before* the script polls. If the script's `is_stale_failure()` check (added in v1.5.0) is working correctly, it silences these. However, if the agent still receives a stale failure (e.g., from a script version before the fix was deployed), the agent must verify the current state before creating cards:

  1. Check the latest CI run on the same branch: `gh run list --branch <branch> --limit 3 --json conclusion,createdAt`
  2. If a newer run succeeded, **do not create fix cards**. The issue is already resolved. Output `[SILENT]` or a brief status report explaining the fix was already applied.
  3. If the newer run also failed (or is still running), proceed with normal card creation.
  4. Never create fix cards for a stale failure — doing so wastes coder capacity and creates redundant worktrees that collide with the existing PR branch.

**`actions/cache` write denied on `pull_request_target` events — \"token has no writable scopes\".** The `GITHUB_TOKEN` in `pull_request_target` events lacks the `actions: write` scope by default. Without it, `actions/cache` fails when trying to save venv/node_modules caches after lint/test jobs, producing: `Warning: Failed to save: Unable to reserve cache ... cache write denied: token has no writable scopes`.\n\n**Two-part fix:**\n\n1. **Add `actions: write` to top-level AND every job-level `permissions` block.** Job-level blocks override the top-level — each job that uses `actions/cache` must individually declare `actions: write`. Check `grep -n 'permissions:' deploy.yml` to find all job-level blocks.\n\n2. **Cache writes are STILL blocked by GitHub on `pull_request_target` events** regardless of permissions — this is a platform security restriction to prevent cache poisoning in privileged workflows. The only way to prime caches is a `push` event on `main`. Add a lightweight `cache-primer` job that only runs on push-to-main and gate ALL heavy jobs with `if: github.event_name != 'push'`:\n\n```yaml\non:\n  push:\n    branches:\n      - main\n\njobs:\n  cache-primer:\n    if: github.event_name == 'push' && github.ref == 'refs/heads/main'\n    steps:\n      - uses: actions/setup-python@v6\n        with: { python-version: '3.12', cache: 'pip' }\n      - run: pip install -r backend/requirements.txt\n      - uses: actions/setup-node@v5\n        with: { node-version: '22', cache: 'npm' }\n      - run: npm ci\n  \n  backend-fast-test:\n    if: github.event_name != 'push' && (needs.changes.outputs.backend == 'true' || ...)\n  # ... all other heavy jobs gated the same way\n```\n\nNet cost: ~5 min of pip/npm install per merge. Break-even: 1 PR sync (saves ~5 min of cold install). Every additional PR sync is net savings.

**`dorny/paths-filter` produces empty diff on `pull_request_target closed` events — deploy always skipped.** The `deploy-to-staging` job condition requires `needs.changes.outputs.backend == 'true'` (or similar path-filter checks). On `pull_request_target closed` events (PR merges), the PR head commit is already an ancestor of main — `git diff` between them is empty, so dorny/paths-filter outputs `'false'` for all paths. `deploy-to-staging` is **always skipped** on merge events.

**Fix:** Add `|| github.event.action == 'closed'` to the path-filter condition so merged PRs bypass the filter:

```yaml
(needs.changes.outputs.backend == 'true' || ... || needs.changes.outputs.config == 'true' ||
 github.event.action == 'closed')
```

**Why this is safe:** A merged PR had code changes (that's why it was merged). The paths-filter was meant to skip docs-only merges. PR merges with only docs changes are rare and the cost of an unnecessary deploy is lower than silently skipping all code deploys.

**Diagnosis:** Check the `Deploy to Staging` job on any `pull_request_target closed` run — it will be `skipped`. Then verify the diff is empty:
```bash
PR_HEAD=$(gh pr view <N> --json headRefOid --jq '.headRefOid')
git diff --name-only $(git merge-base origin/main $PR_HEAD)..$PR_HEAD  # empty if merged
```

**Auto-decomposer assigns children to `orchestrator` instead of `coder`.** When the kanban auto-decomposer (`auxiliary.kanban_decomposer`) processes a card into child tasks, it may assign implementation children to `orchestrator` instead of `coder`. This causes orchestrator workers to spawn for implementation work — they try to decompose further instead of writing code. The card may succeed (if the orchestrator happens to implement), but more often it loops or produces wrong results.

**Detection:** Check for cards with `assignee = 'orchestrator'` and titles that describe implementation work (not decomposition):
```sql
SELECT id, title, status, assignee FROM tasks 
WHERE assignee = 'orchestrator' AND status IN ('todo','ready','running')
AND title NOT LIKE '%Review%' AND title NOT LIKE '%[GH-%' AND title NOT LIKE '%Epic%';
```

**Fix:** Reassign implementation cards to `coder` and create paired reviewer cards:
```sql
UPDATE tasks SET assignee = 'coder' WHERE id = '<stuck-card-id>';
```
Then create a reviewer: `hermes kanban create "Review: <title>" --assignee code-reviewer --parent <id>`.

**Cron model drift guard blocks agent jobs when global default changes.** When a cron job was created without an explicit `model`/`provider` (inheriting the global default), and the global default model later changes, Hermes blocks the job with: `Skipped to prevent unintended spend: global inference config drifted`. This prevents an agent job from silently running with a more expensive model than intended.

**Fix:** Pin the model explicitly on all agent cron jobs:
```json
"model": "deepseek/deepseek-v4-flash",
"provider": "openrouter"
```
Check for unpinned jobs with:
```bash
python3 -c "import json; d=json.load(open('cron/jobs.json')); [print(j['name']) for j in d['jobs'] if not j.get('no_agent') and not j.get('model')]"
```

**PR ingestion: sync script now ingests labeled PRs alongside issues.** The `gh-issues-to-kanban` cron's `hermes_github_sync.sh` has a second ingestion path (section 1b) that pulls open PRs with the `ready-for-agent` label, extracts the latest review comment, and creates an orchestrator kanban card. This works alongside `pr-check-watch` (which handles automated CI/conflict detection) — review feedback is the human-initiated path. Both deduplicate by branch name to prevent double-work. The PR path uses `[PR #N]` prefix (not `[GH-N]`) to avoid the auto-close side effect.