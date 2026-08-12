#!/usr/bin/env python3
"""
verify-deploy-qa.py — Detects new staging deploys for QA verification.

Polls the deploy workflow for successful deploy-to-staging jobs.
Compares against state file to avoid re-processing.
Outputs deploy details to stdout when a new deploy is found.
Silent (empty stdout) when nothing new.

NOTE: This job runs under the ORCHESTRATOR profile scheduler (the QA
profile has no daemon). State is still kept under the QA profile.

State file: ~/.hermes/profiles/qa/state/last_verified_deploy.json
"""
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = "${HERMES_PROJECT_REPO:-$HERMES_PROJECT_REPO}"
STATE_FILE = Path.home() / ".hermes" / "profiles" / "qa" / "state" / "last_verified_deploy.json"


def run(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return -1, "", str(e)


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_run_id": None, "last_verified_version": None}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state))


def main():
    state = load_state()

    # Get the latest successful deploy-to-staging run
    rc, out, _ = run([
        "gh", "run", "list", "--repo", REPO, "--workflow", "deploy.yml",
        "--limit", "20", "--json", "databaseId,conclusion,createdAt,headBranch,displayTitle,event",
        "--jq", '.[] | select(.conclusion == "success") | {id: .databaseId, created: .createdAt, branch: .headBranch, title: .displayTitle, event: .event}'
    ])
    if rc != 0:
        sys.exit(0)

    try:
        runs = [json.loads(line) for line in out.split('\n') if line.strip()]
    except json.JSONDecodeError:
        sys.exit(0)

    # Filter: only deploy runs triggered by pull_request_target closed (merges) or workflow_dispatch
    for deploy in runs:
        run_id = str(deploy["id"])

        # Check if this run actually had a successful Deploy to Staging job
        rc, jobs_out, _ = run([
            "gh", "api", f"repos/{REPO}/actions/runs/{run_id}/jobs",
            "--jq", '.jobs[] | select(.name == "Deploy to Staging" and .conclusion == "success") | .id'
        ])
        if rc != 0 or not jobs_out.strip():
            continue

        # Skip if already verified
        if run_id == state.get("last_run_id"):
            break  # we've seen this and everything newer — stop

        # Extract version from deploy summary or the workflow run
        # Try to get version from the pyproject.toml at the run's commit
        rc, sha, _ = run([
            "gh", "api", f"repos/{REPO}/actions/runs/{run_id}",
            "--jq", ".head_sha"
        ])
        version = "unknown"
        if rc == 0 and sha:
            rc, ver, _ = run([
                "gh", "api", f"repos/{REPO}/contents/backend/pyproject.toml?ref={sha}",
                "--jq", '.content'
            ])
            if rc == 0 and ver:
                import base64
                try:
                    decoded = base64.b64decode(ver).decode()
                    for line in decoded.split('\n'):
                        if line.startswith('version'):
                            version = line.split('"')[1]
                            break
                except Exception:
                    pass

        # Build deploy detection payload
        payload = {
            "run_id": run_id,
            "version": version,
            "deployed_at": deploy.get("created", ""),
            "branch": deploy.get("branch", ""),
            "title": deploy.get("title", ""),
            "event": deploy.get("event", ""),
        }

        # Save state BEFORE printing (so if agent fails, we don't re-process)
        state["last_run_id"] = run_id
        state["last_verified_version"] = version
        save_state(state)

        # Output deploy details as JSON for the agent to consume
        print(json.dumps(payload))
        return

    # No new deploy — silent
    sys.exit(0)


if __name__ == "__main__":
    main()
