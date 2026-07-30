---
name: kanban-worker
description: Pitfalls, examples, and edge cases for Hermes Kanban workers. The lifecycle itself is auto-injected into every worker's system prompt as KANBAN_GUIDANCE (from agent/prompt_builder.py); this skill is what you load when you want deeper detail on specific scenarios.
version: 2.0.0
platforms: [linux, macos, windows]
environments: [kanban]
metadata:
  hermes:
    tags: [kanban, multi-agent, collaboration, workflow, pitfalls]
    related_skills: [kanban-orchestrator]
---

# Kanban Worker — Pitfalls and Examples

> You're seeing this skill because the Hermes Kanban dispatcher spawned you as a worker with `--skills kanban-worker` — it's loaded automatically for every dispatched worker. The **lifecycle** (6 steps: orient → work → heartbeat → block/complete) also lives in the `KANBAN_GUIDANCE` block that's auto-injected into your system prompt. This skill is the deeper detail: good handoff shapes, retry diagnostics, edge cases.

## Workspace handling

Your workspace kind determines how you should behave inside `$HERMES_KANBAN_WORKSPACE`:

| Kind | What it is | How to work |
|---|---|---|
| `scratch` | Fresh tmp dir, yours alone | Read/write freely; it gets GC'd when the task is archived. |
| `dir:<path>` | Shared persistent directory | Other runs will read what you write. Treat it like long-lived state. Path is guaranteed absolute (the kernel rejects relative paths). |
| `worktree` | Git worktree at the resolved path | **If `$HERMES_KANBAN_BRANCH` is set** (base branch specified by the orchestrator): run `git worktree add -b wt/$HERMES_KANBAN_TASK <path> $HERMES_KANBAN_BRANCH` from the main repo — this creates a new worktree branch from the correct base. Then cd and work normally. Commit work here.<br><br>**If `$HERMES_KANBAN_BRANCH` is NOT set** (no base specified): run `git worktree add <path> wt/$HERMES_KANBAN_TASK` from the main repo, creating from HEAD.

### ⚠️ BRANCH GUARDRAIL — CRITICAL

**Before writing any code in a worktree, you MUST verify the current branch and the base branch:**

```bash
git branch --show-current
git log --oneline -1
```

- **If the branch is `main` or `master`** — STOP. You are in a worktree on the wrong branch. Do not write any code. Block the task with:
  ```python
  kanban_block(reason="CRITICAL BRANCH ERROR: worktree resolved to main/master. Cannot implement here.")
  ```
- **If the branch is `wt/t_<task_id>` or `fix/df-*` or another worktree branch** — proceed. You are on the correct branch.
- **If the branch is anything else (develop, feature/..., etc.)** — STOP. This is not the expected worktree branch something is wrong. Block the task.
- **Check the base branch.** The card body says `BASE BRANCH: <name>`. Verify the worktree was created from that base by checking `$HERMES_KANBAN_BRANCH`:
  ```bash
  echo "Base branch: $HERMES_KANBAN_BRANCH"
  ```
  If the base branch in the env var doesn't match the card body, the worktree was created from the wrong base — block the task.

**Never commit, push, or write code to `main` or `master` — ever.** There is no "small fix" exemption. |

## Tenant isolation

If `$HERMES_TENANT` is set, the task belongs to a tenant namespace. When reading or writing persistent memory, prefix memory entries with the tenant so context doesn't leak across tenants:

- Good: `business-a: Acme is our biggest customer`
- Bad (leaks): `Acme is our biggest customer`

## Handoff decision: complete vs block

**Default: complete.** The orchestrator creates a paired reviewer card via `parents=[coder_task_id]` for every implementation task. The reviewer auto-promotes to `ready` when the coder completes. The coder's job is to implement, test, and **complete** — not to block for review.

**Block only when** you hit a genuine roadblock that needs human input (ambiguous requirement, missing credential, broken toolchain). **Do NOT block for review — the review gate is the orchestrator's responsibility.** If you call `kanban_block(reason="review-required:...")`, a watchdog cron will auto-complete your card within 5 minutes anyway — you're wasting your own time. Always call `kanban_complete()` with structured metadata instead.

## Good summary + metadata shapes

The `kanban_complete(summary=..., metadata=...)` handoff is how downstream workers read what you did. Patterns that work:

**Coding task (default — auto-review gate via parent link):**

```python
kanban_complete(
    summary="shipped rate limiter — token bucket, keys on user_id with IP fallback, 14 tests pass",
    metadata={
        "changed_files": ["rate_limiter.py", "tests/test_rate_limiter.py"],
        "tests_run": 14,
        "tests_passed": 14,
        "decisions": ["user_id primary, IP fallback for unauthenticated requests"],
    },
)
```

