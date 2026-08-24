# Case Study: GH-1701 Never Consolidated — Silent Stranded Worktree Branch

## Timeline (2026-08-23)

| Time (UTC) | Event |
|---|---|
| 17:29 | Orchestrator card `t_1ab4c3fb` decomposes GH-1701; coder card `t_3dc6183a` created |
| 17:30 | Coder claimed + spawned, worktree `wt/t_3dc6183a` created |
| 17:43:47 | Coder `kanban_complete()` — **never pushed the branch to origin** |
| 17:47:24 | Reviewer `t_ffee23a7` completed (approved) |
| next 10m ticks | `build-consolidate-prs.py` ran, silently skipped the pair — no PR created |
| days later | User asks "where is the PR consolidating GH-1701?" — branch was never on origin; orchestrator pushed it manually and created PR #1724 |

## What Actually Happened

1. The coder committed to the worktree branch `wt/t_3dc6183a` locally.
2. The coder called `kanban_complete()` WITHOUT `git push origin wt/t_3dc6183a`.
3. The worktree was later pruned/cleaned; the local branch ref disappeared (the pruning script may delete it, or it existed only in the worktree's metadata).
4. `build-consolidate-prs.py` found the done coder+reviewer pair in the kanban DB, but `get_branch_commit_count("wt/t_3dc6183a")` returned `-1` because neither `origin/wt/t_3dc6183a` (never pushed, never fetched) nor the local branch (gone) existed. The script's `check_dedup_and_branch` returned False → "Branch lost" → silently skipped. **No error, no notification.**
5. Manual `git reflog show origin/wt/t_3dc6183a` after the fact showed a single entry: the orchestrator's manual push. The branch had never been on origin before.

## Root Cause: Detached HEAD from Missing `-b` Flag

The fundamental root cause was that `git worktree add` was called **without the `-b` flag** in the no-base-branch case:

```bash
# Wrong -- used for months
git worktree add <path> wt/t_<task-id>

# Correct -- fixed now
git worktree add -b wt/t_<task-id> <path> HEAD
```

`git worktree add <path> <branch>` checks out an **existing** branch by that name. Since `wt/t_XXXXX` didn't exist yet (first run), the command failed with:
```
fatal: 'wt/t_XXXXX' is not a commit and a branch 'wt/t_XXXXX' cannot be created
```

The runner script fell through to bare `git worktree add <path>`, creating a **detached HEAD** worktree. All coder commits landed on the worktree's internal HEAD pointer -- no named branch ref was ever created. When the worktree was pruned, the HEAD pointer was lost and the commits had no branch ref anywhere.

The user's diagnostic question: "Why do the coders commit to detached worktrees? Did we define the correct working branch to use inside the card contents?" cut straight to the root cause -- the `-b` flag was missing from the canonical `git worktree add` instruction for months.

## Why Existing Guardrails Missed It

- `kanban-worker` Do NOT list says "don't open PRs" — but nothing said "must push the branch".
- `audit-stranded-worktrees.py` (every 2h) fetches `--prune` and scans origin — but the branch was never ON origin, so it had nothing to find.
- The consolidation script has no path to recover commits from a pruned worktree's object DB if the branch ref is gone.
- The DB shows the pair as `done`/`done` — everything looks healthy until someone asks about the missing PR.

## Fixes Applied

1. **Worker guidance** — workers MUST push the worktree branch to origin before `kanban_complete()` (see kanban-worker skill, workspace table). Push is required; opening a PR is still forbidden.
2. **Consolidation script** — added `git fetch origin refs/heads/wt/*:refs/remotes/origin/wt/*` in `build-consolidate-prs.py` and `pr-consolidation-watch.py` so branches pushed by workers are always visible (even if the local worktree is gone).

## Verification / Diagnostic Playbook

When a user asks "where is the PR for GH-N?" and no PR exists:

```bash
# 1. Check the kanban pair status
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db \
  "SELECT id, title, status, branch_name, assignee FROM tasks WHERE title LIKE '%[GH-N]%';"

# 2. Check if the branch ever existed on origin (reflog = ground truth)
cd <repo>
git reflog show origin/wt/t_<task_id>   # no output = never pushed

# 3. Check for the branch locally (worktree may be pruned)
git branch | grep t_<task_id>

# 4. If it only exists in a worktree that is still on disk, push it:
git -C <worktree-path> push origin wt/t_<task_id>

# 5. Create the PR manually (orchestrator):
gh pr create --base main --head wt/t_<task_id> --title "..." --body "Closes #N"
```

## Lessons

- **"Done + done in the DB" ≠ merged.** The kanban pair completing is only the middle of the pipeline; the PR is the deliverable. When a pair is done and no PR exists, treat it as an incident, not a no-op.
- **Do not trust the fetch-based diagnosis without checking reflog.** A first hypothesis ("the script didn't fetch wt/* branches") was plausible but wrong — the branch had never been pushed, so no fetch would find it. `git reflog show origin/<branch>` proves whether a branch was ever on origin.
- **Silent skips are the enemy.** The consolidation script printed nothing for the skipped pair. Any skip path in an automation cron should emit a diagnostic line or notification.
