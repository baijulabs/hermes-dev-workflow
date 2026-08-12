# Hermes Agent Development Workflow

> **Last updated:** 2026-08-12  
> **Profiles:** orchestrator, coder, code-reviewer, qa  
> **Repo:** `owner/project`

This document describes the complete automated development pipeline powered by the Hermes Agent multi-profile kanban system. Every step from GitHub issue ingestion through implementation, review, PR creation, merge, staging deployment, and QA verification is automated.

---

## Architecture Overview

```
GitHub Issue (ready-for-agent label)  ← SINGLE SOURCE OF TRUTH
        │                                    Never closed by automation —
        ▼                                    only by PR merge (Closes #XXX)
┌──────────────────────────────────────────────┐
│  01-ingest · ingest-gh-issues (every 5m)    │
│  Syncs issues → orchestrator kanban cards    │  INGESTION ONLY
└──────────────────────────────────────────────┘  (no resolution section)
        │
        ▼
┌──────────────────────────────────────────────┐
│  Orchestrator (manual / issue-driven)         │
│  Decomposes into parallel coder+reviewer      │
│  pairs with strict file isolation              │
│  → Posts "📋 Decomposed" comment to GH issue  │
└──────────────────────────────────────────────┘
        │
        ▼
┌──────────────────────────────────────────────┐
│  02-queue · queue-agent-processor (30m 7-21) │
│  Spawns coder workers → coders implement      │
│  Reviewer auto-promoted after coder done      │
└──────────────────────────────────────────────┘
        │
        ├── Coder done → GH issue comment: "✅ Implementation complete"
        ├── Reviewer done → GH issue comment: "✅ Code review passed"
        │
        ▼
┌──────────────────────────────────────────────┐
│  03-build · build-consolidate-prs (every 5m) │
│  Finds done pairs → consolidation PR          │
│  → Posts "📦 PR #XXX" comment to GH issue    │
│  0-commits check → auto-archive if no diffs   │
└──────────────────────────────────────────────┘
        │
        ▼  CI triggers (test jobs only — deploy does NOT run here)
┌──────────────────────────────────────────────┐
│  04-merge · merge-ready-prs (every 10m)      │
│  Merges only when CI=clean AND no conflicts   │
│  Single version bump per tick after merge      │
└──────────────────────────────────────────────┘
        │
        ▼  PR merged → deploy.yml triggers
┌──────────────────────────────────────────────┐
│  GitHub CI/CD (deploy.yml)                    │
│  Tests → Build → Deploy to Staging            │
│  (Deploy only runs on merged PRs, not on PR   │
│   creation)                                   │
└──────────────────────────────────────────────┘
        │
        ▼
┌──────────────────────────────────────────────┐
│  PR Merged → GH auto-closes issue             │
│  (via "Closes #XXX" in PR body)               │
└──────────────────────────────────────────────┘
        │
        ▼
┌──────────────────────────────────────────────┐
│  01-ingest·ingest-deploy-failures(every 15m) │
│  Detects deploy failures → creates fix cards  │
└──────────────────────────────────────────────┘
        │
        ▼
┌──────────────────────────────────────────────┐
│  06-verify · verify-deploy-qa (every 10m)    │
│  4-layer check: API → DB → Browser → Version  │
│  Reports regressions as GH issues             │
└──────────────────────────────────────────────┘
```

---

## Profiles

### `orchestrator` — Technical Project Manager
**Role:** Decomposes issues into parallel-safe sub-tasks. Routes work to coder+reviewer pairs. Owns PR creation and consolidation.

**Key rules:**
- Never writes implementation code
- Limits to 3 sub-tasks per decomposition
- Ensures strict file isolation between parallel tasks
- Always creates paired reviewer cards (`parents=[coder_id]`)
- Branch guardrails on every coder card body
- Reads parent priority from kanban and passes `--priority N` on child cards

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
- Auto-resolved by `build-reviewer-resolve` cron

### `qa` — Fix Verification Auditor
**Role:** Verifies that deployed fixes actually resolved their reported issues. Runs automatically after every staging deploy.

**Verification layers:**
1. **API** — Curls staging endpoints, checks response shapes
2. **DB** — Queries staging database for data-persistence fixes
3. **Browser** — Drives staging UI for visual verification
4. **Version** — Confirms deployed version matches workflow

---

## GitHub Issue Lifecycle

