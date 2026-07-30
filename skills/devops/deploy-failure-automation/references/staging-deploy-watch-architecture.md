# staging-deploy-watch.py — Deploy failure polling script

Location on disk: `~/.hermes/profiles/orchestrator/scripts/staging-deploy-watch.py`
Full content also available in the kanban-created cron job's record.

## Core logic

```
Poll GitHub Actions for completed workflow_dispatch + pull_request_target runs
  → Filter to failure/cancelled conclusions
  → For pull_request_target, skip if PR is merged/closed
  → Compare against last_run_id in state file
  → If new failure found: output structured failure details to stdout
  → Update state file with new last_run_id
  → If no new failure: exit silently (empty stdout)
```

## Key design decisions

- Uses `gh run list` with `--event workflow_dispatch` and `--event pull_request_target` separately, then merges results
- For pull_request_target events, checks `gh pr list --head <branch> --state open` to exclude merged PRs
- State file prevents re-reporting the same failure
- Output format includes: run URL, event type, branch, conclusion, failed logs (last 200 lines), annotations
