# Merged-but-missing: second instance + path-prefix divergence (2026-07-21)

## Instance 2: GH-102 quiz-submit clobber (sibling of the GH-486 promote clobber)

Same root cause as the GH-486 promote-to-sop clobber (see
`references/merged-but-missing-stale-base-clobber.md`), different symbol.

Symptom: `backend/tests/test_step5_workforce_strategy.py` 8 tests fail with `404`
on `/api/steps/5/modules/{id}/quiz-submit` and `/api/steps/5/modules/{id}/quiz-attempts`.

Diagnosis confirmed:
- `git show origin/main:backend/api/routers/private_routes.py | grep -c "quiz-submit"` → 0
  (route missing from main)
- The `POST /quiz-submit` route + `get_training_module_quiz()` were in `wt/t_765e2702`
  (PR #111 source) but clobbered by the #111/#112 squash-merges' full-file overwrite.
- Only `GET /quiz-attempts` (from #112) survived on main.

### SECOND bug class: test/implementation path-prefix divergence
The tests used `/steps/5/modules/{id}/quiz-submit` but the merged frontend
(`frontend/src/services/step5Service.js`, already on main) calls
`/steps/5/training-modules/{id}/quiz-submit`. So even after restoring the route,
the tests would STILL 404 because of the `/modules/` vs `/training-modules/` prefix.

**Rule: the frontend on `main` is the source of truth for the URL contract.**
```bash
git show origin/main:frontend/src/services/step5Service.js | grep -n "quiz-submit\|quiz-attempts"
# -> /training-modules/{id}/quiz-submit  => canonical path
```
Fix = align BOTH the restored backend route AND the test paths to `/training-modules/`.

### Fix applied (PR #115)
1. Branch `fix/gh-478-quiz-submit-route` from `origin/main` (stash the phantom-modified
   `private_routes.py` first — dirty-main guard).
2. Re-add `POST /steps/5/training-modules/{module_id}/quiz-submit` route, wired to the
   EXISTING `get_training_module()` + `submit_quiz_attempt()` on main (did NOT invent a
   new function — used the merged contract; note `submit_quiz_attempt` arg order is
   `(db, user_id, module_id, answers, score, passed)`).
3. Re-add `get_training_module_quiz()` to `database.py` (reads the `quiz` column).
4. Add `QuizSubmitRequest` schema to `schemas.py` + import in routes.
5. Add `evaluate_quiz` to the existing `from backend.api.services.workforce_service import`
   line in routes (it was already imported for `get_roles_for_plan` — just extend it).
6. Fix the 25 test path refs `/modules/` → `/training-modules/` via global replace.

### Why the surgical approach (not cherry-pick / whole-file checkout)
The source worktree `wt/t_765e2702` had full-file churn in `private_routes.py`
(15k-line diff from the squash-merge). Cherry-pick explodes; `git checkout -- <file>`
reintroduces #112's regression. Extract only the missing route + DB function block
and patch-insert at the correct anchor (before the existing `GET /quiz-attempts` route).

## Cross-lesson for the orchestrator
When you open many PRs from independent worktrees in one batch, a clobber like this is
LIKELY not isolated. After fixing one, sweep the other PRs for the same pattern:
- For each merged PR that added a route/function to a shared file, verify the symbol is
  still on `origin/main` with `git grep -c <symbol> origin/main -- <file>`.
- The 404 tests in CI are the signal — group failing tests by endpoint and check each
  endpoint's survival on main.
