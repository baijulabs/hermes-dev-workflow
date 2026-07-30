# Vitest React Mock Templates

When `@excalidraw/excalidraw` (or another React-interop library) is used in a Vue component, and Vitest can't resolve `react`/`react-dom` from the workspace-hoisted `node_modules`, use these mock files plus Vitest config aliases.

## Vitest Config Addition

In `frontend/vitest.config.js`, under `test` section:

```js
server: {
  deps: {
    inline: ['open-color', 'react', 'react-dom'],
  },
},
alias: [
  {
    find: /^react(-dom)?$/,
    replacement: fileURLToPath(new URL('./tests/unit/__mocks__/react.js', import.meta.url)),
  },
  {
    find: /^react-dom\/client$/,
    replacement: fileURLToPath(new URL('./tests/unit/__mocks__/react-dom-client.js', import.meta.url)),
  },
  {
    find: /^@excalidraw\/excalidraw$/,
    replacement: fileURLToPath(new URL('./tests/unit/__mocks__/excalidraw.js', import.meta.url)),
  },
],
```

## Mock Files

### `tests/unit/__mocks__/react.js`

```js
const React = {
  createElement: (...args) => ({ type: args[0], props: args[1] }),
  Fragment: Symbol('Fragment'),
  useState: (init) => [init, () => {}],
  useEffect: (fn) => fn(),
  useRef: (init) => ({ current: init }),
  useCallback: (fn) => fn,
  useMemo: (fn) => fn(),
  useContext: () => ({}),
};
export default React;
export const {
  createElement, Fragment, useState, useEffect,
  useRef, useCallback, useMemo, useContext,
} = React;
```

### `tests/unit/__mocks__/react-dom-client.js`

```js
const ReactDOMClient = {
  createRoot: () => ({ render: () => {}, unmount: () => {} }),
};
export default ReactDOMClient;
export const { createRoot } = ReactDOMClient;
```

## When This Doesn't Work

If adding mocks isn't feasible (e.g., the component genuinely needs React rendering, not just imports), the alternative is to add `react` and `react-dom` as direct `devDependencies` in the root `package.json` and remove any version override that conflicts. This changes the resolved React version and may break Excalidraw compatibility — test thoroughly.