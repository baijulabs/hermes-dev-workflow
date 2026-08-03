#!/usr/bin/env python3
"""
active-pr-guard-watch.py — no_agent cron safety net

Detects cards that are stuck in 'ready' with the 'active_pr' respawn guard
for 5+ consecutive ticks. These cards have already completed their work
(PR exists). The guard prevents re-spawning a duplicate worker, but the
dispatcher keeps logging "stuck" warnings because the card stays in 'ready'.

Fix: move these cards to 'triage' so the orchestrator handles PR consolidation
and the dispatcher stops logging warnings.

In no_agent mode: empty stdout = silent, non-empty stdout = delivered message.
"""

import sqlite3
import os
import sys
import time

BOARD = "${HERMES_KANBAN_BOARD:-$HERMES_KANBAN_BOARD}"
DB_PATH = os.path.expanduser(f"~/.hermes/kanban/boards/{BOARD}/kanban.db")

if not os.path.exists(DB_PATH):
    sys.exit(0)

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# Find cards in 'ready' status that have recent 'respawn_guarded' events
# with reason 'active_pr'. Count consecutive occurrences.
# A card is stuck if it has 5+ consecutive 'respawn_guarded' events
# without any intervening 'claimed' or 'spawned' event.
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

    # Get the most recent events for this card, newest first
    cur.execute("""
        SELECT kind, created_at
        FROM task_events
        WHERE task_id = ?
        ORDER BY created_at DESC
        LIMIT 20
    """, (task_id,))

    events = cur.fetchall()

    # Count consecutive 'respawn_guarded' events from newest
    guard_count = 0
    has_recent_claim = False
    for e in events:
        if e["kind"] == "respawn_guarded":
            guard_count += 1
        elif e["kind"] in ("claimed", "spawned", "completed"):
            has_recent_claim = True
            break
        else:
            # Non-guard event breaks the chain
            if guard_count > 0:
                break

    # Stuck if: 5+ consecutive guards, NO recent claim/spawn
    if guard_count >= 5 and not has_recent_claim:
        # Move to triage
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
    print(f"🛡 Moved {len(remediated)} active-pr-guarded card(s) to triage:")
    for r in remediated:
        print(f"  • {r['id']}: {r['title']} ({r['guards']} consecutive guards)")
sys.exit(0)