# Vendored terminal assets

These files are served by the python UI at `/terminal/assets/*` — but only
while `SCRIPTENGINE_TERMINAL` is on (the flag-off contract covers the assets,
not just the endpoints).

| File | Source |
|---|---|
| `xterm.js` | `@xterm/xterm` (UMD build), same package the React SPA bundles |
| `xterm.css` | `@xterm/xterm` stylesheet |
| `addon-fit.js` | `@xterm/addon-fit` (UMD build) |
| `terminal.js` | ScriptEngine's own python-UI terminal client (not vendored) |

They are vendored (committed here, not installed) so the python UI needs no
Node, no npm and no build step — cloning the repo is enough.

**Refreshing the vendored copies** after bumping `@xterm/xterm` /
`@xterm/addon-fit` in `frontend/package.json`:

```bash
cp frontend/node_modules/@xterm/xterm/lib/xterm.js       backend/app/vendor/
cp frontend/node_modules/@xterm/xterm/css/xterm.css       backend/app/vendor/
cp frontend/node_modules/@xterm/addon-fit/lib/addon-fit.js backend/app/vendor/
```

Current version: **xterm 6.0.0** / addon-fit 0.11.0.
