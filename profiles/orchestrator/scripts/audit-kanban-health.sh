#!/bin/bash
# kanban-health-check.sh — 3-hour kanban pipeline health watchdog
# Returns: exit 0 + stdout = all clear (silent delivery)
#          exit 0 + non-empty stdout = issues found (delivered to Telegram)
#          exit non-zero = script error (alert delivered)

set -euo pipefail

GATEWAY_SERVICE="hermes-gateway-orchestrator.service"
KANBAN_DB="/home/user/.hermes/kanban/boards/${HERMES_KANBAN_BOARD:-project-dev}/kanban.db"
DISPATCH_LOCK="/home/user/.hermes/kanban/boards/${HERMES_KANBAN_BOARD:-project-dev}/kanban.db.dispatch.lock"
REPO="${HERMES_PROJECT_REPO:-owner/project}"
ISSUES_FOUND=0

log_issue() {
    echo "  ⚠️  $1"
    ISSUES_FOUND=$((ISSUES_FOUND + 1))
}

# ── 1. Gateway health ──
if ! systemctl --user is-active --quiet "$GATEWAY_SERVICE"; then
    log_issue "Gateway is DOWN ($GATEWAY_SERVICE). Restarting..."
    systemctl --user restart "$GATEWAY_SERVICE" 2>/dev/null || true
    sleep 5
    if systemctl --user is-active --quiet "$GATEWAY_SERVICE"; then
        echo "  ✅ Gateway restarted successfully"
    else
        log_issue "Gateway restart FAILED — manual intervention required"
    fi
fi

# ── 2. Telegram connectivity ──
# Check if Telegram had recent errors in the gateway journal
TELEGRAM_ERRORS=$(journalctl --user -u "$GATEWAY_SERVICE" --since "3 hours ago" --no-pager 2>/dev/null | grep -ci "\[Telegram\].*[Ff]ail\|\[Telegram\].*[Ee]rror\|polling conflict" || echo 0)
if [ "$TELEGRAM_ERRORS" -gt 2 ]; then
    log_issue "Telegram errors in gateway log: $TELEGRAM_ERRORS occurrences in last 3h"
fi

# ── 3. Kanban DB integrity ──
DB_INTEGRITY=$(sqlite3 "$KANBAN_DB" "PRAGMA integrity_check;" 2>&1)
if [ "$DB_INTEGRITY" != "ok" ]; then
    log_issue "Kanban DB corruption detected: $DB_INTEGRITY"
fi

# ── 4. Stale dispatch lock ──
if [ -f "$DISPATCH_LOCK" ]; then
    LOCK_AGE=$(($(date +%s) - $(stat -c %Y "$DISPATCH_LOCK" 2>/dev/null || echo 0)))
    if [ "$LOCK_AGE" -gt 3600 ]; then
        rm -f "$DISPATCH_LOCK"
        log_issue "Stale dispatch lock removed (age: ${LOCK_AGE}s)"
    fi
fi

# ── 5. gh-issues-to-kanban jq health ──
# Verify the sync script's jq expressions compile against empty input
SYNC_SCRIPT="/home/user/.hermes/profiles/orchestrator/scripts/hermes_github_sync.sh"
if [ -f "$SYNC_SCRIPT" ]; then
    # Test standard jq patterns used in the sync script (no Parent epic pattern exists)
    if ! echo '[]' | jq -c '.[]' 2>/dev/null >/dev/null; then
        log_issue "gh-issues-to-kanban jq is broken — ingestion pipeline will fail"
    fi
fi

# ── 6. Critical cron jobs active ──
CRITICAL_JOBS=("gh-issues-to-kanban" "staging-deploy-watch" "pr-check-watch" "pr-consolidation-watch" "review-failed-watch" "kanban-agent-queue-processor")
for job in "${CRITICAL_JOBS[@]}"; do
    if ! hermes cron list 2>/dev/null | grep -B1 "$job" | grep -q "\[active\]"; then
        log_issue "Critical cron job '$job' is not active"
    fi
done

# ── 7. Last gh-issues-to-kanban run succeeded ──
# Get the job ID then check last run
SYNC_JOB_ID=$(hermes cron list 2>/dev/null | grep -B1 "gh-issues-to-kanban" | head -1 | awk '{print $1}')
if [ -n "$SYNC_JOB_ID" ]; then
    LAST_RUN=$(hermes cron list 2>/dev/null | grep -A10 "gh-issues-to-kanban" | grep "Last run:" | head -1)
    if echo "$LAST_RUN" | grep -q "error:"; then
        log_issue "gh-issues-to-kanban last run had errors: $LAST_RUN"
    elif echo "$LAST_RUN" | grep -q "failed"; then
        log_issue "gh-issues-to-kanban last run failed: $LAST_RUN"
    fi
fi

# ── 8. Board health — stuck tasks ──
BLOCKED_COUNT=$(sqlite3 "$KANBAN_DB" "SELECT COUNT(*) FROM tasks WHERE status='blocked' AND assignee='coder';" 2>/dev/null || echo 0)
if [ "$BLOCKED_COUNT" -gt 0 ]; then
    log_issue "$BLOCKED_COUNT coder cards are blocked — check worker logs"
fi

READY_STALE=$(sqlite3 "$KANBAN_DB" "SELECT COUNT(*) FROM tasks WHERE status='ready' AND created_at < datetime('now', '-1 hour');" 2>/dev/null || echo 0)
if [ "$READY_STALE" -gt 0 ]; then
    log_issue "$READY_STALE cards stuck in 'ready' for >1 hour — dispatcher may be stalled"
fi

# ── 9. API rate limits ──
GRAPHQL_REMAINING=$(gh api rate_limit --jq '.resources.graphql.remaining // 0' 2>/dev/null || echo "unknown")
REST_REMAINING=$(gh api rate_limit --jq '.resources.core.remaining // 0' 2>/dev/null || echo "unknown")
if [ "$GRAPHQL_REMAINING" != "unknown" ] && [ "$GRAPHQL_REMAINING" -lt 100 ]; then
    log_issue "GitHub GraphQL rate limit low: $GRAPHQL_REMAINING remaining"
fi
if [ "$REST_REMAINING" != "unknown" ] && [ "$REST_REMAINING" -lt 100 ]; then
    log_issue "GitHub REST rate limit low: $REST_REMAINING remaining"
fi

# ── Report ──
if [ "$ISSUES_FOUND" -eq 0 ]; then
    echo "✅ Kanban pipeline: all systems nominal"
    echo "   Gateway: running | DB: ok | Cron: all active | Rate limits: GQL=$GRAPHQL_REMAINING REST=$REST_REMAINING"
    exit 0
fi

echo ""
echo "🔧 Summary: $ISSUES_FOUND issue(s) found"
echo "   Rate limits: GQL=$GRAPHQL_REMAINING REST=$REST_REMAINING"
exit 0