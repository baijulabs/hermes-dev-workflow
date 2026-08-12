#!/usr/bin/env python3
"""
review-failed-watch.py — no_agent cron script (every 5m, 24/7)

Detects blocked code-reviewer cards and writes structured work items
to the agent queue. The unified processor picks them up and creates
kanban fix cards.

Zero LLM calls. Zero token cost. Deterministic.
"""
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

# ── Paths ───────────────────────────────────────────────────────────────────
SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
from agent_queue import enqueue_review_failed

KANBAN_DB = Path.home() / ".hermes" / "kanban" / "boards" / "${HERMES_KANBAN_BOARD:-project-dev}" / "kanban.db"


def main():
    if not KANBAN_DB.exists():
        return  # silent — no board, nothing to do

    conn = sqlite3.connect(str(KANBAN_DB))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # 1. Find blocked code-reviewer cards
    cursor.execute("""
        SELECT DISTINCT t.id, t.title, t.body, t.branch_name
        FROM tasks t
        JOIN task_events e ON e.task_id = t.id
        WHERE t.status = 'blocked'
          AND t.assignee = 'code-reviewer'
          AND e.kind = 'blocked'
          AND json_extract(e.payload, '$.reason') LIKE 'review-failed:%'
        ORDER BY t.created_at DESC
    """)
    blocked_reviewers = cursor.fetchall()

    if not blocked_reviewers:
        conn.close()
        return  # silent — nothing to process

    processed = 0

    for row in blocked_reviewers:
        reviewer_id = row["id"]

        # 2. Read the block reason (from task_events)
        cursor.execute("""
            SELECT payload FROM task_events
            WHERE task_id = ? AND kind = 'blocked'
            ORDER BY created_at DESC LIMIT 1
        """, (reviewer_id,))
        event = cursor.fetchone()
        if not event:
            continue
        payload = json.loads(event["payload"])
        reason = payload.get("reason", "")

        if not reason.startswith("review-failed:"):
            continue  # non-review-failed block → skip

        # 3. Find the parent coder card (via task_links)
        cursor.execute("""
            SELECT parent_id FROM task_links
            WHERE child_id = ?
        """, (reviewer_id,))
        link = cursor.fetchone()
        if not link:
            continue
        coder_id = link["parent_id"]

        # 4. Look up the coder's branch and base branch
        cursor.execute("""
            SELECT title, body, branch_name FROM tasks
            WHERE id = ?
        """, (coder_id,))
        coder = cursor.fetchone()
        if not coder:
            continue

        branch = coder["branch_name"] or "unknown"
        # Extract BASE BRANCH from coder card body
        base_branch = branch
        coder_body = coder["body"] or ""
        for line in coder_body.split("\n"):
            if line.strip().startswith("BASE BRANCH:"):
                base_branch = line.split(":", 1)[1].strip()
                break

        # 5. Read reviewer comments (most recent from code-reviewer)
        cursor.execute("""
            SELECT body FROM task_comments
            WHERE task_id = ? AND author = 'code-reviewer'
            ORDER BY created_at DESC LIMIT 1
        """, (reviewer_id,))
        comment = cursor.fetchone()
        comment_body = comment["body"] if comment else reason

        # 6. Parse findings from the comment (structured JSON or prose)
        findings_summary, findings_details, files, verification = parse_findings(comment_body, reason)

        # 7. Check: resolved 3+ times already? (loop detection)
        coder_title = coder["title"] or ""
        issue_hook = extract_issue_hook(coder_title)
        cursor.execute("""
            SELECT COUNT(*) FROM tasks
            WHERE title LIKE ? AND status IN ('archived','cancelled')
        """, (f"%{issue_hook}%Fix:%",))
        prior_fix_count = cursor.fetchone()[0]

        if prior_fix_count >= 3:
            # Escalate — add comment and archive
            print(f"⚠️ Looping: {reviewer_id} ({coder_title}) — {prior_fix_count} prior fix cycles. Escalating.")
            # We don't auto-create cards for looped items; the processor will handle escalation
            enqueue_review_failed(
                reviewer_task_id=reviewer_id,
                coder_task_id=coder_id,
                branch=branch,
                base_branch=base_branch,
                findings_summary=f"[LOOP: {prior_fix_count} prior cycles] {findings_summary}",
                findings_details=findings_details,
                files=files,
                verification=verification,
            )
        else:
            enqueue_review_failed(
                reviewer_task_id=reviewer_id,
                coder_task_id=coder_id,
                branch=branch,
                base_branch=base_branch,
                findings_summary=findings_summary,
                findings_details=findings_details,
                files=files,
                verification=verification,
            )

        processed += 1

    conn.close()

    if processed > 0:
        print(f"📋 {processed} review-failed item(s) queued for agent processor")


def parse_findings(comment_body, fallback_reason):
    """
    Parse reviewer findings from the comment.
    Handles both structured JSON (```json { ... } ```) and prose formats.
    """
    summary = ""
    details = ""
    files = []
    verification = ""

    # Try JSON extraction
    json_start = comment_body.find("```json")
    if json_start >= 0:
        json_end = comment_body.find("```", json_start + 7)
        if json_end >= 0:
            try:
                data = json.loads(comment_body[json_start + 7:json_end].strip())
                findings_list = data.get("findings", [])
                summary = data.get("summary", "")

                for f in findings_list:
                    details += f"- **{f.get('file','')}** ({f.get('severity','')}): {f.get('issue','')}\n"
                    if f.get("file"):
                        files.append(f.get("file"))

                # Build verification from suggested fixes
                for f in findings_list:
                    sf = f.get("suggested_fix", "")
                    if sf:
                        verification += f"{f.get('file','')}: {sf}\n"

                if not summary:
                    summary = f"FAILED — {len(findings_list)} issue(s) found"

                return summary, details.strip(), _dedup(files), verification.strip()
            except (json.JSONDecodeError, KeyError):
                pass

    # Fallback: extract from review-failed reason
    if fallback_reason.startswith("review-failed:"):
        fallback_reason = fallback_reason.split("review-failed:", 1)[1].strip()

    # Take first sentence as summary, rest as details
    parts = fallback_reason.split(". ", 1)
    summary = parts[0]
    details = parts[1] if len(parts) > 1 else fallback_reason

    # Extract file paths from details
    import re
    files = re.findall(r'(?:backend|frontend|terraform|scripts)/[^\s,;]+', fallback_reason)
    verification = "Tests must pass"

    return summary, details, _dedup(files), verification


def extract_issue_hook(title):
    """Extract issue hook like [DF-1784774204] or [GH-42] from title."""
    import re
    m = re.match(r'(\[[A-Z]+-\d+\])', title)
    return m.group(1) if m else ""


def _dedup(lst):
    """Deduplicate list preserving order."""
    seen = set()
    result = []
    for item in lst:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


if __name__ == "__main__":
    main()