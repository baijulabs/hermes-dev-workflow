---
name: cicd-failure-automation
description: "Automated response to CI/CD deploy failures — polls GitHub Actions, auto-creates kanban fix cards, and auto-consolidates completed fixes into a PR."
version: 1.0.0
metadata:
  hermes:
    tags: [cicd, github-actions, deploy, automation, kanban]
    related_skills: [kanban-orchestrator, my-project-operations, github-pr-workflow]
---

# CI/CD Failure Automation

Automatically detect failed staging deploys (manual or PR-triggered), extract the failure output, create kanban fix cards, and consolidate completed fixes into a single PR.

## Architecture

Two cron jobs work together:

```
GitHub Actions deploy fails
        ↓ (every 10 min)
[staging-deploy-watch] — polls for new failed runs
        ↓ (failure detected)
Agent parses logs → creates kanban coder+reviewer card pairs
        ↓ (coders fix, reviewers approve)
[deploy-fix-pr-watch] — polls for all fix cards done
        ↓ (all done)
Agent consolidates worktrees → runs tests → opens PR
```

## Setup

### 1. Deploy Watch Script

Create `~/.hermes/profiles/<profile>/scripts/staging-deploy-watch.py`:

A Python script that:
- Polls `gh run list` for the deploy workflow (`deploy.yml`)
- Tracks two event types: `workflow_dispatch` (manual) and `pull_request_target` (PR checks)
- **Filters out PR merge events** — checks `gh pr list --head <branch> --state open` to skip closed/merged PRs
- Tracks last-seen run ID in a state JSON file
- Outputs failure logs via `gh run view --log-failed`

Key functions:
- `get_latest_failed_runs()` — fetches completed failed runs, filtered by event type and PR state
- `is_pr_still_open(branch)` — returns False for merged/closed PRs (avoids re-reporting old failures)
- `get_failed_jobs(run_id)` — extracts failed log output (last 200 lines)
- State tracking: saves `last_run_id` to state file so each failure is reported exactly once

### 2. Deploy Watch Cron Job

```bash
hermes cron create \
  --name "staging-deploy-watch" \
  --schedule "every 10m" \
  --script staging-deploy-watch.py \
  --skills "my-project-operations,github-pr-workflow" \
  --deliver local \
  --workdir /path/to/repo
```

The script injects failure output into the agent prompt. The agent then:
1. Reads the failure details (test names, errors, stack traces)
2. Creates kanban `coder` + `code-reviewer` card pairs for each distinct fix
3. Each card: `workspace_kind=worktree`, `assignee=coder`, descriptive branch name

### 3. PR Consolidation Cron Job

A second cron job watches for all fix cards to complete:

```bash
hermes cron create \
  --name "deploy-fix-pr-watch" \
  --schedule "every 10m" \
  --skills "github-pr-workflow,my-project-operations" \
  --deliver local \
  --workdir /path/to/repo
```

The agent:
1. Queries kanban DB for specific card IDs — only proceeds when ALL are `done`
2. Cherry-picks commits from all worktree branches onto a fresh branch off `main`
3. Runs `./scripts/sync-version.sh` to sync version files
4. Runs `./run-tests.sh` — blocks on failure, does NOT push
5. **Version bump:** Follow the `branch-consolidation` skill's Step 11 — scan conventional commit prefixes since `main`, auto-detect bump level (`patch`/`minor`/`major`), run `./scripts/sync-version.sh --bump <level>`, and commit the version bump
6. Pushes and creates PR via `gh pr create` — PR title includes the new version
7. Self-removes the cron job

### 4. Event Differentiation

The watch script captures two event types separately:

| `gh run list --event` | Trigger | PR state filter |
|---|---|---|
| `workflow_dispatch` | Manual staging deploy | None (always watch) |
| `pull_request_target` | PR synchronized/opened/reopened | Checks `gh pr list --head <branch> --state open` |

This prevents card creation for PR merge events (`pull_request_target closed`) where the CI was already green.

## State Management

- State file: `~/.hermes/profiles/<profile>/state/staging-deploy-watch.json`
- Tracks: `last_run_id` (most recently processed run) and `last_checked` (timestamp)
- Only reports each failure once — script exits silently if `last_run_id` hasn't changed

## Script Template

See `references/staging-deploy-watch.py` for the full implementation.

## Pitfalls

- **Rate limits:** `gh run list --limit 5` is lightweight enough for 10-minute intervals. Don't increase limit without understanding GH API rate limits.
- **Branch name collisions:** `gh pr list --head <branch>` requires exact branch name match. Works for `pull_request_target` since the head branch is always the feature branch.
- **PR merge edge case:** An admin-merging a PR with failing checks would produce a `pull_request_target closed` run with `conclusion=failure`. The `is_pr_still_open` check catches this — the PR is already closed, so the run is skipped.
- **Cron self-removal:** The PR consolidation cron should `cronjob(action='remove', job_id=...)` after creating the PR to avoid re-running on the same fix set.
- **CI artifact preservation:** When a CI step generates reports/artifacts that must survive assertion failures (e.g. Lighthouse reports, test screenshots, coverage data), split the workflow into separate **collection** and **assertion** steps. Collection runs with `continue-on-error: false` (must succeed), assertion runs with `continue-on-error: true` and a final `if:` gate that fails the job AFTER `actions/upload-artifact`. Otherwise, a failing assertion exits the step before the upload runs and the reports are lost. This applies to any tool with a combined run+assert command (`lhci autorun`, `vitest --fail`, etc.).