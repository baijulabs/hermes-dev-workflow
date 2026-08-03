---
name: kanban-blocked-task-diagnosis
description: "Diagnose root causes of blocked kanban tasks by reading worker logs, task events, and DB state. Structured diagnosis order for common failure patterns: missing skills, provider/auth crashes, corruption side effects, review-failed findings, spawn failures, wrong-assignee from auto-decomposer, and post-recovery worktree audit for PR gaps."
version: 2.16.0
---

# Kanban Blocked Task Diagnosis

Diagnose why kanban tasks are stuck without guessing. Use this when reviewing blocked cards or when the dispatcher cycles tasks back to `blocked` without completing them.

## Cron-mode restrictions

When running as a cron job (no user present), `execute_code` is blocked — it raises `"BLOCKED: execute_code runs arbitrary local Python (including subprocess calls that bypass shell-string approval checks). Cron jobs run without a user present to approve it."` All diagnostic queries in this skill use `sqlite3` and `bash` through the terminal tool, which works fine in cron mode. Do NOT attempt to use `execute_code` for Python-based DB manipulation or file reading/writing in cron jobs — fall back to inline `python3 -c "..."` one-liners or direct `sqlite3` commands via the terminal tool.

## Diagnosis Order (always follow this sequence)

1. **DB integrity + task count** — a corrupt or empty DB silently blocks all routing.
   ```bash
   sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db "PRAGMA integrity_check;"
   ```
   If not `ok`, recover first (see `kanban-system-health`'s Recovery sections). If `ok` but `SELECT COUNT(*) FROM tasks` returns 0, the board was rebuilt without loading data — restore from the best backup (see `kanban-system-health`'s "Recovery: Clean DB With 0 Tasks (Empty Rebuild)" for the full procedure: find backup candidates by integrity + task count, copy over live DB, remove stale locks, restart gateway, reset stale running tasks).

2. **Worker logs** — the last line of every worker log contains the crash reason.
   ```bash
   ls -lt ~/.hermes/kanban/boards/<board-slug>/logs/ | head -10
   tail -5 ~/.hermes/kanban/boards/<board-slug>/logs/<task-id>.log
   ```

3. **Task events** — the full lifecycle of the task.
   ```bash
   sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db \
     "SELECT task_id, kind, payload, created_at FROM task_events WHERE task_id='<id>' ORDER BY created_at;"
   ```

4. **Reviewer comments** — for `review-failed` tasks, the structured findings.
   ```bash
   sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db \
     "SELECT body FROM task_comments WHERE task_id='<id>' ORDER BY created_at DESC LIMIT 1;"
   ```

### Diagnosis Shortcut: Dispatcher "stuck" Warning

The gateway log warning `"ready queue non-empty for N consecutive ticks but 0 workers spawned"` is **not always a real issue**. Common causes that are NOT provider failures:

| Cause | How to confirm | Fix |
|-------|---------------|-----|
| `active_pr` guard | Check for repeated `respawn_guarded` events with reason `active_pr` (see Pattern 10) | Move to `triage` — handled by `active-pr-guard-watch` cron |
| Guarded by `respawn_guarded` with other reason | Check task events for `respawn_guarded` | Investigate the guard reason |
| Only 1 `ready` card that's been completing fine | Check if the card has a completed worker run (PR exists) | No action needed — guard is working correctly |

**Do not** immediately investigate provider/credentials when the "stuck" warning fires — check the task events first. If the only `ready` card has `respawn_guarded` events, the guard is the cause, not the provider.

## Pattern 1: `Error: Unknown skill(s): <name>`

**Symptoms:** Coder tasks crash on spawn. `last_failure_error` contains `Error: Unknown skill(s): <skill-name>`. `consecutive_failures >= 3`.

**Root cause:** The task was created with `--skill <name>` but that skill doesn't exist in the target profile's skills directory. The skill exists under the orchestrator profile (where decomposition happened) but wasn't copied to the worker profile.

**Fix:**
```bash
# Verify the skill exists in the worker profile
ls ~/.hermes/profiles/<profile>/skills/*/<skill>/   # if empty → missing

# Copy it from the orchestrator profile
cp -r ~/.hermes/profiles/orchestrator/skills/<category>/<skill> \
  ~/.hermes/profiles/<profile>/skills/<category>/<skill>

# Unblock all affected tasks
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db \
  "UPDATE tasks SET status='todo', consecutive_failures=0, last_failure_error=NULL, worker_pid=NULL WHERE status='blocked' AND assignee='<profile>' AND last_failure_error LIKE '%Unknown skill%';"
```

**Prevention:** Before creating cards with `--skill`, verify the skill exists in the target profile:
```bash
ls ~/.hermes/profiles/coder/skills/*/<skill>/ 2>/dev/null || echo "MISSING — copy from orchestrator"
```

This check belongs in the **orchestrator's decomposition step** (profile audit). The orchestrator should verify skill existence BEFORE creating cards, not after workers crash. During decomposition, after discovering available profiles, also check which skills each profile has:

```bash
for profile in coder code-reviewer; do
  echo "=== $profile skills ==="
  ls ~/.hermes/profiles/$profile/skills/*/ 2>/dev/null
done
```

If a card needs a skill that the target profile doesn't have, copy it from the orchestrator's skills directory before creating the card. This prevents the `Error: Unknown skill(s)` crash entirely.

**Upstream reviewer re-dispatch:** After fixing the missing skill and unblocking the coder, the coder re-dispatches and produces code. The paired reviewer card may still be blocked with findings from the original ghost (uncommitted code). Check the reviewer's state:

- **Reviewer was `ready` (never dispatched)** — the coder's code was uncommitted at the time the reviewer became ready. After committing the code (see Pattern 4b), the reviewer can be marked `done` since the code now exists.
- **Reviewer was `blocked` with genuine findings** (e.g. missing v-if, missing loading state) — the re-dispatched coder may have fixed these. Reset the reviewer to `todo` so it re-checks the new code:
  ```bash
  sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db \
    "UPDATE tasks SET status='todo', consecutive_failures=0, last_failure_error=NULL, block_kind=NULL WHERE id='<reviewer-id>';"
  ```
  Do NOT mark the reviewer `done` — the findings were correct at the time and the new code needs a fresh review.

**Coder re-dispatched but reviewer already had findings:** This is the most common pattern after a missing-skill fix. The coder unblocks, re-dispatches, and produces code. The old reviewer findings may no longer apply. Always reset the reviewer to `todo` (not `done`) to get a fresh review pass.

## Pattern 2: `pid <n> not alive` (worker exits before connecting — or corruption side effect)

**Symptoms:** `last_failure_error` is `pid <n> not alive`. The dispatcher started the worker process but it exited before connecting back to the kanban system.

**Two distinct root causes — must distinguish them:**

### Cause 2a: Genuine worker failure (missing skill, provider crash, OOM)

**Most common:** The worker crashed on spawn before it could write to the task row. The crash reason is in the worker log, not the DB.

Check in order:
1. **Missing skill** (check pattern 1 first — this is the most common cause)
2. **API key / provider issue** — check the gateway environment: `tr '\\0' '\\n' < /proc/{gateway-pid}/environ | grep OPENROUTER`
3. **OOM** — process killed by the kernel. Check `dmesg | grep -i oom` or host metrics.
4. **Signal** — process killed by OS or another process (rare).

### Cause 2b: SQLite corruption side effect (multi-task, no real error)

**Critical distinction:** When ALL workers from DIFFERENT profiles (coder + code-reviewer) crash simultaneously with `pid not alive`, the root cause is ALMOST CERTAINLY DB corruption, not individual worker failures. The corruption prevents workers from writing heartbeats, so the dispatcher reclaims them.

**Diagnosis:** Check the DB integrity:
```bash
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db "PRAGMA integrity_check;"
```
If not `ok`, the corruption IS the root cause. Do NOT investigate individual worker logs.

**Sub-step: Find the best recovery candidate**

When the live DB is completely corrupted (file header is overwritten with non-SQLite data), locate the best backup:

```bash
# Check all backup files for valid SQLite headers
for f in ~/.hermes/kanban/boards/<board-slug>/*.bak ~/.hermes/kanban/boards/<board-slug>/kanban.db.* ~/.hermes/kanban/boards/<board-slug>/kanban_fixed*; do
  [ -f "$f" ] && [ -s "$f" ] && { h=$(xxd -l 16 "$f" 2>/dev/null | awk '{print $2$3$4$5}'); s=$(stat -c%s "$f" 2>/dev/null); [ "$h" = "53514c6974652066" ] && echo "SQLITE  $s  $(basename "$f")"; }
done
```

For each candidate with a valid header, check integrity and data count:
```bash
for f in <candidate1> <candidate2>; do
  echo "=== $(basename $f) ==="
  sqlite3 "$f" "PRAGMA integrity_check;"
  sqlite3 "$f" "SELECT COUNT(*) FROM tasks;"
done
```

**Prefer the candidate with the most data AND clean integrity.** If the candidate with the most data is malformed, try `sqlite3 .recover` first (reads rows directly from b-tree pages, preserving more data than `.dump`). If `.recover` also fails, fall back to `.dump` and replace `ROLLBACK -- due to errors` with `COMMIT;` — the data may load cleanly even if the indexes are corrupted. Fall back sequentially through backup candidates sorted by data count descending.

**Fix — three-category unblock after DB recovery:**

After DB recovery (`.recover` → rebuild), blocked tasks fall into three categories requiring different unblock SQL:

```bash
# 1. "pid not alive" — workers were running when corruption hit, dispatcher reclaimed them.
#    Reset to 'ready' so they get retried immediately.
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db \
  "UPDATE tasks SET status='ready', consecutive_failures=0, claim_lock=NULL, claim_expires=NULL, worker_pid=NULL, last_failure_error=NULL, current_run_id=NULL WHERE status='blocked' AND last_failure_error LIKE '%pid % not alive%';"

# 2. "never dispatched" — reviewer tasks with no claim_lock, no worker_pid, no error.
#    Their coder parent is DONE but the corrupt index prevented promotion.
#    Reset to 'todo' — the dispatcher's recompute_ready pass auto-promotes them.
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db \
  "UPDATE tasks SET status='todo', consecutive_failures=0, claim_lock=NULL, claim_expires=NULL, worker_pid=NULL, last_failure_error=NULL WHERE status='blocked' AND claim_lock IS NULL AND worker_pid IS NULL AND (last_failure_error IS NULL OR last_failure_error = '');"

# 3. Stale "running" tasks — workers that were in-flight when corruption hit.
#    Reset to 'todo' so the dispatcher can reclaim them.
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db \
  "UPDATE tasks SET status='todo', consecutive_failures=0, claim_lock=NULL, claim_expires=NULL, worker_pid=NULL, last_failure_error=NULL, current_run_id=NULL WHERE status='running';"
```

