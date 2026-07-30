# Hermes Agent Development Workflow

> **Last updated:** 2026-07-30  
> **Profiles:** orchestrator, coder, code-reviewer, qa  
> **Repo:** my-org/MyProject

This document describes the complete automated development pipeline powered by the Hermes Agent multi-profile kanban system. Every step from GitHub issue ingestion through implementation, review, PR creation, staging deployment, and QA verification is automated.

---

## Architecture Overview

```
GitHub Issue (ready-for-agent label)
        │
        ▼
┌──────────────────────────────────────────────┐
│  gh-issues-to-kanban (every 15m, no_agent)   │
│  Ingests issues → kanban board                │
└──────────────────────────────────────────────┘
        │
        ▼
┌──────────────────────────────────────────────┐
│  Orchestrator (manual / issue-driven)         │
│  Decomposes into parallel coder+reviewer      │
│  pairs with strict file isolation              │
└──────────────────────────────────────────────┘
        │
        ▼
┌──────────────────────────────────────────────┐
│  Dispatcher (gateway, continuous)             │
│  Spawns coder workers → coders implement      │
│  Reviewer auto-promoted after coder done      │
└──────────────────────────────────────────────┘
        │
        ▼
┌──────────────────────────────────────────────┐
│  PR Consolidation Watchdog (every 10m)        │
│  Finds done pairs → version bumps → PR        │
└──────────────────────────────────────────────┘
        │
        ▼
┌──────────────────────────────────────────────┐
│  GitHub CI/CD (deploy.yml)                    │
│  Tests → Build → Deploy to Staging            │
└──────────────────────────────────────────────┘
        │
        ▼
┌──────────────────────────────────────────────┐
│  QA Verification (every 10m)                  │
│  4-layer check: API → DB → Browser → Version  │
│  Reports regressions as GH issues             │
└──────────────────────────────────────────────┘
```

---

## Profiles

### `orchestrator` — Technical Project Manager
**Role:** Decomposes issues into parallel-safe sub-tasks. Routes work to coder+reviewer pairs. Owns PR creation and consolidation.

**Identity file:** `~/.hermes/profiles/orchestrator/SOUL.md`

**Key rules:**
- Never writes implementation code
- Limits to 3 sub-tasks per decomposition
- Ensures strict file isolation between parallel tasks
- Always creates paired reviewer cards (`parents=[coder_id]`)
- Branch guardrails on every coder card body

### `coder` — Implementation Worker
**Role:** Implements fixes in isolated git worktrees. Tests, lints, commits, and hands off to reviewer.

**Key rules:**
- Verifies branch before writing code (3-layer guardrail)
- Never commits to `main` or `master`
- Never opens PRs (orchestrator owns PR creation)
- Calls `kanban_complete()` — never `kanban_block(reason='review-required:...')`

### `code-reviewer` — Quality Gate
**Role:** Reviews coder implementations for correctness, security, and convention compliance.

**Key rules:**
- Blocks with `review-failed:` reason when issues found (structured findings)
- Approves by completing the card
- Auto-resolved by `review-failed-watch` cron

### `qa` — Fix Verification Auditor
**Role:** Verifies that deployed fixes actually resolved their reported issues. Runs automatically after every staging deploy.

**Identity file:** `~/.hermes/profiles/qa/SOUL.md`

**Verification layers:**
1. **API** — Curls staging endpoints, checks response shapes
2. **DB** — Queries staging Neon for data-persistence fixes
3. **Browser** — Drives staging UI for visual verification
4. **Version** — Confirms deployed version matches workflow

---

## Cron Jobs Reference

### Ingestion & Sync

| Job | Schedule | Type | Deliver | Script | Purpose |
|-----|----------|------|---------|--------|---------|
| `gh-issues-to-kanban` | every 15m | no_agent | local | `hermes_github_sync.sh` | **Ingestion:** Pulls labeled issues AND PRs into kanban. Issues get [GH-N] orchestrator cards. PRs with ready-for-agent label get review-feedback orchestrator cards (deduped with pr-check-watch by branch). **Resolution:** Closes issues when all kanban child cards are done. Skips PRs (never auto-closes pull requests). Guards: epic label, open child issues, in-flight coder children. |

