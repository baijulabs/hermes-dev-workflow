# Local/Remote Divergence During Rebase

Detected on PR #567 (`fix/post-merge-fixes`) — July 26, 2026.

## The Pattern

Multiple agents/sessions (cron workers, TUI sessions, background delegations) independently push to the same PR branch. The local working tree falls behind the remote without the agent realising it.

## Symptom

- PR shows `mergeStateStatus: DIRTY` on GitHub
- Running `git rebase origin/main` locally outputs: `Current branch <name> is up to date.`
- Yet the PR is clearly stale — something doesn't add up

## Root Cause

Local `origin/main` was fetched, but `origin/<pr-branch>` was not. The local branch hasn't been tracking the remote PR branch. A prior session force-pushed new commits to the PR branch (e.g., after an earlier rebase + push), but this session's local checkout still points to the old commits.

## Diagnosis

```bash
# Compare local vs remote PR branch
git log --oneline origin/<pr-branch> -5
git log --oneline <pr-branch> -5
# If they diverge at different commits, local is stale
```

## Fix

```bash
# 1. Sync local to remote branch (discards local divergence)
git fetch origin <pr-branch>
git reset --hard origin/<pr-branch>

# 2. Now rebase onto main (conflicts are real, not false "up to date")
git fetch origin main
git rebase origin/main

# 3. Resolve conflicts, then force-push
git push origin <pr-branch> --force-with-lease
```

## Real-Session Transcript

Branch: `fix/post-merge-fixes`
PR: #567

Remote branch had 5 commits (including a revert commit `22b7a74` that undone the react pin).
Local branch had 6 different commits (no revert — different resolution path).

The `git rebase origin/main` claimed "up to date" because the local branch tip was a descendant of `origin/main`, but didn't check whether `origin/fix/post-merge-fixes` pointed elsewhere.

After `git reset --hard origin/fix/post-merge-fixes`, the rebase hit two real conflicts:
1. `package.json` — react version `18.3.1` (branch) vs `19.2.8` (main). Resolved by accepting main's version since a follow-up revert commit removed the pin anyway.
2. `package-lock.json` — accepted `--theirs` (main's version) since it's a generated file.