#!/usr/bin/env python3
"""
PR Consolidation Watchdog v2

Finds done coder+reviewer pairs and creates PRs for genuinely unique fixes.
Deduplicates: same fix spread across 20 worktree branches only gets one PR.
Skips: fixes already in main, branches that already have a PR.

Runs as a no_agent cron job every 10 minutes. Silent when nothing to do.
"""
import json
import os
import sqlite3
import subprocess
import sys
import time

KANBAN_DIR = os.path.expanduser("~/.hermes/kanban/boards")
REPO_DIR = "${HERMES_PROJECT_DIR:-$HERMES_PROJECT_DIR}"

def run(cmd, cwd=REPO_DIR, timeout=60):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "timed out"
    except Exception as e:
        return -1, "", str(e)

def find_boards():
    boards = []
    for root, dirs, files in os.walk(KANBAN_DIR):
        for f in files:
            if f == "kanban.db":
                boards.append(os.path.join(root, f))
    return boards

def get_commit_hashes(branch):
    """Get a fingerprint of the fix commits (not merge commits or infrastructure)."""
    rc, out, _ = run(["git", "rev-list", "--reverse", f"origin/main..origin/{branch}"])
    if rc == 0 and out:
        return [h for h in out.strip().split('\n') if h.strip()]
    # Try local branch
    rc, out, _ = run(["git", "rev-list", "--reverse", f"origin/main..{branch}"])
    if rc == 0 and out:
        return [h for h in out.strip().split('\n') if h.strip()]
    return []

def is_already_in_main(hashes, branch):
    """Check if the branch's fix is already in main by verifying ancestry."""
    if not hashes:
        return True
    # Check if the branch is on origin
    rc, out, _ = run(["git", "branch", "-r", "--list", f"origin/{branch}"])
    if not out.strip():
        return True  # branch not on origin — can't check
    # Check if the branch tip is an ancestor of main (content already merged)
    rc, _, _ = run(["git", "merge-base", "--is-ancestor", hashes[-1], "origin/main"], timeout=10)
    if rc == 0:
        return True  # commit is an ancestor of main = already merged
    return False

def already_has_pr(branch):
    """Check if a PR already exists (open, closed, or merged) for this branch."""
    rc, out, _ = run(["gh", "pr", "list", "--state", "all", "--head", branch, "--json", "number", "--jq", "length"])
    if rc == 0 and out.strip() == "0":
        return False
    return True  # error or non-zero means PR exists

def get_branch_commit_count(branch):
    """Count unique commits vs main."""
    rc, out, _ = run(["git", "rev-list", "--count", f"origin/main..origin/{branch}"])
    if rc == 0 and out.strip():
        return int(out.strip())
    rc, out, _ = run(["git", "rev-list", "--count", f"origin/main..{branch}"])
    if rc == 0 and out.strip():
        return int(out.strip())
    return 0

