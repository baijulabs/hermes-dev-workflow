# pr-consolidate.py — Automated PR consolidation from kanban worktrees

Location on disk: `~/.hermes/profiles/orchestrator/scripts/pr-consolidate.py`

## Core logic

```
Check kanban DB for all coder card statuses
  → If any not "done": exit silently (empty stdout)
  → If all done: proceed with consolidation

Fetch origin/main → create fresh branch off main
For each coder card:
  → Get branch_name from kanban DB
  → Check local branch exists
  → git log <branch> ^main (separate args!) to get new commits
  → Cherry-pick each commit oldest-first
  → If conflict: abort cherry-pick, output ERROR, cleanup

Run backend tests (filtered to affected areas)
Run frontend-all tests
If any test fails: output ERROR, cleanup branch, exit

Push branch → gh pr create → output PR URL
Update state file: pr_created=true
Delete state file (cleanup)
```

## Key design decisions

- no_agent=True: runs in <1 second, no LLM overhead
- State file prevents re-consolidation
- Error output = delivered as notification (non-empty stdout in no_agent mode)
- Works with LOCAL worktree branches — does NOT require origin-pushed branches
- Tests run before push to avoid pushing broken code
- Branch is cleaned up on failure (git checkout main + git branch -D)