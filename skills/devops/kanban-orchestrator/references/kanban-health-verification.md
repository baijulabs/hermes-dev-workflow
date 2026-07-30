# Kanban Health Verification Checklist

Systematic checklist for verifying the full kanban pipeline after DB recovery, gateway changes, service restarts, or corruption remediation.

## Layer 1 — Database Integrity

```bash
BOARD_DIR=~/.hermes/kanban/boards/<board-slug>

# 1. Full integrity check
sqlite3 "$BOARD_DIR/kanban.db" "PRAGMA integrity_check;"
# Expected: "ok"

# 2. Board state summary
sqlite3 "$BOARD_DIR/kanban.db" \
  "SELECT status, COUNT(*) FROM tasks GROUP BY status ORDER BY COUNT(*) DESC;"

# 3. Check for stale lock files
ls -la "$BOARD_DIR"/kanban.db.{dispatch,init}.lock 2>/dev/null
# Expected: no files (or files with recent timestamps if gateway is actively running)
```

## Layer 2 — Gateway Services

```bash
# 1. List all running gateway services
systemctl --user list-units --type=service --state=running --no-legend --plain "hermes-gateway*"

# 2. Verify each gateway's profile
for f in ~/.config/systemd/user/hermes-gateway*.service; do
  name=$(basename "$f" .service)
  profile=$(grep -oP '(?<=--profile )\w+' "$f" || echo "default")
  echo "$name → profile: $profile"
done

# 3. Check orchestrator gateway is enabled (if it should be)
systemctl --user is-enabled hermes-gateway-orchestrator.service
# Expected: "enabled"

# 4. Verify the messaging-only gateway has dispatch DISABLED
if [ -f ~/.hermes/profiles/personal-assistant/config.yaml ]; then
  grep dispatch_in_gateway ~/.hermes/profiles/personal-assistant/config.yaml
  # Expected: "dispatch_in_gateway: false"
fi

# 5. Verify platform separation — each gateway should only have its intended channels
echo "--- Orchestrator platforms ---"
grep -A 5 "^  platforms:" ~/.hermes/profiles/orchestrator/config.yaml 2>/dev/null || echo "(none)"
echo "--- Personal-assistant platforms ---"
grep -A 5 "^  platforms:" ~/.hermes/profiles/personal-assistant/config.yaml 2>/dev/null || echo "(none)"
echo "--- Coder platforms ---"
grep -A 5 "^  platforms:" ~/.hermes/profiles/coder/config.yaml 2>/dev/null || echo "(none)"
echo "--- Code-reviewer platforms ---"
grep -A 5 "^  platforms:" ~/.hermes/profiles/code-reviewer/config.yaml 2>/dev/null || echo "(none)"
# Expected: only orchestrator and personal-assistant have platforms; no other profile

# 6. Audit channel tokens — each token should live in exactly one profile's .env
echo "--- Channel token audit ---"
found_issues=0
for f in ~/.hermes/.env ~/.hermes/profiles/*/.env; do
  tokens=$(grep -E "TELEGRAM_BOT_TOKEN|WHATSAPP_ENABLED" "$f" 2>/dev/null)
  if [ -n "$tokens" ]; then
    profile=$(echo "$f" | grep -oP 'profiles/\K[^/]+' || echo "default")
    count=$(echo "$tokens" | grep -c .)
    echo "  $profile: $count token(s)"
    if [ "$count" -gt 1 ]; then
      echo "    ⚠ DUPLICATE — env loader reads first occurrence only"
      found_issues=1
    fi
  fi
done
# Expected: TELEGRAM_BOT_TOKEN only in orchestrator; WHATSAPP_ENABLED only in personal-assistant

# 7. Verify the orchestrator gateway has OPENROUTER_API_KEY in its environment
GATEWAY_PID=$(ps aux | grep "hermes.*gateway.*orchestrator" | grep -v grep | awk '{print $2}')
if [ -n "$GATEWAY_PID" ]; then
  key_count=$(tr '\0' '\n' < /proc/$GATEWAY_PID/environ 2>/dev/null | grep -c "^OPENROUTER_API_KEY=")
  if [ "$key_count" -eq 0 ]; then
    echo "⚠  CRITICAL: Orchestrator gateway (PID $GATEWAY_PID) has no OPENROUTER_API_KEY in env"
    echo "   All spawned workers will fail with HTTP 403. Restart gateway with the key set."
  else
    echo "✓  OPENROUTER_API_KEY present in gateway environment"
  fi
else
  echo "⚠  No orchestrator gateway process found"
fi
```

