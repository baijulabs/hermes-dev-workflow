# Recurring Corruption Walkthrough — Jul 24, 2026

## Timeline

10 corrupt `.bak` files accumulated over 6 days (Jul 19–Jul 24). The board was
recovered multiple times but corruption kept recurring.

| Date | Corrupt Baks (cumulative) | Error Pattern | Action |
|------|--------------------------|---------------|--------|
| Jul 19 | 2 | `idx_events_task` index | Recovered via `.dump`→rebuild |
| Jul 20 | 4 | Page-level `error code=522` | Jul 20 RCA → restarted both gateways |
| Jul 22 | 1 | `Rowid out of order` | Recovery |
| Jul 23 | 3 | `Rowid out of order` + page errors | Recovery |
| Jul 24 | 10+ | `Tree 314 page 714: unable to get page. error code=522` + `Rowid 9763 out of order` + `Rowid 976 out of order` | Current session |

## Error Signature

Unlike the classic `idx_events_task` index corruption (`wrong # of entries in index`),
the Jul 24 corruption showed a **mixed pattern**:

```
*** in database main ***
Tree 314 page 714: unable to get the page. error code=522    ← missing page
Tree 160 page 160 cell 201: Rowid 9763 out of order          ← index b-tree corruption
Tree 2 page 710 cell 13: Rowid 976 out of order              ← index b-tree corruption
```

Page 714 being unreachable (`error code=522`) alongside `Rowid out of order` in
two different trees suggests a **WAL checkpoint race during a WAL wrap-around**:
one connection checkpointed a frame that spanned across a WAL file boundary,
while another connection was writing the same page range. The resulting main DB
page is torn — neither the old version nor the new version, but a fragment of each.

## RCA Findings

### 1. Three gateway instances still active

Despite the Jul 20 RCA recommending that non-orchestrator gateways be disabled,
the systemd services were **never actually disabled**:

```
hermes-gateway-orchestrator.service       ← dispatches workers
hermes-gateway-personal-assistant.service ← dispatch_in_gateway: false but still connects
hermes-gateway.service                    ← TUI/CLI default gateway (starts automatically)
```

Three processes writing to the same DB, all with WAL mode and auto-checkpoints.
The default profile gateway (`hermes-gateway.service`) starts automatically — it
often goes unnoticed because it's not kanban-specific. It was the root cause of the
**third** writer contributing to the WAL checkpoint race.

### 2. Stale lock files

`kanban.db.dispatch.lock` and `kanban.db.init.lock` from Jul 20 (6 days old)
were still present in the board directory. These lock files prevent the dispatcher
from initializing cleanly and can contribute to timing-sensitive races.

### 3. Corrupt backup accumulation

10 corrupt `.bak` files accumulated, plus many orphaned `-shm` and `-wal` sidecar
files. The cleanup command in the SKILL.md (`tail -n +4 | xargs rm -f`) had not
been run after previous recovery cycles. The clutter itself is not harmful, but
it indicates that post-recovery cleanup steps were skipped.

### 4. busy_timeout=0 on the DB file

`PRAGMA busy_timeout` returned 0 on the corrupt DB. This is **expected** for the
on-disk file (the PRAGMA is set per-connection, not stored in the DB), but it
confirms that no connection was holding a write lock when the integrity check ran.

### 5. SOUL identity contained conflicting `--branch main` guidance

The orchestrator's identity file said to pass `--branch main` for feature work.
This caused all 3 coder cards created in this session to be created with
`branch_name=main`, which the dispatcher interprets as the literal worktree branch
name. Since `main` is already checked out at the repo root, `git worktree add` fails
with `"'main' is already used by worktree at '...'"`.

See kanban-safety-protocols "When to Set" table for the correct pattern.

## Root Cause: Operational Gap

The Jul 20 RCA correctly identified the multi-gateway root cause and documented
the fix (`systemctl --user disable hermes-gateway-personal-assistant.service`),
but the **remediation was never executed**. The systemd services were left running,
and the corruption recurred every ~2 days because the underlying condition (3 writers)
was never resolved.

**Lesson:** A documented fix is not a deployed fix. After RCA, either execute the
remediation immediately or create a tracked card with explicit ownership and a
deadline. Leaving it as "recommendation in a reference file" guarantees it won't
happen.

## Guardrail Improvements Needed

### P0 — Disable extra gateway systemd services

```bash
systemctl --user stop hermes-gateway-personal-assistant.service
systemctl --user disable hermes-gateway-personal-assistant.service
systemctl --user stop hermes-gateway.service
systemctl --user disable hermes-gateway.service
systemctl --user restart hermes-gateway-orchestrator.service
```

Or strip the kanban section from the personal-assistant config as documented in
the main SKILL.md (Isolate non-orchestrator gateways section).

### P1 — Add a cron-based watchdog

A `no_agent: true` cron script that runs every 15 minutes checking:

```bash
count=$(systemctl --user list-units --state=running --no-legend --plain "hermes-gateway*" 2>/dev/null | wc -l)
echo "Gateway count: $count"
```

If `count > 2` (orchestrator + personal-assistant without kanban), alert via
Telegram. This catches newly-spawned gateways between systemd service audits.

### P2 — Auto-clean corrupt backups

A `no_agent: true` cron script that runs daily, keeping only the latest 3 corrupt
backups plus the preswap backup, and cleaning orphaned `-shm`/`-wal` sidecar files.

### P3 — Clear stale lock files on gateway startup

The gateway startup sequence should check for and remove `.dispatch.lock` and
`.init.lock` files older than 1 hour before initializing the dispatcher. This
prevents a stale lock from an unclean shutdown from silently blocking the
dispatcher.

## Recovery Steps Used This Session

Unlike previous episodes where `.dump`→rebuild worked, this time the `.dump` output
had schema mismatches (table declared with 35 columns but INSERT had 36 values),
indicating a migration had occurred between the corruption and the dump attempt.
Alternative recovery paths:

1. **`.clone`** — direct page-level copy (blocked by security guard timeout)
2. **`.recover`** — reads rows from b-tree pages, better than `.dump` for corrupt indexes
3. **Restore from clean backup** — scans all `.bak` files for clean integrity + highest task count

**Actual recovery used:** Clean backup scan found 5 files with `PRAGMA integrity_check = ok`.
The most recent was `kanban.db.corrupt.ce03f169354a157c.bak` (2.9MB, 355 tasks, 9823 events,
timestamp Jul 24 16:55). This preserved all cards created during the session including
the GH-439 and GH-549 work. Restored via `cp + chmod 644`, stale lock files removed,
gateway restarted.

Scan command:
```bash
for f in *.bak; do
  result=$(sqlite3 "$f" "PRAGMA integrity_check;" 2>/dev/null)
  if [ "$result" = "ok" ]; then
    tasks=$(sqlite3 "$f" "SELECT COUNT(*) FROM tasks;")
    echo "CLEAN: $f ($(du -h "$f" | cut -f1), $tasks tasks)"
  fi
done
```

## Key Lessons

1. **A documented fix is not a deployed fix.** Track remediation with a concrete
   card or execute it immediately after RCA.
2. **All three corruption patterns** (index mis-count, page unreadable, rowid out of
   order) trace back to the same root cause: multiple gateway connections racing on
   WAL checkpoints.
3. **Corrupt backup accumulation is a diagnostic signal.** 10+ backups = chronic
   condition, not an accident. Stop recovering and start investigating.
4. **Post-recovery cleanup** (removing stale locks, archiving old baks, resetting
   stuck tasks) is part of the procedure, not optional.