"""Contract tests for the cron template and every packaged script.

These enforce pipeline invariants that the v2.3.0 architecture depends on:
no LLM on a polling timer that acts, detectors deliver to the orchestrator
chat, the drainer exists, the LLM dispatcher stays gone, and no script
hardcodes a machine-specific path.
"""
import json
from pathlib import Path

import pytest

from conftest import REPO_ROOT, SCRIPTS_DIR

TEMPLATE = REPO_ROOT / "profiles" / "orchestrator" / "cron" / "jobs.json.template"

DETECTOR_JOBS = ["ingest-ci-failures", "ingest-deploy-failures",
                 "build-reviewer-resolve", "verify-deploy-qa"]


@pytest.fixture(scope="module")
def jobs():
    raw = TEMPLATE.read_text().split("\n")
    body = "\n".join(l for l in raw if not l.strip().startswith("#"))
    return {j["name"]: j for j in json.loads(body)["jobs"]}


def test_template_is_valid_json_after_comment_strip(jobs):
    assert len(jobs) >= 18


def test_detectors_are_no_agent_and_deliver_origin(jobs):
    for name in DETECTOR_JOBS:
        job = jobs[name]
        assert job["no_agent"] is True, f"{name} must not spin an LLM session"
        assert job["deliver"] == "origin", f"{name} must reach the orchestrator chat"
        assert not job.get("prompt"), f"{name} must not carry an LLM prompt"
        assert not job.get("skills"), f"{name} must not load skills"
        assert "model" not in job and "provider" not in job


def test_no_llm_model_on_unauthorized_polling_jobs(jobs):
    """Timer-driven jobs must not pin an LLM model — agent runs belong to
    kanban cards (worker dispatch), not clocks. Exception allowlist is
    deliberate and documented: the weekly dogfood run."""
    llm_allowed = {"verify-dogfood"}
    offenders = [n for n, j in jobs.items() if "model" in j
                 and n not in llm_allowed]
    assert offenders == []


def test_llm_dispatcher_cron_is_gone(jobs):
    assert "queue-agent-processor" not in jobs, (
        "dispatch_in_gateway owns dispatch; the LLM processor cron must stay gone")


def test_drain_agent_queue_present(jobs):
    job = jobs["drain-agent-queue"]
    assert job["no_agent"] is True
    assert job["script"] == "drain-agent-queue.py"
    assert job["deliver"] == "origin"


def test_every_job_runs_a_script(jobs):
    """A job with no script and no_agent=true is a no-op slot."""
    for name, job in jobs.items():
        if job.get("no_agent"):
            assert job.get("script"), f"{name}: no_agent job without a script"


def test_schedule_kinds_are_valid(jobs):
    for name, job in jobs.items():
        sched = job.get("schedule", {})
        assert sched.get("kind") in ("interval", "cron"), f"{name}: bad schedule"


# ── script contract ──────────────────────────────────────────────────────────

def _python_scripts():
    return sorted(SCRIPTS_DIR.glob("*.py"))


def test_every_script_compiles():
    import py_compile
    for script in _python_scripts():
        py_compile.compile(str(script), doraise=True)


def test_no_machine_specific_home_paths():
    """Scripts must not hardcode a developer's home directory.
    The generic /home/user placeholder (used by setup.sh templating) is the
    only allowed literal."""
    import re
    offenders = []
    for script in _python_scripts():
        text = script.read_text()
        for match in re.finditer(r"/home/([A-Za-z0-9_.-]+)/", text):
            if match.group(1) != "user":
                offenders.append(f"{script.name}:{match.group(0)}")
    assert offenders == []


def test_scripts_never_hardcode_a_github_repo_slug():
    """owner/repo must come from env or `git remote get-url origin` — never a
    string literal with a real slug (placeholder patterns are fine)."""
    import re
    offenders = []
    pattern = re.compile(r"[\"'](?!owner/|\$\{)[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+[\"']")
    allowed = re.compile(r"(git remote get-url|owner/project|owner/repo|"
                         r"<owner>|HEAD|origin/|refs/|wt/|fix/)")
    for script in _python_scripts():
        for line in script.read_text().split("\n"):
            if pattern.search(line) and not allowed.search(line):
                if "github.com" in line or '"--repo"' in line or "REPO" in line.upper():
                    offenders.append(f"{script.name}: {line.strip()[:80]}")
    assert offenders == []


def test_drainer_only_creates_cards():
    """Safety invariant: the drainer's vocabulary must not contain any
    code-modifying or git-mutating operation."""
    text = (SCRIPTS_DIR / "drain-agent-queue.py").read_text()
    for forbidden in ("git push", "git commit", "git merge ", "git cherry-pick",
                      "pr merge", "write_text(", "shutil.", "os.remove"):
        assert forbidden not in text, f"drainer must never {forbidden}"


def test_queue_store_uses_locking():
    text = (SCRIPTS_DIR / "agent_queue.py").read_text()
    assert "flock" in text, "queue writes must be file-locked"
