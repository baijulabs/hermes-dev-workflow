# Recover a Corrupted Kanban DB — `.recover` Method

Use this when `PRAGMA integrity_check` fails on the live `kanban.db`.

## Why `.recover` over `.dump`

`sqlite3 .recover` reads rows directly from b-tree pages instead of going through the SQL parser. It recovers rows that `.dump` would skip due to corrupt indexes. The downside: indexes are lost and must be rebuilt from the recovered schema on restore.

## Procedure

### 1. Stop the gateway
```bash
systemctl --user stop hermes-gateway-orchestrator.service
```

### 2. Back up the broken files
```bash
cp ~/.hermes/kanban/boards/my-project-dev/kanban.db{,.broken}
```

### 3. Run `.recover`
```bash
cd ~/.hermes/kanban/boards/my-project-dev/
sqlite3 kanban.db ".recover" > recovered.sql
```

### 4. Create a fresh database
```bash
sqlite3 kanban.fresh.db < recovered.sql
```

### 5. Verify
```bash
sqlite3 kanban.fresh.db "PRAGMA quick_check;"
sqlite3 kanban.fresh.db "PRAGMA integrity_check;"
sqlite3 kanban.fresh.db "SELECT status, COUNT(*) FROM tasks GROUP BY status ORDER BY 2 DESC;"
```

### 6. Swap in the fresh DB
```bash
mv kanban.db kanban.db.pre-recover
mv kanban.fresh.db kanban.db
rm -f kanban.db.dispatch.lock kanban.db.init.lock
```

### 7. Restart gateway
```bash
systemctl --user restart hermes-gateway-orchestrator.service
```

### 8. Reset stale state (if any)
```bash
sleep 10
sqlite3 ~/.hermes/kanban/boards/my-project-dev/kanban.db "
UPDATE tasks SET
  status = 'todo',
  consecutive_failures = 0,
  claim_lock = NULL,
  claim_expires = NULL,
  worker_pid = NULL,
  last_failure_error = NULL,
  current_run_id = NULL
WHERE status = 'running';
"
```

## Alternative: Restore from Backup (First Resort)

**Before attempting `.recover`, check if a clean backup already exists.** This is faster and more reliable than rebuilding indexes.

### Step 1: Scan all backup files for clean integrity + highest task count

```bash
scan_dir=~/.hermes/kanban/boards/my-project-dev
for f in "$scan_dir"/*.bak "$scan_dir"/kanban.db.*; do
  [ -f "$f" ] && [ -s "$f" ] && [[ "$f" != *-shm ]] && [[ "$f" != *-wal ]] && [[ "$f" != *-wal ]] && {
    result=$(sqlite3 "$f" "PRAGMA integrity_check;" 2>/dev/null)
    [ "$result" = "ok" ] && {
      tasks=$(sqlite3 "$f" "SELECT COUNT(*) FROM tasks;" 2>/dev/null)
      echo "CLEAN: $(basename "$f") ($(du -h "$f" | cut -f1), $tasks tasks)"
    }
  }
done
```

Pick the most recent clean backup with the highest task count.

### Step 2: Stop the gateway, restore, restart

```bash
systemctl --user stop hermes-gateway-orchestrator.service
cp <chosen-backup> ~/.hermes/kanban/boards/my-project-dev/kanban.db
chmod 644 ~/.hermes/kanban/boards/my-project-dev/kanban.db
rm -f ~/.hermes/kanban/boards/my-project-dev/kanban.db.dispatch.lock
rm -f ~/.hermes/kanban/boards/my-project-dev/kanban.db.init.lock
systemctl --user restart hermes-gateway-orchestrator.service
sleep 5  # Let the gateway start and the dispatcher initialize
```

### Step 3: Verify and reset stale state

```bash
sqlite3 ~/.hermes/kanban/boards/my-project-dev/kanban.db \
  "SELECT status, COUNT(*) FROM tasks GROUP BY status ORDER BY 2 DESC;"
```

Reset any stale `running` tasks (see "Recovery: Mass Unblock After DB Corruption" in the main SKILL.md).

### Alternative: Use `.recover` (Second Resort — when no clean backup exists)
