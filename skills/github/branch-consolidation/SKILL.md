---
name: branch-consolidation
description: "Merge 10+ branches into one PR with conflict strategy."
version: 1.2.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [GitHub, PR-Consolidation, Git, Merge-Strategy, Batch-Operations]
    related_skills: [github-pr-workflow, kanban-pr-watch-cron]
---

# Branch Consolidation Workflow

Consolidate 5-60+ unmerged branches (UAT fixes, hotfixes, parallel agent work) into a **single PR**. This covers the manual ad-hoc case — not the automated kanban worktree cron pattern (see `kanban-pr-watch-cron` for that).

## When to Use

- A UAT/dogfood cycle produced 10-60 fix branches, many of which are duplicate iterations
- Parallel agent work generated multiple fix branches on different merge bases
- The user wants **one PR**, not N individual PRs
- Branches like `fix/df-*`, `fix/prfix-*`, `agent/GH-*`, `cherrypick/*`, `consolidate/*`

Do NOT use for:
- A single atomic branch (use `github-pr-workflow` directly)
- Automated kanban worktree consolidation (use `kanban-pr-watch-cron`)
- Feature branches that should stay as individual PRs

## Step 0 — Scope First: Open GitHub Issues are the Source of Truth

The user's definition of "the fixes that need consolidating" is **open GitHub issues**, not "every branch with unmerged commits." There may be 60+ unmerged branches, but most are stale duplicates.

Before cataloging branches, run:

```bash
gh issue list --state open --json number,title,labels --jq \
  '.[] | select(.labels | map(.name) | contains(["dogfood"])) | [.number, .title] | @tsv'
```

This is the user's authoritative list. Cross-reference each open issue against existing branches — not the other way around.

**Pitfall — implementing fixes yourself.** If you start searching for files, reading code, or writing patches, you're doing the wrong job. Stop. The user's explicit correction is: "The fixes are already done on the branches." Search for the existing fix branch first. If no branch exists for an open issue, create a kanban fix card — don't write the code directly.

**Pitfall — scope creep costs user patience.** The user may need to correct you multiple times before you land on the right scope. You will be tempted to merge "everything that isn't merged yet." The user's actual definition is narrower. Three corrections from one real session:

1. "I only need the UAT fixes" — exclude agent branches (`agent/GH-*`), feature branches (`feature/*`), and existing consolidate branches (`consolidate/*`). Cherrypick branches (`cherrypick/*`) should be used as a MERGE BASE but not listed as a separate fix.
2. "The ones from the GH issues currently open" — the source of truth is the open issue list, not "every branch with unmerged commits." Run `gh issue list --state open --json number,title,labels` first.
3. "The fixes are already done on the branches" — search for branches that reference the issue number before writing code. If a branch exists, merge it. If not, create a kanban card.

The pattern: each correction narrows from "everything" → "open issues" → "existing fix branches." Start at step 2 (open issues) and skip straight to searching for existing branches. Do NOT start at step 1 (all branches) and let the user correct you down.

**Pitfall — cross-reference open issues against existing branches before implementing.** After you identify open issues, check whether a branch already exists for each one. Search commit messages across all branches:

```bash
for branch in $(git branch --list 'agent/*' 'fix/*' 'wt/t_*' | sed 's/^..//'); do
  msg=$(git log --oneline -1 $branch 2>/dev/null | grep -iE "556|560|561")  # replace with actual issue numbers
  if [ -n "$msg" ]; then
    echo "=== $branch ==="
    git log --oneline main..$branch 2>/dev/null | grep -iE "556|560|561"
  fi
done
```

If a fix branch exists, merge it. If not, create a kanban card — do not write the code directly.

**Pitfall — assuming all open issues have branches.** If an open issue has no matching branch (no commit with its number, no branch with its keyword), create a new kanban fix card for it. Do NOT implement the fix directly — the kanban system handles implementation and review automatically.

Scan for every branch with commits not yet on `main`:

```bash
for b in $(git branch --list 'fix/*' --list 'agent/*' --list 'cherrypick/*' --list 'consolidate/*' --list 'feature/*' | sed 's/^..//' | sort -u); do
  count=$(git rev-list --count main..$b 2>/dev/null);
  if [ "$count" -gt 0 ] 2>/dev/null; then
    echo "$b ($count commits)";
    git log --oneline -1 main..$b;
  fi
done
```

