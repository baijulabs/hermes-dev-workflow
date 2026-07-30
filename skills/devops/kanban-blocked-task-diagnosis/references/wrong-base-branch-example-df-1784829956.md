# Wrong Base Branch — DF-1784829956 (Jul 23, 2026)

## Overview

Coder task `t_9832cdc2` was created to fix frontend build failures (ERR_MODULE_NOT_FOUND for vue) on PR target branch `fix/df-1784774204-save-values-v2` (PR #548). The coder created its worktree from `main` instead of from the target branch. The resulting reviewer card `t_c36027fd` was blocked with `needs_input` after the reviewer correctly identified the base mismatch.

## Card Chain

| Card | Assignee | Status | Role |
|------|----------|--------|------|
| `t_9832cdc2` | coder | done | Implementation — created worktree from wrong base |
| `t_c36027fd` | code-reviewer | blocked (needs_input) | Review — correctly flagged the base mismatch |

## Diagnosis Queries

### Find the card and its state
```sql
SELECT id, title, assignee, status, block_kind, block_recurrences
FROM tasks WHERE id = 't_c36027fd';
```

### Get the task lifecycle
```sql
SELECT kind, payload, created_at FROM task_events
WHERE task_id = 't_c36027fd' ORDER BY created_at;
```

### Read the reviewer's findings
```sql
SELECT author, substr(body, 1, 500) FROM task_comments
WHERE task_id = 't_c36027fd' ORDER BY created_at DESC LIMIT 1;
```

### Check the parent coder task
```sql
SELECT p.id, p.title, p.status, p.branch_name, p.completed_at
FROM task_links tl JOIN tasks p ON tl.parent_id = p.id
WHERE tl.child_id = 't_c36027fd';
```

### Verify the base branch mismatch (from repo root)
```bash
cd /home/user/MyProject/.worktrees/t_9832cdc2
git log --oneline -5 origin/main..HEAD              # Only "build: update package-lock.json" = inherited content
git merge-base HEAD origin/main                       # Shows main as base
```

## Timeline

1. **Decomposition:** DF-1784829956 was decomposed from DF-1784774204 (the ongoing deploy-fix cycle). The coder task specified target branch `fix/df-1784774204-save-values-v2`.
2. **Coder dispatched:** `t_9832cdc2` created worktree `fix/df-1784829956-frontend-hoisting` from `main` (commit `b4875d5`).
3. **Coder completes:** Only authored commit is lockfile regeneration (`3a4aa25`). The substantive `package.json` and `scripts/check-deps.sh` fixes are inherited from `main` commits — not applied by the coder.
4. **Review promoted:** Reviewer card `t_c36027fd` auto-promotes from `todo` to `ready` when coder completes.
5. **Reviewer dispatched:** Claims the card, checks the target branch `fix/df-1784774204-save-values-v2`.
6. **Review fails:** Four issues still broken on target branch:
   - `package.json` missing `vue`, `@vitejs/plugin-vue`, `react`, `react-dom` devDependencies
   - Missing `@vue/*` compiler overrides
   - `scripts/check-deps.sh` uses wrong sentinel (`@vitest/coverage-v8` instead of `vue-router`)
   - Install dir is `$PROJECT_ROOT/frontend` instead of `$PROJECT_ROOT`
7. **Card blocked:** `block_kind=needs_input`. The PR consolidate cron `pr-consolidate-df-1784829956` waits indefinitely.

## Resolution Required

The fix requires a **new coder task** that:
- Branches from `fix/df-1784774204-save-values-v2` (the target), NOT from `main`
- Includes the exact code diff in the card body (old→new for each file)
- Targets only the files that need fixing on the target branch

## Key Insight

The coder profile creates worktrees from `main` by default. When the task card mentions a *different* target branch (`fix/df-XXX`), the coder reads this as "the branch the PR targets" not "the branch the worktree should be based on." The system prompt doesn't instruct the coder to check out the target branch explicitly. This is a coder-profile AGENTS.md gap: worktree base branch selection defaults to `main` unless the card explicitly says "branch from `<target>` NOT from main."

## Related Cards

- PR #548 — The target PR on `fix/df-1784774204-save-values-v2` that needs the fix
- `pr-consolidate-df-1784829956` — The cron job stuck waiting for the blocked reviewer