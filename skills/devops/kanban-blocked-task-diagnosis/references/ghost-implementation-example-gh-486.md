# Ghost Implementation Example: GH-486 Promote-to-SOP

## Summary

The original coder task `t_9e88cc41` (title: `[GH-486] Frontend: Promote to SOP button + success toast + navigation`) was marked `done` but zero code changes were ever committed to the worktree branch. The reviewer ran **11+ review cycles** on the same 2 unfixed bugs, but the code literally didn't exist on disk.

## Timeline

1. **Coder `t_9e88cc41`** started, marked `done` — but `git diff main...wt/t_9e88cc41 --stat` showed only an unrelated `ProcessMap.vue` change. The target file `ExperimentDetail.vue` had zero promote-to-sop code.

2. **Reviewer `t_cd9831b3`** ran 11+ review cycles, each finding the same 2 issues:
   - No success toast with action link "Go to Step 4" after promote
   - Empty catch block with no error feedback
   
   Early reviews also flagged "changes not on review branch (unstaged in main repo, wt/t_981bac7c has zero changes)" — the code was never there.

3. **Orchestrator review `t_981bac7c`** sat in `todo` waiting for code that didn't exist.

4. **DB corruption** hit, wiping the kanban state. After recovery:
   - `t_9e88cc41` was still `done` (the code never existed)
   - `t_cd9831b3` cycled through more review attempts on non-existent code
   - The 2 bugs were never fixable because the implementation to fix didn't exist

## Diagnostic Commands Used

```bash
# Check if the coder's worktree has any unique commits
cd $HOME/my-project
git log origin/main..wt/t_9e88cc41 --oneline | head -5
# → empty — no unique commits

# Diff the worktree against main
git diff main...wt/t_9e88cc41 --stat
# → 1 file changed, 3 insertions(+), 1 deletion(-) in ProcessMap.vue
# → NOT the target file (ExperimentDetail.vue)

# Check the reviewer's task events for the full review cycle count
sqlite3 ~/.hermes/kanban/boards/my-project-dev/kanban.db \
  "SELECT kind, created_at FROM task_events WHERE task_id='t_cd9831b3' AND kind IN ('block_loop_detected', 'commented') ORDER BY created_at;"
# → 7 block_loop_detected events, 11+ review comments
```

## Resolution

1. Cancelled the stale orchestrator review `t_981bac7c`
2. Left `t_cd9831b3` in `triage` as historical record (used as parent of new coder)
3. Created a **new coder card** `t_2fc58406` with the **full implementation scope** from the reviewer's last comment:
   - Import `useToast` composable
   - Add `success()` call with `actionUrl: '/step4'` after `loadExperiment`
   - Replace empty catch block with `error()` call
   - Include the promote button, API wiring, and i18n keys from scratch
4. Created a **paired reviewer card** `t_64de0f00` with `parents=[t_2fc58406]`

## Lesson

When a reviewer flags the same concrete issue across 3+ review cycles, check whether the underlying code actually exists on disk. A "ghost implementation" coder task will show:
- `status: done` in the kanban board
- Zero unique commits on the worktree branch
- No changes to the target file when diffed against main
- Reviewer comments that describe the same bugs identically each cycle (because the code to fix them was never written)

The fix is always a **fresh coder card with the full spec**, not another retry of the ghost task.

## Sibling-Ghost Audit

When you find one ghost implementation, check for others. In this session (Jul 20), three ghost implementations were found back-to-back:

| Ghost Task | Title | Target |
|---|---|---|
| t_9e88cc41 | [GH-486] Frontend: Promote to SOP button + toast | ExperimentDetail.vue |
| t_1b7a27b6 | [GH-103] Write unit test: empty state for quiz history | OnboardingModules.spec.js |
| t_0c111bdf | [GH-100] #104-BE Backend Step 1 Session Isolation & Auto-Trigger | backend auto_trigger_status_analysis |

All three were produced by the same coder profile in the same dispatch batch. The sibling-ghost search query:

```bash
sqlite3 ~/.hermes/kanban/boards/my-project-dev/kanban.db \
  "SELECT t.id, t.title FROM tasks t JOIN task_events e ON e.task_id = t.id WHERE t.status = 'blocked' AND e.kind = 'blocked' AND e.payload LIKE '%implementation entirely absent%' AND t.assignee = 'code-reviewer';"
