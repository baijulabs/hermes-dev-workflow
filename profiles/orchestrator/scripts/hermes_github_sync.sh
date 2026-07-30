#!/bin/bash
set -euo pipefail

# --- CONFIGURATION ---
REPO="${HERMES_PROJECT_REPO:-my-org/MyProject}"
TRIGGER_LABEL="ready-for-agent"
BOARD_SLUG="${HERMES_KANBAN_BOARD:-my-project-dev}"

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
            --body "GitHub Issue #$ISSUE_NUM: $BODY" \
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
    EXISTING=$(sqlite3 "/home/user/.hermes/kanban/boards/$BOARD_SLUG/kanban.db" \
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

# 2. RESOLUTION: Look for completed cards on the board and update GitHub
echo "Checking Hermes for completed automation cards..."
done_tasks=$(hermes kanban --board "$BOARD_SLUG" list --status done 2>/dev/null || true)

# Extract all GitHub issue references from done card titles:
#   [GH-N]  — e.g. [GH-470] (epic reference)
#   #N      — e.g. #489 (sub-issue reference)
# Merge both lists so every done card closes ALL issues it references.
echo "$done_tasks" | grep -oP '\[GH-\d+\]' 2>/dev/null | sed 's/\[GH-//;s/\]//' > /tmp/gh_sync_numbers.$$ || true
echo "$done_tasks" | grep -oP '#\d+' 2>/dev/null | tr -d '#' >> /tmp/gh_sync_numbers.$$ || true
sort -u /tmp/gh_sync_numbers.$$ | while read -r ISSUE_NUM; do
    [ -z "$ISSUE_NUM" ] && continue

    # Guard 1: skip if this issue is actually a PR (would close open pull requests)
    if gh pr view "$ISSUE_NUM" --repo "$REPO" --json id &>/dev/null; then
        echo "Skipping #$ISSUE_NUM — it's a pull request, not an issue."
        continue
    fi

    # Guard 2: skip EPIC-labeled issues — they are containers for sub-tasks,
    # not atomic work items. An epic should only be closed when ALL its
    # child issues are done, handled manually or by a separate process.
    ISSUE_LABELS=$(gh issue view "$ISSUE_NUM" --repo "$REPO" --json labels --jq '[.labels[].name] | join(",")' 2>/dev/null || echo "")
    if echo "$ISSUE_LABELS" | grep -q "epic"; then
        echo "Skipping #$ISSUE_NUM — labeled 'epic'. Epics are not auto-closed; they contain child sub-tasks."
        continue
    fi

    # Guard 3: skip if the issue has open child issues (sub-tasks referencing it as parent).
        # Check for GH issue references in the body that are still OPEN.
        # Simple heuristic: list any issues whose body contains 'Parent epic: #ISSUE_NUM' and are open.
        OPEN_CHILDREN=$(gh issue list --repo "$REPO" --state open --json number,title 2>/dev/null | \
            jq -r --arg parent "$ISSUE_NUM" '.[] | select(.title | test("Parent epic: #" + $parent)) | "#\\(.number) \\(.title")')
        if [ -n "$OPEN_CHILDREN" ]; then
            echo "Skipping #$ISSUE_NUM — has open child sub-tasks:"
            echo "$OPEN_CHILDREN" | while read -r child; do echo "  $child"; done
            continue
        fi

        # Guard 4: skip if the kanban card that triggered this close has child cards
        # still in progress. Orchestrator epics decompose into coder+reviewer pairs;
        # the epic reaching 'done' means decomposition finished, NOT implementation.
        # Only close when all child cards are done/archived/cancelled.
        KANBAN_DB="/home/user/.hermes/kanban/boards/${HERMES_KANBAN_BOARD:-my-project-dev}/kanban.db"
        PARENT_IDS=$(sqlite3 "$KANBAN_DB" \
          "SELECT DISTINCT t.id FROM tasks t
           WHERE (t.title LIKE '%[GH-$ISSUE_NUM]%' OR t.title LIKE '%#$ISSUE_NUM%')
             AND t.status = 'done'
             AND t.assignee = 'orchestrator';" 2>/dev/null || true)
        if [ -n "$PARENT_IDS" ]; then
          for pid in $PARENT_IDS; do
            IN_FLIGHT=$(sqlite3 "$KANBAN_DB" \
              "SELECT COUNT(*) FROM task_links l
               JOIN tasks c ON c.id = l.child_id
               WHERE l.parent_id = '$pid'
                 AND c.status NOT IN ('done', 'archived', 'cancelled')
                 AND c.assignee = 'coder';" 2>/dev/null || echo 0)
            if [ "$IN_FLIGHT" -gt 0 ]; then
              echo "Skipping #$ISSUE_NUM — orchestrator card $pid has $IN_FLIGHT coder child(ren) still in flight."
              continue 2  # skip to next ISSUE_NUM
            fi
          done
        fi

    echo "Processing completed task for GitHub #$ISSUE_NUM"

    # Close the issue and comment
    gh issue close "$ISSUE_NUM" --repo "$REPO" 2>/dev/null || true
    gh issue edit "$ISSUE_NUM" --repo "$REPO" --remove-label "$TRIGGER_LABEL" 2>/dev/null || true
    gh issue comment "$ISSUE_NUM" --repo "$REPO" --body '✅ **Automated Resolution:** This task was completed by the Hermes agent pool.' 2>/dev/null || true

    # Archive the completed cards on the Kanban board to prevent infinite closure loops and enable future reopens
    sqlite3 "/home/user/.hermes/kanban/boards/${HERMES_KANBAN_BOARD:-my-project-dev}/kanban.db" \
      "UPDATE tasks SET status = 'archived' WHERE (title LIKE '%[GH-$ISSUE_NUM]%' OR title LIKE '%#$ISSUE_NUM%') AND status = 'done';" 2>/dev/null || true

    echo "Closed GitHub #$ISSUE_NUM and removed label, archived corresponding kanban cards."
done

echo "=== Sync Tick Completed Successfully ==="

# Cleanup
rm -f /tmp/gh_sync_numbers.$$