# Analysis Paralysis Ghost — t_839baf1e (GH-487 Backend Tests)

## Summary

The coder was dispatched to write backend unit tests for checklist persistence and promote-to-SOP (GH-487). The task ran for 1.5 hours (70+ heartbeats), imported `write_file`/`read_file` tools, ran `timeout 600 ./run-tests.sh backend`, and debated between `write_file` and `patch` approaches. The worktree had **zero diff from origin/main** — the coder never actually wrote a single file.

## Root Cause

The backend endpoints the tests were supposed to cover did not exist in the codebase. The original task spec referenced FR-2.2, FR-2.3, FR-2.5 (checklist persistence) and FR-4.1 (promote-to-SOP), but:

- No `promote-to-sop` endpoint exists in `private_routes.py`
- No `checklist_persistence` endpoint exists in `private_routes.py`
- No `checklist` or `promote_to_sop` functions exist in `database.py`
- The PRD file `prd-operational-systems-cx-integration.md` does not exist

The coder read the codebase, found nothing to write tests for, and got stuck in a loop of reading, re-reading, running tests, and debating tool choice — never committing an edit.

## Timeline

| Run | Duration | Failure Mode | Details |
|-----|----------|-------------|---------|
| 288-292 | ~60s | protocol_violation ×3 | Worker exited cleanly without calling kanban_complete |
| 295 | ~60s | protocol_violation ×4 | Same pattern |
| 382 | ~6s | crashed (pid not alive) | Worker died during spawn |
| 408-418 | ~60s | protocol_violation ×3 | Same pattern |
| 444 | ~5s | crashed (pid not alive) | Worker died during spawn |
| 470 | **1.5h** | 70+ heartbeats, then promoted | Worker ran tests, debated tools, never wrote a file |

## Diagnostic Commands

```bash
# Worktree was completely clean
cd /path/to/repo/.worktrees/t_839baf1e
git status --short          # ← empty
git diff --stat             # ← empty

# No unique commits
cd /path/to/repo
git log origin/main..wt/t_839baf1e --oneline
# Only shows: 0049dad fix: import Background and Controls from correct packages

# Worker log showed the debate
grep -E "write_file|patch" ~/.hermes/kanban/boards/my-project-dev/logs/t_839baf1e.log | head -10
# Shows: imports of write_file/patch but no actual calls to write paths

# Test files in worktree were identical to main
git diff origin/main -- backend/tests/test_step4_operational_systems.py backend/tests/test_step6_cx_innovation_lab.py
# ← empty — no changes
```

## Resolution

1. The card was blocked to `orchestrator` with `block_kind='needs_input'` and `last_failure_error='backend endpoints do not exist - feature not implemented yet'`
2. A fresh card should be created with inline code blocks for every test function (exact file paths, function signatures, assertions) to prevent the same paralysis
3. The backend endpoints need to be implemented first (or the test spec needs to target existing endpoints)

## Key Distinction from Pure Ghost (4a) and Uncommitted Ghost (4b)

| Aspect | Pure ghost (4a) | Uncommitted ghost (4b) | Analysis paralysis (4f) |
|--------|----------------|----------------------|------------------------|
| git status --short | Clean | Real changes to target files | Clean |
| git diff main...wt --stat | Empty or unrelated | Empty for committed, real in working tree | Empty |
| Worker log evidence | Plausible summary, no tool calls | write_file/patch calls succeeded | Reads files, runs tests, debates tools, **never writes** |
| kanban_complete called? | Yes | Yes | No — exits via protocol_violation or timeout |
| Run duration | 10–30 min | 30–60 min | 1–2 hours |
| consecutive_failures | 1–2 | 1–2 (per reviewer) | 5+ (keeps getting retried) |