# Kanban DB Schema Reference — Column Names & Query Gotchas

## Tasks Table — Actual Columns

```sql
id                   TEXT PRIMARY KEY      -- e.g. t_5ae45a33, t_review_552
title                TEXT NOT NULL
body                 TEXT                  -- Card body/markdown
assignee             TEXT                  -- Profile name: coder, code-reviewer, orchestrator
status               TEXT NOT NULL         -- todo | ready | running | done | blocked | archived | cancelled
priority             INTEGER DEFAULT 0
created_by           TEXT
created_at           INTEGER NOT NULL      -- Unix epoch seconds
started_at           INTEGER               -- Unix epoch seconds
completed_at         INTEGER               -- Unix epoch seconds
workspace_kind       TEXT NOT NULL DEFAULT 'scratch'  -- scratch | worktree
workspace_path       TEXT
branch_name          TEXT                  -- Set by --branch or auto-derived wt/t_<id>
project_id           TEXT                  -- Link to a first-class Project
claim_lock           TEXT                  -- Worker claim UUID
claim_expires        INTEGER               -- Claim TTL (Unix epoch)
tenant               TEXT
result               TEXT
idempotency_key      TEXT
consecutive_failures INTEGER NOT NULL DEFAULT 0
worker_pid           INTEGER
last_failure_error   TEXT                  -- Excerpt of most recent error text
block_kind           TEXT                  -- review-failed | needs_input | infrastructure | dependency | NULL
block_recurrences    INTEGER NOT NULL DEFAULT 0
current_run_id       INTEGER
max_retries          INTEGER               -- Per-task circuit-breaker override
```

## CRITICAL GOTCHAS

### 1. `block_kind` — NOT `block_reason`

There is **no** `block_reason` column. If you query `SELECT block_reason FROM tasks` you get:
```
Error: in prepare, no such column: block_reason
```

The column that exists is `block_kind` — it stores **typed** values:
- `review-failed` — reviewer blocked with findings; qualifies for auto-resolution
- `needs_input` — waiting for human input
- `infrastructure` — infrastructure/environment issue
- `dependency` — waiting on a parent task (not a true block; dispatcher handles this)
- NULL — legacy/untyped blocks

The free-text block reason (the message you see in `kanban_block(reason="...")`) is stored in **two places**:
- **`task_events` table**, column `payload` — JSON like `{"reason": "review-failed: ...", "kind": "review-failed", "recurrences": 0}`
- **`task_comments` table**, column `body` — may contain "review-failed handoff:" with structured JSON findings

### 2. `task_events` — NOT `events` or `kanban.db.events`

The events table is named `task_events`, with these columns:
```sql
id         INTEGER PRIMARY KEY AUTOINCREMENT
task_id    TEXT NOT NULL
run_id     INTEGER
kind       TEXT NOT NULL        -- created | promoted | blocked | heartbeat | completed | etc.
payload    TEXT                 -- JSON with event-specific data
created_at INTEGER NOT NULL
```

Do NOT query `kanban.db.events` — that table does not exist. The pitfall in kanban-orchestrator that says "check `kanban.db.events`" is wrong; use `task_events`.

The `kind` column (not `event_type` or `type`) stores event types. Common kinds:
- `created` — task created
- `promoted` — auto-promoted from todo to ready
- `blocked` — task blocked (payload has reason/kind/recurrences)
- `heartbeat` — worker heartbeat
- `completed` — task completed
- `archived` — task archived

### 3. `task_comments` — Structured Findings

Reviewer comments with "review-failed handoff:" contain structured JSON in the `body` column:

```json
{
  "findings": [
    {
      "severity": "critical",
      "file": "backend/api/routers/private_routes.py",
      "line": 2785,
      "issue": "Description of the problem",
      "fix": "Suggestion (if provided)"
    }
  ],
  "approved": false,
  "summary": "Review failed — N critical issues"
}
```

Query pattern:
```sql
SELECT body FROM task_comments
WHERE task_id='<task-id>' AND body LIKE '%review-failed%'
ORDER BY created_at DESC LIMIT 1;
```

## Common Query Patterns

### Find auto-resolution candidates
```sql
SELECT id, title, block_kind, block_recurrences FROM tasks
WHERE status='blocked' AND assignee='code-reviewer' AND block_kind='review-failed'
ORDER BY created_at DESC;
```

### Find all blocked tasks with error info
```sql
SELECT id, title, status, assignee, block_kind, consecutive_failures,
       substr(last_failure_error, 1, 100) as error
FROM tasks WHERE status='blocked' ORDER BY created_at DESC;
```

### Check board state summary
```sql
SELECT status, COUNT(*) FROM tasks GROUP BY status ORDER BY COUNT(*) DESC;
```

### Check if a card has review-failed in comments (even if not blocked)
```sql
SELECT t.id, t.title, t.status, c.body
FROM task_comments c JOIN tasks t ON c.task_id = t.id
WHERE c.body LIKE '%review-failed%' AND t.status != 'done'
ORDER BY c.created_at DESC;
```

### Get recent task events for debugging
```sql
SELECT kind, datetime(created_at, 'unixepoch') as ts, substr(task_id,1,20) as tid
FROM task_events ORDER BY id DESC LIMIT 25;
```

### Get the block payload from task_events
```sql
SELECT payload FROM task_events
WHERE task_id='<task-id>' AND kind='blocked'
ORDER BY created_at DESC LIMIT 1;
```