# hermes_github_sync.sh Guard Layers

The `hermes_github_sync.sh` cron script (every 15m, `no_agent: true`) scans `done` kanban cards for `[GH-N]` patterns and closes matching GitHub issues. Without proper guards, it silently closes issues whose work is still in flight and even closes open PRs (GitHub treats PRs as issues).

## Guard 1: Skip PRs

Check if the issue number is actually a pull request. If so, skip — never auto-close PRs.

```bash
if gh pr view "$ISSUE_NUM" --repo "$REPO" --json id &>/dev/null; then
    echo "Skipping #$ISSUE_NUM — it's a pull request, not an issue."
    continue
fi
```

## Guard 2: Skip Epic-Labeled Issues

Epics are containers for sub-tasks, not atomic work items. Only close when ALL child issues are done.

```bash
ISSUE_LABELS=$(gh issue view "$ISSUE_NUM" --repo "$REPO" --json labels --jq '[.labels[].name] | join(",")')
if echo "$ISSUE_LABELS" | grep -q "epic"; then
    echo "Skipping #$ISSUE_NUM — labeled 'epic'."
    continue
fi
```

## Guard 3: Skip Issues with Open GitHub Child Issues

Check if the issue has open GitHub sub-issues referencing it as parent. Uses a heuristic: search for open issues whose title contains "Parent epic: #N".

```bash
OPEN_CHILDREN=$(gh issue list --repo "$REPO" --state open --json number,title | \
    jq -r --arg parent "$ISSUE_NUM" '.[] | select(.title | test("Parent epic: #" + $parent)) | "#\(.number)")
if [ -n "$OPEN_CHILDREN" ]; then
    echo "Skipping #$ISSUE_NUM — has open child sub-tasks:"
    continue
fi
```

## Guard 4: Skip if Orchestrator Epic has In-Flight Coder Children (Kanban)

**This is the most critical guard.** When an orchestrator epic card reaches `done`, it means decomposition finished — NOT that implementation is complete. The orchestrator decomposes into coder+reviewer children; if those children are still in progress, the issue must NOT be closed.

This guard queries the kanban DB to check if any `done` orchestrator card matching `[GH-N]` has coder children that are still `todo`/`ready`/`running`/`blocked` (i.e., NOT `done`/`archived`/`cancelled`).

```bash
KANBAN_DB="/home/user/.hermes/kanban/boards/my-project-dev/kanban.db"
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
      continue 2  # skip to next ISSUE_NUM entirely
    fi
  done
fi
```

**Why `continue 2`:** The outer loop iterates over issue numbers. `continue` only skips the inner `for` loop over parent IDs. `continue 2` breaks out to the outer `while read ISSUE_NUM` loop, skipping the close for this issue entirely.

## Recovery from Premature Close

If an issue was closed prematurely (Guard 4 missing at the time):

```bash
# 1. Reopen the issue
gh issue reopen $ISSUE_NUM --repo my-org/MyProject

# 2. If the kanban cards were archived by the sync script, check if they need recovery
sqlite3 $KANBAN_DB \
  "SELECT id, status, assignee FROM tasks WHERE title LIKE '%[GH-$ISSUE_NUM]%';"

# 3. Find stranded worktree branches that never got PR'd
# (see pr-consolidation-watch.py for automated recovery)
```
