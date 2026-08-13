#!/usr/bin/env python3
"""
PR Consolidation Watchdog v3 — with GH-issue consolidation.

Finds done coder+reviewer pairs and creates PRs.
KEY CHANGE: if multiple pairs share the same [GH-N], their branches
are merged into a SINGLE consolidation PR instead of N individual ones.

Runs as a no_agent cron job every 10 minutes. Silent when nothing to do.
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from collections import defaultdict

KANBAN_DIR = os.path.expanduser("~/.hermes/kanban/boards")
REPO_DIR = "/home/julianbeggs/Liberkyma"
REPO = "baijulabs/Liberkyma"
MAX_CONSOLIDATE_PER_RUN = 2  # max consolidation PRs per tick
MAX_INDIVIDUAL_PER_RUN = 3   # max individual PRs per tick

def gh_issue_is_open(issue_num):
    result = subprocess.run(
        ["gh", "issue", "view", str(issue_num), "--repo", REPO,
         "--json", "state", "--jq", ".state"],
        capture_output=True, text=True, timeout=15,
    )
    return result.stdout.strip() == "OPEN"

def gh_issue_comment(issue_num, body):
    result = subprocess.run(
        ["gh", "issue", "comment", str(issue_num),
         "--repo", REPO, "--body", body],
        capture_output=True, text=True, timeout=30,
    )
    return result.returncode == 0

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
    rc, out, _ = run(["git", "rev-list", "--reverse", f"origin/main..origin/{branch}"])
    if rc == 0 and out:
        return [h for h in out.strip().split('\n') if h.strip()]
    rc, out, _ = run(["git", "rev-list", "--reverse", f"origin/main..{branch}"])
    if rc == 0 and out:
        return [h for h in out.strip().split('\n') if h.strip()]
    return []

def is_already_in_main(hashes, branch):
    if not hashes:
        return True
    rc, out, _ = run(["git", "branch", "-r", "--list", f"origin/{branch}"])
    if not out.strip():
        rc2, out2, _ = run(["git", "rev-parse", "--verify", f"refs/heads/{branch}"])
        if rc2 != 0 or not out2.strip():
            return False
        rc3, _, _ = run(["git", "merge-base", "--is-ancestor", hashes[-1], "origin/main"], timeout=10)
        return rc3 == 0
    rc, _, _ = run(["git", "merge-base", "--is-ancestor", hashes[-1], "origin/main"], timeout=10)
    return rc == 0

def already_has_pr(branch):
    rc, out, _ = run(["gh", "pr", "list", "--state", "all", "--head", branch, "--json", "number", "--jq", "length"])
    if rc == 0 and out.strip() == "0":
        return False
    return True

def get_branch_commit_count(branch):
    rc, out, _ = run(["git", "rev-list", "--count", f"origin/main..origin/{branch}"])
    if rc == 0 and out.strip():
        return int(out.strip())
    rc, out, _ = run(["git", "rev-list", "--count", f"origin/main..{branch}"])
    if rc == 0 and out.strip():
        return int(out.strip())
    return -1

def group_cards_by_issue(db_path, cutoff):
    """Query done coder+reviewer pairs and group them by GH issue number."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Get ALL done coder cards with their reviewer siblings
    cursor.execute("""
        SELECT c.id as coder_id, c.title, c.branch_name
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
    """, (cutoff,))
    rows = cursor.fetchall()
    conn.close()

    # Group by GH issue number
    groups = defaultdict(list)
    singles = []

    for row in rows:
        title = row["title"] or ""
        gh_nums = re.findall(r'\[GH-(\d+)\]', title)
        # Use the first GH number found, or "single" if none
        group_key = gh_nums[0] if gh_nums else None

        entry = {"coder_id": row["coder_id"], "title": title, "branch": row["branch_name"]}
        if group_key:
            groups[group_key].append(entry)
        else:
            singles.append(entry)

    # Groups with 2+ cards = consolidation candidates
    to_consolidate = {k: v for k, v in groups.items() if len(v) >= 2}
    # Groups with 1 card + ungrouped singles = individual PRs
    to_individual = []
    for k, v in groups.items():
        if len(v) == 1:
            to_individual.extend(v)
    to_individual.extend(singles)

    return to_consolidate, to_individual

def check_dedup_and_branch(entry, seen_commit_sets, skip_lost=False):
    """Common pre-flight checks for a coder entry. Returns True if OK to process."""
    branch = entry["branch"]
    count = get_branch_commit_count(branch)
    if count == -1:
        if not skip_lost:
            print(f"  ⚠️  Branch {branch} lost — skipping")
        return False, None
    if count == 0:
        return False, None

    hashes = get_commit_hashes(branch)
    if not hashes:
        return False, None

    dedup_key = "|".join(sorted(hashes))
    if dedup_key in seen_commit_sets:
        return False, None

    if is_already_in_main(hashes, branch):
        seen_commit_sets.add(dedup_key)
        return False, None

    if already_has_pr(branch):
        seen_commit_sets.add(dedup_key)
        return False, None

    return True, hashes

