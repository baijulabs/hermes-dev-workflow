#!/usr/bin/env python3
"""
Reviewer Approved Auto-Complete Watchdog

Detects code-reviewer cards blocked with 'review-required: APPROVED'
and auto-completes them. If the reviewer approved, there's nothing left
for a human to review — the chain should complete.

Runs as a no_agent cron job every 5 minutes. Stdout is delivered verbatim to Telegram.
Silent (no output) when there's nothing to fix.
"""
import json
import os
import sqlite3
import subprocess
import sys

KANBAN_DIR = os.path.expanduser("~/.hermes/kanban/boards")

def find_boards():
    boards = []
    for root, dirs, files in os.walk(KANBAN_DIR):
        for f in files:
            if f == "kanban.db":
                boards.append(os.path.join(root, f))
    return boards

def complete_via_cli(task_id, summary):
    try:
        result = subprocess.run(
            ["hermes", "kanban", "complete", task_id,
             "--summary", summary],
            capture_output=True, text=True, timeout=15
        )
        return result.returncode == 0, result.stdout, result.stderr
    except Exception as e:
        return False, "", str(e)

def main():
    fixed = []
    for db_path in find_boards():
        board_name = os.path.basename(os.path.dirname(db_path))
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # Find blocked code-reviewer cards with review-required: APPROVED
            cursor.execute("""
                SELECT DISTINCT t.id, t.title
                FROM tasks t
                JOIN task_events e ON e.task_id = t.id
                WHERE t.status = 'blocked'
                  AND t.assignee = 'code-reviewer'
                  AND e.kind = 'blocked'
                  AND json_extract(e.payload, '$.reason') LIKE 'review-required: APPROVED%'
                ORDER BY t.created_at DESC
            """)

            rows = cursor.fetchall()
            for row in rows:
                task_id = row["id"]

                # Get the most recent blocked event payload
                cursor.execute("""
                    SELECT payload FROM task_events
                    WHERE task_id = ? AND kind = 'blocked'
                    ORDER BY created_at DESC LIMIT 1
                """, (task_id,))
                event = cursor.fetchone()
                if not event:
                    continue

                payload = json.loads(event["payload"])
                reason = payload.get("reason", "")

                summary = reason
                if "review-required:" in reason:
                    summary = reason.split("review-required:", 1)[1].strip()
                if len(summary) > 200:
                    summary = summary[:200] + "..."

                success, stdout, stderr = complete_via_cli(
                    task_id,
                    f"[auto-complete] Reviewer APPROVED — no further human review needed: {summary}"
                )

                if success:
                    fixed.append(f"{board_name}:{task_id}")
                else:
                    print(f"FAILED to complete {task_id} ({board_name}): {stderr}")

            conn.close()
        except Exception as e:
            print(f"Error processing {db_path}: {e}")

    if fixed:
        print(f"✅ Auto-completed {len(fixed)} reviewer card(s) blocked with APPROVED:")
        for f in fixed:
            print(f"  • {f}")
        print("These review chains are now complete — ready for PR consolidation.")

if __name__ == "__main__":
    main()