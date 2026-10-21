#!/bin/bash
# setup.sh — Bootstrap the Hermes Agent Development Workflow for a project.
#
# Usage:
#   ./setup.sh --repo "owner/project" --dir "/path/to/project"
#
# This copies portable profiles, skills, and scripts into ~/.hermes,
# templating cron jobs with the project's values. Secrets must be
# configured manually in ~/.hermes/profiles/*/.env afterward.

set -euo pipefail

REPO=""
PROJECT_DIR=""
BOARD_SLUG=""
STAGING_URL=""
TELEGRAM_TARGET=""
DRY_RUN=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --repo) REPO="$2"; shift 2 ;;
        --dir) PROJECT_DIR="$2"; shift 2 ;;
        --board) BOARD_SLUG="$2"; shift 2 ;;
        --staging-url) STAGING_URL="$2"; shift 2 ;;
        --telegram-target) TELEGRAM_TARGET="$2"; shift 2 ;;
        --dry-run) DRY_RUN=true; shift ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

if [ -z "$REPO" ] || [ -z "$PROJECT_DIR" ]; then
    echo "Usage: $0 --repo owner/project --dir /path/to/project [--board slug] [--staging-url url]"
    exit 1
fi

BOARD_SLUG="${BOARD_SLUG:-$(basename "$PROJECT_DIR")-dev}"
STAGING_URL="${STAGING_URL:-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES="$HOME/.hermes"

# ---- Pre-flight: the project checkout must be a git repo with an origin ----
# Every script self-configures its GitHub owner/repo from `git remote get-url
# origin` in this directory — a non-repo or remote-less checkout silently
# breaks repo resolution downstream.
if [ ! -d "$PROJECT_DIR/.git" ]; then
    echo "❌ --dir is not a git repository: $PROJECT_DIR"
    echo "   The workflow drives issues/PRs on the project's origin remote."
    exit 1
fi
if ! git -C "$PROJECT_DIR" remote get-url origin >/dev/null 2>&1; then
    echo "❌ $PROJECT_DIR has no 'origin' remote."
    echo "   Add one: git -C $PROJECT_DIR remote add origin https://github.com/$REPO.git"
    exit 1
fi
if ! gh auth status >/dev/null 2>&1; then
    echo "⚠ gh CLI is not authenticated (gh auth status failed)."
    echo "  Every cron script calls gh — run 'gh auth login' or export GITHUB_TOKEN"
    echo "  into the profile .env files before starting the gateway."
fi

echo "============================================"
echo " Hermes Agent Development Workflow Setup"
echo "============================================"
echo ""
echo "  Project:     $REPO"
echo "  Directory:   $PROJECT_DIR"
echo "  Board:       $BOARD_SLUG"
echo "  Staging URL: ${STAGING_URL:-<not configured>}"
echo ""

# ---- Prerequisites ----
check_cmd() {
    if ! command -v "$1" &>/dev/null; then
        echo "❌ Missing prerequisite: $1"
        exit 1
    fi
}
check_cmd git
check_cmd gh
check_cmd hermes
check_cmd sqlite3
check_cmd python3
check_cmd jq
echo "✓ Prerequisites met"
echo ""

# ---- Copy profiles ----
copy_profile() {
    local name="$1"
    local src="$SCRIPT_DIR/profiles/$name"
    local dst="$HERMES/profiles/$name"

    if [ ! -d "$src" ]; then
        echo "  ⚠ $name: not found, skipping"
        return
    fi

    mkdir -p "$dst"

    # SOUL.md
    if [ -f "$src/SOUL.md" ]; then
        cp "$src/SOUL.md" "$dst/SOUL.md"
    fi

    # Scripts
    if [ -d "$src/scripts" ]; then
        mkdir -p "$dst/scripts"
        cp -r "$src/scripts/"* "$dst/scripts/" 2>/dev/null || true
        find "$dst/scripts" -name "*.py" -o -name "*.sh" | xargs chmod +x 2>/dev/null || true
    fi

    # Cron (template → real)
    if [ -f "$src/cron/jobs.json.template" ]; then
        mkdir -p "$dst/cron"
        # Match the full placeholder form ${VAR:-<default>} regardless of the
        # baked-in default, and replace with this install's values. Under
        # `set -u`, referencing $HERMES_PROJECT_REPO directly would abort the
        # script when unset — match the placeholder text instead.
        sed -e "s|\${HERMES_PROJECT_REPO:-[^}]*}|$REPO|g" \
            -e "s|\${HERMES_PROJECT_DIR:-[^}]*}|$PROJECT_DIR|g" \
            -e "s|\${HERMES_KANBAN_BOARD:-[^}]*}|$BOARD_SLUG|g" \
            -e "s|\${HERMES_STAGING_URL:-[^}]*}|${STAGING_URL:-staging.example.com}|g" \
            -e "s|telegram:<chat_id>:<thread_id>|${TELEGRAM_TARGET:-local}|g" \
            "$src/cron/jobs.json.template" \
            | sed '/^[[:space:]]*#/d' > "$dst/cron/jobs.json"
    fi

    # Orchestrator ships a full config template (model/provider, SOUL.md
    # prefill). Deploy it as the profile's config.yaml if none exists yet.
    if [ "$name" = "orchestrator" ] && [ -f "$src/config.yaml.example" ] && [ ! -f "$dst/config.yaml" ]; then
        cp "$src/config.yaml.example" "$dst/config.yaml"
    fi

    echo "  ✓ $name"
}