**Pitfall — branches on different merge bases:** Most fix branches were created from an older `main`. They will conflict when merged. Plan for conflict resolution (Step 5).

## Step 2 — Categorize & Deduplicate

Group branches by topic reading their tip commit messages. Common categories from UAT cycles:

| Category | Typical Branches | Files Affected |
|----------|-----------------|-----------------|
| Vue hoisting / npm | `fix/df-*vue*`, `fix/df-*hoisting*`, `fix/prfix-*npm*` | `package.json`, `package-lock.json`, `Dockerfile`, `scripts/check-deps.sh` |
| DDL / DB migrations | `fix/df-*ddl*`, `fix/df-*label*` | `backend/database/migrations/` |
| Backend API routes | `fix/df-*save-values*`, `fix/df-*simulation*`, `fix/df-*deviation*` | `backend/api/routers/private_routes.py`, `backend/tests/` |
| Test fixes | `fix/df-*test*`, `fix/df-*lint*`, `cherrypick/*` | `backend/tests/`, `frontend/tests/` |
| Config / CI | `fix/df-*ruff*`, `fix/df-*docker*`, `fix/vpc-*` | `.github/workflows/`, `ruff.toml`, infrastructure |
| Agent GH branches | `agent/GH-*` | Varies per issue |

**Dedup rule:** Within each category, only include the **latest iteration** of each fix. Branches like `fix/df-1784774204-save-values`, `fix/df-1784774204-save-values-v2`, `fix/df-1784774204-save-values-v2-ddl`, and `fix/df-1784774204-save-values-v2-docker-fix` are all iterations of the same fix. Pick the most comprehensive one (usually the one with the highest version number).

Check for pre-existing consolidation work — `consolidate/*` and `cherrypick/*` branches often already contain combined versions:

```bash
for b in consolidate/* cherrypick/*; do
  echo "$b: $(git rev-list --count main..$b) commits"
done
```

If one of these exists with a large commit count, use it as your merge **base branch** in Step 4.

## Step 3 — Decide: One PR or Thematic Split?

Before creating the branch, decide whether to consolidate everything into **one PR** or split into **multiple thematic PRs**.

### When to split

| Theme | Example | Branch name |
|---|---|---|
| CI/DevOps | `deploy.yml`, action version bumps, CI config | `consolidate/devops-ci` |
| App code | Frontend Vue/JS, backend Python, locale files, tests | `consolidate/app-fixes` |
| Infrastructure | Terraform, Dockerfiles, Cloud Run | `consolidate/infra` |
| Docs | README, CHANGELOG, `docs/` | `consolidate/docs` |

### Benefits of splitting

- **Independent review timelines.** A DevOps PR can merge as soon as CI passes, without waiting for app code review.
- **Smaller diffs.** Each PR is focused on one concern — faster review, less error-prone.
- **Safer rollbacks.** An app revert doesn't undo the DevOps improvements.
- **Clearer git history.** Each branch log shows only one concern.

### When NOT to split

- Changes are tightly coupled (e.g. backend API + frontend + CI workflow for the same feature)
- Fewer than 3-4 meaningful branches
- User explicitly asked for a single PR

### Splitting technique

After categorizing branches by theme:

```bash
# DevOps PR
git checkout -b consolidate/devops-ci origin/main
git cherry-pick <ci-related-commits>

# App PR
git checkout -b consolidate/app-fixes origin/main
git cherry-pick <app-related-commits>
```

**Pitfall — cherry-picking shared commits.** If a commit touches both CI and app files (e.g. `deploy.yml` + `Chat.vue`), split it: `git checkout <sha> -- .github/workflows/` for the DevOps branch, then the remainder for the app branch.

**Pitfall — commits already in main.** Before cherry-picking, verify the commit's content isn't already on `main` via equivalent changes (different hash, same content). Run `git diff origin/main..<sha> --stat | wc -l` — if 0, skip it.

**Pitfall — blurred boundaries.** A commit that modifies `frontend/package.json` (app dep) and `.github/workflows/deploy.yml` (CI) is a mixed commit. Don't cherry-pick it into both branches — that creates divergent histories. Instead, take the relevant file changes only:

