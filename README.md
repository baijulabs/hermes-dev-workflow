# Hermes Agent Development Workflow

A portable, automated development pipeline powered by Hermes Agent multi-profile kanban. Bootstraps issue ingestion, parallel coder+reviewer implementation, PR consolidation, merge, and QA verification onto any project.

## From-Zero Install

Verified procedure (setup.sh is exercised end-to-end in a sandbox before every release; see CHANGELOG).

### Prerequisites

**Tools** (setup.sh aborts if missing): `git`, `gh`, `hermes` (Hermes Agent CLI), `sqlite3`, `python3`, `jq`.

**Accounts & auth (manual, before setup):**

- **GitHub CLI authenticated:** `gh auth login` — setup does not configure gh auth; every cron script calls `gh`.
- **Push access** to the target repo from this machine.
- **An LLM provider key** (e.g. OpenRouter) for coder/reviewer/qa workers and the orchestrator.
- **A Telegram bot token + chat/thread id** if you want pipeline notifications delivered (cron `deliver` targets); otherwise deliveries fall back to `local`.

**Target project requirements:**

- A **git checkout with an `origin` remote** pointing at the GitHub repo setup.sh is pointed at (setup aborts otherwise — scripts resolve `owner/repo` from that remote).
- Default branch `main`.
- The `ready-for-agent` label (create it: `gh label create ready-for-agent --repo owner/project`).
- Optional, for the merge/deploy stages: a `deploy.yml` triggered by PR merge and a `scripts/sync-version.sh` for version bumps. Without them, ingest/build stages still work; merge/deploy stages no-op.

### Install

```bash
git clone https://github.com/baijulabs/hermes-dev-workflow.git
cd hermes-dev-workflow
./setup.sh \
  --repo "owner/project" \
  --dir "/path/to/project-checkout" \
  --board "project-dev" \
  --staging-url "https://staging.project.com" \
  --telegram-target "telegram:<chat_id>:<thread_id>"
```

All flags except `--repo`/`--dir` are optional. setup.sh then:

1. Registers the four Hermes profiles (`orchestrator`, `qa`, `coder`, `code-reviewer`) — a directory under `~/.hermes/profiles/` alone is **not** a profile; registration is what makes the dispatcher able to route work.
2. Deploys `SOUL.md`, scripts, and a generated `config.yaml` per profile (model/provider defaults to `deepseek/deepseek-v4-flash` on OpenRouter — **edit each `config.yaml`** to your provider/model).
3. Installs the orchestrator skills.
4. Writes `.env` templates (secrets are **never** auto-filled).
5. Creates the kanban board and sets its `default_workdir` to your checkout — this is what every portable script self-configures from.
6. Templates `cron/jobs.json` from the template (repo, dir, board, staging URL, telegram target substituted; template comments stripped so the file is valid JSON).

### Post-install checklist

```bash
# 1. Secrets (GitHub token, LLM provider key, Telegram token, optional staging DB):
vim ~/.hermes/profiles/orchestrator/.env        # + copy into coder/code-reviewer/qa .env if separate

# 2. Model per profile (all four config.yaml files):
for p in orchestrator coder code-reviewer qa; do
  ${EDITOR:-vi} ~/.hermes/profiles/$p/config.yaml
done

# 3. Verify the board is wired:
hermes kanban boards list
hermes -p orchestrator cron list                # should list 20 jobs (incl. drain-agent-queue)

# 4. Start the gateway (single shared gateway serves all profiles):
hermes gateway start

# 5. Smoke test the pipeline end to end:
gh issue create --repo owner/project --title "Test: pipeline smoke" \
  --body "Ping" --label ready-for-agent
# expect: ingest-gh-issues creates coder+reviewer cards within 5m (see cron output / kanban board)

# 6. Smoke test the drainer (empty queue → silent, exit 0):
hermes -p orchestrator cron run drain-agent-queue   # no output = clean empty-queue drain
```

**Non-default data roots:** if you run Hermes with a custom `HERMES_HOME` (not `~/.hermes`), set `KANBAN_DIR` to `$HERMES_HOME/kanban/boards` in the cron environment — the scripts' default path assumes the standard layout.

### Environment Variables

**Setup flags → written into profile config at install time** (not needed at runtime):

| Flag | Purpose |
|------|---------|
| `--repo owner/project` | GitHub repo; also the source for `owner/repo` self-configuration |
| `--dir /path/to/checkout` | Project checkout; becomes the board's `default_workdir` and each profile's `terminal.cwd` |
| `--board slug` | Kanban board slug (default `<project-dir-basename>-dev`) |
| `--staging-url URL` | QA staging target (`HERMES_STAGING_URL` in cron jobs) |
| `--telegram-target t` | Cron delivery target, e.g. `telegram:<chat_id>:<thread_id>` (default `local`) |

**`.env` secrets** (per profile; setup writes empty templates):

| Variable | Profiles | Purpose |
|----------|----------|---------|
| `GITHUB_TOKEN` | orchestrator, qa | GitHub API access (gh CLI) |
| `OPENROUTER_API_KEY` | all | LLM access for workers — **workers inherit the gateway's env; without it every dispatch fails 403** |
| `TELEGRAM_BOT_TOKEN` | orchestrator, qa | Telegram delivery for cron notifications |
| `NEON_DATABASE_URL` | qa | Staging database connection for QA DB-layer verification |

