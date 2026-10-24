# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [v2.3.0] — 2026-10-24

### Added
- **`drain-agent-queue.py` — deterministic playbook handler for the agent queue (no_agent, every 10m).** Closes the loop that previously required an LLM cron ("unified processor"): the queue's standard findings now resolve by fixed recipe, zero tokens. Playbooks: `review-failed` → fix coder card + paired code-reviewer card, comment + archive the dead blocked reviewer; `ci-failure` → CI-fix coder card + paired reviewer for an open PR with a merge conflict or failing checks; `deploy-qa` → QA verification card (assignee `qa`, scratch workspace) for each new staging deploy. Unknown item types are marked processed with an `ESCALATED:` resolution so the orchestrator sees them once instead of every tick. Guardrails are hard-coded, not judgment: max 2 card-creating actions per tick, board-native idempotency dedup (`--idempotency-key` = the queue's `dedup_key`), skip if the branch tip is already an ancestor of the default branch, skip if the PR is not OPEN, skip if the source reviewer card is no longer blocked, 3 consecutive failures park an item. The drainer only creates cards — it never modifies code, pushes, or merges. Ships with `agent_queue.py`, the file-locked JSON queue store, now packaged.
- **Deploy-triggered QA enqueue:** `verify-deploy-qa.py` now also enqueues a `deploy-qa` work item (deduped per deploy run) when it detects a new deploy, so QA verification fires once per deploy instead of polling on a timer.
- **`kanban-pr-watch-cron` skill: "Orchestrator-Owned Act Layer" section** documenting the architecture rule — detection is script-only; action belongs either to a deterministic playbook script or to the orchestrator session.

### Changed
- **The LLM polling tier is gone; scripts detect, playbooks act.** The four hybrid cron jobs (`ingest-ci-failures`, `ingest-deploy-failures`, `build-reviewer-resolve`, `verify-deploy-qa`) previously ran a zero-LLM detection script and *then* spun up a fresh LLM session on every tick to decide and act. The scripts were already deterministic and mostly silent; the LLM layer was ~800+ cold-start sessions/day (each loading 30–100k tokens of skill context for a poll that was a no-op the overwhelming majority of the time — several hundred million input tokens per month of pure overhead, at flash-class pricing roughly $150–480/month). All four are now `no_agent=true` detectors delivering to the orchestrator chat (`origin`), with their actionable findings flowing through the agent queue to `drain-agent-queue.py`.
- **`queue-agent-processor` cron removed from the template.** Its dispatch function was already redundant (`kanban.dispatch_in_gateway: true` makes the gateway daemon dispatch ready cards every 60s), and its queue-processing function is superseded by the deterministic drainer. Its stranded-worktree triage side-duty is covered by the existing git-state audit cron.
- **Detector delivery retargeted to `origin`** so actionable findings surface in the orchestrator's own chat — the previous template target (a Telegram thread placeholder) was the source of recurring `delivery_failed` noise while executions continued silently.

