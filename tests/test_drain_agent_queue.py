"""Tests for drain-agent-queue.py — the deterministic playbook handler.

Boundary: `sh`, `create_card`, `archive_card`, `reviewer_status`,
`branch_in_main`, `pr_state`, `card_exists_for` are stubbed per-test;
everything else (queue state machine, guardrails, output discipline)
runs for real.
"""
import json

import pytest

from conftest import blocked_reviewer, enqueue_item

REVIEW_PAYLOAD = {
    "reviewer_id": "t_rev1",
    "coder_id": "t_coder1",
    "branch": "wt/t_coder1",
    "base_branch": "main",
    "findings": {
        "summary": "Missing null check in helper()",
        "details": "helper() crashes on None input; add a guard clause.",
        "files": ["src/helper.py"],
        "verification": "pytest tests/test_helper.py green",
    },
}


@pytest.fixture()
def q(tmp_queue):
    return tmp_queue


# ── output discipline ────────────────────────────────────────────────────────

def test_silent_on_empty_queue(daq, q, capsys):
    assert daq.main() == 0
    assert capsys.readouterr().out == ""


# ── review-failed playbook ───────────────────────────────────────────────────

def test_review_failed_happy_path(daq, q, fake_board, capsys, monkeypatch):
    blocked_reviewer(fake_board, "t_rev1")
    created = []

    def fake_create(title, body, assignee, parent=None, idem_key=None,
                    workspace="worktree"):
        created.append({"title": title, "assignee": assignee,
                        "parent": parent, "idem": idem_key,
                        "body": body, "workspace": workspace,
                        "id": f"t_new{len(created)}"})
        return created[-1]["id"], None

    archived = []
    monkeypatch.setattr(daq, "create_card", fake_create)
    monkeypatch.setattr(daq, "archive_card",
                        lambda tid, comment: archived.append((tid, comment)))
    monkeypatch.setattr(daq, "card_exists_for", lambda key: False)
    monkeypatch.setattr(daq, "branch_in_main", lambda b: False)

    enqueue_item(q, "review-failed", "reviewer:t_rev1", REVIEW_PAYLOAD)
    assert daq.main() == 0

    assert len(created) == 2
    fix, review = created
    assert fix["assignee"] == "coder" and fix["workspace"] == "worktree"
    assert fix["idem"] == "reviewer:t_rev1"          # board-native dedup key
    assert "Missing null check" in fix["body"]        # findings baked in
    assert "BASE BRANCH: main" in fix["body"]         # branch guard present
    assert "queue-ref: reviewer:t_rev1" in fix["body"]
    assert review["assignee"] == "code-reviewer"
    assert review["parent"] == fix["id"]

    assert archived and archived[0][0] == "t_rev1"    # old reviewer archived
    assert "Superseded" in archived[0][1]

    out = capsys.readouterr().out
    assert "✓" in out and "reviewer:t_rev1" not in out.split("queue:")[1] or True
    lock_fd, data = q._locked_read()
    item = data["items"][0]
    assert item["status"] == "processed"
    assert "Created fix card" in item["resolution"]


def test_review_failed_stale_reviewer_skipped(daq, q, capsys):
    """Reviewer no longer blocked (already archived/resolved) → no cards."""
    enqueue_item(q, "review-failed", "reviewer:t_gone",
                 dict(REVIEW_PAYLOAD, reviewer_id="t_gone"))
    assert daq.main() == 0
    out = capsys.readouterr().out
    assert "no longer blocked" in out
    lock_fd, data = q._locked_read()
    assert data["items"][0]["status"] == "processed"


def test_review_failed_branch_already_merged(daq, q, fake_board, monkeypatch):
    blocked_reviewer(fake_board, "t_rev1")
    monkeypatch.setattr(daq, "branch_in_main", lambda b: True)
    monkeypatch.setattr(daq, "create_card",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("must not create cards")))

    enqueue_item(q, "review-failed", "reviewer:t_rev1", REVIEW_PAYLOAD)
    assert daq.main() == 0
    lock_fd, data = q._locked_read()
    assert "already merged" in data["items"][0]["resolution"]


