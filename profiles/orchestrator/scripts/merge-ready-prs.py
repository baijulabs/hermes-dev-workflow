#!/usr/bin/env python3
"""
merge-ready-watch.py — Auto-merge open PRs + centralized version bump.

1. Scan open PRs for MERGEABLE, up-to-date candidates
2. Merge them via squash --auto
3. After all merges: bump version once in a temp worktree, push to main

Single version bump avoids the per-PR bump conflicts that created noise
and cascading merge conflicts on parallel PRs.

Runs as no_agent cron every 5 min. Telegram summary on activity.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

REPO = os.environ.get("HERMES_PROJECT_REPO", "owner/project")
REPO_DIR = os.environ.get("HERMES_PROJECT_DIR", str(Path.home() / "project"))
MAX_MERGES_PER_RUN = 3
MERGE_STAGGER = True
DEPLOY_START_TIMEOUT = 90     # s to wait for a deploy run to appear after a merge
DEPLOY_DONE_TIMEOUT = 420     # s to wait for that deploy to finish
MIN_STAGGER_SECONDS = 60      # floor between merges even if no deploy observed
STAGGER_BUDGET_SECONDS = 600  # total per-tick stagger wait ceiling (cron interval is 10m)

def run(cmd, timeout=60, cwd=REPO_DIR):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "timed out"
    except Exception as e:
        return -1, "", str(e)

def do_version_bump(bump_level):
    """Bump version in a temp worktree, commit, and push to main."""
    run(["git", "fetch", "origin", "main"], timeout=30)
    
    rc, current_ver, _ = run(["grep", "^version", "backend/pyproject.toml"])
    current_ver = current_ver.split('"')[1] if '"' in current_ver else "0.0.0"
    
    wt_dir = tempfile.mkdtemp(prefix="wt_bump_")
    try:
        # Create temp worktree from origin/main
        rc, _, err = run(["git", "worktree", "add", "--detach", wt_dir, "origin/main"], timeout=30)
        if rc != 0:
            return current_ver, f"worktree failed: {err[:80]}"
        
        # Bump version inside worktree
        rc, _, err = run(["bash", "scripts/sync-version.sh", "--bump", bump_level], cwd=wt_dir)
        if rc != 0:
            return current_ver, f"sync-version failed: {err[:80]}"
        
        rc, new_ver_raw, _ = run(["grep", "^version", "backend/pyproject.toml"], cwd=wt_dir)
        new_ver = new_ver_raw.split('"')[1] if '"' in new_ver_raw else current_ver
        
        if new_ver == current_ver:
            return current_ver, "noop"
        
        # Commit and push from worktree
        run(["git", "-C", wt_dir, "add",
             "backend/pyproject.toml", "frontend/package.json", "package.json"])
        rc, _, err = run(["git", "-C", wt_dir, "commit", "--allow-empty",
                          "-m", f"chore: bump version to {new_ver}"])
        if rc != 0:
            return current_ver, f"commit failed: {err[:80]}"
        
        # Push from worktree: rebase onto latest main first in case of race
        run(["git", "-C", wt_dir, "fetch", "origin", "main"], timeout=15)
        rc, _, err = run(["git", "-C", wt_dir, "rebase", "origin/main"], timeout=15)
        if rc != 0:
            run(["git", "-C", wt_dir, "rebase", "--abort"])
            # Can't rebase — someone else may have bumped. Accept and move on.
            return new_ver, "rebase failed (likely concurrent bump)"
        
        rc, _, err = run(["git", "-C", wt_dir, "push", "origin", "HEAD:main"], timeout=60)
        if rc != 0:
            return current_ver, f"push failed: {err[:80]}"
        
        return new_ver, "ok"
    finally:
        run(["git", "worktree", "remove", "--force", wt_dir])

def deploys_in_progress():
    rc, n, _ = run(["gh", "run", "list", "--workflow", "deploy.yml",
                    "--branch", "main", "--status", "in_progress",
                    "--json", "databaseId", "--jq", "length"], timeout=15)
    return rc == 0 and n.strip().isdigit() and int(n) > 0


def wait_for_deploy_settle(log, max_wait=None):
    """Stagger merges so deploys don't collide on the Terraform state lock.

    Same-minute merges trigger same-minute deploy runs, which stampede the
    GCS state lock (412 conditionNotMet) and leave staging in limbo. After a
    merge: wait for the deploy to start, then for it to finish. If none
    appears in DEPLOY_START_TIMEOUT, fall back to a MIN_STAGGER_SECONDS pause.
    max_wait caps the total wait so a tick can't run unbounded past its
    cron interval.
    """
    if max_wait is not None and max_wait <= 0:
        return
    start = time.time()
    start_timeout = DEPLOY_START_TIMEOUT
    if max_wait is not None:
        start_timeout = min(start_timeout, max_wait)
    while time.time() - start < start_timeout:
        if deploys_in_progress():
            break
        time.sleep(10)
    else:
        log(f"⏳ no deploy observed after merge; min stagger {MIN_STAGGER_SECONDS}s")
        pause = MIN_STAGGER_SECONDS
        if max_wait is not None:
            pause = min(pause, max(max_wait - (time.time() - start), 0))
        if pause:
            time.sleep(pause)
        return
    started = time.time()
    done_timeout = DEPLOY_DONE_TIMEOUT
    if max_wait is not None:
        done_timeout = min(done_timeout, max(max_wait - (time.time() - start), 10))
    while time.time() - started < done_timeout:
        if not deploys_in_progress():
            log(f"✅ deploy finished in {int(time.time() - started)}s; proceeding")
            return
        time.sleep(20)
    log(f"⚠️ deploy still running after {int(done_timeout)}s — proceeding cautiously")


def main():
    # Tick lock: stagger waits can make a tick run well past the 5-minute
    # cron interval. Without this, the next tick would merge more PRs while
    # this one is still waiting for its deploy — the exact stampede the
    # stagger exists to prevent.
    lock = open(tempfile.gettempdir() + "/merge-ready-prs.lock", "w")
    try:
        import fcntl
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return  # another tick is already merging — do nothing

    run(["git", "fetch", "origin", "main"], timeout=30)

    # Deploy cooldown: if a deploy workflow is currently running on main,
    # skip this tick entirely. Merging more PRs before the current deploy
    # finishes creates overlapping deploy runs, cascading CI failures that
    # spawn fix cards that create more PRs → more deploys.
    rc, running, _ = run(["gh", "run", "list", "--workflow", "deploy.yml",
                          "--branch", "main", "--status", "in_progress",
                          "--json", "databaseId", "--jq", "length"], timeout=15)
    if rc == 0 and running.strip().isdigit() and int(running) > 0:
        return  # deploy in progress — wait for next tick

    rc, out, _ = run(["gh", "pr", "list", "--state", "open", "--repo", REPO,
                      "--json", "number,title,headRefName,mergeable,mergeStateStatus,baseRefName",
                      "--jq", r'.[] | "\(.number)|\(.headRefName)|\(.mergeable)|\(.mergeStateStatus)|\(.baseRefName)"'],
                     timeout=30)
    if rc != 0 or not out:
        return

    merged = []
    skipped = []
    candidates = []
    bump_level = "patch"

    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) < 5:
            continue
        pr_num, head_ref, mergeable, merge_status, base_ref = parts

        if head_ref.startswith("dependabot/"):
            continue

        if mergeable != "MERGEABLE":
            if merge_status == "DIRTY" or mergeable == "CONFLICTING":
                skipped.append(f"#{pr_num} CONFLICT ({head_ref})")
            elif mergeable == "UNKNOWN":
                skipped.append(f"#{pr_num} UNKNOWN ({head_ref})")
            else:
                skipped.append(f"#{pr_num} {mergeable}/{merge_status} ({head_ref})")
            continue

        # Also check CI status via mergeStateStatus — only merge when green
        if merge_status != "clean":
            if merge_status == "unstable":
                skipped.append(f"#{pr_num} CI FAILING ({head_ref})")
            elif merge_status == "behind":
                skipped.append(f"#{pr_num} BEHIND main ({head_ref})")
            elif merge_status == "blocked":
                skipped.append(f"#{pr_num} BLOCKED ({head_ref})")
            else:
                skipped.append(f"#{pr_num} CI:{merge_status} ({head_ref})")
            continue

        # Determine bump level from commit messages
        rc, msgs, _ = run(["git", "log", "--oneline", f"origin/main..origin/{head_ref}",
                           "--format=%s"])
        if any(c.startswith(("feat", "feature")) for c in msgs.splitlines()):
            bump_level = "minor"

        candidates.append((pr_num, head_ref))

    if not candidates:
        conflicts = [s for s in skipped if "CONFLICT" in s]
        if conflicts:
            print(f"⚠️ {len(conflicts)} PRs with conflicts: {', '.join(conflicts[:5])}")
        return

    # Merge candidates one at a time. After each merge, re-check the next
    # candidate's MERGEABLE status (it may have become CONFLICTING if the
    # previous merge shifted its base).
    stagger_deadline = time.time() + STAGGER_BUDGET_SECONDS
    for pr_num, head_ref in candidates[:MAX_MERGES_PER_RUN]:
        # Re-check mergeable before merging (first merge may shift the base)
        if merged:
            rc, check, _ = run(["gh", "pr", "view", pr_num, "--json", "mergeable",
                                "--jq", ".mergeable"], timeout=15)
            if check != "MERGEABLE":
                skipped.append(f"#{pr_num} became {check} after prior merge — deferring")
                continue
        rc, merge_out, err = run(["gh", "pr", "merge", str(pr_num), "--merge",
                                  "--repo", REPO, "--delete-branch"], timeout=120)
        if rc == 0:
            merged.append(f"#{pr_num} ({head_ref})")
            if MERGE_STAGGER and len(candidates) > candidates.index((pr_num, head_ref)) + 1:
                remaining = stagger_deadline - time.time()
                if remaining > 5:
                    print(f"⏳ staggering after #{pr_num}: waiting for deploy to settle…")
                    wait_for_deploy_settle(lambda m: print(m), max_wait=remaining)
                else:
                    print("⏳ stagger budget exhausted — skipping further waits")
        else:
            skipped.append(f"#{pr_num} MERGE FAILED: {err[:80]}")

    if not merged:
        return

    # Centralized version bump after all merges land
    new_ver, bump_status = do_version_bump(bump_level)

    rc, current_ver, _ = run(["grep", "^version", "backend/pyproject.toml"])
    current_ver = current_ver.split('"')[1] if '"' in current_ver else "?"

    if bump_status == "ok":
        print(f"✅ Merged {len(merged)} PRs ({current_ver}→{new_ver}): {', '.join(merged)}")
    elif bump_status == "noop":
        print(f"✅ Merged {len(merged)} PRs (version unchanged {current_ver}): {', '.join(merged)}")
    else:
        print(f"✅ Merged {len(merged)} PRs (bump: {bump_status}): {', '.join(merged)}")

if __name__ == "__main__":
    main()