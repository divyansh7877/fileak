# vulnerable-shop — fileak chaos-run target

A deliberately **leak-prone** local Express app used as a real target for the
`fileak` fault-injection engine. **Do not deploy it** — it exists to leak
internals when a dependency misbehaves so the engine can detect the regression.

## Why it's a good chaos target

- `/` always returns **HTTP 200**, so the app boots/readies even when a
  dependency is swapped for a broken mock (the engine needs it to boot so Kane
  can inspect the page content).
- Each homepage section uses one mutated dependency, and the way it handles
  failure decides whether the outcome is a **leak** or a **graceful (safe)**
  failure:

| Section   | Dependency      | fileak profile (suggested)   | Broken behavior        | Outcome when broken            |
| --------- | --------------- | ---------------------------- | ---------------------- | ------------------------------ |
| Account   | `jsonwebtoken`  | `broken_token_service`       | `THROW_UNHANDLED`      | **Leak** — raw stack trace rendered |
| Telemetry | `uuid`          | `crashed_telemetry`          | `THROW_UNHANDLED`      | Safe — generic message, no internals |
| Newsletter| `validator`     | `compromised_input_handler`  | `LEAK_DEBUG_STATE`     | **Leak** — raw debug/env state echoed |

## Run it standalone

```bash
cd examples/vulnerable-shop
npm install
npm run start        # serves http://localhost:3000
```

## Run a chaos experiment against it

A ready-made config + driver lives in `examples/run_chaos.py`:

```bash
# from the repo root
python examples/run_chaos.py --target examples/vulnerable-shop --port 3000
```

This injects each profile's broken mock, boots the app, drives Kane against it,
inverts the verdict, writes a report, and always restores the app to baseline.