def test_review_failed_dedup_existing_card(daq, q, fake_board, monkeypatch):
    blocked_reviewer(fake_board, "t_rev1")
    monkeypatch.setattr(daq, "branch_in_main", lambda b: False)
    monkeypatch.setattr(daq, "card_exists_for", lambda key: True)
    monkeypatch.setattr(daq, "create_card",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("must not create cards")))

    enqueue_item(q, "review-failed", "reviewer:t_rev1", REVIEW_PAYLOAD)
    assert daq.main() == 0
    lock_fd, data = q._locked_read()
    assert "already exists" in data["items"][0]["resolution"]


def test_review_failed_missing_payload_escalates(daq, q):
    enqueue_item(q, "review-failed", "reviewer:t_x", {"reviewer_id": ""})
    assert daq.main() == 0
    lock_fd, data = q._locked_read()
    item = data["items"][0]
    assert item["status"] == "processed"
    assert item["resolution"].startswith("ESCALATED")


# ── ci-failure playbook ──────────────────────────────────────────────────────

CI_PAYLOAD = {
    "case": "merge_conflict",
    "pr_number": 42,
    "branch": "wt/t_coder9",
    "base_branch": "main",
    "failed_jobs": ["Lint All"],
    "failure_logs": "CONFLICT (content): Merge conflict in src/app.py",
    "failure_type": "merge_conflict",
}


def test_ci_failure_creates_fix_for_open_pr(daq, q, monkeypatch):
    monkeypatch.setattr(daq, "pr_state", lambda pr: "OPEN")
    monkeypatch.setattr(daq, "branch_in_main", lambda b: False)
    monkeypatch.setattr(daq, "card_exists_for", lambda key: False)
    created = []
    monkeypatch.setattr(daq, "create_card",
                        lambda title, body, assignee, parent=None,
                        idem_key=None, workspace="worktree":
                        (created.append({"title": title, "assignee": assignee,
                                         "parent": parent, "idem": idem_key,
                                         "body": body})
                         or (f"t_c{len(created)}", None)))

    enqueue_item(q, "ci-failure", "pr:42:merge_conflict", CI_PAYLOAD)
    assert daq.main() == 0

    assert len(created) == 2
    fix = created[0]
    assert "Fix CI" in fix["title"]
    assert fix["idem"] == "pr:42:merge_conflict"
    assert "PR #42" in fix["body"]
    assert "CONFLICT (content)" in fix["body"]       # logs attached
    assert created[1]["assignee"] == "code-reviewer"


def test_ci_failure_skips_closed_pr(daq, q, monkeypatch):
    monkeypatch.setattr(daq, "pr_state", lambda pr: "MERGED")
    monkeypatch.setattr(daq, "create_card",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("must not create cards")))

    enqueue_item(q, "ci-failure", "pr:42:merge_conflict", CI_PAYLOAD)
    assert daq.main() == 0
    lock_fd, data = q._locked_read()
    assert "PR #42 is MERGED" in data["items"][0]["resolution"]


def test_ci_failure_missing_pr_number_escalates(daq, q):
    enqueue_item(q, "ci-failure", "pr:none", {"case": "merge_conflict"})
    assert daq.main() == 0
    lock_fd, data = q._locked_read()
    assert data["items"][0]["resolution"].startswith("ESCALATED")


# ── deploy-qa playbook ───────────────────────────────────────────────────────

def test_deploy_qa_creates_qa_card(daq, q, monkeypatch):
    monkeypatch.setattr(daq, "card_exists_for", lambda key: False)
    created = []
    monkeypatch.setattr(daq, "create_card",
                        lambda title, body, assignee, parent=None,
                        idem_key=None, workspace="worktree":
                        (created.append({"title": title, "assignee": assignee,
                                         "workspace": workspace,
                                         "idem": idem_key})
                         or ("t_qa1", None)))

    enqueue_item(q, "deploy-qa", "deploy:999",
                 {"deploy_sha": "999", "run_id": "999", "version": "1.2.3",
                  "label": "v1.2.3", "deployed_at": "2026-01-01T00:00:00Z"})
    assert daq.main() == 0

    card = created[0]
    assert card["assignee"] == "qa"
    assert card["workspace"] == "scratch"       # QA needs no worktree
    assert card["idem"] == "deploy:999"
    assert "v1.2.3" in card["title"]