## Layer 3 — Dispatcher & Board Activity

```bash
# 1. Dispatcher lock status (use Python for reliable flock check)
python3 -c "
import fcntl
f = open('/home/user/.hermes/kanban/.dispatcher.lock', 'a+b')
try:
    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    print('Dispatcher lock: FREE')
    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
except BlockingIOError:
    print('Dispatcher lock: HELD — gateway has it')
f.close()
"
# Expected: "HELD" when orchestrator gateway is running

# 2. Kanban CLI list works
hermes -p orchestrator kanban list | head -20
# Expected: readable output with tasks

# 3. Kanban stats works
hermes -p orchestrator kanban stats
# Expected: status/assignee breakdown

# 4. Verify a specific task is readable
hermes -p orchestrator kanban show <any-task-id> | head -5
# Expected: task title, status, assignee, workspace
```

## Layer 4 — Worker Pipeline

```bash
# 1. Check for any running workers
ps aux | grep -E "hermes.*-p coder" | grep -v grep
# Expected: workers present when tasks are in "running" status

# 2. Sample first few worker logs for provider/auth errors (fast check)
for log in $(ls ~/.hermes/kanban/boards/<board-slug>/logs/*.log 2>/dev/null | head -3); do
  error_count=$(grep -c "403\\|401\\|429\\|Key limit\\|Budget limit\\|billing\\|exhausted\\|not found" "$log" 2>/dev/null)
  if [ "$error_count" -gt 0 ]; then
    echo "$(basename $log): $error_count provider/auth errors"
    grep -m1 "403\\|Key limit\\|Budget limit\\|billing\\|exhausted" "$log" 2>/dev/null | head -2
  fi
done

# 3. Check all recent worker logs for errors (full scan)
for log in ~/.hermes/kanban/boards/<board-slug>/logs/*.log; do
  if grep -q "Budget limit\\|Key limit\\|exhausted\\|403\\|401\\|429\\|500\\|model.*not found" "$log" 2>/dev/null; then
    echo "$(basename $log): $(grep -c "403\\|401\\|429\\|500\\|Key limit\\|exhausted\\|not found" "$log") provider errors"
  fi
done

# 4. Check the most recent worker log's tail for real errors
latest=$(ls -t ~/.hermes/kanban/boards/<board-slug>/logs/*.log 2>/dev/null | head -1)
if [ -n "$latest" ]; then
  echo "=== Latest worker log tail ==="
  tail -10 "$latest"
fi

# 4. Verify provider reachability for worker profiles
hermes -p coder chat -q "hello" --quiet
# Expected: responds. "HTTP 403" means budget exhausted.
```

## Layer 5 — Gateway Logs

```bash
# 1. Check gateway journal for dispatcher activity
journalctl --user -u hermes-gateway-orchestrator.service --no-pager |
  grep -E "dispatch|spawn|claim|ready|running|block|complete" | tail -10
# Expected: recent timestamps showing dispatcher ticks

# 2. Check for startup errors
journalctl --user -u hermes-gateway.service --no-pager -n 20 |
  grep -E "error|traceback|exception|fail|cannot"
# Expected: none (warnings about SOUL.md or WhatsApp are normal)

# 3. Check gateway state file for platform connectivity
cat ~/.hermes/profiles/<profile>/gateway_state.json |
  python3 -c "import sys,json; d=json.load(sys.stdin); [print(f'{k}: {v.get(\"state\",\"?\")}') for k,v in d.get('platforms',{}).items()]"
# Expected: platforms show "connected" or expected "disconnected" states
```

## Health Score

| Layer | What it tells you | Pass criteria |
|-------|-------------------|---------------|
| 1 — DB | Data integrity, no corruption | `integrity_check: ok` |
| 2 — Services | Only necessary gateways running | 1-2 gateways, no redundant profile gateways |
| 3 — Dispatcher | Dispatcher is active and claiming tasks | Lock held, CLI commands work |
| 4 — Workers | Workers can execute | No provider errors in logs |
| 5 — Gateway | Gateway is healthy, platforms connected | No crash loop, recent dispatcher ticks |

If all five layers pass, the kanban pipeline is healthy. If a layer fails, the root cause is within that layer — don't chase symptoms in other layers first.