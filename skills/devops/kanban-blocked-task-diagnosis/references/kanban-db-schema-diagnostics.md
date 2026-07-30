# Kanban DB Schema — Blocked Card Diagnostics

When diagnosing blocked cards via raw SQL, the `tasks` table schema has specific columns for blocked state — not all of which are intuitive from the column names alone.

## ⚠️ Common Query Pitfall: `task_events.id` is NOT `task_id`

The `task_events` table has an integer `id` column (the event's own rowid) and a separate `task_id` column (the foreign key referencing `tasks.id`). These are **different values**:

```sql
-- WRONG: this returns event rowids (e.g. 14079, 14783), NOT task IDs
SELECT id FROM task_events WHERE kind = 'blocked' LIMIT 5;
-- Returns: 14079, 14783, ... (integers)

-- WRONG: treating those integers as task IDs returns nothing
SELECT * FROM tasks WHERE id = '14079';
-- Returns: (empty — no task has id '14079')

-- CORRECT: always select task_id explicitly
SELECT task_id FROM task_events WHERE kind = 'blocked' LIMIT 5;
-- Returns: t_cb9app7f, t_3c398901, ... (text task IDs)

-- CORRECT: use task_id for joins/lookups
SELECT t.id, t.title, t.status
FROM tasks t
JOIN task_events e ON e.task_id = t.id
WHERE e.kind = 'blocked' AND e.payload LIKE '%review-failed%';
```

**Memory trick:** In `task_events`, `id` is the event number, `task_id` is the task identifier. Always select `task_id` when you want to reference a task. The same trap applies to `task_comments` and `task_links` — their `id` column is their own rowid, with `task_id`/`child_id`/`parent_id` being the foreign keys.

## tasks table columns relevant to blocked cards

| Column | Type | Purpose | Example |
|--------|------|---------|---------|
| `status` | TEXT | Current lifecycle state | `blocked`, `todo`, `ready`, `running`, `done`, `archived`, `cancelled` |
| `assignee` | TEXT | Worker profile assigned | `coder`, `code-reviewer` |
| `block_kind` | TEXT | **Typed** block reason — one of VALID_BLOCK_KINDS in the codebase (e.g. `needs_input`, `dependency`, `stale`). **`review-failed` is NOT a valid block_kind** — when the reviewer calls `kanban_block(reason="review-failed: ...")`, the block_kind is stored as `needs_input`, not `review-failed`. | `needs_input`, `dependency`, `stale` |
| `block_recurrences` | INTEGER | How many times the card has been re-blocked for the same `block_kind` after being unblocked. Resets to 0 only on completion. | `0`, `1`, `2` |
| `consecutive_failures` | INTEGER | Worker crash/failure count. | `0`, `1`, `2` |
| `last_failure_error` | TEXT | Short excerpt of the most recent failure's error text. | `worker exited without calling kanban_complete` |
| `claim_lock` | TEXT | Worker's session ID when claimed. NULL = not claimed. | `sess_abc123` |
| `claim_expires` | INTEGER | Unix timestamp when claim expires. | `1740000000` |
| `worker_pid` | INTEGER | PID of the dispatched worker process. | `12345` |
| `branch_name` | TEXT | Git branch the worktree was created from. | `fix/df-1784774204-save-values-v2` |

## Finding the block reason text

The `block_kind` column stores the **kind** of block (e.g. `needs_input`), not the actual reason text. The full reason lives in two places:

### Via task_events (the `kanban_block` reason)

When a worker calls `kanban_block(reason="review-failed: ...")`, the reason text is stored in the `task_events` table with `kind='blocked'` and the reason in the `payload` JSON field:

```sql
-- Get the block reason for all blocked cards
SELECT e.task_id, t.title, t.block_kind,
       json_extract(e.payload, '$.reason') as block_reason,
       e.created_at
FROM task_events e
JOIN tasks t ON e.task_id = t.id
WHERE e.kind = 'blocked'
  AND t.status = 'blocked'
ORDER BY e.created_at DESC;

-- Get block reason for a specific task
SELECT kind, created_at,
       json_extract(payload, '$.reason') as reason
FROM task_events
WHERE task_id = '<task-id>' AND kind = 'blocked'
ORDER BY created_at DESC LIMIT 1;
```

**Important:** There is NO `block_reason` column in the `tasks` table. Do not query for it. Always use `task_events` or `task_comments` to find the reason text.

### Via task_comments (structured findings)

The reviewer's structured findings (files examined, what passed, what failed, severity) are in the comments table. Since `review-failed` is NOT a valid `block_kind`, filter by the comment prefix instead:

```sql
-- Find the block reason text for blocked code-reviewer cards
SELECT t.id, t.title, t.block_kind, c.body, c.created_at
FROM tasks t
JOIN task_comments c ON c.task_id = t.id
WHERE t.status = 'blocked'
  AND t.assignee = 'code-reviewer'
  AND c.body LIKE 'review-failed handoff:%'
ORDER BY c.created_at DESC;
```

The most recent comment on a blocked card is typically the block reason. The reviewer's structured findings are in the comment body as JSON.

## Typical query for review-failed cards

```sql
-- Find all review-failed cards with their block reason
SELECT t.id,
       substr(t.title, 1, 60) as title,
       t.block_kind,
       t.block_recurrences,
       c.body
FROM tasks t
JOIN task_comments c ON c.task_id = t.id
WHERE t.status = 'blocked'
  AND t.assignee = 'code-reviewer'
  AND c.body LIKE 'review-failed handoff:%'
  AND c.id = (
    -- Get the most recent comment per task
    SELECT MAX(c2.id) FROM task_comments c2 WHERE c2.task_id = t.id
  )
ORDER BY t.created_at DESC;
```

**Important note on the subquery above:** `c.id` in the subquery is the comment's own rowid (integer), which is fine here because we're comparing within the same table. The `task_id` is used for the join to tasks. This is the correct pattern — `id` is safe to use within a single table, just not as a cross-table reference.

## Block kinds and their meanings

| `block_kind` | Meaning | Auto-resolvable? |
|---|---|---|
| `needs_input` | Waiting for human input/decision **or** reviewer blocked via `kanban_block(reason="review-failed: ...")`. The `review-failed` prefix lives in the task_events JSON or comment body, NOT in block_kind. | Depends — check event/comment for `review-failed:` prefix |
| `dependency` | Waiting on a parent task (non-blocking, task goes to `todo` not `blocked`) | N/A — handled by dependency engine |
| `stale` | Card hasn't been touched in too long | No — human review |
| `recurrence` | Blocked 3+ times for the same reason | No — escalate to human |
| NULL | Legacy/untyped block | Depends on context |

## Detecting `review-failed` cards via events (not block_kind)

Since `review-failed` is not stored as a `block_kind`, detect it via the `task_events` table instead:

```sql
-- Find blocked reviewer cards whose block reason starts with review-failed:
SELECT t.id, t.title, e.payload
FROM tasks t
JOIN task_events e ON e.task_id = t.id AND e.kind = 'blocked'
WHERE t.status = 'blocked'
  AND t.assignee = 'code-reviewer'
  AND json_extract(e.payload, '$.reason') LIKE 'review-failed:%'
ORDER BY e.created_at DESC;
```

**Comment-based detection** (the structured handoff comment also contains the `review-failed handoff:` marker):

```sql
SELECT t.id, c.body
FROM tasks t
JOIN task_comments c ON c.task_id = t.id
WHERE t.status = 'blocked'
  AND t.assignee = 'code-reviewer'
  AND c.body LIKE 'review-failed handoff:%'
ORDER BY c.created_at DESC;
```

## Quick status summary

```sql
-- Summary of all blocked cards by assignee and block_kind
SELECT assignee, block_kind, COUNT(*) as count
FROM tasks
WHERE status = 'blocked'
GROUP BY assignee, block_kind
ORDER BY assignee, block_kind;

-- Blocked cards that need auto-resolution (review-failed — detect via comments, not block_kind)
SELECT t.id, substr(t.title, 1, 60) as title, t.block_recurrences
FROM tasks t
JOIN task_comments c ON c.task_id = t.id
WHERE t.status = 'blocked'
  AND t.assignee = 'code-reviewer'
  AND c.body LIKE 'review-failed handoff:%'
ORDER BY t.created_at DESC;
```

## Checking for the 3+ recurrence loop

Before auto-resolving, check `block_recurrences`:

```sql
SELECT id, title, block_kind, block_recurrences
FROM tasks
WHERE status = 'blocked'
  AND assignee = 'code-reviewer'
  AND block_recurrences >= 3
  AND id IN (
    SELECT DISTINCT c.task_id
    FROM task_comments c
    WHERE c.body LIKE 'review-failed handoff:%'
  );
```

If this returns results, those cards should be **escalated to human** — the fix loop has cycled 3+ times without progress.

## Previous event status query (broken at "not blocked" but old events exist)

When querying the `task_events` table for blocked events, the task may no longer be blocked — it may have been archived, completed, or cancelled after the event was recorded. Always join with the `tasks` table and filter by `t.status = 'blocked'` to get only current blocked cards:

```sql
-- Only current blocked cards
SELECT e.task_id, e.kind, e.payload, e.created_at
FROM task_events e
JOIN tasks t ON e.task_id = t.id
WHERE e.kind = 'blocked'
  AND t.status = 'blocked'
ORDER BY e.created_at DESC;

-- ALL historical blocked events (including archived/completed)
SELECT e.task_id, e.kind, e.payload, e.created_at
FROM task_events e
WHERE e.kind = 'blocked'
ORDER BY e.created_at DESC;
```

The second query is useful for audit/history but will return many more rows, and the task IDs may reference tasks that no longer exist in the `tasks` table if they were purged during recovery. When this happens, the `id` values from the second query (which are event rowids) may look like missing task IDs — they're not; they're event numbers that happen to be integers.