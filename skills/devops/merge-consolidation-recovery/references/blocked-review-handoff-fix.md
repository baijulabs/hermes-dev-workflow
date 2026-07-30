# Blocked Coder Card — Review-Required Handoff Fix

## Problem

A coder card is `blocked` with a comment saying "Implementation complete — PR" but the paired reviewer card never promotes from `todo` to `ready`. The pipeline deadlocks.

## Root cause

The coder called `kanban_block(reason="review-required: ...")` instead of `kanban_complete()`. The parent card is `blocked`, so the reviewer (linked via `parents=[coder_id]`) stays in `todo` — it only promotes when the parent reaches `done`.

## Fix

```bash
sqlite3 ~/.hermes/kanban/boards/my-project-dev/kanban.db \
  "UPDATE tasks SET status='done', block_kind=NULL WHERE id='<coder-task-id>';"
```

After the update, the dispatcher auto-promotes the reviewer from `todo` to `ready` on the next tick.

## Detection

```bash
# Find blocked coder cards
sqlite3 ~/.hermes/kanban/boards/my-project-dev/kanban.db \
  "SELECT t.id, t.title FROM tasks t \
   WHERE t.status='blocked' AND t.assignee='coder' AND t.block_kind IS NULL;"

# Check if a reviewer card exists for each blocked coder
sqlite3 ~/.hermes/kanban/boards/my-project-dev/kanban.db \
  "SELECT * FROM task_links tl \
   JOIN tasks tp ON tl.parent_id = tp.id \
   WHERE tp.status='blocked' AND tp.assignee='coder';"
```

## Prevention

### AGENTS.coder.md (enforcement layer)

Updated `AGENTS.coder.md` with explicit rule:

> **Always call `kanban_complete()` to hand off — never `kanban_block()`.** Blocking leaves the card stuck and prevents the reviewer gate from promoting. If the card body says `review-required`, still call `kanban_complete()` with the changed files and test results in the metadata. The reviewer card will auto-promote to `ready` when the coder completes.

### kanban-worker skill (advisory)

The `kanban-worker` skill already documents the rule but it's a user-owned skill that can't be curator-updated. The AGENTS.coder.md instruction is the enforcement layer — skills are advisory, AGENTS.md is loaded into every worker's context.

### Orchestrator monitoring

The orchestrator should periodically check for blocked coder cards with linked reviewer children — if found, the coder used the wrong handoff call. Fix with the SQL UPDATE above.