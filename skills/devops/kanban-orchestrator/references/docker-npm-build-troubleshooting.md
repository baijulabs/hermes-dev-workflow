# Docker / npm Build Troubleshooting

Common Docker build failures in the MyProject monorepo and how to fix them. This reference covers patterns encountered during CI/CD deploy jobs.

## 1. EOVERRIDE — Override conflicts with direct dependency

### Symptom

```
npm error code EOVERRIDE
npm error Override for react@^18.3.1 conflicts with direct dependency
```

The Docker `final` stage runs `npm install --omit=dev` standalone (no workspace context), and `frontend/package.json` lists the same package in both `dependencies` and `overrides`.

### Root Cause

npm v10+ (shipped with Node 20) rejects overrides that target direct dependencies. The `overrides` field is designed to pin transitive dependencies — packages that are NOT in your direct `dependencies` or `devDependencies` but are pulled in through other packages.

When a package appears in both:
```json
{
  "dependencies": { "react": "^18.3.1" },
  "overrides":  { "react": "18.3.1" }
}
```

npm fails with `EOVERRIDE` even if the versions match. The override is redundant: if the direct dependency already specifies `^18.3.1`, installing `18.3.1` is within range.

### Why it only fails in Docker (not local dev)

- **Local dev / `npm ci` builder stage**: runs inside the npm workspace. The **root** `package.json`'s `overrides` section applies to all workspaces, including `frontend/`. The workspace-level override in `frontend/package.json` is present but npm tolerates it because the workspace context makes the resolution chain different.
- **Docker `final` stage**: runs standalone (`COPY frontend/package*.json ./` then `npm install --omit=dev`). No root workspace — only `frontend/package.json` is present. npm v10+ strictly checks overrides against direct deps and fails.

### Fix

Remove the redundant entries from `frontend/package.json` overrides. The root workspace `package.json` handles transitive pinning:

```diff
  "overrides": {
-   "react": "18.3.1",
-   "react-dom": "18.3.1",
    "shell-quote": "^1.8.4",
    ...
  }
```

The root `package.json` already has these overrides for workspace-level transitive dependency pinning.

### Detection

```bash
# Check if any override targets a direct dependency (potential EOVERRIDE)
cd frontend
node -e "
const pkg = require('./package.json');
const deps = new Set([
  ...Object.keys(pkg.dependencies || {}),
  ...Object.keys(pkg.devDependencies || {})
]);
for (const [k, v] of Object.entries(pkg.overrides || {})) {
  if (deps.has(k)) console.log('WARNING:', k, 'is both a direct dep and an override —', v);
}
"
```

## 2. npm ci — lockfile-mismatch failures

### Symptom

```
npm ci --workspace=frontend
npm error code EUSAGE
npm error The package-lock.json file was created with an older version of npm
```

Or in Docker:

```
#14 [builder 5/8] RUN npm ci --workspace=frontend
#14 CANCELED
```

### Root Cause

`npm ci` requires an exact lockfile match. Different npm versions (e.g., npm 10 vs npm 11) produce different `package-lock.json` formats. When the CI runner's npm version differs from the developer's npm, `npm ci` fails.

### Fix

Replace `npm ci` with `npm install --prefer-offline --legacy-peer-deps`:

```diff
- RUN npm ci --workspace=frontend
+ RUN npm install --prefer-offline --legacy-peer-deps --workspace=frontend
```

- `--prefer-offline` uses cached packages when available (same speed as `npm ci`)
- `--legacy-peer-deps` is tolerant of dependency conflicts that `npm ci` would reject
- This is the same pattern used in `.github/workflows/deploy.yml` for the lint job

Apply this to:
- `frontend/Dockerfile` line 21 (builder stage)
- Any CI workflow step that uses `npm ci`

## 3. npm ci also canceled (cascading failure)

