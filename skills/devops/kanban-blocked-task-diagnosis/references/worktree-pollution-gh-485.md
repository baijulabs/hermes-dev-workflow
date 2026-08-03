# Worktree Pollution: Pre-Existing Working Tree Changes

## What Happened (GH-485 Era)

During the July 2026 dispatch batch, the main repo (`$HOME/my-project`) had `feature/gh-485-kanban-columns-migration` checked out with uncommitted working tree changes in `private_routes.py`, `database.py`, and `package-lock.json`. When kanban worktrees were created via `git worktree add`, those pre-existing working tree changes leaked into every new worktree.

## Symptom

Multiple worktrees from the same batch show the **same unrelated working tree changes**:

```
┊ cd /path/to/repo/.worktrees/t_<task-id> && git status --short
  M backend/api/routers/private_routes.py   # NOT the coder's work
  M backend/database.py                     # NOT the coder's work
  M package-lock.json                       # NOT the coder's work
```

These files appear in `git status --short` for EVERY worktree created while the main repo was in that dirty state. The changes are identical across worktrees — they're the same pre-existing working tree state from the main repo, not independent coder work.

## Root Cause

`git worktree add` creates a linked working tree at a specific commit, but the **working tree state** (uncommitted changes) of the main repo is NOT copied to the new worktree. However, if the main repo has uncommitted changes in the checked-out branch, and the worktree is added at the same branch/commit, the **index** and **working tree** of the worktree can inherit the main repo's dirty state through the shared `.git` directory's index.

More precisely: when the main repo has a dirty working tree and you run `git worktree add --checkout <path> <branch>`, the checkout process may merge the index state from the main repo, bringing the dirty files into the new worktree.

## Diagnosis

```bash
# Check if the pollution is pre-existing (same files across many worktrees)
for wt in /path/to/repo/.worktrees/t_*; do
  bname=$(basename "$wt")
  files=$(cd "$wt" 2>/dev/null && git status --short 2>/dev/null)
  if [ -n "$files" ]; then
    echo "=== $bname ==="
    echo "$files"
  fi
done | head -40
```

If the same files (`private_routes.py`, `database.py`, `package-lock.json`) appear in **most or all** worktrees from the same time period, it's pollution from the main repo's dirty state, not independent coder work.

## Impact on Coders

When a coder sees these pre-existing changes in `git status --short`, it can:

1. **Confuse the coder into thinking the changes are its own work.** The coder may include these files in its completion summary ("changed files: private_routes.py, database.py, test_*.py"), claiming credit for work it didn't do.

2. **Mask the coder's own changes.** The coder may think its `write_file`/`patch` calls succeeded because `git status --short` shows files, but those files are actually pre-existing pollution, not the coder's work.

3. **Cause committer attribution errors.** If the coder commits the polluted files along with its own work, the commit history attributes the pre-existing changes to the wrong task/branch.

## Fix

When diagnosing a potentially ghost task and finding working tree changes, first check whether those changes are pre-existing pollution:

```bash
# 1. Check if the same files are dirty in the main repo
cd /path/to/repo
git status --short

# 2. Check if the main repo is on a feature branch with uncommitted work
git branch --show-current
git log --oneline -1

# 3. Compare the dirty files across multiple worktrees
# If private_routes.py and database.py are dirty in ALL of them, it's pollution
```

If confirmed as pollution:

```bash
# Discard the polluted files before committing the coder's real work
cd /path/to/repo/.worktrees/t_<task-id>
git checkout -- backend/api/routers/private_routes.py backend/database.py package-lock.json
```

Then add only the coder's actual work:

```bash
git add -A
git status --short    # Verify only the coder's files are staged
git commit -m "[GH-XXX] <summary>"
```

## Prevention

Before creating worktrees, ensure the main repo has a clean working tree:

```bash
cd /path/to/repo
if [ -n "$(git status --short)" ]; then
  echo "WARNING: Main repo has uncommitted changes. Stash or commit before creating worktrees."
  git stash push -m "auto-stash before worktree creation"
fi
```

This check should be added to the kanban dispatcher's worktree-creation step or the SOUL's pre-decomposition setup.