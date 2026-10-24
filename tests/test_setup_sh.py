"""setup.sh contract tests.

setup.sh touches the real $HOME/.hermes when run for real, so these tests
only exercise the --dry-run path, with HERMES redirected to a throwaway
dir to prove the invariant that matters: --dry-run must perform ZERO
writes anywhere.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
SETUP = REPO / "setup.sh"


@pytest.fixture()
def sandbox(tmp_path):
    """A fake project checkout + isolated HERMES home."""
    proj = tmp_path / "fakeproj"
    proj.mkdir()
    subprocess.run(["git", "init", "-q", str(proj)], check=True)
    subprocess.run(
        ["git", "-C", str(proj), "remote", "add", "origin",
         "https://github.com/test-owner/fake-project.git"],
        check=True,
    )
    home = tmp_path / "hermes-home"
    home.mkdir()
    return proj, home


def _dry_run(proj, home, tmp_path, extra_args=()):
    # redirect HOME so even a buggy setup.sh cannot touch the real one
    env = dict(os.environ)
    env["HOME"] = str(home)
    env.pop("HERMES_HOME", None)
    out = subprocess.run(
        ["bash", str(SETUP), "--repo", "test-owner/fake-project",
         "--dir", str(proj), "--dry-run", *extra_args],
        env=env, capture_output=True, text=True, timeout=60,
        cwd=str(tmp_path),
    )
    return out


def test_dry_run_prints_plan_and_zero_writes(sandbox, tmp_path):
    proj, home = sandbox
    out = _dry_run(proj, home, tmp_path)
    assert out.returncode == 0, out.stderr
    assert "DRY RUN" in out.stdout
    assert "no changes will be made" in out.stdout
    # the plan names the real work
    assert "fakeproj-dev" in out.stdout
    assert "orchestrator" in out.stdout
    # ZERO writes: the isolated HOME stays pristine
    assert list(home.rglob("*")) == [], (
        f"--dry-run wrote into $HOME: {list(home.rglob('*'))}"
    )


def test_dry_run_does_not_require_gh_auth_or_hermes(sandbox, tmp_path):
    """Dry-run must not gate on runtime checks that real setup performs
    (gh auth, hermes profile commands) — it only prints the plan."""
    proj, home = sandbox
    out = _dry_run(proj, home, tmp_path)
    assert out.returncode == 0, out.stderr
    assert "hermes profile" not in out.stderr.lower()


def test_missing_args_still_fails(sandbox, tmp_path):
    proj, home = sandbox
    env = dict(os.environ)
    env["HOME"] = str(home)
    out = subprocess.run(
        ["bash", str(SETUP)],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 1
    assert "Usage" in out.stderr or "Usage" in out.stdout


def test_setup_sh_syntax():
    out = subprocess.run(["bash", "-n", str(SETUP)], capture_output=True,
                         text=True, timeout=30)
    assert out.returncode == 0, out.stderr