When the `final` stage fails, the `builder` stage is also canceled mid-step. This can make it look like the `npm ci` in the builder was the cause, when the real failure was in the final stage. Always scroll past `CANCELED` entries to find the actual `ERROR` line.

```diff
- #14 [builder 5/8] RUN npm ci --workspace=frontend   ← CANCELED (victim)
- #14 CANCELED
+ #13 [final 6/7] RUN npm install --omit=dev           ← ACTUAL ERROR
+ #13 ERROR: process ... exit code: 1
```

## 4. Vite Build — CSS Syntax Error (lightningcss)

### Symptom

```
[plugin vite:css-post]
SyntaxError: [lightningcss minify] Unexpected token CloseParenthesis
 372 |    padding: 8px 10px;
 373 |    z-index: 50;
 374 |    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1));
     |                                             ^
```

The build fails in the `vite build` step (always in the `builder` stage, not `final`). Lightningcss is strict about CSS syntax during minification.

### Root Cause

A misplaced or duplicate closing parenthesis/brace in a CSS property value — in this case an extra `)` at the end of a `box-shadow` value containing `rgba()`. The parentheses are unbalanced: `rgba(...))` has one more `)` than needed.

This is easy to miss because browsers tolerate the extra paren in dev mode (Chrome/Firefox ignore it). Only the lightningcss minifier in Vite's production build catches it.

### Fix

Remove the extra closing parenthesis. Trace the parens:

```diff
-  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1));
+  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1);
```

### Finding the Bad File

```bash
# From the Docker build log, look for the error line and file reference
grep -B5 "SyntaxError: \[lightningcss minify\]" build-log.txt

# Or scan all Vue files for unbalanced parens in CSS values
grep -rn 'rgba.*))' frontend/src/ --include='*.vue'
grep -rn 'hsla.*))' frontend/src/ --include='*.vue'
```

### Why it only fails in Docker (not local dev)

- **Local dev (`vite dev`):** uses the dev server, no CSS minification.
- **Local build (`vite build`):** uses lightningcss for minification — will fail the same way if the Docker image's node version matches.
- **Docker CI:** always runs production build with minification enabled, catches the error.

Test locally before pushing:
```bash
cd frontend && npm run build   # will catch the same error
```

## 5. EBADENGINE — Node version mismatch (warnings now, errors later)

### Symptom

```
npm warn EBADENGINE Unsupported engine {
  package: '@intlify/core-base@11.4.6',
  required: { node: '>= 22' },
  current: { node: 'v20.20.2' }
}
```

These are non-fatal `warn` level messages during `npm install`. The install succeeds, but the packages are running on an unsupported Node version.

### Root Cause

The Dockerfile uses `node:20-slim` but several production dependencies now require Node 22+:
- `@intlify/*` (i18n internals) — requires Node >= 22
- `concurrently@10.0.3` — requires Node >= 22
- `http-proxy-middleware@4.2.0` — requires Node ^22.15.0 || ^24.0.0 || >=26.0.0

### Risk

Currently warnings only. As these packages adopt Node 22+ APIs, they may start failing at runtime. The fix is to bump the Docker base image from `node:20-slim` to `node:22-slim` (or `node:24-slim`).

### Upgrade path

```diff
  # frontend/Dockerfile
- FROM node:20-slim AS builder
+ FROM node:22-slim AS builder
  ...
- FROM node:20-slim AS final
+ FROM node:22-slim AS final
```

Also update the CI workflow's `actions/setup-node@v4` `node-version` to match.

## 6. Cache dependency path — setup-node fails to resolve lockfile

### Symptom

```
Error: Some specified paths were not resolved, unable to cache dependencies.
```

The `actions/setup-node@v4` step fails immediately, before any install or build runs. The error appears in the GitHub Actions log for the job that uses `cache: npm` with `cache-dependency-path`.

### Root Cause