**Verify:**
```bash
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db \
  "SELECT status, COUNT(*) FROM tasks GROUP BY status ORDER BY 2 DESC;"
```

Expected: no `blocked` or `running` tasks remaining. 71+ corrupt backups in the board directory is the definitive signal that this was corruption, not a genuine worker failure — clean them up with `ls -1t kanban.db.corrupt.*.bak | tail -n +4 | xargs rm -f`.

### Post-Recovery: Audit Worktree Branches for PR Gaps

After corruption recovery and unblocking, worktree branches that had unique commits BEFORE the corruption may still be sitting on disk without PRs. Workers completed their code, but the orchestrator never processed the completed tasks because the corrupt index prevented it.

**Procedure:** See `references/post-recovery-worktree-audit.md` — full audit script with categorization, consolidation steps, and PR creation commands.

**Shortcut for independent worktrees:** If each worktree branch touches different files (no conflicts), you can push directly as PR branches instead of cherry-picking. See `references/worktree-to-pr-shortcut.md` for the direct-push workflow and when to use it vs cherry-pick.

## Pattern 3: Blocked with no error — never dispatched

**Symptoms:** Task is `blocked` with `consecutive_failures=0`, `claim_lock=NULL`, `worker_pid=NULL`, `last_failure_error=NULL`. Parent task is `done`. This is the classic "corruption prevented promotion" pattern.

**Root cause:** The corrupt index prevented the dispatcher's `recompute_ready` pass from promoting the reviewer from `todo` to `ready`. The coder parent completed, but the promotion SQL couldn't write to the broken index.

**Diagnosis:** Check the parent link:
```bash
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db \
  "SELECT p.id, p.status, p.title FROM task_links tl JOIN tasks p ON tl.parent_id = p.id WHERE tl.child_id = '<task-id>';"
```
If the parent is `done`, the corruption is the cause.

**Fix:** Reset to `todo` — the dispatcher's `recompute_ready` pass handles the rest. See Category 2 in the fix above.

## Schema note: finding the block reason and checking reviewer status

**New pitfall:** When querying `task_events`, always select `task_id` explicitly — `id` is the event's own rowid (integer), not the task foreign key. Using `SELECT id FROM task_events` returns event numbers (e.g. 14079), NOT task IDs (e.g. `t_cb9app7f`). Joining on the wrong column silently returns empty results. See `references/kanban-db-schema-diagnostics.md`'s ⚠️ Common Query Pitfall section for full before/after SQL.

The `tasks` table has a `block_kind` column (typed reason: `review-failed`, `needs_input`, `dependency`, etc.) but NO `block_reason` column. The detailed reason text is stored in `task_events`. Also, the `task_events` table uses `kind` (not `event_type`) for the event type, and `task_links` uses `parent_id` / `child_id` (not `task_id` / `parent_id`).

### Quick diagnostic: find all blocked reviewer cards with block reasons

```bash
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db \
  "SELECT e.task_id, t.title, t.block_kind, json_extract(e.payload, '$.reason') as block_reason, e.created_at FROM task_events e JOIN tasks t ON e.task_id = t.id WHERE e.kind = 'blocked' AND t.status = 'blocked' AND t.assignee = 'code-reviewer' ORDER BY e.created_at DESC;"
```

### Check reviewer status with parent status in one query

```bash
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db \
  "SELECT r.id AS reviewer_id, r.title AS reviewer_title, r.status AS reviewer_status, p.id AS parent_id, p.title AS parent_title, p.status AS parent_status FROM tasks r JOIN task_links l ON l.child_id = r.id JOIN tasks p ON p.id = l.parent_id WHERE r.assignee = 'code-reviewer' ORDER BY r.created_at DESC;"
```

### Finding orphaned reviewers with cancelled/archived parents

```bash
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db \
  "SELECT tl.child_id, t.title, t.assignee, p.status AS parent_status FROM task_links tl JOIN tasks t ON tl.child_id = t.id JOIN tasks p ON tl.parent_id = p.id WHERE t.status IN ('ready', 'todo') AND p.status IN ('archived', 'cancelled');"
```

The detailed reason text is stored in `task_events`:

```bash
# Get the block reason text for all blocked cards
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db \
  "SELECT e.task_id, t.title, t.block_kind, json_extract(e.payload, '$.reason') as block_reason, e.created_at FROM task_events e JOIN tasks t ON e.task_id = t.id WHERE e.kind = 'blocked' AND t.status = 'blocked' ORDER BY e.created_at DESC;"

# Get block reason for a specific task
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db \
  "SELECT kind, created_at, json_extract(payload, '$.reason') as reason FROM task_events WHERE task_id = '<task-id>' AND kind = 'blocked' ORDER BY created_at DESC LIMIT 1;"
```

Do NOT query `block_reason` — it doesn't exist. Always join `task_events` for the reason text.

## Pattern 4: `review-failed` with genuine findings

**Symptoms:** Reviewer card calls `kanban_block(reason="review-failed: ...")`. Comment thread contains structured JSON with specific files, line numbers, and severity. The coder parent IS `done` — the code exists but has a real bug the coder cannot fix without a new dispatch.

**Resolution:**

1. **Read the reviewer comment** — it specifies exact files, line numbers, and the required fix.
2. **Create a new coder card** with the fix scope derived from the reviewer's findings, not a re-run of the original coder task. Use `workspace=worktree` and a unique `branch` name (do NOT reuse the original coder's branch_name — it may collide with the sibling worktree).
3. **Create a new reviewer card** with `parents=[new-coder-id]`.
4. **Archive the old blocked reviewer card** — add a comment tracing the replacement, then archive it. This prevents the old blocked card from polluting the block view and ensures the old reviewer isn't accidentally re-dispatched.
5. **Do NOT re-run the original coder task** — a fresh worker with a fresh worktree is the right tool. The old worker can't write code it already claimed to have written.

> **Full automated flow with example code:** See `kanban-orchestrator` skill's **Automated Review-Failed Resolution** section. It covers reading findings, creating fix+reviewer pairs, archiving, and pitfalls in one integrated workflow.
>
> **CLI equivalent (for cron/terminal):** When running in cron mode (`execute_code` blocked), use the CLI commands documented in `references/cli-based-review-auto-resolution.md` instead of the Python API. Key differences: `--workspace` (not `workspace_kind`), positional text for comments (not `--body`), `--parent` (not `parents`).

### Sub-pattern 4a: "ghost implementation" — coder completed without writing code

**Symptoms:** Reviewer flags: "Implementation entirely absent — the worktree has zero changes from the base commit. Coder's branch is at commit {hash} with zero diff." This pattern repeats across multiple retries of the same task. The same concrete issues are flagged identically across 3+ review cycles.

**Common variant — "coder claimed push but branch unchanged":** The reviewer phrases it as "Diff shows zero changes to that file despite parent task claiming it was fixed" or "The fix was not actually pushed to the branch." The coder's kanban_complete summary explicitly claims to have pushed to the target branch, but the branch shows no diff from base. This is a milder signal than "implementation entirely absent" but has the same root cause — the coder never actually wrote or committed the code. The fix is identical (full re-spec from reviewer findings), and the diagnosis is faster: skip the cross-worktree sweep (Step 4) since the only question is whether the target branch has the code. Verify with `git diff origin/<target-branch> -- <target-file>` instead of scanning all worktrees.

**Root cause:** The coder worker completed its lifecycle (API calls, reasoning, handoff) but the code changes were never committed to the worktree branch. The branch has zero diff from HEAD.

**Diagnosis — verify the worktree actually has changes:**

```bash
# Check if the worktree branch has any unique commits
cd /path/to/repo
git log origin/main..wt/t_<task-id> --oneline | head -5

# If no unique commits, diff against main directly
git diff main...wt/t_<task-id> --stat
```

If `git diff ... --stat` returns empty or only shows unrelated files, **do not conclude ghost yet** — run two more checks:

### Step 3: Check the working tree for uncommitted changes

Before sweeping worktrees, check whether the coder wrote code but never committed it:

```bash
cd /path/to/repo/.worktrees/t_<task-id>
git status --short              # staged + unstaged changes
git diff --stat                 # unstaged working tree changes
git diff --cached --stat        # staged but not committed
```

If this shows real changes to the **target files** the task was supposed to modify, the code EXISTS — it's an **uncommitted ghost (4b)**, not a pure ghost (4a). The fix for these two variants is radically different (see below).

### Step 4: Cross-worktree sweep (only if Step 3 shows nothing)

If the working tree is also clean, the coder may have committed code to a sibling worktree instead of the assigned one:

```bash
# Scan ALL worktrees for changes to the task's target directories
# (e.g. backend/tests/, frontend/tests/, e2e/tests/)
for wt in /path/to/repo/.worktrees/t_*; do
  bname=$(basename "$wt")
  diff=$(cd /path/to/repo && \
    git diff --stat origin/main.."wt/$bname" -- <target-paths> 2>/dev/null)
  [ -n "$diff" ] && echo "=== $bname ===\n$diff"
done
```

If a sibling worktree has the expected additions, the coder wrote code but in the wrong branch — create a fresh coder card scoped to move that code to the correct branch. If **no** sibling worktree has the expected additions, AND the working tree was clean, the coder genuinely never wrote code — pure ghost (4a). Cross-reference with task events:

```bash
# A pure ghost shows spawn -> heartbeat(1-3) -> completed with no real work
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db \
  "SELECT kind, created_at FROM task_events WHERE task_id='<id>' ORDER BY created_at;"
```

**Diagnosis summary table:**

| Branch commits | Working tree | Sibling worktrees | Variant | Fix |
|---|---|---|---|---|
| Empty or unrelated | Real changes to target files | Any | **Uncommitted ghost (4b)** | Commit existing code, re-dispatch reviewer |
| Empty or unrelated | Clean — no target file changes | No additions | **Pure ghost (4a)** | Re-spec from scratch (below) |
| Empty or unrelated | Clean — no target file changes | Has additions in sibling | **Wrong-branch ghost** | Move code from sibling worktree |

