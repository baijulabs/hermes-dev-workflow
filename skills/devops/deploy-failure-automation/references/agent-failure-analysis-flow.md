# Agent-Side Failure Analysis Flow — Script Output to Fix Cards

This reference fills the gap between the polling scripts (`staging-deploy-watch.py`, `pr-check-watch`) detecting a failure and the agent creating kanban fix cards. The scripts detect and deduplicate at the script level; the agent must analyze root causes and create properly paired coder+reviewer cards.

## Entry Points

| Entry point | Source | Script output shape |
|---|---|---|
| **Deploy-watch** | `staging-deploy-watch.py` (every 10m) | Run URL, branch, event type, conclusion. Script may have already re-triggered CI (step 2 dedup). |
| **PR-check-watch** | `pr-check-watch` cron (every 15m) | PR number, branch, failing check names, run URLs. |

Both entry points converge at the same agent-side analysis flow once the script dedup passes and a new failure is confirmed.

## Step-by-Step Agent Flow

### Step 1: Read Script Context

The script output arrives as a pre-run context block. Extract:

```
Branch: fix/df-1784774204-save-values-v2
PR: #548 (if applicable)
Run URL: https://github.com/.../actions/runs/30031575376
Re-triggered CI: yes (if the script already ran step 2 dedup)
```

If the script says "Re-triggered CI" — the re-trigger may still be in progress. Do NOT immediately create cards.

### Step 2: Check PR Status (Critical — Determine if Genuine Failure)

Check the PR's native check status AND merge state:

```bash
# PR's official status check rollup
gh pr view <N> --json statusCheckRollup,mergeStateStatus

# List specific check conclusions
gh pr checks <N>
```

**Merge state matters:**
- `DIRTY` (merge conflicts) → `pull_request_target` checks may fail because the merge commit has different code than branch HEAD. If a `workflow_dispatch` on the same branch passes, the failures are merge-conflict artifacts, not code bugs.
- `CLEAN` → failures are genuine code issues on the branch.

**Decision tree (from `project-operations` skill):**
| Re-trigger result | PR checks | Merge state | Action |
|---|---|---|---|
| `success` | Still red | `DIRTY` | No fix cards — PR needs rebase to resolve conflicts and refresh checks |
| `success` | Still red | `CLEAN` | Infrastructure discrepancy — push trivial amendment or rebase to re-trigger |
| `failure` | Still red | Any | **Genuine unresolved failure** — proceed to root cause analysis |
| `failure` or `success` | Green | Any | Deploy-only flake — skip card creation, mark as resolved |

### Step 3: If Re-trigger In Progress — Wait for Completion

```bash
# Poll until the re-triggered run completes
for i in $(seq 1 20); do
  conclusion=$(gh run view <RUN_ID> --json conclusion,status \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('conclusion','') or d.get('status',''))")
  if [ "$conclusion" = "success" ] || [ "$conclusion" = "failure" ]; then
    break
  fi
  sleep 15
done
```

Then re-check the decision tree from Step 2.

### Step 4: Get CI Logs for Failed Checks

Get the full failure context:

```bash
# For pull_request_target failures (PR's native checks)
gh run view <RUN_ID> --log-failed

# For workflow_dispatch failures (deploy stage)
gh run view <DEPLOY_RUN_ID> --log-failed
```

Look for specific error messages:
- `UndefinedColumn: column "label" of relation "value_options" does not exist` → DDL staleness
- `ERR_MODULE_NOT_FOUND: Cannot find package 'vue'` → npm hoisting issue
- `Cannot find module` → missing dependency or workspace misconfiguration
- `ImportError` / `SyntaxError` → code-level bug
- `401` / `403` → auth/permissions issue

### Step 5: Categorize Distinct Root Causes

Group failures by root cause, NOT by test name or job name. One root cause may manifest in multiple jobs:

| Root cause | Jobs affected | Same card? |
|---|---|---|
| DDL staleness (missing column) | Backend Fast Tests | One card for database.py fix |
| npm hoisting (vue not found) | Frontend Unit Tests + Docker build | One card for check-deps.sh fix |
| Missing route | Backend Fast Tests | Separate card |
| Wrong HTTP status | Backend Fast Tests | Separate card |
| Wrong response shape | Backend Fast Tests | Separate card |