`cache-dependency-path` points to a file that doesn't exist. In a npm workspace monorepo, there is no `frontend/package-lock.json` — the lockfile lives at the root (`package-lock.json`). A path like `frontend/package-lock.json` in the `cache-dependency-path` setting causes `setup-node` to fail during cache key generation.

### Fix

Change the path to point to the root lockfile:

```diff
-       cache-dependency-path: frontend/package-lock.json
+       cache-dependency-path: package-lock.json
```

### When to suspect this

- The error appears in the `Setup Node.js` step, not during `npm install` or `npm run build`
- The job uses `actions/setup-node@v4` with `cache: npm` and `cache-dependency-path`
- The project is a npm workspace monorepo (lockfile at root, not per-workspace)

### Detection

```bash
# Check if the path exists
ls -la frontend/package-lock.json 2>&1   # should fail in monorepo
ls -la package-lock.json                 # should exist (root)
```

## 7. MISSING_EXPORT — Wrong import path for @vue-flow components

### Symptom

```
[MISSING_EXPORT] "Background" is not exported by "../node_modules/@vue-flow/core/dist/vue-flow-core.mjs".
    ╭─[ src/views/ProcessMap.vue:48:31 ]╮
48 │ import { VueFlow, useVueFlow, Background, Controls } from '@vue-flow/core'
```

The build fails in the `vite build` step with `MISSING_EXPORT` for `Background` and `Controls`. The error may appear only in Docker (CI) and not locally, or may fail everywhere.

### Root Cause

The import statement imports `Background` and `Controls` from `@vue-flow/core`, but in `@vue-flow/core@1.48.2`, these components were moved to separate packages (`@vue-flow/background`, `@vue-flow/controls`). They are NOT re-exported from the core package.

The correct import paths are:
```js
import { VueFlow, useVueFlow } from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import { Controls } from '@vue-flow/controls'
```

### Why it may appear only in Docker

If the lockfile was generated with npm 11+ (Node 22+) but the Dockerfile uses `node:20-slim` (npm 10), npm 10 may not correctly resolve the workspace-hoisted packages, causing Rolldown to merge the failed resolution into a single import from `@vue-flow/core`. In this case, two issues compound: the wrong import path AND the npm version mismatch. Fix both:

1. **Fix the import path** — use separate imports from the correct packages
2. **Upgrade the Docker base image** — from `node:20-slim` to `node:22-slim` (or `node:24-slim`)

### Fix

```diff
- import { VueFlow, useVueFlow, Background, Controls } from '@vue-flow/core'
+ import { VueFlow, useVueFlow } from '@vue-flow/core'
+ import { Background } from '@vue-flow/background'
+ import { Controls } from '@vue-flow/controls'
```

Verify the fix locally:
```bash
cd frontend && npm run build
```

### Detection

```bash
# Check if the import is from the wrong package
grep -rn "from '@vue-flow/core'" frontend/src/ --include='*.vue' --include='*.js'
# If it imports Background or Controls from @vue-flow/core, it's wrong
```

## 8. Quick diagnostic checklist

| Symptom | Check | Fix |
|---|---|---|
| `EOVERRIDE` | Is the overridden package also a direct dep? | Remove from child `overrides`; root handles it |
| `npm ci` fails / CANCELED | Lockfile version mismatch | Replace with `npm install --prefer-offline --legacy-peer-deps` |
| `SyntaxError: lightningcss minify` | Unbalanced parens in CSS value | Fix extra/missing `)` or `}` |
| `cache-dependency-path` error | File path doesn't exist | Point to root `package-lock.json` |
| Docker build slow | `--no-cache` flag | Remove for development rebuilds |
| Builder stage fails, final CANCELED | Check builder logs for actual error | Fix builder issue (often vite build) |
| Builder stage succeeds, final fails | Check final stage deps | Remove redundant overrides (see #1) |
| `MISSING_EXPORT` in Docker but not locally | Import from wrong package, or npm version mismatch | Fix import path (see #7), upgrade Docker Node image to 22+ |