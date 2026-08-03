# Consolidation Regression Reference — PR #566 Session

Real session transcript of what broke when a stale consolidation branch
(`consolidate/uat-fixes-20260725`) was merged into `fix/uat-dogfood-consolidated-20260725`.

## Root Cause

The consolidation branch was 71 commits behind `main`. Three-way merge silently
overwrote newer main code with the consolidation branch's older versions. No
conflict markers — git resolved all conflicts in favor of the incoming branch.

## What Got Clobbered

| Original commit | Fix | How it broke |
|----------------|-----|-------------|
| `4be4370` | POST /steps/1/values route | `@router.post` decorator stripped from `save_values` |
| `a30c301` | Express catch-all `/{*path}` | Reverted to `/*` — PathError on deploy |
| PR #120 merge | Step2 403→200 deviation response | Tests expected 403, code returned 200 with `status:"blocked"` |
| Various | Step 6 stages i18n keys | `feasibility`/`design`/`testing` in en.json only, missing in es/fr/pt |
| Various | React 18.3.1 pin | Override removed, bumped to 19.2.8 — 10 MISSING_EXPORT build errors |
| Various | glob in devDependencies | npm v11 workspace hoisting dropped it; find-untranslated.js broke |
| `27161f8` pre-merge commit | Added react 19 + removed override | Actually made things worse — caused the Docker build failure |

## Recovery Iterations

1. **Route decorator loss** — restored `@router.post("/steps/1/values")` + auto_trigger logic
2. **Test expectations** — reverted step2 403 assertions to 200 + `status:"blocked"`
3. **Orphaned tests** — removed 3 test_simulation_parameters tests (route never existed on main)
4. **i18n keys** — added all 6 stage keys to es/fr/pt locales (then pushed to main for pull_request_target)
5. **React pin** — restored 18.3.1 in both devDependencies and overrides
6. **Workflow** — added push trigger to deploy-to-staging and lighthouse job conditions
7. **Catch-all** — restored `/{*path}` syntax (third try — clobbered twice)

## False Close: GH-552

After all 10 issues were checked, GH-552 was found closed but the fix
(commits `50c8d52` + `f30aa13`, 300 lines changed) only existed in worktree
`wt/t_12461cbc` — never merged to main or any consolidation branch.

```bash
# Detection:
git branch --contains 50c8d52 --all
# → wt/t_12461cbc  (only worktree, nothing on main)
```

Reopened the issue and created fresh kanban cards. Lesson: a coder card marked
`done` does not mean the fix is on `main`. Verify every issue number has a
commit on main post-consolidation.

## Prevention

Run `./scripts/pre-merge-check.sh` before creating or updating any consolidation PR.
Also: `git log --oneline main --grep="GH-..."` for every issue number in the PR scope.