### Failure Detection & Auto-Remediation

| Job | Schedule | Type | Deliver | Script | Purpose |
|-----|----------|------|---------|--------|---------|
| `staging-deploy-watch` | every 10m | agent | telegram | `staging-deploy-watch.py` | Polls staging deploy workflow for failures. Deduplicates against existing kanban cards. Creates `[DF-<timestamp>]` fix cards on new failures. |
| `pr-check-watch` | every 15m | agent | telegram | — | Monitors open PRs for CI failures and merge conflicts. For conflicts: creates "Resolve merge conflicts" coder+reviewer pairs. For CI failures: creates `[PRFIX-<timestamp>]` fix cards. Skips dependabot PRs. |
| `review-failed-watch` | every 5m | agent | telegram | — | Auto-resolves blocked code-reviewer cards with `review-failed:` reason. Extracts findings from reviewer comments, creates replacement coder+reviewer pairs, archives old blocked card. Escalates after 3+ cycles. |

### Pipeline Health & Safety

| Job | Schedule | Type | Deliver | Script | Purpose |
|-----|----------|------|---------|--------|---------|
| `worktree-collision-watch` | every 5m | no_agent | telegram | `worktree-collision-watch.py` | Detects coder cards blocked by git worktree branch collisions (`fatal: already used by worktree`). Auto-assigns unique branch names and resets card to `todo`. |
| `active-pr-guard-watch` | every 5m | no_agent | telegram | `active-pr-guard-watch.py` | Detects cards stuck in `ready` state with 5+ consecutive `respawn_guarded` events (PR already exists). Moves them to `triage` for orchestrator handling. |
| `coder-review-required-watch` | every 5m | no_agent | telegram | `coder-review-required-watch.py` | Auto-completes coder cards blocked with `review-required:` instead of calling `kanban_complete()`. Unblocks the paired reviewer card so PR consolidation can proceed. |
| `prune-worktrees` | every 360m | no_agent | telegram | `prune-worktrees.sh` | Cleans up stale git worktrees (prunable metadata directories) to prevent disk bloat. |

### PR Creation & Versioning

| Job | Schedule | Type | Deliver | Script | Purpose |
|-----|----------|------|---------|--------|---------|
| `pr-consolidation-watch` | every 10m | no_agent | telegram | `pr-consolidation-watch.py` | Finds done (`done`/`archived`) coder+reviewer pairs. Version-bumps via `sync-version.sh --bump`, pushes branch to origin, creates GitHub PR. Deduplicates by commit hash, skips already-merged branches, skips branches with existing PRs. |

### QA Verification

| Job | Schedule | Type | Deliver | Script | Purpose |
|-----|----------|------|---------|--------|---------|
| `qa-verify-deploy` | every 10m | agent | telegram | `deploy-watch.py` | Detects new staging deploys. Runs 4-layer fix verification (API → DB → Browser → Version). Creates GitHub QA report issue. Files regression issues with `bug` + `QA review` labels for any failed verification. |

---

## Skills Reference

Skills are reusable procedural knowledge loaded by agent-driven cron jobs:

| Skill | Used By | Purpose |
|-------|---------|---------|
| `project-operations` | `staging-deploy-watch`, `pr-check-watch`, `qa-verify-deploy` | Operational workflows, deploy pipeline, test suite execution |
| `github-pr-workflow` | `staging-deploy-watch`, `pr-check-watch`, `qa-verify-deploy` | PR lifecycle: branch creation, PR open, CI checks, merge strategy |
| `github-issues` | `qa-verify-deploy` | Issue creation with labels, regression tracking |
| `kanban-orchestrator` | `review-failed-watch` | Decomposition playbook, review-failed auto-resolution |

---

## Branch Guardrails (3-Layer Defense)

To prevent coders from committing to wrong branches:

