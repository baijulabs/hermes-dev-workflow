# PR Merge Conflict Auto-Resolution Recipe

Automated workflow pattern for detecting and programmatically resolving merge conflicts on existing open pull requests without risking worktree branch collisions or duplicate PR creation.

## 1. Detection & Deduplication

A pull request has merge conflicts when queried via the GitHub CLI:
* `mergeable` status is `"CONFLICTING"`
* `mergeStateStatus` status is `"DIRTY"`

```bash
# Query open PRs with conflict indicators
gh pr list --state open --json number,title,headRefName,mergeable,mergeStateStatus
```

### Deduplication Rule
Before creating a resolution task, query the Kanban SQLite database for active cards targeting the same branch or title keywords:
```sql
SELECT id FROM tasks 
WHERE status NOT IN ('done', 'cancelled', 'archived') 
  AND (branch_name = '<branch>' OR title LIKE '%Resolve merge conflicts in <branch>%');
```
If an active card exists, **exit silently** (resolution is already in flight).

---

## 2. Card Creation (Pattern 5b / Collision Avoidance)

To completely prevent Pattern 5b (fatal git worktree branch collisions where `git worktree add` fails because a branch is already checked out on disk by an old or un-pruned worktree), **do NOT specify a branch name during card creation**:

* **Incorrect:** `hermes kanban create ... --branch <pr-branch>` (causes fatal collision if old worktree exists on disk)
* **Correct:** Omit `--branch` entirely. The Kanban dispatcher will auto-derive a unique worktree branch name (e.g. `wt/t_<task-id>`) from main, ensuring zero collision risk.

---

## 3. Coder Card Body & Instructions Template

```markdown
## Goal
Resolve Git merge conflicts in pull request branch `<pr-branch>` by merging the latest `main` and resolving conflict markers.

## Instructions
1. Fetch the original PR branch from origin: 
   `git fetch origin <pr-branch>`
2. Merge the PR branch into your current isolated worktree branch: 
   `git merge origin/<pr-branch>`
3. Fetch and merge the latest main: 
   `git fetch origin main && git merge origin/main`
4. If/when merge conflicts occur, read the conflicting files, locate the conflict markers (`<<<<<<<`, `=======`, `>>>>>>>`), and resolve them. Keep both sides' changes where appropriate or choose the correct logic.
5. Run the test suite locally to verify the resolution: 
   `./run-tests.sh`
6. Once tests pass, push your resolved branch directly back to the original PR branch on origin:
   `git push origin HEAD:<pr-branch>` (This seamlessly updates the PR on GitHub!)
7. Mark the task complete.

BASE BRANCH: <pr-branch>
CRITICAL: Before writing code, run `git branch --show-current` and verify you are on a worktree branch derived from the base branch above. You must NOT be on main or master. If you are, block the task immediately.
```

---

## 4. Key Pitfalls & Safeguards

* **Do not use the `[GH-N]` prefix in titles.** If the PR number is `#552`, titling a card `[GH-552]` will cause the automated issue-sync script (`hermes_github_sync.sh`) to run `gh issue close 552` once the card completes, which silently closes the open pull request! Use `[PRFIX-<timestamp>]` or `[DF-<timestamp>]` instead.
* **Always pair with a reviewer.** Ensure every coder resolution card has a paired `code-reviewer` card created with `parents=[<coder_task_id>]` to maintain strict quality gates.
