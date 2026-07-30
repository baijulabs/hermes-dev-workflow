#!/usr/bin/env python3
"""
worktree-collision-watch.py — no_agent cron safety net

Detects kanban tasks that failed with "already used by worktree" (Pattern 5b)
and auto-remediates by assigning a unique branch name and resetting to 'todo'.

Designed for no_agent cron mode: exit 0 = nothing to report (silent),
exit 1 = remediation applied (output is the notification).
"""

import sqlite3
import os
import subprocess
import re
import sys

BOARD = "${HERMES_KANBAN_BOARD:-my-project-dev}"
REPO = "${HERMES_PROJECT_DIR:-/home/user/MyProject}"
DB_PATH = os.path.expanduser(f"~/.hermes/kanban/boards/{BOARD}/kanban.db")

if not os.path.exists(DB_PATH):
    sys.exit(0)  # Silent — board may not exist yet

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# Find blocked tasks with branch collision errors
cur.execute("""
    SELECT id, title, branch_name, consecutive_failures,
           coalesce(last_failure_error, '') as error
    FROM tasks
    WHERE status = 'blocked'
      AND assignee = 'coder'
      AND last_failure_error LIKE '%already used by worktree%'
      AND consecutive_failures >= 1
""")

rows = cur.fetchall()
conn.close()

if not rows:
    sys.exit(0)  # Silent — no collisions

remediated = []

for row in rows:
    task_id = row["id"]
    old_branch = row["branch_name"] or "(none)"
    title = row["title"]
    failures = row["consecutive_failures"]

    # Extract GH issue number from title for a unique branch name
    gh_match = re.search(r'GH[_-](\d+)', title)
    gh_part = f"gh-{gh_match.group(1)}" if gh_match else f"auto-{task_id.replace('t_', '')[:8]}"

    # Generate a unique branch: fix/<gh-part>-<short-descriptor>-auto
    # Check if it actually conflicts with an existing worktree before assigning
    new_branch = f"fix/{gh_part}-collision-auto"

    # Open a fresh connection per task
    conn2 = sqlite3.connect(DB_PATH)
    cur2 = conn2.cursor()

    # Check if the new branch is truly unused (both in git and DB)
    wt_check = subprocess.run(
        ["git", "worktree", "list"],
        capture_output=True, text=True, cwd=REPO,
    )
    branch_used = False
    for line in wt_check.stdout.splitlines():
        parts = line.strip().split()
        if len(parts) >= 3:
            wt_branch = parts[2].strip("[]")
            if wt_branch == new_branch:
                branch_used = True
                break

    if not branch_used:
        cur2.execute("""
            SELECT COUNT(*) FROM tasks
            WHERE branch_name = ? AND status NOT IN ('archived', 'done')
        """, (new_branch,))
        if cur2.fetchone()[0] > 0:
            branch_used = True

    if branch_used:
        # Fallback: append a timestamp suffix
        import time
        new_branch = f"fix/{gh_part}-collision-{int(time.time())}"

    # Apply the fix
    cur2.execute("""
        UPDATE tasks
        SET branch_name = ?,
            status = 'todo',
            consecutive_failures = 0,
            last_failure_error = NULL
        WHERE id = ?
    """, (new_branch, task_id))
    conn2.commit()
    conn2.close()

    remediated.append({
        "id": task_id,
        "title": title[:60],
        "old_branch": old_branch,
        "new_branch": new_branch,
        "failures": failures,
    })

# Output notification (used as no_agent delivery text — print only, exit 0)
# In no_agent mode: empty stdout = silent, non-empty stdout = delivered message.
# Non-zero exit = error alert (not what we want). Always exit 0.
if remediated:
    print(f"⚡ Auto-remediated {len(remediated)} worktree collision(s):")
    for r in remediated:
        print(f"  • {r['id']}: {r['title']}")
        print(f"    Branch: {r['old_branch']} → {r['new_branch']}")
        print(f"    Failures: {r['failures']}, reset to todo")
sys.exit(0)  # Always 0 — empty stdout = silent, content = notification