"""Tests for verify-deploy-qa.py — deploy detection + QA enqueue.

Boundary: `run()` (the gh subprocess wrapper) is scripted per test;
queue + state file behavior run for real inside tmp_path.
"""
import base64
import json

import pytest

from conftest import SCRIPTS_DIR

PYPROJECT = base64.b64encode(b'version = "9.9.9"\n').decode()


@pytest.fixture()
def vdq(monkeypatch, tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "verify_deploy_qa", SCRIPTS_DIR / "verify-deploy-qa.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "STATE_FILE",
                        tmp_path / "state" / "last_verified_deploy.json")
    return module


def _script_run(monkeypatch, module, responses):
    """responses: list of (rc, stdout) consumed in call order."""
    def fake_run(cmd, timeout=30):
        rc, out = responses.pop(0)
        return rc, out, ""

    monkeypatch.setattr(module, "run", fake_run)


def call_main(module):
    """main() exits via sys.exit on the silent paths — normalize to a code."""
    try:
        return module.main() or 0
    except SystemExit as e:
        return e.code or 0


def _deploy_responses(run_id="555"):
    return [
        (0, json.dumps({"id": int(run_id), "created": "2026-01-01T00:00:00Z",
                        "branch": "main", "title": "deploy",
                        "event": "pull_request_target"})),
        (0, "12345"),                       # jobs api: successful Deploy job
        (0, "abc123"),                      # head sha
        (0, PYPROJECT),                     # pyproject contents
    ]


def test_new_deploy_enqueues_qa_item_and_prints(vdq, monkeypatch, tmp_queue,
                                                capsys):
    _script_run(monkeypatch, vdq, _deploy_responses())
    enqueued = []
    monkeypatch.setattr(tmp_queue, "enqueue",
                        lambda **kw: enqueued.append(kw) or True)

    call_main(vdq)
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload["run_id"] == "555"
    assert payload["version"] == "9.9.9"

    assert len(enqueued) == 1
    item = enqueued[0]
    assert item["item_type"] == "deploy-qa"
    assert item["dedup_key"] == "deploy:555"
    assert item["payload"]["version"] == "9.9.9"
    assert item["payload"]["label"] == "v9.9.9"

    # state saved so the run is not re-processed
    assert json.loads(vdq.STATE_FILE.read_text())["last_run_id"] == "555"


def test_seen_deploy_is_silent(vdq, monkeypatch, tmp_queue, capsys):
    vdq.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    vdq.STATE_FILE.write_text(json.dumps({"last_run_id": "555",
                                          "last_verified_version": "9.9.9"}))
    _script_run(monkeypatch, vdq, _deploy_responses())
    monkeypatch.setattr(tmp_queue, "enqueue",
                        lambda **kw: (_ for _ in ()).throw(
                            AssertionError("must not enqueue")))
    call_main(vdq)
    assert capsys.readouterr().out == ""


def test_enqueue_failure_does_not_kill_the_notice(vdq, monkeypatch, tmp_queue,
                                                  capsys):
    _script_run(monkeypatch, vdq, _deploy_responses())

    def broken_enqueue(**kw):
        raise RuntimeError("queue disk full")

    monkeypatch.setattr(tmp_queue, "enqueue", broken_enqueue)
    vdq.main()  # must not raise
    assert json.loads(capsys.readouterr().out)["run_id"] == "555"


def test_no_deploy_job_is_silent(vdq, monkeypatch, tmp_queue, capsys):
    """A run without a successful 'Deploy to Staging' job is skipped."""
    responses = [
        (0, json.dumps({"id": 777, "created": "2026-01-01T00:00:00Z",
                        "branch": "main", "title": "deploy",
                        "event": "pull_request_target"})),
        (0, ""),                            # jobs api: no successful job
    ]
    _script_run(monkeypatch, vdq, responses)
    monkeypatch.setattr(tmp_queue, "enqueue",
                        lambda **kw: (_ for _ in ()).throw(
                            AssertionError("must not enqueue")))
    call_main(vdq)
    assert capsys.readouterr().out == ""


def test_gh_failure_is_silent(vdq, monkeypatch, tmp_queue, capsys):
    _script_run(monkeypatch, vdq, [(-1, "")])
    call_main(vdq)
    assert capsys.readouterr().out == ""
