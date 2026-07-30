#!/usr/bin/env python3
"""
active-pr-guard-watch.py — no_agent cron safety net

Detects cards stuck in 'ready' with the 'active_pr' respawn guard
for 5+ consecutive ticks. These cards have already completed their work
(PR exists). Moves them to 'triage' so the orchestrator handles PR
consolidation and the dispatcher stops logging "stuck" warnings.

In no_agent mode: empty stdout = silent, non-empty stdout = notification.
"""

import sqlite3
import os
import sys

BOARD = "${HERMES_KANBAN_BOARD:-main-dev}"
DB_PATH = os.path.expanduser(f"~/.hermes/kanban/boards/{BOARD}/kanban.db")

if not os.path.exists(DB_PATH):
    sys.exit(0)

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

cur.execute("""
    SELECT id, title, assignee
    FROM tasks
    WHERE status = 'ready'
      AND assignee IN ('coder', 'orchestrator')
    ORDER BY created_at
""")

candidates = cur.fetchall()
remediated = []

for card in candidates:
    task_id = card["id"]

    cur.execute("""
        SELECT kind, created_at
        FROM task_events
        WHERE task_id = ?
        ORDER BY created_at DESC
        LIMIT 20
    """, (task_id,))

    events = cur.fetchall()

    guard_count = 0
    has_recent_claim = False
    for e in events:
        if e["kind"] == "respawn_guarded":
            guard_count += 1
        elif e["kind"] in ("claimed", "spawned", "completed"):
            has_recent_claim = True
            break
        else:
            if guard_count > 0:
                break

    if guard_count >= 5 and not has_recent_claim:
        cur.execute("""
            UPDATE tasks
            SET status = 'triage'
            WHERE id = ? AND status = 'ready'
        """, (task_id,))

        if cur.rowcount > 0:
            remediated.append({
                "id": task_id,
                "title": card["title"][:60],
                "guards": guard_count,
            })

conn.commit()
conn.close()

if remediated:
    print(f"Moved {len(remediated)} active-pr-guarded card(s) to triage:")
    for r in remediated:
        print(f"  - {r['id']}: {r['title']} ({r['guards']} consecutive guards)")
sys.exit(0)