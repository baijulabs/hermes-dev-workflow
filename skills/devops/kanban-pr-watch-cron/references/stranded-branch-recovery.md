# Stranded Branch Recovery Procedure

Procedure for recovering code from worktree branches that were committed locally but never pushed to origin or PR'd.

## Detection

### Find all local-only branches with commits not in main

```bash
git branch --format='%(refname:short)' | grep -E '^(wt/|fix/)' | while read branch; do
  remote_exists=$(git branch -r --list "origin/$branch" | head -1)
  if [ -z "$remote_exists" ]; then
    commits=$(git rev-list --count origin/main..$branch 2>/dev/null || echo 0)
    if [ "$commits" -gt 0 ]; then
      echo "$branch|$commits"
    fi
  fi
done
```

### Deduplicate identical commits

Multiple worktree branches often contain the same fix. Group by commit hash:

```bash
git branch --format='%(refname:short)' | grep -E '^wt/' | while read branch; do
  hashes=$(git rev-list origin/main..$branch 2>/dev/null | tr '\n' ',' | sed 's/,$//')
  if [ -n "$hashes" ]; then
    echo "$branch|$hashes"
  fi
done | sort -t'|' -k2
```

## Assessment

### Check if a commit is already in main

Three methods, in order of reliability:

**1. `git cherry` (patch-id comparison)**
```bash
git cherry origin/main <branch>
# '+' = patch-id NOT in main; '-' = patch-id already in main
```
Fast but unreliable when commits were rebased (patch-id changes).

**2. Cherry-pick onto fresh main (most reliable)**
```bash
git checkout -b test-rebase origin/main
git cherry-pick --allow-empty <hash>
# Empty result = already in main; conflict or clean apply = not in main
```

**3. Commit message search**
```bash
git log --oneline --all --grep='<first 40 chars>' --format='%H|%s'
```
Catches rebased commits that `git cherry` misses.

### Beware of misleading diffs

**Do NOT use `git diff origin/main..branch --stat` to decide if a fix is needed.** For stale branches based on old main, the diff shows everything that changed in main since the branch was created — not the branch's fix. A single-file fix branch can show 100+ files changed and 30k+ lines of diff. Use `git merge-base --is-ancestor` or `git cherry` instead.

## Recovery

### Option A: Push directly and create PR (single-fix branches)

```bash
git push origin <branch>
gh pr create --base main --head <branch> \
  --title "fix: <commit message>" \
  --body "Recovered from local-only worktree branch."
```

### Option B: Cherry-pick onto fresh main (multi-fix branches on stale base)

```bash
git checkout -b consolidate/<name> origin/main
git rev-list --reverse origin/main..<source-branch> | while read h; do
  git cherry-pick --allow-empty "$h" || true
done
git push origin consolidate/<name>
gh pr create --base main --head consolidate/<name> \
  --title "fix: <summary>"
```

### Option C: Direct file patch (for branches with massive conflicts)

When the stale base produces unresolvable conflicts, apply just the fix file's diff:

```bash
git diff origin/main..origin/<branch> -- <target-file> > /tmp/fix.patch
git checkout -b fix-<name> origin/main
git apply /tmp/fix.patch
git add -A && git commit -m "fix: <description>"
git push origin fix-<name>
gh pr create --base main --head fix-<name> --title "fix: <description>"
```

## Mass Cleanup

### Close all stale PRs at once

```bash
gh pr list --state open --json number --jq '.[].number' | while read pr; do
  gh pr close $pr --comment "Already in main — consolidated via other PRs"
done
```

### Cancel running Actions on closed PRs

```bash
gh run list --limit 100 --json databaseId,status,headBranch \
  --jq '.[] | select(.status != "completed") | .databaseId' \
  | while read id; do gh run cancel $id; done
```

### Delete duplicate local branches

```bash
git branch -D <branch-name>
```

## Pitfalls

- **`git diff --stat` is misleading for stale branches.** Shows "everything that changed in main since the branch was created," not the branch's fix. Use `git merge-base --is-ancestor` or `git cherry` instead.
- **Squash-merged branches are NOT ancestors of main.** After a squash merge, the branch's commits are not in main's DAG. `git merge-base --is-ancestor` returns false. Use `git cherry` or commit message search instead.
- **Force-pushing a rebased branch removes old commits.** Always create a new branch for the recovered fix; don't force-push over the old one.
- **`gh run cancel` only cancels in-progress runs.** Completed runs are already done. Cancel the ones still running after closing PRs to avoid wasted CI minutes.
- **`import time` missing in no_agent scripts.** If a cron script uses `int(time.time())` without `import time`, it crashes silently with no output. The cron job shows `last_status: ok` despite never running. Always add `import time`.
- **SQLite `strftime` vs Python `int(time.time())` cutoff.** The `%s` format specifier must be escaped as `%%s` in Python strings. Prefer computing the cutoff in Python and passing it as a SQL parameter: `cutoff = int(time.time()) - 86400; cursor.execute(..., (cutoff,))`.