#!/usr/bin/env python3
"""
staging-deploy-watch.py — Poll GitHub Actions for the latest failed deploy runs.

Outputs failure details to stdout when a new failed run is detected.
Designed as the script input for a cron job that auto-creates kanban fix cards.

State tracking: a JSON file at ~/.hermes/profiles/<profile>/state/staging-deploy-watch.json
remembers the last processed run ID so we only alert on new failures.

Usage:
  python3 staging-deploy-watch.py
  # Pipe into an agent or use as cronjob(script='staging-deploy-watch.py')
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

STATE_DIR = Path.home() / ".hermes" / "profiles" / "orchestrator" / "state"
STATE_FILE = STATE_DIR / "staging-deploy-watch.json"
REPO = "$HERMES_PROJECT_REPO"
WORKFLOW_ID = "deploy.yml"
EVENTS = ["workflow_dispatch", "pull_request_target"]


def ensure_state():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not STATE_FILE.exists():
        STATE_FILE.write_text(json.dumps({"last_run_id": None, "last_checked": 0}))


def load_state():
    return json.loads(STATE_FILE.read_text())


def save_state(state):
    STATE_FILE.write_text(json.dumps(state))


def gh(*args, timeout=30):
    result = subprocess.run(
        ["gh", *args],
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        print(f"gh error: {result.stderr}", file=sys.stderr)
        return None
    return result.stdout.strip()


def is_pr_still_open(branch):
    """Check if the branch has an open PR. Returns True for open/active, False for merged/closed."""
    if not branch or branch == "main":
        return True  # main branch runs are always relevant
    data = gh("pr", "list",
              "--repo", REPO,
              "--head", branch,
              "--state", "open",
              "--json", "number",
              "--limit", "1")
    if not data:
        return False  # no open PR = PR was merged/closed
    try:
        prs = json.loads(data)
        return len(prs) > 0
    except json.JSONDecodeError:
        return True  # on error, include it (false positive > false negative)


def get_latest_failed_runs():
    """Fetch completed failed runs for each tracked event.
    Excludes pull_request_target runs where the PR is already closed/merged."""
    all_failed = []
    for event in EVENTS:
        data = gh("run", "list",
                  "--repo", REPO,
                  "--workflow", WORKFLOW_ID,
                  "--event", event,
                  "--limit", "5",
                  "--json", "databaseId,conclusion,createdAt,displayTitle,headBranch,url,status,event")
        if not data:
            continue
        try:
            runs = json.loads(data)
        except json.JSONDecodeError:
            continue
        completed = [r for r in runs if r.get("conclusion") in ("failure", "cancelled")]
        # For pull_request_target, exclude runs where the PR is already merged/closed
        if event == "pull_request_target":
            completed = [r for r in completed if is_pr_still_open(r.get("headBranch", ""))]
        all_failed.extend(completed)
    all_failed.sort(key=lambda r: r.get("createdAt", ""), reverse=True)
    return all_failed


def get_failed_jobs(run_id):
    """Get failed job log output for a specific run."""
    result = subprocess.run(
        ["gh", "run", "view", str(run_id), "--repo", REPO, "--log-failed"],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode == 0 and result.stdout.strip():
        lines = result.stdout.strip().split("\n")
        return "\n".join(lines[-200:])
    return None


def main():
    ensure_state()
    state = load_state()

    runs = get_latest_failed_runs()
    if not runs:
        state["last_checked"] = int(time.time())
        save_state(state)
        return  # silent exit — no new failures

    # Find runs newer than our last processed one
    last_id = state.get("last_run_id")
    new_failures = []
    for run in runs:
        if last_id and run["databaseId"] <= last_id:
            break  # runs are ordered newest-first
        new_failures.append(run)

    if not new_failures:
        state["last_checked"] = int(time.time())
        save_state(state)
        return

    # Report the most recent new failure
    run = new_failures[0]
    state["last_run_id"] = run["databaseId"]
    state["last_checked"] = int(time.time())
    save_state(state)

    run_id = run["databaseId"]
    branch = run.get("headBranch", "unknown")
    title = run.get("displayTitle", f"Run {run_id}")

    failed_logs = get_failed_jobs(run_id)

    output = [
        f"## Deploy Failed — {run.get('event', 'unknown').replace('_', ' ').title()}",
        f"Run: {run['url']}",
        f"Event: {run.get('event', 'unknown')}",
        f"Branch: {branch}",
        f"Title: {title}",
        f"Conclusion: {run['conclusion']}",
        f"Created: {run.get('createdAt', 'unknown')}",
        "",
    ]

    if failed_logs:
        output.append("### Failed Logs (last 200 lines)")
        output.append(failed_logs)

    print("\n".join(output))


if __name__ == "__main__":
    main()