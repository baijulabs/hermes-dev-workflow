# Re-dispatched Coder Ignores Review Findings — t_2fc58406 / t_64de0f00

## Summary

The original coder `t_2fc58406` was created to fix 2 review findings in ExperimentDetail.vue (missing `v-if` on promote button visibility, missing `isPromoting` loading state). The card was blocked with `Error: Unknown skill(s): project-testing` (skill existed in orchestrator profile but not in coder profile). After the skill was copied to coder, the card was unblocked and re-dispatched.

The re-dispatched coder ran successfully, wrote new code, and completed. But the new code still had the same 2 bugs. The reviewer flagged the identical issues a second time.

## Timeline

1. Original coder `t_2fc58406` created to fix promote-to-sop bugs
2. Coder blocked: `Error: Unknown skill(s): project-testing`
3. `project-testing` skill copied from orchestrator to coder profile
4. Coder unblocked and re-dispatched
5. Coder ran, wrote code, called `kanban_complete` — marked `done`
6. Reviewer `t_64de0f00` dispatched, found the same 2 bugs still present
7. Reviewer findings: missing `v-if`, missing `isPromoting` ref/disabled binding
8. Reviewer archived, replacement pair created with exact inline code

## Root Cause

The coder profile's AGENTS.md says "Orient — read the card body." The coder reads the original card body (which says "fix the remaining issues") but does NOT read the reviewer's comment thread. The reviewer's specific line-level findings exist only in the comment thread, not in the card body.

The re-dispatched coder re-implements from scratch using only the card body spec and produces code with the same defects. The card body said "fix the remaining issues" but didn't include the reviewer's exact fix instructions.

## Diagnosis Commands

```bash
# Verify the coder was re-dispatched
sqlite3 ~/.hermes/kanban/boards/my-project-dev/kanban.db \
  "SELECT created_at, kind FROM task_events WHERE task_id='t_2fc58406' AND kind IN ('promoted', 'claimed', 'spawned', 'completed') ORDER BY created_at;"

# Check re-dispatched coder produced real code
cd /path/to/repo
git log origin/main..wt/t_2fc58406 --oneline | head -3
git diff origin/main...wt/t_2fc58406 --stat -- frontend/src/components/step6/ExperimentDetail.vue

# Read the reviewer's findings
sqlite3 ~/.hermes/kanban/boards/my-project-dev/kanban.db \
  "SELECT substr(body, 1, 500) FROM task_comments WHERE task_id='t_64de0f00' ORDER BY created_at DESC LIMIT 1;"
```

## Fix

1. Archive the looping reviewer
2. Create a new coder card with the exact old-to-new code changes inline in the body
3. Create a paired reviewer card

The card body must be self-contained — the coder should be able to implement every line from the card body alone, without reading any linked review thread.