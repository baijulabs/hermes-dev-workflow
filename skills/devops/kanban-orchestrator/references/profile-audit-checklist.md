# Profile Audit Checklist

Run this against **every worker profile** when reviewing fleet configuration.

## 1. System Prompt

- [ ] Profile has `agent.system_prompt` set
- [ ] Prompt matches the profile's role (coder = implementer, reviewer = fail-closed, orchestrator = decomposer/router)
- [ ] Prompt is NOT the default/generic "You are a technical expert"

## 2. Working Directory (cwd)

- [ ] `terminal.cwd` is a valid path for the host OS
- [ ] On WSL: path is `/home/user/...` (NOT `\\\\wsl.localhost\\...`)
- [ ] Path actually exists on disk
- [ ] Path is the project root (usually where AGENTS.md lives)

## 3. Delegation Model

- [ ] `delegation.model` is pinned (not empty string)
- [ ] `delegation.provider` is pinned (not empty string)
- [ ] The pinned model actually exists (check against model cache)
- [ ] Model is suitable for the profile's tasks (coding models for coders)

## 4. Model Name Validity

- [ ] `model.default` resolves to a real model
- [ ] `x_search.model` resolves to a real model (if configured)
- [ ] Check cache for typos: `python3 -c "import json; d=json.load(open('path/to/cache.json')); [print(k) for k in d if 'partial-name' in k]"`

## 5. SOUL Identity

- [ ] If orchestrator: `prefill_messages_file` points to an existing `SOUL.md`
- [ ] The `SOUL.md` file exists and contains the full identity + decomposition rules
- [ ] `agent.system_prompt` carries a compact version of the same identity

## 6. Duplicate Profiles

- [ ] No two profiles are byte-for-byte identical (unless intentionally load-balanced)
- [ ] Clones have been consolidated or differentiated

## 7. Gateway Status

- [ ] Active worker profiles have gateway running (`hermes profile list`)
- [ ] Orphaned profile directories removed via `echo "name" | hermes profile delete name`

## Fix Commands Reference

```bash
# Set system prompt
hermes config set agent.system_prompt "Your identity here"

# Fix WSL path
hermes config set terminal.cwd "/home/user/project"

# Pin delegation model
hermes config set delegation.model "deepseek/deepseek-v4-flash"
hermes config set delegation.provider "openrouter"

# Set SOUL identity file
hermes config set prefill_messages_file "SOUL.md"

# Fix model typo
hermes config set x_search.model "grok-4.20"

# Remove dead profile
echo "profile-name" | hermes profile delete profile-name
```