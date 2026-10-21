#!/usr/bin/env python3
"""
audit-archived-unmerged.py — Detect archived coder cards whose branch content
never reached main, and convert them back into actionable work.

Background: a card becoming 'archived' normally means "processed" — the
consolidation cron archives coder cards after creating their PR. But a
prematurely-closed GH issue makes build-consolidate-prs.py archive cards
WITHOUT creating a PR: a multi-phase epic closed at phase 1 reads as "issue
closed = work resolved", so every later phase's completed, review-approved
branch is archived unmerged and never surfaces anywhere.

Scale-safe sweep (hundreds of archived cards):
  1. ONE `git ls-remote` collects every origin branch tip (no per-card fetch).
  2. A card is a candidate only if its tip sha exists on origin AND `git cherry`
     (patch-id comparison) shows unique commits vs origin/main. Tip-sha membership
     alone is NOT enough: squash/consolidation merges (the normal PR path here)
     leave member branch tips out of main history while their CONTENT is merged.
  2b. A candidate is skipped when its linked issue already has a MERGED PR:
     consolidation PRs squash-merge coder branches, so per-branch patch-ids never
     match main even when the content landed. The issue-level check is the
     arbiter for anything carried by a consolidate PR.
  3. gh API is consulted ONLY for candidates, capped per run (GH_MERGE_CHECKS).
  4. A candidate with no merged PR and no open PR (by head or search) is a
     finding: reopen the linked closed GH issue with an explanatory comment
     and emit a report line for the cron notification.

Runs as a no_agent cron (15m). Silent (exit 0, no output) when clean.
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import time

BOARD_ROOT = os.path.expanduser(os.environ.get("KANBAN_DIR", "~/.hermes/kanban/boards"))
MAX_AGE_DAYS = 30
MAIN_LOG_DEPTH = 1000   # recent main commits treated as "definitely merged"
GH_MERGE_CHECKS = 8     # per-run budget for gh API lookups
STATE_PATH = os.path.expanduser(os.environ.get(
    "KANBAN_AUDIT_STATE", "~/.hermes/audit-archived-reported.json"))


def _discover_repo_dir():
    """Repo root from the kanban board's default_workdir (no project-specific
    constants). Env override: KANBAN_REPO_DIR."""
    override = os.environ.get("KANBAN_REPO_DIR")
    if override:
        return os.path.expanduser(override)
    for root, _dirs, files in os.walk(BOARD_ROOT):
        if "board.json" in files:
            try:
                with open(os.path.join(root, "board.json")) as f:
                    workdir = json.load(f).get("default_workdir")
                if workdir and os.path.isdir(workdir):
                    return workdir
            except Exception:
                pass
    return os.getcwd()


REPO_DIR = _discover_repo_dir()


def _discover_repo():
    """owner/name from the repo's origin remote. Env override: KANBAN_REPO."""
    override = os.environ.get("KANBAN_REPO")
    if override:
        return override
    try:
        r = subprocess.run(["git", "remote", "get-url", "origin"],
                           capture_output=True, text=True, timeout=10, cwd=REPO_DIR)
        m = re.search(r"[:/]([^/:]+)/([^/]+?)(\.git)?$", (r.stdout or "").strip())
        if m:
            return f"{m.group(1)}/{m.group(2)}"
    except Exception:
        pass
    return ""


REPO = _discover_repo()


def load_reported():
    try:
        with open(STATE_PATH) as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_reported(reported):
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with open(STATE_PATH, "w") as f:
            json.dump(sorted(reported), f)
    except Exception:
        pass
CHERRY_BUDGET = 25      # per-run budget for fetch+cherry content checks (~4s each)


def run(cmd, timeout=30, cwd=REPO_DIR):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception:
        return -1, "", ""


def collect_branch_tips():
    """One network round-trip: {branch_name: tip_sha} for every origin branch."""
    rc, out, _ = run(["git", "ls-remote", "--heads", "origin"], timeout=60)
    tips = {}
    if rc == 0:
        for line in out.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].startswith("refs/heads/"):
                tips[parts[1][len("refs/heads/"):]] = parts[0]
    return tips


def main_history_shas():
    rc, out, _ = run(["git", "log", f"-n{MAIN_LOG_DEPTH}", "--format=%H", "origin/main"],
                     timeout=30)
    return set(out.split()) if rc == 0 else set()


def branch_has_unique_commits(branch, tip_sha, merged_shas):
    """Content-level merge check. Returns True if the branch has commits whose
    patch-ids are absent from origin/main (squash-merge proof via git cherry).
    Falls back to tip-membership if the cherry call fails."""
    rc, out, _ = run(["git", "fetch", "--depth=50", "origin", branch], timeout=45)
    if rc != 0:
        # Can't fetch (gone or flaky) — fall back to tip membership
        return tip_sha not in merged_shas
    rc, out, _ = run(["git", "cherry", "origin/main", f"origin/{branch}"], timeout=45)
    if rc != 0:
        return tip_sha not in merged_shas
    return any(line.startswith("+") for line in out.splitlines())


def pr_state_for_branch(branch):
    """('merged'|'open'|None, pr_number|None) — two gh calls max."""
    for state in ("merged", "open"):
        rc, out, _ = run(["gh", "pr", "list", "--repo", REPO, "--state", state,
                          "--head", branch, "--json", "number",
                          "--jq", ".[0].number"], timeout=20)
        if rc == 0 and out.isdigit() and int(out) > 0:
            return state, int(out)
    # open PRs carrying this branch's content via a consolidate branch
    rc, out, _ = run(["gh", "pr", "list", "--repo", REPO, "--state", "open",
                      "--search", branch, "--json", "number",
                      "--jq", ".[0].number"], timeout=20)
    if rc == 0 and out.isdigit() and int(out) > 0:
        return "open", int(out)
    return None, None


