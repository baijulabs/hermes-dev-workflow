# Auto-Resolution Worktree Branch Collision

## The Pattern

When the Automated Review-Failed Resolution playbook creates a new fix card, it copies the original coder card's `branch_name` (e.g., `agent/GH-584-chat-vue-i18n`) into the new card's `--branch` parameter. But that branch is **still checked out** as a worktree by the completed coder card. When the fix card's dispatcher tries `git worktree add`, git refuses:

```
fatal: 'agent/GH-584-chat-vue-i18n' is already used by worktree at
       '/home/user/project/.worktrees/t_f9d74d91'
```

The fix card crashes on spawn (`spawn_failed`), and its paired reviewer stays in `todo` forever waiting for a parent coder that will never complete.

## Real Example (Jul 28)

| Card | Status | Role |
|---|---|---|
| `t_f9d74d91` | `done` | Original coder — committed code on `agent/GH-584-chat-vue-i18n` |
| `t_1fc882c9` | `archived` | Original reviewer — blocked with `review-failed:` (interpolation params mismatch) |
| `t_9a603b11` | `blocked` | **Fix card from auto-resolution** — failed with `spawn_failed` because it reused branch `agent/GH-584-chat-vue-i18n` |
| `t_fe7a5b9a` | `todo` | **Paired reviewer** — stuck forever because parent coder `t_9a603b11` never completed |

The auto-resolution correctly read the reviewer's findings (interpolation params `{id}→{phaseId}` and `{name}→{phaseName}` in locale files) and created a fix card. But it set `branch=agent/GH-584-chat-vue-i18n` (the original coder's branch), which was already active.

## Fix

1. **Assign a new unique branch** to the stuck coder card:
   ```bash
   sqlite3 ~/.hermes/kanban/boards/my-project-dev/kanban.db \
     "UPDATE tasks SET branch_name='fix/gh-584-interpolation-params', status='todo', consecutive_failures=0, last_failure_error=NULL WHERE id='t_9a603b11';"
   ```

2. **The paired reviewer** (`t_fe7a5b9a`) will auto-promote from `todo` to `ready` when the coder completes.

3. **Verify** the new branch isn't already in use:
   ```bash
   cd /path/to/repo && git worktree list | grep -c "fix/gh-584-interpolation-params"
   # Expect 0 — if >0, append a further disambiguator like -v2
   ```

## Prevention

In the auto-resolution flow, the new fix card MUST get a unique branch — never copy `branch_name` from the original coder. Patterns:

| Original coder's branch | Safe fix card branch | Why |
|---|---|---|
| `agent/GH-584-chat-vue-i18n` | `fix/gh-584-interpolation-params` | Describes the fix, not the agent run |
| `fix/df-1784774204-save-values-v2` | `fix/df-1784774204-values-v3` | Increments the version |
| `agent/GH-477` | `fix/gh-477-<descriptor>` | Task ID prefix + descriptor |

After creating the fix card, do a fast spawn check:

```bash
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db \
  "SELECT kind, payload FROM task_events WHERE task_id='<new-card-id>' AND kind='spawn_failed' ORDER BY created_at DESC LIMIT 1;"
```

If `spawn_failed` with a worktree collision error, fix the branch and reset the card before the user even notices the stalled cycle.