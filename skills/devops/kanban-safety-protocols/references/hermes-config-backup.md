# Hermes Config Backup & Restore

Portable backup of Hermes configuration for restoring on a new instance.

## Automated Mirror into Repo (preferred)

The `hermes-config-sync.py` script mirrors critical agent configuration into `hermes-config/` in the MyProject repo. A `hermes-config-sync` cron job (every 60m, no_agent) detects changes via byte-level comparison and auto-commits + pushes to main.

**What's synced:** `config.yaml`, all profiles' `SOUL.md`, `cron/jobs.json`, `scripts/`, `skills/` (no venvs, no node_modules). Cron runtime noise (`ticker_heartbeat`, `output/`, `executions.db`) is excluded.

**What's excluded:** `.env`, `auth.json`, state DBs, sessions, kanban DBs. Secrets must be restored manually.

**Repo structure:**
```
MyProject/hermes-config/
├── config.yaml
├── profiles/
│   ├── orchestrator/   # SOUL.md, cron/, scripts/, skills/
│   ├── qa/             # SOUL.md, cron/, scripts/
│   ├── coder/          # SOUL.md
│   └── code-reviewer/  # SOUL.md
└── restore.sh          # one-command restore
```

**Script:** `~/.hermes/profiles/orchestrator/scripts/hermes-config-sync.py`
**Cron:** `hermes-config-sync` (every 60m, no_agent, deliver=local)

**Restore on new machine:**
```bash
git clone https://github.com/my-org/my-project.git
cd MyProject
./hermes-config/restore.sh
# Manually restore secrets:
#   - ~/.hermes/.env (OpenRouter key, Telegram token, etc.)
#   - ~/.hermes/auth.json
#   - ~/.hermes/profiles/qa/.env (NEON_DATABASE_URL)
hermes gateway restart
```

**Pitfall: content-aware comparison.** The sync script uses byte-level comparison for every file. If cron runtime files (`ticker_heartbeat`, `output/*.md`, `executions.db`) are not excluded, they generate noise commits every tick. The exclusion list must cover all files in `cron/` except `jobs.json`.

## Manual Backup (tar/gpg)

### What to Back Up

#### Essential configs (portable, no machine-local state)
- `~/.hermes/config.yaml` — root config
- `~/.hermes/profiles/*/config.yaml` — per-profile configs
- `~/.hermes/profiles/*/SOUL.md` — identity documents
- `~/.hermes/profiles/*/.env` — API keys and tokens (secrets!)
- `~/.hermes/auth.json` — authentication store (secrets!)
- `~/.hermes/scripts/` — symlinked scripts
- `~/.hermes/profiles/*/scripts/` — per-profile scripts
- `~/.hermes/profiles/*/skills/` — skill definitions (exclude venvs, node_modules)
- `~/.hermes/profiles/*/plugins/` — plugin definitions
- `~/.hermes/profiles/*/cron/` — cron job definitions

#### NOT to back up (machine-local, regenerated)
- `~/.hermes/kanban/` — SQLite DBs, worker logs, worktrees
- `~/.hermes/cache/` — model caches, temp files
- `~/.hermes/sessions/` — session history DB
- `~/.hermes/profiles/*/logs/` — worker logs
- `~/.hermes/profiles/*/state-snapshots/` — local snapshots
- `venv/`, `node_modules/`, `__pycache__/` — regenerated artifacts

### Backup Command

```bash
# Configs + identities + auth + scripts
tar czf ~/hermes-config.tar.gz \
  ~/.hermes/config.yaml \
  ~/.hermes/profiles/*/config.yaml \
  ~/.hermes/profiles/*/SOUL.md \
  ~/.hermes/profiles/*/.env \
  ~/.hermes/auth.json \
  ~/.hermes/scripts/ \
  ~/.hermes/profiles/*/scripts/

# Skills + plugins + cron (exclude local artifacts)
tar czf ~/hermes-skills.tar.gz \
  --exclude='*__pycache__' \
  --exclude='*venv*' \
  --exclude='*venv_*' \
  --exclude='*node_modules' \
  --exclude='*.pyc' \
  --exclude='*/.git' \
  ~/.hermes/profiles/*/skills/ \
  ~/.hermes/profiles/*/plugins/ \
  ~/.hermes/profiles/*/cron/
```

### Restore

```bash
tar xzf ~/hermes-config.tar.gz -C ~/
tar xzf ~/hermes-skills.tar.gz -C ~/
```

### Encrypt for Storage

The `.env` and `auth.json` files contain API keys (OpenRouter, Telegram, WhatsApp, etc.). Encrypt before storing in git or cloud:

```bash
gpg --symmetric --cipher-algo AES256 ~/hermes-config.tar.gz
# Now store ~/hermes-config.tar.gz.gpg instead of the plain .tar.gz
```

## Built-in Alternative

Hermes also has `hermes backup` (full archive ~1.5GB) and `hermes import`:
```bash
hermes backup                    # 1.5GB zip — includes everything
hermes backup --quick            # State snapshot — configs only
hermes import <backup.zip>       # Restore
```

The full backup is large because it includes kanban DBs, worker logs, and skill venvs that are not needed on a fresh instance. The targeted tarball approach above produces a portable ~320MB archive. `--quick` mode captures config + cron + .env + auth but only for one profile and includes the 1GB state.db.
