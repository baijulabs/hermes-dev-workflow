# Merged-but-missing: the schema survived, but the import into the routes file was clobbered

## When this bites

You restore a clobbered route (Pattern 9) by surgical-patching the route + DB function into `main`. CI then fails with `ruff F821 Undefined name 'X'` even though the class/schema `X` is defined in `backend/schemas.py` on `main`. The *import* into `private_routes.py` was never restored.

This is a second-order clobber: the stale-base merge that killed the route ALSO killed the `from backend.schemas import (...)` line that references the symbol. The schema file itself survived (standalone addition to `schemas.py`), but the routes-file import did not.

## Diagnosis (don't guess)

```bash
cd /home/user/MyProject
git fetch origin main 2>&1 | tail -1

# 1. Is the schema/class defined on main? (expect >=1)
git show origin/main:backend/schemas.py | grep -c "^class PromoteToSopResponse"

# 2. Is it IMPORTED in the routes file on main? (expect >=1, but often 0)
git show origin/main:backend/api/routers/private_routes.py | grep -c "PromoteToSopResponse"

# 3. Was it imported in the original worktree? (expect >=1)
git show wt/t_c9de841e:backend/api/routers/private_routes.py | grep -n "PromoteToSopResponse"

# 4. ruff F821 confirmation
python3 -m ruff check backend/api/routers/private_routes.py
```

If step 1 is >=1 and step 2 is 0, you have an import-only clobber.

## Fix (one line, no schema re-add)

The schema already exists. You only need to add the import to the routes file's `from backend.schemas import (...)` block.

```bash
cd /home/user/MyProject
git checkout -b fix/<gh>-restore-import origin/main

# Find the import block anchor (the line just before the closing paren)
grep -n "CXReportRequest,\|PromoteToSopResponse," backend/api/routers/private_routes.py

# Patch: add the missing import line before the closing paren
# old_string = ...CXReportRequest,\n)\nfrom backend.schemas import CohortCreate, CohortUpdate
# new_string = ...CXReportRequest,\n    PromoteToSopResponse,\n)\nfrom backend.schemas import CohortCreate, CohortUpdate

git add backend/api/routers/private_routes.py
git commit -m "fix(gh-486): add missing PromoteToSopResponse import"
git push origin fix/<gh>-restore-import
gh pr create --base main --head fix/<gh>-restore-import \
  --title "fix: add missing PromoteToSopResponse import"
```

## Real-world example (GH-486, PR #536, Jul 21)

After #534 (promote-to-sop restore) merged, CI lint failed with:
```
F821 Undefined name 'PromoteToSopResponse'
    --> backend/api/routers/private_routes.py:6958:12
```

Diagnosis:
- `git show origin/main:backend/schemas.py | grep -c "^class PromoteToSopResponse"` → 1 (schema at line 845 ✓)
- `git show origin/main:backend/api/routers/private_routes.py | grep -c "PromoteToSopResponse"` → 0 (import missing ✗)
- `git show wt/t_c9de841e:backend/api/routers/private_routes.py | grep -n "PromoteToSopResponse"` → 132 (was imported in worktree ✓)

Fix: added `PromoteToSopResponse,` to the import block (line 133). One line. PR #536.

**Key insight:** When you restore a route from a clobbered PR, the route body is the visible casualty. The import line is the invisible one. Verify both survive.