def create_individual_pr(entry, seen_commit_sets):
    """Create a single PR for a coder branch (existing behavior)."""
    ok, hashes = check_dedup_and_branch(entry, seen_commit_sets)
    if not ok:
        return 0

    branch = entry["branch"]
    coder_id = entry["coder_id"]
    dedup_key = "|".join(sorted(hashes))
    seen_commit_sets.add(dedup_key)

    # Push branch
    rc, _, _ = run(["git", "push", "origin", branch])
    if rc != 0:
        return 0

    # Create PR
    rc, out, _ = run(["git", "log", "--oneline", f"origin/main..origin/{branch}",
                      "--format=%s", "--reverse"])
    commits = [c for c in out.strip().split('\n') if c.strip()]
    title = commits[0][:72] if commits else f"fix: consolidate {coder_id}"
    body = "Auto-consolidated from kanban coder+reviewer pair.\n## Commits\n"
    for c in commits:
        body += f"  • {c[:80]}\n"

    # Resolve GH issues from title
    gh_issues = list(set(re.findall(r'\[GH-(\d+)\]', entry["title"])))
    if gh_issues:
        body += "\n"
        for issue_num in gh_issues:
            body += f"Closes #{issue_num}\n"

    rc, out, err = run(["gh", "pr", "create", "--base", "main", "--head", branch,
                        "--title", title, "--body", body])
    if rc != 0:
        print(f"❌ Failed to create PR for {branch}: {err[:100]}")
        return 0

    pr_url = out.strip()
    print(f"✅ Created PR for {branch}: {pr_url[:80]}")

    # Post comment on GH issues
    pr_match = re.search(r'/pull/(\d+)', pr_url)
    pr_num = pr_match.group(1) if pr_match else None
    if pr_num and gh_issues:
        for issue_num in gh_issues:
            body = (
                f"📦 **PR #{pr_num} created:** {pr_url}\n"
                f"Merging this PR will close this issue automatically "
                f"(Closes #{issue_num} in PR body)."
            )
            gh_issue_comment(issue_num, body)
            print(f"  📝 Posted PR comment on GH-{issue_num}")
    return 1