| Layer | Where | What |
|-------|-------|------|
| **1 — Card Body** | Every `kanban_create` call | `BASE BRANCH:` line + `CRITICAL:` guardrail as last lines of body |
| **2 — AGENTS.md** | Loaded by coder at startup | Step 2: `git branch --show-current` + `echo $HERMES_KANBAN_BRANCH` verification |
| **3 — kanban-worker Skill** | Loaded by coder | Worktree setup from base branch, "Do NOT commit to main" hard stop |

**Zero-exemption rule:** No "quick fix" exemption. Committing to `main`/`master` is always wrong for a dispatched coder.

---

## PR Consolidation Flow

```
Coder done + Reviewer approved (both 'done' or 'archived')
        │
        ▼
pr-consolidation-watch.py picks up the pair
        │
        ├── 1. Check branch has commits vs main
        ├── 2. Dedup: same commit hashes already PR'd?
        ├── 3. Check: commits already in main? (merge-base --is-ancestor)
        ├── 4. Check: PR already exists for branch? (--state all)
        ├── 5. Version bump: sync-version.sh --bump patch|minor
        │      - Finds existing worktree or creates temp worktree
        │      - Runs sync-version.sh, git add, git commit
        ├── 6. Push branch to origin
        └── 7. Create PR via gh pr create
                │
                ▼
        deploy.yml triggers CI
                │
                ▼
        PR merged → deploy-to-staging
                │
                ▼
        QA profile verifies fix survived deployment
```

### Version Bump Logic
- Scans commit messages between `origin/main` and branch head
- `feat:`/`feature:` prefix → `minor` bump
- Everything else → `patch` bump
- Runs `scripts/sync-version.sh --bump <level>` which updates:
  - `backend/pyproject.toml` (source of truth)
  - `frontend/package.json`
  - `package.json` (root)
- Commits as `chore: bump version to X.Y.Z`

---

## Deploy Pipeline (deploy.yml)

Triggered by `pull_request_target` events on PRs targeting `main` (opened, synchronize, reopened, closed) and `workflow_dispatch`.

### Job Execution Flow

```
Pull Request Event
        │
        ▼
┌──────────────────┐
│  changes         │  Detects changed paths (dorny/paths-filter)
│  (always runs)   │  Outputs: backend, frontend, config, infra, terraform, docs
└──────────────────┘
        │
        ├──────────────────────────────────────┐
        ▼                                      ▼
┌──────────────────┐                  ┌──────────────────┐
│  lint            │                  │  backend-fast-test│
│  (skip on push)  │                  │  (skip on push)  │
└──────────────────┘                  └──────────────────┘
                                               │
        ┌──────────────────────────────────────┤
        ▼                                      ▼
┌──────────────────┐                  ┌──────────────────┐
│  frontend-unit   │                  │  backend-slow    │
│  (skip on push)  │                  │  (tag/dispatch)  │
└──────────────────┘                  └──────────────────┘
        │                                      │
        └──────────────┬───────────────────────┘
                       ▼
              ┌──────────────────┐
              │  prepare-deploy  │  Computes image tag from commit SHA
              └──────────────────┘
                       │
                       ▼
              ┌──────────────────┐
              │ deploy-to-staging│  PR merge (action=closed, merged) OR workflow_dispatch
              │                  │  Deploys to Cloud Run via Terraform
              │  Deploy Summary  │  Outputs version, image tag, commit, branch to job summary
              └──────────────────┘
                       │
                       ▼
              ┌──────────────────┐
              │  lighthouse      │  (disabled: if false — redundant)
              │  deploy-terraform│  (disabled: if false)
              │  e2e-on-staging  │  (disabled: if false)
              └──────────────────┘
```

### Cache Primer
On every push to `main`, a lightweight `cache-primer` job installs Python (`uv`) and Node (`npm`) dependencies to populate GitHub Actions caches. All heavy jobs skip push events to conserve minutes. Cache writes are blocked on `pull_request_target` events by GitHub platform policy — the primer works around this.

---

## Safety Protocols

### `[GH-N]` Pattern Warning
The `hermes_github_sync.sh` script scans done kanban cards for `[GH-N]` patterns and closes matching GitHub issues. To prevent premature closure:

- **Guard 1:** Skips pull requests (checked via `gh pr view`)
- **Guard 2:** Skips `epic`-labeled issues
- **Guard 3:** Skips issues with open child GitHub issues
- **Guard 4:** Skips if the orchestrator card has coder children still in flight

