# CLI-Based Review-Failed Auto-Resolution

Use this when executing the automated review-failed resolution flow from a **cron job or terminal** (no Python API available). The `kanban-orchestrator` skill's Automated Review-Failed Resolution section shows `kanban_create()` / `kanban_comment()` / `kanban_archive()` Python API calls; this reference maps them to the equivalent `hermes kanban` CLI commands.

## When to use this

- You're in **cron mode** where `execute_code` is blocked
- You're running in a **terminal** and prefer CLI over Python scripts
- You need the **precise CLI flags** — `kanban_create` Python kwargs map to different CLI flag names

## CLI Command Reference

### Step 1: Find blocked reviewer cards

```bash
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db \
  "SELECT e.task_id, t.title, t.block_kind, json_extract(e.payload, '$.reason') as block_reason, e.created_at \
   FROM task_events e JOIN tasks t ON e.task_id = t.id \
   WHERE e.kind = 'blocked' AND t.status = 'blocked' AND t.assignee = 'code-reviewer' \
   ORDER BY e.created_at DESC;"
```

### Step 2: Read the reviewer's structured findings

The comment body is in `task_comments`, NOT in `task_events.payload`:

```bash
sqlite3 <board-dir>/kanban.db \
  "SELECT body FROM task_comments WHERE task_id='<reviewer-id>' ORDER BY created_at DESC LIMIT 1;"
```

### Step 3: Get the base branch from the original coder card

The reviewer card's parent is the coder task — find it and read its body for `BASE BRANCH`:

```bash
sqlite3 <board-dir>/kanban.db \
  "SELECT p.title, p.body, p.branch_name FROM task_links tl \
   JOIN tasks p ON tl.parent_id = p.id \
   WHERE tl.child_id='<reviewer-id>' AND tl.parent_id LIKE 't_%';"
```

### Step 4: Create the fix coder card (Python → CLI mapping)

| Python API call | CLI equivalent |
|---|---|
| `kanban_create(title=..., assignee="coder")` | `hermes kanban create "..." --assignee coder` |
| `body=...` | `--body "..."` (passed inline) |
| `workspace="worktree"` | `--workspace worktree` |
| `branch=...` (omit in review-failed to avoid collision) | **Omit `--branch`** — dispatcher auto-derives `wt/t_<task-id>` (or pass a unique name like `fix/<descriptor>`) |
| `--json` output | `--json` flag returns structured JSON with `id`, `status`, `assignee`, etc. — use `jq` instead of `grep` to extract IDs |

**⚠️ CRITICAL:** Do NOT pass `--branch <original-coder-branch-name>` — that branch is still checked out by the original coder's worktree. The dispatcher will try `git worktree add` and get `fatal: already used by worktree`. Omit `--branch` entirely so the dispatcher auto-derives a unique name.

Example:
```bash
hermes kanban create "[GH-123] Fix: Remove orphaned functions" \
  --assignee coder \
  --workspace worktree \
  --body "## Goal
...fix description...

BASE BRANCH: <original-coder-branch>
CRITICAL: Before writing code, run \`git branch --show-current\`..."
```

Capture the returned task id from the output: `Created t_<hex>  (ready, assignee=coder)`

**Robust extraction with `--json` + `jq`** (preferred over `grep` — the JSON output is version-stable):

```bash
# Create coder card with --json flag
JSON_OUTPUT=$(hermes kanban create "Fix: Add declarations" \
  --assignee coder \
  --workspace worktree \
  --body "..." \
  --json)

CODER_ID=$(echo "$JSON_OUTPUT" | jq -r '.id')
echo "Created coder: $CODER_ID (status=$(echo "$JSON_OUTPUT" | jq -r '.status'))"
```

The `--json` output returns: `id`, `title`, `body`, `assignee`, `status`, `workspace_kind`, `workspace_path`, `branch_name`, `created_by`, `created_at`, `skills`, `max_retries`, `model_override`, `provider_override` — parse with `jq` for reliability.

### Step 5: Create the paired reviewer card (Python → CLI mapping)

| Python API call | CLI equivalent |
|---|---|
| `kanban_create(parents=[coder_id])` | `--parent <coder-id>` |
| `kanban_create(assignee="code-reviewer")` | `--assignee code-reviewer` |

Example:
```bash
hermes kanban create "Review: [GH-123] Fix orphaned functions" \
  --assignee code-reviewer \
  --parent t_<coder-task-id> \
  --body "Review implementation of [GH-123] Fix...

Coder task: t_<coder-task-id>
Files changed: src/file.js
Verification: ./run-tests.sh passes"
```

The new reviewer card is created in `todo` status (auto-promotes to `ready` when the coder completes).

### Step 6: Comment on and archive the old blocked reviewer

**Comment syntax** (positional args, NOT `--body`):

```bash
hermes kanban comment <old-reviewer-id> "Superseded by new fix card <coder-id> (paired reviewer: <new-reviewer-id>)."
```

