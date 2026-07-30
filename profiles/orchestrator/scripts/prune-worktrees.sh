#!/bin/bash
set -euo pipefail
# prune-worktrees.sh — Remove stale git worktrees from completed kanban tasks.
# Runs as no_agent=true cron job. Empty stdout = silent.

REPO_DIR="$HOME/MyProject"
KANBAN_DB="$HOME/.hermes/kanban/boards/${HERMES_KANBAN_BOARD:-my-project-dev}/kanban.db"

cd "$REPO_DIR"

# Get active (non-terminal) task IDs
ACTIVE=$(sqlite3 "$KANBAN_DB" "SELECT id FROM tasks WHERE status NOT IN ('done','cancelled','archived');" 2>/dev/null || true)

PRUNED=0
for WT in .worktrees/t_*; do
    [ -d "$WT" ] || continue
    WTNAME=$(basename "$WT")
    # Check if task is still active
    if ! echo "$ACTIVE" | grep -qxF "$WTNAME"; then
        # Remove the .git file first to detach from git
        rm -f "$WT/.git" 2>/dev/null
        # Remove the entire worktree directory
        rm -rf "$WT" 2>/dev/null && PRUNED=$((PRUNED+1)) || true
    fi
done

# Clean up git worktree metadata
git worktree prune --expire=now 2>/dev/null || true

if [ "$PRUNED" -gt 0 ]; then
    echo "Pruned $PRUNED stale worktrees"
fi