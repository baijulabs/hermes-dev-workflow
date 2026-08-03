# Worktree Branch Collision — GH-135 Real-World Case Study

## What Happened

The GH-135 decomposition created two coder cards:
- `t_f9d74d91` — Fix Chat.vue workspaceTitle/StepName/PhaseName i18n → branch `agent/GH-135-chat-vue-i18n` ✅ done
- `t_9a603b11` — Fix workspace.phaseName interpolation params mismatch → branch `agent/GH-135-chat-vue-i18n` ❌ blocked

Both cards had the **same branch name**. The first completed and its worktree remained on disk. When the second tried to create its worktree, git refused:

```
fatal: 'agent/GH-135-chat-vue-i18n' is already used by worktree at
  '$HOME/my-project/.worktrees/t_f9d74d91'
```

## Root Cause

The second card was created during auto-resolution (or decomposition) and **copied the branch name** from the first card. The original coder's `branch_name` was reused, but the original worktree was still on disk.

## Impact

- 1 coder card blocked, consecutive_failures=2
- 1 reviewer card `t_fe7a5b9a` stuck in `todo` (waiting on blocked parent)
- Manual intervention required: assign unique branch `fix/gh-584-interpolation-params`

## Three Guardrails That Now Prevent This

1. **Pre-creation check** — `assert-branch-unique.sh` catches the collision before `kanban_create` is called
2. **Auto-resolution change** — fix cards now omit `--branch` or generate fresh names instead of copying the original
3. **Safety net cron** — `worktree-collision-watch` auto-remediates any collisions that slip through

## Diagnostic Commands

```bash
# Find active worktree branches
cd /path/to/repo && git worktree list

# Find kanban tasks using a specific branch
sqlite3 ~/.hermes/kanban/boards/my-project-dev/kanban.db \
  "SELECT id, title, status FROM tasks WHERE branch_name='agent/GH-135-chat-vue-i18n';"

# Fix: assign unique branch and reset
sqlite3 ~/.hermes/kanban/boards/my-project-dev/kanban.db \
  "UPDATE tasks SET branch_name='fix/gh-584-interpolation-params',
   status='todo', consecutive_failures=0, last_failure_error=NULL
   WHERE id='t_9a603b11';"
```
