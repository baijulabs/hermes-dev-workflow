# UAT Consolidation Example (2026-07-25)

Real session: 60+ unmerged fix branches from a `uat/dogfood-2026-07-24` cycle consolidated into one PR.

## Branch Landscape

The UAT branch `uat/dogfood-2026-07-24` contained 50+ commits of fixes, most already merged to `main` via individual PRs. However, 60+ local fix branches remained with unmerged commits — many were duplicate iterations of the same fix.

## Branch Categories Found

| Category | Count | Examples |
|----------|-------|----------|
| Vue/NPM hoisting | ~15 | `fix/df-1784750037-vue-hoisting`, `fix/df-1784763893-vue-hoisting`, `fix/prfix-1784774204-frontend-npm-hoisting` |
| DDL/label columns | ~7 | `fix/df-1784766129-ddl-label`, `fix/df-1784768623-ddl-label`, `fix/df-1784831327-ddl-label-column` |
| Deviation gating | ~2 | `fix/df-1784766129-deviation-gating`, `fix/df-1784768623-deviation-gating` |
| Simulation params | ~5 | `fix/df-1784766129-simulation-params`, `fix/df-1784774204-simulation-params-v2`, `fix/df-1784774204-simulation-params-v3` |
| Save values / finalise | ~5 | `fix/df-1784774204-save-values`, `fix/df-1784774204-save-values-v2`, `fix/df-1784774204-save-values-v2-ddl`, `fix/df-1784774204-save-values-v2-docker-fix` |
| Test fixes | ~5 | `fix/df-1784775711-step2-test`, `fix/df-1784775711-step2-test-v2`, `fix/df-1784852601-lint-dup-funcs` |
| Config/CI | ~5 | `fix/df-1784840668-pin-ruff`, `fix/vpc-egress-private-ranges`, `fix/ci-stability-after-430` |
| Agent branches | ~4 | `agent/GH-470`, `agent/GH-477`, `agent/GH-500`, `agent/GH-558-use-markdown-fix` |
| Feature branches | ~2 | `feature/gh-485-kanban-columns-migration`, `feature/video-pipeline-locator-navigation` |

## Key Challenge: All Branches Behind Main

Every fix branch was based on a version of `main` that was ~20 commits behind. Direct merge without `-X theirs` produced conflicts on nearly every shared file.

## Strategy Used

1. **Found base:** `cherrypick/fix-test-assertions` had 26 commits covering most fixes. This became the merge base.

2. **First merge:** `git merge -X theirs cherrypick/fix-test-assertions --no-edit` — accepted all fix branch changes on conflict.

3. **Second phase:** Remaining 25+ branches merged one at a time with `-X theirs`. This was delegated to a background subagent to avoid consuming 25+ conversation turns.

4. **Conflict fallback:** When a merge failed, `git merge --abort` + individual commit cherry-pick with `-X theirs`.

5. **Verification:** `git diff --stat main..HEAD ':!package-lock.json'` to see meaningful code changes without lockfile noise.

## Lessons

- **Always check `git rev-list --count HEAD..$branch`** to see if a branch still needs merging. Many branches already had their content covered by the initial cherrypick merge.
- **`-X theirs` is safe for targeted fix branches** but should be verified against what main had.
- **Sequential merge order:** comprehensive base first, then targeted layers on top.
- **Delegate bulk work** to a background subagent. The sequential merge loop is mechanical — no reasoning needed after the strategy is set.
- **Scope creep costs user patience.** In an earlier session, the user corrected scope three times: (1) "only the UAT fixes" → exclude agent/feature/consolidate branches, (2) "the ones from the GH issues currently open" → open issues are the source of truth, (3) "the fixes are already done on the branches" → search for existing branches, don't write code. Start at step 2.
- **When `gh pr edit` fails with `Projects (classic) is being deprecated`**, skip it — that GraphQL deprecation warning is treated as an error by `gh`. Use `curl -X PATCH` against the REST API `/repos/<org>/<repo>/pulls/<N>` endpoint instead.
- **Cross-reference open issues against branches** with `git log --oneline --all | grep GH-<N>` before implementing anything. If a fix branch exists, merge it. If not, create a kanban card.