The reviewer card was created by the orchestrator with `parents=[this_task_id]`. The dispatcher promotes it to `ready` automatically. **Do not block for review.**

**Coding task without paired reviewer (human review only):**

This is an extremely rare exception. If there is genuinely no paired reviewer card (check `kanban_show` for `children`), block with `reason` prefixed `review-required:`. But first, **always check** — the orchestrator creates paired reviewers for all code tasks. If you see a child card, you must `kanban_complete()`, not block.

```python
# ⚠️ ONLY if kanban_show confirms NO reviewer card exists:
import json

kanban_comment(
    body="review-required handoff:\n" + json.dumps({
        "changed_files": ["rate_limiter.py", "tests/test_rate_limiter.py"],
        "tests_run": 14,
        "tests_passed": 14,
        "diff_path": "/path/to/worktree",
        "decisions": ["user_id primary, IP fallback for unauthenticated requests"],
    }, indent=2),
)
kanban_block(
    reason="review-required: rate limiter shipped, 14/14 tests pass — needs eyes on the user_id/IP fallback choice before merging",
)
```

**Research task:**
```python
kanban_complete(
    summary="3 competing libraries reviewed; vLLM wins on throughput, SGLang on latency, Tensorrt-LLM on memory efficiency",
    metadata={
        "sources_read": 12,
        "recommendation": "vLLM",
        "benchmarks": {"vllm": 1.0, "sglang": 0.87, "trtllm": 0.72},
    },
)
```

**Review task:**
```python
kanban_complete(
    summary="reviewed PR #123; 2 blocking issues found (SQL injection in /search, missing CSRF on /settings)",
    metadata={
        "pr_number": 123,
        "findings": [
            {"severity": "critical", "file": "api/search.py", "line": 42, "issue": "raw SQL concat"},
            {"severity": "high", "file": "api/settings.py", "issue": "missing CSRF middleware"},
        ],
        "approved": False,
    },
)
```

Shape `metadata` so downstream parsers (reviewers, aggregators, schedulers) can use it without re-reading your prose.

## Claiming cards you actually created

If your run produced new kanban tasks (via `kanban_create`), pass the ids in `created_cards` on `kanban_complete`. The kernel verifies each id exists and was created by your profile; any phantom id blocks the completion with an error listing what went wrong, and the rejected attempt is permanently recorded on the task's event log. **Only list ids you captured from a successful `kanban_create` return value — never invent ids from prose, never paste ids from earlier runs, never claim cards another worker created.**

```python
# GOOD — capture return values, then claim them.
c1 = kanban_create(title="remediate SQL injection", assignee="security-worker")
c2 = kanban_create(title="fix CSRF middleware", assignee="web-worker")

kanban_complete(
    summary="Review done; spawned remediations for both findings.",
    metadata={"pr_number": 123, "approved": False},
    created_cards=[c1["task_id"], c2["task_id"]],
)
```

```python
# BAD — claiming ids you don't have captured return values for.
kanban_complete(
    summary="Created remediation cards t_a1b2c3d4, t_deadbeef",  # hallucinated
    created_cards=["t_a1b2c3d4", "t_deadbeef"],                   # → gate rejects
)
```

If a `kanban_create` call fails (exception, tool_error), the card was NOT created — do not include a phantom id for it. Retry the create, or omit the id and mention the failure in your summary. The prose-scan pass also catches `t_<hex>` references in your free-form summary that don't resolve; these don't block the completion but show up as advisory warnings on the task in the dashboard.

## Block reasons that get answered fast

Bad: `"stuck"` — the human has no context.

Good: one sentence naming the specific decision you need. Leave longer context as a comment instead.

```python
kanban_comment(
    task_id=os.environ["HERMES_KANBAN_TASK"],
    body="Full context: I have user IPs from Cloudflare headers but some users are behind NATs with thousands of peers. Keying on IP alone causes false positives.",
)
kanban_block(reason="Rate limit key choice: IP (simple, NAT-unsafe) or user_id (requires auth, skips anonymous endpoints)?")
```

The block message is what appears in the dashboard / gateway notifier. The comment is the deeper context a human reads when they open the task.

## Heartbeats worth sending

Good heartbeats name progress: `"epoch 12/50, loss 0.31"`, `"scanned 1.2M/2.4M rows"`, `"uploaded 47/120 videos"`.

Bad heartbeats: `"still working"`, empty notes, sub-second intervals. Every few minutes max; skip entirely for tasks under ~2 minutes.

## Retry scenarios

If you open the task and `kanban_show` returns `runs: [...]` with one or more closed runs, you're a retry. The prior runs' `outcome` / `summary` / `error` tell you what didn't work. Don't repeat that path. Typical retry diagnostics:

