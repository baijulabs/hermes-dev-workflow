#!/usr/bin/env python3
"""
worktree-merge-audit.py — Continuous worktree-to-main merge audit.

Prevents divergent fixes from being orphaned on worktree branches.
Scans ALL origin/wt/* and local wt/* branches for unique commits vs main
that have no open PR, then auto-pushes + creates a PR (or flags for triage).

This is git-state based, NOT kanban-state based — it catches branches that
the pr-consolidation-watch misses (archived cards, direct worktree deploys,
branches never registered on a kanban card).

Runs as a no_agent cron job every 10 minutes. Silent when nothing to do.
"""
import os
import re
import subprocess
import sys
import time
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "lib"))
from report_utils import should_report

REPO_DIR = "/home/julianbeggs/Liberkyma"
REPO = "baijulabs/Liberkyma"
KANBAN_DB = os.path.expanduser("~/.hermes/kanban/boards/liberkyma-dev/kanban.db")
MAX_PER_RUN = 5  # avoid hammering the API

def run(cmd, cwd=REPO_DIR, timeout=60):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "timed out"
    except Exception as e:
        return -1, "", str(e)

def main():
    # Fetch latest main + all branches
    run(["git", "fetch", "origin", "--prune"], timeout=60)

    # Collect all worktree branches (origin + local)
    branches = set()
    rc, out, _ = run(["git", "branch", "-r", "--format=%(refname:short)"])
    for b in out.splitlines():
        b = b.strip()
        if b.startswith("origin/wt/") and b != "origin/HEAD":
            branches.add(b[len("origin/"):])
    rc, out, _ = run(["git", "branch", "--format=%(refname:short)"])
    for b in out.splitlines():
        b = b.strip()
        if b.startswith("wt/"):
            branches.add(b)

    results = []  # (branch, count, local_only)
    for branch in sorted(branches):
        # Count unique commits vs main
        rc, out, _ = run(["git", "rev-list", "--count", f"origin/main..{branch}"])
        if rc != 0 or not out.strip():
            continue
        count = int(out.strip())
        if count == 0:
            continue  # already merged

        # Check if branch tip is an ancestor of main (content merged via other path)
        rc, out, _ = run(["git", "rev-parse", "--verify", f"origin/{branch}"])
        ref = f"origin/{branch}" if rc == 0 else branch
        rc, _, _ = run(["git", "merge-base", "--is-ancestor", f"{ref}", "origin/main"], timeout=10)
        if rc == 0:
            continue  # already in main via consolidation

        # Check for existing PR (any state)
        rc, out, _ = run(["gh", "pr", "list", "--state", "all", "--head", branch,
                          "--json", "number", "--jq", "length"], timeout=20)
        if rc == 0 and out.strip() != "0":
            continue  # PR exists (open/closed/merged)

        # Skip if kanban shows this is a done coder awaiting consolidation
        m = re.match(r'^wt/t_(t_.+)$', branch)
        if m:
            task_id = m.group(1)
            try:
                import sqlite3
                conn = sqlite3.connect(KANBAN_DB)
                cur = conn.cursor()
                # Check: coder card done AND linked reviewer done
                cur.execute("""
                    SELECT 1 FROM tasks c
                    JOIN task_links l ON l.parent_id = c.id
                    JOIN tasks r ON r.id = l.child_id AND r.assignee = 'code-reviewer'
                    WHERE c.id = ? AND c.status IN ('done', 'archived')
                      AND r.status IN ('done', 'archived')
                    LIMIT 1
                """, (task_id,))
                if cur.fetchone():
                    conn.close()
                    continue  # awaiting PR consolidation, not stranded
                conn.close()
            except Exception:
                pass  # if DB is unreachable, fall through to flagging

        # Determine local-only
        rc, _, _ = run(["git", "rev-parse", "--verify", f"origin/{branch}"])
        local_only = rc != 0

        results.append((branch, count, local_only))

    if not results:
        return  # silent — nothing stranded

    # Process up to MAX_PER_RUN (most commits first)
    results.sort(key=lambda x: x[1], reverse=True)
    created = 0
    for branch, count, local_only in results[:MAX_PER_RUN]:
        # Build commit list for issue body
        rc, out, _ = run(["git", "log", "--oneline", f"origin/main..origin/{branch}",
                          "--format=%s", "--reverse"])
        commits = [c for c in out.splitlines() if c.strip()]

        # Build issue body
        body = f"## Stranded Worktree: `{branch}`\n\n"
        body += f"**{count} unique commits** behind main.\n"
        if local_only:
            body += "**Not on origin** — local only.\n"
        body += "\n### Commits (first 30)\n"
        for c in commits[:30]:
            body += f"- {c[:80]}\n"
        if len(commits) > 30:
            body += f"- ... and {len(commits) - 30} more commits\n"

        # Resolve GH issues referenced in commits
        gh_issues = set()
        for c in commits:
            found = re.findall(r'\[GH-(\d+)\]', c)
            gh_issues.update(found)
        if gh_issues:
            body += "\n### Related Issues\n"
            for i in sorted(gh_issues):
                body += f"- #{i}\n"

        body += "\n### Action\nInvestigate whether these commits need to be merged to main. "
        body += "If yes, create a new kanban fix card or consolidation PR. "
        body += "If stale/duplicate, close this issue."

        title = f"stale-fix: stranded worktree `{branch}` ({count} commits)"

        # Dedup: skip if an open triage issue already exists for this branch
        rc, out, _ = run(["gh", "issue", "list",
                          "--repo", REPO,
                          "--label", "triage",
                          "--state", "open",
                          "--search", f'"{branch}" in:title',
                          "--json", "number",
                          "--jq", "length"], timeout=20)
        if rc == 0 and out.strip() and int(out.strip()) > 0:
            print(f"⏭️  Issue already exists for {branch} — skipping")
            continue

        rc, out, err = run(["gh", "issue", "create",
                            "--repo", REPO,
                            "--title", title,
                            "--label", "triage",
                            "--body", body], timeout=60)
        if rc == 0:
            created += 1
            url = out.strip()
            print(f"📝 Flagged stranded fix {branch} ({count} commits): {url[:80]}")
        else:
            print(f"❌ Failed to create issue for {branch}: {err[:100]}")

    if created:
        print(f"\nFlagged {created} stranded worktree fix(es) as GH issues for triage.")
    elif not should_report("audit-stranded-worktrees", json.dumps({"created": created, "skipped": [l for l in locals().get('_output_lines', [])]}, sort_keys=True)):
        # Suppress duplicate "nothing new" reports
        pass

if __name__ == "__main__":
    main()