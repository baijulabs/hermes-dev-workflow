# Hermes Agent Development Workflow

A portable, automated development pipeline powered by Hermes Agent multi-profile kanban. Bootstraps issue ingestion, parallel coder+reviewer implementation, PR consolidation, merge, and QA verification onto any project.

## Quick Start

```bash
git clone https://github.com/baijulabs/hermes-dev-workflow.git
cd hermes-dev-workflow
./setup.sh --repo "owner/project" --dir "/path/to/project"
```

Edit secrets, start the gateway, and label an issue `ready-for-agent`. The pipeline picks it up within minutes.

## Pipeline

### 👑 Happy Path (GH Issue → Staging Deploy)

```
GH issue (labeled ready-for-agent)
        │
        ▼  every 5m
┌──────────────────────────────────────────────┐
│  01-ingest · ingest-gh-issues                │
│  Syncs labeled issues → orchestrator kanban  │
│  card. Ingestion only — never closes issues. │
└──────────────────────────────────────────────┘
        │
        ▼  auto-decompose → coder + reviewer cards
┌──────────────────────────────────────────────┐
│  02-queue · queue-agent-processor            │
│  Dispatches coder (implements) + reviewer    │
│  (approves) from kanban board.               │
│  Schedule: every 30m (7am-9pm)               │
└──────────────────────────────────────────────┘
        │  coder done + reviewer approved
        ▼  every 5m
┌──────────────────────────────────────────────┐
│  03-build · build-consolidate-prs            │
│  Finds done coder+reviewer pairs on same GH  │
│  issue → merges worktrees → creates          │
│  consolidation PR. Auto-adds Closes #N.      │
│  0-commits check → auto-archive if content   │
│  already on main.                            │
└──────────────────────────────────────────────┘
        │  PR created → CI triggers
        ▼  every 10m
┌──────────────────────────────────────────────┐
│  04-merge · merge-ready-prs                  │
│  Merges PRs where mergeable=MERGEABLE AND    │
│  mergeStateStatus=clean (all CI green).      │
│  Uses --merge (preserves branch hashes on    │
│  main). Single version bump after all merges.│
│  Deploy cooldown gate prevents overlapping.  │
└──────────────────────────────────────────────┘
        │  PR merged to main → deploy.yml triggers
        ▼  every 15m
┌──────────────────────────────────────────────┐
│  01-ingest · ingest-deploy-failures          │
│  Monitors main deploy runs at job-level.     │
│  Detects Deploy to Staging failures →        │
│  creates fix cards. Dedup via kanban board.  │
└──────────────────────────────────────────────┘
        │  deploy succeeds
        ▼  every 10m
┌──────────────────────────────────────────────┐
│  06-verify · verify-deploy-qa                │
│  Validates staging health, critical flows,   │
│  maps changed files → E2E steps. Creates     │
│  GH issues for bugs found.                   │
└──────────────────────────────────────────────┘
```

### ⟳ Parallel Detection

```
┌──────────────────────────────────────────────┐
│  01-ingest · ingest-ci-failures (every 5m)   │
│  Monitors open PRs for CI failures and merge │
│  conflicts. Enqueues fix tasks → agent       │
│  processor creates coder+reviewer cards.     │
│  Conflict resolution pushed directly to the  │
│  existing PR branch (no new PR created).     │
└──────────────────────────────────────────────┘
```

### Edge Case Handlers (every 5m, 03-build phase)

| Job | Handles |
|-----|---------|
| `build-reviewer-resolve` | Auto-resolves `review-failed:` blocked reviewer cards. Extracts findings, creates replacement coder+reviewer pair. |
| `build-reviewer-approve` | Auto-completes reviewer cards when coder is done. |
| `build-coder-resolve` | Handles coder timeouts and `review-required:` blocks. |

### 🧹 Hygiene (Maintenance)

| Job | Schedule | Purpose |
|-----|----------|---------|
| `audit-stranded-worktrees` | every 2h | Flags worktree branches with unique commits not on main and no pending kanban work. Creates triage GH issue. |
| `audit-worktree-collisions` | every 5m | Detects worktree branch conflicts (same branch checked out by multiple worktrees). |
| `audit-pr-guard` | every 5m | Enforces PR guard conditions (main-tip requirement, no workflow_dispatch from non-main branches). |
| `audit-archive-cancelled` | every 15m | Archives cancelled kanban cards, cleans up stale state. |
| `audit-prune-worktrees` | every 48h | Deletes stale .worktrees/ directories older than 7 days. |
| `audit-kanban-health` | every 3h | Kanban DB integrity checks (SQLite integrity_check). |
| `sync-gh-comments` | every 5m | Posts milestone comments to GH issues: decomposed → coder done → reviewer approved → PR created. |
| `verify-dogfood` | weekly (Sat 8am) | Full exploratory QA session on staging — walks critical workflows, creates issues for bugs. |
| `cfg-config-sync` | every 60m | Internal: syncs Hermes configuration files across profiles. |

## Profiles

| Profile | Role |
|---------|------|
| `orchestrator` | Decomposes issues into parallel-safe sub-tasks, owns PR creation and merge. |
| `coder` | Implements fixes in isolated git worktrees, never commits to main. |
| `code-reviewer` | Reviews implementations for correctness, security, conventions. |
| `qa` | 4-layer verification (API/DB/Browser/Version) + weekly dogfood. |

## Merge Strategy

- **Type:** `gh pr merge --merge --delete-branch` (merge commit, not squash)
- **Why:** Preserves branch commit hashes on main so `git merge-base --is-ancestor` works correctly. Squash merges destroyed hashes, making every branch appear "stranded."
- **Guard:** Only merges when `mergeable == "MERGEABLE"` AND `mergeStateStatus == "clean"` (all CI checks green, no pending).

## Version Bump

- **When:** After all merges in a tick, in `merge-ready-prs`.
- **How:** `scripts/sync-version.sh --bump <level>` (patch for fixes, minor for features).
- **Why:** One bump per tick avoids per-PR collision conflicts.

## Naming Convention

All cron jobs follow `{phase}-{action}` naming:

```
00-cfg · 01-ingest · 02-queue · 03-build · 04-merge · 05-audit · 06-verify · 07-sync
```

The phase prefix makes the pipeline sequence obvious in cron listings.

## Environment Variables

| Variable | Profiles | Purpose |
|----------|----------|---------|
| `HERMES_PROJECT_DIR` | all | Project repository path (falls back to `~/project` in setup) |
| `HERMES_PROJECT_REPO` | all | GitHub repo in `owner/repo` format |
| `HERMES_KANBAN_BOARD` | orchestrator | Kanban board name (defaults to `project-dev`) |
| `HERMES_STAGING_URL` | qa | Staging deployment URL (defaults to `staging.project.com`) |
| `GITHUB_TOKEN` | orchestrator, qa | GitHub API access |
| `TELEGRAM_BOT_TOKEN` | orchestrator, qa | Telegram delivery for cron notifications |