**Fix — full re-spec workflow (pure ghost only):**

1. **Cancel stale tasks:** Cancel the orchestrator review task that sits in `todo` waiting for ghost code. Leave the ghost coder task as `done` (historical record). Leave the blocked reviewer in `triage` (use as parent of new cards for traceability).

2. **Create a new coder card** with the **full implementation scope** from the reviewer's last comment. Do NOT just say "fix the 2 remaining issues" — the code literally doesn't exist on disk, so the new worker needs the complete spec from scratch. Label the title clearly (e.g. "Fix: Implement ...") and include the exact file paths, function signatures, and i18n keys the reviewer documented.

3. **Create a paired reviewer card** with `parents=[new-coder-id]` so it auto-promotes when the coder completes.

4. **Include the full task body** — paste the reviewer's finding as inline context so the new coder doesn't need to rediscover the requirements. The body should be self-contained (no dependency on reading the old task).

**Sibling-ghost audit:** When you find one ghost implementation, check for others. The same coder profile likely produced multiple ghosts in the same dispatch batch. Search for other `done` tasks whose reviewer is `blocked` with similar "implementation absent" language:

```bash
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db \
  "SELECT t.id, t.title FROM tasks t JOIN task_events e ON e.task_id = t.id WHERE t.status = 'blocked' AND e.kind = 'blocked' AND e.payload LIKE '%implementation entirely absent%' AND t.assignee = 'code-reviewer';"
```

If the pattern repeats after creating new cards, check whether the coder profile's system prompt explicitly instructs the agent to commit code changes. The `git add` + `git commit` steps must be in the agent's workflow.

**Distinguishing pure ghost from uncommitted ghost:** When the diagnosis reveals zero unique commits AND zero working tree changes, it is a pure ghost — the coder never wrote anything. When the branch has zero unique commits but the working tree has real uncommitted changes, it is an **uncommitted ghost (4b)** — the coder wrote code but never committed. See Sub-pattern 4b for the fix (which is cheaper than a full re-spec).

**Real-world example (GH-486):** The original coder task `t_9e88cc41` ("[GH-486] Frontend: Promote to SOP button + success toast + navigation") was marked `done` with zero changes to the target file. The reviewer ran 11+ review cycles on the same 2 unfixed bugs, but the code simply didn't exist on disk — `git diff main...wt/t_9e88cc41 --stat` showed only an unrelated `ProcessMap.vue` change. The fix: created a fresh coder card `t_2fc58406` with the full spec from the reviewer's last comment, paired with a new reviewer card `t_64de0f00`. The stale orchestrator review `t_981bac7c` was cancelled. See `references/ghost-implementation-example-gh-486.md` for the full walkthrough.

### Sub-pattern 4b: "uncommitted ghost" — code exists in working tree but was never committed

**Symptoms:** Reviewer flags: "Implementation entirely absent — zero commits unique to the branch. The branch tip is at the base commit with no diff from main." The coder's `completed` summary claims specific changes that sound plausible. But `git log origin/main..wt/TASK --oneline` returns empty or shows only unrelated commits.

**Key distinction from 4a (pure ghost):** The code actually EXISTS — as uncommitted working tree changes. Running `cd .worktrees/t_TASK && git status --short` shows real modifications to the target files and/or new untracked files. The coder used `write_file` and `patch` tools successfully but never ran `git add` or `git commit`.

**Root cause:** The coder's instructions (AGENTS.md) said "Commit messages are not your responsibility (the orchestrator handles that). Your output is the working diff on disk." The model interpreted this as "do NOT commit — leave the diff on disk." Combined with a conflicting system-prompt directive ("do not commit, push, or rewrite history unless asked"), the coder correctly concluded it should not commit and called `kanban_complete` instead.

**Model question shortcut:** When asked "is this due to the model?", check the working tree first (`git status --short` inside the worktree). If it shows real, functional code in the expected files, the model worked correctly — the root cause is instructional (AGENTS.md), not model quality. Skip the model-debugging tangent.

**Diagnosis — distinguish from pure ghost:**

```bash
# Committed check (Step 1-2 from 4a) — shows empty/unrelated
cd /path/to/repo
git log origin/main..wt/t_<task-id> --oneline | head -5
git diff origin/main...wt/t_<task-id> --stat

# Working tree check — shows real code
cd /path/to/repo/.worktrees/t_<task-id>
git status --short                 # M files, ?? new files
git diff --stat -- <target-dirs>   # actual additions
```

**Check the worker log for the smoking gun — the coder's internal debate:**

```bash
tail -100 ~/.hermes/kanban/boards/<board-slug>/logs/<coder-task-id>.log | grep -iE "commit|my responsibility|working diff"
```

Look for the model explicitly saying "The instructions say do not commit" or "commit messages are not my responsibility."

**Worktree pollution check:** Before committing, verify that the working tree changes are actually the coder's work and not pre-existing pollution from the main repo's dirty state. If the same files (`private_routes.py`, `database.py`, `package-lock.json`) appear dirty across MULTIPLE worktrees, they're likely pollution (see `references/worktree-pollution-gh-485.md`). Discard polluted files with `git checkout -- <file>` before committing the coder's real work.

**Fix — commit the existing code, do NOT re-spec:**

The code is already on disk. The right fix is cheaper than a full re-spec:

1. **Commit the uncommitted changes** to the worktree branch:
   ```bash
   cd /path/to/repo/.worktrees/t_<task-id>
   git add -A
   git commit -m "[GH-XXX] Tests & QA implementation (uncommitted ghost recovery)"
   ```

2. **Re-dispatch or mark-done the reviewer** — depends on the reviewer's state:

   - **Reviewer was `ready` (never dispatched):** Mark it `done` — the code now exists as committed diffs, so the reviewer has nothing to check:
     ```bash
     sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db \
       "UPDATE tasks SET status='done' WHERE id='<reviewer-task-id>';"
     ```

   - **Reviewer was `blocked` with genuine findings** (e.g., missing v-if, missing loading state, 403 assertion change): Reset it to `todo` so it runs a fresh review pass against the now-committed code:
     ```bash
     sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db \
       "UPDATE tasks SET status='todo', consecutive_failures=0, last_failure_error=NULL, worker_pid=NULL, block_kind=NULL, block_recurrences=0 WHERE id='<reviewer-task-id>';"
     ```
     Do NOT mark it `done` — the findings were correct at the time and the new code needs a fresh review. The dispatcher promotes it to `ready` automatically when the parent coder is `done`.

3. **Verify the reviewer picks it up**:
   ```bash
   sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db \
     "SELECT status, assignee FROM tasks WHERE id='<reviewer-task-id>';"
   ```

**Prevention:** The coder profile's AGENTS.md must explicitly include a commit step. The phrase "Commit messages are not your responsibility (the orchestrator handles that)" should be replaced with clear instructions: "After implementation, run `git add -A && git commit -m \"[GH-XXX] <summary>\"` on the worktree branch. The orchestrator handles PR creation and merging, but your local commits are required so the reviewer can see your diff."

**The worktree AGENTS.md trap:** The AGENTS.md fix is committed to the repo's branch, but **existing worktrees were checked out from the committed git tree, not the working tree.** They still have the OLD AGENTS.md that says "Commit messages are not your responsibility." New worktrees created after the fix is merged to `main` will pick up the corrected instructions. Worktrees created before the fix will continue to produce uncommitted ghosts until they are recreated from the updated base.

```bash
# Check which AGENTS.md a worktree will read
head -5 /path/to/repo/.worktrees/t_<id>/AGENTS.md | grep "Commit messages"
# If it still says "not your responsibility", the worktree has the old instructions

# The fix needs to be on main for new worktrees to pick it up
# Option 1: Merge the fix branch to main
# Option 2: Manually commit code for any worktrees created before the fix
```

**Implication for batch remediation:** When sweeping uncommitted ghosts, the worktrees with uncommitted code were created BEFORE the AGENTS.md fix. After committing their code and marking reviewers done, the same worktrees will produce another uncommitted ghost if re-dispatched. The fix only takes effect for NEW worktrees created from the updated base.

**Real-world example (GH-100 Tests & QA):** Coder task `t_71de4a53` wrote over 400 lines of real test code across 5 files (`test_step3_unicorn.py`, `test_step2_enterprise_vision.py`, `test_step5_workforce_strategy.py`, `test_step1.py`, and new file `test_audit_log.py`). But `git log origin/main..wt/t_71de4a53 --oneline` showed only GH-485 commits from a shared base branch. `git status --short` from the worktree showed the real test code as uncommitted `M` and `??` entries. The coder's log contained the exact reasoning: "The instructions say 'do not commit, push, or rewrite history unless asked' and 'commit messages are not my responsibility.'" The fix: commit the working tree changes and re-dispatch the reviewer. See `references/uncommitted-ghost-example-gh-468.md` for the full walkthrough.

**Batch remediation (sweep):** When the root cause of an uncommitted ghost is a bad AGENTS.md instruction (e.g. "Commit messages are not your responsibility"), ALL coder tasks dispatched under that instruction set are likely affected. After finding one, sweep the board for blocked reviewers whose parent coders are `done`:

```bash
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db \
  "SELECT tl.child_id, tl.parent_id, t.title, t.assignee FROM task_links tl \
   JOIN tasks t ON tl.child_id = t.id \
   JOIN tasks p ON tl.parent_id = p.id \
   WHERE t.status = 'blocked' AND t.assignee = 'code-reviewer' \
   AND p.status = 'done' AND p.assignee = 'coder';"
```

For each candidate, run the diagnosis (git status --short, git diff --stat) against the coder's worktree. If the code exists uncommitted, commit it and mark the reviewer done. The sweep can be done in parallel — each worktree is independent.

**Reviewer-finding inaccuracy note:** When a reviewer says "implementation was never implemented" or "coder's handoff was fabricated", this conclusion is about the *committed* state at the time of review. The code may exist as uncommitted working tree changes (see diagnosis above). Do not assume the reviewer is wrong about the existence of code — verify with `git status --short` inside the worktree. If the working tree shows real code, the reviewer's finding was accurate about the committed diff but inaccurate about code existence. The fix is to commit (not to re-spec).

