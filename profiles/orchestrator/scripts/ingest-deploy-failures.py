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
REPO = "baijulabs/Liberkyma"
WORKFLOW_ID = "deploy.yml"
REPO_DIR = Path.home() / "Liberkyma"
KANBAN_DB = Path.home() / ".hermes" / "kanban" / "boards" / "liberkyma-dev" / "kanban.db"

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
        # UNLESS the run had a Deploy to Staging job (merge events do trigger deploy)
        if event == "pull_request_target":
            filtered = []
            for r in completed:
                if is_pr_still_open(r.get("headBranch", "")):
                    filtered.append(r)
                else:
                    # PR is closed/merged — check if deploy job ran on this run
                    deploy_status, _ = get_deploy_job_status(r["databaseId"])
                    if deploy_status in ("success", "failed", "skipped"):
                        # Deploy job was attempted — this is a merge event, keep it
                        filtered.append(r)
                    # deploy_status == "unknown" means no deploy job found — stale CI run, skip
            completed = filtered
        all_failed.extend(completed)
    all_failed.sort(key=lambda r: r.get("createdAt", ""), reverse=True)
    return all_failed

def get_deploy_job_status(run_id):
    """Check whether Deploy to Staging job succeeded, failed, or was skipped.
    Returns ('success', None), ('failed', None), ('skipped', None), or ('unknown', error_msg)."""
    data = gh("run", "view", str(run_id),
              "--repo", REPO,
              "--json", "jobs")
    if not data:
        return ("unknown", "Could not fetch job data")
    try:
        jobs = json.loads(data).get("jobs", [])
    except json.JSONDecodeError:
        return ("unknown", "Could not parse job data")
    for j in jobs:
        if j.get("name") == "Deploy to Staging":
            status = j.get("status", "unknown")
            conclusion = j.get("conclusion")
            if conclusion == "success":
                return ("success", None)
            elif conclusion == "failure":
                return ("failed", None)
            elif conclusion == "skipped":
                return ("skipped", None)
            elif status in ("queued", "in_progress", "waiting"):
                return ("running", None)
    return ("unknown", "Deploy to Staging job not found in run")

def get_test_failed_jobs(run_id):
    """Get names of non-deploy jobs that failed in this run."""
    data = gh("run", "view", str(run_id),
              "--repo", REPO,
              "--json", "jobs")
    if not data:
        return []
    try:
        jobs = json.loads(data).get("jobs", [])
    except json.JSONDecodeError:
        return []
    deploy_jobs = {"Deploy to Staging", "Deploy to Production", "Deploy Terraform",
                   "E2E on Staging", "Prepare Deployment", "Lighthouse CI (Mobile)"}
    failed = []
    for j in jobs:
        if j.get("conclusion") == "failure" and j.get("name") not in deploy_jobs:
            failed.append(j["name"])
    return failed

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
    """Get annotations/errors from a run via gh api."""
    data = gh("api", f"repos/{REPO}/actions/runs/{run_id}",
              "--jq", ".annotations")
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


