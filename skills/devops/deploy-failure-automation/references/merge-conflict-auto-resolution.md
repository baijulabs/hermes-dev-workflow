# Auto-Resolution of PR Merge Conflicts

This reference documents the automated playbook and design for detecting and auto-resolving PR merge conflicts on the repository, implemented in the `pr-check-watch` watchdog.

## Core Problem
When a pull request has merge conflicts with `main`, the CI checks often fail or cannot run, and the PR cannot be merged. In an automated multi-agent workflow, having a human manually resolve these conflicts breaks the autonomous continuous delivery loop.

## The Auto-Resolution Playbook
To resolve merge conflicts without human intervention, we delegate the task to a `coder` worker on the Kanban board using a specialized, collision-free git strategy:

### 1. Collision-Free Card Creation (Pattern 5b Prevention)
When creating the `coder` card on the Kanban board:
* **OMIT the `--branch` parameter** (do not pass `--branch <pr-branch>`). If we pass the original PR's branch name, the dispatcher tries to check out that branch directly in a worktree. If another task's worktree still has that branch checked out on disk, git will fail with a fatal worktree collision error (`fatal: already used by worktree`).
* By omitting `--branch`, the dispatcher auto-derives a guaranteed unique worktree branch name like `wt/t_<task-id>` based on HEAD.
* Specify `BASE BRANCH: <original-pr-branch>` in the card body for Layer 1 branch guardrail defense.

### 2. Isolated Merge & Resolution Workflow
The `coder` worker runs inside its isolated, collision-free branch (`wt/t_<task-id>`) and executes these steps:
1. **Fetch and Merge original PR code:**
   ```bash
   git fetch origin <original-pr-branch>
   git merge origin/<original-pr-branch>
   ```
   This pulls the PR's current state into the coder's isolated branch.
2. **Fetch and Merge latest main:**
   ```bash
   git fetch origin main
   git merge origin/main
   ```
   This merges the latest main into the PR's code, triggering the merge conflicts *inside* the isolated, safe coder branch.
3. **Resolve Conflict Markers:**
   The coder programmatically reads the conflicting files, identifies the standard git conflict markers (`<<<<<<<`, `=======`, `>>>>>>>`), and resolves them by keeping both sides' changes where appropriate or choosing the correct logic.
4. **Verify Resolution:**
   The coder runs the test suite (`./run-tests.sh`) to verify that the conflict resolution compiles and passes all checks.
5. **Direct Push back to Origin:**
   Instead of opening a new PR, the coder pushes the resolved changes directly back to the original PR branch on origin:
   ```bash
   git push origin HEAD:<original-pr-branch>
   ```
   This automatically updates the existing pull request on GitHub, clearing the conflicts and triggering a fresh green CI run!
6. **Hand off & Complete:**
   The coder calls `kanban_complete()`. The paired reviewer card promotes to ready, performs a code review of the resolved diff, and approves.

## SQLite Deduplication Query
To prevent creating duplicate conflict-resolution cards in every poll cycle, the watchdog queries the Kanban board's SQLite database before acting:

```sql
SELECT id FROM tasks 
WHERE status NOT IN ('done', 'cancelled', 'archived') 
  AND (branch_name = '<original-pr-branch>' OR title LIKE '%Resolve merge conflicts in <original-pr-branch>%');
```
If an active card exists, the watchdog exits silently.
