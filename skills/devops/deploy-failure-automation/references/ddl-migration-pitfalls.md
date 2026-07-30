# DDL Migration Pitfalls with `CREATE TABLE IF NOT EXISTS`

## The Problem

MyProject uses `CREATE TABLE IF NOT EXISTS` in `init_db()` (backend/database.py) for schema setup. This pattern is **not a migration tool** — it only creates tables that don't already exist. It never alters existing table definitions.

When a column constraint changes (e.g., `INTEGER NOT NULL` → `INTEGER`), `CREATE TABLE IF NOT EXISTS` silently skips the existing table. The old constraint persists. Tests that rely on the new constraint fail.

## The Two Fixes Required

A DDL change like removing `NOT NULL` requires **both**:

### 1. DDL fix (incomplete alone)

```sql
-- Old: impact_analysis_id INTEGER NOT NULL
CREATE TABLE IF NOT EXISTS step6_update_log (
    ...
    impact_analysis_id INTEGER,  -- NOT NULL removed
    ...
);
```

This only works on a **fresh database** where the table is created from scratch. CI creates a fresh Postgres per job, so this should work — **unless** the CI run's checkout happened before the DDL commit was pushed (see "Stale CI runs" below).

### 2. Runtime migration (the reliable fix)

To make the change idempotent on existing databases (dev, staging, production), add an explicit `ALTER TABLE`:

```python
# After CREATE TABLE IF NOT EXISTS, apply runtime migration
cursor.execute("""
    ALTER TABLE step6_update_log
    ALTER COLUMN impact_analysis_id DROP NOT NULL
""")
```

This is safe on both fresh databases (the column is already nullable, `DROP NOT NULL` is a no-op) and existing databases (the NOT NULL constraint is removed).

### 3. Function signature alignment

Update the database function signature so parameter types match the actual usage:

```python
# BEFORE — rejects None at type-check time (but not at runtime):
def log_step6_update(db, experiment_id: int, impact_analysis_id: int, ...):

# AFTER — accepts None explicitly:
def log_step6_update(db, experiment_id: int, impact_analysis_id: int = None, ...):
```

## Stale CI Runs

A common failure mode: the DDL fix is committed to the PR branch, but the CI run was triggered **before** the fix was pushed. The deploy-watch script sees the old failed run and reports it as a new failure. By the time the agent processes it, the fix is already on the branch and a newer CI run may have passed.

**Defense:** The deploy-watch script (v1.5.0+) checks for newer successful runs on the same branch before reporting. If the agent still receives a stale failure, verify with `gh run list --branch <branch> --limit 3 --json conclusion,createdAt` before creating fix cards.

## Detection Pattern

When investigating a `NotNullViolation` on a column that was recently changed:
1. Check the DDL in `database.py` — is the column nullable?
2. Check `git log --oneline backend/database.py` — was there a recent DDL change?
3. Check if the CI run's commit includes the DDL change: `gh run view <RUN_ID> --json headSha`
4. Check if a newer CI run on the same branch passed: `gh run list --branch <branch> --limit 3 --json conclusion`
5. If the DDL is correct in the latest commit but the CI run predates it, the failure is stale — no fix needed.

## Related Patterns

- See `deploy-failure-automation` pitfall: "Stale failure delivery to agent"
- `CREATE TABLE IF NOT EXISTS` is idempotent for creation, but `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` is needed for column additions