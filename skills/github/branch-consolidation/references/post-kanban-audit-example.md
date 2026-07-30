# Post-Kanban Audit Example: PR #566

Real session from 2026-07-26. The kanban board showed all cards `done`, but the open PR #566 was missing 5 fixes.

## Discovery

The user asked "did we consolidate all the work into PR?" — this was the trigger for the audit.

### Step 1: Inventory done cards

```sql
-- Query the kanban board for recent done coder cards
SELECT id, status, assignee, substr(title,1,80) as title
FROM tasks WHERE status NOT IN ('archived', 'cancelled')
ORDER BY status, assignee;
```

Result: 325 done tasks (148 coder, 134 code-reviewer, 42 orchestrator, 1 personal-assistant). No blocked/running/todo/ready — all caught up.

### Step 2: Find PR and check what's on it

```bash
gh pr list --state open --json number,title,headRefName,state
```

PR #566 was open on `fix/uat-dogfood-consolidated-20260725` titled "Consolidated UAT fixes: GH-556, GH-560, GH-564".

### Step 3: Cross-reference kanban fixes vs PR branch

```bash
# Search for each issue number in the PR branch's commit log
git log --oneline fix/uat-dogfood-consolidated-20260725 --all --grep="562" --max-count=10
# → Empty: GH-562 not on the PR branch

git log --oneline fix/uat-dogfood-consolidated-20260725 --all --grep="558\|559\|561\|562\|563" | sort -u
# → Showed GH-558, GH-559, GH-561, GH-563 commits on a different branch (consolidate/uat-fixes-20260725)
#   but NOT on the PR branch
```

### Step 4: Discover the branch topology

```bash
# List all fix branches
git branch --list 'fix/gh-*' 'agent/GH-*' | sort
# → fix/gh-561-add-department-dialog
# → fix/gh-562-google-fonts-csp
# → fix/gh-563-step7-progress-milestones
# → agent/GH-558-use-markdown-fix

# Check what's on each fix branch
git log --oneline fix/gh-562-google-fonts-csp --max-count=3
# → a726f0b fix(gh-562): self-host Inter + Lexend fonts to bypass staging CSP block
# → (parent: 859ab84 — the PR branch tip, meaning this branch was created FROM the PR branch)

# Check the consolidation branch
git log --oneline consolidate/uat-fixes-20260725 --max-count=20
# → fa8c574 fix: make impact_analysis_id nullable...
# → 382792a Merge branch 'agent/GH-558-use-markdown-fix' into consolidate/uat-fixes-20260725
# → fe00031 fix: swap DOMPurify and marked.parse order in useMarkdown.js (GH-558)
# → 838ea6f feat: add externalUrl field to video data... (GH-559)
# → 29ca24b fix: Step 3 Add Department button... (GH-561)
# → 188b7d5 fix: orchestrator - compute milestone total... (GH-563)
# → (GH-562 NOT present — it was the only one missing from consolidation too)

# Assess topology
git merge-base consolidate/uat-fixes-20260725 fix/uat-dogfood-consolidated-20260725
# → bcb0380 (the GH-554 fix)
git log --oneline consolidate/uat-fixes-20260725 ^fix/uat-dogfood-consolidated-20260725 | wc -l
# → 71 commits on consolidation not on PR
git log --oneline fix/uat-dogfood-consolidated-20260725 ^consolidate/uat-fixes-20260725 | wc -l
# → 3 commits on PR not on consolidation (GH-564, GH-560, GH-565)
```

### Step 5: Merge

```bash
# Check we're on the PR branch
git branch --show-current
# → fix/uat-dogfood-consolidated-20260725 ✓

# Stash dirty package-lock.json
git stash push -m "dirty package-lock before merge"

# Merge consolidation branch first (brings in GH-558, GH-559, GH-561, GH-563 + 67 other fixes)
git merge consolidate/uat-fixes-20260725 --no-edit
# → Merge made by the 'ort' strategy (19 files changed)

# Merge GH-562 separately (wasn't in the consolidation branch)
git merge fix/gh-562-google-fonts-csp --no-edit
# → Merge made by the 'ort' strategy (4 files: fonts + CSS)
```

### Step 6: Push and update PR

```bash
git push origin fix/uat-dogfood-consolidated-20260725

# Try gh pr edit — fails with GraphQL deprecation warning
gh pr edit 566 \
  --title "Consolidated UAT fixes: GH-554, GH-556, GH-558, GH-559, GH-560, GH-561, GH-562, GH-563, GH-564, GH-565"
# → exit code 1: "GraphQL: Projects (classic) is being deprecated..."

# Fall back to REST API
curl -s -X PATCH \
  -H "Authorization: token $(gh auth token)" \
  -H "Accept: application/vnd.github.v3+json" \
  https://api.github.com/repos/my-org/MyProject/pulls/566 \
  -d '{"title":"Consolidated UAT fixes: GH-554, GH-556, GH-558, GH-559, GH-560, GH-561, GH-562, GH-563, GH-564, GH-565","body":"## Scope\n\nConsolidated UAT/dogfood fixes across multiple issues..."}' | jq '.title, .state, .html_url'
# → "Consolidated UAT fixes: GH-554, GH-556, GH-558, GH-559, GH-560, GH-561, GH-562, GH-563, GH-564, GH-565"
# → "open"
# → "https://github.com/my-org/MyProject/pull/566"
```

### Step 7: Verify

```bash
gh pr view 566 --json number,title,state,headRefName,baseRefName
# → title updated correctly, 78 commits ahead of main
```

## Key Takeaways

1. **Kanban done ≠ PR covered.** Don't assume all completed cards made it into the consolidation PR — the PR was created before the last batch of fixes completed.
2. **Three-way topology:** There was a local consolidation branch (71 commits), a PR branch (3 unique commits), and individual fix branches (GH-562 on its own). The fix branches were worktrees FROM the PR branch, not from the consolidation branch.
3. **Stash before merge:** `package-lock.json` was dirty from npm operations. Must stash before `git merge` or the merge aborts.
4. **`gh pr edit` GraphQL fallback:** The REST API is the reliable path when `gh pr edit` hits the Projects classic deprecation warning.