#!/usr/bin/env python3
"""
archive-cancelled-watch.py — Auto-archive cancelled kanban tasks.

Prevents cancelled cards from accumulating in the Todo column
on the dashboard. Runs as no_agent cron. Silent (empty stdout)
when nothing to archive.
"""
import sqlite3
import os
import sys

BOARD = "${HERMES_KANBAN_BOARD:-project-dev}"
DB_PATH = os.path.expanduser(f"~/.hermes/kanban/boards/{BOARD}/kanban.db")

if not os.path.exists(DB_PATH):
    sys.exit(0)

conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()
cur.execute("SELECT id, title FROM tasks WHERE status = 'cancelled'")
rows = cur.fetchall()

if not rows:
    conn.close()
    sys.exit(0)

ids = [r[0] for r in rows]
placeholders = ",".join("?" for _ in ids)
cur.execute(f"UPDATE tasks SET status = 'archived' WHERE id IN ({placeholders})", ids)
conn.commit()
conn.close()

print(f"Archived {len(rows)} cancelled task(s):")
for tid, title in rows:
    print(f"  • {tid}: {title[:70]}")
sys.exit(0)