# Recurring Corruption Walkthrough — Jul 20, 2026

## Timeline

Three corruption episodes in 5 hours on the `my-project-dev` board, all with the same
pattern: page-level errors (`unable to get the page. error code=522`), not the classic
`idx_events_task` index corruption. All 4 prevention layers (synchronous=FULL,
wal_autocheckpoint=100, redundant-WAL-pragma skip, concurrency caps) were deployed
from the first recovery onward, but corruption kept recurring.

| Time | Event | Gateway State |
|---|---|---|
| 10:54 | First corruption detected. Restored from `.bak` backup. | orchestrator restarted |
| 11:22 | Gateway restarted. Board healthy. | orchestrator new PID |
| ~15:48 | Second corruption. Restored from self-healing backup. | orchestrator restarted |
| 15:50 | Gateway restarted after second recovery. | orchestrator new PID (209901) |
| ~15:52 | Third corruption (the one that hit you in this session). | orchestrator just started minutes ago |

## Root Cause Found

The **personal-assistant gateway** (PID 43958) had been running continuously since
**11:27** — before any of the 3 recovery episodes. It was never restarted when the
orchestrator gateway was restarted, because:

1. The recovery procedure in the kanban-system-health skill only said to
   stop/restart `hermes-gateway-orchestrator.service`
2. The personal-assistant gateway has `dispatch_in_gateway: false`, so it doesn't
   dispatch workers — but it still connects to the shared kanban DB for status checks
3. Its cached module imports had older pragma settings that raced with the newly
   restarted orchestrator gateway's checkpoint cycle

## Diagnosis Commands That Found It

```bash
# List ALL running gateway processes
ps aux | grep "hermes.*gateway run" | grep -v grep
# Returns: personal-assistant gateway PID 43958, started 11:27
#          orchestrator gateway PID 209901, started 15:50

# Check personal-assistant config
cat ~/.hermes/profiles/personal-assistant/config.yaml
# dispatch_in_gateway: false — no dispatch, but still connects for status
```

## Fix Applied

```bash
systemctl --user restart hermes-gateway-personal-assistant.service
systemctl --user restart hermes-gateway-orchestrator.service
```

Both gateways restarted with fresh processes. No further corruption after this.

## Key Lessons

1. **Always restart ALL gateways**, not just the orchestrator. Use the bulk command:
   ```bash
   for unit in $(systemctl --user list-units --state=running --no-legend --plain "hermes-gateway*" | awk '{print $1}'); do
     systemctl --user restart "$unit"
   done
   ```

2. **`dispatch_in_gateway: false` does NOT mean "no DB connection"** — the gateway
   still opens connections to the kanban DB for queries, and those connections carry
   the same WAL pragma risks as a dispatching gateway.

3. **When corruption recurs immediately after recovery**, check the non-orchestrator
   gateways before assuming the code-level fixes are wrong. A stale process is often
   the cause, not a missing fix.