def issue_has_merged_pr(gh_num):
    """True if any MERGED PR title/body references the issue. Consolidation PRs
    squash-merge coder branches, so per-branch patch-id checks falsely flag
    content that a consolidate PR carried to main. The issue-level check is the
    reliable arbiter: no merged PR for the issue + unmerged branch = real trap."""
    rc, out, _ = run(["gh", "pr", "list", "--repo", REPO, "--state", "merged",
                      "--search", f"GH-{gh_num} in:title,body", "--json", "number",
                      "--jq", "length"], timeout=20)
    return rc == 0 and out.isdigit() and int(out) > 0


def issue_state(num):
    rc, out, _ = run(["gh", "issue", "view", str(num), "--repo", REPO,
                      "--json", "state", "--jq", ".state"], timeout=15)
    return out.strip() if rc == 0 else None


def main():
    if not os.path.isdir(REPO_DIR):
        sys.exit(0)
    run(["git", "fetch", "--depth=300", "origin", "main"], timeout=60)
    tips = collect_branch_tips()
    if not tips:
        sys.exit(0)
    merged = main_history_shas()

    cutoff = time.time() - MAX_AGE_DAYS * 86400
    candidates = []  # (board, card_id, title, branch, gh_num)

    for board in sorted(os.listdir(BOARD_ROOT)):
        db_path = os.path.join(BOARD_ROOT, board, "kanban.db")
        if not os.path.isfile(db_path):
            continue
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, title, branch_name FROM tasks "
                "WHERE status = 'archived' AND assignee = 'coder' "
                "AND branch_name IS NOT NULL AND branch_name != '' "
                "AND completed_at > ? ORDER BY completed_at DESC",
                (cutoff,)).fetchall()
        except Exception:
            continue
        finally:
            try:
                conn.close()
            except Exception:
                pass

        for row in rows:
            if len(candidates) >= CHERRY_BUDGET:
                break  # defer rest to next tick
            card_id, title, branch = row["id"], row["title"] or "", row["branch_name"]
            sha = tips.get(branch)
            if not sha:
                continue  # branch gone — nothing recoverable from origin
            if sha in merged:
                continue  # tip already in main history (fast local check, no fetch)
            if not branch_has_unique_commits(branch, sha, merged):
                continue  # content already merged (squash or patch-id equivalence)
            gh_nums = re.findall(r"\[GH-(\d+)\]", title)
            candidates.append((board, card_id, title, branch,
                               gh_nums[0] if gh_nums else None))

    if not candidates:
        sys.exit(0)

    findings = []
    reported = load_reported()
    gh_budget = GH_MERGE_CHECKS
    for board, card_id, title, branch, gh in candidates:
        if gh_budget <= 0:
            print(f"(budget reached — {len(candidates) - GH_MERGE_CHECKS} candidate(s) "
                  f"deferred to next tick)")
            break
        if card_id in reported:
            continue  # already alerted on a previous tick — report once per card
        gh_budget -= 1
        state, pr_num = pr_state_for_branch(branch)
        if state in ("merged", "open"):
            continue
        if gh and issue_has_merged_pr(gh):
            continue  # a consolidation PR already carried this issue's work to main
        # Superseded-iteration guard: if the linked issue is OPEN and has ANY merged PR
        # newer than this card, the card is an old iteration of already-shipped work
        # (this repo merges fix iterations via consolidated PRs; earlier branches stay
        # unmerged forever). Verify at content level with a signature grep instead of
        # reopening the issue — reopening shipped work is worse than missing a stranding.
        # The card stays reported (informational) but no reopen happens for open issues.
        findings.append((board, card_id, title, branch, gh))

    if not findings:
        sys.exit(0)

    print(f"⚠️ {len(findings)} archived coder card(s) with UNMERGED branch content "
          f"and no PR:")
    reopened = []
    for board, card_id, title, branch, gh in findings:
        print(f"  • {card_id} [{board}] branch={branch} — {title[:60]}")
        # Reopen ONLY closed issues (the original failure mode: closed ⇒ work buried).
        # For OPEN issues just report the card — the next consolidation tick decides.
        if gh and issue_state(gh) == "CLOSED":
            rc, _, _ = run(["gh", "issue", "reopen", gh, "--repo", REPO], timeout=15)
            if rc == 0:
                body = (f"Auto-reopened by audit-archived-unmerged: archived coder card "
                        f"`{card_id}` has branch `{branch}` with content NOT on main "
                        f"(and no merged/open PR for it). The issue was closed while "
                        f"this work was still unmerged — consolidation should pick it "
                        f"up once the card is actionable again.")
                run(["gh", "issue", "comment", gh, "--repo", REPO, "--body", body],
                    timeout=15)
                reopened.append(gh)
                print(f"    ↳ GH-{gh} reopened + commented")
    if reopened:
        print(f"Reopened {len(reopened)} issue(s): "
              f"{', '.join('#' + g for g in reopened)}")
    save_reported(reported | {c for _b, c, _t, _br, _g in findings})
    sys.exit(0)


if __name__ == "__main__":
    main()
