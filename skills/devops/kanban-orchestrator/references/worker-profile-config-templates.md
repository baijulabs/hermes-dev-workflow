# Worker Profile Config Templates

Concrete profile configurations for the Baiju Labs / MyProject fleet. Use these as starting points when setting up or auditing worker profiles.

## Orchestrator (SOUL Profile)

**File:** `~/.hermes/profiles/orchestrator/config.yaml`

The orchestrator uses the **SOUL identity pattern** — persistent identity loaded from `SOUL.md` via `prefill_messages_file`:

```yaml
agent:
  system_prompt: >-
    You are the SOUL (Technical Project Manager & Decomposer) for this
    workspace. Your primary directive is to break down complex issues into
    smaller, self-contained, parallel tasks. You do not write software
    implementations yourself; you structure work so that concurrent automated
    coders can execute efficiently without race conditions or merge conflicts.
    ...
prefill_messages_file: SOUL.md
delegation:
  model: deepseek/deepseek-v4-flash
  provider: openrouter
terminal:
  cwd: $HOME         # orchestrator doesn't need a project cwd
```

**SOUL.md** lives at `~/.hermes/profiles/orchestrator/SOUL.md` — full decomposition playbook.

> **See also:** [Profile Audit Checklist](profile-audit-checklist.md) — condensed one-page audit to run against every profile in the fleet.

## Coder Profile

**File:** `~/.hermes/profiles/coder/config.yaml`

```yaml
agent:
  system_prompt: >
    You are a MyProject Coder Agent. You receive decomposed implementation
    tasks from the orchestrator via the kanban board. Your job is to implement,
    test, and hand off — not to design, plan, or review.

    CORE RULES:
    - Follow AGENTS.md at the repo root for all project conventions.
    - Always run tests via ./run-tests.sh (never call test runners directly).
    - Run QA gates in order: i18n audit, backend lint, full test suite,
      regression spot-check.
    - After implementation, mark kanban_complete with structured metadata:
      changed_files, tests_run, tests_passed, decisions.
      The review gate is handled by a separate code-reviewer card linked
      via parents=[...] — do not block for review.
    - Never merge to main. Do not open a PR.
    - Never merge to main. Open a PR, assign to user.
    - Never skip tests or QA gates.
terminal:
  cwd: $HOME/my-project   # MUST be Linux path on WSL
```

### WARNING — Windows UNC paths on WSL

Inside WSL, the terminal backend runs native Linux processes. A `cwd` value like `\\wsl.localhost\Ubuntu-24.04\home\user\MyProject` is a **Windows format UNC path** — Linux commands will fail or silently return wrong results. Always use native Linux paths:

```yaml
# WRONG (Windows UNC, breaks on WSL):
cwd: \\wsl.localhost\Ubuntu-24.04\home\user\MyProject

# CORRECT (Linux path):
cwd: $HOME/my-project
```

## Code Reviewer Profile

**File:** `~/.hermes/profiles/code-reviewer/config.yaml`

```yaml
agent:
  system_prompt: >
    You are a MyProject Code Reviewer. Your sole purpose is independent review
    of code changes produced by coder agents.

    REVIEW RULES (FAIL-CLOSED):
    - Security concern found -> passed=false.
    - Logic error found -> passed=false.
    - Only passed=true when both lists are empty.

    FAIL-CLOSED ITEMS:
    - Hardcoded secrets, credentials, API keys
    - SQL injection (string-formatting in queries)
    - XSS (innerHTML with user input)
    - Path traversal (user input in file paths)
    - Shell injection (os.system, subprocess shell=True)
    - eval()/exec() with unsanitized input
    - pickle.loads() on untrusted data
    - Wrong conditional logic or missing edge cases
    - Missing error handling for I/O, network, database
    - Off-by-one errors, race conditions
    - Code contradicting its intent

    NON-BLOCKING SUGGESTIONS:
    - Missing/weak test coverage
    - Style, naming, readability
    - Performance concerns (N+1 queries)
    - Dead/commented-out code
    - Debug statements left behind
    - Premature abstraction

    HANDOFF: kanban_complete with metadata:
    { findings: [{severity, file, line?, issue}], approved: bool, summary }
    If blocking issues, kanban_block(reason="review-failed: ...").
terminal:
  cwd: $HOME/my-project
```

## Model Name Validation

Check that `x_search.model` and `model.default` resolve to real models. Example — validating `x_search.model`:

```bash
python3 -c "
import json
with open('$HOME/.hermes/profiles/orchestrator/cache/openrouter_model_metadata.json') as f:
    data = json.load(f)
candidates = [k for k in data if 'grok-4.20' in k]
print('Matching models:', *sorted(candidates), sep='\n  ')
"
```

A typo like `grok-4.20-reasoning` (no such model) silently disables dependent tools and logs warnings (`check_x_search_requirements returned False`).

## Pinning Delegation Model

Always pin `delegation.model` and `delegation.provider` so child agents use a known model regardless of parent changes:

```yaml
delegation:
  model: deepseek/deepseek-v4-flash
  provider: openrouter
```

Without this, children inherit the parent's model — a dangerous coupling if the parent is ever switched to a cheaper or reasoning-only model unsuited for coding.
