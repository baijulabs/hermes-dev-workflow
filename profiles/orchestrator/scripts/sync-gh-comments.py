#!/usr/bin/env python3
"""
kanban-to-gh-tracker.py — Post milestone audit comments to GitHub issues.

Scans the kanban board for state transitions in the coder→reviewer pipeline
and posts idempotent audit comments to linked GitHub issues. Never closes issues
— that happens naturally via "Closes #XXX" in PR merge.

Runs as no_agent: true cron job. Empty stdout = nothing to do (silent delivery).
Non-empty stdout = comments posted (for audit/debug).

Milestones tracked:
  - decomposed: orchestrator card done (decomposition complete)
  - coder_done: coder card done (implementation complete)
  - reviewer_approved: reviewer card done (code review passed)

Idempotency: state file prevents duplicate comments per card chain.
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

STATE_DIR = Path.home() / ".hermes" / "profiles" / "orchestrator" / "state"
STATE_FILE = STATE_DIR / "kanban-to-gh-tracker.json"
KANBAN_DB = Path.home() / ".hermes" / "kanban" / "boards" / "${HERMES_KANBAN_BOARD:-project-dev}" / "kanban.db"
REPO = "${HERMES_PROJECT_REPO:-owner/project}"

STATE_DIR.mkdir(parents=True, exist_ok=True)


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"posted": {}}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def posted(state, key, milestone):
    """Check if a milestone was already posted for a given key (card/chain ID)."""
    return milestone in state["posted"].get(key, [])


def mark_posted(state, key, milestone):
    """Record that a milestone was posted for a given key."""
    state["posted"].setdefault(key, []).append(milestone)


def gh_issue_comment(issue_num, body):
    """Post a comment to a GitHub issue. Returns True on success."""
    result = subprocess.run(
        ["gh", "issue", "comment", str(issue_num),
         "--repo", REPO, "--body", body],
        capture_output=True, text=True, timeout=30,
    )
    return result.returncode == 0


def gh_issue_is_open(issue_num):
    """Check if a GitHub issue is open."""
    result = subprocess.run(
        ["gh", "issue", "view", str(issue_num), "--repo", REPO,
         "--json", "state", "--jq", ".state"],
        capture_output=True, text=True, timeout=15,
    )
    return result.stdout.strip() == "OPEN"


def extract_gh_issue(title):
    """Extract GH issue number from a kanban card title like '[GH-831] ...'."""
    m = re.search(r'\[GH-(\d+)\]', title)
    return int(m.group(1)) if m else None


def find_chain_root(cursor, card_id, assignee):
    """Walk up the parent chain to find the orchestrator root card.
    Falls back to extracting GH issue from the card's own title if
    no parent chain exists (some cards are created without parent links).
    Returns (orchestrator_card_id, issue_number) or (None, None)."""
    current = card_id
    for _ in range(5):  # safety limit, chains are max 3 deep
        cursor.execute(
            "SELECT parent_id FROM task_links WHERE child_id = ?", (current,))
        row = cursor.fetchone()
        if not row:
            break
        current = row[0]

    # Check if this root is an orchestrator card with a GH issue reference
    cursor.execute(
        "SELECT id, title, assignee FROM tasks WHERE id = ?", (current,))
    root = cursor.fetchone()
    if root and root[2] == "orchestrator":
        gh_issue = extract_gh_issue(root[1])
        if gh_issue:
            return (root[0], gh_issue)

    # Fallback: extract GH issue from the card's own title
    # (coder/reviewer cards created without parent links still have [GH-N])
    cursor.execute(
        "SELECT id, title, assignee FROM tasks WHERE id = ?", (card_id,))
    self_card = cursor.fetchone()
    if self_card:
        gh_issue = extract_gh_issue(self_card[1])
        if gh_issue:
            return (self_card[0], gh_issue)

    return (None, None)


def scan_and_post():
    """Main scan loop: find milestone transitions and post comments."""
    state = load_state()
    conn = sqlite3.connect(str(KANBAN_DB))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    posted_count = 0

    # ── 1. Orchestrator cards that just reached 'done' → "decomposed" ──
    cursor.execute("""
        SELECT id, title, status FROM tasks
        WHERE assignee = 'orchestrator'
          AND status = 'done'
          AND title LIKE '%[GH-%]%'
        ORDER BY created_at
    """)
    for row in cursor.fetchall():
        issue_num = extract_gh_issue(row["title"])
        if not issue_num:
            continue
        key = row["id"]
        if posted(state, key, "decomposed"):
            continue
        if not gh_issue_is_open(issue_num):
            mark_posted(state, key, "decomposed")  # don't retry closed issues
            continue

        body = (
            f"📋 **Decomposed** into implementation tasks.\n"
            f"Kanban orchestrator card: `{key}`.\n"
            f"The pipeline will post updates here as the fix progresses."
        )
        if gh_issue_comment(issue_num, body):
            mark_posted(state, key, "decomposed")
            posted_count += 1
            print(f"[decomposed] GH-{issue_num} ← {key}")

    # ── 2. Coder cards that just reached 'done' → "coder_done" ──
    cursor.execute("""
        SELECT id, title, status FROM tasks
        WHERE assignee = 'coder'
          AND status = 'done'
        ORDER BY created_at
    """)
    for row in cursor.fetchall():
        key = row["id"]
        if posted(state, key, "coder_done"):
            continue

        root_id, issue_num = find_chain_root(cursor, key, "coder")
        if not issue_num:
            continue
        if not gh_issue_is_open(issue_num):
            mark_posted(state, key, "coder_done")
            continue

        body = (
            f"✅ **Implementation complete** by coder (`{key}`).\n"
            f"Awaiting code review."
        )
        if gh_issue_comment(issue_num, body):
            mark_posted(state, key, "coder_done")
            posted_count += 1
            print(f"[coder_done] GH-{issue_num} ← {key}")

    # ── 3. Reviewer cards that just reached 'done' → "reviewer_approved" ──
    cursor.execute("""
        SELECT id, title, status FROM tasks
        WHERE assignee = 'code-reviewer'
          AND status = 'done'
        ORDER BY created_at
    """)
    for row in cursor.fetchall():
        key = row["id"]
        if posted(state, key, "reviewer_approved"):
            continue

        root_id, issue_num = find_chain_root(cursor, key, "code-reviewer")
        if not issue_num:
            continue
        if not gh_issue_is_open(issue_num):
            mark_posted(state, key, "reviewer_approved")
            continue

        body = (
            f"✅ **Code review passed** (`{key}`).\n"
            f"Awaiting PR consolidation — the fix will be merged and deployed automatically."
        )
        if gh_issue_comment(issue_num, body):
            mark_posted(state, key, "reviewer_approved")
            posted_count += 1
            print(f"[reviewer_approved] GH-{issue_num} ← {key}")

    conn.close()
    save_state(state)

    if posted_count > 0:
        print(f"Posted {posted_count} milestone comment(s)")
    return posted_count


if __name__ == "__main__":
    scan_and_post()