**Post-fix verification:** After committing and re-dispatching the reviewer, check whether the repo's AGENTS.md (coder profile section) still contains the old "Commit messages are not your responsibility" language. If so, patch it to add the explicit commit step (see `references/agents-ghost-fix-diff.md` for the canonical diff). This prevents the same coder instructions from causing another uncommitted ghost on the next dispatch.

### Sub-pattern 4c: Reviewer passed code that was absent at first review but exists later

**Symptoms:** The reviewer's first comment says "implementation entirely absent", but a later comment says "Implementation now present — Review PASSES". The task is still `blocked` from the first failure. The code actually exists on disk, confirmed by searching the target files.

**Root cause:** The coder's code was committed to the worktree after the first review spawned but before the second review spawned. This happens when the coder completes mid-review-cycle, or the worktree was populated by a parallel process. The task never got unblocked from the first `blocked` event.

**Diagnosis:**

```bash
# Check if the claimed code actually exists
grep -n <function-name> <target-file>

# Check the reviewer comments in chronological order
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db \
  "SELECT kind, payload, created_at FROM task_events WHERE task_id='<id>' AND kind IN ('commented', 'blocked') ORDER BY created_at;"
```

If a later comment says "PASSES" or "approved", the code is present and the block is stale.

**Fix:** Simply mark the task `done`:

```bash
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db \
  "UPDATE tasks SET status = 'done' WHERE id = '<id>';"
```

### Sub-pattern 4d: Persistent FAIL-CLOSED bug — code exists but never fixed

**Symptoms:** The same FAIL-CLOSED issue flagged identically across **3+ consecutive dispatches** of the same reviewer task. The coder worktree has changes (not a ghost) but the bug is never addressed. The reviewer gets re-specified and re-dispatched instead of a fix being written.

**Root cause:** The orchestrator keeps re-dispatching the same reviewer expecting a different outcome. The coder task is already `done` so the buggy code never changes. The dispatcher cycles: blocked → specified → promoted → spawn → same finding → blocked again.

**Diagnosis — count repeated cycles:**
```bash
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db \
  "SELECT kind, COUNT(*) FROM task_events WHERE task_id='<id>' AND kind IN ('commented', 'blocked') GROUP BY kind;"
```
If `commented` > 3 with identical findings each time, it is a persistent-bug cycle. Verify the code actually exists but is buggy:
```bash
grep -n '<buggy-pattern>' <target-file>
```

**Fix — create a targeted fix coder card (do NOT re-dispatch the reviewer):**\n1. Archive the looping reviewer task — it correctly found the same bug every time, the fault is in the workflow.\n2. Create a coder card with the **exact fix** baked into the body: file, line numbers, old code, replacement, and the reason WHY.\n3. Create a paired reviewer card with `parents=[new-coder-id]`.\n\n### Sub-pattern 4d2: "re-dispatched coder with fresh worker still misses review findings"\n\n**Symptoms:** Same as 4d (identical findings across multiple review cycles), but the coder WAS re-dispatched with a fresh worker (not a ghost — the worker ran, wrote code, and committed changes). The new code still has the same bugs. The reviewer's findings reference specific lines and conditions that should have been addressed but weren't.\n\n**Diagnosis:**\n\n```bash\n# Verify the coder WAS re-dispatched after the missing-skill fix\nsqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db \\\n  "SELECT created_at, kind FROM task_events WHERE task_id='<coder-task-id>' AND kind IN ('promoted', 'claimed', 'spawned', 'completed') ORDER BY created_at;"\n# Look for a spawned+completed sequence AFTER the gave_up/blocked event\n\n# Check whether the re-dispatched coder produced real code\ngit log origin/main..wt/t_<coder-task-id> --oneline | head -3\ngit diff origin/main...wt/t_<coder-task-id> --stat | head -10\n# Should show new commits from the re-dispatched worker\n\n# Read the reviewer's original findings to see if they reference lines\n# that still exist in the new code\nsqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db \\\n  "SELECT substr(body, 1, 500) FROM task_comments WHERE task_id='<reviewer-task-id>' ORDER BY created_at DESC LIMIT 1;"\n```\n\n**Root cause:** The coder profile's AGENTS.md says "Orient — read the card body." The coder reads the original card body (which says "fix the remaining issues") but does NOT read the reviewer's comment thread. The reviewer's specific line-level findings ("line 73 needs v-if", "missing isPromoting ref") exist only in the comment thread, not in the card body. The coder re-implements from scratch using only the card body spec and produces code with the same defects.\n\n**Critical distinction from 4d (persistent bug cycle):** In 4d, the coder was NEVER re-dispatched (same buggy code circulates through the reviewer alone). In 4d2, the coder WAS re-dispatched and wrote fresh code, but the fresh code still has the same bugs because the coder didn't see the reviewer's fix instructions.\n\n**Fix — archive the looping reviewer, create a new card with EXACT inline code:**\n\n1. Archive the looping reviewer task.\n2. Create a **new coder card** with the **exact old-to-new code changes** baked directly into the body as paste-able diff blocks. Do NOT just say "fix the 2 remaining issues from the review" — the new coder won't read the review thread. Instead, include exact old/new code blocks:\n\n   ```\n   ## Fix 1: Button visibility — add v-if on outcome\n\n   File: path/to/Component.vue, line ~73\n\n   Current code:\n   <div class="detail-section promote-section">\n\n   Replace with:\n   <div v-if="experiment && experiment.outcome === 'succeeded'" class="detail-section promote-section">\n\n   ## Fix 2: Loading state — add isPromoting ref + disabled binding\n\n   File: path/to/Component.vue, line ~77\n\n   Current button:\n   <button class="btn-primary" @click="handlePromoteToSop">\n     {{ $t('step6.detail.promoteToSop') }}\n   </button>\n\n   Replace with:\n   <button class="btn-primary" :disabled="isPromoting" @click="handlePromoteToSop">\n     {{ isPromoting ? $t('common.loading') : $t('step6.detail.promoteToSop') }}\n   </button>\n\n   After the line `const feasibilityResult = ref(null)`, add:\n   const isPromoting = ref(false)\n   ```\n\n   The body must be **self-contained** — the coder should be able to implement every line from the card body alone, without reading any linked review thread.\n\n3. Create a paired reviewer card with `parents=[new-coder-id]`.\n\n**Real-world example (t_2fc58406/t_64de0f00 — ExperimentDetail.vue promote-to-sop):** The original coder `t_2fc58406` was created to fix 2 review findings (missing `v-if`, missing `isPromoting`). The missing-skill error prevented it from spawning. After the skill was copied to the coder profile, the coder re-dispatched, wrote new code, and completed. But the new code still had the same 2 bugs — the coder read the card body ("fix remaining issues") but never read the reviewer's comment thread with the exact fix instructions. The reviewer flagged the same issues. The fix: archived the looping reviewer `t_64de0f00` and created a replacement pair (`t_3cf6109b` coder to `t_2ff4eef4` reviewer) with the exact old-to-new code inline in the card body.\n\n**Real-world example (t_efc0e740, useDeviations.js):** The computed checked `d.status === 'unresolved'` on data with no `status` field. Backend already filters at the SQL level. Bug was flagged 4 times (runs 729, 730, 731, 733) unfixed. Fix: remove `&& d.status === 'unresolved'`. Created `t_11902eb9` (coder) → `t_285cf729` (reviewer). Old reviewer archived.

### Sub-pattern 4e: API URL path mismatch

**Symptoms:** Reviewer flags: "Frontend calls `/api/steps/6/...` but backend route is `/step6/...`. Will 404 at runtime."

**Common root cause:** The coder guessed the API path instead of reading the actual backend route definition. The frontend service file has a typo in the URL path — typically `steps/6` vs `step6`, `training-modules` vs `modules`, or a missing `/api` prefix.

**Fix:**
- Create a coder card with the exact fix: change the URL in the frontend service file to match the backend route.
- Include the exact backend route from the reviewer's comment so the coder doesn't need to rediscover it.

### Sub-pattern 4f: "analysis paralysis ghost" — coder ran for 1h+ but never wrote a single file

**Symptoms:** The coder was dispatched for a long time (1–2 hours, 70+ heartbeats) but the worktree has zero diff from origin/main. `git status --short` is completely clean. The worker log shows extensive file reading, test-running, and internal debate about which tool to use (`write_file` vs `patch`), but **no actual write_file or patch calls that land on disk**. The coder never calls `kanban_complete` or `kanban_block` — exits with `protocol_violation` (rc=0 without completing the kanban protocol) or gets killed by timeout.

**Distinction from 4a (pure ghost) and 4b (uncommitted ghost):**

| Aspect | Pure ghost (4a) | Uncommitted ghost (4b) | Analysis paralysis (4f) |
|--------|----------------|----------------------|------------------------|
| git status --short | Clean | Real changes to target files | Clean |
| git diff main...wt --stat | Empty or unrelated | Empty for committed, real in working tree | Empty |
| Worker log evidence | Plausible summary, no tool calls | write_file/patch calls succeeded | Reads files, runs tests, debates tools, **never writes** |
| kanban_complete called? | Yes | Yes | No — exits via protocol_violation or timeout |
| Run duration | 10–30 min | 30–60 min | 1–2 hours |
| `consecutive_failures` | 1–2 | 1–2 (per reviewer) | 5+ (keeps getting retried by dispatcher) |

**Diagnosis:**

```bash
# Step 1: Check the worktree — should be completely clean
cd /path/to/repo/.worktrees/t_<task-id>
git status --short                    # Empty — no files changed
git diff --stat -- <target-dirs>       # No output

# Step 2: Check the worker log for the debate pattern
grep -E "write_file|patch|write|edit" ~/.hermes/kanban/boards/<board-slug>/logs/<task-id>.log | head -20
# Look for: imports of write_file/patch but no actual calls to write paths
# under the worktree directory

# Step 3: Check for protocol_violation events
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db \
  "SELECT kind, created_at FROM task_events WHERE task_id='<id>' AND kind IN ('protocol_violation', 'crashed', 'gave_up') ORDER BY created_at;"
```

**Root cause:** The coder couldn't decide how to write the code. Common internal debates:
- "Should I use `write_file` or `patch`?" — spends time debating tool choice
- "The tests are too slow — I need to run just specific test files" — keeps running test subsets
- "Let me read more files to understand the context" — reads the same files repeatedly
- "The test suite requires a PostgreSQL instance" — gets stuck on environment constraints

