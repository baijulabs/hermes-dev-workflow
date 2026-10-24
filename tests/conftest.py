"""Shared fixtures for the dev-workflow pipeline test suite.

Boundary rule: every test either exercises real file/SQLite/locking behavior
inside tmp_path, or mocks the subprocess boundary (gh / git / hermes CLI /
sqlite3 CLI). No test talks to a real board, repo, or GitHub.
"""
import importlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "profiles" / "orchestrator" / "scripts"
REPO_ROOT = Path(__file__).resolve().parents[1]

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


@pytest.fixture()
def scripts_dir():
    return SCRIPTS_DIR


@pytest.fixture()
def tmp_queue(monkeypatch, tmp_path):
    """Point agent_queue's store at an isolated tmp file."""
    import agent_queue
    monkeypatch.setattr(agent_queue, "QUEUE_FILE", tmp_path / "agent-queue.json")
    monkeypatch.setattr(agent_queue, "LOCK_FILE", tmp_path / "agent-queue.lock")
    return agent_queue


@pytest.fixture()
def fake_board(tmp_path):
    """A minimal kanban board directory: board.json + kanban.db with a tasks
    table shaped like the real one (only the columns the drainer touches)."""
    board_dir = tmp_path / "boards" / "test-dev"
    board_dir.mkdir(parents=True)
    (board_dir / "board.json").write_text(json.dumps({
        "slug": "test-dev",
        "default_workdir": str(tmp_path / "repo"),
    }))
    db = board_dir / "kanban.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT,"
        " assignee TEXT, status TEXT, branch_name TEXT)"
    )
    con.commit()
    con.close()
    return board_dir


def _make_fake_sh():
    """A subprocess boundary that executes sqlite3 SELECTs for real against
    the fake board (via the sqlite3 module — no CLI dependency) and refuses
    everything else, so unmocked external calls fail loudly."""
    import sqlite3 as _sq

    def fake_sh(cmd, cwd=None, timeout=120):
        if isinstance(cmd, list) and cmd and cmd[0] == "sqlite3":
            db, sql = cmd[1], cmd[2]
            con = _sq.connect(db)
            try:
                rows = con.execute(sql).fetchall()
                return True, "\n".join(str(r[0]) for r in rows if r)
            finally:
                con.close()
        raise AssertionError(f"unmocked subprocess call: {cmd}")

    return fake_sh


@pytest.fixture()
def daq(fake_board, monkeypatch):
    """drain-agent-queue module (hyphenated filename — loaded by path),
    configured against the fake board with its subprocess boundary mocked
    (sqlite3 reads execute for real; anything else fails loudly)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "drain_agent_queue", SCRIPTS_DIR / "drain-agent-queue.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.configure(
        board_slug="test-dev", kanban_dir=str(fake_board),
        repo_dir=str(fake_board.parent.parent / "repo"),
    )
    monkeypatch.setattr(module, "sh", _make_fake_sh())
    return module


def blocked_reviewer(board_dir, task_id="t_rev1"):
    """Insert a blocked code-reviewer card into the fake board DB."""
    con = sqlite3.connect(board_dir / "kanban.db")
    con.execute(
        "INSERT INTO tasks (id, title, assignee, status) VALUES (?,?,?,?)",
        (task_id, "Review: something", "code-reviewer", "blocked"),
    )
    con.commit()
    con.close()


def enqueue_item(agent_queue_module, item_type, dedup_key, payload,
                 item_id=None):
    import time as _t
    item_id = item_id or f"{item_type[:2]}_{int(_t.time()*1000)}"
    agent_queue_module.enqueue(item_id=item_id, item_type=item_type,
                               dedup_key=dedup_key, payload=payload)
    return item_id
