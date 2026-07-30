# Pre-Merge Validation via Staging Deploy

For risky changes (Dockerfile edits, CI workflow changes, dependency bumps, infrastructure config), validate the branch on staging before merging to main.

## Using workflow_dispatch

If the repo has a `workflow_dispatch` trigger on its deploy workflow, you can deploy any branch to staging:

1. Go to **GitHub Actions → [Workflow Name] → Run workflow**
2. Set `ref: <branch-name>` (the branch with your fix)
3. Set `environment: staging`
4. Leave `deploy_production: false`
5. Click **Run workflow**

This builds Docker images from the branch and deploys them to staging. The full pipeline runs — lint, tests, Docker build, Terraform apply. Monitor the run for any build failures that didn't surface in PR checks.

## When to validate

| Scenario | Validate? | Why |
|---|---|---|
| Dockerfile change | Yes | Docker build only runs in CI, not locally |
| CI workflow change | Yes | Bad syntax or missing files fail the runner |
| Dependency bump | Yes | Lockfile conflicts or EOVERRIDE only surface in Docker |
| CSS change | Maybe | Lightningcss catches syntax errors in production build |
| Pure JS/TS logic change | No | PR tests cover this |
| Documentation change | No | No deploy needed |

## Real-world example

In the MyProject monorepo, three rounds of fix-PR → staging-deploy-fail → create-fix-PR occurred in sequence:

1. **Dockerfile `npm ci` → `npm install`**: Lockfile version mismatch between npm 10 and 11. PR checks passed (they use the install pattern), but the Docker build used `npm ci` which failed.
2. **EOVERRIDE for react/react-dom**: The `frontend/package.json` had react in both `dependencies` and `overrides`. npm v10+ rejects this.
3. **CSS syntax error**: An extra `)` in `rgba(...))` — Lightningcss caught it in production build but browsers tolerated it in dev.
4. **`cache-dependency-path` pointing to non-existent file**: Lighthouse CI job had `frontend/package-lock.json` instead of root `package-lock.json`.

Lessons learned:
- PR checks validate the **test** path, not the **Docker build** path. Always deploy to staging for Docker/CI changes.
- Scroll past `CANCELED` entries in build logs — they're victims of the first error, not causes.
- A staging deploy failure is faster to debug than a main-merge revert.

## Pitfalls

- **`workflow_dispatch` is manual.** There's no automated trigger to deploy a PR branch. You must go to the Actions tab and kick it off yourself.
- **The ref must be exact.** Use the branch name (e.g., `fix/cache-dependency-path`), not the PR number. Commits pushed after the run starts won't be included.
- **Staging may have different config.** Secrets, env vars, and service bindings may differ from production. A staging pass does not guarantee production success.
- **Clean up after validation.** The staging deploy creates a new Docker image and Terraform apply. It doesn't interfere with the next deploy, but the image tag (commit SHA) is permanent in the registry.
- **The `workflow_dispatch` input `ref` defaults to `main`.** If you forget to change it, you'll re-deploy main, not your branch — wasting 10+ minutes of build time.