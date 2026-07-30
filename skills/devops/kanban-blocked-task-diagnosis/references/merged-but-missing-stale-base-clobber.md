# Merged-but-missing: stale worktree base clobbers a sibling PR's file content

## When this bites

You open PRs for many kanban cards, each from its own git worktree. Worktrees are
checked out once at decomposition time. If PR #X merges, any worktree created
*before* that merge still has the old base. When you later push and merge that
stale worktree's branch as PR #Y, #Y's copy of any shared file
(`private_routes.py`, `database.py`, `schemas.py`) overwrites #X's additions.

The result: `gh pr view #X` says `MERGED`, the merge commit IS an ancestor of
`main`, but the actual symbol/route/function is absent from `origin/main`. CI
that depends on it (often a test from a *third* PR) then fails with 404 or
`UndefinedColumn`.

## Reproduction / diagnosis recipe (MyProject, 2026-07-21)

Symptom: `backend/tests/test_step6_cx_innovation_lab.py` 4 tests fail with
`assert 404 == 200` on `/api/step6/experiments/{id}/promote-to-sop`.

```bash
cd /home/user/MyProject
git fetch origin main 2>&1 | tail -1

# 1. Smoking gun — route absent from main TODAY
git show origin/main:backend/api/routers/private_routes.py | grep -c "promote"
# -> 0

# 2. But it IS on the merged PR's source branch
git show origin/pr/gh-486:backend/api/routers/private_routes.py | grep -c "promote"
# -> >=1  (route at line ~6891)

# 3. And the merge commit is an ancestor of main (so "merged" is TRUE)
git merge-base --is-ancestor e883ac4 origin/main && echo YES || echo NO
# -> YES

# 4. Which later commits touched the file and dropped it?
git log --oneline e883ac4..origin/main -- backend/api/routers/private_routes.py
# -> 2d966d0 [GH-479] (#530), c70c66f [GH-478] (#528)
git show 2d966d0:backend/api/routers/private_routes.py | grep -c "promote"   # -> 0
git show c70c66f:backend/api/routers/private_routes.py | grep -c "promote"   # -> 0
```

Conclusion: #528 and #530 were pushed from `wt/t_765e2702` / `wt/t_79ce9f6f`
(checked out before #524 merged). Their squash-merges reverted the route.

## Fix applied (working version)

The `cherry-pick` approach does NOT work here: the source commit `wt/t_c9de841e`
is a squash-merge that rewrote the ENTIRE `private_routes.py` (~15k-line diff), so
cherry-pick explodes into conflicts and `git checkout wt/t_c9de841e -- private_routes.py`
would replay the whole stale file. The fix is a **surgical patch** of only the missing
hunks.

```bash
cd /home/user/MyProject

# Gotcha: the main working tree had a phantom-modified private_routes.py from GH-485
# pollution (git status shows 'M ' with empty diff). git checkout -b aborts unless stashed:
git stash push -m 'pre-fix stash' -- backend/api/routers/private_routes.py
git checkout -b fix/gh-486-promote-route origin/main

# Extract ONLY the missing route + DB function from the source branch:
git show wt/t_c9de841e:backend/api/routers/private_routes.py | sed -n '6891,6910p'   # route block
# Find the anchor (line just before where the route belongs) and patch-insert it:
#   old_string = ...return db_utils.get_latest_impact_analysis(db, experiment_id) or {}\n\n\n@router.get("/step6/status")
#   new_string = <same> + the route block + blank line + @router.get("/step6/status")

# DB function: same surgical insertion before def create_ab_test
#   old_string = def create_ab_test(...)
#   new_string = <promote_experiment_to_sop() function> + blank line + def create_ab_test(...)

git show HEAD:backend/api/routers/private_routes.py | grep -c "promote"   # -> >=1
# Verify #528/#530 routes still present (no regression):
git show HEAD:backend/api/routers/private_routes.py | grep -c "quiz-attempts"   # -> >=1

git add -A && git commit -m "fix(gh-486): restore promote-to-sop route + DB function"
git push origin fix/gh-486-promote-route
gh pr create --base main --head fix/gh-486-promote-route \
  --title "fix: restore promote-to-sop endpoint clobbered by stale-base merge"
# -> PR #534
```

Do NOT `git merge` the whole stale branch and do NOT `git checkout <branch> -- <file>`
(whole-file) — both reintroduce #528/#530's regression in the other direction.

## Prevention (enforce at PR-open time)

- Always `git fetch origin main && git rebase origin/main` into the PR branch
  before `gh pr create` / before merge. Never merge a branch whose tip predates
  a sibling PR's merge.
- For PRs touching shared files, assert the expected symbols survive:
  `git grep -c <symbol> origin/main` (before) vs on the branch (after).
- `references/worktree-to-pr-shortcut.md` direct-push is safe ONLY when branches
  touch disjoint files. When two PRs touch the same shared file, sequence the
  merges: merge #X fully, then rebase #Y onto main before merging #Y.