```bash
# On devops branch
git checkout <sha> -- .github/workflows/
git commit -m "ci: <message>"

# On app branch
git checkout <sha> -- frontend/package.json
git commit -m "fix: <message>"
```

## Step 4 — Create the Consolidated Branch

```bash
git checkout main && git pull origin main
git checkout -b consolidate/$(date +%Y%m%d)-fixes
```

## Step 5 — Ensure Freshness: Rebase on Latest Main FIRST

**CRITICAL — do not skip.** The consolidation branch must be current with `main` BEFORE merging any fix branches. Merging a stale consolidation branch clobbers newer `main` changes via silent three-way merge. This is the #1 cause of post-consolidation CI failures (ghost 404s, reverted catch-all routes, wrong React versions, missing i18n keys).

```bash
# 1. Fetch latest main
git fetch origin main

# 2. Check staleness
BEHIND=$(git rev-list --count HEAD..origin/main)
echo "Consolidation branch is $BEHIND commits behind origin/main"

# 3. If > 0, rebase or merge from main FIRST
if [ "$BEHIND" -gt 0 ]; then
  echo "Rebasing onto latest main to prevent clobbering newer changes..."
  git rebase origin/main
  # If conflicts, resolve them keeping BOTH sides' changes (don't discard main's newer code)
fi

# 4. CRITICAL: verify no fixes were lost
# Check for known regression points:
git grep "app.get.*\*" frontend/server.js  # Should show '/{*path}' not '/*'
grep "react" package.json | grep "18.3.1"   # Should find react pin
```

**After rebasing, re-verify all fix branches still have unique commits** (Step 2 may need to be re-run after a rebase).

## Step 6 — Merge Base Branch (Comprehensive First)

Merge the most comprehensive branch first (e.g., a `cherrypick/*` branch with 26+ commits):

```bash
git merge -X theirs <base-branch> --no-edit
```

