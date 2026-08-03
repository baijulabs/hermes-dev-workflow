# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0] - 2026-08-03

### Changed
- **GH issue is now the single source of truth.** Kanban cards are ephemeral implementation artifacts. Issues are NEVER closed by automation — only by PR merge (`Closes #XXX`).
- **Removed destructive resolution section** from `hermes_github_sync.sh` — no more `gh issue close`, no more auto-archive, no more "Automated Resolution" spam. The sync script now handles ingestion ONLY.
- **`gh-issues-to-kanban` row corrected** in docs: type is `no_agent` (not `agent`), schedule is `every 5m`. Previously had stale docs from before the no_agent conversion.

### Added
- **`kanban-to-gh-tracker`** — new `no_agent: true` cron job that posts idempotent audit comments to GitHub issues at each pipeline milestone: decomposed, coder done, reviewer approved. Never closes issues.
- **PR consolidation posts GH comments** — when `pr-consolidation-watch.py` creates a PR, it now posts a "📦 PR #XXX created" comment on the linked GH issue.
- **`kanban-health-check`** — 3-hour watchdog (gateway, DB integrity, cron job health, rate limits, Telegram connectivity). Silent when nominal. Already deployed in v0.2.0, documented here.

## [0.2.0] - 2026-08-01

### Added
- **Test failure auto-remediation:** `staging-deploy-watch.py` now creates GitHub issues with `ready-for-agent` label when the deploy succeeds but non-gating tests fail. Previously these failures were reported once to Telegram and forgotten; now they automatically flow into the kanban fix cycle via `gh-issues-to-kanban`.
  - New `create_test_failure_issue()` function with run-level dedup (no duplicate issues for the same run ID).
  - Handles both `deploy succeeded + tests failed` and `deploy skipped + tests failed` scenarios.
  - Respects existing kanban-card dedup logic — skips issue creation when fix cards are already in flight for the branch.

### Fixed
- **QA cron jobs now live under the orchestrator profile.** The `qa` profile has no running scheduler daemon, so cron jobs defined in `profiles/qa/cron/jobs.json` were never ticked and silently never fired (caused a missed weekly dogfood scan). `qa-verify-deploy` (every 10m) and `dogfood-weekly` (Sat 8 AM) now run from the orchestrator scheduler via `profiles/orchestrator/cron/jobs.json.template`, with `deploy-watch.py` moved to `profiles/orchestrator/scripts/`. QA state and `.env` stay under `profiles/qa/`.
- **Documented the scheduler-location constraint** in the QA profile `SOUL.md` so fresh installs don't recreate the dead-end config.
- **KB documentation corrected:** `staging-deploy-watch` was documented as `agent` type at `every 10m`; corrected to `no_agent` at `every 15m` matching the actual cron config. Architecture diagram and cron tables now document the test-failure→GH-issue→kanban pipeline.

## [0.1.0] - 2026-07-30

### Added
- Initial portable release: automated development pipeline powered by Hermes Agent multi-profile kanban system.
- Profiles: `orchestrator` (decomposition + PR ownership), `coder` (worktree implementation), `code-reviewer` (quality gate), `qa` (4-layer verification + weekly dogfood).
- `setup.sh` one-command bootstrap: installs profiles, skills, cron templates, and `.env` template for any project.
- Cron jobs: `gh-issues-to-kanban`, `pr-check-watch`, `staging-deploy-watch`, `pr-consolidation-watch`, `review-failed-watch`, `worktree-collision-watch`, `active-pr-guard-watch`, `coder-review-required-watch`, `hermes-config-sync`, `prune-worktrees`, `qa-verify-deploy`, `dogfood-weekly`.
- Skills: branch consolidation, deploy-failure automation, kanban orchestration/safety/worker patterns, PR watch, CI automation.
- MIT license.

### Changed
- **Genericized hardcoded project references.** Board name, project directory, repo slug, and QA agent identity are now templated via environment variables (`HERMES_KANBAN_BOARD`, `HERMES_PROJECT_DIR`, `HERMES_PROJECT_REPO`) with MyProject defaults, so the repo bootstraps onto any project.
- Removed all references to the `my-project-operations` skill (not shipped in the portable repo); docs now reference `project-operations`.

### Fixed
- Hardcoded board name in the orchestrator `SOUL.md` templated for portability.
- QA profile identity made project-agnostic ("QA verification agent for this project").
