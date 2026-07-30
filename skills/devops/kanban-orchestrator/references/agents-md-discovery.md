# AGENTS.md Discovery for Kanban Workers

How Hermes discovers and injects AGENTS.md (and other context files) into a kanban-dispatched worker's system prompt.

## The Chain

```
terminal.cwd (profile config)
    ↓ bridged at gateway startup
TERMINAL_CWD (environment variable)
    ↓ runtime_cwd.py::resolve_context_cwd()
cwd_path (Path object)
    ↓ system_prompt.py → build_context_files_prompt(cwd=...)
_load_agents_md(cwd_path)
    ↓ checks for AGENTS.md | agents.md
System prompt injection
```

## Source Code Trace

### 1. `agent/runtime_cwd.py` — resolve_context_cwd()

```python
def resolve_context_cwd() -> Path | None:
    # 1. Check _SESSION_CWD contextvar (set by multi-session gateways)
    override = _session_cwd_override()
    if override:
        return Path(override).expanduser()
    # 2. Check TERMINAL_CWD env var (set from profile's terminal.cwd)
    raw = os.environ.get("TERMINAL_CWD", "").strip()
    return Path(raw).expanduser() if raw else None  # None = fallback to os.getcwd()
```

Priority: `_SESSION_CWD` > `TERMINAL_CWD` > `os.getcwd()` (fallback in `build_context_files_prompt`).

### 2. `agent/prompt_builder.py` — build_context_files_prompt()

```python
def build_context_files_prompt(cwd=None, ...):
    if cwd is None:
        cwd = os.getcwd()           # fallback for local CLI
    cwd_path = Path(cwd).resolve()
    # First match wins (only ONE project context type loaded):
    project_context = (
        _load_hermes_md(cwd_path, ...)       # .hermes.md / HERMES.md
        or _load_agents_md(cwd_path, ...)     # AGENTS.md / agents.md
        or _load_claude_md(cwd_path, ...)     # CLAUDE.md / claude.md
        or _load_cursorrules(cwd_path, ...)   # .cursorrules / .cursor/rules/*.mdc
    )
```

Only ONE project context file type is loaded per session. First match wins.

### 3. `agent/prompt_builder.py` — _load_agents_md()

```python
def _load_agents_md(cwd_path, context_length=None):
    for name in ["AGENTS.md", "agents.md"]:
        candidate = cwd_path / name
        if candidate.exists():
            content = candidate.read_text(encoding="utf-8").strip()
            if content:
                content = _scan_context_content(content, name)
                # Scanned for prompt injection / promptware patterns
                ...
```

Content passes through a threat-pattern scanner (`_scan_context_content`). Matches are replaced with `[BLOCKED: ...]` placeholders — the file still loads, only the offending content is masked.

## When TERMINAL_CWD Is Set vs Unset

| Scenario | TERMINAL_CWD | cwd for context files |
|---|---|---|
| Gateway dispatches kanban worker | Set from profile's `terminal.cwd` | Profile's working directory → AGENTS.md found |
| Local CLI session | Unset | `os.getcwd()` (where user launched `hermes`) |
| Cron job (no `workdir`) | Depends on scheduler cwd | Scheduler's working directory |
| Cron job (with `workdir`) | `workdir` overrides | The `workdir` path → AGENTS.md found |

## Debugging Checklist

If AGENTS.md isn't loading in a worker session:

1. **Check `terminal.cwd` on the worker profile** — is it set to the project root?
   ```bash
   hermes config show | grep -A2 'terminal:' | grep cwd
   ```

2. **Verify TERMINAL_CWD at runtime** — look for the env var in the gateway logs:
   ```bash
   grep TERMINAL_CWD ~/.hermes/logs/gateway.log | tail -5
   ```

3. **Check for scanner blocks** — the threat scanner logs blocks:
   ```bash
   grep -i "AGENTS.md blocked" ~/.hermes/logs/agent.log
   ```

4. **Verify AGENTS.md exists at the cwd** — is the file actually there?
   ```bash
   ls -la "$(hermes config get terminal.cwd 2>/dev/null || echo .)/AGENTS.md"
   ```

5. **Confirm no higher-priority file exists** — AGENTS.md is only loaded if no `.hermes.md` or `HERMES.md` is found first in the same directory.

## Key Files

- `agent/runtime_cwd.py` — `resolve_context_cwd()`, `set_session_cwd()`
- `agent/prompt_builder.py` — `build_context_files_prompt()`, `_load_agents_md()`, `_load_hermes_md()`
- `agent/system_prompt.py` — calls `build_context_files_prompt(cwd=resolve_context_cwd())`