**Per-root-cause dedup check:** Before creating cards, query the kanban board for cards whose body references the PR branch AND the same root cause category. Use both branch match AND keyword match:

```bash
sqlite3 ~/.hermes/kanban/boards/my-project-dev/kanban.db "
SELECT id, title, status FROM tasks
WHERE status NOT IN ('done','cancelled','archived')
AND (body LIKE '%$PR_BRANCH%')
AND (body LIKE '%database.py%' OR body LIKE '%label%')
"
```

Only create cards for root causes that have NO matching in-flight fix card.

### Step 6: For DDL Staleness — Confirm via Git Diff

When a test inserts into columns that don't exist, check if it's stale-base clobber (branch's DDL is from old main) vs missing code:

```bash
# Two-dot diff (branch vs main — use this, NOT three-dot)
git diff origin/main..origin/$BRANCH -- backend/database.py

# Check if the branch has ANY commits touching database.py
git log --oneline origin/$BRANCH --not origin/main -- backend/database.py

# Check what the branch's DDL actually has
git show origin/$BRANCH:backend/database.py | grep -A5 "CREATE TABLE.*value_options"

# Compare against what the test expects
git show origin/$BRANCH:backend/tests/test_step1.py | grep "INSERT INTO value_options"
```

If `git log --oneline` returns empty (no branch-specific commits touch database.py) AND the two-dot diff shows the branch is missing columns main added → **stale-base clobber**. The fix is to restore main's DDL on the branch.

### Step 7: For npm Hoisting — Check Install Count

When the error is `ERR_MODULE_NOT_FOUND: Cannot find package 'vue'` (or similar workspace-hoisted dep):

```bash
# In the CI log, count the number of npm install calls
# One install from root = correct
# Two installs (root + frontend/) = check_frontend_deps triggered reinstall
```

Look for this pattern in the CI log:
```
⚠️  Frontend dependencies are missing or incomplete. Running a clean install...
(cd frontend && npm install --legacy-peer-deps)   # ← THIS breaks hoisting
```

The fix is documented in `references/ci-npm-troubleshooting.md` section 5: change the sentinel from `@vitest/coverage-v8` (hoisted to root) to `vue-router` (stays in workspace) and install from PROJECT_ROOT.

### Step 8: Create Fix Cards with Proper Pairing

For each distinct root cause, create:
1. One **coder card** with `workspace_kind=worktree`, unique branch name (`fix/df-<timestamp>-<short-name>`)
2. One **reviewer card** with `--parent <coder_id>` (no branch_name — defaults to scratch)

**Card body must include:**
- Goal (one sentence)
- Files to Modify (exact path list)
- Verification criteria (test command or expected behavior)
- Context (root cause, error message, where to look)

**Verify after creation:**
```bash
# Body was stored
sqlite3 $KANBAN_DB "SELECT length(body) FROM tasks WHERE id='<card_id>';"  # must be > 0

# Parent linkage stored
sqlite3 $KANBAN_DB "SELECT parent_id, child_id FROM task_links WHERE child_id='<reviewer_id>';"
```

### Step 9: Report

Output the summary with:
- Branch and PR link
- Distinct root causes identified
- Cards created (coder+reviewer pairs with IDs)
- Any prior fix cycles that were found stale/already-pushed

## Example: PR #548 Full Flow

```
Script output: CI failure on fix/df-1784774204-save-values-v2 — open PR #548 exists
               Re-triggered CI on fix/df-1784774204-save-values-v2

Agent:
1. Checked PR status → mergeStateStatus=DIRTY, Backend Fast Tests=FAIL, Frontend Unit Tests=FAIL
2. Waited for re-trigger run 30033303646 → failure conclusion
3. Re-checked decision tree → genuine unresolved failure
4. Fetched CI logs → 2 root causes:
   a. DDL staleness: UndefinedColumn on value_options.label
   b. npm hoisting: ERR_MODULE_NOT_FOUND for vue from @vitejs/plugin-vue
5. Confirmed DDL via two-dot diff → branch's database.py missing label column
6. Confirmed hoisting via CI log → check_frontend_deps triggered reinstall from frontend/
7. Created 2 coder+reviewer card pairs:
   - [DF-1784831327] t_55a3e5c2 (coder) + t_182898b9 (reviewer)
   - [DF-1784831364] t_53a3316c (coder) + t_8ee4fb32 (reviewer)
```