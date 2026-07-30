# Hermes Agent Development Workflow

A portable, automated development pipeline powered by Hermes Agent multi-profile kanban system. Bootstraps issue ingestion, decomposition, parallel coder+reviewer implementation, PR consolidation, and QA verification onto any project with one command.

## Quick Start

```bash
git clone https://github.com/baijulabs/hermes-dev-workflow.git
cd hermes-dev-workflow
./setup.sh --repo "owner/project" --dir "/path/to/project"
```

Edit secrets, start the gateway, and label an issue `ready-for-agent`. The pipeline picks it up within 15 minutes.

## Architecture

```
GitHub Issue (ready-for-agent)
        │
        ▼  gh-issues-to-kanban (every 15m)
        │
┌─────────────────────────────────┐
│  Orchestrator                   │
│  Decomposes → coder+reviewer    │
└─────────────────────────────────┘
        │
        ▼  Dispatcher (continuous)
┌─────────────┐   ┌──────────────┐
│  Coder      │   │  Reviewer    │
│  Worktree   │ → │  Quality     │
│  Impl       │   │  Gate        │
└─────────────┘   └──────────────┘
        │
        ▼  pr-consolidation-watch (every 10m)
┌─────────────────────────────────┐
│  Version bump → PR → CI → Deploy│
└─────────────────────────────────┘
        │
        ▼  QA (every 10m + weekly)
┌─────────────────────────────────┐
│  API → DB → Browser → Version   │
└─────────────────────────────────┘
```

## Profiles

| Profile | Role |
|---------|------|
| `orchestrator` | Decomposes issues into parallel-safe sub-tasks, owns PR creation |
| `coder` | Implements fixes in isolated git worktrees, never commits to main |
| `code-reviewer` | Reviews implementations for correctness, security, conventions |
| `qa` | 4-layer verification (API/DB/Browser/Version) + weekly dogfood |

## Cron Jobs

| Job | Schedule | Purpose |
|-----|----------|---------|
| `gh-issues-to-kanban` | every 15m | Ingests labeled issues/PRs into kanban |
| `pr-check-watch` | every 15m | Detects CI failures and merge conflicts on open PRs |
| `staging-deploy-watch` | every 10m | Detects deploy failures, creates fix cards |
| `pr-consolidation-watch` | every 10m | Creates PRs from done coder+reviewer pairs with version bumps |
| `review-failed-watch` | every 5m | Auto-resolves blocked reviewer cards |
| `worktree-collision-watch` | every 5m | Fixes worktree branch collisions |
| `active-pr-guard-watch` | every 5m | Unsticks cards blocked by active PR guards |
| `coder-review-required-watch` | every 5m | Auto-completes coder review-required blocks |
| `hermes-config-sync` | every 60m | Mirrors agent config into project repo |
| `prune-worktrees` | every 360m | Cleans stale git worktrees |
| `qa-verify-deploy` | every 10m | 4-layer fix verification on staging deploys |
| `dogfood-weekly` | Saturday 8 AM | Full-site exploratory QA scan |

## Environment Variables

Set in `~/.hermes/profiles/orchestrator/.env`:

| Variable | Required | Purpose |
|----------|----------|---------|
| `GITHUB_TOKEN` | Yes | GitHub API access |
| `OPENROUTER_API_KEY` | Yes | LLM access for agent profiles |
| `TELEGRAM_BOT_TOKEN` | Recommended | Cron job notifications |
| `HERMES_PROJECT_REPO` | Auto-set | `owner/repo` slug |
| `HERMES_PROJECT_DIR` | Auto-set | Local project path |
| `HERMES_KANBAN_BOARD` | Auto-set | Board slug |
| `HERMES_STAGING_URL` | Optional | For QA verification |
| `NEON_DATABASE_URL` | Optional | For QA DB-layer checks |

## Project-Specific Configuration

The SOUL.md files contain a `## Project-Specific Context` section. Edit this to describe your application's architecture, endpoints, and verification patterns. The workflow adapts its QA checks accordingly.

## Disaster Recovery

The `hermes-config-sync` cron mirrors all agent config into `hermes-config/` in your project repo. To restore:

```bash
git clone https://github.com/owner/project.git
cd project
./hermes-config/restore.sh
# Restore secrets manually
vim ~/.hermes/.env
hermes gateway restart
```

## Documentation

- `docs/hermes-agent-development-workflow.md` — Full pipeline reference with all jobs, skills, and safety protocols
- Profile SOUL.md files — Per-profile identity documents with verification patterns
- Skill SKILL.md files — Reusable procedural knowledge

## Requirements

- Hermes Agent installed
- `gh` CLI authenticated
- `python3`, `sqlite3`, `jq` available
- Git repository with `main` branch
