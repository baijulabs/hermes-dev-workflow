# Worktree-to-PR Shortcut: Push Worktree Branches Directly

When worktree branches have independent commits that don't conflict, you can skip the cherry-pick step and push them directly as PR branches.

## When to Use

Use this shortcut when each worktree branch touches **different files**. If two worktrees modify the same file (e.g., both touch `backend/database.py`), the direct push will include their different base commits and the PR diff will show the full worktree history, which may include unrelated commits from the shared base.

## Procedure

```bash
# Push a worktree branch directly as a PR branch
cd /path/to/repo
git push origin wt/t_<task-id>:refs/heads/pr/gh-<number>

# Create a PR
gh pr create \
  --base main \
  --head pr/gh-<number> \
  --title "[GH-<number>] <title>" \
  --body "<description>"
```

## When to Cherry-Pick Instead

If the worktree branch was created from a base that's not `origin/main` (e.g., created from `feature/gh-485-kanban-columns-migration`), the direct push will include the additional base commits in the PR diff. The PR will show changes from both the worktree's commits AND the base branch's commits.

**Symptoms of base-commit pollution:**
- The PR diff shows files that aren't related to the task (e.g., migration scripts, unrelated DB changes, `package-lock.json` noise)
- The PR shows more commits than expected

**Fix — cherry-pick onto a clean origin/main branch:**

```bash
# Create a clean branch from origin/main
git checkout -b pr/gh-<number>-fixed origin/main

# Cherry-pick only the task-specific commit
git cherry-pick <task-commit-hash>

# Handle any conflicts
# Force-push to update the PR
git push origin pr/gh-<number>-fixed:refs/heads/pr/gh-<number> --force
```

## Identification

Before pushing, check whether the worktree's base is origin/main:

```bash
git merge-base --is-ancestor origin/main wt/t_<task-id> && echo "Based on main" || echo "NOT based on main"
```

If not based on main, the direct push will include extra commits. Use cherry-pick instead.

## Batch Push

For multiple independent worktrees, push all at once:

```bash
git push origin wt/t_<task-id-1>:refs/heads/pr/gh-<num-1> &
git push origin wt/t_<task-id-2>:refs/heads/pr/gh-<num-2> &
wait
```

## Example

This session pushed 14 worktree branches directly as PRs (#520-#113). Most were based on `0049dad` (not `origin/main`), so the PRs included the `0049dad` commit as an ancestor. This was acceptable because the PR diff showed the correct delta — the extra base commit was a small fix that was already on the path to main. Cherry-pick was only needed for PR #521 (GH-100) which had GH-485 migration commits polluting the diff.