**Script self-configuration** (portable scripts contain no hardcoded project constants; overrides rarely needed):

| Variable | Purpose |
|----------|---------|
| `KANBAN_DIR` | Kanban boards root (default `~/.hermes/kanban/boards`) |
| `KANBAN_REPO_DIR` | Repo checkout override (default: board's `default_workdir`) |
| `KANBAN_REPO` | `owner/repo` override (default: parsed from `git remote get-url origin` in the repo dir) |
| `KANBAN_DB` | Kanban DB path override (default: first `kanban.db` under `KANBAN_DIR`) |
| `KANBAN_DEPLOY_WORKFLOW` | Deploy workflow file ingest-deploy-failures watches (default `deploy.yml`) |
| `KB_VAULT` | Optional Obsidian vault for the roadmap docs mirror (`update-roadmap-docs`; unset = mirror disabled) |

## Pipeline

> **Architecture principle:** Scripts detect, deterministic playbooks act, the orchestrator triages — no LLM session decides anything on a polling timer.

### 👑 Happy Path (GH Issue → Staging Deploy)

```
GH issue (labeled ready-for-agent)
        │
        ▼  every 5m
┌──────────────────────────────────────────────┐
│  01-ingest · ingest-gh-issues                │
│  Syncs labeled issues → READY-TO-IMPLEMENT   │
│  coder + reviewer kanban cards (worktrees).  │
│  Ingestion only — never closes issues.       │
└──────────────────────────────────────────────┘
        │  cards dispatched by the gateway daemon
        │  (kanban.dispatch_in_gateway: true — every 60s, no cron)
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
│  enqueues fix tasks → agent queue. Dedup via │
│  kanban board. (no_agent detector)           │
└──────────────────────────────────────────────┘
        │
        ▼  every 10m
┌──────────────────────────────────────────────┐
│  05-act · drain-agent-queue                  │
│  Deterministic playbook handler for the      │
│  agent queue (agent-queue.json). Playbooks:  │
│  review-failed → fix card + paired reviewer  │
│  (+ archive dead reviewer) · ci-failure →    │
│  CI-fix card + reviewer · deploy-qa → QA     │
│  card per new deploy · unknown types         │
│  escalate once for orchestrator triage.      │
│  Guardrails: max 2 cards/tick, idempotency   │
│  dedup, skip if merged/closed/unblocked,     │
│  3-strike parking. Only creates cards —      │
│  never modifies code.                        │
└──────────────────────────────────────────────┘
        │  deploy succeeds
        ▼  every 10m
┌──────────────────────────────────────────────┐
│  06-verify · verify-deploy-qa                │
│  Validates staging health, critical flows,   │
│  maps changed files → E2E steps. Creates     │
│  GH issues for bugs found. Also enqueues a   │
│  deploy-qa item per new deploy (deduped per  │
│  run) → agent queue for 05-act.              │
└──────────────────────────────────────────────┘
```

### ⟳ Parallel Detection

```
┌──────────────────────────────────────────────┐
│  01-ingest · ingest-ci-failures (every 5m)   │
│  Monitors open PRs for CI failures and merge │
│  conflicts. no_agent detector: enqueues      │
│  findings → agent queue (agent-queue.json);  │
│  the drainer (05-act) acts on them.          │
│  Conflict resolution pushed directly to the  │
│  existing PR branch (no new PR created).     │
└──────────────────────────────────────────────┘
```

### ⚙️ Act Layer (deterministic playbooks, no LLM)

| Job | Schedule | Handles |
|-----|----------|---------|
| `drain-agent-queue` | every 10m | Drains `agent-queue.json` with fixed recipes: `review-failed` → fix coder card + paired code-reviewer (+ archive the dead blocked reviewer); `ci-failure` → CI-fix coder card + paired reviewer; `deploy-qa` → QA card per new staging deploy. Unknown item types escalate once for orchestrator triage. Guardrails: max 2 card-creating actions/tick, idempotency dedup, skips merged/closed/unblocked sources, 3-strike parking. Only creates cards — never modifies code. |

### Edge Case Handlers (every 5m, 03-build phase)

| Job | Handles |
|-----|---------|
| `build-reviewer-resolve` | no_agent detector: extracts findings from `review-failed:` blocked reviewer cards and enqueues `review-failed` items → agent queue; the drainer (`drain-agent-queue`) creates the replacement coder+reviewer pair. |
| `build-reviewer-approve` | Auto-completes reviewer cards when coder is done. |
| `build-coder-resolve` | Handles coder timeouts and `review-required:` blocks. |

### 🧹 Hygiene (Maintenance)

| Job | Schedule | Purpose |
|-----|----------|---------|
| `audit-stranded-worktrees` | every 15m | Flags worktree branches with unique commits not on main and no pending kanban work. Creates triage GH issue. |
| `audit-archived-unmerged` | every 15m | Detects archived coder cards whose branch content never reached main with no merged PR — reopens the linked closed issue (premature-epic-close stranding guard). |
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
00-cfg · 01-ingest · 03-build · 04-merge · 05-act · 05-audit · 06-verify · 07-sync
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