#!/usr/bin/env python3
"""
update-roadmap-docs.py — Keeps roadmap documentation current automatically.

Two-track design (per October 2026 decisions):
  1. MERGED FEATURES (mechanical, fully automatic):
     - Reuse deploy-watch.py state-file pattern (last_pr_id).
     - Query merged PRs since last run; update:
       * ROADMAP.md        — Version History row (keyed by pyproject version) +
                             Completed Initiative rows for PRs closing GH issues
       * feature-map.md    — flip row to ✅ Ready when the row references the
                             closed GH issue; bump header version; recount summary
       * gap-analysis.md   — flip "- [ ]" to "- [x]" for resolved gaps
     - Bump header "Last Updated" and current version on ROADMAP.md.
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
  REPO  (owner/repo; if unset, parsed from the checkout's origin remote)
  WORKDIR (repo checkout root; if unset, resolved from the kanban board default_workdir)
  KB_VAULT (optional Obsidian vault for the docs mirror; unset = mirror disabled)
"""
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

PROFILE_STATE = Path.home() / ".hermes" / "profiles" / "orchestrator" / "state"
STATE_FILE = PROFILE_STATE / "roadmap_last_run.json"
KANBAN_DIR = Path(os.environ.get("KANBAN_DIR", str(Path.home() / ".hermes" / "kanban" / "boards")))


def _discover_workdir() -> Path:
    """Repo checkout root, resolved from the kanban board's default_workdir.
    Env override: WORKDIR (or KANBAN_REPO_DIR)."""
    override = os.environ.get("WORKDIR") or os.environ.get("KANBAN_REPO_DIR")
    if override:
        return Path(override)
    if KANBAN_DIR.is_dir():
        for board_dir in KANBAN_DIR.iterdir():
            board_json = board_dir / "board.json"
            if board_json.is_file():
                try:
                    meta = json.loads(board_json.read_text())
                    workdir = meta.get("default_workdir")
                    if workdir and Path(workdir).is_dir():
                        return Path(workdir)
                except Exception:
                    pass
    return Path.cwd()


def _discover_repo(root: Path) -> str:
    """owner/repo from the checkout's origin remote. Env override: REPO."""
    override = os.environ.get("REPO")
    if override:
        return override
    try:
        r = subprocess.run(["git", "remote", "get-url", "origin"],
                           capture_output=True, text=True, timeout=10, cwd=root)
        m = re.search(r"[:/]([^/:]+)/([^/]+?)(\.git)?$", (r.stdout or "").strip())
        if m:
            return f"{m.group(1)}/{m.group(2)}"
    except Exception:
        pass
    return ""


WORKDIR = _discover_workdir()
REPO = _discover_repo(WORKDIR)
PROJECT = REPO.split("/")[-1] if "/" in REPO else REPO
VAULT = Path(os.environ["KB_VAULT"]) if os.environ.get("KB_VAULT") else None

DOCS = [
    "ROADMAP.md",
    "docs/PRD/feature-map.md",
    "docs/PRD/implementation-roadmap.md",
    "docs/PRD/comprehensive-codebase-gap-analysis.md",
]
STALE_DAYS = 14  # warn if any doc not updated in this long

# GH issue labels that signal "upcoming feature work"
FEATURE_LABELS = ("feat", "epic", "ux", "enhancement")

GAP_FILE = "docs/PRD/comprehensive-codebase-gap-analysis.md"

STATUS_ORDER = {"✅ Ready": 0, "🟡 Partial": 1, "❌ Pending": 2, "🔵 Removed": 3}


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


# ---------------------------------------------------------------- merged track

