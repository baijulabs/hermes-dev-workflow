#!/usr/bin/env python3
"""
hermes-config-sync.py — Mirror Hermes agent config into the project repo.

Syncs critical agent configuration from ~/.hermes/profiles/ into
hermes-config/ in the repo. Excludes secrets, state DBs, and generated
artifacts. Designed for disaster recovery: clone the repo, run restore.sh.

Profiles synced: orchestrator, qa, coder, code-reviewer
What's synced:   SOUL.md, cron/jobs.json, scripts/, skills/ (no venvs)
What's excluded: .env, auth.json, state.db, sessions, kanban DBs, venvs

Usage:
  python3 hermes-config-sync.py          # sync, report changes
  python3 hermes-config-sync.py --commit # sync + git commit + push
"""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERMES = Path.home() / ".hermes"
REPO = Path("${HERMES_PROJECT_DIR:-/home/user/MyProject}")
TARGET = REPO / "hermes-config"

PROFILES = ["orchestrator", "qa", "coder", "code-reviewer"]

# Files/dirs to sync per profile (relative to profile root)
SYNC_ITEMS = [
    "SOUL.md",
    "cron",
    "scripts",
    "skills",
]

# Files to sync from Hermes root
ROOT_ITEMS = [
    "config.yaml",
]


def run(cmd, cwd=None, timeout=30):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return -1, "", str(e)


def copy_dir(src, dst):
    """Copy directory, excluding bloat. Returns True if any content changed."""
    # Exclude patterns
    exclude = {"__pycache__", ".pyc", "venv", "venv_", "node_modules", ".git", "state-snapshots"}
    # Cron runtime files that change every tick — only sync jobs.json
    cron_state = {"ticker_heartbeat", "ticker_last_success", "output", "executions.db"}
    
    def ignore_func(directory, files):
        ignored = set()
        for f in files:
            if any(f.startswith(p) or f.endswith(p) for p in exclude):
                ignored.add(f)
        return ignored
    
    # Content-aware sync: only copy files that actually changed
    changed = False
    for src_file in src.rglob("*"):
        if src_file.is_dir():
            continue
        rel = src_file.relative_to(src)
        # Skip excluded patterns
        if any(rel.parts[0].startswith(p) or str(rel).endswith(p) for p in exclude):
            continue
        if rel.parts[0] == "cron" and rel.name in cron_state:
            continue
        dst_file = dst / rel
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        
        src_content = src_file.read_bytes()
        dst_content = dst_file.read_bytes() if dst_file.exists() else b""
        
        if src_content != dst_content:
            dst_file.write_bytes(src_content)
            changed = True
    
    # Remove files in dst that don't exist in src
    for dst_file in dst.rglob("*") if dst.exists() else []:
        if dst_file.is_dir():
            continue
        rel = dst_file.relative_to(dst)
        if not (src / rel).exists():
            dst_file.unlink()
            changed = True
    
    return changed


def sync_profile(profile_name):
    """Sync one profile from ~/.hermes to repo."""
    src_profile = HERMES / "profiles" / profile_name
    dst_profile = TARGET / "profiles" / profile_name
    
    if not src_profile.exists():
        return 0
    
    changed = 0
    for item in SYNC_ITEMS:
        src = src_profile / item
        dst = dst_profile / item
        
        if not src.exists():
            continue
        
        if src.is_dir():
            if copy_dir(src, dst):
                changed += 1
        elif src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            current = dst.read_text() if dst.exists() else ""
            new = src.read_text()
            if current != new:
                dst.write_text(new)
                changed += 1
    
    return changed


def sync_root():
    """Sync root config files."""
    changed = 0
    for item in ROOT_ITEMS:
        src = HERMES / item
        dst = TARGET / item
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            current = dst.read_text() if dst.exists() else ""
            new = src.read_text()
            if current != new:
                dst.write_text(new)
                changed += 1
    return changed