### Branch Naming Convention
- Coder worktrees: `wt/t_<task-id>` (auto-derived by dispatcher)
- Named fix branches: `fix/<descriptor>-<task-id>`
- Never pass `--branch main` — causes worktree collision with repo root

### Wrong-Branch Recovery
If a coder commits to `main`:
1. `git reset --hard HEAD~1`
2. `git push origin main --force-with-lease`
3. Diagnose which guardrail layer failed
4. File fix card for the root cause

---

## QA Verification Pipeline

The `qa` profile runs after every successful staging deploy:

### 4-Layer Verification

| Layer | Method | Time Budget | When Applied |
|-------|--------|-------------|--------------|
| **API** | `curl` against `staging.my-project.com` | 30s/endpoint | Backend route bugs, response shape changes |
| **DB** | `psql` against staging Neon | 15s/query | Data persistence fixes (audit logs, FRS, quiz, checklist) |
| **Browser** | `browser_navigate` + snapshot | 60s/check | UI fixes (layout, component visibility, i18n) |
| **Version** | Compare deploy version vs repo | 5s | Every run |

### Reporting
- **All pass:** Brief Telegram confirmation
- **Any failure:** Telegram alert with failure summary + GitHub QA report issue
- **Regression:** New GitHub issue with `bug` + `QA review` labels, version in body
- User manually reviews and applies `ready-for-agent` label to kick off fix cycle

### State Tracking
- `~/.hermes/profiles/qa/state/last_verified_deploy.json` — idempotency guard
- `~/.hermes/profiles/qa/state/verification_history.json` — trend tracking

---

## Environment & Configuration

### Required Environment Variables

| Variable | Profile | Purpose |
|----------|---------|---------|
| `GITHUB_TOKEN` | orchestrator, qa | GitHub API access (gh CLI) |
| `NEON_DATABASE_URL` | qa | Staging database connection for DB-layer verification |
| `TELEGRAM_BOT_TOKEN` | orchestrator, qa | Telegram delivery for cron job notifications |

### Key Configuration Files

| File | Purpose |
|------|---------|
| `~/.hermes/profiles/orchestrator/SOUL.md` | Orchestrator identity + decomposition rules |
| `~/.hermes/profiles/orchestrator/cron/jobs.json` | All orchestrator cron job definitions |
| `~/.hermes/profiles/qa/SOUL.md` | QA profile identity + 4-layer verification workflow |
| `~/.hermes/profiles/qa/cron/jobs.json` | QA verification cron job |
| `~/.hermes/profiles/orchestrator/scripts/pr-consolidation-watch.py` | PR creation + version bump watchdog |
| `~/.hermes/profiles/orchestrator/scripts/hermes_github_sync.sh` | GitHub ↔ Kanban sync |
| `.github/workflows/deploy.yml` | CI/CD pipeline definition |

---

## Common Troubleshooting

### PRs not being created
1. Check `pr-consolidation-watch` cron output
2. Verify coder+reviewer pair both have `status IN ('done','archived')`
3. Check branch is on origin (`git branch -r --list origin/<branch>`)
4. Check no existing PR (`gh pr list --state all --head <branch>`)
5. Verify commits not already in main (`git merge-base --is-ancestor`)

### Deploy not triggering on merge
1. Verify `deploy-to-staging` job condition evaluates correctly
2. Check `paths-filter` doesn't see empty diff (merged PR head == main ancestor)
3. Manual trigger: `gh workflow run deploy.yml -f environment=staging`

### Coder card stuck in ready
1. Check `task_events` for `respawn_guarded` (PR already exists → active-pr-guard-watch)
2. Check `last_failure_error` for worktree collision (→ worktree-collision-watch)
3. Verify dispatcher is running (`hermes kanban dispatch`)

### Issue closed prematurely
1. Check `hermes_github_sync.sh` — it closes issues for any done `[GH-N]` kanban card
2. Verify Guard 4 is catching orchestrator epics with in-flight children
3. Reopen: `gh issue reopen <N>`
