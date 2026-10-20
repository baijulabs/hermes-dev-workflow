#!/usr/bin/env python3
"""
update-roadmap-docs.py — Keeps ROADMAP.md and docs/PRD/ roadmap docs current.

Two-track design (per July/October 2026 decisions):
  1. MERGED FEATURES (mechanical, fully automatic):
     - Reuse deploy-watch.py state-file pattern (last_run_id).
     - Query merged PRs since last run; append "Version History" rows + update
       "Completed Initiatives" for any PR that closes a GH issue.
     - Bump header "Last Updated" and version when new rows land.
  2. UPCOMING ROADMAP (semi-automated, curator-assisted):
     - NEVER invents priorities. Generates a "candidate pool" digest:
       * open feat/epic/ux GH issues
       * unresolved PRD gaps (parsed from comprehensive-codebase-gap-analysis.md)
       * ready + in_progress kanban cards
     - Posts ONE actionable Telegram line when NEW candidates appear or docs
       go stale. Silent otherwise.

State file: ~/.hermes/profiles/orchestrator/state/roadmap_last_run.json
Output to stdout (JSON payload) consumed by the cron deliver mechanism.
Exit code 0 always; silent (empty stdout) when nothing to report.

Environment:
  REPO  (default baijulabs/Liberkyma)
  WORKDIR (default ~/Liberkyma)
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = os.environ.get("REPO", "baijulabs/Liberkyma")
PROFILE_STATE = Path.home() / ".hermes" / "profiles" / "orchestrator" / "state"
STATE_FILE = PROFILE_STATE / "roadmap_last_run.json"
WORKDIR = Path(os.environ.get("WORKDIR", str(Path.home() / "Liberkyma")))
VAULT = Path("/mnt/c/Users/julia/Obsidian Vaults/Liberkyma_KB")

DOCS = [
    "ROADMAP.md",
    "docs/PRD/feature-map.md",
    "docs/PRD/implementation-roadmap.md",
    "docs/PRD/comprehensive-codebase-gap-analysis.md",
]
STALE_DAYS = 14  # warn if any doc not updated in this long

# GH issue labels that signal "upcoming feature work"
FEATURE_LABELS = ("feat", "epic", "ux", "enhancement")


def run(cmd, timeout=30, cwd=None):
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd
        )
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return -1, "", str(e)


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"last_pr_id": None, "known_candidates": [], "created_at": None}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------- merged track

def fetch_merged_prs(last_pr_id):
    """Return merged PRs newer than last_pr_id (by databaseId)."""
    rc, out, err = run([
        "gh", "pr", "list", "--repo", REPO, "--state", "merged",
        "--limit", "50", "--json",
        "number,title,mergedAt,mergeCommit,body,labels,headRefName",
    ])
    if rc != 0:
        return []
    try:
        prs = json.loads(out)
    except json.JSONDecodeError:
        return []
    prs.sort(key=lambda p: p.get("mergedAt", ""))
    new_prs = []
    for p in prs:
        pid = p.get("number")
        if last_pr_id and pid <= last_pr_id:
            continue
        new_prs.append(p)
    return new_prs


def extract_closed_issue(body):
    """Return GH issue number closed by this PR body, or None."""
    if not body:
        return None
    m = re.search(r"[\r\n#]closes?\s+#(\d+)", body, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r"fixes?\s+#(\d+)", body, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


def current_version(root):
    """Read the current app version from pyproject.toml (CI bumps per release)."""
    pp = root / "backend" / "pyproject.toml"
    if pp.exists():
        for line in pp.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("version"):
                m = re.search(r"[\"']([^\"']+)[\"']", line)
                if m:
                    return m.group(1)
    pj = root / "package.json"
    if pj.exists():
        try:
            return json.loads(pj.read_text(encoding="utf-8")).get("version")
        except Exception:
            pass
    return None


def known_version_rows(content):
    """Return set of version labels already present in the Version History table."""
    existing = set()
    for m in re.finditer(r"\| \*\*\[([^\]]+)\]\*\*", content):
        existing.add(m.group(1))
    return existing


def merged_version_rows(prs, root):
    """Group merged PRs since last run into ONE row keyed by current app version.
    Matches ROADMAP convention: | [0.83.0] | Date | feature summary | """
    version = current_version(root) or "next"
    if len(prs) > 3:
        summary = f"+{len(prs)} merged PRs"
    else:
        summary = "; ".join(
            re.sub(r"^(feat|fix|chore|docs|test|refactor)\([^)]*\):\s*", "", p.get("title", "")).strip()
            for p in prs
            if p.get("title")
        )
        summary = summary[:140]
    merged_date = prs[-1].get("mergedAt", "")[:10] if prs else datetime.now(timezone.utc).strftime("%Y-%m-%d")
    refs = ", ".join(f"#{p['number']}" for p in prs)
    return version, merged_date, f"{summary} ({refs})"


def read_preserve_newlines(path):
    """Read file content + detect its newline style (CRLF vs LF)."""
    data = path.read_bytes()
    newline = "\r\n" if b"\r\n" in data else "\n"
    # decode respecting the detected newline
    text = data.decode("utf-8-sig")
    text = text.replace("\r\n", "\n")  # normalize
    return text, newline


def write_preserve_newlines(path, text, newline):
    path.write_bytes(text.replace("\n", newline).encode("utf-8"))


def append_to_roadmap(rows, root):
    """Insert merged rows at the top of Version History table in ROADMAP.md."""
    path = root / "ROADMAP.md"
    content, newline = read_preserve_newlines(path)

    # Update header "Last Updated" + Current version
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    content = re.sub(
        r"> Last Updated: [^·]*·",
        f"> Last Updated: {today} ·",
        content,
        count=1,
    )
    if rows:
        newest = max(
            (r[0] for r in rows), key=lambda v: [int(x) for x in re.findall(r"\d+", v)] or [0]
        )
        content = re.sub(
            r"Current version: \*\*[^*]+\*\*",
            f"Current version: **{newest}** (staging)",
            content,
            count=1,
        )
    # Insert after the table header separator line (|---------|------|----------|)
    # Find the Version History table start
    m = re.search(
        r"(\| Version \| Date \| Features \|\n\|---------\|------\|----------\|\n)",
        content,
    )
    if not m:
        return False
    insert_at = m.end()
    new_rows = "\n".join(
        f"| **[{v}]** | {d} | {s} |" for v, d, s in rows
    )
    content = content[:insert_at] + new_rows + "\n" + content[insert_at:]
    write_preserve_newlines(path, content, newline)
    return True


def append_completed_initiative(pr_row, root):
    """Add one row to Completed Initiatives when PR closes a GH issue."""
    path = root / "ROADMAP.md"
    content, newline = read_preserve_newlines(path)
    issue_num = extract_closed_issue(pr_row.get("body"))
    if not issue_num:
        return False
    # Check it's not already there
    if f"issues/{issue_num}" in content:
        return False
    title = re.sub(
        r"^(feat|fix|chore|docs|test|refactor)\([^)]*\):\s*", "",
        pr_row.get("title", ""),
    ).strip()
    if len(title) > 80:
        title = title[:77] + "..."
    month = datetime.now(timezone.utc).strftime("%b %Y")
    # Insert after Completed Initiatives header separator
    m = re.search(
        r"(\| Initiative \| GH Issue \| Completed \| Summary \|\n\|------------\|----------\|-----------\|---------\|\n)",
        content,
    )
    if not m:
        return False
    row = f"| {title} | [#{issue_num}](https://github.com/baijulabs/Liberkyma/issues/{issue_num}) | {month} | {pr_row.get('title','')} |"
    content = content[: m.end()] + row + "\n" + content[m.end():]
    write_preserve_newlines(path, content, newline)
    return True


def commit_and_push(root, message):
    # SAFETY: verify we're pushing to the right repo before touching origin.
    # (A prior session accidentally pointed Liberkyma's origin at Liberkyma_KB
    #  and a doc-update push would have sailed into the vault repo.)
    rc, out, _ = run(["git", "remote", "get-url", "origin"], cwd=root)
    if rc != 0 or (REPO not in out and REPO.replace("baijulabs/", "") not in out):
        return False
    rc, branch_out, _ = run(["git", "branch", "--show-current"], cwd=root)
    if rc != 0 or branch_out.strip() != "main":
        return False
    rc, out, _ = run(["git", "add", "ROADMAP.md", "docs/PRD/"], cwd=root)
    if rc != 0:
        return False
    rc, out, _ = run(["git", "diff", "--cached", "--quiet"], cwd=root)
    if rc == 0:
        return False  # nothing staged
    rc, out, _ = run(["git", "commit", "-m", message], cwd=root)
    if rc != 0:
        return False
    # Pull-rebase before push (CI auto-bumps / other processes land between runs)
    rc, out, _ = run(["git", "pull", "--rebase", "origin", "main"], cwd=root, timeout=60)
    if rc != 0:
        return False
    rc, out, _ = run(["git", "push", "origin", "main"], cwd=root, timeout=60)
    return rc == 0


def mirror_to_vault(root):
    """Copy ROADMAP.md + docs/PRD to the Obsidian vault (standing instruction)."""
    if not VAULT.exists():
        return False
    try:
        (VAULT / "docs").mkdir(parents=True, exist_ok=True)
        (VAULT / "docs" / "PRD").mkdir(parents=True, exist_ok=True)
        import shutil
        shutil.copy2(root / "ROADMAP.md", VAULT / "ROADMAP.md")
        for f in (root / "docs" / "PRD").glob("*.md"):
            shutil.copy2(f, VAULT / "docs" / "PRD" / f.name)
        # Update vault index date
        idx = VAULT / "docs" / "Current_PRD_Index.md"
        if idx.exists():
            content = idx.read_text(encoding="utf-8")
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            content = re.sub(
                r"> \*\*Last updated:\*\* [^*]*",
                f"> **Last updated:** {today}",
                content,
                count=1,
            )
            idx.write_text(content, encoding="utf-8")
        return True
    except Exception:
        return False


def commit_and_push_vault(message):
    # SAFETY: verify we're in the KB vault repo, not Liberkyma (belt-and-braces).
    rc, out, _ = run(["git", "remote", "get-url", "origin"], cwd=VAULT)
    if rc != 0 or "Liberkyma_KB" not in out:
        return False
    rc, out, _ = run(["git", "add", "-A"], cwd=VAULT)
    if rc != 0:
        return False
    rc, out, _ = run(["git", "diff", "--cached", "--quiet"], cwd=VAULT)
    if rc == 0:
        return False
    rc, out, _ = run(["git", "commit", "-m", message], cwd=VAULT)
    if rc != 0:
        return False
    # Pull-rebase before push (Octosync auto-commits land between runs)
    rc, out, _ = run(["git", "pull", "--rebase", "origin", "main"], cwd=VAULT, timeout=60)
    if rc != 0:
        return False
    rc, out, _ = run(["git", "push", "origin", "main"], cwd=VAULT, timeout=60)
    return rc == 0


# ------------------------------------------------------------ upcoming track

def fetch_open_feature_issues():
    rc, out, _ = run([
        "gh", "issue", "list", "--repo", REPO, "--state", "open",
        "--limit", "50", "--json", "number,title,labels",
    ])
    if rc != 0:
        return []
    try:
        issues = json.loads(out)
    except json.JSONDecodeError:
        return []
    return [
        {"id": i["number"], "title": i["title"],
         "labels": [l["name"] for l in i.get("labels", [])]}
        for i in issues
        if any(l["name"] in FEATURE_LABELS for l in i.get("labels", []))
    ]


def fetch_unresolved_gaps(root):
    gap_file = root / "docs" / "PRD" / "comprehensive-codebase-gap-analysis.md"
    if not gap_file.exists():
        return []
    content = gap_file.read_text(encoding="utf-8")
    gaps = re.findall(r"- \[ \]\s+(.+)", content)
    return [g.strip() for g in gaps]


def fetch_kanban_ready():
    rc, out, _ = run(["hermes", "kanban", "list", "--status", "ready"])
    if rc != 0:
        return []
    ready = []
    for line in out.split("\n"):
        line = line.strip()
        if not line:
            continue
        m = re.match(r"✓\s+(\S+)\s+\S+\s+\S+\s+(.+)", line)
        if m:
            ready.append({"id": m.group(1), "title": m.group(2)})
    return ready


def doc_staleness(root):
    """Return stale docs (last modified > STALE_DAYS ago)."""
    stale = []
    for rel in DOCS:
        p = root / rel
        if not p.exists():
            stale.append((rel, "missing"))
            continue
        rc, out, _ = run(["git", "log", "-1", "--format=%ci", "--", rel], cwd=root)
        if rc != 0 or not out:
            continue
        try:
            from datetime import datetime as dt
            last = dt.fromisoformat(out.strip())
            age_days = (datetime.now(timezone.utc) - last.replace(tzinfo=timezone.utc)).days
        except Exception:
            age_days = 0
        if age_days >= STALE_DAYS:
            stale.append((rel, f"{age_days}d"))
    return stale


def main():
    state = load_state()
    root = WORKDIR
    if not (root / "ROADMAP.md").exists():
        print(json.dumps({"error": "WORKDIR missing ROADMAP.md"}))
        return

    last_pr_id = state.get("last_pr_id")
    new_prs = fetch_merged_prs(last_pr_id)
    notes = []
    changes_made = False

    # ---- merged track: update ROADMAP.md
    if new_prs:
        version, merged_date, feature_summary = merged_version_rows(new_prs, root)
        content_check, _ = read_preserve_newlines(root / "ROADMAP.md")
        if version not in known_version_rows(content_check):
            if append_to_roadmap(
                [(version, merged_date, feature_summary)], root
            ):
                notes.append(f"Version History: +{version} ({len(new_prs)} merged PRs)")
                changes_made = True
        for p in new_prs:
            if append_completed_initiative(p, root):
                notes.append(f"Completed Initiative: #{extract_closed_issue(p.get('body'))}")
                changes_made = True
        # commit + push
        if changes_made:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if commit_and_push(root, f"docs: roadmap auto-update — {len(new_prs)} merged PRs ({today})"):
                notes.append("repo pushed")
            if mirror_to_vault(root):
                if commit_and_push_vault(f"docs: roadmap auto-update mirror ({today})"):
                    notes.append("vault mirrored")
        # advance state
        state["last_pr_id"] = max(p["number"] for p in new_prs)

    # ---- upcoming track: candidate pool + freshness
    candidates = fetch_open_feature_issues()
    gaps = fetch_unresolved_gaps(root)
    ready_cards = fetch_kanban_ready()

    known = set(state.get("known_candidates", []))
    new_candidates = [c for c in candidates if c["id"] not in known]
    for c in candidates:
        known.add(c["id"])
    state["known_candidates"] = sorted(known)

    stale = doc_staleness(root)

    # ---- build output digest (actionable only)
    digest_lines = []
    if new_candidates:
        digest_lines.append(
            f"📋 Roadmap candidates: +{len(new_candidates)} new open feature issue(s): "
            + ", ".join(f"#{c['id']}" for c in new_candidates[:5])
        )
    if notes:
        digest_lines.append("🔄 " + " · ".join(notes))
    if stale and not new_candidates and not notes:
        stale_str = ", ".join(f"{rel} ({age})" for rel, age in stale)
        digest_lines.append(f"⚠️ Roadmap docs stale: {stale_str}")
    if not digest_lines:
        save_state(state)
        return  # silent

    save_state(state)
    print(json.dumps({"digest": " | ".join(digest_lines),
                      "new_prs": len(new_prs),
                      "new_candidates": len(new_candidates),
                      "unresolved_gaps": len(gaps),
                      "ready_cards": len(ready_cards),
                      "stale_docs": len(stale)}))


if __name__ == "__main__":
    main()