**Archive:**
```bash
hermes kanban archive <old-reviewer-id>
```

## Post-Creation Verification

After creating the new cards, verify the dispatcher picked them up and no more blocked reviewers remain:

```bash
# Verify coder was picked up by dispatcher
sqlite3 "$DB" "SELECT status, assignee FROM tasks WHERE id='$CODER_ID';"
# Expected: 'running' (dispatcher claimed it), 'todo' (queued), or 'ready' (waiting)

# Verify new reviewer is in correct state
sqlite3 "$DB" "SELECT status, assignee FROM tasks WHERE id='$NEW_REVIEWER_ID';"
# Expected: 'todo' (gated by parent coder)

# Verify no more blocked reviewers remain
sqlite3 "$DB" "SELECT COUNT(*) FROM tasks WHERE status='blocked' AND assignee='code-reviewer';"
# Expected: 0

# Verify old reviewer is archived
sqlite3 "$DB" "SELECT status FROM tasks WHERE id='$OLD_REVIEWER_ID';"
# Expected: 'archived'
```

## Full End-to-End Example

```bash
# --- CONFIG ---
BOARD=my-project-dev
DB="$HOME/.hermes/kanban/boards/$BOARD/kanban.db"

# 1. Find blocked reviewer
REVIEWER=$(sqlite3 "$DB" \
  "SELECT e.task_id FROM task_events e JOIN tasks t ON e.task_id=t.id \
   WHERE e.kind='blocked' AND t.status='blocked' AND t.assignee='code-reviewer' \
   AND json_extract(e.payload,'$.reason') LIKE 'review-failed:%' \
   ORDER BY e.created_at DESC LIMIT 1;")
echo "Reviewer: $REVIEWER"

# 2. Read findings and base branch
FINDINGS=$(sqlite3 "$DB" "SELECT body FROM task_comments WHERE task_id='$REVIEWER' ORDER BY created_at DESC LIMIT 1;")
CODER_PARENT=$(sqlite3 "$DB" "SELECT p.id FROM task_links tl JOIN tasks p ON tl.parent_id=p.id WHERE tl.child_id='$REVIEWER' AND tl.parent_id LIKE 't_%';")
BASE_BRANCH=$(sqlite3 "$DB" "SELECT substr(body, instr(body, 'BASE BRANCH:')+12, 80) FROM tasks WHERE id='$CODER_PARENT';" | head -1 | xargs)

# 3. Create fix coder card (omit --branch to avoid collision)
CODER_JSON=$(hermes kanban create "[PR #142] Fix: Remove orphaned functions" \
  --assignee coder \
  --workspace worktree \
  --json \
  --body "## Goal\\nRemove orphaned functions...\\n\\nBASE BRANCH: $BASE_BRANCH\\nCRITICAL:...")
CODER_ID=$(echo "$CODER_JSON" | jq -r '.id')
echo "New coder: $CODER_ID (status=$(echo "$CODER_JSON" | jq -r '.status'), branch=$(echo "$CODER_JSON" | jq -r '.branch_name // "auto"'))"

# 4. Create paired reviewer
REVIEWER_JSON=$(hermes kanban create "Review: [PR #142] Fix..." \
  --assignee code-reviewer \
  --parent "$CODER_ID" \
  --json \
  --body "Review implementation...\\nCoder task: $CODER_ID")
REVIEWER_ID=$(echo "$REVIEWER_JSON" | jq -r '.id')
echo "New reviewer: $REVIEWER_ID (status=$(echo "$REVIEWER_JSON" | jq -r '.status'))"

# 5. Comment and archive old reviewer
hermes kanban comment "$REVIEWER" "Superseded by $CODER_ID (paired: $REVIEWER_ID)."
hermes kanban archive "$REVIEWER"

# 6. Verify
echo "--- Verification ---"
sqlite3 "$DB" "SELECT status FROM tasks WHERE id='$CODER_ID';"
sqlite3 "$DB" "SELECT COUNT(*) FROM tasks WHERE status='blocked' AND assignee='code-reviewer';"
```

## Pitfalls

| Pitfall | Symptom | Fix |
|---------|---------|-----|
| `--workspace` not `--workspace_kind` | `hermes kanban create: error: unrecognized arguments` | Use `--workspace worktree` (CLI flag name differs from the Python API kwarg `workspace_kind`) |
| `kanban_comment --body` doesn't exist | `error: unrecognized arguments: --body` | Text is positional: `hermes kanban comment <id> "comment text"` |
| Reusing original coder's branch name | `fatal: already used by worktree` | Omit `--branch` — dispatcher auto-derives `wt/t_<task-id>` |
| `--parent` not `--parents` (CLI is singular) | `error: unrecognized arguments` | Use `--parent <id>` once per parent (repeatable for multiple parents) |
| Capturing task id from output | Different output format depending on status | Grep for `t_[a-f0-9]+` in the output line |