**`-X theirs` strategy:** Auto-resolve all conflicts in favor of the fix branch. This is appropriate ONLY when the consolidation branch has been freshly rebased on main (Step 4). Without the rebase, `-X theirs` silently overwrites newer main code.
- The fix branch changes are known correct (they've passed tests)
- Main's conflicting changes are unrelated infrastructure
- You will verify the final diff in Step 7

**Pitfall — stale base conflicts:** The base branch may be 20+ commits behind main. Without `-X theirs`, you'll get conflicts on almost every file. The `-X theirs` flag accepts the fix branch's version for conflicted hunks, while non-conflicted files merge normally.

## Step 7 — Merge Remaining Unique Branches

For each unique fix branch not already covered by the base, merge one at a time:

```bash
count=$(git rev-list --count HEAD..$branch 2>/dev/null)
if [ "$count" -gt 0 ]; then
  git merge -X theirs $branch --no-edit
fi
```

**Never use octopus merge** (passing multiple branch args to `git merge`). It fails with `fatal: merge program failed` when there are content conflicts. Always merge **one branch per invocation**.

**Handle merge failures gracefully:**

```bash
git merge --abort
# Try cherry-picking individual commits instead
git cherry-pick -X theirs <commit-sha>
# If cherry-pick also fails, resolve manually:
# 1. Read the conflicting files
# 2. Remove conflict markers, keep both sides' changes
# 3. git add <file> && git cherry-pick --continue
```

## Step 7 — Delegate Bulk Merging to a Subagent

When there are 15+ remaining branches, the sequential merge loop will take too many conversation turns. Delegate to a background subagent:

```python
delegate_task(
    goal="Merge all remaining fix branches into the consolidated branch <name>",
    context="""Branch list (one per line):
<branch-1>
<branch-2>
...
Process for each: 
1. `git merge -X theirs <branch> --no-edit`
2. If that fails: `git merge --abort`, then cherry-pick individual commits
3. After all merges: verify with `git log --oneline -5` and `git diff --stat main..HEAD`

The branch already exists at consolidate/uat-fixes-<date> based on origin/main."""
)
```

The subagent will work methodically through merges while the main conversation stays responsive.

## Step 8 — Verify the Consolidated Diff

After all merges complete, verify the diff is correct:

```bash
# Summary excluding massive lockfiles
git diff --stat main..HEAD ':!package-lock.json' | tail -10

# Check specific areas
git diff main..HEAD -- backend/api/routers/       # Route changes
git diff main..HEAD -- backend/tests/              # Test changes
git diff main..HEAD -- package.json                # Dependency changes

# Verify a specific fix survived
git diff main..HEAD -- path/to/file | grep "expected_change"
```

**Pitfall — fix clobber:** When a full-file-rewrite branch is merged AFTER a targeted-fix branch for the same file, the rewrite silently overwrites the targeted fix — no conflict markers, no error. The fix commit's hash exists in the branch history but the code change doesn't survive in the working tree. Mitigation: merge rewrite-heavy branches FIRST, then targeted-fix branches on top.

## Step 9 — Run Pre-Merge Regression Check

After all merges complete, run the automated regression check before pushing:

```bash
./scripts/pre-merge-check.sh
```

This checks the 4 most common consolidation regression points: route decorators, Express catch-all syntax, React version pin, and i18n key consistency. Any failure must be fixed before creating the PR. See `merge-consolidation-recovery` skill for the full diagnostic order.

## Step 10 — Update from Main (if consolidation took a while)

```bash
git fetch origin main
git rebase origin/main
# Resolve any rebase conflicts
```

## Step 11 — Auto-Detect Bump Level & Version Bump

Analyse all commits since `main` to determine the appropriate semver bump level based on conventional commit prefixes:

```bash
# Detect bump level from commit messages since main
BUMP_LEVEL="patch"  # default — safe floor
if git log --oneline main..HEAD --format="%s" | grep -qE "^(feat|feature)(\(.+\))?!?:"; then
    # Check for BREAKING CHANGE in any commit body
    if git log --oneline main..HEAD --format="%b" | grep -qi "BREAKING CHANGE"; then
        BUMP_LEVEL="major"
    else
        BUMP_LEVEL="minor"
    fi
fi
# If only fix/chore/docs/refactor commits, stays at patch (default)
echo "🔍 Detected bump level: $BUMP_LEVEL"

# Dry-run first to preview
./scripts/sync-version.sh --bump "$BUMP_LEVEL" --dry-run

# Apply the bump
./scripts/sync-version.sh --bump "$BUMP_LEVEL"

# Commit the version bump as its own commit
git add backend/pyproject.toml frontend/package.json package.json
git commit -m "chore: bump version to $(grep '^version' backend/pyproject.toml | head -1 | sed 's/version = \"\(.*\)\"/\1/')"
```

**Pitfall — no conventional commits found.** If all commit messages are plain English without prefixes (legacy branches), the default `patch` bump is safe. For a more accurate bump, rewrite commit messages during cherry-pick with `git cherry-pick -n <sha> && git commit --amend -m "fix: <message>"` before this step.

**Pitfall — the version must be bumped BEFORE pushing.** The push and PR creation follow in Step 12. If you bump after pushing, the version commit won't be in the PR.

## Step 12 — Push and Open PR

```bash
git push -u origin HEAD
```

Get a summary of all included fix types and the new version:

```bash
FIX_SUMMARY=$(git log --oneline main..HEAD | grep -E "^(fix|chore|docs|feat)" | sort -t: -k1,1 -k2,2 | sed 's/^/* /')
NEW_VERSION=$(grep '^version' backend/pyproject.toml | head -1 | sed 's/version = "\(.*\)"/\1/')
```

Then create the PR:

```bash
gh pr create \
  --title "fix: consolidate $(date +%Y%m%d) fixes — v$NEW_VERSION" \
  --body "## Summary
Consolidates fixes from <source> on <date>.

### Version
**$NEW_VERSION** — $(echo "$FIX_SUMMARY" | head -1 | grep -oE '^(patch|minor|major)') bump

### Fixes included
$FIX_SUMMARY

### Testing
- [ ] CI passes" \
  --label "fix"
```

### Step 12 — Update PR After Push

If you push additional commits to the PR branch (e.g., merging more fix branches after the PR was created), `gh pr edit` may fail with a GraphQL deprecation warning:

```
GraphQL: Projects (classic) is being deprecated in favor of the new Projects experience
```

This exits with code 1 and does NOT apply your changes despite being a non-blocking warning. **Fall back to the REST API:**

```bash
curl -s -X PATCH \
  -H "Authorization: token $(gh auth token)" \
  -H "Accept: application/vnd.github.v3+json" \
  https://api.github.com/repos/<org>/<repo>/pulls/<N> \
  -d '{"title":"Updated title","body":"Updated body"}'
```

This bypasses the GraphQL layer entirely and will succeed when `gh pr edit` does not. Verify with `gh pr view <N> --json title,state --jq '{title, state}'`.

**Pitfall — don't waste time on `gh pr edit` when it fails with this specific error.** The GraphQL deprecation warning is treated as an error by `gh`, but it is not an actual API failure. Switching to the REST `PATCH /pulls/N` endpoint fixes it immediately.

## Pitfalls

- **package-lock.json dominates diff stat.** Always use `':!package-lock.json'` when checking diff size. The lockfile changes are legitimate but drown out meaningful code changes in the PR body.
- **Sequential merge order matters.** Merge the most comprehensive/reliable branch FIRST, then layer smaller fix branches on top. This minimizes conflict resolution.
- **Dead-end branches.** v1/v2 experiment branches exist alongside their v3+ successors. Only merge the final version. Check with `git log --oneline main..$branch | grep -c "fix:"` to quickly assess if a branch has substantive work.
- **Feature branches need user direction.** Ask whether to include `feature/*` branches in a "fixes" consolidation PR.
- **`-X theirs` can silently discard main infra changes.** Always verify the final diff against what you expect.
- **Subagent summaries are self-reports.** After the subagent finishes, verify the consolidated branch exists and has the expected commits. Don't trust "Merge successful" without `git log` confirmation.
- **Some branches are already on main despite `git rev-list --count` showing >0.** This happens when a branch's merge base is not `main`. Check: `git merge-base main $branch` may be behind main.

## Related Skills

- `merge-consolidation-recovery` — diagnostic order for CI failures after consolidation; includes `scripts/pre-merge-check.sh`
- `github-pr-workflow` — single-branch PR lifecycle (creation, CI, merge)
- `kanban-pr-watch-cron` — automated cron-based PR consolidation from kanban worktrees
- `deploy-failure-automation` — CI failure → automated kanban fix → PR pipeline
- `subagent-driven-development` — using delegate_task for bulk parallel work

## Post-Kanban Audit: Adding Missed Cards to an Existing Open PR

When the kanban board shows all cards `done` but an existing open PR doesn't include them, the orchestrator needs to audit, merge, and update rather than starting a new consolidation from scratch.

### When to Use

- The kanban board has completed cards (coder+reviewer pairs, all `done`)
- An open PR already exists covering an earlier batch of fixes (e.g., `fix/uat-dogfood-consolidated-20260725`)
- The user asks "did we consolidate everything into the PR?"
- Some fix branches are on the board as `done` but their commits are not on the PR branch

Do NOT use for: first-time consolidation (use the ad-hoc workflow above), or automated cron-based detection (use `kanban-pr-watch-cron`).

### Step 1 — Inventory: Done Cards vs PR Branch Commits

Query the kanban board for recent done coder cards and cross-reference against what's already on the PR branch:

```bash
# 1. Get recent done coder cards (issue numbers from titles)
sqlite3 ~/.hermes/kanban/boards/<board-slug>/kanban.db \
  "SELECT id, substr(title,1,80) FROM tasks WHERE status='done' AND assignee='coder' ORDER BY created_at DESC LIMIT 20;"

# 2. Check what's already on the PR branch vs main
cd /path/to/repo
git log --oneline fix/uat-dogfood-consolidated-<date> --not main | head -20

# 3. Search for each issue number in the PR branch's log
git log --oneline fix/uat-dogfood-consolidated-<date> --all --grep="558\|559\|561\|562\|563" | sort -u
```

If a git log grep comes back empty for an issue number, that fix is not yet on the PR branch.

### Step 2 — Find the Fix Branches

Individual fix branches (from worktrees) are the source of the missing commits:

```bash
git branch --list 'fix/gh-*' 'agent/GH-*' 'fix/df-*' | sort
```

For each missing issue, check:
```bash
git log --oneline fix/gh-<issue>-<description> --max-count=3
```

Note whether each branch was created from `main` or from the PR branch (check by looking at `git merge-base` with both).

### Step 3 — Assess the Branch Topology

Understand how the branches relate before merging:

```bash
# Merge base between consolidation branch and PR branch
git merge-base consolidate/uat-fixes-<date> fix/uat-dogfood-consolidated-<date>

# Count commits on each side of the divergence
git log --oneline consolidate/uat-fixes-<date> ^fix/uat-dogfood-consolidated-<date> | wc -l
git log --oneline fix/uat-dogfood-consolidated-<date> ^consolidate/uat-fixes-<date> | wc -l

# View the PR-only commits
git log --oneline fix/uat-dogfood-consolidated-<date> ^consolidate/uat-fixes-<date>
```

The consolidation branch typically has MORE commits (because it merged all fix branches). The PR branch may have 1-3 unique commits (the PR's original scope). Plan to merge the consolidation branch INTO the PR branch so the PR retains its history.

### Step 4 — Merge into the Existing PR Branch

```bash
# Check you're on the PR branch (the one with the open PR)
git branch --show-current

# Check for dirty working tree (common: package-lock.json modified by npm)
git status --short

# Stash dirty state if needed
git stash push -m "dirty state before merge"

# Merge the comprehensive consolidation branch first
git merge consolidate/uat-fixes-<date> --no-edit

# Then merge any remaining individual branches not covered
git merge fix/gh-<issue>-<description> --no-edit
```

**Pitfall — package-lock.json dirty.** npm operations leave `package-lock.json` modified. Stash before merging or the merge is aborted. After merge, resolve the package-lock conflict with `git checkout --theirs package-lock.json && git add package-lock.json && git stash drop`.

### Step 5 — Update PR Title, Body, and Push

```bash
# Push the updated branch
git push origin fix/uat-dogfood-consolidated-<date>

# Update PR metadata
gh pr edit <N> \
  --title "Consolidated fixes: GH-<A>, GH-<B>, GH-<C>, ..." \
  --body "## Scope\n\nUpdated scope covering all consolidated fixes..."
```

If `gh pr edit` fails with `GraphQL: Projects (classic) is being deprecated...`, fall back to the REST API (see Step 10 in the main workflow above):

```bash
curl -s -X PATCH \
  -H "Authorization: token $(gh auth token)" \
  -H "Accept: application/vnd.github.v3+json" \
  https://api.github.com/repos/<org>/<repo>/pulls/<N> \
  -d '{"title":"...","body":"..."}' | jq '.title, .state, .html_url'
```

### Pitfalls

- **Individual fix branches may be worktrees from the PR branch, not from main.** These branches parent to the PR branch tip, not to main. They CAN be merged directly into the PR branch (fast-forward or recursive) because they share the same base — no need to rebase.
- **The PR title may not update even when `gh pr edit` reports success** due to the GraphQL deprecation warning that exits with code 1 but doesn't actually fail. Always verify with `gh pr view <N> --json title,state`. If it didn't update, use the REST API fallback.
- **Dirty `package-lock.json` from prior npm operations** causes merge abort. Stash it first, then use `--theirs` resolution after merge.
- **Don't create a new PR when an existing one covers the same work.** Merging new commits into the existing PR branch preserves the review thread, CI status, and comments.

### Verification

After push, verify:
```bash
gh pr view <N> --json number,title,state,headRefName,baseRefName
git log --oneline fix/uat-dogfood-consolidated-<date> --not main | wc -l
```

### Post-Merge False-Close Audit

After the consolidation PR merges to main, verify every issue number in the PR scope has a commit on main:
```bash
for issue in 554 556 558 559 560 561 562 563 564 565; do
  echo "GH-$issue: $(git log --oneline main --grep="GH-$issue" | wc -l) commits on main"
done
```
Any issue with 0 commits on main means the fix was done on a worktree branch that was never consolidated. Reopen the issue and create a fresh kanban card. A coder card marked `done` and a reviewer card approved does not guarantee the fix is on `main` — worktree branches exist outside the main history.

## References

- See `references/uat-consolidation-example.md` for a real session transcript with 60+ branches across 10 fix categories
- See `references/kanban-sqlite-card-creation.md` for creating kanban cards directly via SQLite when the CLI is blocked in delegate/cron contexts
- See `references/post-kanban-audit-example.md` for a real session: detecting GH-558/GH-559/GH-561/GH-562/GH-563 as missing from PR #566, merging consolidation branch + GH-562 branch, updating PR via REST API