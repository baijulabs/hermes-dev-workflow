#!/usr/bin/env python3
"""
pr-check-watch.py — no_agent cron script (every 5m, 24/7)

Polls open PRs on ${HERMES_PROJECT_REPO:-owner/project} for merge conflicts and CI failures.
Writes structured work items to the agent queue. The unified processor
picks them up and creates kanban fix cards.

Zero LLM calls. Zero token cost. Deterministic.

Re-triggers CI only when a fix has landed on the PR branch (HEAD changed).
"""
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

# ── Paths ───────────────────────────────────────────────────────────────────
SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
from agent_queue import enqueue_ci_failure

REPO = "${HERMES_PROJECT_REPO:-owner/project}"
REPO_DIR = "/home/user/Project"
KANBAN_DB = Path.home() / ".hermes" / "kanban" / "boards" / "${HERMES_KANBAN_BOARD:-project-dev}" / "kanban.db"
STATE_FILE = Path.home() / ".hermes" / "profiles" / "orchestrator" / "state" / "pr-check-watch.json"

# ── Helpers ─────────────────────────────────────────────────────────────────

def gh(*args, timeout=60) -> str | None:
    """Run a gh command, return stdout or None on failure."""
    try:
        r = subprocess.run(
            ["gh", *args],
            capture_output=True, text=True, timeout=timeout, cwd=REPO_DIR,
        )
        return r.stdout.strip() if r.returncode == 0 else None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def gh_json(*args, timeout=60) -> dict | list | None:
    """Run a gh command with --json, parse output."""
    data = gh(*args, timeout=timeout)
    if not data:
        return None
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        return None


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {"branches": {}, "last_checked": 0}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def has_active_kanban_card(branch: str) -> bool:
    """Check if there's an active (in-flight) coder card for this branch."""
    if not KANBAN_DB.exists():
        return False
    try:
        conn = sqlite3.connect(str(KANBAN_DB))
        cursor = conn.cursor()
        # Safer: only todo/ready/running, NOT blocked (blocked=crashed/dead)
        cursor.execute("""
            SELECT COUNT(*) FROM tasks
            WHERE status IN ('todo', 'ready', 'running')
              AND assignee = 'coder'
              AND (branch_name = ? OR body LIKE '%' || ? || '%')
        """, (branch, branch))
        count = cursor.fetchone()[0]
        # Also check queued items (in the agent queue)
        queue_count = count_queued_for_pr(branch)
        conn.close()
        return (count + queue_count) > 0
    except sqlite3.Error:
        return False


def count_queued_for_pr(branch: str) -> int:
    """Check agent queue for pending items for this branch."""
    queue_file = Path.home() / ".hermes" / "profiles" / "orchestrator" / "state" / "agent-queue.json"
    if not queue_file.exists():
        return 0
    try:
        data = json.loads(queue_file.read_text())
        count = 0
        for item in data.get("items", []):
            if item.get("status") in ("pending", "processing"):
                p = item.get("payload", {})
                if p.get("branch") == branch:
                    count += 1
        return count
    except (json.JSONDecodeError, FileNotFoundError, KeyError):
        return 0


def pr_head_changed(branch: str, new_sha: str) -> bool:
    """Check if the PR branch HEAD has changed since last detection."""
    state = load_state()
    old_sha = state.get("branches", {}).get(branch, {}).get("sha")
    return old_sha is not None and old_sha != new_sha


def update_branch_state(branch: str, sha: str):
    """Record the current HEAD SHA for this branch."""
    state = load_state()
    state["branches"][branch] = {"sha": sha, "updated": int(time.time())}
    state["last_checked"] = int(time.time())
    save_state(state)


# ── CI Re-Trigger ───────────────────────────────────────────────────────────

