# Case Study: Wrong-Base Worktree Failure

## The Failure

Card `t_c36027fd` (reviewer) caught a coder (`t_9832cdc2`) working on the wrong base branch.

**Timeline:**

1. Orchestrator tasked coder to fix `package.json` and `check-deps.sh` on branch `fix/df-1784774204-save-values-v2` (PR #120)
2. Coder created branch `fix/df-1784829956-frontend-hoisting` **from `main`**, not from the target branch
3. Coder committed only a lockfile regeneration — the actual "fix" code was inherited from `main` commits, not authored on the target branch
4. Coder marked themselves **done** claiming "files were already correct from prior commits"
5. The reviewer caught the discrepancy:
   - On the target branch (`fix/df-1784774204-save-values-v2`), `package.json` was still **missing** `vue`, `@vitejs/plugin-vue`, `react`, `react-dom` from devDependencies
   - `package.json` was missing `@vue/*` compiler overrides
   - `scripts/check-deps.sh` used `@vitest/coverage-v8` sentinel instead of `vue-router`
   - `scripts/check-deps.sh` installed from `$PROJECT_ROOT/frontend` instead of `$PROJECT_ROOT`
6. Reviewer blocked with `review-failed`, card eventually archived by stale cleanup

**Root cause:** The worktree was created from `main`/HEAD instead of the target branch. The coder didn't verify the base, didn't notice commits were inherited from `main`, and committed to a branch descended from `main`. The coder's handoff claimed "before implementing, I verified package.json and check-deps.sh were already correct" — which was true **on `main`**, but false on the target branch.

## The Fix (Three-Layer Defense)

### Layer 1 — Orchestrator (card creation)
- Every coder card must pass `branch_name=<target-branch>` on `kanban_create`
- Every card body must end with `BASE BRANCH: <name>` and a block-on-main instruction
- This sets `$HERMES_KANBAN_BRANCH` in the worker's environment

### Layer 2 — Worktree setup
- When `$HERMES_KANBAN_BRANCH` is set: `git worktree add -b wt/$TASK <path> $BRANCH`
- Creates a NEW branch from the correct base, not from HEAD
- When unset (feature work on main): `git worktree add <path> wt/$TASK`

### Layer 3 — Pre-flight branch verification
Golden rule — every coder must run before writing code:

```bash
git branch --show-current
echo "HERMES_KANBAN_BRANCH=$HERMES_KANBAN_BRANCH"
```

Checks:
- Not on `main`/`master` → block
- Not on a worktree branch (`wt/t_*`, `fix/df-*`) → block
- `$HERMES_KANBAN_BRANCH` doesn't match card body's `BASE BRANCH:` → block

## Prevention Summary

With all three layers in place, the failure would be prevented:

1. Orchestrator creates card with `branch_name="fix/df-1784774204-save-values-v2"`
2. Worktree: `git worktree add -b wt/t_9832cdc2 /path fix/df-1784774204-save-values-v2`
3. Coder runs golden rule → branch is `wt/t_9832cdc2`, base matches card body → proceed
4. Commits go to `wt/t_9832cdc2` based on target → fixes actually land on the correct branch
