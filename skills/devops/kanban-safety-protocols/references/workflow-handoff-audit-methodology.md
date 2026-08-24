# Workflow Handoff Audit Methodology

A systematic method for auditing every transition boundary in the Hermes kanban development pipeline. Each boundary is evaluated on: what it assumes, what can silently fail, whether that failure has happened in production, and whether a guardrail exists.

## Trigger

Use this when:
- A new failure mode is discovered and you need to find related ones
- The user asks "review the workflow for gaps"
- A cron job, script, or handoff point has been modified and the impact on adjacent boundaries needs assessment
- Too many bugs are "falling through the cracks" and a structural review is needed

## Method

Walk the entire flow issue-by-issue, transition-by-transition. At EVERY handoff boundary between two components, ask:

1. **Does component A verify component B actually completed its work?** (not just "claimed done" — verified the artifact exists)
2. **What happens if B silently fails?** (uncommitted work, unpushed branch, detached HEAD, wrong branch, test failure)
3. **What telemetry exists to detect the failure?** (cron output, kanban events, git state, GH comments)
4. **Has this failure happened in production?** (cite actual GH issues / commits)

## Boundaries to Audit

```
GH Issue → Ingest → Decompose → Coder → Reviewer → Consolidate → PR → Merge → Deploy → QA
```

### 1. GH Issue → Ingestion
- Does the script handle missing labels?
- What if the label doesn't exist in the repo (`gh issue create --label` fails silently)?
- What if a rate limit is hit mid-batch?
- What if an issue was already closed and gets re-ingested?
- Are issues ingested with `label: ready-for-agent` but re-ingested on every label change?

### 2. Ingestion → Orchestrator Decomposition
- Does the orchestrator know what profiles exist before decomposing?
- Is file isolation actually enforced between parallel coders?
- Is `branch_name` set on every `kanban_create` call? If not, the coder gets detached HEAD.
- Are reviewer cards created with `workspace_kind=worktree` (not the default `scratch`)?
- Does the card body include `BASE BRANCH:` and the `CRITICAL:` guardrail?

### 3. Orchestrator → Coder (Dispatch)
- Does the coder receive the correct `branch_name` from the env?
- Is `--branch` passed on every `kanban_create` for named branches? (Omitted for `main`-base feature work.)
- Does the worktree creation use `-b` flag? (Without it, detached HEAD — GH-1701 root cause.)
- Does the coder commit before completing? (GH-1731 — coder completed with dirty worktree.)
- Does the coder push before completing? (GH-1701 — commits lost after worktree prune.)

### 4. Coder → Reviewer (Auto-promote)
- Does the reviewer have access to the coder's files? (Requires `workspace_kind=worktree` AND the coder's branch name.)
- Can the reviewer verify the coder committed? (`git status --short` check.)
- Can the reviewer verify the branch was pushed? (`git branch -r --list "origin/$(git branch --show-current)"`.)
- Is the reviewer workflow instructing these checks?
- Is the reviewer card created with the coder's branch? (See Layer 1 in Reviewer Workspace Blindness protocol.)

### 5. Reviewer → Consolidation
- Does the branch exist on origin? If not, script marks "lost" and skips silently.
- Does the script fetch all refs including `wt/*`? (Was only fetching `origin/main` until Aug 2026 fix.)
- Can the script recover from a pruned worktree? (`recover_from_worktree()` — only if worktree dir still exists.)
- What notification is sent when a pair is skipped? (None in consolidate mode — GH-1701/1731.)

### 6. Consolidation → PR Creation
- Does the PR body include `Closes #N` for every covered issue? (Without it, issue never auto-closes on merge.)
- Does the PR title correctly reflect the commits?
- What if the consolidation branch already exists on origin? (Force-push or skip?)
- What if the PR already exists for this branch? (Dedup by `gh pr list --state all`.)

### 7. PR → Merge
- CI must be clean (`mergeStateStatus == "clean"`) AND mergeable (`mergeable == "MERGEABLE"`).
- What if CI results are stale between check and merge command? (Race condition.)
- What if another PR merged between CI run and this merge? (Base shifted, re-check MERGEABLE.)
- Is deploy cooldown active? (If a deploy run on main is in progress, merging is blocked.)
- Is merge type `--merge` not `--squash`? (Squash destroys commit hashes, breaks ancestry checks.)

### 8. Merge → Deploy
- Is the deploy running from main? (`workflow_dispatch` accepts any ref — no ancestry check.)
- Does the PR that triggered the deploy also modify `deploy.yml`? (YAML changes are never tested by PR checks — latent defect.)
- Is the deploy workflow itself the correct version? (If `deploy.yml` changed in the PR, the workflow file running CI is the OLD version.)
- Did the version get bumped? (Auto-bump step added Aug 2026, but only runs if `sync-version.sh` is executable.)

### 9. Deploy → QA
- Does QA verify individual fixes or just structural health? (Currently only structural — GH-1660 fixes were on main but not deployed, QA reported "all healthy".)
- Does QA check the deployed SHA against the merge SHA? (Fixes on main ≠ fixes deployed.)
- Does QA identify which `Closes #N` fixes should be present?
- What if the deploy rolled back silently? (No notification mechanism.)

## Output Format

For each transition, produce:

| Boundary | Assumption | Failure Mode | Production Hit? | Detection | Fix Status |
|----------|-----------|--------------|-----------------|-----------|------------|

## Reference Audit Results (Aug 2026)

See the first run of this methodology in the conversation history (Aug 24, 2026) — it identified **10 boundaries**, **8 production-hit failure modes**, and **4 P0/P1 guardrail gaps** spanning the full pipeline. Key findings:

| Priority | Gap | Boundaries | Production Evidence |
|----------|-----|------------|-------------------|
| P0 | Reviewer workspace blindness (71% scratch) | 2, 4 | GH-1731 — reviewer approved uncommitted work |
| P0 | No worktree deploy guard (any ref can deploy) | 8 | Multiple wt/t_* deploys bypassed PR process |
| P1 | No post-deploy fix verification | 9 | GH-1660 — 3 fixes on main, none on staging, QA passed |
| P1 | Consolidation script silent skips | 5 | GH-1701, GH-1731 — pairs silently skipped for hours |