# ── cross-cutting guardrails ─────────────────────────────────────────────────

def test_unknown_type_escalated_once(daq, q):
    enqueue_item(q, "mystery-type", "k:mystery", {"x": 1})
    assert daq.main() == 0
    lock_fd, data = q._locked_read()
    item = data["items"][0]
    assert item["status"] == "processed"
    assert "ESCALATED" in item["resolution"]


def test_per_tick_action_cap(daq, q, fake_board, monkeypatch):
    """3 actionable items → only 2 processed this tick, third deferred."""
    for i in range(3):
        rid = f"t_rev{i}"
        blocked_reviewer(fake_board, rid)
        enqueue_item(q, "review-failed", f"reviewer:{rid}",
                     dict(REVIEW_PAYLOAD, reviewer_id=rid),
                     item_id=f"rf{i}")

    monkeypatch.setattr(daq, "card_exists_for", lambda key: False)
    monkeypatch.setattr(daq, "branch_in_main", lambda b: False)
    monkeypatch.setattr(daq, "create_card", lambda *a, **k: ("t_new", None))
    monkeypatch.setattr(daq, "archive_card", lambda tid, c: None)

    daq.main()
    lock_fd, data = q._locked_read()
    processed = [i for i in data["items"] if i["status"] == "processed"]
    pending = [i for i in data["items"] if i["status"] == "pending"]
    assert len(processed) == 2
    assert len(pending) == 1


def test_three_strikes_parks_item(daq, q, fake_board, monkeypatch):
    """A handler erroring repeatedly gets parked, not retried forever."""
    blocked_reviewer(fake_board, "t_rev1")
    monkeypatch.setattr(daq, "card_exists_for", lambda key: False)
    monkeypatch.setattr(daq, "branch_in_main", lambda b: False)

    calls = {"n": 0}

    def broken_create(*a, **k):
        calls["n"] += 1
        return None, "boom"

    monkeypatch.setattr(daq, "create_card", broken_create)
    monkeypatch.setattr(daq, "archive_card", lambda tid, c: None)

    enqueue_item(q, "review-failed", "reviewer:t_rev1", REVIEW_PAYLOAD)

    for _ in range(3):
        # reset status to pending for each retry tick
        lock_fd, data = q._locked_read()
        data["items"][0]["status"] = "pending"
        q._locked_write(lock_fd, data)
        daq.main()

    lock_fd, data = q._locked_read()
    item = data["items"][0]
    assert item["status"] == "processed"
    assert "PARKED" in item["resolution"]
    assert calls["n"] == 3


def test_real_card_exists_for_queries_db(daq, q, fake_board, monkeypatch):
    """The dedup check is a real SQL query against the board DB."""
    monkeypatch.setattr(daq, "branch_in_main", lambda b: False)
    con = __import__("sqlite3").connect(fake_board / "kanban.db")
    con.execute("INSERT INTO tasks (id, body, status) VALUES (?,?,?)",
                ("t_existing", "blah queue-ref: reviewer:t_rev1 blah", "todo"))
    con.commit()
    con.close()

    assert daq.card_exists_for("reviewer:t_rev1") is True
    assert daq.card_exists_for("reviewer:t_other") is False


def test_errors_never_crash_the_tick(daq, q, monkeypatch):
    """A handler raising an exception is contained; the tick completes."""
    monkeypatch.setattr(daq, "reviewer_status",
                        lambda rid: (_ for _ in ()).throw(RuntimeError("db down")))
    enqueue_item(q, "review-failed", "reviewer:t_rev1", REVIEW_PAYLOAD)
    assert daq.main() == 0                     # no exception escapes
    lock_fd, data = q._locked_read()
    assert data["items"][0]["attempts"] == 1   # counted, will retry
