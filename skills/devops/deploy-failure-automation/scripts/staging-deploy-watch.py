"""staging-deploy-watch.py — Poll GitHub Actions for failed staging deploy runs.

Outputs failure details to stdout when a new failed run is detected.
For no_agent=True cron: empty stdout = silent, non-empty = delivery.

State file: ~/.hermes/profiles/orchestrator/state/staging-deploy-watch.json
"""
import json, subprocess, sys, time
from pathlib import Path

STATE_DIR = Path.home() / ".hermes" / "profiles" / "orchestrator" / "state"
STATE_FILE = STATE_DIR / "staging-deploy-watch.json"
REPO = "my-org/MyProject"
WORKFLOW_ID = "deploy.yml"

def ensure_state():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not STATE_FILE.exists():
        STATE_FILE.write_text(json.dumps({"last_run_id": None, "last_checked": 0}))

def load_state(): return json.loads(STATE_FILE.read_text())
def save_state(s): STATE_FILE.write_text(json.dumps(s))

def gh(*args, timeout=30):
    r = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout)
    return None if r.returncode else r.stdout.strip()

def is_pr_still_open(branch):
    if not branch or branch == "main": return True
    data = gh("pr", "list", "--repo", REPO, "--head", branch, "--state", "open",
              "--json", "number", "--limit", "1")
    if not data: return False
    try: return len(json.loads(data)) > 0
    except: return True

EVENTS = ["workflow_dispatch", "pull_request_target"]

def get_failed_runs():
    all_failed = []
    for event in EVENTS:
        data = gh("run", "list", "--repo", REPO, "--workflow", WORKFLOW_ID,
                   "--event", event, "--limit", "5",
                   "--json", "databaseId,conclusion,createdAt,displayTitle,headBranch,url,status,event")
        if not data: continue
        try: runs = json.loads(data)
        except: continue
        failed = [r for r in runs if r.get("conclusion") in ("failure", "cancelled")]
        if event == "pull_request_target":
            failed = [r for r in failed if is_pr_still_open(r.get("headBranch", ""))]
        all_failed.extend(failed)
    all_failed.sort(key=lambda r: r.get("createdAt", ""), reverse=True)
    return all_failed

def get_failed_logs(run_id):
    r = subprocess.run(["gh", "run", "view", str(run_id), "--repo", REPO, "--log-failed"],
                       capture_output=True, text=True, timeout=60)
    if r.returncode == 0 and r.stdout.strip():
        return "\n".join(r.stdout.strip().split("\n")[-200:])
    return None

def get_latest_successful_runs():
    """Return dict of {branch: created_at} for the latest successful run per branch."""
    success_by_branch = {}
    for event in EVENTS:
        data = gh("run", "list", "--repo", REPO, "--workflow", WORKFLOW_ID,
                   "--event", event, "--limit", "10",
                   "--json", "databaseId,conclusion,createdAt,headBranch,status")
        if not data:
            continue
        try:
            runs = json.loads(data)
        except:
            continue
        for r in runs:
            if r.get("conclusion") != "success":
                continue
            branch = r.get("headBranch", "")
            created = r.get("createdAt", "")
            if branch and (branch not in success_by_branch or created > success_by_branch[branch]):
                success_by_branch[branch] = created
    return success_by_branch


def is_stale_failure(run):
    """Check if a newer successful run exists on the same branch (fix already pushed)."""
    if run.get("event") == "workflow_dispatch":
        return False  # manual deploys don't auto-re-run
    branch = run.get("headBranch", "")
    if not branch:
        return False
    success_by_branch = get_latest_successful_runs()
    latest_success = success_by_branch.get(branch)
    if not latest_success:
        return False
    return latest_success > run.get("createdAt", "")


def main():
    ensure_state()
    state = load_state()
    runs = get_failed_runs()
    if not runs:
        state["last_checked"] = int(time.time())
        save_state(state)
        return

    last_id = state.get("last_run_id")
    new_failures = [r for r in runs if not last_id or r["databaseId"] > last_id]
    if not new_failures:
        state["last_checked"] = int(time.time())
        save_state(state)
        return

    # Filter out stale failures — a newer successful run on the same branch
    # means a fix was already pushed after the failed run started.
    fresh_failures = [r for r in new_failures if not is_stale_failure(r)]
    if not fresh_failures:
        state["last_checked"] = int(time.time())
        save_state(state)
        return

    run = fresh_failures[0]
    state["last_run_id"] = run["databaseId"]
    state["last_checked"] = int(time.time())
    save_state(state)

    logs = get_failed_logs(run["databaseId"])
    output = [
        f"## Deploy Failed \u2014 {run.get('event', 'unknown').replace('_', ' ').title()}",
        f"Run: {run['url']}",
        f"Event: {run.get('event', 'unknown')}",
        f"Branch: {run.get('headBranch', 'unknown')}",
        f"Conclusion: {run['conclusion']}",
        f"Created: {run.get('createdAt', 'unknown')}",
        "",
    ]
    if logs:
        output.append("### Failed Logs (last 200 lines)")
        output.append(logs)
    print("\n".join(output))

if __name__ == "__main__":
    main()