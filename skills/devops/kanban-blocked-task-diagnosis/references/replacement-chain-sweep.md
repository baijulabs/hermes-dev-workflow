# Replacement Chain Sweep — Diagnostics & Decision Tree

After creating replacement coder+reviewer pairs for ghost implementations or persistent-bug cycles, sweep the board for stale cards that should be archived. This file provides the diagnostic queries and the decision tree for each category.

## Step 1: Find all stale candidates

### Blocked tasks with replacement chains

```bash
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db "
SELECT id, title, assignee, last_failure_error
FROM tasks WHERE status = 'blocked';
"
```

Then check each blocked task's `created` event to see if it's a child of the old chain:
```bash
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db "
SELECT kind, payload FROM task_events
WHERE task_id='<id>' AND kind='created'
ORDER BY created_at LIMIT 1;
"
```

If created with `parents: [...]` pointing to a ghost coder (coder marked `done` but code absent), check for a replacement chain by searching for tasks created later with that blocked task as `--parent`:
```bash
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db "
SELECT id, title, assignee, status FROM tasks
WHERE id IN (
  SELECT child_id FROM task_links WHERE parent_id = '<blocked-id>'
);
"
```

If a replacement coder card exists, the blocked card is stale — archive it.

### Ready tasks with archived/cancelled parents

```bash
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db "
SELECT tl.child_id, t.title, t.assignee, p.status AS parent_status
FROM task_links tl
JOIN tasks t ON tl.child_id = t.id
JOIN tasks p ON tl.parent_id = p.id
WHERE t.status = 'ready'
  AND p.status IN ('archived', 'cancelled');
"
```

### Orphaned ready tasks (no parent links)

```bash
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db "
SELECT id, title, assignee FROM tasks
WHERE status = 'ready'
  AND id NOT IN (SELECT child_id FROM task_links);
"
```

## Step 2: Categorize each candidate

For each candidate, check its `created` event:
```bash
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db "
SELECT kind, payload FROM task_events
WHERE task_id='<id>' AND kind='created'
ORDER BY created_at LIMIT 1;
"
```

### Decision Table

| Created-event payload | Status | Action |
|---|---|---|
| `"from_decompose_of": "<archived-or-cancelled-id>"` | Blocked / Ready / Triage | Archive — replacement chain exists |
| `"from_decompose_of": "<done-id>"` and the done parent's reviewer confirmed PASS | Blocked | Mark `done` — stale block, code verified present |
| `"assignee": "coder", "status": "ready", "parents": []` | Ready | Keep — genuine standalone coder task |
| `"assignee": "code-reviewer", "status": "todo", "parents": ["<done-coder>"]` | Ready | Keep — genuine review task, parent coder done |
| `"by": "auto-decomposer", "from_decompose_of": "<archived-reviewer>"` | Ready | Archive — decomposed from a dead parent review |

## Step 3: Apply the sweep

```bash
# Archive stale cards
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db "
UPDATE tasks SET status = 'archived'
WHERE id IN ('<orphan-id-1>', '<orphan-id-2>', ...);
"

# Mark done for reviewer that passed after code was added
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db "
UPDATE tasks SET status = 'done', result = 'Code verified present, reviewer confirmed PASS'
WHERE id = '<id>';
"

# Verify
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db "
SELECT status, COUNT(*) FROM tasks GROUP BY status ORDER BY 2 DESC;
"
```

## Step 4: Validate remaining ready tasks

Confirm every remaining `ready` card is a genuine open issue by spot-checking its body and parent chain:
```bash
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db "
SELECT id, title, assignee, body FROM tasks WHERE status = 'ready';
"
```

For review cards, verify the parent coder is `done` (code was written):
```bash
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db "
SELECT tl.child_id, p.id, p.status, p.title
FROM task_links tl JOIN tasks p ON tl.parent_id = p.id
WHERE tl.child_id IN (SELECT id FROM tasks WHERE status = 'ready' AND assignee IN ('code-reviewer', 'orchestrator'));
"
```

If any ready reviewer's parent coder is also `archived` or `cancelled`, add it to the archive list.
