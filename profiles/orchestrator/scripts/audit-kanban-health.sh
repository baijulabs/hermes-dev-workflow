#!/bin/bash
set -euo pipefail

# ── CONFIG ──
GATEWAY_SERVICE="hermes-gateway"
KANBAN_DB="${KANBAN_DB:-$HOME/.hermes/kanban/boards/liberkyma-dev/kanban.db}"
PROFILE="${PROFILE:-orchestrator}"
STATE_DIR="$HOME/.hermes/profiles/$PROFILE/state"
STATE_FILE="$STATE_DIR/kanban-health-check.json"

# ── Helpers ──
ISSUES=0

log_issue() {
    echo "⚠️  $1"
    ISSUES=$((ISSUES + 1))
}

# ── 1. Stale worktree dirs ──
STALE_COUNT=0
if [ -d "$HOME/.hermes/kanban/worktrees" ]; then
    STALE_COUNT=$(find "$HOME/.hermes/kanban/worktrees" -maxdepth 1 -type d -mtime +7 2>/dev/null | wc -l || true)
fi
if [ "$STALE_COUNT" -gt 0 ] 2>/dev/null; then
    log_issue "Stale worktree directories: $STALE_COUNT (older than 7 days)"
fi

# ── 2. Telegram connectivity ──
if command -v journalctl &>/dev/null; then
    TELEGRAM_ERRORS=$(journalctl --user -u "$GATEWAY_SERVICE" --since "3 hours ago" --no-pager 2>/dev/null | grep -ci "\[Telegram\].*[Ff]ail\|\[Telegram\].*[Ee]rror\|polling conflict" 2>/dev/null || true)
    if [ "${TELEGRAM_ERRORS:-0}" -gt 2 ] 2>/dev/null; then
        log_issue "Telegram errors in gateway log: $TELEGRAM_ERRORS occurrences in last 3h"
    fi
fi

# ── 3. Kanban DB integrity ──
if [ -f "$KANBAN_DB" ]; then
    DB_INTEGRITY=$(sqlite3 "$KANBAN_DB" "PRAGMA integrity_check;" 2>&1)
    if [ "$DB_INTEGRITY" != "ok" ]; then
        log_issue "Kanban DB corruption detected: $DB_INTEGRITY"
    fi
fi

# ── 4. Issues threshold ──
if [ "$ISSUES" -gt 0 ]; then
    echo "Found $ISSUES issue(s)"
else
    echo "All checks nominal"
fi
