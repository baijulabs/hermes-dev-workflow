#!/usr/bin/env python3
"""
Agent queue — file-locked JSON store for the unified processor.

Two producers (review-failed-watch.py, pr-check-watch.py) write items.
One consumer (kanban-agent-queue-processor) reads and processes them.

Thread/process safety: fcntl.flock on the state file.
"""
import fcntl
import json
import os
import time
from pathlib import Path

STATE_DIR = Path(__file__).resolve().parent
QUEUE_FILE = STATE_DIR / "agent-queue.json"
LOCK_FILE = STATE_DIR / "agent-queue.lock"


def _locked_read():
    """Open the lock file, acquire exclusive lock, read JSON."""
    lock_fd = open(LOCK_FILE, "a+")
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    data = {"items": [], "last_processed_at": None}
    if QUEUE_FILE.exists():
        try:
            data = json.loads(QUEUE_FILE.read_text())
        except (json.JSONDecodeError, FileNotFoundError):
            pass
    return lock_fd, data


def _locked_write(lock_fd, data):
    """Write JSON, release lock, close."""
    QUEUE_FILE.write_text(json.dumps(data, indent=2))
    fcntl.flock(lock_fd, fcntl.LOCK_UN)
    lock_fd.close()


def enqueue(item_id, item_type, payload, dedup_key=None):
    """
    Add an item to the queue. Skips if a pending item with the same
    dedup_key already exists (prevents duplicate work items).
    """
    lock_fd, data = _locked_read()

    # Dedup: skip if same dedup_key already pending
    if dedup_key:
        for existing in data["items"]:
            if existing.get("dedup_key") == dedup_key and existing.get("status") in ("pending", "processing"):
                _locked_write(lock_fd, data)
                return False  # duplicate, skipped

    data["items"].append({
        "id": item_id,
        "type": item_type,
        "dedup_key": dedup_key,
        "status": "pending",
        "created_at": int(time.time()),
        "payload": payload,
    })
    _locked_write(lock_fd, data)
    return True


def get_pending(limit=None):
    """Return all pending items. Does NOT mark them — caller calls `mark_processing` first."""
    lock_fd, data = _locked_read()
    items = [i for i in data["items"] if i.get("status") == "pending"]
    if limit:
        items = items[:limit]
    _locked_write(lock_fd, data)
    return items


def mark_processing(item_ids):
    """Mark items as in-progress so they aren't picked up by another tick."""
    lock_fd, data = _locked_read()
    for item in data["items"]:
        if item["id"] in item_ids and item.get("status") == "pending":
            item["status"] = "processing"
    _locked_write(lock_fd, data)


def mark_processed(item_ids):
    """Mark items as done."""
    lock_fd, data = _locked_read()
    now = int(time.time())
    for item in data["items"]:
        if item["id"] in item_ids:
            item["status"] = "processed"
            item["processed_at"] = now
    data["last_processed_at"] = now
    # Keep last 100 items, drop older
    data["items"] = [
        i for i in data["items"]
        if i.get("status") == "pending" or (now - i.get("created_at", 0)) < 86400
    ][-200:]
    _locked_write(lock_fd, data)


def get_queue_summary():
    """Return counts for display."""
    lock_fd, data = _locked_read()
    counts = {"pending": 0, "processing": 0, "processed": 0, "total": len(data["items"])}
    for item in data["items"]:
        s = item.get("status", "unknown")
        counts[s] = counts.get(s, 0) + 1
    _locked_write(lock_fd, data)
    return counts


# ── Convenience: create a review-failed queue item ──

def enqueue_review_failed(reviewer_task_id, coder_task_id, branch,
                           base_branch, findings_summary, findings_details,
                           files, verification):
    """Write a review-failed work item to the queue."""
    return enqueue(
        item_id=f"rf_{reviewer_task_id}_{int(time.time())}",
        item_type="review-failed",
        dedup_key=f"reviewer:{reviewer_task_id}",
        payload={
            "reviewer_id": reviewer_task_id,
            "coder_id": coder_task_id,
            "branch": branch,
            "base_branch": base_branch,
            "findings": {
                "summary": findings_summary,
                "details": findings_details,
                "files": files,
                "verification": verification,
            },
        },
    )


# ── Convenience: create a ci-failure queue item ──

def enqueue_ci_failure(case, pr_number, branch, base_branch,
                       failed_jobs, failure_logs, failure_type,
                       run_url="", run_id=""):
    """Write a CI failure work item to the queue."""
    return enqueue(
        item_id=f"ci_{pr_number}_{int(time.time())}",
        item_type="ci-failure",
        dedup_key=f"pr:{pr_number}:{case}",
        payload={
            "case": case,  # "merge_conflict" or "failing_checks"
            "pr_number": pr_number,
            "branch": branch,
            "base_branch": base_branch,
            "failed_jobs": failed_jobs,
            "failure_logs": failure_logs,
            "failure_type": failure_type,
            "run_url": run_url,
            "run_id": run_id,
        },
    )