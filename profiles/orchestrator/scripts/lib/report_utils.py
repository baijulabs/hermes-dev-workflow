#!/usr/bin/env python3
"""
Shared reporting utility for no_agent cron jobs.
Tracks last reported output hash and suppresses duplicate reports.
"""
import hashlib
import json
import os

STATE_DIR = os.path.expanduser("~/.hermes/profiles/orchestrator/state/report_hashes")


def _ensure_dir():
    os.makedirs(STATE_DIR, exist_ok=True)


def _state_path(job_name):
    return os.path.join(STATE_DIR, f"{job_name}.json")


def should_report(job_name, output):
    """Returns True if this output differs from last reported output for this job."""
    _ensure_dir()
    path = _state_path(job_name)
    current_hash = hashlib.sha256(output.encode()).hexdigest()[:16]

    last = {}
    if os.path.exists(path):
        try:
            last = json.load(open(path))
        except (json.JSONDecodeError, OSError):
            pass

    if last.get("hash") == current_hash:
        return False

    json.dump({"hash": current_hash, "preview": output[:80]}, open(path, "w"))
    return True
