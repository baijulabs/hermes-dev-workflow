#!/bin/bash
set -euo pipefail

# --- CONFIGURATION ---
REPO="${HERMES_PROJECT_REPO:-owner/project}"
TRIGGER_LABEL="ready-for-agent"
BOARD_SLUG="${HERMES_KANBAN_BOARD:-project-dev}"

echo "=== Hermes GitHub Sync Tick: $(date) ==="

# 1. INGESTION: Pull matching issues from GitHub into Hermes Kanban
echo "Polling GitHub Project Board for labeled issues..."
# Wrap entire block in || true: if GH API rate-limited, skip ingestion silently this tick
# (set -o pipefail + set -e would kill the script on a single gh failure without this)
gh issue list --repo "$REPO" --label "$TRIGGER_LABEL" --state open --json number,title,body 2>/dev/null | jq -c '.[]' 2>/dev/null | while read -r issue; do
    ISSUE_NUM=$(echo "$issue" | jq -r '.number')
    TITLE=$(echo "$issue" | jq -r '.title')
    BODY=$(echo "$issue" | jq -r '.body')

    TASK_SIG="[GH-$ISSUE_NUM]"

    # Use -F for fixed-string grep to avoid regex bracket issues
    if ! hermes kanban --board "$BOARD_SLUG" list 2>/dev/null | grep -Fq "$TASK_SIG"; then
        echo "Found new issue: #$ISSUE_NUM. Injecting into Hermes board: $BOARD_SLUG"

        # Create task with issue number in body for traceability
        hermes kanban --board "$BOARD_SLUG" create "$TASK_SIG $TITLE" \
            --body "GitHub Issue #$ISSUE_NUM: $BODY\n\nThe GH issue is the source of truth — never closed by automation, only by PR merge (Closes #XXX). Kanban cards are ephemeral implementation artifacts." \
            --assignee orchestrator
    fi
done || true

# 1b. PR INGESTION: Pull labeled PRs (review feedback, not automated failures).
# pr-check-watch handles CI/conflict detection. This path handles human-initiated
# review comments assigned to the agent via the ready-for-agent label.
echo "Polling for labeled PRs with review feedback..."
gh pr list --repo "$REPO" --label "$TRIGGER_LABEL" --state open --json number,title,headRefName,body 2>/dev/null | jq -c '.[]' 2>/dev/null | while read -r pr; do
    PR_NUM=$(echo "$pr" | jq -r '.number')
    PR_TITLE=$(echo "$pr" | jq -r '.title')
    PR_BRANCH=$(echo "$pr" | jq -r '.headRefName')
    PR_BODY=$(echo "$pr" | jq -r '.body')

    # Dedup: skip if pr-check-watch or a previous run already created cards for this branch
    KANBAN_DB="$HOME/.hermes/kanban/boards/$BOARD_SLUG/kanban.db"
    EXISTING=$(sqlite3 "$KANBAN_DB" \
      "SELECT COUNT(*) FROM tasks
       WHERE branch_name = '$PR_BRANCH'
         AND status NOT IN ('done', 'archived', 'cancelled');" 2>/dev/null || echo 0)
    if [ "$EXISTING" -gt 0 ]; then
        echo "  PR #$PR_NUM: $EXISTING card(s) already in flight on branch $PR_BRANCH — skipping"
        continue
    fi

    # Get the latest review comments on this PR (skip automated comments)
    REVIEW_COMMENT=$(gh api "repos/$REPO/pulls/$PR_NUM/comments" --jq '
      [.[] | select(.user.login != "app/dependabot" and .user.login != "github-actions[bot]")]
      | sort_by(.created_at)
      | .[-1].body // ""
    ' 2>/dev/null || echo "")
    
    # Extract actionable review context
    REVIEW_CTX=""
    if [ -n "$REVIEW_COMMENT" ]; then
        REVIEW_CTX="Review feedback: $REVIEW_COMMENT"
    fi

    TASK_TITLE="[PR #$PR_NUM] $PR_TITLE"
    echo "  PR #$PR_NUM: Creating orchestrator card on branch $PR_BRANCH"

    hermes kanban --board "$BOARD_SLUG" create "$TASK_TITLE" \
        --assignee orchestrator \
        --body "GitHub PR #$PR_NUM on branch \`$PR_BRANCH\`.

$REVIEW_CTX

PR body:
$PR_BODY

BASE BRANCH: $PR_BRANCH
CRITICAL: Coder worktrees must be based on this branch. Do NOT commit to main." 2>/dev/null

    # Remove the ready-for-agent label so we don't re-process on next tick
    gh pr edit "$PR_NUM" --repo "$REPO" --remove-label "$TRIGGER_LABEL" 2>/dev/null || true
done || true

# No resolution section — GH issues are NEVER closed by automation.
# Issues close naturally via "Closes #XXX" in PR merge.
# Audit comments are posted by kanban-to-gh-tracker.py.
echo "=== Sync Tick Completed Successfully ==="