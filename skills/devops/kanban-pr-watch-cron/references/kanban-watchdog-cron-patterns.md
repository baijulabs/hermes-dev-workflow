# Kanban Watchdog Cron Patterns

Two `no_agent: true` cron jobs maintain the coder→reviewer→PR pipeline automatically.

## 1. coder-review-required-watch (every 5 min)

**Purpose:** Catches coders that block with `review-required:` instead of calling `kanban_complete()`. Auto-completes them so the paired reviewer card can promote.

**Script:** `~/.hermes/profiles/orchestrator/scripts/coder-review-required-watch.py`

**How it works:**
- Queries the kanban DB for `status='blocked' AND assignee='coder'`
- Joins with `task_events` where `kind='blocked'` and `json_extract(payload, '$.reason') LIKE 'review-required:%'`
- Extracts the summary from the block reason
- Calls `hermes kanban complete <task-id> --summary "..."` for each match
- The `last_failure_error` field is often empty for these — the reason lives in the events table, not the task row

**Why this is needed:** Coders consistently ignore the "don't block for review" instruction. The watchdog is a self-healing safety net, not a fix for the root cause.

## 2. pr-consolidation-watch (every 10 min)

**Purpose:** Finds done coder cards with done reviewer children (approved reviews) and creates PRs from their worktree branches.

**Script:** `~/.hermes/profiles/orchestrator/scripts/pr-consolidation-watch.py`

**How it works:**
- Queries the kanban DB for `status='done' AND assignee='coder'` joined with `task_links` to find done reviewer children
- Filters out cards older than 24 hours (`c.completed_at > strftime('%s', 'now', '-24 hours')`)
- Checks if a PR already exists for the branch across **all states (open, closed, merged)** via `gh pr list --state all --head <branch>` (CRITICAL: without `--state all`, closed/merged PRs are ignored, causing duplicate PR loops)
- Verifies the branch isn't already merged to main via `git merge-base --is-ancestor <branch-tip> origin/main`
- Automatically bumps the version prior to PR creation: scans commit messages (`feat:` -> `minor`, otherwise `patch`), runs `scripts/sync-version.sh --bump <level>` inside the worktree, and commits `chore: bump version to X.Y.Z`
- Pushes the branch to origin: `git push origin <branch>`
- Creates a PR: `gh pr create --base main --head <branch> --title "fix: ..." --body "..."`

**Key insight:** Coders commit to local worktree branches (e.g., `wt/t_<task-id>`) but rarely push them. The branch exists locally but not on origin, so no PR can be created. The script handles both the push and the PR creation.

**Silent when nothing to do:** If no done coder+reviewer pairs have unmerged commits, the script exits without output. Telegram notification only on PR creation.

## Setup

Both cron jobs are registered via:

```bash
cronjob action=create \
  schedule="every 5m" \
  name="coder-review-required-watch" \
  script="coder-review-required-watch.py" \
  deliver="telegram" \
  no_agent=true

cronjob action=create \
  schedule="every 10m" \
  name="pr-consolidation-watch" \
  script="pr-consolidation-watch.py" \
  deliver="telegram" \
  no_agent=true
```

## Verification

Check that both cron jobs are running:

```bash
cronjob action=list
```

Expected output shows both jobs with `state: "scheduled"` and `enabled: true`.