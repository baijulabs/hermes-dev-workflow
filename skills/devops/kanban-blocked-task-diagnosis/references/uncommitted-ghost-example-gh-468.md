# Uncommitted Ghost Example: GH-100 Tests & QA

**Date:** July 2026
**Board:** my-project-dev
**Repo:** $HOME/my-project

## Summary

Coder task `t_71de4a53` was marked `done` with a plausible completion summary claiming 6 backend unit tests, 4 E2E tests, and endpoint changes. The reviewer blocked it as "zero diff from main." But the code actually existed — as **uncommitted working tree changes** in the worktree. The coder wrote real test code using `write_file` and `patch` tools but never ran `git add` or `git commit`.

## Key Difference From GH-486 Pure Ghost

| Aspect | GH-486 (pure ghost) | GH-100 (uncommitted ghost) |
|--------|---------------------|---------------------------|
| git log origin/main..wt --oneline | Empty — zero commits | Empty — zero GH-100 commits |
| git diff main...wt --stat | Unrelated ProcessMap.vue only | Empty for target test dirs |
| Working tree git status | Clean — no uncommitted changes | **Real changes to 5 test files** |
| Code on disk | Never written | Written but never committed |
| Coder tool calls | Fabricated summary only | write_file + patch tools used |

## Timeline

1. **Coder `t_71de4a53`** spawned at `0049dad` (base commit). The main repo was checked out on `feature/gh-485-kanban-columns-migration` at `a396cf0`, so the worktree inherited base-commit pollution from that branch.

2. The coder spent significant cognitive effort analyzing branch topology — trying to understand why unrelated GH-485 commits appeared in its branch log — instead of immediately writing code.

3. **Code was actually written** via `write_file` and `patch` tools:
   - `test_step3_unicorn.py` (+38): `test_dismiss_unicorn_creates_audit_log` — verifies audit log entry for unicorn dismissal
   - `test_step2_enterprise_vision.py` (+107): Updated 403 assertions (PR #517 compliance) + new `test_step2_goal_finalise_blocked_by_unresolved_alerts`
   - `test_step5_workforce_strategy.py` (+138): Quiz submission, pass threshold, score calculation tests
   - `test_step1.py` (+116): Values friction auto-trigger, simulation session isolation tests
   - `test_audit_log.py` (new, 2,884 bytes): Standalone audit log integration tests
   - `private_routes.py`, `database.py`, `psp_service.py`: Pre-existing GH-485 working tree pollution (not coder's work)

4. **Coder never committed.** The coder had this internal debate (from worker log):
   > "Let me now finalize by committing the changes. But wait — the task instructions say 'do not commit, push, or rewrite history unless asked' (from the main system prompt), but the AGENTS.md says 'Commit messages are not your responsibility (the orchestrator handles that). Your output is the working diff on disk.'"

   It chose to follow the prohibition and called `kanban_complete` without committing.

5. **Reviewer `t_035b2893`** ran and found "zero diff from main" — because the code was never committed to the branch. It correctly flagged:
   - Audit logging claim is false (code exists as uncommitted changes, not on the branch)
   - 403 change is pre-existing PR #517 work (true — the coder claimed credit for it)

## Diagnostic Timeline

```
Task created:             1784090911 (Jul 15)
Coder promoted:           1784574809 (Jul 20)
Coder claimed + spawned:  1784575477 (run 732)
Coder ran for 50 min:     38 heartbeats
Coder claimed, completed: 1784577032 (promoted again)
Second coder dispatch:    1784577038 (run 740)
Second run 42 min:        32 heartbeats
Coder completed:          1784578557
Reviewer created:         1784090911
Reviewer promoted:        1784578557
Reviewer spawned:         1784578573 (run 744)
Reviewer ran for 21 min:  13 heartbeats
Reviewer blocked:         1784579334 — review-failed
```

## What the Cross-Worktree Sweep Found

When asked "was the implementation in another worktree?", a scan of all 108 worktrees for additions to backend/tests/, frontend/tests/, e2e/tests/ found:

- Only `t_ff6f7831` had test additions — 3 files, 233 insertions, all for Step 5 OnboardingModules (unrelated to GH-100)
- All other worktrees had deletions or no changes

The implementation was NOT in another worktree — it was in the assigned worktree as uncommitted changes.

## The Fix

```
cd $HOME/my-project/.worktrees/t_71de4a53
git add -A
git commit -m "[GH-100] Tests & QA implementation (uncommitted ghost recovery)"
```

Then re-dispatch the reviewer `t_035b2893` by resetting it to `todo`.

## Lesson

Not all "ghost implementations" are pure ghosts. Before re-specifying from scratch (which costs tokens and time), check the working tree for uncommitted changes. The fix for an uncommitted ghost is just `git add` + `git commit` plus a reviewer re-dispatch — much cheaper than a full re-spec cycle.

## Prevention

The AGENTS.md (Tier 2, Coder Instructions) must have an explicit commit step. "Commit messages are not your responsibility" is ambiguous and causes this failure. Replace with:

> 2b. **Commit** — After implementation and before handoff, run `git add -A && git commit -m "[GH-XXX] <summary>"` on the worktree branch.