**The most common root cause — the backend endpoints don't exist.** Before investigating the coder's internal debate, check whether the task's target endpoints actually exist in the codebase. If the task asks for tests of features that haven't been implemented, the coder will inevitably get stuck in analysis paralysis:

```bash
# Check if the target endpoints exist
grep -n "target-endpoint\|expected-route" backend/api/routers/private_routes.py | head -5

# Check if the PRD file exists
ls docs/PRD/prd-*.md 2>/dev/null | grep -i "relevant-name"

# Check if the database functions exist
grep -n "target-function" backend/database.py | head -5
```

If endpoints don't exist, the fix is NOT to re-spec the test card — it's to create implementation cards first, then re-spec the test card after the backend exists.

The model gets trapped in a loop of reading, analyzing, and running tests without ever committing an edit.

**Fix — do NOT retry the same card:**

1. **Block the task permanently** — it's been tried 5+ times with zero output:
   ```bash
   sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db \
     "UPDATE tasks SET status='blocked', consecutive_failures=0, last_failure_error='manual block: analysis paralysis ghost — 5+ attempts with zero code produced', worker_pid=NULL, block_kind='needs_input' WHERE id='<task-id>';"
   ```

2. **Create a fresh coder card** with the exact code as inline code blocks in the body. The previous card was too vague (e.g. "write tests for checklist persistence"). The new card must include:
   - Exact file paths
   - Exact function signatures
   - Exact test content as paste-able code blocks
   - The specific test assertions expected

3. **Pair with a new reviewer card** with `parents=[new-coder-id]`.

**Real-world example (t_839baf1e — GH-487 Backend Unit Tests):** The coder ran for 1.5 hours (70+ heartbeats), imported `write_file`/`read_file` tools, ran `timeout 600 ./run-tests.sh backend`, and debated between `write_file` and `patch` approaches. The worktree had zero diff from origin/main — the coder never actually wrote a single file. 5 consecutive failures accumulated across 3 different failure modes (protocol_violation, crashed, pid not alive). The card was manually blocked. The replacement card needs inline code blocks for every test function to prevent the same paralysis. See `references/analysis-paralysis-example-gh-487.md` for the full walkthrough.

### Sub-pattern 4g: Wrong base branch — coder branched from main instead of target

**Symptoms:** Reviewer flags: "Wrong base branch. The coder's branch was created from main, not from the target branch `<target>`." The fix commits on the coder's branch were inherited from main (not authored by the coder). The target branch remains broken with all original issues still present. The coder's only authored commit may be a lockfile regeneration or other non-functional change — the real fixes were never applied to the target.

**Key markers:**
- Reviewer says "coder's handoff contradicts reality — they were reviewing against main, not the correct branch"
- `git log --author=coder <worktree-branch>` shows zero or one trivial commits (lockfile, whitespace)
- The substantive changes (`package.json` devDeps, route additions, etc.) are on `main` already, not authored by the coder
- Target branch `git diff <target-branch>..origin/main -- <target-files>` shows the actual needed diff

**Diagnosis — confirm the base mismatch:**

```bash
# 1. Check which branch the coder's worktree branched from
cd /path/to/repo/.worktrees/t_<coder-task-id>
git log --oneline -3 origin/main..HEAD      # Only trivial commits?
git log --oneline <target-branch>..HEAD      # Should show all fix commits if base was correct
git merge-base HEAD origin/main              # First shared ancestor with main
git merge-base HEAD <target-branch>          # First shared ancestor with target

# If both merge-base values are the same, the coder used main as base.
# If merge-base with target-branch differs from main, the coder used the correct base.

# 2. Count authored vs inherited commits
git log --oneline HEAD --not <target-branch> --format="%H %an %s"
# If the only listed commits are from "Hermes" or a non-coder author (inherited from main),
# AND the coder's commit count is 0 or 1, it's a wrong-base-branch.

# 3. Verify the target branch is still broken
git checkout <target-branch>
# Check each claimed fix on the target branch — they won't be there
git diff HEAD -- package.json | grep -c "vue"         # expect 0 — still broken
git diff HEAD -- scripts/check-deps.sh | grep "vue-router" # expect 0 — still wrong
git checkout -
```

**Root cause:** The coder profile's worktree creation defaults to `main` as the base branch. When the task card says "fix branch `fix/df-XXX`", the coder reads this as the *target for the PR* but uses `main` as the *base for the worktree*. The coder never runs `git branch --set-upstream-to` or checks out the target branch. The worktree has the correct code (because main already has it), and the coder declares "done" — but the target branch never received any fix.

**Distinction from other sub-patterns:**

| Aspect | Pure ghost (4a) | Wrong base (4g) |
|--------|----------------|-----------------|
| Worktree has changes vs main? | No — zero diff | Yes — inherited from main |
| Coder authored commits? | 0 | 1 (lockfile) or 0 |
| Target branch fixed? | No | No |
| Reviewer correctly blames? | "Code doesn't exist" | "Code exists but was inherited, not applied to target" |

**Fix — create a new coder task with explicit base branch direction:**

1. **Do NOT re-use the existing coder task** — its worktree is on the wrong base. The coder profile will create another worktree from `main` again.

2. **Create a new coder card** with an explicit base instruction baked into the body:
   ```
   ## IMPORTANT — Base Branch
   This card's worktree MUST branch from `<target-branch>`, NOT from `main`.
   The target branch is missing the following changes that exist on main:
   - package.json: vue, @vitejs/plugin-vue, react, react-dom devDependencies
   - overrides block for @vue/* compiler
   - scripts/check-deps.sh: use vue-router sentinel, install from $PROJECT_ROOT
   ```

3. **Include exact code blocks** in the body — the coder should not need to infer what main has. Provide the exact old→new diff for each file.

4. **Create a paired reviewer card** with `parents=[new-coder-id]`.

5. **Archive the old reviewer card** — its findings were correct but the implementation needs a fresh start from the correct base.

6. **Mark the old coder task as `archived`** — it's a historical record of the wrong-base-branch pattern, not a task to be re-dispatched.

