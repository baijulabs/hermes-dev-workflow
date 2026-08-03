# Worktree Branch Guardrails

Three-layer defense against Pattern 5b (`git worktree add failed` — branch already checked out by sibling worktree) and Pattern 5a (branch=main collision).

## Layer 1: Pre-Creation Check (Prevention)

**Script:** `~/.hermes/profiles/orchestrator/scripts/assert-branch-unique.sh`

Run BEFORE `kanban_create --branch <name>` to verify the branch name is unused:

```bash
~/.hermes/profiles/orchestrator/scripts/assert-branch-unique.sh "fix/gh-592-foo" "my-project-dev" "$HOME/my-project"
# exit 0 = unique, safe to create
# exit 1 = collision detected — omit --branch or use a different name
```

The script checks **two layers**:
1. **Git worktree list** — catches branches checked out by completed tasks' live worktrees on disk (the actual collision source). Even if the kanban task is `done`, the worktree branch persists on disk.
2. **Kanban DB** — catches branches queued by other active (non-archived, non-done) tasks.

**When to use:** Before every `kanban_create` with `--branch` and `workspace_kind=worktree`. If the script returns collision, either omit `--branch` (dispatcher auto-derives `wt/t_<task-id>`) or generate a unique name like `fix/<gh>-<descriptor>`.

## Layer 2: Skill Convention (Design)

**Location:** `kanban-orchestrator` SKILL.md — Automated Review-Failed Resolution section

The auto-resolution flow was patched to **never copy the original coder's `branch_name`** into a new fix card. Instead:
- Omit `--branch` entirely (dispatcher auto-derives `wt/t_<id>` — guaranteed unique)
- Or generate a fresh name: `fix/<issue_hook>-<short-descriptor>`

The card body's `BASE BRANCH:` should reference the original coder's worktree branch (the content base), NOT the fix card's own branch name. These serve different purposes:
- `BASE BRANCH:` in the body = tells the worker which branch to verify they branched from
- `--branch` on create = the new worktree branch being created

## Layer 3: Automated Safety Net (Recovery)

**Cron:** `worktree-collision-watch` — runs every 5 minutes as `no_agent` watchdog

**Script:** `~/.hermes/profiles/orchestrator/scripts/worktree-collision-watch.py`

A Python script that:
1. Polls for `blocked` coder cards with `last_failure_error` containing `"already used by worktree"`
2. Auto-assigns a unique branch name (`fix/<gh-part>-collision-<ts>`)
3. Resets to `todo` with `consecutive_failures=0`
4. Delivers Telegram notification only when remediation is needed (silent when clear)

**Delivery:** Telegram — only fires when it actually remediates a collision. Clean runs produce no output.

## Integration Points

| Layer | When to trigger | How |
|-------|----------------|-----|
| Pre-creation check | Before every `kanban_create --branch` | `assert-branch-unique.sh` |
| Skill convention | During auto-resolution of review-failed cards | Don't copy `branch_name` |
| Safety net | Every 5 minutes, automatically | cron `worktree-collision-watch` |