def write_restore_script():
    """Generate restore.sh that copies everything back into ~/.hermes."""
    script = TARGET / "restore.sh"
    lines = [
        "#!/bin/bash",
        "# hermes-config/restore.sh — Restore Hermes agent config from repo backup",
        "# Run from the project repo root. Secrets (.env, auth.json) must be",
        "# restored manually from a password manager or secure backup.",
        "set -euo pipefail",
        "",
        "SCRIPT_DIR=\"$(cd \"$(dirname \"${BASH_SOURCE[0]}\")\" && pwd)\"",
        "HERMES=\"$HOME/.hermes\"",
        "",
        'echo "=== Restoring Hermes agent configuration ==="',
        'echo ""',
        "",
        "# Root config",
        'if [ -f "$SCRIPT_DIR/config.yaml" ]; then',
        '    cp "$SCRIPT_DIR/config.yaml" "$HERMES/config.yaml"',
        '    echo "✓ config.yaml"',
        "fi",
        "",
        "# Profiles",
        'for profile in orchestrator qa coder code-reviewer; do',
        '    src="$SCRIPT_DIR/profiles/$profile"',
        '    dst="$HERMES/profiles/$profile"',
        '    if [ -d "$src" ]; then',
        '        mkdir -p "$dst"',
        '        # SOUL.md',
        '        [ -f "$src/SOUL.md" ] && cp "$src/SOUL.md" "$dst/" && echo "✓ $profile/SOUL.md"',
        '        # cron',
        '        [ -d "$src/cron" ] && cp -r "$src/cron" "$dst/" && echo "✓ $profile/cron/"',
        '        # scripts',
        '        [ -d "$src/scripts" ] && cp -r "$src/scripts" "$dst/" && echo "✓ $profile/scripts/"',
        '        # skills',
        '        if [ -d "$src/skills" ]; then',
        '            mkdir -p "$dst/skills"',
        '            cp -r "$src/skills/"* "$dst/skills/" 2>/dev/null && echo "✓ $profile/skills/"',
        "        fi",
        "    fi",
        "done",
        "",
        "# Make scripts executable",
        'find "$HERMES/profiles" -name "*.py" -o -name "*.sh" | xargs chmod +x 2>/dev/null || true',
        "",
        'echo ""',
        'echo "=== Restore complete ==="',
        'echo "⚠️  Manual steps needed:"',
        'echo "   1. Restore .env files from password manager (~/.hermes/.env, ~/.hermes/profiles/*/.env)"',
        'echo "   2. Restore auth.json from backup (~/.hermes/auth.json)"',
        'echo "   3. Run: hermes gateway restart"',
    ]
    current = script.read_text() if script.exists() else ""
    new = "\n".join(lines) + "\n"
    if current != new:
        script.write_text(new)
        script.chmod(0o755)
        return 1
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", action="store_true", help="Git commit and push changes")
    args = parser.parse_args()

    TARGET.mkdir(parents=True, exist_ok=True)

    total_changed = 0
    total_changed += sync_root()
    for profile in PROFILES:
        changed = sync_profile(profile)
        if changed:
            print(f"  {profile}: {changed} file(s) updated")
        total_changed += changed
    
    total_changed += write_restore_script()

    if total_changed == 0:
        print("No changes detected.")
        return

    print(f"\n{total_changed} item(s) synced to {TARGET}")

    if args.commit:
        # Check if there are actual git changes
        rc, out, _ = run(["git", "status", "--porcelain", "hermes-config/"], cwd=REPO)
        if not out.strip():
            print("No git changes to commit.")
            return
        
        run(["git", "add", "hermes-config/"], cwd=REPO)
        rc, _, err = run([
            "git", "commit", "-m",
            f"chore: sync hermes agent config ({total_changed} files updated) [skip ci]"
        ], cwd=REPO)
        if rc == 0:
            rc_push, _, push_err = run(["git", "push", "origin", "main"], cwd=REPO, timeout=60)
            if rc_push == 0:
                print("✓ Committed and pushed to main")
            else:
                # Main moved — pull and retry once
                run(["git", "pull", "origin", "main", "--rebase"], cwd=REPO, timeout=30)
                rc_push2, _, _ = run(["git", "push", "origin", "main"], cwd=REPO, timeout=60)
                if rc_push2 == 0:
                    print("✓ Pushed to main (after rebase)")
                else:
                    print(f"⚠ Push failed after retry: {push_err}")
        else:
            print(f"Commit skipped: {err}")


if __name__ == "__main__":
    main()
