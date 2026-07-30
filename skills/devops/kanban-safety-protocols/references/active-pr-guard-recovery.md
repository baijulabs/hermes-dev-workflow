# Active PR Guard Recovery

## Problem

After a coder completes work and opens a PR, the card may be unblocked (e.g., after a `review-required` → unblocked cycle). The dispatcher tries to re-spawn the coder but detects an active PR and guards the spawn with `respawn_guarded` (reason `active_pr`). The card stays in `ready` forever — the guard correctly prevents duplicate work, but the dispatcher keeps logging "ready queue non-empty for N ticks but 0 workers spawned" warnings.

## Timeline (Real Example: GH-585 `t_c7b9a4f7`)

1. Coder spawned → worked → opened PR #589  
2. Coder blocked with `review-required`  
3. Card unblocked → dispatcher tries to re-spawn  
4. Guard fires: `respawn_guarded` (reason: `active_pr`)  
5. Card stays `ready` for 300+ dispatcher ticks  
6. Gateway logs: `"ready queue non-empty for 296 consecutive ticks but 0 workers spawned"`  
7. Fix: move to `triage` so the orchestrator picks up PR consolidation  

## Diagnosis

Check event history for the pattern:

```bash
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db "
SELECT kind, created_at
FROM task_events
WHERE task_id = '<id>'
ORDER BY created_at DESC
LIMIT 10;
"
```

Look for:
- `blocked` with `review-required` — coder completed, opened PR
- `unblocked` — someone cleared the block
- `respawn_guarded` (reason `active_pr`) — repeated 5+ times
- No intervening `claimed` or `spawned` — guard prevents re-spawn

## Automated Recovery

The `active-pr-guard-watch` cron job (every 5 min, no_agent) detects this pattern:

1. Queries for `status=ready` cards with 5+ consecutive `respawn_guarded` events (no intervening `claimed`/`spawned`)
2. Moves them to `status=triage` (assignee `orchestrator`)  
3. Delivers Telegram notification when remediation is applied

### Script

`scripts/active-pr-guard-watch.py` from the orchestrator scripts directory.

### Cron creation

```bash
hermes cron create \
  --name active-pr-guard-watch \
  --schedule "every 5m" \
  --deliver telegram \
  --no-agent \
  --script active-pr-guard-watch
```

The script must be symlinked to `~/.hermes/scripts/` for the cron system to find it.

### Manual fix (one-off)

```bash
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db "
UPDATE tasks
SET status = 'triage'
WHERE id = '<task-id>' AND status = 'ready';
"
```

## Distinction from Pattern 6 (stuck ready with failures)

| Aspect | active_pr guard | Pattern 6 (high failures) |
|--------|-----------------|---------------------------|
| Event pattern | `respawn_guarded` only | `spawn_failed` / `crashed` |
| Worker attempted? | Yes (successfully) | Yes (failed) |
| PR exists? | Yes | No |
| Fix | Move to `triage` | Block and investigate worker crash |