- `outcome: "timed_out"` — the previous attempt hit `max_runtime_seconds`. You may need to chunk the work or shorten it.
- `outcome: "crashed"` — OOM or segfault. Reduce memory footprint.
- `outcome: "spawn_failed"` + `error: "..."` — usually a profile config issue (missing credential, bad PATH). Ask the human via `kanban_block` instead of retrying blindly.
- `outcome: "reclaimed"` + `summary: "task archived..."` — operator archived the task out from under the previous run; you probably shouldn't be running at all, check status carefully.
- `outcome: "blocked"` — a previous attempt blocked; the unblock comment should be in the thread by now.

## Notification routing

You can configure the gateway to receive cross-profile Kanban task notifications by adding `notification_sources` to `~/.hermes/config.yaml`.
- `notification_sources: ['*']` accepts subscriptions from all profiles.
- `notification_sources: ['default', 'zilor-ppt']` or `"default,zilor-ppt"` restricts subscriptions to specified profiles.
- Omitting the key keeps the default behavior (profile isolation).

## Do NOT

- Call `delegate_task` as a substitute for `kanban_create`. `delegate_task` is for short reasoning subtasks inside YOUR run; `kanban_create` is for cross-agent handoffs that outlive one API loop.
- Call `clarify` to ask the human a question. You are running headless — there is no live user to answer. The call will time out (default ~120s) and the task will sit silently in `running` with no signal that it needs input. Use `kanban_comment` (context) + `kanban_block(reason=...)` (decision needed) instead — the task surfaces on the board as blocked, the operator sees it, unblocks with their answer in a comment, and you respawn with the thread.
- Modify files outside `$HERMES_KANBAN_WORKSPACE` unless the task body says to.
- Create follow-up tasks assigned to yourself — assign to the right specialist.
- Complete a task you didn't actually finish. Block it instead.
- Block with `review-required` when a paired reviewer card exists. The reviewer card won't promote until you complete — check `children` in `kanban_show` to see if one exists.
- **Open a Pull Request.** The orchestrator creates ONE PR per epic after all sub-tasks and reviews are done. If you open a PR from your worktree, the orchestrator loses control of the branch and may create duplicate PRs. Commit your changes to the worktree branch, then complete the task — the orchestrator handles the rest.
- **Commit or push to `main` or `master` — ever.** This is a hard stop. If your worktree resolved to `main`, do not write a single line of code — block the task with `kanban_block(reason="CRITICAL BRANCH ERROR: worktree on main")`. There is no "quick fix" exemption. Only the orchestrator creates PRs to main.

## Pitfalls

**Task state can change between dispatch and your startup.** Between when the dispatcher claimed and when your process actually booted, the task may have been blocked, reassigned, or archived. Always `kanban_show` first. If it reports `blocked` or `archived`, stop — you shouldn't be running.

**Workspace may have stale artifacts.** Especially `dir:` and `worktree` workspaces can have files from previous runs. Read the comment thread — it usually explains why you're running again and what state the workspace is in.

**Don't rely on the CLI when the guidance is available.** The `kanban_*` tools work across all terminal backends (Docker, Modal, SSH). `hermes kanban <verb>` from your terminal tool will fail in containerized backends because the CLI isn't installed there. When in doubt, use the tool.

**Don't block for review when a paired reviewer card exists.** The orchestrator creates a code-reviewer card with `parents=[coder_task_id]` for every implementation task. If you call `kanban_block(reason="review-required: ...")`, the task stays blocked and the reviewer card never promotes — the review gate deadlocks. Instead, call `kanban_complete` with structured metadata. The dispatcher handles the rest. Check `kanban_show` on your task for the `children` field — if you see a reviewer card listed, you should complete, not block.

**`hermes kanban worker start` does not exist in current Hermes.** The `worker` subcommand was removed — the kanban dispatcher runs inside the gateway process, not as standalone worker systemd services. If you see `invalid choice: 'worker'` in a systemd journal, the service template is outdated. Disable the stale worker services:

```bash
systemctl --user disable --now hermes-worker@<profile>.service
```

The gateway's dispatcher handles all worker claiming, task assignment, and lifecycle management. Standalone worker processes are not needed.

## CLI fallback (for scripting)

Every tool has a CLI equivalent for human operators and scripts:
- `kanban_show` ↔ `hermes kanban show <id> --json`
- `kanban_complete` ↔ `hermes kanban complete <id> --summary "..." --metadata '{...}'`
- `kanban_block` ↔ `hermes kanban block <id> "reason"`
- `kanban_create` ↔ `hermes kanban create "title" --assignee <profile> [--parent <id>]`
- etc.

Use the tools from inside an agent; the CLI exists for the human at the terminal.