def create_consolidated_pr(gh_num, entries, seen_commit_sets):
    """Merge multiple coder branches into ONE consolidation PR for a GH issue."""
    # Pre-flight: check each branch, collect valid ones
    valid_entries = []
    all_hashes = []
    all_branches = []
    for entry in entries:
        ok, hashes = check_dedup_and_branch(entry, seen_commit_sets, skip_lost=True)
        if ok:
            valid_entries.append(entry)
            all_hashes.extend(hashes)
            all_branches.append(entry["branch"])

    if not valid_entries:
        return 0

    print(f"📦 Consolidating GH-{gh_num}: {len(valid_entries)} fix branches → one PR")

    # Mark all hashes as seen
    for entry in valid_entries:
        hashes = get_commit_hashes(entry["branch"])
        if hashes:
            dedup_key = "|".join(sorted(hashes))
            seen_commit_sets.add(dedup_key)

    # Create consolidation branch from latest main
    branch_name = f"fix/consolidate-gh-{gh_num}"

    # DEDUP: check if consolidation branch already exists on origin
    rc, br_out, _ = run(["git", "branch", "-r", "--list", f"origin/{branch_name}"])
    if br_out and br_out.strip():
        # Branch exists — check if it has a PR
        rc2, pr_out, _ = run(["gh", "pr", "list", "--head", branch_name,
                              "--state", "all", "--json", "number,state",
                              "--jq", '.[0].number // empty'])
        if pr_out.strip():
            print(f"  ⏭️  PR already exists for {branch_name} — skipping (cards already consolidated)")
        else:
            print(f"  ⏭️  Branch {branch_name} already on origin (no PR) — force-pushing update")
            # Push force to update the existing branch
            rc3, _, err3 = run(["git", "push", "--force", "-u", "origin", branch_name], timeout=120)
            if rc3 != 0:
                print(f"❌ Failed to force-push {branch_name}: {err3[:100]}")

        # Mark hashes as seen so we don't reprocess
        for entry in valid_entries:
            hashes = get_commit_hashes(entry["branch"])
            if hashes:
                seen_commit_sets.add("|".join(sorted(hashes)))
        run(["git", "checkout", "origin/main"])
        run(["git", "branch", "-D", branch_name])
        return 0 if not pr_out.strip() else 0

    run(["git", "checkout", "origin/main", "-b", branch_name])

    # Merge each valid branch sequentially
    for entry in valid_entries:
        branch = entry["branch"]
        rc, out, err = run(["git", "merge", "--no-edit", "-X", "theirs", branch])
        if rc != 0:
            # Try cherry-pick as fallback
            hashes = get_commit_hashes(branch)
            if hashes:
                for h in hashes:
                    run(["git", "cherry-pick", "--no-edit", "-X", "theirs", h])
        # Check if this branch exists on origin and push if not
        rc2, _, _ = run(["git", "branch", "-r", "--list", f"origin/{branch}"])
        if not rc2 or not rc2 == 0:  # if not on origin, push it
            run(["git", "push", "origin", branch])

    # Check for conflicts in the consolidation branch
    rc, out, _ = run(["git", "diff", "--name-only", "--diff-filter=U"])
    if rc == 0 and out:
        print(f"  ⚠️  Unresolved conflicts in {branch_name}, aborting consolidation")
        run(["git", "merge", "--abort"])
        run(["git", "checkout", "origin/main"])
        run(["git", "branch", "-D", branch_name])
        return 0

    # Check if consolidation branch has any commits vs main
    rc, count_out, _ = run(["git", "rev-list", "--count", f"origin/main..{branch_name}"])
    if rc == 0 and count_out.strip() == "0":
        print(f"  ⏭️  No commits vs main — content already landed. Archiving cards.")
        # Archive the coder cards so consolidation watch doesn't reprocess them
        for entry in valid_entries:
            run(["sqlite3", KANBAN_DIR + "/liberkyma-dev/kanban.db",
                 f"UPDATE tasks SET status='archived' WHERE id='{entry['coder_id']}';"])
        run(["git", "checkout", "origin/main"])
        run(["git", "branch", "-D", branch_name])
        return 0

    # Push consolidation branch
    rc, out, err = run(["git", "push", "-u", "origin", branch_name])
    if rc != 0:
        print(f"❌ Failed to push consolidation branch {branch_name}: {err[:100]}")
        run(["git", "checkout", "origin/main"])
        run(["git", "branch", "-D", branch_name])
        return 0

    # Get commit summary
    rc, out, _ = run(["git", "log", "--oneline", f"origin/main..origin/{branch_name}",
                      "--format=%s", "--reverse"])
    commits = [c for c in out.strip().split('\n') if c.strip()]
    title = f"fix: consolidate GH-{gh_num} fixes"
    body = f"## Consolidated Fixes for GH-{gh_num}\n\n### Branches merged\n"
    for entry in valid_entries:
        body += f"- `{entry['branch']}` — {entry['title'][:80]}\n"
    body += "\n### Commits\n"
    for c in commits:
        body += f"- {c[:80]}\n"
    body += f"\nCloses #{gh_num}\n"

    rc, out, err = run(["gh", "pr", "create", "--base", "main", "--head", branch_name,
                        "--title", title, "--body", body])
    if rc != 0:
        print(f"❌ Failed to create PR for {branch_name}: {err[:100]}")
        return 0

    pr_url = out.strip()
    pr_match = re.search(r'/pull/(\d+)', pr_url)
    pr_num = pr_match.group(1) if pr_match else None
    pr_ref = f" (#{pr_num})" if pr_num else ""
    print(f"✅ Created consolidated PR for GH-{gh_num} ({len(valid_entries)} branches){pr_ref}: {pr_url[:80]}")

    # Also post to individual branches as PR comments on their GH issues
    pr_match = re.search(r'/pull/(\d+)', pr_url)
    pr_num = pr_match.group(1) if pr_match else None
    if pr_num and gh_issue_is_open(gh_num):
        comment = (
            f"📦 **Consolidated PR #{pr_num} created:** {pr_url}\n"
            f"Merges {len(valid_entries)} fix branches into one PR.\n"
            f"Merging this PR will close this issue (Closes #{gh_num} in PR body)."
        )
        gh_issue_comment(gh_num, comment)
        print(f"  📝 Posted PR comment on GH-{gh_num}")

    # Go back to main (or some known-safe head)
    run(["git", "checkout", "origin/main"])
    run(["git", "branch", "-D", branch_name])  # clean up local, remote stays

    return 1

def main():
    created = 0
    seen_commit_sets = set()

    run(["git", "fetch", "--depth=100", "origin", "main"], timeout=30)

    for db_path in find_boards():
        try:
            # Group cards by GH issue
            to_consolidate, to_individual = group_cards_by_issue(db_path, int(time.time()) - 604800)  # 7 days

            # Process consolidation groups first (batch-limited)
            consolidated = 0
            for gh_num, entries in to_consolidate.items():
                if consolidated >= MAX_CONSOLIDATE_PER_RUN:
                    break
                created += create_consolidated_pr(gh_num, entries, seen_commit_sets)
                consolidated += 1

            # Process individual cards (batch-limited)
            individual = 0
            for entry in to_individual:
                if individual >= MAX_INDIVIDUAL_PER_RUN:
                    break
                created += create_individual_pr(entry, seen_commit_sets)
                individual += 1

        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"Error processing {db_path}: {e}")

    if created > 0:
        print(f"\nCreated {created} new PR(s).")

if __name__ == "__main__":
    main()