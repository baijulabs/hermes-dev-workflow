# Board Phantom Card Diagnosis

> Technique for finding cards visible in the dashboard that shouldn't be there — "phantom cards" caused by status/column mapping mismatches or stale state.

## The Dashboard Query

The Hermes kanban dashboard plugin queries:

```python
# plugin_api.py line 277
rows = conn.execute(
    "SELECT * FROM tasks WHERE status != 'archived'",
).fetchall()
```

This returns **every non-archived card** — including `done`, `cancelled`, `running`, `todo`, `blocked`, etc. The dashboard then maps these into board columns (todo, ready, running, blocked, review, done) based on the board's column configuration.

## Symptom

User says "the dashboard shows more todo cards than the DB" or "I see phantom cards." The agent queries the DB directly (filtering by specific statuses) and gets a different count.

## Diagnosis Technique

### Step 1 — Query the dashboard API to see actual column state

The dashboard API is at `http://127.0.0.1:9119/api/plugins/kanban/board?board=<slug>`.
Auth via `Authorization: Bearer <token>` where token = `$HERMES_DASHBOARD_SESSION_TOKEN`.

```python
import urllib.request, json

req = urllib.request.Request(
    "http://127.0.0.1:9119/api/plugins/kanban/board?board=my-project-dev"
)
req.add_header("Authorization", f"Bearer {token}")
resp = urllib.request.urlopen(req, timeout=5)
data = json.loads(resp.read().decode())

for col in data.get('columns', []):
    name = col.get('name', '?')
    tasks = col.get('tasks', [])
    statuses = {}
    for t in tasks:
        s = t.get('status','?')
        statuses[s] = statuses.get(s, 0) + 1
    status_str = ', '.join(f'{s}={c}' for s,c in sorted(statuses.items()))
    print(f"[{name:12s}] {len(tasks):3d} tasks ({status_str})")
    if tasks and name in ('todo', 'blocked', 'running', 'ready'):
        for t in tasks:
            print(f"  {t.get('id','?'):15s} | {t.get('status','?'):12s} | {t.get('title','')[:70]}")
```

### Step 2 — Compare with direct DB query

```bash
sqlite3 ~/.hermes/kanban/boards/<slug>/kanban.db \
  "SELECT status, assignee, id, substr(title,1,85) FROM tasks \
   WHERE status NOT IN ('done','archived') ORDER BY status;"
```

### Step 3 — Spot the discrepancy

Look for cards whose DB `status` doesn't match the column they appear in:

| DB status | Dashboard column | Problem |
|-----------|-----------------|---------|
| `cancelled` | `todo` | Phantom — should be archived |
| `done` | `blocked` | Phantom — wrong column |
| `blocked` | `todo` | Mapping issue |
| `running` | `done` | Stale — worker died |

### Step 4 — Fix

**Cancelled cards in todo:** they were cancelled but never archived:
```bash
hermes kanban archive <task_id>
```

**Bulk:** `sqlite3 kanban.db "SELECT id FROM tasks WHERE status='cancelled'" | while read id; do hermes kanban archive $id; done`

## Common Patterns

### Pattern 1: Cancelled re-spec cards in todo
Old re-spec/review cards from a previous decomposition cycle were `cancelled` but never `archived`. The dashboard pulls them because `status != 'archived'`, and the column mapping puts `cancelled` into `todo`.

### Pattern 2: Stale running tasks
Worker crashed mid-flight. Task stays `running` in DB but column shows `running` or `blocked`. Fix: reset to `todo`/`ready` and let dispatcher re-claim.

### Pattern 3: Dashboard cache vs direct SQL writes
After direct SQLite changes, dashboard may show old state — this is a cache issue, not phantom cards. The dashboard self-heals on next API call. If not, restart the dashboard/gateway.

## Prevention

- After cancelling a card, **always archive it**
- Verify against BOTH dashboard API AND direct SQL before reporting counts
- Dashboard API = user's view; direct SQL = actual state