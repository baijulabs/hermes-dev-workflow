# pull_request_target Checkout Gotcha

## Problem

A CI job (often Lint All) keeps failing with the same error across multiple commits, even though the fix is clearly on the PR branch. The linter reports missing keys/files that exist on the PR branch but not on the base branch.

## Root cause

`pull_request_target` events run the workflow file from the **base branch** (e.g., `main`), not the PR branch. If the workflow's checkout step uses `ref: ${{ github.ref }}` instead of `ref: ${{ github.event.pull_request.head.sha }}`, it checks out **main's code** for every PR.

## Detection

```bash
grep -A4 "actions/checkout" .github/workflows/deploy.yml | grep "github.ref"
# If any job uses github.ref without checking for pull_request_target first,
# that job is testing the base branch, not the PR.
```

Look for this pattern:
```yaml
- uses: actions/checkout@v4
  with:
    ref: ${{ github.event.inputs.ref || github.ref }}  # BAD — checks out main on PR events
```

vs the correct pattern:
```yaml
- uses: actions/checkout@v4
  with:
    ref: ${{ github.event_name == 'pull_request_target' && github.event.pull_request.head.sha || github.event.inputs.ref || github.ref }}  # OK
```

## Fix (two-step)

1. **Fix the data on the base branch immediately** to unblock the PR. E.g., if the linter complains about missing i18n keys, push the missing translations directly to main.
2. **Fix the workflow** in a separate PR by updating the checkout `ref:` to use `pull_request.head.sha` for `pull_request_target` events.

## Why iterating on the PR won't help

The CI reads the workflow file from the base branch. Any fix to the workflow file inside the PR branch is invisible to the CI runner until the PR is merged. This is a chicken-and-egg problem: you need the workflow fix merged to fix the CI, but the CI failure blocks merging.

## Real example

In MyProject's `deploy.yml`, the `lint-all` job used:
```yaml
ref: ${{ github.event.inputs.ref || github.ref }}
```

When the consolidation PR added `feasibility`, `design`, `testing` stage keys to `en.json`, the lint job (checking out main) couldn't find them in es/fr/pt. The fix had to be pushed directly to main. The workflow file was fixed in a follow-up PR.