The GH issue is the single source of truth — never closed by automation, only by PR merge. Kanban cards are ephemeral implementation artifacts.

### The issue is never auto-closed
- `ingest-gh-issues` handles ingestion only — pulls labeled issues into kanban
- No resolution section, no gh issue close, no "Automated Resolution" comments
- When the coder+reviewer pair is done, the consolidation creates a PR
- The PR body includes `Closes #XXX` — GitHub auto-closes the issue on merge

### Audit trail on the issue
`sync-gh-comments` posts idempotent comments at each milestone:
- Decomposed: Orchestrator card reaches done
- Implementation done: Coder card reaches done
- Review passed: Reviewer card reaches done
- PR created: Consolidation watchdog creates PR

---

## Naming Convention

All cron jobs follow `{phase}-{action}` naming. The phase prefix makes the pipeline sequence obvious in cron listings:

```
Phase 0: cfg-        Config sync (internal)
Phase 1: ingest-     Issue/CI/deploy failure ingestion into kanban
Phase 2: queue-      Agent queue processing
Phase 3: build-      PR consolidation, review resolution
Phase 4: merge-      PR merge (only when CI is green)
Phase 5: audit-      Health checks, stranded worktrees, pruning
Phase 6: verify-     Post-deploy QA, weekly dogfood
Phase 7: sync-       GH milestone comments
```

---

## Cron Jobs Reference

### Phase 1: Ingestion

| Job | Schedule | Type | Deliver | Purpose |
|-----|----------|------|---------|---------|
| `ingest-gh-issues` | every 5m | no_agent | local | Pulls labeled issues into kanban as orchestrator cards. Ingestion only — never closes issues. |
| `ingest-ci-failures` | every 5m | agent | telegram | Monitors open PRs for CI failures and merge conflicts. Enqueues fix tasks for agent processor. |
| `ingest-deploy-failures` | every 15m | agent | telegram | Checks main deploy runs at job-level conclusion. Creates fix cards on deploy failure. |

### Phase 2: Queue

| Job | Schedule | Type | Deliver | Purpose |
|-----|----------|------|---------|---------|
| `queue-agent-processor` | */30 7-21 | agent | telegram | Claims ready cards → dispatches coder + reviewer workers. |

### Phase 3: Build

| Job | Schedule | Type | Deliver | Purpose |
|-----|----------|------|---------|---------|
| `build-consolidate-prs` | every 5m | no_agent | telegram | Finds done coder+reviewer pairs → merges worktrees → creates consolidation PR with Closes #N. |
| `build-reviewer-resolve` | every 5m | agent | telegram | Auto-resolves blocked reviewer cards with `review-failed:` reason. |
| `build-reviewer-approve` | every 5m | no_agent | telegram | Auto-completes reviewer cards when coder is done. |
| `build-coder-resolve` | every 5m | no_agent | telegram | Handles coder timeouts and `review-required:` blocks. |

### Phase 4: Merge

| Job | Schedule | Type | Deliver | Purpose |
|-----|----------|------|---------|---------|
| `merge-ready-prs` | every 10m | no_agent | telegram | Merges MERGEABLE PRs with clean CI. Uses `--merge` (preserves branch hashes). Single version bump per tick. |

### Phase 5: Audit

| Job | Schedule | Type | Deliver | Purpose |
|-----|----------|------|---------|---------|
| `audit-stranded-worktrees` | every 2h | no_agent | telegram | Flags worktree branches with unique commits not on main and no pending kanban work. |
| `audit-worktree-collisions` | every 5m | no_agent | telegram | Detects worktree branch conflicts (same branch in multiple worktrees). |
| `audit-pr-guard` | every 5m | no_agent | telegram | Enforces PR guard conditions (main-tip, no workflow_dispatch from non-main). |
| `audit-archive-cancelled` | every 15m | no_agent | telegram | Archives cancelled or timed-out kanban cards. |
| `audit-prune-worktrees` | every 48h | no_agent | telegram | Deletes stale .worktrees/ > 7 days old. |
| `audit-kanban-health` | every 3h | no_agent | telegram | Kanban DB SQLite integrity checks. |

### Phase 6: Verify

