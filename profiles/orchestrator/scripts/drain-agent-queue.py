#!/usr/bin/env python3
"""
drain-agent-queue.py — deterministic playbook handler for agent-queue.json.

no_agent cron (every 10m). Zero LLM calls. Replaces the former
queue-agent-processor LLM cron's *judgment* layer with fixed recipes;
anything the playbooks don't cover stays pending for the orchestrator.

Playbooks:
  review-failed  -> fix coder card + paired code-reviewer card,
                    comment + archive the dead blocked reviewer.
  ci-failure     -> CI-fix coder card + paired reviewer for an open PR
                    with a merge conflict or failing checks.
  deploy-qa      -> QA verification card (assignee=qa) for a new staging deploy.
  anything else  -> left pending (escalated to orchestrator).

Guardrails (all hard-coded):
  - max 2 card-creating actions per tick
  - board-native idempotency dedup (--idempotency-key = queue dedup_key)
  - skip if the branch tip is already an ancestor of origin/main
  - skip if the PR is no longer OPEN
  - skip if the source reviewer card is no longer blocked
  - worktree workspace only; never modifies code, never pushes, never merges
  - items failing 3+ consecutive ticks are parked as 'failed' (visible in
    output, not retried)

Silent (empty stdout) when there is nothing pending or nothing actionable.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import agent_queue  # noqa: E402

MAX_ACTIONS_PER_TICK = 2
MAX_ATTEMPTS = 3

# Resolved lazily by configure() — module import must never touch the
# filesystem outside the script's own directory (testability + a board-less
# environment must fail in main(), not at import).
BOARD_SLUG = None
BOARD_DIR = None
DB = None
REPO = None

GUARD = (
    "CRITICAL: Before writing code, run `git branch --show-current` and verify "
    "you are on a worktree branch derived from the base branch above. You must "
    "NOT be on main or master. If you are, block the task immediately."
)


def configure(board_slug=None, kanban_dir=None, repo_dir=None):
    """Resolve board + repo paths. Precedence: explicit args > env overrides
    (KANBAN_DIR, KANBAN_REPO_DIR, HERMES_KANBAN_BOARD) > auto-discovery.

    Auto-discovery: when no slug is given, scan the boards root for a board
    whose default_workdir matches the repo checkout (cron workers on a live
    install carry no HERMES_KANBAN_BOARD env, so a hardcoded default would
    point at a nonexistent board). Falls back to scanning for exactly one
    existing board directory."""
    global BOARD_SLUG, BOARD_DIR, DB, REPO
    if board_slug:
        BOARD_SLUG = board_slug
    elif env_slug := os.environ.get("HERMES_KANBAN_BOARD"):
        BOARD_SLUG = env_slug
    else:
        BOARD_SLUG = None  # resolve below, from boards root scan

    if kanban_dir:
        BOARD_DIR = Path(kanban_dir)
    else:
        env_dir = os.environ.get("KANBAN_DIR")
        boards_root = Path(env_dir) if env_dir else (
            Path.home() / ".hermes" / "kanban" / "boards")
        if BOARD_SLUG:
            BOARD_DIR = boards_root / BOARD_SLUG
        else:
            BOARD_DIR = boards_root  # scan root; BOARD_DIR re-pointed below

    if repo_dir:
        REPO = Path(repo_dir)
    else:
        REPO = None  # resolve below

    # board.json self-config: default_workdir is the repo, and when the slug
    # is unresolved, the directory name of the matching board is the slug.
    if REPO is None or BOARD_SLUG is None:
        _candidates = [BOARD_DIR] if BOARD_SLUG else sorted(
            p for p in BOARD_DIR.iterdir()
            if (p / "board.json").is_file()) if BOARD_DIR.is_dir() else []
        for _cand in _candidates:
            try:
                _meta = json.loads((_cand / "board.json").read_text())
            except (OSError, ValueError):
                continue
            _workdir = _meta.get("default_workdir")
            if not _workdir:
                continue
            if REPO is None:
                REPO = Path(_workdir)
            if BOARD_SLUG is None:
                BOARD_SLUG = _cand.name
                BOARD_DIR = _cand  # re-point from scan root to the board dir
            break
        if REPO is None:
            raise RuntimeError(
                "configure(): could not resolve repo dir — no board.json "
                f"with default_workdir found under {BOARD_DIR}. Set "
                "HERMES_KANBAN_BOARD or KANBAN_DIR.")
    DB = BOARD_DIR / "kanban.db"


def sh(cmd, cwd=None, timeout=120):
    """Run a command, return (ok, combined_output)."""
    env = dict(os.environ)
    env["HERMES_KANBAN_BOARD"] = BOARD_SLUG or ""
    try:
        if isinstance(cmd, str):
            r = subprocess.run(cmd, shell=True, cwd=cwd or str(REPO),
                               capture_output=True, text=True, timeout=timeout,
                               env=env)
        else:
            r = subprocess.run(cmd, cwd=cwd or str(REPO),
                               capture_output=True, text=True, timeout=timeout,
                               env=env)
        return r.returncode == 0, (r.stdout or "") + (r.stderr or "")
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, str(e)


def branch_in_main(branch):
    """True if origin/<branch> tip is an ancestor of origin/main (fix landed)."""
    if not branch:
        return False
    sh("git fetch origin main --quiet", timeout=60)
    ok, out = sh(f"git merge-base --is-ancestor origin/{branch} origin/main")
    return ok and "error" not in out.lower()


def pr_state(pr_number):
    ok, out = sh(
        f"gh pr view {pr_number} --json state --jq .state", timeout=60
    )
    return out.strip() if ok else "UNKNOWN"


def card_exists_for(dedup_key):
    """True if a non-terminal card already embeds this queue-ref marker."""
    ok, out = sh(
        ["sqlite3", str(DB),
         "SELECT COUNT(*) FROM tasks WHERE status NOT IN "
         "('done','archived','cancelled','trash') "
         f"AND body LIKE '%queue-ref: {dedup_key}%';"]
    )
    return ok and out.strip() not in ("", "0")


def reviewer_status(reviewer_id):
    ok, out = sh(
        ["sqlite3", str(DB),
         f"SELECT status FROM tasks WHERE id='{reviewer_id}';"]
    )
    return out.strip() if ok else ""


def create_card(title, body, assignee, parent=None, idem_key=None,
                workspace="worktree"):
    """Create a kanban card via CLI. Returns (task_id, error). Native
    board dedup via --idempotency-key when provided."""
    cmd = ["hermes", "kanban", "create", title,
           "--assignee", assignee,
           "--workspace", workspace,
           "--body-file", "-",
           "--json"]
    if parent:
        cmd += ["--parent", parent]
    if idem_key:
        cmd += ["--idempotency-key", idem_key]
    try:
        env = dict(os.environ)
        env["HERMES_KANBAN_BOARD"] = BOARD_SLUG or ""
        r = subprocess.run(cmd, input=body, capture_output=True, text=True,
                           timeout=90, env=env)
        out = (r.stdout or "") + (r.stderr or "")
        if r.returncode != 0:
            return None, out.strip()[:300]
        data = json.loads(out[out.index("{"):])
        return data.get("id"), None
    except (subprocess.TimeoutExpired, ValueError, KeyError, OSError) as e:
        return None, str(e)[:300]


def archive_card(task_id, comment):
    sh(["hermes", "kanban", "comment", task_id, comment], timeout=60)
    sh(["hermes", "kanban", "archive", task_id], timeout=60)


def handle_review_failed(item, actions):
    p = item["payload"]
    reviewer_id = p.get("reviewer_id", "")
    coder_id = p.get("coder_id", "")
    branch = p.get("branch", "")
    base = p.get("base_branch") or "main"
    f = p.get("findings", {})
    summary = (f.get("summary") or "").strip()
    details = (f.get("details") or "").strip()
    files = ", ".join(f.get("files") or []) or "(see findings)"
    verification = (f.get("verification") or "Tests must pass").strip()
    if not (reviewer_id and summary):
        return "escalate", "missing reviewer_id or findings.summary"

    if reviewer_status(reviewer_id) != "blocked":
        return "processed", f"reviewer {reviewer_id} no longer blocked (stale finding)"

    if branch and branch_in_main(branch):
        return "processed", f"branch {branch} already merged to main — no fix needed"

    if card_exists_for(item["dedup_key"]):
        return "processed", "fix card already exists for this finding"

    short = summary.replace("\n", " ")[:90]
    body = (
        "## Goal\n"
        f"Fix review findings on `{branch}` (from reviewer {reviewer_id}, "
        f"parent coder {coder_id}).\n\n"
        "## Findings\n"
        f"{summary}\n\n{details}\n\n"
        "## Files to Modify/Inspect\n"
        f"{files}\n\n"
        "## Expected Verification\n"
        f"{verification}\n\n"
        f"queue-ref: {item['dedup_key']}\n\n"
        f"BASE BRANCH: {base}\n{GUARD}"
    )
    fix_id, err = create_card(f"Fix: {short}", body, "coder",
                              idem_key=item["dedup_key"])
    if not fix_id:
        return "error", f"create fix card failed: {err}"

    rbody = (
        f"Review implementation of: {short}\n\n"
        f"Coder task: {fix_id}\n"
        f"Original finding source: reviewer {reviewer_id}\n"
        f"Files: {files}\n"
        f"Verification: {verification}\n\n"
        f"queue-ref: {item['dedup_key']}\n\n"
        f"BASE BRANCH: {base}\n{GUARD}"
    )
    new_rev, err = create_card(f"Review: Fix {short[:70]}", rbody,
                               "code-reviewer", parent=fix_id)
    archive_card(reviewer_id, f"Superseded by fix card {fix_id}"
                 + (f" (paired reviewer: {new_rev})" if new_rev else "."))
    actions.append(f"review-failed {reviewer_id}: created fix {fix_id}"
                   + (f" + reviewer {new_rev}" if new_rev else ""))
    return "processed", (f"Created fix card {fix_id} + reviewer {new_rev}; "
                         f"archived old reviewer {reviewer_id}.")


def handle_ci_failure(item, actions):
    p = item["payload"]
    pr = p.get("pr_number")
    case = p.get("case") or "failing_checks"
    branch = p.get("branch", "")
    base = p.get("base_branch") or "main"
    logs = (p.get("failure_logs") or "").strip()[:2000]
    jobs = ", ".join(p.get("failed_jobs") or []) or "(see logs)"
    if not pr:
        return "escalate", "missing pr_number"

    state = pr_state(pr)
    if state != "OPEN":
        return "processed", f"PR #{pr} is {state} — nothing to fix"

    if branch and branch_in_main(branch):
        return "processed", f"branch {branch} already merged to main"

    if card_exists_for(item["dedup_key"]):
        return "processed", "fix card already exists for this PR failure"

    short = f"PR #{pr} {case.replace('_', ' ')}"
    body = (
        "## Goal\n"
        f"Resolve {case.replace('_', ' ')} on PR #{pr} so it can merge.\n"
        f"Branch: `{branch}`  Base: `{base}`\n\n"
        "## Failed jobs\n"
        f"{jobs}\n\n"
        "## Failure logs (excerpt)\n"
        f"```\n{logs}\n```\n\n"
        f"queue-ref: {item['dedup_key']}\n\n"
        f"BASE BRANCH: {base}\n{GUARD}"
    )
    fix_id, err = create_card(f"Fix CI: {short}", body, "coder",
                              idem_key=item["dedup_key"])
    if not fix_id:
        return "error", f"create fix card failed: {err}"

    rbody = (
        f"Review implementation of CI fix for PR #{pr}.\n\n"
        f"Coder task: {fix_id}\n"
        f"Branch: `{branch}`\n"
        f"Verification: CI green on the new branch; PR #{pr} becomes MERGEABLE.\n\n"
        f"queue-ref: {item['dedup_key']}\n\n"
        f"BASE BRANCH: {base}\n{GUARD}"
    )
    new_rev, _ = create_card(f"Review: Fix CI {short[:60]}", rbody,
                             "code-reviewer", parent=fix_id)
    actions.append(f"ci-failure PR#{pr}: created fix {fix_id}"
                   + (f" + reviewer {new_rev}" if new_rev else ""))
    return "processed", f"Created CI-fix card {fix_id} (+ reviewer {new_rev}) for PR #{pr}."


def handle_deploy_qa(item, actions):
    p = item["payload"]
    sha = p.get("deploy_sha") or p.get("run_id") or ""
    label = p.get("label") or sha[:8] or "latest"
    if card_exists_for(item["dedup_key"]):
        return "processed", "QA card already exists for this deploy"
    body = (
        "## Goal\n"
        f"Dogfood-test the staging deploy ({label}) and file findings.\n"
        "Deploy SHA/run: "
        f"{sha or 'see queue'}\n\n"
        "## Expected Verification\n"
        "Login works; core flows pass; report ALL severities as GH sub-issues "
        "under the main QA report with labels QA review + dogfood.\n\n"
        f"queue-ref: {item['dedup_key']}"
    )
    qa_id, err = create_card(f"QA: verify staging deploy {label}", body, "qa",
                             idem_key=item["dedup_key"], workspace="scratch")
    if not qa_id:
        return "error", f"create QA card failed: {err}"
    actions.append(f"deploy-qa: created QA card {qa_id} for {label}")
    return "processed", f"Created QA card {qa_id} for deploy {label}."


HANDLERS = {
    "review-failed": handle_review_failed,
    "ci-failure": handle_ci_failure,
    "deploy-qa": handle_deploy_qa,
}


def main():
    if REPO is None:  # fresh process (cron tick): resolve from env/board.json
        configure()
    counts = agent_queue.get_queue_summary()
    if counts.get("pending", 0) == 0:
        return 0  # silent

    actions, lines = [], []
    lock_fd, data = agent_queue._locked_read()
    pending = [i for i in data["items"] if i.get("status") == "pending"]
    processed_now = 0

    for item in pending:
        if processed_now >= MAX_ACTIONS_PER_TICK:
            lines.append(f"… {item['id']}: deferred (per-tick cap reached)")
            continue
        handler = HANDLERS.get(item.get("type"))
        try:
            if handler:
                outcome, note = handler(item, actions)
            else:
                outcome, note = "escalate", f"no playbook for type '{item.get('type')}'"
        except Exception as e:  # never crash the tick
            outcome, note = "error", f"handler exception: {e}"

        if outcome == "processed":
            item["status"] = "processed"
            item["processed_at"] = int(time.time())
            item["resolution"] = note
            processed_now += 1
            lines.append(f"✓ {item['id']}: {note}")
        elif outcome == "escalate":
            item["status"] = "processed"
            item["processed_at"] = int(time.time())
            item["resolution"] = f"ESCALATED: {note}"
            processed_now += 1
            lines.append(f"→ ESCALATE {item['id']} ({item.get('type')}): {note}")
        else:  # error — count attempts, park after MAX_ATTEMPTS
            attempts = item.get("attempts", 0) + 1
            item["attempts"] = attempts
            if attempts >= MAX_ATTEMPTS:
                item["status"] = "processed"
                item["processed_at"] = int(time.time())
                item["resolution"] = f"PARKED after {attempts} attempts: {note}"
                lines.append(f"✗ PARKED {item['id']}: {note}")
            else:
                lines.append(f"↻ retry {item['id']} (attempt {attempts}): {note}")

    data["last_processed_at"] = int(time.time())
    now = int(time.time())
    data["items"] = [
        i for i in data["items"]
        if i.get("status") == "pending" or (now - i.get("created_at", 0)) < 86400
    ][-200:]
    agent_queue._locked_write(lock_fd, data)

    if lines:
        print("\n".join(lines))
        print(f"queue: {sum(1 for i in data['items'] if i.get('status') == 'pending')} "
              f"pending / {len(data['items'])} total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
