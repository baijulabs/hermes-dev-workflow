"""pr-consolidate.py — Autonomous PR consolidation from kanban worktrees.

Watches a set of coder cards and when all are done, cherry-picks commits from
their local worktree branches into a fresh PR branch, runs tests, and opens a PR.

Usage:  python3 pr-consolidate.py --epic <name> --coder-cards <id1> <id2> ...
For no_agent=True cron: empty stdout = silent, non-empty = delivery.
"""
import argparse, json, subprocess, sys
from pathlib import Path

STATE_DIR = Path.home() / ".hermes" / "profiles" / "orchestrator" / "state"
REPO_DIR = Path.home() / "MyProject"
KANBAN_DB = Path.home() / ".hermes" / "kanban" / "boards" / "${HERMES_KANBAN_BOARD:-main-dev}" / "kanban.db"

def msg(text): print(text)
def run(cmd, cwd=REPO_DIR, timeout=120):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    return r.returncode == 0, r.stdout.strip() + "\n" + r.stderr.strip()

def get_card(cid):
    r = subprocess.run(["sqlite3", "-separator", "|", str(KANBAN_DB),
        f"SELECT id, status, assignee, branch_name FROM tasks WHERE id='{cid}';"],
        capture_output=True, text=True, timeout=10)
    parts = r.stdout.strip().split("|")
    if len(parts) < 4: return None
    return {"id": parts[0], "status": parts[1], "branch_name": parts[3]}

def all_done(cids):
    return all((get_card(c) or {}).get("status") == "done" for c in cids)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--epic", required=True)
    ap.add_argument("--coder-cards", required=True, nargs="+")
    args = ap.parse_args()

    state_file = STATE_DIR / f"pr-consolidate-{args.epic}.json"
    state = json.loads(state_file.read_text()) if state_file.exists() else {"pr_created": False}
    if state.get("pr_created"): return

    if not all_done(args.coder_cards): return

    # Consolidation
    ok, out = run(["git", "fetch", "origin", "main", "--prune"])
    if not ok: return msg(f"ERROR: fetch failed: {out}")
    run(["git", "checkout", "main"])
    run(["git", "pull", "origin", "main"])
    branch = f"fix/{args.epic}"
    run(["git", "branch", "-D", branch])
    ok, out = run(["git", "checkout", "-b", branch])
    if not ok: return msg(f"ERROR: create branch: {out}")

    merged = False
    for cid in args.coder_cards:
        card = get_card(cid)
        if not card or not card["branch_name"]: continue
        lb = card["branch_name"]
        ok, out = run(["git", "rev-parse", "--verify", lb])
        if not ok: continue
        ok, out = run(["git", "log", "--oneline", "--format=%H", lb, "^main"])
        if not ok or not out.strip(): continue
        commits = out.strip().split("\n")
        commits.reverse()
        for sha in commits:
            ok, result = run(["git", "cherry-pick", sha, "--allow-empty"])
            if not ok:
                if "CONFLICT" in result:
                    msg(f"ERROR: conflict cherry-picking {sha[:8]} from {lb}")
                    msg(result[-1000:])
                    run(["git", "cherry-pick", "--abort"])
                    run(["git", "checkout", "main"])
                    run(["git", "branch", "-D", branch])
                    return
                run(["git", "cherry-pick", "--skip"])
        merged = True

    if not merged:
        run(["git", "checkout", "main"])
        run(["git", "branch", "-D", branch])
        return msg("ERROR: no commits to merge")

    # Tests
    ok, out = run(["./run-tests.sh", "backend", "-k",
        "test_list_quiz_attempts or test_promote_to_sop"], timeout=300)
    if not ok or "FAILED" in out:
        fl = [l for l in out.split("\n") if "FAILED" in l]
        msg("ERROR: backend tests failed:\n" + "\n".join(fl[-10:]))
        return run(["git", "checkout", "main"]) or run(["git", "branch", "-D", branch])

    ok, out = run(["./run-tests.sh", "frontend-all"], timeout=300)
    if not ok or "FAILED" in out:
        fl = [l for l in out.split("\n") if "FAILED" in l]
        return msg("ERROR: frontend tests failed:\n" + "\n".join(fl[-10:]))

    ok, out = run(["git", "push", "-u", "origin", "HEAD"])
    if not ok: return msg(f"ERROR: push failed: {out}")

    r = subprocess.run(["gh", "pr", "create", "--title", f"fix: {args.epic.replace('-', ' ')}",
        "--body", f"Automated PR consolidating cards: {', '.join(args.coder_cards)}"],
        cwd=REPO_DIR, capture_output=True, text=True, timeout=30)
    if r.returncode: return msg(f"ERROR: PR create failed: {r.stderr}")

    state["pr_created"] = True
    state_file.write_text(json.dumps(state))
    state_file.unlink(missing_ok=True)
    msg(f"PR created: {r.stdout.strip()}")

if __name__ == "__main__":
    main()