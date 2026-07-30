# Hermes Config Auto-Sync Backup

Automated disaster recovery: mirror all agent configuration (profiles, cron jobs, scripts, skills) into the project repo. A `[skip ci]` cron job keeps it in sync every hour.

## Architecture

```
~/.hermes/profiles/        (live agent config)
        │
        │  hermes-config-sync.py (every 60m, no_agent)
        ▼
repo/hermes-config/         (mirror backup in git)
        │
        ├── profiles/
        │   ├── orchestrator/
        │   ├── qa/
        │   ├── coder/
        │   └── code-reviewer/
        ├── config.yaml
        └── restore.sh
```

## Sync Script

`hermes-config-sync.py` does content-aware byte-level comparison before copying — no noise commits. Excludes secrets (`.env`, `auth.json`), cron runtime state (ticker_heartbeat, output logs, `executions.db`), and generated artifacts (venvs, `node_modules`, `__pycache__`).

### Critical details

- **`[skip ci]` in commit message** — prevents config sync pushes from triggering CI workflows
- **Retry on push failure** — if main advances between commit and push (race condition), pulls and rebases before retrying
- **Content-aware comparison** — `read_bytes()` comparison before `write_bytes()`, not timestamp-based. Returns actual change count for accurate reporting

### Cron setup

```json
{
  "name": "hermes-config-sync",
  "script": "hermes-config-sync.py",
  "no_agent": true,
  "schedule": "every 60m",
  "deliver": "local"
}
```

### Restore

```bash
git clone <repo>
cd <repo>
./hermes-config/restore.sh
# Manually restore secrets: ~/.hermes/.env, ~/.hermes/auth.json
hermes gateway restart
```

## What's synced

| Included | Excluded |
|----------|----------|
| `SOUL.md` | `.env`, `auth.json` |
| `cron/jobs.json` | `cron/ticker_heartbeat`, `cron/output/` |
| `scripts/*.py`, `scripts/*.sh` | `state-snapshots/` |
| `skills/` (no venvs) | `__pycache__`, `*.pyc`, `venv/`, `node_modules/` |
| `config.yaml` | `state.db`, `sessions/`, `kanban/` |
| `hermes-config-sync.py` itself | `executions.db` |
