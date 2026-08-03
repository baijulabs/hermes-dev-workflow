# QA Verification Pipeline

Automated post-deploy verification agent that confirms deployed fixes actually resolved their reported issues, plus exploratory dogfood QA to find new bugs. Runs as a separate Hermes profile (`qa`).

## Architecture

```
Staging Deploy Succeeds
        │
        ▼
┌──────────────────────────────────┐
│ deploy-watch.py (every 10m)      │  no_agent script
│ Detects new deploy → outputs JSON │  State: last_verified_deploy.json
└──────────────────────────────────┘
        │ (new deploy found)
        ▼
┌──────────────────────────────────┐
│ Mode 1: Fix Verification         │  4-layer: API → DB → Browser → Version
└──────────────────────────────────┘
        │
        ▼
┌──────────────────────────────────┐
│ Mode 2: Per-Deploy Targeted      │  Dogfood on changed areas
│ git diff → map files to steps    │  Load dogfood + workspace-app-qa
└──────────────────────────────────┘
```

**Weekly:** A separate `dogfood-weekly` cron runs Saturday 8 AM for a full-site scan (all 7 steps + dashboard + profile + help + vision board).

## Cron Jobs

| Job | Profile | Schedule | Mode |
|-----|---------|----------|------|
| `qa-verify-deploy` | qa | every 10m | Fix verification + per-deploy dogfood |
| `dogfood-weekly` | qa | Saturday 8 AM (`0 8 * * 6`) | Full-site dogfood scan |

Both use `deepseek/deepseek-v4-flash`, `deliver=telegram`, and load skills: `dogfood`, `workspace-app-qa`, `github-issues`, `github-pr-workflow`, `project-operations`.

## Profile Structure

```
~/.hermes/profiles/qa/
├── SOUL.md                  # Identity + two-mode workflow definition
├── .env                     # NEON_DATABASE_URL, GITHUB_TOKEN
├── scripts/
│   └── deploy-watch.py      # Polls for new deploys (idempotent, stateful)
├── cron/
│   └── jobs.json            # Two jobs: qa-verify-deploy + dogfood-weekly
└── state/
    ├── last_verified_deploy.json
    ├── verification_history.json
    ├── last_dogfood_weekly.json
    └── last_dogfood_targeted.json
```

## 4-Layer Fix Verification

| Layer | Method | Time Budget | When Applied |
|-------|--------|-------------|--------------|
| API | `curl` against staging.example.com | 30s/endpoint | Backend route bugs, response shape changes |
| DB | `psql` against staging Neon | 15s/query | Data persistence fixes (audit logs, FRS, quiz, checklist) |
| Browser | `browser_navigate` + snapshot | 60s/check | UI fixes (layout, component visibility, i18n) |
| Version | Compare deploy version vs repo | 5s | Every run |

## Dogfood Scope

**Weekly Full Scan:** All 7 steps + dashboard + profile + help + vision board. Cross-cutting checks: i18n (es/fr/pt), dark mode, 404 page, responsive layout at mobile viewport. Uses `dogfood` skill for structured exploratory testing with annotated screenshots and console error capture per `workspace-app-qa` skill.

**Per-Deploy Targeted:** Maps changed files from `git diff` to affected workspace steps. Tests only those steps plus a login/dashboard smoke test.

## Finding Handling

| Finding Type | Severity | Action |
|---|---|---|
| Fix verification failure | Any | Auto-create `Regression:` GH issue (`bug` + `QA review`) |
| Dogfood finding | Critical/High | Auto-create `[Dogfood]` GH issue (`bug` + `QA review` + `dogfood`) |
| Dogfood finding | Medium/Low | Include in report body for manual review |

All findings require manual `ready-for-agent` labeling before entering the fix cycle.

## Reporting

### Telegram (on any failure)
```
🔴 Staging v{version} — {mode}: {pass}/{total} passed
FAILED:
• GH-{N} — {title}: {reason}
Full report: {github_issue_url}
```

### GitHub Issue (always)
Full pass/fail table per verified issue with dogfood findings section. Title: `QA: Staging v{version} — {mode} — {date}`. Labels: `qa-report` (plus `dogfood` for exploratory runs).

## Deploy Watch Script Details

`deploy-watch.py` polls the deploy workflow for successful `Deploy to Staging` jobs. State is tracked in `last_verified_deploy.json` to prevent re-processing.

**Key pitfall:** The `for run in runs` loop variable shadows the `run()` helper function, causing `UnboundLocalError` at runtime. Fix: rename the loop variable to `for deploy in runs` or `for r in runs`.

**Version extraction:** Reads `backend/pyproject.toml` from the deploy run's commit SHA via GitHub Contents API, base64-decoded.

**Cron model pinning:** The `qa-verify-deploy` and `dogfood-weekly` cron jobs must explicitly declare `"model": "deepseek/deepseek-v4-flash"` and `"provider": "openrouter"`. Without explicit pinning, the cron inherits the global default model — if the global default changes, Hermes' drift guard blocks the job with: `Skipped to prevent unintended spend: global inference config drifted`.

## Staging Access Requirements

- **API:** `https://staging.example.com` — needs auth token (JWT from login flow)
- **DB:** Neon PostgreSQL — set `NEON_DATABASE_URL` in `~/.hermes/profiles/qa/.env`
- **Error handling:** If staging unreachable, report as "staging degraded", do NOT file regressions
