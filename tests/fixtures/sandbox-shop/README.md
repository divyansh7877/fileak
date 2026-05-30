# sandbox-shop — fileak target fixture

A tiny, committable sandbox Node app used as a **target** for the fileak
fault-injection engine. It exists so the three default chaos profiles have real
dependency packages to mutate (Requirements 4.1–4.4).

## What it is

A minimal `http`-only Node server (`server.js`) that touches three dependencies
on every request and serves a clean `200` page in its healthy baseline. The
engine mutates one dependency at a time (rewriting `package.json` to point it at
a broken local mock under `.fileak_mocks/`), reboots the app, and drives Kane
CLI against it to check whether the broken state leaks sensitive internals.

## The three deps and which profile mutates each

| Dependency | Role        | Default profile               | Injected behavior  |
| ---------- | ----------- | ----------------------------- | ------------------ |
| `next-auth`| auth        | `broken_token_service`        | `THROW_UNHANDLED`  |
| `analytics`| analytics   | `crashed_telemetry`           | `THROW_UNHANDLED`  |
| `validator`| form handler| `compromised_input_handler`   | `LEAK_DEBUG_STATE` |

These names match `DEFAULT_PROFILES` in `fileak/config.py`.

## Layout

```
sandbox-shop/
  package.json          # declares the three target deps + a `start` script
  package-lock.json      # minimal lockfile (second BaselineGuard-tracked file)
  server.js              # tiny http server using the three deps
  node_stubs/            # vendored stubs so the app runs WITHOUT npm install
    next-auth/           #   (require() falls back to these when node_modules
    analytics/           #    is absent; the engine still mutates package.json)
    validator/
```

## Running it (optional, not in CI)

This fixture is **not run in CI** — no `node_modules` is committed and no `npm
install` is performed. `server.js` falls back to the vendored `node_stubs/` so a
BYO operator or the optional live Kane smoke test can boot it directly:

```bash
cd tests/fixtures/sandbox-shop
node server.js        # serves http://localhost:3000
```

Or point the engine at it:

```bash
python -m fileak --target tests/fixtures/sandbox-shop --port 3000
```
