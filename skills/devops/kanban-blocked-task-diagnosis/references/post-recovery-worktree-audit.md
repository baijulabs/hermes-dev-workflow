# Post-Recovery Worktree Branch Audit

After DB corruption recovery and task unblocking, the dispatcher may resume dispatching and completing tasks — but **worktree branches that had unique commits before the corruption are still sitting on disk without PRs**. The orchestrator must audit all worktree branches to find completed work that needs consolidation.

## When to Run This

Run this after any DB corruption recovery that involved unblocking tasks. The corruption may have masked completed work — workers completed their code but the PR was never created because the orchestrator never processed the completed tasks.

## Audit Procedure

### 1. List all worktree branches

```bash
git branch | grep "wt/t_"
```

### 2. Find branches with unique commits not in main

```bash
for b in $(git branch | grep "wt/t_"); do
  behind=$(git rev-list --count "origin/main..$b" 2>/dev/null)
  if [ "$behind" -gt 0 ]; then
    echo "=== $b ($behind commits ahead of main) ==="
    git log origin/main..$b --oneline
    echo
  fi
done
```

### 3. Check what files were changed

```bash
git diff origin/main...<branch> --stat
```

### 4. Cross-reference with kanban task status

```bash
sqlite3 ~/.hermes/kanban/boards/my-project-dev/kanban.db \
  "SELECT id, status, assignee, substr(title,1,60) FROM tasks WHERE id LIKE '%<task-id>%' ORDER BY created_at;"
```

The task ID is the branch name after `wt/t_` — e.g., `wt/t_2b8b86bb` → task `t_2b8b86bb`.

### 5. Categorize each branch

| Category | Action |
|---|---|
| **Unique commits, all tasks done** | Consolidate into a feature branch and open a PR |
| **Unique commits, but dependent tasks incomplete** | Block until dependencies resolve, then PR |
| **No unique commits (empty)** | Stale — created by kanban system but no work was committed. Safe to delete |
| **Already merged via PR** | Can be deleted (the squash merge absorbed the changes) |

### 6. Consolidate completed work into a feature branch

```bash
git checkout main
git pull origin main
git checkout -b feature/<epic-slug>
git cherry-pick <commit-hash-1> <commit-hash-2>
# Resolve any conflicts
git push origin feature/<epic-slug>
```

### 7. Open a PR

```bash
gh pr create \
  --base main \
  --head feature/<epic-slug> \
  --title "<descriptive title>" \
  --body "<summary of changes, references to GH issues>"
```

## Checking if a merge was squash-merged

When a worktree branch was merged via GitHub PR squash, the original commit hash is NOT in main. To check:

```bash
# Check if the worktree branch's unique commit content exists in main
# by comparing the diff
git diff origin/main...<branch> --stat | wc -l
# If > 0, there are changes not in main
```

Alternatively, search for the PR that merged the branch:

```bash
gh pr list --state merged --head <branch> --json number,title
```