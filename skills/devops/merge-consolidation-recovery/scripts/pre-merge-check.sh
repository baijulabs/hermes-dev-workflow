#!/usr/bin/env bash
# pre-merge-check.sh — run from a consolidation branch before creating or updating a PR
# Verifies: freshness (not behind main), route decorators, Express catch-all,
# React version pin, and i18n key consistency.
#
# Usage: ./scripts/pre-merge-check.sh
# Exit code: 0 = all clear, 1 = issue found (block the PR)

set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
FAILED=0

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

echo "=== Pre-Merge Verification Gate ==="
echo ""

# 0. Freshness check
echo "--- Freshness check ---"
git fetch origin main 2>/dev/null
BEHIND=$(git rev-list --count HEAD..origin/main 2>/dev/null || echo "unknown")
echo "  Consolidation branch is $BEHIND commits behind origin/main"
if [ "$BEHIND" != "0" ] && [ "$BEHIND" != "unknown" ]; then
    echo -e "  ${RED}FAIL: Stale branch — rebase onto origin/main before merging${NC}"
    FAILED=$((FAILED + 1))
else
    echo -e "  ${GREEN}PASS${NC}"
fi
echo ""

# 1. Route decorators — count on HEAD vs main
echo "--- Route decorator check ---"
HEAD_COUNT=$(grep -c "@router\.\|@app\." backend/api/routers/private_routes.py 2>/dev/null || echo 0)
MAIN_COUNT=$(git show origin/main:backend/api/routers/private_routes.py 2>/dev/null | grep -c "@router\.\|@app\." || echo 0)
echo "  HEAD: $HEAD_COUNT routes | main: $MAIN_COUNT routes"
if [ "$HEAD_COUNT" -lt "$MAIN_COUNT" ] 2>/dev/null; then
    echo -e "  ${RED}FAIL: Route decorators stripped — fewer than main${NC}"
    FAILED=$((FAILED + 1))
else
    echo -e "  ${GREEN}PASS${NC}"
fi
echo ""

# 2. Express catch-all — must be v8+ syntax
echo "--- Express catch-all check ---"
if grep -q "/{\*path}" frontend/server.js 2>/dev/null; then
    echo -e "  ${GREEN}PASS: Using /{*path} (path-to-regexp v8+)${NC}"
elif grep -q "app\.get.*\*" frontend/server.js 2>/dev/null; then
    echo -e "  ${RED}FAIL: Old wildcard — will crash with PathError on deploy${NC}"
    FAILED=$((FAILED + 1))
else
    echo "  WARNING: no catch-all route found"
fi
echo ""

# 3. React version pin
echo "--- React version pin check ---"
REACT_PINS=$(grep -c "react.*18\.3" package.json 2>/dev/null || echo 0)
echo "  React 18.3 pin count: $REACT_PINS (need 2: devDeps + overrides)"
if [ "$REACT_PINS" -ge 2 ] 2>/dev/null; then
    echo -e "  ${GREEN}PASS${NC}"
else
    echo -e "  ${RED}FAIL: React pin missing — Docker build will fail with MISSING_EXPORT${NC}"
    FAILED=$((FAILED + 1))
fi
echo ""

# 4. i18n key consistency
echo "--- i18n key check ---"
if node scripts/find-untranslated.js 2>/dev/null; then
    echo -e "  ${GREEN}PASS: All locale files in sync${NC}"
else
    echo -e "  ${RED}FAIL: i18n keys out of sync${NC}"
    FAILED=$((FAILED + 1))
fi
echo ""

echo "=== Result ==="
if [ "$FAILED" -eq 0 ]; then
    echo -e "${GREEN}All pre-merge checks passed. Safe to merge.${NC}"
else
    echo -e "${RED}$FAILED check(s) failed. Fix before merging.${NC}"
fi
exit $FAILED
