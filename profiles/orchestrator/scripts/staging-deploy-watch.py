#!/usr/bin/env python3
"""
staging-deploy-watch.py — Poll GitHub Actions for the latest manual staging deploy runs.

Outputs failure details to stdout when a new failed run is detected.
Designed to run as the script of a no_agent=True cron job, or piped
into an agent prompt for automatic card creation.

State tracking: a JSON file at ~/.hermes/orchestrator/state/staging-deploy-watch.json
remembers the last processed run ID so we only alert on new failures.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

STATE_DIR = Path.home() / ".hermes" / "profiles" / "orchestrator" / "state"
STATE_FILE = STATE_DIR / "staging-deploy-watch.json"
REPO = "${HERMES_PROJECT_REPO:-my-org/MyProject}"
WORKFLOW_ID = "deploy.yml"
REPO_DIR = Path("${HERMES_PROJECT_DIR:-/home/user/project}")
KANBAN_DB = Path.home() / ".hermes" / "kanban" / "boards" / "${HERMES_KANBAN_BOARD:-my-project-dev}" / "kanban.db"

def ensure_state():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not STATE_FILE.exists():
        STATE_FILE.write_text(json.dumps({"last_run_id": None, "last_checked": 0}))

def load_state():
    return json.loads(STATE_FILE.read_text())

def save_state(state):
    STATE_FILE.write_text(json.dumps(state))

def gh(*args, timeout=60):
    result = subprocess.run(
        ["gh", *args],
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        print(f"gh error: {result.stderr}", file=sys.stderr)
        return None
    return result.stdout.strip()

EVENTS = ["workflow_dispatch", "pull_request_target", "push"]

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
    """Fetch the last 5 completed runs for each tracked event type on the deploy workflow.
    Filters out pull_request_target runs where the PR is already closed/merged.
    Filters out dependabot branches entirely — they are auto-generated dependency
    bumps that fail independently of our code and should not trigger fix cards."""
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
        # Filter out dependabot branches — auto-generated dependency bumps
        completed = [r for r in completed if not (r.get("headBranch", "") or "").startswith("dependabot/")]
        # For pull_request_target, exclude runs where the PR is already merged/closed
        if event == "pull_request_target":
            completed = [r for r in completed if is_pr_still_open(r.get("headBranch", ""))]
        all_failed.extend(completed)
    all_failed.sort(key=lambda r: r.get("createdAt", ""), reverse=True)
    return all_failed

def get_failed_jobs(run_id):
    """Get failed job details for a specific run."""
    data = gh("run", "view", str(run_id),
              "--repo", REPO,
              "--log-failed",
              "--json", "jobs")
    if not data:
        # --log-failed outputs text, not JSON. Let's try different approach
        pass
    # Try getting just the failed step names and log excerpts
    result = subprocess.run(
        ["gh", "run", "view", str(run_id), "--repo", REPO, "--log-failed"],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode == 0 and result.stdout.strip():
        lines = result.stdout.strip().split("\n")
        # Return the last 200 lines of failed output
        return "\n".join(lines[-200:])
    return None

def get_annotations(run_id):
    """Get annotations/errors from a run."""
    data = gh("run", "view", str(run_id),
              "--repo", REPO,
              "--json", "annotations")
    if data:
        try:
            annotations = json.loads(data)
            errors = [a for a in annotations if a.get("annotation_level") in ("failure", "error")]
            if errors:
                return "\n".join(f"{a.get('path','')}:{a.get('start_line','')} - {a.get('message','')}" for a in errors[:10])
        except json.JSONDecodeError:
            pass
    return None

def has_open_pr_for_branch(branch):
    """Check if there's an open PR for the given branch. Returns PR number or None."""
    if not branch or branch == "main":
        return None
    data = gh("pr", "list",
              "--repo", REPO,
              "--head", branch,
              "--state", "open",
              "--json", "number,url",
              "--limit", "1")
    if not data:
        return None
    try:
        prs = json.loads(data)
        if prs:
            return prs[0]
    except json.JSONDecodeError:
        pass
    return None


def rerun_ci(pr_number, branch):
    """Re-trigger CI on an existing PR by pushing an empty commit or using gh pr checks."""
    # Trigger a re-run via gh pr checks
    result = subprocess.run(
        ["gh", "pr", "checks", str(pr_number), "--watch", "--fail-fast"],
        cwd=REPO_DIR, capture_output=True, text=True, timeout=10,
    )
    # If that doesn't work, trigger via workflow_dispatch
    return False


def main():
    ensure_state()
    state = load_state()

    runs = get_latest_failed_runs()
    if not runs:
        # No failed runs — just update timestamp
        state["last_checked"] = int(time.time())
        save_state(state)
        return  # silent exit (no_agent mode: empty stdout = no alert)

    # Find runs newer than our last processed one
    last_id = state.get("last_run_id")
    if last_id is not None:
        last_id = int(last_id)  # state file may store as string
    new_failures = []
    for run in runs:
        if last_id is not None and run["databaseId"] <= last_id:
            break  # runs are ordered newest-first
        new_failures.append(run)

    if not new_failures:
        # Update timestamp but nothing new to report
        state["last_checked"] = int(time.time())
        save_state(state)
        return

    # Report the most recent new failure
    run = new_failures[0]
    run_id = run["databaseId"]
    branch = run.get("headBranch", "unknown")

    # DEDUP: Check if there are open kanban fix cards for this branch
    # If cards are still in progress, the fix hasn't landed yet — do nothing.
    # If all previous cards are done/cancelled, start a new cycle.
    if branch != "main" and branch != "unknown":
        # Check if there's an open PR for this branch (fix already exists, just needs CI)
        open_pr = has_open_pr_for_branch(branch)
        
        # Check if there are open kanban fix cards for this branch
        result = subprocess.run(
            ["sqlite3", str(KANBAN_DB),
             f"SELECT COUNT(*) FROM tasks WHERE branch_name = '{branch}' AND status NOT IN ('done','cancelled','archived') AND assignee = 'coder';"],
            capture_output=True, text=True, timeout=10,
        )
        try:
            open_cards = int(result.stdout.strip())
        except (ValueError, TypeError):
            open_cards = 0

        if open_cards > 0:
            # Fix cards are still in flight — let them finish silently
            state["last_run_id"] = run_id
            state["last_checked"] = int(time.time())
            save_state(state)
            return

        if open_pr:
            # Fix already has an open PR — re-trigger CI on it
            print(f"## CI Failure on {branch} — open PR #{open_pr['number']} exists")
            print(f"PR: {open_pr['url']}")
            print(f"Failed run: {run['url']}")
            subprocess.run(
                ["gh", "workflow", "run", WORKFLOW_ID, "--repo", REPO, "--ref", branch],
                capture_output=True, text=True, timeout=30,
            )
            print(f"Re-triggered CI on {branch}")
            state["last_run_id"] = run_id
            state["last_checked"] = int(time.time())
            save_state(state)
            return

    state["last_run_id"] = run_id
    state["last_checked"] = int(time.time())
    save_state(state)
    title = run.get("displayTitle", f"Run {run_id}")

    # Get failure details
    failed_logs = get_failed_jobs(run_id)
    annotations = get_annotations(run_id)

    # Output structured data for delivery
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

    if annotations:
        output.append("### Annotations")
        output.append(annotations)
        output.append("")

    if failed_logs:
        output.append("### Failed Logs (last 200 lines)")
        output.append(failed_logs)

    print("\n".join(output))

if __name__ == "__main__":
    main()
