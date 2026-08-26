# Consolidation Edge Cases — No-Branch Cards and Closed Issues

## Problem: Cards with no branch_name never get a PR

Coder cards created with `workspace_kind=scratch` (common before the prefill fix) have `branch_name = null` in the kanban DB. The consolidation script's SQL query previously filtered these out with `branch_name IS NOT NULL`, making them **permanently invisible** to every cron tick. The fix content sat on origin (pushed manually by the coder) but no PR was ever created, and the issue remained open indefinitely.

**Evidence:** 30 cards from the pre-fix epoch had null branch_name. GH-1725 (t_fd0f2bad) was the canonical failure: coder pushed to `agent/GH-1725` but DB never recorded it.

### Defense — Query ALL cards, handle null branches

1. Remove the `branch_name IS NOT NULL` filter from the SQL query — include cards with null branches.
2. In `check_dedup_and_branch()`, when `branch` is falsy (None or empty):
   - Extract the GH issue number from the card title: `re.findall(r'\[GH-(\d+)\]', title)`
   - If an issue number is found, check if it's already closed: `gh issue view N --json state --jq .state`
   - If closed → archive the coder card (fix already on main via another PR) and count as `no_branch_gh_closed`
   - If open or no issue → count as `no_branch_card` and return (no PR to create)

### Defense — Same pattern for blocked consolidation groups

When a consolidation group (multiple branches under one GH issue) is skipped due to `is_blocked()` (retry cooldown), the script now double-checks whether the issue is actually closed:

```python
rc, state_out, _ = run(["gh", "issue", "view", str(gh_num), "--repo", REPO,
                        "--json", "state", "--jq", ".state"], timeout=15)
if rc == 0 and state_out.strip() == "CLOSED":
    for entry in entries:
        archive_coder_card(entry["coder_id"])
    print(f"📦 GH-{gh_num} already closed — archiving N card(s)")
```

This prevents the retry-cooldown loop from suppressing cards whose issue was resolved while they were blocked.

---

## Problem: PR already merged but branch ref deleted

When a PR is merged with `--delete-branch`, the branch ref is gone. The consolidation script's `get_branch_commit_count(branch)` returns `-1`, and previously it printed `"⚠️  Branch X lost — skipping"`. This was a false alarm — the work was already on main via the merged PR.

### Defense — Check for merged PR before declaring lost

In the `count == -1` skip path, before printing "lost", call:

```python
def pr_for_branch_is_merged(branch):
    rc, out, _ = run(["gh", "pr", "list", "--state", "merged", "--head", branch,
                      "--json", "number,state", "--jq", "length"], timeout=15)
    return rc == 0 and out.strip().isdigit() and int(out.strip()) > 0
```

If the branch has a merged PR → archive the coder card and count as `already_merged`. No "lost" message, no wasted retries.

### Defense — is_blocked() checks issue state first

```python
def is_blocked(gh_num):
    rc, out, _ = run(["gh", "issue", "view", str(gh_num), "--repo", REPO,
                      "--json", "state", "--jq", ".state"], timeout=15)
    if rc == 0 and out.strip() == "CLOSED":
        return True  # blocked — issue already resolved, no point retrying
    # ... existing retry-cooldown logic ...
```

---

## Skip Counters (all reasons)

```
skip_counters = {
    "branch_lost": 0,          # branch ref gone, no merged PR exists
    "branch_empty": 0,         # branch exists but 0 unique commits
    "dedup_already_pr": 0,     # same commit set already seen (dedup)
    "already_on_main": 0,      # content already on main via different hash
    "pr_already_exists": 0,    # PR already exists for this branch
    "recovery_succeeded": 0,   # recovered branch from disk worktree
    "already_merged": 0,       # branch lost but merged PR exists → archived
    "no_branch_card": 0,       # branch_name is null and issue is still open
    "no_branch_gh_closed": 0,  # branch_name is null and issue already closed → archived
}
```

Every run prints: `Created N PR(s); Skipped M pair(s)  • reason1: X  • reason2: Y`

See `build-consolidate-prs.py` in the orchestrator profile's `scripts/` directory for the implementation.