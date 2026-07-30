# Bulk Clobber Audit — find ALL stale-base clobbers across a PR batch at once

Used after one "merged but missing" failure appears (Pattern 9) to check whether
*siblings* of that PR were clobbered too. The single-PR diagnosis confirms one
clobber; this audit confirms the batch is clean before you trust CI again.

## When to run it

- A merged PR's endpoint/function is gone from `origin/main` (Pattern 9 confirmed).
- That PR was one of several touching a shared file (`private_routes.py`,
  `database.py`, `schemas.py`) that were all branched off an old base.
- CI shows a cluster of `404` test failures across multiple PRs' test files.

Do NOT run it for an isolated single-PR regression — the Pattern 9 diagnosis is
enough there. Run the bulk audit when >=2 PRs share a base + a shared file.

## The core technique: per-branch "uniquely added vs current main"

For each backend PR branch, compute what THAT branch uniquely added (present in the
branch but NOT in its merge-base with `origin/main`), then check whether that's
still missing from current `origin/main`. A branch can't clobber what it didn't
add, and an added-then-missing symbol is by definition clobbered.

Routes:
```bash
ROUTE_FILE=backend/api/routers/private_routes.py
for br in wt/t_648c8dfb wt/t_765e2702 wt/t_79ce9f6f wt/t_c9de841e wt/t_fb76d7ef; do
  mb=$(git merge-base origin/main $br)
  br_routes=$(git grep -h -n '@router\.' $br -- $ROUTE_FILE | grep -oE '@router\.(get|post|put|patch|delete)\(\s*"[^"]+"' | sed -E 's/.*"(.*)"/\1/' | sort -u)
  mb_routes=$(git grep -h -n '@router\.' $mb -- $ROUTE_FILE | grep -oE '@router\.(get|post|put|patch|delete)\(\s*"[^"]+"' | sed -E 's/.*"(.*)"/\1/' | sort -u)
  main_routes=$(git grep -h -n '@router\.' origin/main -- $ROUTE_FILE | grep -oE '@router\.(get|post|put|patch|delete)\(\s*"[^"]+"' | sed -E 's/.*"(.*)"/\1/' | sort -u)
  missing=$(comm -23 <(echo "$br_routes") <(echo "$main_routes"))
  [ -n "$missing" ] && echo "### $br CLOBBERED:" && echo "$missing"
done
```

DB functions (capture to temp files — `git show ref:path` through `execute_code`'s
`terminal()` can drop output; write to disk and read locally):
```bash
DB_FILE=backend/database.py
for b in wt/t_648c8dfb wt/t_765e2702 wt/t_79ce9f6f wt/t_c9de841e wt/t_fb76d7ef origin/main; do
  git show $b:$DB_FILE 2>/dev/null | grep -oE '^def [a-zA-Z0-9_]+\(' | sed 's/def //;s/(//' | sort -u > /tmp/dbfunc_${b//\//_}.txt
done
# then in Python: for each branch, missing = (branch_funcs - mergebase_funcs) - main_funcs
```

Schemas: same pattern against `backend/schemas.py` (`^class \w+`).

## Two sub-lessons the single-PR diagnosis misses

### Sub-lesson A — `404` is NOT an auth failure

CI output like `test_x_requires_auth - assert 404 in (401, 422)` is NOT an auth
problem. A route that returns 401/422 must first EXIST. `404` means the route is
not registered at all — auth is never reached. Before concluding "auth issue on the
runner", grep `origin/main:<file>` for the symbol. If it's 0, it's a clobber (Pattern 9),
not auth. The user will often misread a cluster of `404`s as auth/runner flakiness;
correct it explicitly and verify the route's presence first.

### Sub-lesson B — URL-prefix mismatch between tests / frontend / backend

Even after restoring a route, tests can still 404 if they call the WRONG PATH.
Sources of truth, in priority order:
1. **Frontend service file already merged** (`git show origin/main:frontend/src/services/<svc>.js`)
   — this is the contract; the route path must match it.
2. The backend route on `origin/main`.
3. The failing test file (LOWEST priority — tests are often written against a
   worktree's divergent path convention and are the thing that's wrong).

Real example: tests called `/api/steps/5/modules/{id}/quiz-submit` but frontend
and the restored route use `/api/steps/5/training-modules/{id}/quiz-submit`. The
fix was `sed`/replace_all `/modules/` -> `/training-modules/` in the test file, NOT
changing the (correct) route. Always reconcile to the frontend contract, not the test.

## Table-name fork (deeper clobber)

When auditing DB functions, also check whether the *table* a function queries
survived. A clobbered PR may have added a `user_quiz_attempts` table DDL + functions
that query it, while `main` silently kept an older `quiz_attempts` table and rewrote
the merged functions to use it. Verify no dangling references remain:
```bash
git grep -c "user_quiz_attempts" origin/main -- backend/   # expect 0 if main uses quiz_attempts
```
If 0 and the restored route writes to the surviving table, the feature is whole —
no broken references. No need to resurrect the dead table name.

## Real-world result (GH-478/479/486 batch, Jul 21)

Bulk audit of 8 backend PR branches found exactly 2 clobbered routes:
- `POST /step6/experiments/{id}/promote-to-sop` (PR #524) -> restored in #534
- `POST /steps/5/training-modules/{id}/quiz-submit` (PR #528) -> restored in #535

Plus a URL-prefix mismatch in the GH-479 test file (`/modules/` vs `/training-modules/`)
fixed in #535, and a dead `user_quiz_attempts` table DDL that was safely superseded
by `quiz_attempts` on main. All other backend PR branches (db, api, checklist,
unicorn, tests) verified clean — no further clobbers.