def create_test_failure_issue(run, test_failures):
    """Create a GitHub issue for test failures on a deploy run.
    Deduplicates against existing issues for the same run_id.
    Returns issue number or None if already exists."""
    run_id = run["databaseId"]
    branch = run.get("headBranch", "unknown")
    event = run.get("event", "unknown")

    # Dedup: check if an issue already exists for this run_id
    existing = gh("issue", "list",
                  "--repo", REPO,
                  "--label", "test-failure",
                  "--search", f"Run-{run_id}",
                  "--json", "number",
                  "--limit", "1")
    if existing:
        try:
            existing_issues = json.loads(existing)
            if existing_issues:
                return None  # Issue already exists
        except json.JSONDecodeError:
            pass

    # Build title from failed test names (cap at 3 for readability)
    failed_names = ", ".join(test_failures[:3])
    if len(test_failures) > 3:
        failed_names += f" and {len(test_failures) - 3} more"

    title = f"[Test Failure] {failed_names} Run {run_id}"

    body_parts = [
        f"## Test Failure Report",
        f"**Run:** {run['url']}",
        f"**Run ID:** {run_id}",
        f"**Branch:** {branch}",
        f"**Event:** {event}",
        "**Failed Tests:**",
    ]
    for t in test_failures:
        body_parts.append(f"- {t}")
    body_parts.append("")
    body_parts.append("Deploy to staging proceeded but these non-gating tests failed.")
    body_parts.append("Please investigate and fix the underlying issues.")
    body = "\n".join(body_parts)

    result = gh("issue", "create",
                "--repo", REPO,
                "--title", title,
                "--body", body,
                "--label", "ready-for-agent,test-failure")
    if result:
        # Extract issue number from output like "https://github.com/baijulabs/Liberkyma/issues/123"
        try:
            return int(result.rsplit("/", 1)[-1])
        except (ValueError, IndexError):
            return result
    return None


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
    event = run.get("event", "unknown")

    # DEDUP: Check if there are open kanban fix cards for main branch
    if branch == "main":
        # Check if there are open kanban fix cards for main branch
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
    else:
        # Non-main branches are handled by ingest-ci-failures (every 5m).
        # Skip silently — ingest-ci-failures will detect CI failures on open PRs
        # and enqueue fix tasks via the agent queue.
        state["last_run_id"] = run_id
        state["last_checked"] = int(time.time())
        save_state(state)
        return

    # ── Deploy vs Test failure classification ──
    deploy_status, deploy_error = get_deploy_job_status(run_id)
    test_failures = get_test_failed_jobs(run_id)

    # Create GH issue for test failures BEFORE deploy-status filtering,
    # so deploy status does not suppress issue creation.
    created_issue = None
    if test_failures:
        # PR dedup is handled above (open_cards > 0 returns early);
        # if we reached here, no fix cards are in flight.
        created_issue = create_test_failure_issue(run, test_failures)

    # If deploy succeeded and no test failures, this run had a non-gating failure
    # (e.g. a skipped job showing failure). Skip silently.
    if deploy_status == "success" and not test_failures:
        state["last_run_id"] = run_id
        state["last_checked"] = int(time.time())
        save_state(state)
        return

    state["last_run_id"] = run_id
    state["last_checked"] = int(time.time())
    save_state(state)
    title = run.get("displayTitle", f"Run {run_id}")

    failed_logs = None
    if deploy_status == "failed":
        failed_logs = get_failed_jobs(run_id)

    output = []
    if deploy_status == "failed":
        output.extend([
            f"## ⚠️ Deploy Failure — {event.replace('_', ' ').title()}",
            f"Run: {run['url']}",
            f"Event: {event}",
            f"Branch: {branch}",
            f"Title: {title}",
            f"Conclusion: {run['conclusion']}",
            f"Created: {run.get('createdAt', 'unknown')}",
            "",
        ])
    elif deploy_status == "success" and test_failures:
        output.extend([
            f"## 🧪 Test Failure (deploy succeeded) — {event.replace('_', ' ').title()}",
            f"Deploy: ✅ Staging deployed successfully",
            f"Run: {run['url']}",
            f"Failed tests:",
        ])
        for t in test_failures:
            output.append(f"  • {t}")
        output.append("")
        output.append("The deploy itself went through, but non-gating tests failed.")
        output.append("These should be reviewed but don't block the staging environment.")
        output.append("")
        if created_issue:
            output.append(f"Created GitHub issue #{created_issue} for these test failures.")
    elif deploy_status == "skipped" and test_failures:
        output.extend([
            f"## 🧪 Test Failure (deploy skipped) — {event.replace('_', ' ').title()}",
            f"Run: {run['url']}",
            f"Event: {event}",
            f"Branch: {branch}",
            f"Title: {title}",
            f"Failed tests:",
        ])
        for t in test_failures:
            output.append(f"  • {t}")
        output.append("")
        if created_issue:
            output.append(f"Created GitHub issue #{created_issue} for these test failures.")
    else:
        # Generic fallback for other failure patterns
        output.extend([
            f"## ⚠️ Run Failure — {event.replace('_', ' ').title()}",
            f"Deploy to Staging: {deploy_status}",
            f"Run: {run['url']}",
            f"Event: {event}",
            f"Branch: {branch}",
            f"Title: {title}",
            f"Created: {run.get('createdAt', 'unknown')}",
            "",
        ])
        if test_failures:
            output.append("Failed tests:")
            for t in test_failures:
                output.append(f"  • {t}")
            output.append("")

    annotations = get_annotations(run_id)
    if annotations:
        output.append("### Annotations")
        output.append(annotations)
        output.append("")

    print("\n".join(output))

if __name__ == "__main__":
    main()
