"""Tests for agent_queue.py — the file-locked JSON queue store."""
import json
import time

import pytest


def test_enqueue_and_get_pending(tmp_queue):
    assert tmp_queue.enqueue("i1", "review-failed", {"a": 1}, dedup_key="reviewer:t_1")
    items = tmp_queue.get_pending()
    assert len(items) == 1
    assert items[0]["id"] == "i1"
    assert items[0]["status"] == "pending"
    assert items[0]["payload"] == {"a": 1}


def test_enqueue_dedups_same_pending_key(tmp_queue):
    assert tmp_queue.enqueue("i1", "ci-failure", {}, dedup_key="pr:5:merge_conflict") is True
    assert tmp_queue.enqueue("i2", "ci-failure", {}, dedup_key="pr:5:merge_conflict") is False
    assert len(tmp_queue.get_pending()) == 1


def test_same_key_allowed_after_processed(tmp_queue):
    tmp_queue.enqueue("i1", "ci-failure", {}, dedup_key="pr:5:x")
    tmp_queue.mark_processed(["i1"])
    assert tmp_queue.enqueue("i2", "ci-failure", {}, dedup_key="pr:5:x") is True


def test_mark_processing_hides_from_get_pending(tmp_queue):
    tmp_queue.enqueue("i1", "t", {}, dedup_key="k1")
    tmp_queue.mark_processing(["i1"])
    assert tmp_queue.get_pending() == []


def test_mark_processed_sets_timestamp_and_prunes_old(tmp_queue):
    old = int(time.time()) - 2 * 86400
    tmp_queue.enqueue("old", "t", {}, dedup_key="k-old")
    tmp_queue.enqueue("new", "t", {}, dedup_key="k-new")
    # backdate the first item past the 24h retention window
    lock_fd, data = tmp_queue._locked_read()
    data["items"][0]["created_at"] = old
    tmp_queue._locked_write(lock_fd, data)

    tmp_queue.mark_processed(["old", "new"])
    lock_fd, data = tmp_queue._locked_read()
    ids = [i["id"] for i in data["items"]]
    assert "old" not in ids          # pruned by the 24h retention rule
    assert "new" in ids              # kept
    assert data["last_processed_at"] is not None


def test_queue_summary_counts(tmp_queue):
    tmp_queue.enqueue("i1", "t", {}, dedup_key="k1")
    tmp_queue.enqueue("i2", "t", {}, dedup_key="k2")
    tmp_queue.mark_processing(["i2"])
    counts = tmp_queue.get_queue_summary()
    assert counts["pending"] == 1
    assert counts["processing"] == 1
    assert counts["total"] == 2


def test_persistence_across_module_reload(tmp_queue, tmp_path, monkeypatch):
    """The queue file must survive a fresh import (cron ticks are new processes)."""
    tmp_queue.enqueue("i1", "t", {"payload": True}, dedup_key="k1")
    import importlib
    importlib.reload(tmp_queue)
    # reload re-executes module-level constants — re-point at the tmp store
    monkeypatch.setattr(tmp_queue, "QUEUE_FILE", tmp_path / "agent-queue.json")
    monkeypatch.setattr(tmp_queue, "LOCK_FILE", tmp_path / "agent-queue.lock")
    items = tmp_queue.get_pending()
    assert len(items) == 1 and items[0]["payload"] == {"payload": True}
