# fileak

**Autonomous, LLM-driven dependency fault-injection & leak-detection engine.**

`fileak` hunts for security and information-leak regressions that only surface
when an application's dependencies misbehave. Instead of writing brittle,
hardcoded end-to-end assertions, it:

1. **Plans chaos** — an LLM (Anthropic Claude) reads a target app's
   dependencies and authors experiments: which package to break, how, and what
   semantic security assertions to check.
2. **Injects chaos** — swaps a real npm dependency for a deliberately broken
   local mock, then boots the mutated app.
3. **Judges with Kane** — drives [Kane CLI](https://www.testmuai.com/kane-cli/)'s
   vision + DOM browser agent against the running app to decide whether the
   broken state *leaks* sensitive internals (stack traces, file paths, SQL,
   secrets, env vars) or *fails gracefully*.
4. **Inverts the verdict** — a Kane "pass" (nothing leaked) is **SAFE**; a Kane
   "fail" or a matched leak pattern is **LEAK_DETECTED**; a Kane error is
   **INCONCLUSIVE**.
5. **Advises fixes** — for every detected leak, the LLM proposes a concrete,
   minimal remediation.
6. **Always restores** — the target repo is returned to baseline byte-for-byte
   on every exit path (success, crash, or Ctrl-C).

A live **dashboard** surfaces the whole flow: the plan, the experiments, the
findings ledger with evidence, the raw report/plan/advice JSON, the exact LLM
prompts, and the property-test suite — auto-refreshing as you run experiments.

> **Local-only, by design.** `fileak` deliberately introduces broken/insecure
> dependencies and (optionally) executes LLM-authored mock code. Only ever point
> it at a sandbox app you control. Never run it against production or shared
> infrastructure.

---

## How it works

```
                ┌─────────────┐
   package.json │   PLANNER   │  Claude authors profiles + assertions
   + source ───▶│   (LLM)     │  (optionally writes the mock JS itself)
                └──────┬──────┘
                       │ ChaosProfile[] + SecurityAssertion[]
                       ▼
                ┌─────────────┐
                │ ORCHESTRATOR│  deterministic loop, guarantees cleanup
                └──────┬──────┘
        inject ┌───────┼───────┐ revert (always)
               ▼       ▼       ▼
        ┌──────────┐ ┌──────┐ ┌──────────────┐
        │ChaosMutat│ │ App  │ │ BaselineGuard│  snapshot + restore
        │  + mock  │ │Runner│ │  (byte-exact)│
        └──────────┘ └──┬───┘ └──────────────┘
                        │ booted app on localhost
                        ▼
                ┌─────────────┐
                │  KANE CLI   │  vision+DOM agent judges each assertion
                └──────┬──────┘
                       │ KaneResult (pass/fail/error)
                       ▼
                ┌─────────────┐     ┌─────────────┐
                │  RATCHET    │────▶│  ADVISOR    │  Claude proposes fixes
                │  REPORTER   │     │   (LLM)     │  for each leak
                └──────┬──────┘     └─────────────┘
                       │ run_report.json + report.md
                       ▼
                ┌─────────────┐
                │  DASHBOARD  │  live view of the whole run
                └─────────────┘
```

The deterministic outer loop contains **no AI** — all semantic judgement is
delegated to Kane, and all chaos authoring to the LLM. This keeps the engine
debuggable and reproducible while still being "autonomous."

---

## Repository layout

| Path | What it is |
|------|------------|
| `fileak/` | The core engine (stdlib-only, property-tested). |
| `fileak/orchestrator.py` | The autonomous loop + guaranteed cleanup. |
| `fileak/chaos.py` | `ChaosMutator` — inject/revert a broken mock; mock rendering. |
| `fileak/app_runner.py` | Install, boot, readiness-poll, and tear down the target app. |
| `fileak/kane.py` | Adapter over the `kane-cli` binary; parses `Result.md`. |
| `fileak/reporter.py` | Inverted-verdict logic + leak-pattern scanning + reports. |
| `fileak/baseline.py` | `BaselineGuard` — snapshot + unconditional byte-exact restore. |
| `fileak/config.py` | Fail-fast config validation + default profiles. |
| `fileak/llm/` | **LLM layer** (Anthropic). |
| `fileak/llm/planner.py` | Claude authors chaos profiles + assertions. |
| `fileak/llm/mock_guard.py` | Static safety gate for LLM-authored mock code. |
| `fileak/llm/advisor.py` | Claude proposes fixes for detected leaks. |
| `examples/vulnerable-shop/` | A deliberately leak-prone Express target app. |
| `examples/run_chaos.py` | Run a chaos experiment with built-in profiles. |
| `examples/run_llm_chaos.py` | Run the full LLM-driven pipeline (plan → judge → advise). |
| `dashboard/` | Static, auto-refreshing dashboard (no build step). |
| `tests/` | Unit + property-based tests for the engine. |
| `.kiro/specs/fault-injection-leak-engine/` | Requirements, design, and tasks. |
| `.kiro/hooks/` | One-click "run chaos + refresh dashboard" agent hook. |

---

## Requirements

- **Python 3.10+**
- **Node.js 18+** and a local **Google Chrome** (for live Kane runs)
- **`kane-cli`** authenticated — `kane-cli whoami` must succeed
  (`npm install -g @testmuai/kane-cli`, then `kane-cli login`)
- **`ANTHROPIC_API_KEY`** for the LLM planner/advisor (see below)

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"           # engine + pytest + hypothesis
pip install anthropic python-dotenv  # for the LLM layer

cp .env.example .env              # then add your ANTHROPIC_API_KEY
```

The API key is read from `.env` (gitignored) and never written into generated
files, mocks, or reports.

---

## Quick start

### 1. Run the test suite (offline, no keys needed)

```bash
pytest --ignore=tests/test_live_kane_smoke.py -q
```

### 2. Prepare the sample target app

```bash
cd examples/vulnerable-shop && npm install && cd -
```

### 3. Run a chaos experiment

Free/offline (no Kane credits — scans the page over HTTP):

```bash
python examples/run_llm_chaos.py --target examples/vulnerable-shop --fake-kane --allow-custom-code
```

Full live run (real Kane browser agent judges each assertion):

```bash
python examples/run_llm_chaos.py --target examples/vulnerable-shop --port 3000 --allow-custom-code
```

Outputs land in `examples/.chaos_output/`:
`run_report.json`, `report.md`, `llm_plan.json`, `fix_suggestions.json`,
`llm_prompts.json`. The process exit code is `1` if any leak was detected, else `0`.

### 4. View the dashboard (auto-refreshing)

```bash
# terminal A — rebuild dashboard data whenever a run produces output
python dashboard/generate_data.py --watch

# terminal B — serve the dashboard (run from the repo root)
python -m http.server 8777 --directory dashboard
```

Open <http://localhost:8777>. As you run experiments, the dashboard updates on
its own (it polls `data.json` every few seconds; a "live" dot pulses on change).

Or trigger everything with one click via the Kiro agent hook
**"Run LLM chaos + refresh dashboard"** in the Agent Hooks panel.

---

## The LLM layer

The planner turns a target's `package.json` (+ a few source excerpts) into
chaos experiments. Two modes:

- **params-only** (default): the LLM picks a target package, one of four vetted
  mock behaviors (`THROW_UNHANDLED`, `RETURN_EMPTY`, `HTTP_500`,
  `LEAK_DEBUG_STATE`), and tailored assertions. **No model-authored code runs.**
- **free-form** (`--allow-custom-code`): the LLM also authors the mock's
  JavaScript, enabling novel break modes.

### Safety gate for generated code

Because free-form mode executes model-authored JS inside the target app
(via `npm install`), every generated mock must pass
`fileak/llm/mock_guard.py` **before it touches disk**:

- **Denylist scan** — rejects `child_process`, `fs`, `net`/`http`/`https`,
  `fetch`, `eval`, `new Function`, `vm`, `process.exit`, env writes, and any
  `require`/`import` of something other than the package being mocked.
- **Syntax gate** — `node --check`.
- **Size cap** and provenance (every mock is shown in the dashboard).

Anything that fails is **dropped** (with the reason recorded), never run.
Containment beyond the gate is the engine's guarantee: mocks live only under
`.fileak_mocks/`, only `package.json` is mutated, the target is local-only, and
`BaselineGuard` restores byte-for-byte on every exit path.

> A static denylist is a strong speed-bump, not a sandbox. Treat free-form mode
> as "run only against a throwaway local target you can afford to have restored."

---

## The sample target: `vulnerable-shop`

A tiny Express app under `examples/vulnerable-shop/` that **deliberately leaks**
when a dependency misbehaves, so the engine has something real to detect:

| Section | Dependency | Broken behavior | Outcome |
|---------|-----------|-----------------|---------|
| Account | `jsonwebtoken` | throws | **Leak** — renders the raw stack trace |
| Telemetry | `uuid` | throws | **Safe** — generic message, no internals |
| Newsletter | `validator` | leaks debug state | **Leak** — echoes raw debug/env object |

`/` always returns HTTP 200 so the app boots even when a dependency is broken;
each section's error handling is what differentiates a leak from a graceful
failure. **Do not deploy it.**

---

## Correctness & testing

The engine is developed against eight formal correctness properties (restore
safety, mutation isolation, revert-is-inverse, complete coverage, verdict
soundness, exit-code fidelity, parser totality, process hygiene), validated with
[Hypothesis](https://hypothesis.readthedocs.io/) property-based tests plus unit
tests. The mock-guard safety gate has its own test suite.

```bash
pytest --ignore=tests/test_live_kane_smoke.py -q   # full offline suite
```

The optional live Kane smoke test is gated behind `FILEAK_LIVE_KANE=1` and
requires a running app + `kane-cli` login.

Full requirements, design, and the implementation task list live under
`.kiro/specs/fault-injection-leak-engine/`.

---

## Known limitations

- **Dashboard "real-time" is poll + watch**, not intra-run streaming: you see
  updates as each *run* completes, not verdict-by-verdict within a single Kane
  run.
- **Target-app fragility**: some LLM-authored break modes can crash the target
  at boot rather than rendering a leak, producing `INCONCLUSIVE` findings. The
  durable fix is hardening the target so every section fails inside its own
  error handler.
- **LLM output is non-deterministic**; plans are cached to disk so a dashboard
  refresh doesn't re-spend tokens, but two runs may propose different chaos.