def rerun_ci(branch: str) -> bool:
    """Re-trigger CI on the PR branch via workflow_dispatch."""
    result = subprocess.run(
        ["gh", "workflow", "run", "deploy.yml", "--repo", REPO, "--ref", branch],
        capture_output=True, text=True, timeout=30,
    )
    return result.returncode == 0


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    # 1. Get open PRs (non-dependabot)
    # Try GraphQL (gh pr list) first; fall back to REST on rate limit
    prs = gh_json("pr", "list", "--repo", REPO, "--state", "open",
                  "--json", "number,title,headRefName,baseRefName,mergeable,statusCheckRollup",
                  "--limit", "30")

    if prs is None:
        # GraphQL exhausted — try REST
        rest_data = gh("api", f"repos/{REPO}/pulls",
                       "-f", "state=open", "-f", "per_page=30",
                       "--jq", '.[] | {number, title, headRefName: .head.ref, baseRefName: .base.ref, mergeable, headSha: .head.sha}')
        if rest_data:
            prs = []
            for line in rest_data.strip().split("\n"):
                try:
                    pr = json.loads(line)
                    prs.append({
                        "number": pr.get("number"),
                        "title": pr.get("title"),
                        "headRefName": pr.get("headRefName"),
                        "baseRefName": pr.get("baseRefName"),
                        "mergeable": pr.get("mergeable"),
                        "statusCheckRollup": None,  # REST doesn't include this
                    })
                except json.JSONDecodeError:
                    continue

    if not prs:
        return  # no open PRs

    state = load_state()
    queued_items = 0
    recent_item_summaries = []  # collect for stdout delivery message

    for pr in prs:
        # Skip dependabot
        branch = pr.get("headRefName", "")
        if not branch or branch.startswith("dependabot/"):
            continue

        pr_number = pr.get("number")
        base_branch = pr.get("baseRefName", "main")

        # 2. Check for merge conflicts
        mergeable = pr.get("mergeable")
        # mergeable can be "MERGEABLE", "CONFLICTING", "UNKNOWN", or None
        # Also check mergeStateStatus if available
        merge_state = pr.get("mergeStateStatus", "")

        is_conflicting = (mergeable == "CONFLICTING" or merge_state == "DIRTY")

        # 3. Check for CI failures
        check_rollup = pr.get("statusCheckRollup", [])
        failing_checks = []
        if isinstance(check_rollup, list):
            failing_checks = [
                c for c in check_rollup
                if c.get("status") == "COMPLETED" and c.get("conclusion") == "FAILURE"
            ]

        if not is_conflicting and not failing_checks:
            continue  # healthy PR

        # 4. Dedup — skip if fix already in flight
        if has_active_kanban_card(branch):
            continue

        # 5. Check if fix landed and we should re-trigger
        # Get current HEAD SHA
        head_sha = gh("api", f"repos/{REPO}/pulls/{pr_number}",
                      "--jq", ".head.sha")
        if head_sha and pr_head_changed(branch, head_sha):
            # Head changed since last failure detection → re-trigger CI
            if rerun_ci(branch):
                print(f"🔄 Re-triggered CI on {branch} (HEAD changed)")
                update_branch_state(branch, head_sha)
            continue

        # Record current state
        if head_sha:
            update_branch_state(branch, head_sha)

        # 6. Classify and queue
        if is_conflicting:
            # Case A: Merge conflicts
            enqueue_ci_failure(
                case="merge_conflict",
                pr_number=pr_number,
                branch=branch,
                base_branch=base_branch,
                failed_jobs=[],
                failure_logs=f"mergeable={mergeable}, mergeStateStatus={merge_state}",
                failure_type="merge_conflict",
                run_url="",
                run_id="",
            )
            queued_items += 1
            recent_item_summaries.append(f"PR #{pr_number} ({branch}): merge_conflict")

        elif failing_checks:
            # Case B: CI failures — get failure details
            failed_job_names = [c.get("name", "unknown") for c in failing_checks]

            # Get latest failed run logs
            runs = gh_json("run", "list", "--repo", REPO, "--branch", branch,
                          "--limit", "3", "--json", "databaseId,conclusion,event,status,url")
            failed_runs = []
            if runs:
                failed_runs = [r for r in runs if r.get("conclusion") in ("failure", "cancelled")]

            failure_logs = ""
            run_url = ""
            run_id = ""
            if failed_runs:
                run_id = str(failed_runs[0].get("databaseId", ""))
                run_url = failed_runs[0].get("url", "")
                # Get log excerpts
                log_output = gh("run", "view", run_id, "--repo", REPO, "--log-failed")
                if log_output:
                    lines = log_output.split("\n")
                    failure_logs = "\n".join(lines[-200:])  # last 200 lines

            enqueue_ci_failure(
                case="failing_checks",
                pr_number=pr_number,
                branch=branch,
                base_branch=base_branch,
                failed_jobs=failed_job_names,
                failure_logs=failure_logs,
                failure_type="ci",
                run_url=run_url,
                run_id=run_id,
            )
            queued_items += 1
            summary = f"PR #{pr_number} ({branch}): ci"
            if run_url:
                summary += f" — {run_url}"
            recent_item_summaries.append(summary)

    if queued_items > 0:
        print(f"📋 {queued_items} CI item(s):")
        for s in recent_item_summaries:
            print(f"  • {s}")


if __name__ == "__main__":
    main()