**Real-world example (DF-2222222222, Jul 23):** Coder task `t_9832cdc2` was assigned to fix frontend build failures (ERR_MODULE_NOT_FOUND for vue) on PR target branch `fix/df-1784774204-save-values-v2` (PR #120). The coder created worktree branch `fix/df-1784829956-frontend-hoisting` from main (commit `b4875d5`). The only authored commit was a `package-lock.json` regeneration (`3a4aa25`). The correct `package.json` devDeps and `scripts/check-deps.sh` content existed on the coder's branch because they were inherited from main commits — not because the coder applied any fixes. On the target branch, all four issues were still broken. Reviewer card `t_c36027fd` correctly blocked the task. The PR consolidate cron (`pr-consolidate-df-1784829956`) hung waiting for the blocked reviewer. See `references/wrong-base-branch-example-df-1784829956.md` for the full walkthrough.

### Sub-pattern 5b: `git worktree add failed` — branch already checked out by sibling worktree (not `main`)

**Symptoms:** `last_failure_error` contains:
```
workspace: git worktree add failed for ... on branch agent/GH-135-chat-vue-i18n:
fatal: 'agent/GH-135-chat-vue-i18n' is already used by worktree at '/home/user/project/.worktrees/t_f9d74d91'
```
The branch name is NOT `main` — it's a named feature or agent branch. `consecutive_failures >= 2`. The branch IS legitimately active in another worktree.

**Distinction from Pattern 5a (branch=main):** In 5a, the branch is `main` and the error happens because the card was created without `--branch`, causing the dispatcher to default to the already-checked-out `main`. In 5b, the card WAS created with a branch, but that branch is already checked out in a sibling worktree from a different card. The root cause is branch name reuse.

**Common contexts for Pattern 5b:**
- **Auto-resolution of review-failed cards** — the orchestrator copies the original coder's `branch_name` (e.g., `agent/GH-135-chat-vue-i18n`) into the new fix card, but that branch is still checked out by the original coder's worktree. The fix card needs a *new* unique branch.
- **Decompose of an epic** — sibling cards from the same decomposition all get the same agent branch name. Each needs a unique name.

**Diagnosis:** Confirm the branch is legitimately in use by another worktree:
```bash
cd /path/to/repo
git worktree list | grep "<branch-name>"
# Output shows: /path/.worktrees/t_sibling  agent/GH-135-chat-vue-i18n  [commit-hash]
```

**Fix — assign a unique branch and reset:**

```bash
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db \
  "UPDATE tasks SET branch_name='fix/<gh>-<descriptor>-v2', status='todo', consecutive_failures=0, last_failure_error=NULL WHERE id='<task-id>';"
```

Use a disambiguated branch name — append `-v2`, `-fix`, or derive from the task's own ID (e.g., `fix/gh-584-interpolation-params`). Do NOT reuse the sibling's branch.

If the card was created by the automated review-failed resolution flow, the fix is the same. The original coder's `branch_name` is not safe to reuse.

**Prevention (3-layer guardrail system):** See `references/worktree-branch-guardrails.md` for the full system. In summary:
1. **Pre-creation check** — `assert-branch-unique.sh` validates branch name uniqueness before every `kanban_create --branch`
2. **Skill convention** — The `kanban-orchestrator` auto-resolution flow now explicitly forbids copying `branch_name` from the original coder card
3. **Automated cron** — `worktree-collision-watch` (every 5 min, no_agent) detects and auto-remediates any collisions that slip through

**Prevention:**
- For auto-resolved fix cards, always derive a unique branch — do NOT copy `branch_name` from the original coder card.
- When decomposing an epic into multiple worktree cards, append a per-task disambiguator to each branch name.
- After creating fix cards, verify they didn't immediately fail: check for `spawn_failed` events on the new card within the first cycle.

## Pattern 5: `spawn_failed` / `crashed` (mid-work, not a review)

**Symptoms:** Task shows `spawn_failed` or `crashed` in events. No `review-failed` or `last_failure_error`. The worker had heartbeats then stopped.

**Root causes:**
- **Timeout on git worktree add** (common on large repos with many worktrees): `spawn_failed` with `timed out after 60 seconds`
- **OOM** on a complex operation: `crashed` with no error

**Fix:** Unblock and let the dispatcher retry:
```bash
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db \
  "UPDATE tasks SET status='todo', consecutive_failures=0, last_failure_error=NULL, worker_pid=NULL WHERE id='<task-id>' AND status='blocked' AND last_failure_error IS NULL;"
```

If it crashes repeatedly with the same timeout, consider switching to `workspace_kind=scratch` or `workspace_kind=dir:` instead of `worktree`.

## Pattern 5a: `git worktree add failed` — branch `main` already checked out

**Symptoms:** `last_failure_error` contains:
```
workspace: git worktree add failed for ... on branch main:
fatal: 'main' is already used by worktree at '/home/user/project'
```
`consecutive_failures=2`. `branch_name` is NULL on the task row. Other cards from the same decomposition batch may have the identical error.

**Root cause:** The card was created with `workspace_kind=worktree` but without a `branch` parameter. The dispatcher defaulted to `main`, but `main` is already checked out as the main repo directory — git refuses to create a second worktree on the same branch.

**Diagnosis:** Confirm the card was created without a branch:
```bash
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db \
  "SELECT id, title, branch_name, workspace_kind FROM tasks WHERE id='<task-id>';"
```
If `branch_name IS NULL` and `workspace_kind='worktree'`, the card was created without `--branch`.

**Fix — depends on whether the card has a target branch:**

1. **The card was supposed to work on a named feature branch** (common when the user specifies a PR target branch):
   ```bash
   sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db \
     "UPDATE tasks SET branch_name='fix/df-1234-feature', status='todo', consecutive_failures=0, last_failure_error=NULL WHERE id='<task-id>';"
   ```
   The dispatcher retries and creates the worktree from the correct branch.

2. **The card was genuinely supposed to work on main** (rare — only for hotfixes or docs-only changes):
   This is a git limitation — you can't have two worktrees on `main`. The card must be re-created with `workspace_kind=scratch` (no worktree) or the worktree must be created from a different branch and then `git merge main` applied inside the worker.

3. **Mass fix for a batch of cards** — multiple cards from the same decomposition all lack branches:
   ```bash
   sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db \
     "UPDATE tasks SET branch_name='fix/df-1234-feature', status='todo', consecutive_failures=0, last_failure_error=NULL WHERE status='blocked' AND workspace_kind='worktree' AND branch_name IS NULL AND last_failure_error LIKE '%already used by worktree%';"
   ```

**Prevention:** Always pass `branch=<target-branch>` on `kanban_create` when `workspace_kind=worktree`. The orchestrator's SOUL.md and decomposition playbook already mandate this — the error arises when a card is created outside the orchestrator (e.g., by a cron job, by the GitHub-issues-to-kanban sync, or by a script that directly inserts into the DB). Any script that creates worktree cards must also set `branch`.

**Distinction from Pattern 5 (timeout):** A timeout on `git worktree add` shows `timed out after 60 seconds` — the worktree creation was slow but the branch was valid. Pattern 5a shows a `fatal` error — the branch itself is invalid (already checked out). The fix is different: timeout needs retry; fatal needs a branch change.

## Pattern 6: `ready` with high `consecutive_failures` (not blocked, but stuck)

**Symptoms:** Task is `ready` but has `consecutive_failures >= 3`. It should have been `blocked` after `failure_limit` (default 2) attempts. The worker keeps respawning and failing, but the dispatcher never blocks it.

**Root causes:**
- **Custom `failure_limit`** in the board config — check `kanban.failure_limit` in the gateway config. It may be set higher than the default 2.
- **Dispatcher resetting the counter** — the dispatcher may reset `consecutive_failures` before the task reaches the limit (e.g., if the failure is categorized differently).
- **Worker crashes before the spawn record** — the worker dies before the dispatcher can record the failure, so the counter never increments.

**Diagnosis:**
```bash
# Check the board's failure_limit
grep -A 2 "failure_limit" ~/.hermes/profiles/orchestrator/config.yaml

# Check the task's event history
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db \
  "SELECT kind, payload, created_at FROM task_events WHERE task_id='<id>' AND kind IN ('spawn_failed', 'gave_up') ORDER BY created_at DESC LIMIT 10;"
```

**Fix:**
- If the `failure_limit` is intentionally high, these tasks are being retried as expected. Let them cycle.
- If the `failure_limit` is default 2 but tasks have 4+ failures, the dispatcher is not properly recording spawn failures. Check the gateway logs for `spawn_failed` events.
- If the tasks are stuck in a respawn loop, manually block them to break the cycle:
  ```bash
  sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db \
    "UPDATE tasks SET status='blocked', last_failure_error='manual block: respawn loop' WHERE id='<id>' AND status='ready';"
  ```
  Then investigate the worker logs manually (Pattern 1 / 2a).

**Preventing dispatcher re-promotion of manually blocked cards:** When you manually block a card that the dispatcher has been promoting back to `ready`, the dispatcher may re-promote it on the next tick if `block_kind` and `block_recurrences` are not set. Always set both when manually blocking:

```bash
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db \
  "UPDATE tasks SET status='blocked', consecutive_failures=0, last_failure_error='manual block: <reason>', worker_pid=NULL, block_kind='needs_input', block_recurrences=1 WHERE id='<id>';"
```

- `block_kind='needs_input'` — categorizes the block as needing human attention (not a dependency)
- `block_recurrences=1` — prevents the dispatcher from auto-promoting this block. A card with `block_recurrences > 0` is treated as a genuine block, not a transient failure.
- Without `block_recurrences`, the dispatcher may re-promote the card to `ready`, undoing the manual block.

## Pattern 7: Gateway crash loop prevents any dispatch

**Symptoms:** The gateway is `active` but restarts every 30-60 seconds. The kanban dispatcher never completes a full tick. No tasks are dispatched or completed. The journal shows `Main process exited, code=exited, status=1/FAILURE` followed by auto-restart.

**Root causes:**
- **WhatsApp bridge crash in orchestrator gateway** — the orchestrator profile's `.env` contains `WHATSAPP_*` vars that belong in the personal-assistant profile. The WhatsApp bridge starts, fails to connect, and the gateway exits.
- **Platform connection failure** — a messaging platform (Telegram, Discord, etc.) crashes during connect and the gateway treats it as fatal.
- **Missing prefill file** — `SOUL.md` referenced in `prefill_messages_file` doesn't exist or is invalid JSON.

**Diagnosis:**
```bash
# Check gateway status and recent exits
systemctl --user status hermes-gateway-<profile>.service
journalctl --user -u hermes-gateway-<profile>.service --no-pager -n 20 | grep -E "exit code|FAILURE|Whatsapp|Bridge|prefill"

# Check for WhatsApp env var bleed in orchestrator
cat ~/.hermes/profiles/orchestrator/.env 2>/dev/null | grep WHATSAPP

# Check for redundant messaging platforms
grep -A 10 "^  platforms:" ~/.hermes/profiles/orchestrator/config.yaml 2>/dev/null
```

**Fix — WhatsApp bridge crash:**
```python
# Strip WHATSAPP_* vars from the orchestrator .env
import os
path = os.path.expanduser('~/.hermes/profiles/orchestrator/.env')
with open(path) as f:
    lines = f.readlines()
filtered = [l for l in lines if not l.startswith('WHATSAPP_')]
with open(path, 'w') as f:
    f.writelines(filtered)
```
Then restart: `systemctl --user restart hermes-gateway-orchestrator.service`

**Fix — missing prefill file:**
```bash
echo '{}' > ~/.hermes/profiles/orchestrator/SOUL.md
systemctl --user restart hermes-gateway-<profile>.service
```

**Prevention:** After gateway reconfiguration, audit all profiles for leaked env vars:
```bash
for f in ~/.hermes/.env ~/.hermes/profiles/*/.env; do
  echo "--- $f ---"
  grep -E "TELEGRAM_|WHATSAPP_|DISCORD_|GOOGLE_CHAT_" "$f" 2>/dev/null || echo "(none)"
done
```
Every channel token should appear in EXACTLY one profile's `.env`.

## Pattern 9: "merged but missing" — a squash-merged PR's file content silently clobbered by a later PR from a stale worktree base

**Symptoms:** CI fails on a test that calls an endpoint/function that was supposedly merged in PR #X (which shows `MERGED` in `gh pr view`). The code is confirmed present on the PR's source branch and on `origin/<pr-branch>`, but `git grep <symbol> origin/main -- <file>` returns 0 matches. Yet `git merge-base --is-ancestor <merge-commit> origin/main` returns true — the merge commit IS an ancestor of main, but its content is gone.

**Root cause:** The PR was built from a worktree branch checked out BEFORE a *different* PR merged. When PR #X (with the new code) and PR #Y (built from the same old base, without #X's code) are both squash-merged, the later merge (#Y) writes its older copy of the shared file (`private_routes.py`, `database.py`, etc.) over #X's version. The route/function that #X added vanishes from `main` even though `e883ac4` (the #X merge commit) is still in main's history.

This is the inverse of a ghost: the code WAS written and merged, but a sibling PR's stale base reverted it. It looks like a CI flake ("tests passed when #X merged, now 404") but is a deterministic content regression.

**Diagnosis — confirm the clobber (don't guess):**

```bash
cd /path/to/repo
git fetch origin main <pr-branch-x> 2>&1 | tail -1

# 1. Is the route/function on main RIGHT NOW? (the smoking gun)
git show origin/main:<file> | grep -c "<symbol>"      # expect 0

# 2. Is it on the merged PR's source branch? (confirms it was written)
git show origin/<pr-branch-x>:<file> | grep -c "<symbol>"   # expect >=1

# 3. Is the merge commit an ancestor of main? (confirms "merged" is true)
git merge-base --is-ancestor <merge-commit-x> origin/main && echo YES || echo NO

# 4. Which later commits touched the file and dropped the symbol?
git log --oneline <merge-commit-x>..origin/main -- <file>
# Then inspect each: git show <commit>:<file> | grep -c "<symbol>"
```

If step 4 reveals a later PR (#Y) whose merge result has 0 occurrences, #Y clobbered #X.

**Why this happens in kanban workflows:** Worktrees are checked out once, at decomposition time. If PR #X merges after a worktree for #Y was created, #Y's branch still points at the pre-#X base. Pushing #Y and merging it replays the old file. Always rebase/merge `origin/main` into a PR branch immediately before opening/merging the PR — never merge a branch whose tip predates a sibling PR's merge.

**Two triage sub-lessons the single-PR diagnosis above assumes you already know:**

1. **`404` is NOT an auth failure.** CI like `test_x_requires_auth - assert 404 in (401, 422)` looks like an auth/runner problem but isn't — a route must EXIST to return 401/422; `404` means it's not registered. Grep `origin/main:<file>` for the symbol first. If 0, it's a clobber, not auth. Users will misread a `404` cluster as runner flakiness; correct it and verify route presence before touching auth.

2. **URL-prefix mismatch between tests / frontend / backend.** After restoring a route, tests can still 404 if they call the wrong PATH. The source of truth is the **frontend service file already merged** (`git show origin/main:frontend/src/services/<svc>.js`) — the route path must match IT, not the test. Real case: tests used `/api/steps/5/modules/{id}/quiz-submit` but frontend + restored route used `/api/steps/5/training-modules/{id}/quiz-submit`; the fix was `replace_all /modules/ -> /training-modules/` in the test file, not changing the (correct) route.

**When one clobber appears, audit the WHOLE batch.** A single confirmed clobber means sibling PRs from the same stale base are suspect too. Run the **bulk clobber audit** — see `references/bulk-clobber-audit.md` for the per-branch "uniquely-added vs current-main" scan (routes + DB funcs + schemas) that finds every clobbered symbol across all backend PR branches in one pass, plus the table-name-fork check (`user_event_log` vs `event_log`).

**Fix — restore the missing content without a noisy revert:**

1. Build a clean branch from current `origin/main`:
   ```bash
   git checkout -b fix/<gh>-restore-<symbol> origin/main
   ```
   **Gotcha — dirty main repo blocks branch creation:** If the main working tree has a phantom-modified shared file (common after GH-485-style pollution — `git status` shows `M  private_routes.py` with an empty `git diff`), `git checkout -b` aborts with *"Please commit your changes or stash them."* Stash only the tracked file so untracked assets are left untouched:
   ```bash
   git stash push -m 'pre-fix stash' -- <shared-file>
   git checkout -b fix/<gh>-restore-<symbol> origin/main
   # ... apply fix ... (optional later: git stash pop to restore the polluted state)
   ```

2. Re-apply only the missing content. Pick the technique by whether the source commit is a clean unit:

   **Technique A — cherry-pick (only if the symbol was added in a clean unit commit).** A clean unit = a commit whose ONLY change is adding the route/function, with a small targeted diff (`git show <commit> --stat` shows a few lines, not a full-file rewrite).
   ```bash
   git cherry-pick <commit-that-added-symbol>
   # If it conflicts, take origin/main's version of the file + manually re-add the symbol
   ```

   **Technique B — surgical patch (when the source commit has full-file churn).** This is the COMMON case for squash-merged PRs: the merge commit rewrote the entire shared file (`private_routes.py` shows a ~15k-line diff), so cherry-pick explodes into conflicts AND `git checkout <branch> -- <file>` (whole-file) re-introduces #Y's regression. Instead, extract just the missing hunks and apply them surgically:
   ```bash
   # View the missing route/function as it exists on the source branch
   git show <source-branch>:<file> | sed -n '<start>,<end>p>'   # copy the route/DB-function block

   # Find the insertion anchor on main (the line just BEFORE where it belongs)
   grep -n "def get_latest_impact_analysis\|def create_ab_test" <file>

   # Apply with the patch tool: old_string = anchor context, new_string = anchor + inserted block.
   # This inserts ONLY the missing symbol and leaves #Y's content intact.
   ```
   Avoid `git merge` of the whole stale branch and `git checkout <branch> -- <file>` (whole-file) — both reintroduce #Y's regression.

3. Verify on the new branch: `git show HEAD:<file> | grep -c "<symbol>"` → >=1, AND the file still contains #Y's content (no regression). Spot-check that #Y's routes/functions are still present.

4. Push and open a PR. Title it `fix: restore <symbol> clobbered by stale-base merge of #Y`.

**Prevention — enforced at PR-open time:**
- Before `gh pr create`, always `git fetch origin main && git rebase origin/main` (or `git merge origin/main`) into the PR branch so its base is current.
- For PRs touching shared files (`private_routes.py`, `database.py`, `schemas.py`), check `git diff origin/main...HEAD -- <shared-file>` is non-empty AND that the expected symbols survive. A pre-push check: `git grep -c <symbol> origin/main` vs on the branch.
- The `references/worktree-to-pr-shortcut.md` direct-push workflow is safe ONLY when each branch touches disjoint files. When two PR branches touch the same shared file, sequence their merges (merge #X fully, then rebase #Y onto main before merging #Y).

**Symbol-not-imported check (when restoring code from a clobbered PR, verify imports survive too):** When you restore a route/function by patching it into `main`, do NOT assume the `from backend.schemas import (...)` line it references also survived. The schema/class may exist on `main` (e.g. `PromoteToSopResponse` at `schemas.py:845`) but the *import* into the routes file may have been clobbered by the same stale-base merge that killed the route. CI `ruff F821 Undefined name 'X'` is the signal. Always verify: `git grep -c "<symbol>" origin/main -- <routes-file>` AND `git grep -c "^class <symbol>" origin/main -- <schemas-file>`. If the class exists but the import is missing, add it to the import block — do NOT re-add the schema (it's already there). See `references/merged-but-missing-symbol-import.md` for the full walkthrough.

**Real-world example (GH-486 promote-to-sop, Jul 21):** PR #110 (GH-486) added `POST /step6/experiments/{id}/promote-to-sop` + `promote_experiment_to_sop()`. `e883ac4` is an ancestor of `origin/main`. But PRs #111 (GH-102-routes) and #112 (GH-103-api) were pushed from worktrees (`wt/t_765e2702`, `wt/t_79ce9f6f`) checked out before #110 merged. Their `private_routes.py` was the pre-#110 version. Their squash-merges overwrote the file, deleting the route. `test_step6_cx_innovation_lab.py` (from #113, GH-487) then 404'd on the endpoint. Fix: surgical patch of just the route + DB function from `wt/t_c9de841e` onto a fresh branch from `origin/main` (the source commit had full-file churn so cherry-pick/whole-file-checkout were NOT viable) and re-open as PR #114. See `references/merged-but-missing-stale-base-clobber.md`.

## Pattern 10: `active_pr` guard — coder completed but card stuck in `ready`

### Symptoms

- Card is `ready` status but never dispatched
- Gateway logs: `"ready queue non-empty for N consecutive ticks but 0 workers spawned"`
- No `last_failure_error`, no `consecutive_failures`
- The coder already completed successfully and opened a PR

### Diagnosis

Check task events for the guard pattern:

```bash
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db "
SELECT kind, created_at
FROM task_events
WHERE task_id = '<id>'
ORDER BY created_at DESC
LIMIT 15;
"
```

Look for:
1. `blocked` with `review-required` — coder completed, opened a PR
2. `unblocked` — someone cleared the block
3. `respawn_guarded` (reason `active_pr`) — repeated 5+ times
4. No intervening `claimed` or `spawned` — the guard prevents re-spawn

### Root cause

The dispatcher's `active_pr` guard correctly prevents spawning a duplicate worker for work that already has a PR. But the card stays in `ready` indefinitely, and the dispatcher logs "stuck" warnings every tick because the ready queue is non-empty.

### Fix

**Automated (recommended):** The `active-pr-guard-watch` cron job (every 5 min, no_agent) detects cards with 5+ consecutive `respawn_guarded` events and moves them to `triage`. See `kanban-safety-protocols`'s **Active PR Guard Recovery** section.

**Manual (one-off):**

```bash
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db "
UPDATE tasks
SET status = 'triage'
WHERE id = '<task-id>' AND status = 'ready';
"
```

This moves the card to `triage` where the orchestrator picks it up for PR consolidation. The dispatcher stops logging warnings.

### Distinction from Pattern 6 (stuck ready with failures)

| Aspect | Pattern 10 (active_pr) | Pattern 6 (high failures) |
|--------|----------------------|---------------------------|
| Event pattern | `respawn_guarded` only | `spawn_failed` / `crashed` |
| Worker attempted? | Yes (successfully — PR exists) | Yes (failed) |
| `consecutive_failures` | 0 | >= 3 |
| PR exists? | Yes | No |
| Fix | Move to `triage` | Investigate worker crash, block card |

### Prevention

- The `active-pr-guard-watch` cron catches this automatically
- After unblocking a card that already has a PR, manually move it to `triage` instead of leaving it `ready`
- The dispatcher should ideally move `active_pr`-guarded cards to `triage` itself (future improvement)

## Pattern 11: Wrong assignee — auto-decomposer assigned implementation children to `orchestrator` instead of `coder`

**Symptoms:** Cards appear in `todo` or `ready` with `assignee=orchestrator` but their titles describe concrete implementation work (e.g., "Add backend pytest suite", "Create Vitest component suite", "Refactor completion gate"). The dispatcher may spawn workers (the board DOES route to `orchestrator` profiles), but those workers try to *decompose* instead of *implement* — wasting cycles. Alternatively, cards stuck in `todo` waiting on running parents with the wrong assignee never progress.

**Key signals:**
- `hermes kanban stats` shows orchestrator-assigned cards in `ready`, `todo`, or `running`
- Implementation work (code, tests, refactoring) assigned to `orchestrator` instead of `coder`
- `task_events` show `crashed`, `gave_up`, or `decomposed` on the parent epic card
- The `decomposed` event payload includes `"root_assignee": "orchestrator"` — confirming the auto-decomposer is the source

**Root cause:** The `auto-decomposer` (triggered after a card hits its failure limit and is sent to `triage`) routes children to the same profile as the parent. When the parent is an `orchestrator` epic, all children inherit `assignee=orchestrator`. Implementation children should go to `coder`; only orchestration/decomposition parents should stay on `orchestrator`.

**Diagnosis:**

```bash
# 1. Quick scan — find all non-epic orchestrator cards
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db \
  "SELECT id, title, status, assignee FROM tasks 
   WHERE assignee = 'orchestrator' 
     AND status NOT IN ('done','archived','cancelled')
   ORDER BY created_at DESC;"

# 2. Check if the auto-decomposer is the source
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db \
  "SELECT e.task_id, e.kind, json_extract(e.payload, '$.from_decompose_of'), 
          json_extract(e.payload, '$.root_assignee')
   FROM task_events e
   WHERE e.task_id IN ('<id1>','<id2>',...)
     AND e.kind = 'created'
   ORDER BY e.created_at;"

# 3. Check for parent epic crashes (the decomposer trigger)
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db \
  "SELECT task_id, kind, payload, datetime(created_at, 'unixepoch')
   FROM task_events
   WHERE task_id = '<epic-id>'
     AND kind IN ('crashed', 'gave_up', 'decomposed')
   ORDER BY created_at;"
```

**Fix:**

```bash
# 1. Reclaim any running orchestrator workers (they're trying to decompose, not implement)
hermes kanban reclaim <task-id>   # one per running card

# 2. Reassign implementation cards from orchestrator -> coder via SQL
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db \
  "UPDATE tasks SET assignee = 'coder' WHERE id IN ('<id1>','<id2>',...);"

# 3. Create missing reviewer cards — auto-decomposed children rarely have paired reviewers
hermes kanban create "Review: <implementation title>" \
  --assignee code-reviewer \
  --parent <coder-task-id> \
  --body "Review implementation of <title>.
Coder task: <coder-task-id>
Files: <expected target files>
Verify: <expected test output>"
```

**Verification:** After reassignment, check:
```bash
hermes kanban stats   # should show 0 orchestrator cards in ready/todo/running
# For cards that were ready: dispatcher picks them up on next tick
# For cards that were todo with running parents: promote when parents complete
```

**Prevention:** When creating epic orchestration cards, ensure their bodies/prompts instruct the decomposer to assign *implementation* children to `coder`, not to itself. The `root_assignee` in the decompose payload should be `coder`, not `orchestrator`, for any task that writes code.

**Real-world example (Jul 30, 2026):** Epic `t_1aae7197` ("[GH-141] Add FR 4.5 PSP Manifesto Test Suite") crashed twice and was auto-decomposed into 4 children — all assigned to `orchestrator`. Three spawned workers that sent heartbeats but were presumably trying to decompose test requirements instead of writing test code. One child (`t_40e7c25f`) completed, but the other three were stuck. Also `t_f5576d2a` ("[GH-140] Backend: Refactor Step 5 completion gate") was assigned to `orchestrator` and stuck in `todo` waiting on a running coder parent. All reassigned to `coder` + paired reviewer cards created.

## Bulk Unblocking

When you've fixed the root cause (e.g., copied missing skills, resolved provider auth, recovered from DB corruption), unblock all affected tasks in one shot. Use the three-category approach:

```bash
# 1. Genuine worker failures (missing skills, provider auth) — reset to 'todo'
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db \
  "UPDATE tasks SET status='todo', consecutive_failures=0, last_failure_error=NULL, worker_pid=NULL, current_run_id=NULL WHERE status='blocked' AND assignee='coder' AND last_failure_error LIKE '%Unknown skill%';"

# 2. Reviewer tasks that crashed mid-work (no error text, never dispatched)
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db \
  "UPDATE tasks SET status='todo', consecutive_failures=0, last_failure_error=NULL, worker_pid=NULL WHERE status='blocked' AND assignee='code-reviewer' AND (last_failure_error IS NULL OR last_failure_error = '');"

# 3. "pid not alive" — only after confirming DB integrity is OK
# If integrity check fails, the corruption is the root cause — use the three-category
# procedure in Pattern 2b instead, which handles running→todo, pid-not-alive→ready,
# and never-dispatched→todo in the correct order.
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db \
  "UPDATE tasks SET status='ready', consecutive_failures=0, last_failure_error=NULL, worker_pid=NULL WHERE status='blocked' AND last_failure_error LIKE '%pid % not alive%';"
```

For corruption-side-effect unblocking (DB integrity fails), see Pattern 2b above — it has a three-category procedure that handles `running` → `todo`, `pid not alive` → `ready`, and never-dispatched → `todo` in the correct order.

## Pattern 8: Stale ready/todo tasks after replacement — archive sweep

**Symptoms:** After creating replacement coder+reviewer pairs for ghost implementations or persistent-bug cycles, the old blocked/triage tasks' parent chains still have `ready` or `todo` cards pointing at the original (archived/cancelled) parent. These cards sit in limbo forever because their parent is gone — `ready` cards block the ready view, `todo` cards never promote.

**Root cause:** Creating replacement cards (coder fix + paired reviewer) leaves the original task chain intact. When the original parent is archived/cancelled, any dependent cards become orphaned. The dispatcher can't promote them (no parent to chain from), and they pollute the board.

**`todo` orphaned reviewers:** A `todo` reviewer with a `cancelled` parent will never promote because the dispatcher only promotes when the parent is `done`. These are invisible in the `todo` view but will never be dispatched. Sweep them alongside `ready` orphans.

**Diagnosis — find orphaned tasks with archived/cancelled parents:**

```bash
# Find ready tasks with archived/cancelled parents
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db "
SELECT tl.child_id, t.title, t.assignee, p.status AS parent_status
FROM task_links tl
JOIN tasks t ON tl.child_id = t.id
JOIN tasks p ON tl.parent_id = p.id
WHERE t.status = 'ready'
  AND p.status IN ('archived', 'cancelled');
"

# Also check todo tasks with cancelled parents — these will never promote
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db "
SELECT tl.child_id, t.title, t.assignee, p.status AS parent_status
FROM task_links tl
JOIN tasks t ON tl.child_id = t.id
JOIN tasks p ON tl.parent_id = p.id
WHERE t.status = 'todo'
  AND p.status IN ('cancelled');
"
```

Also check `ready` tasks with no parent links at all (standalone reviewer cards decomposed from an archived parent review that had no coder):

```bash
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db "
SELECT id, title, assignee FROM tasks
WHERE status = 'ready'
  AND id NOT IN (SELECT child_id FROM task_links);
"
```

**Diagnosis — check creation source for orphaned ready tasks:**

For each orphan found, check the `created` event to understand the source:

```bash
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db "
SELECT kind, payload FROM task_events
WHERE task_id='<id>' AND kind='created'
ORDER BY created_at LIMIT 1;
"
```

- `"from_decompose_of": "<archived-id>"` — decomposed from an archived parent, safe to archive
- `parents: []` (empty) — created directly with `status: ready`, likely a genuine task (keep unless manually verified as stale)

**Categorize and sweep:**

| Source | Status | Action |
|---|---|---|
| Decomposed from an archived/cancelled parent | ready or todo | Archive — the replacement chain exists |
| Decomposed from a done parent that had ALL review conditions PASSED | ready or todo | Mark done — review was successful, code verified present |
| Created directly with empty `parents: []` and `status: ready` | ready | Keep — genuine open task |
| Created directly with empty `parents: []` and `status: todo` | todo | Check if it has a parent link — if genuinely standalone, keep |
| Decomposed from a parent that was archived but the code was later verified present | ready or todo | Mark done — stale block, code exists |

**Archive command:**

```bash
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db "
UPDATE tasks SET status = 'archived'
WHERE id IN ('<orphan-id-1>', '<orphan-id-2>');
"
```

**Prevention:** When creating replacement coder+reviewer pairs, immediately sweep the board for stale `blocked` and `ready` tasks related to the original chain. Create all replacement cards first (so the new chain is live), then archive/deprecate the originals in a single transaction. This prevents the gap between creation and cleanup where the dispatcher might pick up a stale card.

**Real-world example (Jul 20 sweep):** After creating replacement chains for 3 ghost implementations (t_8966b1d5, t_01b0c1cf, t_eef6a08c, t_11902eb9) and 1 persistent-bug cycle (t_efc0e740), a sweep found 8 stale ready cards: 5 decomposed from archived parent t_cd9831b3 (sub-review breakdowns), 2 decomposed from t_506afc01 (code verified present, marked done), and 1 orchestrator review of a ghost coder. All archived. The remaining 11 ready cards were genuine open issues.

## Reference Files

- `references/kanban-db-schema-diagnostics.md` — blocked-card DB schema columns (block_kind, block_recurrences vs free-form reason), finding block reason text in task_comments, block kind reference table, 3+ recurrence loop check
- `references/post-recovery-worktree-audit.md` — audit worktree branches for PR gaps after corruption recovery
- `references/worktree-to-pr-shortcut.md` — push worktree branches directly as PR branches (skip cherry-pick when no conflicts)
- `references/fix-validation.md` — validate root cause before making configuration or workflow changes; avoid unnecessary diffs that erode trust
- `references/ghost-implementation-example-gh-486.md` — detailed walkthrough of a pure ghost (coder marked `done` without ever writing code — GH-486 Promote-to-SOP), with diagnostic commands and resolution pattern
- `references/uncommitted-ghost-example-gh-468.md` — detailed walkthrough of an uncommitted ghost (coder wrote real code via write_file/patch but never ran `git add` or `git commit` — GH-100 Tests & QA), with timeline, diagnostic commands, and fix
- `references/ghost-implementation-cross-worktree-sweep-gh-468.md` — cross-worktree sweep technique used during the GH-100 investigation to confirm the implementation wasn't in a sibling worktree
- `references/replacement-chain-sweep.md` — diagnostic queries and decision tree for sweeping stale blocked/ready/triage tasks after creating replacement coder+reviewer pairs
- `references/worktree-pollution-gh-485.md` — diagnosing and fixing pre-existing working tree changes from the main repo leaking into kanban worktrees
- `references/re-dispatched-coder-ignores-review-findings.md` — detailed walkthrough of a re-dispatched coder that wrote new code but still missed the same review findings (t_2fc58406 / t_64de0f00 — ExperimentDetail.vue promote-to-sop), with timeline, diagnostic commands, and resolution pattern
- `references/merged-but-missing-stale-base-clobber.md` — diagnosis + fix for squash-merged PR content silently clobbered by a later PR from a stale worktree base (GH-486 promote-to-sop)
- `references/merged-but-missing-symbol-import.md` — second-order clobber: schema survived but the import into the routes file did not; ruff F821 diagnosis + one-line fix
- `references/deploy-monitor-dedup-and-consolidation.md` — deploy monitoring dedup, worktree branch naming, no_agent consolidation scripts, and cron delivery configuration
- `references/analysis-paralysis-example-gh-487.md` — detailed walkthrough of an analysis paralysis ghost
- `references/cli-based-review-auto-resolution.md` — CLI command reference for automated review-failed resolution in cron/terminal mode (when `execute_code` is blocked)