def main():
    created = 0
    seen_commit_sets = set()
    
    # Fetch latest main first
    run(["git", "fetch", "origin", "main"], timeout=15)
    
    for db_path in find_boards():
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            cutoff = int(time.time()) - 86400
            cursor.execute("""
                SELECT DISTINCT c.id, c.title, c.branch_name
                FROM tasks c
                JOIN task_links l ON l.parent_id = c.id
                JOIN tasks r ON r.id = l.child_id AND r.assignee = 'code-reviewer'
                WHERE c.status IN ('done', 'archived')
                  AND c.assignee = 'coder'
                  AND c.branch_name IS NOT NULL
                  AND c.branch_name != ''
                  AND r.status IN ('done', 'archived')
                  AND c.completed_at > ?
                ORDER BY c.completed_at DESC
                LIMIT 10
            """, (cutoff,))
            
            rows = cursor.fetchall()
            for row in rows:
                coder_id = row["id"]
                branch = row["branch_name"]
                
                # 1. Check branch has commits vs main
                count = get_branch_commit_count(branch)
                if count == 0:
                    continue  # already on main
                
                # 2. Get commit hashes for dedup
                hashes = get_commit_hashes(branch)
                if not hashes:
                    continue
                
                # Use a string of sorted hashes as the dedup key
                dedup_key = "|".join(sorted(hashes))
                if dedup_key in seen_commit_sets:
                    continue  # same fix already PR'd
                
                # 3. Check if commits are already in main
                if is_already_in_main(hashes, branch):
                    seen_commit_sets.add(dedup_key)
                    continue  # already in main
                
                # 4. Check if a PR already exists for this branch
                if already_has_pr(branch):
                    seen_commit_sets.add(dedup_key)
                    continue  # PR already exists
                
                # 5. Auto-bump version on the branch before pushing
                # Use local branch ref (not origin/) since worktree branches may be local-only
                rc, out, _ = run(["git", "log", "--oneline", f"origin/main..{branch}", "--format=%s"])
                bump_level = "minor" if any(c.startswith(("feat", "feature")) for c in out.splitlines()) else "patch"
                
                # Find the existing worktree for this branch (coders leave them checked out).
                # Creating a new worktree fails if the branch is already checked out elsewhere.
                wt_dir = None
                rc, out, _ = run(["git", "worktree", "list", "--porcelain"])
                for line in out.split('\n'):
                    if line.startswith('worktree '):
                        wt_dir = line.split(' ', 1)[1]
                    elif line.startswith('branch ') and line.split(' ', 2)[-1].startswith('refs/heads/'):
                        if line.split('refs/heads/')[1] == branch:
                            break  # found existing worktree
                    else:
                        wt_dir = None  # next block
                else:
                    wt_dir = None  # not found
                
                if wt_dir and os.path.isdir(wt_dir):
                    # Use existing coder worktree
                    pass
                else:
                    # Try creating a temp worktree
                    wt_dir = f"/tmp/wt_bump_{coder_id}"
                    rc, _, _ = run(["git", "worktree", "add", "--force", wt_dir, branch])
                    if rc != 0:
                        # Can't create worktree — skip version bump, still create PR
                        wt_dir = None
                
                if wt_dir:
                    try:
                        rc, _, _ = run(["bash", "scripts/sync-version.sh", "--bump", bump_level], cwd=wt_dir)
                        if rc == 0:
                            rc, new_ver, _ = run(["grep", "^version", "backend/pyproject.toml"], cwd=wt_dir)
                            new_ver_str = new_ver.split('"')[1] if '"' in new_ver else "unknown"
                            run(["git", "-C", wt_dir, "add", "backend/pyproject.toml", "frontend/package.json", "package.json"])
                            run(["git", "-C", wt_dir, "commit", "--allow-empty", "-m", f"chore: bump version to {new_ver_str}"])
                    finally:
                        if wt_dir.startswith("/tmp/wt_bump_"):
                            run(["git", "worktree", "remove", "--force", wt_dir])
                
                # 6. Ensure branch is on origin
                rc, out, _ = run(["git", "push", "origin", branch])
                if rc != 0:
                    continue  # failed to push
                
                # 7. Create PR
                rc, out, _ = run(["git", "log", "--oneline", f"origin/main..origin/{branch}",
                                  "--format=%s", "--reverse"])
                commits = [c for c in out.strip().split('\n') if c.strip()]
                title = commits[0][:72] if commits else f"fix: consolidate {coder_id}"
                body = "Auto-consolidated from kanban coder+reviewer pair.\n## Commits\n"
                for c in commits:
                    body += f"  • {c[:80]}\n"
                
                rc, out, err = run(["gh", "pr", "create",
                                    "--base", "main", "--head", branch,
                                    "--title", title, "--body", body])
                if rc == 0:
                    created += 1
                    seen_commit_sets.add(dedup_key)
                    print(f"✅ Created PR for {branch}: {out.strip()[:80]}")
                else:
                    print(f"❌ Failed to create PR for {branch}: {err[:100]}")
            
            conn.close()
        except Exception as e:
            print(f"Error processing {db_path}: {e}")
    
    if created > 0:
        print(f"\nCreated {created} new PR(s).")
    # Silent when nothing to do — no-op is normal

if __name__ == "__main__":
    main()