echo "Installing profiles..."

# ---- Register worker profiles (files alone don't register a profile) ----
# A directory under $HERMES/profiles is NOT a Hermes profile until
# `hermes profile create` registers it. Without this, the dispatcher has
# nothing to route to and every cron targeting the profile fails.
for name in orchestrator qa coder code-reviewer; do
    if hermes profile list 2>/dev/null | grep -qE "^ +$name "; then
        echo "  ✓ profile '$name' already registered"
    else
        hermes profile create "$name" >/dev/null 2>&1 && \
            echo "  ✓ profile '$name' created" || \
            echo "  ⚠ failed to create profile '$name' — create it manually: hermes profile create $name"
    fi
done
echo ""

copy_profile "orchestrator"
copy_profile "qa"
copy_profile "coder"
copy_profile "code-reviewer"
echo ""

# ---- Write per-profile config.yaml (model/provider/cwd) ----
# The repo ships config.yaml.example only for orchestrator; worker profiles
# get a minimal generated config. All of them need terminal.cwd pointed at
# the project and a model the dispatcher can spawn with.
write_config() {
    local name="$1"
    local prompt="$2"
    local dst="$HERMES/profiles/$name/config.yaml"
    [ -f "$dst" ] && { echo "  ⚠ $name: config.yaml exists — not overwriting"; return; }
    cat > "$dst" << EOF
# Generated by hermes-dev-workflow setup.sh — EDIT model.default to your provider/model.
model:
  default: deepseek/deepseek-v4-flash
  provider: openrouter

terminal:
  cwd: $PROJECT_DIR

agent:
  system_prompt: >-
    $prompt
EOF
    echo "  ✓ $name config.yaml"
}
write_config "orchestrator" "You are the SOUL (Technical Project Manager & Decomposer) for this workspace. Decompose issues into parallel-safe sub-tasks and route via the kanban board. NEVER implement yourself."
write_config "coder" "You are an implementation worker. Complete exactly the kanban card you were dispatched: implement, test, commit to your worktree branch, push, and hand off via kanban_complete."
write_config "code-reviewer" "You are a fail-closed code reviewer. Verify the coder's pushed branch content against the card criteria and hand off via kanban_complete (approved) or kanban_block (review-failed)."
write_config "qa" "You are a QA verification worker. Verify deployed changes against the staging environment across API, DB, browser, and version layers."
echo ""

# ---- Copy skills ----
echo "Installing skills..."
if [ -d "$SCRIPT_DIR/skills" ]; then
    mkdir -p "$HERMES/profiles/orchestrator/skills"
    for skill_dir in "$SCRIPT_DIR/skills/"*/*; do
        [ -d "$skill_dir" ] || continue
        skill_name="$(basename "$(dirname "$skill_dir")")/$(basename "$skill_dir")"
        dst="$HERMES/profiles/orchestrator/skills/$skill_name"
        mkdir -p "$(dirname "$dst")"
        rm -rf "$dst" 2>/dev/null || true
        cp -r "$skill_dir" "$dst"
        echo "  ✓ $skill_name"
    done
fi
echo ""

# ---- Create .env template ----
ENV_FILE="$HERMES/profiles/orchestrator/.env"
if [ ! -f "$ENV_FILE" ]; then
    cat > "$ENV_FILE" << EOF
# Hermes Agent Workflow — Project Secrets
# Generated by hermes-dev-workflow setup.sh

# GitHub token for API access (gh CLI)
GITHUB_TOKEN=

# OpenRouter API key (for LLM access)
OPENROUTER_API_KEY=

# Telegram bot token (for cron notifications)
TELEGRAM_BOT_TOKEN=

# Staging database URL (for QA DB-layer verification)
NEON_DATABASE_URL=
EOF
    echo "✓ Created .env template at $ENV_FILE"
    echo "  ⚠  Edit this file with your actual secrets before enabling cron jobs."
else
    echo "⚠ $ENV_FILE already exists — not overwriting."
fi
echo ""

# ---- Initialize kanban board ----
if ! hermes kanban boards list 2>/dev/null | grep -q "$BOARD_SLUG"; then
    hermes kanban boards create "$BOARD_SLUG" 2>/dev/null || true
    echo "✓ Kanban board '$BOARD_SLUG' created"
else
    echo "✓ Kanban board '$BOARD_SLUG' already exists"
fi

# ---- Point the board at the project checkout ----
# CRITICAL: worktree task creation fails without default_workdir, and every
# portable script self-configures its repo path from this value.
if hermes kanban boards set-default-workdir "$BOARD_SLUG" "$PROJECT_DIR" 2>/dev/null; then
    echo "✓ Board default_workdir set to $PROJECT_DIR"
else
    echo "⚠ Could not set default_workdir automatically. Run:"
    echo "    hermes kanban boards set-default-workdir $BOARD_SLUG $PROJECT_DIR"
fi
echo ""

# ---- Done ----
echo "============================================"
echo " Setup Complete!"
echo "============================================"
echo ""
echo "Next steps:"
echo "  1. Edit secrets:     vim ~/.hermes/profiles/orchestrator/.env"
echo "  2. Verify config:    hermes config get openrouter.api_key"
echo "  3. Start gateway:    hermes gateway start"
echo "  4. Test ingestion:   gh issue create --repo $REPO --title 'Test' --label ready-for-agent"
echo ""
echo "Cron jobs will start on next gateway restart. Monitor with:"
echo "  hermes cron list"
echo ""