### Fixed
- **`build-consolidate-prs` reopen churn (same squash false-positive as the audit script):** the new premature-close guard used ancestry checks only — squash-merged consolidation PRs leave member branch tips out of main history, so the guard reopened issues whose content had already shipped (GH-6049/6043/6039/6035), which re-triggered ingest into duplicate coder runs and a duplicate consolidation PR (#6058, near-no-op). The closed-issue path now also consults an issue-level merged-PR arbiter (any merged PR referencing the issue ⇒ content shipped). Deployed to the live cron the same day; the four issues were re-closed and 9 churned kanban cards cancelled.
- **`setup.sh` fallback delivery target** when `--telegram-target` is omitted is now `local` (a bare `telegram` target fails with "platform not configured" — the same lesson `ingest-deploy-failures` learned earlier).
- **`setup.sh` was broken from zero — found and fixed by sandboxed install simulation.** Running setup.sh against a fresh `HERMES_HOME` surfaced 9 defects, all fixed and re-verified with a clean end-to-end pass:
  1. Crashed at the first profile under `set -u` (sed referenced unset `HERMES_PROJECT_REPO`); placeholder matching now matches `${VAR:-default}` text instead of expanding the variable.
  2. Skills install failed (`cp: cannot create directory`) — category subdirectories were never created.
  3. Profiles were never *registered* — files copied under `~/.hermes/profiles/<name>/` are not a Hermes profile; `hermes profile create` now runs for all four workers and the dispatcher can actually route to them.
  4. No `config.yaml` for any profile — orchestrator now gets the shipped `config.yaml.example` (with the critical `prefill_messages_file: SOUL.md`), workers get a generated minimal config with the project as `terminal.cwd`.
  5. Board was created without `default_workdir` — setup now runs `hermes kanban boards set-default-workdir`; every portable script self-configures from this value.
  6. `telegram:<chat_id>:<thread_id>` deliver placeholders were never substituted — new `--telegram-target` flag templates them.
  7. Template comment lines leaked into `jobs.json` → invalid JSON → the cron engine loaded zero jobs. Comments are now stripped during templating.
  8. `jobs.json.template` itself was structurally broken (an entry inserted outside the jobs array by a previous edit) — repaired and template now validated as JSON in CI-able form (20 jobs).
  9. `setup.sh` accepted a non-git or remote-less `--dir` silently — pre-flight now fails fast; `gh auth status` warns when unauthenticated; `_discover_repo` no longer keeps a trailing `.git`.

### Added
- **Board-pin guard documented for `kanban create`:** the kanban CLI's "current board" (last `boards switch`) silently determines where unpinned `kanban create` lands — cards can strand on an unrelated board, invisible to the pipeline, and worktree cards go blocked if that board has no `default_workdir`. The orchestrator playbook now mandates the global pre-subcommand override (`hermes kanban --board <slug> create`) on every manual/scripted create. (`ingest-gh-issues.sh` already pinned; the drift only bit interactive creation.)
- **README "From-Zero Install" section:** prerequisites (tools, gh auth, LLM key, Telegram target, target-repo requirements), the verified install commands with all setup.sh flags, the post-install checklist, and complete environment-variable tables (setup flags, `.env` secrets, script self-configuration overrides).
### Fixed
- **Repository-wide scrub of proprietary constants (public repo):** `update-roadmap-docs.py`, `ingest-deploy-failures.py`, `audit-stranded-worktrees.py`, `audit-kanban-health.sh`, and `build-reviewer-resolve.py` hardcoded project-specific repo paths, GitHub `owner/repo`, board slugs, the developer's vault path, and user home paths — leaking project identity into this public repo and preventing reuse. All orchestrator scripts now self-configure with zero hardcoded project data: repo path from the kanban board's `default_workdir`, `owner/repo` parsed from `git remote get-url origin`, board DB discovered under `KANBAN_DIR`, and the docs vault mirror gated behind the optional `KB_VAULT` env var (unset = mirror disabled, fail-closed safety checks preserved). Overrides: `KANBAN_REPO_DIR`, `KANBAN_REPO`, `KANBAN_DB`, `KANBAN_DIR`, `WORKDIR`, `REPO`, `KB_VAULT`. Verified: all scripts resolve identical values to the previous hardcoded config, and `py_compile` + full-diff scrub grep pass.
- **Docs/skills scrub:** `kanban-ci-automation` SKILL.md and the `qa` SOUL template carried the same project identifiers in ~25 example commands (board slug, repo slug, user home paths) and the staging domain. Replaced with `<board-slug>` / `<owner>/<repo>` / `~` placeholders; the qa staging URL now requires `HERMES_STAGING_URL` with no baked-in default. A repo-wide `git grep` for project identifiers now returns clean except the LICENSE copyright holder and the README clone URL (both legitimately the repo's own identity).

### Fixed
- **`build-consolidate-prs` premature-close stranding (critical):** `is_blocked()` treated a CLOSED GitHub issue as "work resolved" and archived the group's completed coder+reviewer cards **without creating a PR**. A multi-phase epic closed at phase 1 therefore buried every later phase's completed, review-approved work indefinitely (production incident: an epic closed on phase-1 merge day; a finished phase-2 branch sat stranded for 5 days). The closed-issue path now verifies each branch's content actually reached `main` (merged-PR check or ancestry) before archiving; unmerged cards are kept and the issue is auto-reopened with an evidence comment so the next tick consolidates normally.
- **`build-consolidate-prs` hardcoded board path:** one archive path updated the kanban DB via a hardcoded `<board>/kanban.db` sqlite3 call — cards on other boards were missed. Replaced with the shared parameterized `archive_coder_card()`.
- **`build-consolidate-prs` + `audit-archived-unmerged` hardcoded project constants:** repo path / GitHub repo / board slug were hardcoded, leaking project identity into the public repo and breaking reuse. Both scripts now self-configure: repo root from the kanban board's `default_workdir` (`board.json`), GitHub `owner/name` parsed from `git remote get-url origin`, with `KANBAN_REPO_DIR` / `KANBAN_REPO` / `KANBAN_DIR` env overrides. Deployed behavior is unchanged when the board has `default_workdir` set (required by the worktree workflow anyway).

### Added
- **`audit-archived-unmerged` cron + script (every 15m, no_agent):** the detection counterpart to the stranding fix. Sweeps archived coder cards (30-day window) for worktree branches whose content never reached `main` and have no merged PR, then reopens the linked closed issue with an evidence comment and reports via Telegram. Scale-safe by design: one `ls-remote` for all branch tips, fast tip-in-main-history skip, budgeted `git cherry` content checks (squash-merge proof), issue-level merged-PR arbiter (consolidation PRs squash-merge coder branches, so per-branch patch-ids never match), and a once-per-card alert state file so superseded iterations don't re-alert every tick. Reopens only CLOSED issues — findings on open issues are informational only.

### Fixed
- **`ingest-gh-issues` creates worktree coder+reviewer pairs, not scratch triage cards:** Previously every `ready-for-agent` issue became a scratch card assigned to `orchestrator`, requiring manual re-dispatch into a coder/reviewer pair with worktrees. The script now creates the coder card directly with `--workspace worktree --assignee coder` and a paired reviewer card (`--assignee code-reviewer --parent <coder_id>`) so workers immediately get real branches.
- **`build-consolidate-prs` lost-branch warnings:** Done cards with non-`[GH-N]` titles (e.g. `[PRFIX-...]`) had no way to resolve a lost branch — no GH issue to look up. The script now archives them silently: the card and its reviewer are both done, the work was consumed, the branch cleanup is expected. Warns only for cards where the work is genuinely at risk.
- **`build-consolidate-prs` conflict-resolution card noise:** Done cards titled `Resolve merge conflicts` have no GH issue reference and their branches are ephemeral worktrees. The script fell through to `branch_lost` on every tick with no way to resolve. Fixed by skipping these titles early in `check_dedup_and_branch` — no consolidation attempt, no notification. Cards with lost branches are archived immediately.

### Added
- **`update-roadmap-docs` cron + script:** New daily job (9am) keeping roadmap docs current without human prompting:
  - **Merged track (mechanical):** queries merged PRs since last run, appends one Version History row keyed by the current `pyproject.toml` version, adds Completed Initiative rows for PRs that close GH issues, bumps header version/date.
  - **Upcoming track (curator-assisted):** builds a candidate pool (open `feat`/`epic`/`ux` issues, unresolved PRD gaps, ready kanban cards), flags NEW candidates, warns when roadmap docs go >14 days stale. Never invents priorities — one actionable Telegram digest only when there is something to report.
  - **feature-map.md auto-update:** flips rows to ✅ Ready when a merged PR closes the GH issue the row references (e.g. `GH-6035` in notes), bumps header version, recomputes Summary table with counts.
  - **gap-analysis.md auto-update:** marks prioritization rows resolved for GH-issue matches.
  - Safety: checks origin URL + branch before any push (prevents wrong-repo push), preserves CRLF line endings, pulls-rebase before vault push (Octosync-synced).
  - State file: `~/.hermes/profiles/orchestrator/state/roadmap_last_run.json`

## [2.2.0] - 2026-10-10

### Added
- **CI/CD workflow split:** Monolithic `deploy.yml` split into `ci.yml` (tests only, no GCP secrets) and `deploy.yml` (deploy only, triggered by PR merge/tag/dispatch). `ingest-ci-failures` can safely re-trigger `ci.yml` without ever risking a deploy from a worktree branch.
- **CHANGELOG auto-release on deploy:** New `scripts/release-changelog.py` moves `[Unreleased]` content into a versioned `[X.Y.Z] — YYYY-MM-DD` section after every staging deploy. Committed together with the version bump.
- **`ingest-deploy-failures`: merged-PR deploy detection.** Tag merged PR deploy failures with `merged_pr_deploy=True`. The script now treats deploy failures from merged PRs as main-level failures — the old `branch == "main"` filter silently skipped these.

### Changed
- **Workflow-level deploy guard:** `deploy.yml` now has a `workflow-guard` job that blocks any `workflow_dispatch` from a non-`main` branch. Prevents accidental staging deploys from worktree/PR branches.
- **\`build-consolidate-prs\` notification filter:** Only notifies on actionable items (PRs created, \`branch_lost\`, \`no_branch_card\`). Silently suppresses resolved/archived categories (already\_on\_main, pr\_already\_exists, already\_merged, etc.) — the script still processes them internally but no longer bothers the user with unnecessary notifications.
- **`ingest-ci-failures` no longer re-triggers CI.** The `rerun_ci()` function (which called `gh workflow run deploy.yml --ref <branch>`) was removed entirely. The script now only detects and queues failures — never dispatches any workflow.
- **`ingest-deploy-failures` query scope increased.** `--limit` raised from 5 to 20 with a `--created >=2days` time window. The old `--limit 5` was too small — failures could be pushed out of the query result by newer successful runs, causing them to be silently skipped.
- **`ingest-deploy-failures` delivery target.** Changed from bare `"telegram"` to explicit `"telegram:<chat_id>:<thread_id>"` format to resolve "no delivery target resolved" errors.

### Fixed
- **`audit-stranded-worktrees` false-positive flood:** The script derived the kanban task ID from a `wt/t_<hex>` branch as `<hex>` — but DB IDs are `t_<hex>`. Every kanban skip-check (`WHERE id = ?`) never matched, so all done-card branches got flagged as stranded and spawned recovery cards → new strands (self-amplifying loop; e.g. GH-5870..5874 were all false positives). Fixed the ID mapping to `f"t_{m.group(1)}"` and added an explicit skip for recovery/conflict-resolution cards (stranded / merge-conflict / PRFIX / cherry-pick / lint-fix titles) whose local branches are phantom worktrees.
- **Reviewer branch collision:** Git refused to check out the same branch in two worktrees. Fixed reviewer inspection to use their own unique branch and inspect coder files via `git show origin/<coder-branch>:path`.
- **Reviewer workspace blindness:** Reviewer cards using `workspace_kind=scratch` got an empty temp dir. Fixed by using `workspace="worktree"` with the coder's branch name.
- **Orchestrator profile allowlist:** SOUL.md now restricts card assignments to `coder`, `code-reviewer`, `qa` only — prevents `personal-assistant` from being misassigned to dev work.
- **`gh issue create --label` silent failure:** Label must exist in repo before use. Verified labels exist before creating issues.
- **Reviewer card branch parameter:** SOUL.md updated — reviewer `--branch` must not use the coder's branch name (git worktree collision). Reviewer gets own unique branch from `--branch` omission.
- **`ingest-gh-issues` duplicate card creation:** Script used `hermes kanban list | grep "[GH-N]"` for dedup, but `list` is paginated and misses completed cards. Every sync tick created a new card for the same GH issue. Replaced with SQL query against the kanban DB matching the PR ingestion pattern.
- **`build-consolidate-prs` repeated "branch lost" reports:** When a branch was consumed by a consolidation PR, `gh issue view` returned state `MERGED` (not `CLOSED`), so the cleanup check failed and the same card was reported as "branch lost" every cron tick. Fixed by accepting both `CLOSED` and `MERGED` as terminal states.

## [2.1.0] - 2026-08-12

### Changed
- **`ingest-deploy-failures` now only monitors `main` branch.** Previously handled non-main branch (worktree/PR) failures by re-triggering CI on the assumption "open PR = fix exists, just needs CI re-run". This was wrong — CI failures on PRs need actual fix cards. Non-main failures are now silently deferred to `ingest-ci-failures` (every 5m) which enqueues fix tasks via the agent queue.
- **`test-failure` label created.** The label didn't exist in the repo, causing `gh issue create --label "ready-for-agent,test-failure"` to silently fail. Created with red color `d73a4a`.

### Fixed
- **`ingest-deploy-failures` PR filter skipping merge events.** Line 90-91 filtered out all `pull_request_target` runs where the PR was already merged — but deploy-to-staging only runs on `closed + merged` events. The filter was silently discarding the very runs that needed monitoring. Fixed by checking deploy job presence before filtering out merged-PR runs.
- **`ingest-deploy-failures` state file stuck.** `last_run_id` was set to a run with a higher numeric ID but earlier timestamp, causing newer deploy failures with lower IDs to be skipped. State reset to 0.
- **`audit-kanban-health` shell syntax.** `grep -ci || echo 0` produced `"0\n0"` instead of `0` when no matches, causing `[: 0 0: integer expression expected`. Fixed with `|| true` pattern.

## [2.0.0] - 2026-08-12

### Changed
- **Phased cron naming:** All cron jobs renamed to `{phase}-{action}` format (e.g. `pr-consolidation-watch` → `build-consolidate-prs`). Phase prefix makes pipeline sequence obvious in listings.
- **Merge strategy:** `gh pr merge --squash` → `gh pr merge --merge` (merge commit). Preserves branch commit hashes on main so `git merge-base --is-ancestor` works correctly.
- **Merge guard tightened:** Now requires BOTH `mergeable == "MERGEABLE"` AND `mergeStateStatus == "clean"`. Only merges when all CI is green — no pending or failing checks.
- **Schedule swap:** `build-consolidate-prs` 10m → 5m (creates PRs faster), `merge-ready-prs` 5m → 10m (CI takes ~15min anyway).
- **Version bump moved:** From `build-consolidate-prs` to `merge-ready-prs`. Single bump per tick after all merges land — avoids per-PR collision conflicts.
- **Zero-commits auto-archive:** `build-consolidate-prs` checks `git rev-list --count origin/main..branch` before creating PR. If 0 commits (content already on main), archives coder cards and skips.
- **`--ignore-scripts` added** to Lighthouse CI npm install to avoid `patch-package: not found` postinstall error.
- **Genericized all hardcoded paths.** Kanban DB path, project directory, repo slug replaced with `${HERMES_PROJECT_*}` env var placeholders.

### Added
- **`ingest-deploy-failures`** state reset. State file was stuck on an old run ID, causing newer deploy failures to be silently skipped. Reset to 0 to catch all failures fresh.
- **Pipeline diagram** (`dev-workflow-pipeline.html`) — light-themed visual of the full happy path and hygiene separation.

### Fixed
- **Deploy env var mismatch:** Documentation now warns that deploy.yml service name env vars must match between definition and reference (`SERVICE_NAME` vs `SERVICE_NAME_STAGING` suffix mismatch).

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
- **Genericized hardcoded project references.** Board name, project directory, repo slug, and QA agent identity are now templated via environment variables (`HERMES_KANBAN_BOARD`, `HERMES_PROJECT_DIR`, `HERMES_PROJECT_REPO`), so the repo bootstraps onto any project.
- Removed all references to the internal project-specific skill (not shipped in the portable repo); docs now reference `project-operations`.

### Fixed
- Hardcoded board name in the orchestrator `SOUL.md` templated for portability.
- QA profile identity made project-agnostic ("QA verification agent for this project").