| Job | Schedule | Type | Deliver | Purpose |
|-----|----------|------|---------|---------|
| `verify-deploy-qa` | every 10m | agent | telegram | Detects new staging deploys. Runs 4-layer fix verification. Creates GH regression issues. |
| `verify-dogfood` | Sat 8am | agent | telegram | Weekly full exploratory QA session on staging. |

### Phase 7: Sync

| Job | Schedule | Type | Deliver | Purpose |
|-----|----------|------|---------|---------|
| `sync-gh-comments` | every 5m | no_agent | local | Posts milestone comments to GH issues. Idempotent state tracking. |

### Phase 0: Config

| Job | Schedule | Type | Deliver | Purpose |
|-----|----------|------|---------|---------|
| `cfg-config-sync` | every 60m | no_agent | local | Syncs Hermes configuration across profiles. |

---

## Merge Strategy

- **Type:** `gh pr merge --merge --delete-branch` (merge commit, not squash)
- **Why:** Preserves branch commit hashes on main so `git merge-base --is-ancestor` works correctly. Squash merges destroy hashes, making every branch appear "stranded" even when content is on main.
- **Guard:** Merges only when BOTH conditions are met:
  1. `mergeable == "MERGEABLE"` — no git merge conflicts
  2. `mergeStateStatus == "clean"` — all CI checks green, nothing pending
- **Deploy cooldown:** Skips merge tick if a deploy.yml run on main is in-progress (prevents overlapping deploys)

## Version Bump

- **When:** After all merges in a tick, inside `merge-ready-prs`.
- **How:** Reads commit messages between `origin/main` and merged branch heads.
  - `feat:` prefix → `minor` bump
  - Everything else → `patch` bump
  - Runs `scripts/sync-version.sh --bump <level>` which updates `backend/pyproject.toml`, `frontend/package.json`, root `package.json`
  - Commits as `chore: bump version to X.Y.Z`
- **Why single bump:** Each tick does exactly one version bump. No per-PR bumps avoids collision conflicts.
- **Conflict handling:** If another merge bumps between diff and push, rebase fails gracefully — no crash.

### Merge → CI → Deploy Timing

```
build-consolidate-prs (every 5m)   creates PR → CI triggers
        ↓
CI runs (test jobs only —          ~15-20 min
deploy does NOT run on PR create)
        ↓
merge-ready-prs (every 10m)        checks MERGEABLE + clean → merges
        ↓
deploy.yml triggered on            pull_request_target closed + merged
        ↓
Deploy to Staging runs
        ↓
ingest-deploy-failures (15m)       detects deploy failure if any
verify-deploy-qa (10m)             validates health if deploy succeeded
```

The deploy-to-staging job is correctly gated: it only fires on `pull_request_target closed + merged` or `workflow_dispatch`. Test jobs (lint, unit, slow tests) run on PR creation/update, but `Deploy to Staging` does not.

---

## PR Consolidation Flow

```
Coder done + Reviewer approved (both 'done' or 'archived')
        │
        ▼
build-consolidate-prs.py picks up the pair
        │
        ├── 1. Check branch has commits vs main (rev-list --count)
        ├── 2. Dedup: same commit hashes already PR'd?
        ├── 3. Check: commits already in main? (merge-base --is-ancestor)
        ├── 4. Check: PR already exists for branch?
        ├── 5. If 0 commits vs main → auto-archive, skip PR creation
        ├── 6. Push branch to origin
        └── 7. Create PR via gh pr create (body includes Closes #N)
                │
                ▼
        CI triggers (test jobs only)
                │
                ▼
        merge-ready-prs checks MERGEABLE + clean → merges
                │
                ▼
        Deploy to Staging (only on merge event)
```

### Zero-Commits Auto-Archive
Before creating a PR, `build-consolidate-prs` runs `git rev-list --count origin/main..branch`. If the count is 0 (all content already on main via other paths), it archives the coder+reviewer cards and skips PR creation. This prevents "No commits between main and branch" errors.

### Merge Conflict Resolution Flow
```
merge-ready-prs: detects CONFLICTING → skips PR
        ↓
ingest-ci-failures: detects mergeable=CONFLICTING (every 5m)
        ↓
Enqueues merge_conflict task → agent processor → coder+reviewer cards
        ↓
Coder checks out existing PR branch (fix/consolidate-gh-N), resolves conflict, pushes
        ↓
PR updates (synchronize event), CI re-runs
        ↓
merge-ready-prs sees MERGEABLE + clean → merges
```

