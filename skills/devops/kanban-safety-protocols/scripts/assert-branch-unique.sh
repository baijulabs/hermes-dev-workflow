#!/usr/bin/env bash
# assert-branch-unique.sh — Pre-creation guardrail against worktree branch collisions
#
# Usage:  assert-branch-unique.sh <branch-name> [board-slug] [repo-path]
#
# Returns exit 0 if the branch is safe to use.
# Returns exit 1 with details if the branch is already checked out
# by a live git worktree, or queued by an active kanban task.
#
# Call this BEFORE kanban_create --branch <name> when creating
# worktree cards. On collision, either omit --branch (dispatcher
# auto-derives wt/t_<task-id>) or generate a fresh unique name.

set -euo pipefail

BRANCH="${1:-}"
BOARD="${2:-my-project-dev}"
REPO="${3:-$HOME/my-project}"

[ -n "$BRANCH" ] || { echo "Usage: $0 <branch-name> [board-slug] [repo-path]"; exit 2; }

# ---- Check 1: live git worktree (catches branches on disk from completed tasks)
if [ -d "$REPO" ]; then
    LIVE_WT=$(cd "$REPO" && git worktree list 2>/dev/null | awk -v b="$BRANCH" \
        'NF >= 3 && $3 ~ "\\[" b "\\]" {print $1, $3}' | head -1)
    if [ -n "$LIVE_WT" ]; then
        WT_PATH=$(echo "$LIVE_WT" | awk '{print $1}')
        echo "BRANCH COLLISION: '$BRANCH' is already checked out as a worktree at:"
        echo "  $WT_PATH"
        echo "Use a DIFFERENT branch name or omit --branch to let the dispatcher auto-derive."
        exit 1
    fi
fi

# ---- Check 2: kanban DB (catches queued tasks with same branch)
KANBAN_DB="$HOME/.hermes/kanban/boards/$BOARD/kanban.db"
if [ -f "$KANBAN_DB" ]; then
    COUNT=$(sqlite3 "$KANBAN_DB" \
        "SELECT COUNT(*) FROM tasks WHERE branch_name = '$BRANCH'
         AND status NOT IN ('archived', 'done');" 2>/dev/null || echo "0")
    if [ "$COUNT" -gt 0 ]; then
        echo "BRANCH COLLISION: '$BRANCH' is used by $COUNT active kanban task(s)."
        echo "Active tasks:"
        sqlite3 "$KANBAN_DB" \
            "SELECT id, title, status, assignee FROM tasks
             WHERE branch_name = '$BRANCH' AND status NOT IN ('archived', 'done')
             ORDER BY status, created_at;"
        exit 1
    fi
fi

echo "OK: branch '$BRANCH' is unique."
exit 0