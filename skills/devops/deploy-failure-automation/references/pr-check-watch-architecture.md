# pr-check-watch — PR CI failure and Merge Conflict monitoring cron

## Purpose

Separate cron job that monitors open PRs on the repo for failing CI checks and merge conflicts. Unlike `staging-deploy-watch` (which watches workflow runs triggered by pushes), `pr-check-watch` polls the open PR list, checks each one's `statusCheckRollup` for failures, and checks `mergeable`/`mergeStateStatus` for merge conflicts.

## Architecture

```
Every 15 minutes:
  → gh pr list --state open --json number,headRefName,mergeable,mergeStateStatus
  → For each PR (ignoring Dependabot):
      → IF mergeable == "CONFLICTING" or mergeStateStatus == "DIRTY" (Merge Conflict):
          → Check Kanban DB for active card containing "Resolve merge conflicts in <branch>"
          → If no active card exists: Create coder + reviewer card pair to resolve conflicts:
              → Coder card: fetches PR branch, merges main, resolves conflict markers, runs tests, and pushes back to origin
              → Reviewer card: reviews the conflict resolution
      → IF statusCheckRollup contains failures (Failing CI Check):
          → Fetch failure details via gh run view --log-failed
          → Check Kanban DB for active card on this branch
          → If no active card exists: Create coder + reviewer card pair to fix CI checks:
              → Coder card: fetches PR branch, fixes tests/compilation, runs tests locally, and pushes back to origin
              → Reviewer card: reviews the CI fix
  → If no conflicts/failures: exit silently
```

## Dedup Logic

The most common failure mode is creating duplicate fix cards for the same persistent CI failure. Every poll cycle detects the same failed check and creates new cards unless dedup is applied:

1. Check if the PR branch name or title matches an existing open PR with a fix
2. Check kanban board for recent cards (last N hours) with similar titles
3. Only create cards for genuinely new failures

```bash
# Check for existing open PRs on the same branch
gh pr list --state open --head <branch> --json number

# Check for recent kanban cards with similar keywords
sqlite3 $KANBAN_DB "SELECT id, status FROM tasks WHERE title LIKE '%<keyword>%' AND status != 'cancelled' AND created_at > $(date +%s -d '1 hour ago')"
```

## Branch Naming

Auto-generated fix cards must use unique branch names. A date-only prefix like `fix/pr-fail-20260721` will collide with previous runs. Use:

```
fix/pr-fail-<unix-timestamp>-<random-4-char-suffix>
```

The random suffix prevents collisions when the same failure is detected in multiple poll cycles before the first batch of cards completes.

## Relationship to staging-deploy-watch

| Aspect | staging-deploy-watch | pr-check-watch |
|--------|---------------------|----------------|
| Watches | Workflow runs (deploy.yml) | Open PRs with failing CI or merge conflicts |
| Trigger | New push / workflow_dispatch | Any failed check on any open PR |
| Scope | Manual deploys + PR pushes | All open PRs |
| Card creation | Yes | Yes (with dedup) |
| PR consolidation | Yes (registers pr-consolidate) | No (the PR branch already exists — fix it) |