def fetch_merged_prs(last_pr_id):
    """Return merged PRs newer than last_pr_id (by databaseId)."""
    rc, out, _ = run([
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
    return [p for p in prs if not last_pr_id or p.get("number") > last_pr_id]


def extract_closed_issue(body):
    """Return GH issue number closed by this PR body, or None."""
    if not body:
        return None
    for pat in (r"[\r\n#]closes?\s+#(\d+)", r"fixes?\s+#(\d+)", r"resolves?\s+#(\d+)"):
        m = re.search(pat, body, re.IGNORECASE)
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
    return set(m.group(1) for m in re.finditer(r"\| \*\*\[([^\]]+)\]\*\*", content))


def read_preserve_newlines(path):
    """Read file content + detect its newline style (CRLF vs LF)."""
    data = path.read_bytes()
    newline = "\r\n" if b"\r\n" in data else "\n"
    text = data.decode("utf-8-sig").replace("\r\n", "\n")
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
            (r[0] for r in rows),
            key=lambda v: [int(x) for x in re.findall(r"\d+", v)] or [0],
        )
        content = re.sub(
            r"Current version: \*\*[^*]+\*\*",
            f"Current version: **{newest}** (staging)",
            content,
            count=1,
        )
    m = re.search(
        r"(\| Version \| Date \| Features \|\n\|---------\|------\|----------\|\n)",
        content,
    )
    if not m:
        return False
    insert_at = m.end()
    new_rows = "\n".join(f"| **[{v}]** | {d} | {s} |" for v, d, s in rows)
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
    if f"issues/{issue_num}" in content:
        return False
    title = re.sub(
        r"^(feat|fix|chore|docs|test|refactor)\([^)]*\):\s*", "",
        pr_row.get("title", ""),
    ).strip()
    if len(title) > 80:
        title = title[:77] + "..."
    month = datetime.now(timezone.utc).strftime("%b %Y")
    m = re.search(
        r"(\| Initiative \| GH Issue \| Completed \| Summary \|\n\|------------\|----------\|-----------\|---------\|\n)",
        content,
    )
    if not m:
        return False
    row = f"| {title} | [#{issue_num}](https://github.com/{REPO}/issues/{issue_num}) | {month} | {pr_row.get('title','')} |"
    content = content[: m.end()] + row + "\n" + content[m.end():]
    write_preserve_newlines(path, content, newline)
    return True


def update_feature_map(prs, root):
    """Flip feature-map rows to ✅ Ready when a merged PR closes the GH issue
    the row references (e.g. 'GH-6035' in notes). Bumps header version and
    recomputes the Summary table."""
    fm = root / "docs" / "PRD" / "feature-map.md"
    if not fm.exists():
        return []
    content, newline = read_preserve_newlines(fm)

    changed = []
    lines = content.split("\n")
    for i, line in enumerate(lines):
        m = re.match(
            r"(\| .*? \| )(✅ Ready|🟡 Partial|❌ Pending|🔵 Removed)( \|.*)$",
            line,
        )
        if not m:
            continue
        prefix, status, suffix = m.group(1), m.group(2), m.group(3)
        for p in prs:
            n = extract_closed_issue(p.get("body"))
            if n and f"GH-{n}" in line and status != "✅ Ready":
                new_suffix = re.sub(
                    r"\s*\|\s*$",
                    f" — auto-ready via PR #{p['number']} |",
                    suffix,
                )
                lines[i] = f"{prefix}✅ Ready{new_suffix}"
                changed.append(f"GH-{n}")
                break

    version = current_version(root)
    if version:
        for i, line in enumerate(lines):
            if line.startswith("# ") and line.rstrip().endswith("Feature Map"):
                lines[i] = f"# {PROJECT} Feature Map — v{version}"
                break

    content = "\n".join(lines)
    content = recompute_summary(content)
    if changed or version:
        write_preserve_newlines(fm, content, newline)
    return changed


def recompute_summary(content):
    """Recount per-area statuses and rewrite the Summary table + health line."""
    lines = content.split("\n")
    areas = {}
    area_order = []
    current_area = None

    for line in lines:
        if line.startswith("## "):
            current_area = None
            if line.startswith("## Summary"):
                continue
            if line.startswith("## Step") or "Platform Features" in line:
                current_area = re.sub(r" \(.*\)$", "", line[3:].strip())
                areas.setdefault(current_area, [0, 0, 0, 0])
                if current_area not in area_order:
                    area_order.append(current_area)
            continue
        if current_area and line.startswith("|"):
            m = re.match(
                r"\| (.*?) \| (✅ Ready|🟡 Partial|❌ Pending|🔵 Removed) \|",
                line,
            )
            if m:
                idx = STATUS_ORDER[m.group(2)]
                areas[current_area][idx] += 1

    total = [sum(areas[a][i] for a in area_order) for i in range(4)]
    summary_rows = []
    for area in area_order:
        c = areas[area]
        summary_rows.append(f"| {area} | {c[0]} | {c[1]} | {c[2]} | {c[3]} |")
    summary_rows.append(
        f"| **Total** | **{total[0]}** | **{total[1]}** | **{total[2]}** | **{total[3]}** |"
    )
    ready_pct = round(100 * total[0] / max(1, sum(total[:3])))
    health = (
        f"**Health: {total[0]}/{sum(total[:3])} features ready ({ready_pct}%)**"
        f" — {total[1] + total[2]} items need work, {total[3]} removed from scope."
    )

    out = []
    in_summary = False
    for line in lines:
        if line.startswith("## Summary"):
            in_summary = True
            out.append("## Summary")
            out.append("")
            out.append("| Area | Ready | Partial | Pending | Removed |")
            out.append("|------|-------|---------|---------|---------|")
            out.extend(summary_rows)
            out.append("")
            out.append(health)
            continue
        if in_summary:
            continue
        out.append(line)
    return "\n".join(out)


def update_gap_analysis(prs, root):
    """Mark gap-analysis rows resolved when a merged PR closes a referenced GH
    issue. Handles the prioritization table (P1–P9 rows referencing GH-N) by
    appending '(auto-resolved via PR #N)' to the Notes column and updating the
    header status line."""
    gap_path = root / GAP_FILE
    if not gap_path.exists():
        return []
    content, newline = read_preserve_newlines(gap_path)

    # Map each closed GH issue -> merged PR number
    closed = {}
    for p in prs:
        n = extract_closed_issue(p.get("body"))
        if n:
            closed[n] = p["number"]
    if not closed:
        return []

    changed = []
    lines = content.split("\n")
    for i, line in enumerate(lines):
        if not line.startswith("| P"):
            continue
        for n, pr_num in closed.items():
            if f"GH-{n}" in line and "(auto-resolved" not in line:
                lines[i] = line.rstrip() + f" — auto-resolved via PR #{pr_num} |"
                changed.append(f"GH-{n}")
                break

    # Update the header status line (e.g. '9 of 15 ... — 4 remaining + 2 new items')
    if changed:
        m = re.search(r"(\*\*Status:\*\*) [^*]+(\*\*)", "\n".join(lines[:8]))
        if m:
            new_status = f"**Status:** updated by auto-updater — " + ", ".join(f"GH-{n} resolved" for n in closed) + "**"
            lines[6] = new_status

    if changed:
        write_preserve_newlines(gap_path, "\n".join(lines), newline)
    return changed


def commit_and_push(root, message):
    # SAFETY: verify we're pushing to the right repo before touching origin.
    rc, out, _ = run(["git", "remote", "get-url", "origin"], cwd=root)
    if rc != 0 or (REPO not in out and PROJECT not in out):
        return False
    rc, branch_out, _ = run(["git", "branch", "--show-current"], cwd=root)
    if rc != 0 or branch_out.strip() != "main":
        return False
    rc, out, _ = run(
        ["git", "add", "ROADMAP.md", "docs/PRD/feature-map.md", GAP_FILE],
        cwd=root,
    )
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
    """Copy ROADMAP.md + docs/PRD to the Obsidian vault (standing instruction).
    Requires KB_VAULT to be set; silently skipped otherwise (env-driven)."""
    if VAULT is None or not VAULT.exists():
        return False
    try:
        (VAULT / "docs").mkdir(parents=True, exist_ok=True)
        (VAULT / "docs" / "PRD").mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / "ROADMAP.md", VAULT / "ROADMAP.md")
        for f in (root / "docs" / "PRD").glob("*.md"):
            shutil.copy2(f, VAULT / "docs" / "PRD" / f.name)
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
    if VAULT is None:
        return False
    # SAFETY: verify we're in the KB vault repo, not the project checkout (belt-and-braces).
    rc, out, _ = run(["git", "remote", "get-url", "origin"], cwd=VAULT)
    if rc != 0 or Path(VAULT).name not in out:
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
    gap_file = root / GAP_FILE
    if not gap_file.exists():
        return []
    content = gap_file.read_text(encoding="utf-8")
    return [g.strip() for g in re.findall(r"- \[ \]\s+(.+)", content)]


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
            last = datetime.fromisoformat(out.strip())
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

    new_prs = fetch_merged_prs(state.get("last_pr_id"))
    notes = []
    changes_made = False

    # ---- merged track: update ROADMAP.md + feature-map + gap-analysis
    if new_prs:
        version = current_version(root) or "next"
        merged_date = new_prs[-1].get("mergedAt", "")[:10]
        refs = ", ".join(f"#{p['number']}" for p in new_prs)
        if len(new_prs) > 3:
            feature_summary = f"+{len(new_prs)} merged PRs ({refs})"
        else:
            parts = []
            for p in new_prs:
                t = re.sub(r"^(feat|fix|chore|docs|test|refactor)\([^)]*\):\s*", "", p.get("title", "")).strip()
                if t:
                    parts.append(t)
            feature_summary = "; ".join(parts)[:140] + f" ({refs})"

        content_check, _ = read_preserve_newlines(root / "ROADMAP.md")
        if version not in known_version_rows(content_check):
            if append_to_roadmap([(version, merged_date, feature_summary)], root):
                notes.append(f"ROADMAP: +{version} ({len(new_prs)} merged PRs)")
                changes_made = True
        for p in new_prs:
            if append_completed_initiative(p, root):
                notes.append(f"Completed: #{extract_closed_issue(p.get('body'))}")
                changes_made = True

        fm_changed = update_feature_map(new_prs, root)
        if fm_changed:
            notes.append(f"Feature map ready: {', '.join(fm_changed)}")
            changes_made = True
        gap_changed = update_gap_analysis(new_prs, root)
        if gap_changed:
            notes.append(f"Gap analysis resolved: {', '.join(gap_changed)}")
            changes_made = True

        if changes_made:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if commit_and_push(root, f"docs: roadmap auto-update — {len(new_prs)} merged PRs ({today})"):
                notes.append("repo pushed")
            if mirror_to_vault(root):
                if commit_and_push_vault(f"docs: roadmap auto-update mirror ({today})"):
                    notes.append("vault mirrored")
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