# GitHub Issues → Kanban Board Sync

Pattern for automatically syncing GitHub issues with a specific label to the Hermes kanban board, and closing issues when kanban cards complete.

## Overview

A `no_agent: true` cron job (pure bash script) runs every 15 minutes. It does two things:

1. **Ingestion**: Polls GitHub for open issues with the `ready-for-agent` label → creates kanban cards on the `my-project-dev` board assigned to `orchestrator`
2. **Resolution**: Scans the kanban board for completed cards whose titles match `[GH-<number>]` → closes the GitHub issue, removes the label, and posts a completion comment

## Script

**Location:** `~/.hermes/profiles/orchestrator/scripts/hermes_github_sync.sh`

**Cron job config:**

```yaml
# From `hermes cron list`:
schedule: "every 15m"
deliver: local        # output saved to disk, not delivered to user
no_agent: true        # script-only, no LLM cost
script: hermes_github_sync.sh
workdir: $HOME/my-project
```

## The Sync Script

```bash
#!/bin/bash
set -euo pipefail

REPO="my-org/my-project"
TRIGGER_LABEL="ready-for-agent"
BOARD_SLUG="my-project-dev"

# 1. INGESTION: Poll GitHub for labeled issues, create kanban cards
gh issue list --repo "$REPO" --label "$TRIGGER_LABEL" --state open \
  --json number,title,body | jq -c '.[]' | while read -r issue; do
    ISSUE_NUM=$(echo "$issue" | jq -r '.number')
    TITLE=$(echo "$issue" | jq -r '.title')
    BODY=$(echo "$issue" | jq -r '.body')
    TASK_SIG="[GH-$ISSUE_NUM]"

    # Dedup: skip if card already exists on the board
    if ! hermes kanban --board "$BOARD_SLUG" list 2>/dev/null | grep -Fq "$TASK_SIG"; then
        hermes kanban --board "$BOARD_SLUG" create "$TASK_SIG $TITLE" \
            --body "GitHub Issue #$ISSUE_NUM: $BODY" \
            --assignee orchestrator
    fi
done

# 2. RESOLUTION: Scan completed cards, close matching GitHub issues
done_tasks=$(hermes kanban --board "$BOARD_SLUG" list --status done 2>/dev/null \
  | grep -oP '\[GH-\d+\]' 2>/dev/null || true)

echo "$done_tasks" | sort -u | while read -r match; do
    [ -z "$match" ] && continue
    ISSUE_NUM=$(echo "$match" | grep -oP '\d+')
    gh issue close "$ISSUE_NUM" --repo "$REPO" 2>/dev/null || true
    gh issue edit "$ISSUE_NUM" --repo "$REPO" --remove-label "$TRIGGER_LABEL" 2>/dev/null || true
    gh issue comment "$ISSUE_NUM" --repo "$REPO" \
      --body '✅ **Automated Resolution:** This task was completed by the Hermes agent pool.' \
      2>/dev/null || true

    # Archive the completed cards on the Kanban board to prevent infinite closure loops and enable future reopens
    sqlite3 "$HOME/.hermes/kanban/boards/my-project-dev/kanban.db" \
      "UPDATE tasks SET status = 'archived' WHERE (title LIKE '%[GH-$ISSUE_NUM]%' OR title LIKE '%#$ISSUE_NUM%') AND status = 'done';" 2>/dev/null || true
done
```

## The Full Pipeline

```
1. Label an issue "ready-for-agent" on GitHub
         ↓
2. Cron job (every 15m) → creates kanban card on "my-project-dev" board
   assigned to orchestrator profile
         ↓
3. auto_decompose: true → orchestrator breaks it into subtasks
   (each coder card paired with a code-reviewer card via parents=[])
         ↓
4. Kanban dispatcher (in gateway) → spawns coder workers
   → coder completes → reviewer auto-promotes → spawns code-reviewer
         ↓
5. Cron job next tick → detects completed cards, closes GitHub issue
```

## Flow of Cards

```
GitHub Issue #467
  └→ Kanban card [GH-42] assigned to orchestrator
       └→ auto_decompose creates:
            ├→ Coder card T1 (assignee: coder)
            │    └→ Reviewer card R1 (parents=[T1], assignee: code-reviewer)
            ├→ Coder card T2 (assignee: coder)
            │    └→ Reviewer card R2 (parents=[T2], assignee: code-reviewer)
            └→ ...
```

## Dedup Mechanism

The script checks for existing cards by searching for the `[GH-<N>]` signature in the board's task list. This prevents duplicate cards on re-runs.

## Pitfalls

- **Label must be exact.** The script uses `--label "$TRIGGER_LABEL"` which is an exact match. A misspelled label won't be picked up.
- **Card assigned to orchestrator, not coder.** The script creates the card assigned to `orchestrator`. The `auto_decompose: true` setting in the orchestrator's kanban config then breaks it into subtasks. If you want to skip the orchestrator and go directly to a coder, change `--assignee orchestrator` to `--assignee coder`.
- **15-minute delay.** The cron job runs every 15 minutes. After labeling an issue, it may take up to 15 minutes for the card to appear on the board.
- **CLI is required.** The script uses `gh` CLI and `hermes kanban` CLI. Both must be installed and authenticated in the cron job's environment.
- **`no_agent: true` means no LLM.** The script runs purely as bash — no reasoning, no decomposition. The orchestrator handles decomposition when the card is dispatched.
- **Output goes to disk.** `deliver: local` means the script's stdout is saved to `~/.hermes/profiles/orchestrator/cron/output/<job_id>/` but not delivered to the user. Check there if the script seems to be doing nothing.