#!/usr/bin/env python3
"""
worktree-collision-watch.py — no_agent cron safety net (Pattern 5b auto-remediation)

Detects kanban tasks that failed with "already used by worktree" and
auto-remediates by assigning a unique branch name and resetting to 'todo'.

Designed for no_agent cron mode: empty stdout = silent (no collisions),
non-empty stdout = notification (remediation applied).
Always exits 0 — non-zero exit would trigger an error alert.
"""

import sqlite3, os, subprocess, re, sys, time

BOARD = "${HERMES_KANBAN_BOARD:-main-dev}"
REPO = "${HERMES_PROJECT_DIR:-/home/user/project}"
DB_PATH = os.path.expanduser(f"~/.hermes/kanban/boards/{BOARD}/kanban.db")

if not os.path.exists(DB_PATH):
    sys.exit(0)

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

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
    sys.exit(0)

remediated = []

for row in rows:
    task_id = row["id"]
    old_branch = row["branch_name"] or "(none)"
    title = row["title"]
    failures = row["consecutive_failures"]

    gh_match = re.search(r'GH[_-](\d+)', title)
    gh_part = f"gh-{gh_match.group(1)}" if gh_match else f"auto-{task_id.replace('t_', '')[:8]}"

    new_branch = f"fix/{gh_part}-collision-auto"

    # Check both git worktrees and DB for uniqueness
    wt_check = subprocess.run(
        ["git", "worktree", "list"],
        capture_output=True, text=True, cwd=REPO,
    )
    branch_used = False
    for line in wt_check.stdout.splitlines():
        parts = line.strip().split()
        if len(parts) >= 3 and parts[2].strip("[]") == new_branch:
            branch_used = True
            break

    if not branch_used:
        conn2 = sqlite3.connect(DB_PATH)
        cur2 = conn2.cursor()
        cur2.execute("SELECT COUNT(*) FROM tasks WHERE branch_name = ? AND status NOT IN ('archived', 'done')", (new_branch,))
        if cur2.fetchone()[0] > 0:
            branch_used = True
        conn2.close()

    if branch_used:
        new_branch = f"fix/{gh_part}-collision-{int(time.time())}"

    conn2 = sqlite3.connect(DB_PATH)
    cur2 = conn2.cursor()
    cur2.execute("""
        UPDATE tasks SET branch_name = ?, status = 'todo',
            consecutive_failures = 0, last_failure_error = NULL
        WHERE id = ?
    """, (new_branch, task_id))
    conn2.commit()
    conn2.close()

    remediated.append({"id": task_id, "title": title[:60],
        "old_branch": old_branch, "new_branch": new_branch, "failures": failures})

if remediated:
    print(f"Auto-remediated {len(remediated)} worktree collision(s):")
    for r in remediated:
        print(f"  {r['id']}: {r['title']}")
        print(f"    Branch: {r['old_branch']} -> {r['new_branch']}")
sys.exit(0)