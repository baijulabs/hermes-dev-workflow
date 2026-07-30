# Deploy Monitor — Dedup, Worktree Naming & PR Consolidation

Prevention patterns for the deploy→fix→PR pipeline. These keep the
auto-monitoring system from creating duplicate cards, colliding worktrees,
or silently failing to consolidate.

## Worktree Branch Naming — Avoid Collision Across Concurrent Card Sets

When creating kanban cards with `workspace_kind=worktree`, every card
needs a TRULY unique branch name. Date-based names like
`fix/deploy-fail-20260721` collide if two cron jobs create cards for the
same deploy failure in the same minute.

**Bad (collides):**
```
--branch fix/deploy-fail-20260721
```

**Good (unique):**
```
--branch "fix/deploy-fail-$(date +%s)-$(openssl rand -hex 2)"
```

This is especially important when multiple cron jobs
(`staging-deploy-watch`, `pr-check-watch`) run concurrently and may
detect the same workflow failure.

## PR Dedup — Check for Existing Open PR Before Spawning Fix Cards

When a cron job detects CI failures and auto-creates fix cards, it must
check whether a fix is already in progress. The dead-simple check: look
for an existing open PR on the same branch as the failed run.

**Script-level check (no LLM cost):**

```python
import subprocess, json

result = subprocess.run(
    ["gh", "pr", "list", "--head", branch, "--state", "open",
     "--json", "number,url", "--limit", "1"],
    capture_output=True, text=True, timeout=15,
)
if result.returncode == 0 and result.stdout.strip():
    prs = json.loads(result.stdout)
    if prs:
        pr = prs[0]
        # Re-trigger CI on the existing PR
        subprocess.run(
            ["gh", "workflow", "run", "deploy.yml",
             "--ref", branch],
            capture_output=True, timeout=30,
        )
        print(f"Re-triggered CI on PR #{pr['number']}: {pr['url']}")
        return  # Don't output failure details — no new cards needed
```

This handles the common case where:
- A PR branch pushes new commits → CI fails → the cron detects the failure
- An open PR already exists for that branch (the fix is in progress)
- The cron should just re-trigger CI, not create duplicate cards for issues
  already being addressed

Only if no open PR exists should the agent be invoked to create new fix
cards.

## `no_agent: True` Consolidation Scripts (Preferred Over Agent Cron)

When the orchestrator needs to consolidate multiple worktree branches
into a single PR after all sub-tasks complete, use a `no_agent: True`
Python script instead of an LLM-driven cron job. Agent-based cron jobs
have several failure modes:

| Failure | Agent cron | no_agent script |
|---------|-----------|-----------------|
| Spin-up time | 30+ seconds (load skills, model, provider) | <1 second |
| Schedule drift | Agent takes 30 min → next tick pushed 30 min | Script runs in ms → no drift |
| Silent failure | `last_status: ok` with no output | Every `print()` is delivered |
| Determinism | LLM may hallucinate commands | Deterministic Python |

**Design the script:**

1. Poll the kanban DB for card status (fast SQLite query).
2. If not all cards done, exit silently (empty stdout = no delivery).
3. If all done, run the git merges, run tests, create the PR.
4. If any step fails, `print("ERROR: ...")` — it gets delivered as a notification.

**Script location:** `~/.hermes/profiles/<profile>/scripts/<name>.py`
**Cron job reference:** `script: '<name>.py'` with `no_agent: true`

## Cron Delivery Configuration

`no_agent: True` cron jobs deliver their stdout as notifications. The
`deliver` field determines WHERE the output goes:

| deliver | Behavior |
|---------|----------|
| `local` | Log only — saved to job log, NO notification sent |
| `telegram` | Sent to configured Telegram chat |
| `discord` | Sent to configured Discord channel |
| `all` | Sent to every connected platform |

**The `local` default is the most common cause of "silent cron."** Always
set `deliver` to a real platform when the job should notify the user.
If the platform is not configured, the delivery fails silently — check
`last_delivery_error` in `cronjob(action='list')` for the error message.

**Telegram-specific requirements:**
```yaml
# Required in config.yaml for delivery to work
telegram:
  enabled: true              # default: false
  allowed_chats: '1458851085'  # default: '' (no one)
  extra:
    rich_messages: true
```

## Worktree Branch Lifecycle — Push Requirements

Worktree branches created by kanban coder tasks are LOCAL by default.
The coder commits to the worktree branch but does NOT push to origin.
This means:

- `git merge origin/<branch>` fails — the branch doesn't exist on remote
- `git merge <branch>` works — the local branch ref exists
- Cherry-pick from the local branch works if the ref is reachable

**For the consolidation script, attempt both strategies:**

```python
# Try fetching from origin first
ok, out = run_cmd(["git", "fetch", "origin", local_branch])
if not ok:
    # Fall back to local branch
    ok, out = run_cmd(["git", "log", "--format=%H", local_branch, "^main"])
    commits = out.strip().split("\n") if ok and out.strip() else []
    for sha in reversed(commits):
        run_cmd(["git", "cherry-pick", sha, "--allow-empty"])
```

**Worktree branch `^main` syntax:** When passing branch exclusion to
git, `^main` MUST be a separate argument, not part of the same string:

```python
# WRONG — git treats this as a single ref name
run_cmd(["git", "log", "--format=%H", f"{branch} ^main"])

# RIGHT — git sees two separate refs
run_cmd(["git", "log", "--format=%H", branch, "^main"])
```

## Script Output Flow

```
Script runs (no_agent, <1s)
  ├─ No new failures → exit silently (empty stdout, no delivery)
  ├─ New failure + open PR exists → print "re-triggered CI on PR #X"
  │   → delivered to Telegram
  └─ New failure + NO open PR → print failure details
      → agent spawns, creates fix cards, registers consolidation cron
```

## Real-World Example (Jul 21, 2026)

The `staging-deploy-watch` cron ran at 15:57 and detected a failed
`workflow_dispatch` run. The `pr-check-watch` cron then created duplicate
fix cards for the same issues already addressed by PR #537. Two problems:

1. **Branch name collision:** `fix/deploy-fail-20260721` was used by both
   the manual PR branch and the auto-created fix cards. The dispatcher
   failed with `"branch is already used by worktree"`.

2. **No dedup:** The cron didn't check if PR #537 already existed on the
   same branch. It created cards for issues the PR already fixes.

3. **Silent failure:** The cron job had `deliver: local` and Telegram
   platform wasn't enabled (`enabled: false`, `allowed_chats: ''`).
   No notification was sent.

**Fixes applied:**
- Added `has_open_pr_for_branch()` check to the polling script
- Switched to unique branch names (`<timestamp>-<random-4-char>`)
- Changed `deliver` from `local` to `telegram`
- Enabled Telegram in config.yaml