The fix is pushed directly to the existing PR branch — no new PR is created.

---

## Deploy Pipeline (deploy.yml)

Triggered by `pull_request_target` events on PRs targeting `main` and `workflow_dispatch`.

### Key Gating Logic

The `deploy-to-staging` job has this condition:
- `pull_request_target closed + merged` (PR merged to main)
- OR `workflow_dispatch` (manual trigger)
- AND at least one path filter matches (backend, frontend, terraform, infra, or config changed)

**Deploy-to-staging does NOT run on PR creation or update.** Only test jobs run on those events.

### Common Pitfalls

| Pitfall | Symptom | Fix |
|---------|---------|------|
| `SERVICE_NAME_STAGING` mismatch | `Error parsing [service]` | Match env var names between definition and reference (no `_STAGING` suffix if name doesn't have it) |
| Cloudflare token expired | Terraform apply 401 | Generate new CF API token, update GitHub secret |
| paths-filter empty on merge | Deploy always skipped | Add `github.event.action == 'closed'` to path-filter condition |
| Skipped needs cascade | Deploy skipped | Use `always()` in `if:` condition, remove test jobs from deploy `needs` |

### Cache Primer
On push to `main`, a lightweight `cache-primer` job installs Python and Node dependencies. All heavy jobs skip push events to conserve minutes. Cache writes are blocked on `pull_request_target` by GitHub policy — the primer works around this.

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

## Environment Variables

| Variable | Profiles | Purpose |
|----------|----------|---------|
| `HERMES_PROJECT_DIR` | all | Project repository path |
| `HERMES_PROJECT_REPO` | all | GitHub repo in `owner/repo` format |
| `HERMES_KANBAN_BOARD` | orchestrator | Kanban board name |
| `HERMES_STAGING_URL` | qa | Staging deployment URL |
| `GITHUB_TOKEN` | orchestrator, qa | GitHub API access (gh CLI) |
| `NEON_DATABASE_URL` | qa | Staging database connection for DB-layer verification |
| `TELEGRAM_BOT_TOKEN` | orchestrator, qa | Telegram delivery for cron notifications |

---

## QA Verification Pipeline

The `qa` profile runs after every successful staging deploy.

### 4-Layer Verification

| Layer | Method | Time Budget | When Applied |
|-------|--------|-------------|--------------|
| **API** | `curl` against staging URL | 30s/endpoint | Backend route bugs, response shape changes |
| **DB** | `psql` against staging DB | 15s/query | Data persistence fixes |
| **Browser** | `browser_navigate` + snapshot | 60s/check | UI fixes (layout, component visibility, i18n) |
| **Version** | Compare deploy version vs repo | 5s | Every run |

### Reporting
- **All pass:** Brief Telegram confirmation
- **Any failure:** Telegram alert with failure summary + GitHub QA report issue
- **Regression:** New GitHub issue with `bug` + `QA review` labels

---

## Common Troubleshooting

### PRs not being created
1. Check `build-consolidate-prs` cron output
2. Verify coder+reviewer pair both have `status IN ('done','archived')`
3. Check branch is on origin (`git branch -r --list origin/<branch>`)
4. Check no existing PR (`gh pr list --state all --head <branch>`)
5. Verify commits not already in main (`git merge-base --is-ancestor`)

### PRs not being merged
1. Check `merge-ready-prs` cron output — is it "CONFLICT", "CI FAILING", or "BEHIND"?
2. Verify `mergeStateStatus == "clean"` (not `unstable`, `has_hooks`, `behind`)
3. Check if deploy cooldown is active (deploy.yml run on main in progress)

### Deploy not triggering on merge
1. Verify `deploy-to-staging` job condition evaluates correctly
2. Check `paths-filter` doesn't see empty diff (merged PR head == main ancestor)
3. Manual trigger: `gh workflow run deploy.yml -f environment=staging`

### Coder card stuck in ready
1. Check `task_events` for `respawn_guarded` (PR already exists → `audit-pr-guard`)
2. Check `last_failure_error` for worktree collision (→ `audit-worktree-collisions`)
3. Verify dispatcher is running (`hermes kanban dispatch`)

### Wrong-branch recovery
If a coder commits to `main`:
1. `git reset --hard HEAD~1`
2. `git push origin main --force-with-lease`
3. Diagnose which guardrail layer failed
4. File fix card for the root cause