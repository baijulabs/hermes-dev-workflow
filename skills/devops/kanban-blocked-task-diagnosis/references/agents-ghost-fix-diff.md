# AGENTS.md Ghost Prevention Fix

## What Changed

The canonical fix for preventing uncommitted ghosts. The root cause was the coder instructions in AGENTS.md saying "Commit messages are not your responsibility (the orchestrator handles that). Your output is the working diff on disk." This told the coder NOT to commit.

## The Two Changes

### Change 1: Insert explicit commit step between Lint and Hand off

```
-5. **Hand off** — call `kanban_complete` with structured metadata:
+5. **Commit** — after all tests and lint pass, commit your work to the worktree branch:
+   - `git add` the changed files (verify with `git status --short`)
+   - `git commit -m "feat: <scope>: <card title or summary>"`
+   - Re-run `git status --short` — it must show a clean working tree.
+   - If `git status --short` is empty after step 2 (Implement), you did not write any files. Go back to step 2 and use `write_file()` or `patch()` to write changes to disk.
+6. **Hand off** — call `kanban_complete` with structured metadata:
```

### Change 2: Replace the misleading "not your responsibility" line

```
-- Commit messages are not your responsibility (the orchestrator handles that). Your output is the working diff on disk.
+- Commit your changes to the worktree branch before calling `kanban_complete` (see step 5 above). Stage all files with `git add` and write a descriptive commit message. The review worktree needs committed diffs to read; uncommitted working tree changes are invisible to the reviewer.
```

## The Ghost-Busting Guardrail

The key line in Change 1: **"If `git status --short` is empty after step 2 (Implement), you did not write any files."** This forces the coder to notice when `write_file`/`patch` calls produced no files and retry instead of completing with an empty diff.

## Activation

After applying this fix to a repo's AGENTS.md, commit it to the feature branch:

```bash
cd /path/to/repo
git add AGENTS.md
git commit -m "fix(instructions): add explicit commit step to coder workflow to prevent ghost implementations"
```

## The Worktree AGENTS.md Trap

The fix is committed to the repo's feature branch, but **existing worktrees were checked out from the committed git tree, not the working tree.** They still have the OLD AGENTS.md.

```bash
# Check which AGENTS.md a worktree reads
head -5 /path/to/repo/.worktrees/t_<id>/AGENTS.md | grep "Commit messages"
# If it still says "not your responsibility", the worktree has the old instructions
```

New worktrees created after the fix is merged to `main` will pick up the corrected instructions. Worktrees created before the fix will continue to produce uncommitted ghosts until recreated from the updated base. This means:

- **Batch-remediated worktrees** (committed manually) will ghost again if re-dispatched — their AGENTS.md is still the old version.
- **The fix must be on `main`** before any new coder task can benefit from it.
- Until then, any new coder card dispatched to a worktree created from the pre-fix base will still produce uncommitted code.

## Verification

New coder tasks dispatched after this commit will have the fixed instructions. Monitor the first few dispatches for the "5. Commit" step appearing in worker logs:

```bash
grep "5. \*\*Commit" ~/.hermes/kanban/boards/<board-slug>/logs/<new-task-id>.log
```

## Bulk Uncommitted Ghost Recovery

When you find one uncommitted ghost, scan all blocked reviewers for the same pattern:

```bash
# Find all blocked reviewers with done parents
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db "
SELECT tl.child_id AS reviewer, t.title, p.status AS parent_status
FROM task_links tl
JOIN tasks t ON tl.child_id = t.id
JOIN tasks p ON tl.parent_id = p.id
WHERE t.status = 'blocked'
  AND t.assignee = 'code-reviewer'
  AND p.status = 'done';
"

# For each, check if code exists in the working tree
cd /path/to/repo/.worktrees/t_<coder-task-id>
git status --short
git diff --stat -- <target-files>
```

If the working tree has real code, the fix is the same for all: `git add -A && git commit -m "..."` + mark reviewer `done`.