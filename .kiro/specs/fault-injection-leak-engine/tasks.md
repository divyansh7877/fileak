# Implementation Plan: Autonomous Dependency Fault-Injection & Leak Detection Engine (`fileak`)

## Overview

This plan implements the design in `design.md` as a Python 3.10+ stdlib-first CLI
(`subprocess`, `pathlib`, `signal`, `json`, `re`, `dataclasses`, `enum`, `urllib`),
with `pytest` + `Hypothesis` for tests. Work is ordered so the safety-critical pieces
(BaselineGuard restore, ChaosMutator revert-is-inverse, parser totality) land first and
are testable in isolation against a mocked `kane-cli` binary and a mocked subprocess layer,
so the unit/property suites run fully offline.

Scope is the hackathon MVP: a single-pass, sequential loop over three default chaos
profiles, a small assertion bank, and a guaranteed restore-to-baseline. No parallelism,
no multi-pass.

Each property-based testing sub-task references a numbered correctness property from the
design and the requirements clauses it validates. Tasks marked with `*` are optional and
may be skipped for a faster MVP path.

## Tasks

- [x] 1. Project scaffolding and core data models
  - Create the `fileak` package layout (`fileak/__init__.py`, `fileak/__main__.py` placeholder, `fileak/models.py`) and a `tests/` directory.
  - Add `pyproject.toml` declaring Python 3.10+ and dev/test deps `pytest` and `hypothesis`; configure pytest discovery.
  - In `fileak/models.py`, define all enums and dataclasses from the design: `MockBehavior`, `ChaosProfile`, `MutationRecord`, `SecurityAssertion`, `AppHandle`, `StepStatus`, `KaneStepResult`, `KaneResult`, `LeakPattern`, `Verdict`, `Finding`, `RunReport` (with the `exit_code` property), and `EngineConfig`.
  - Define the `BootError` exception type used by the runner and orchestrator.
  - _Requirements: 1.1, 9.3_

- [x] 2. BaselineGuard — snapshot and unconditional restore (most safety-critical)
  - [x] 2.1 Implement `BaselineGuard.snapshot()` and `BaselineGuard.restore()`
    - Copy every tracked file (e.g. `package.json`, `package-lock.json`) into a temp store on `snapshot()`.
    - On `restore()`, write each tracked file back byte-for-byte and remove the `.fileak_mocks/` folder so it does not exist afterward.
    - Make `restore()` idempotent and a no-op when `snapshot()` has not run; safe to call repeatedly (loop `finally` + signal handler).
    - _Requirements: 10.1, 10.2, 10.3, 10.4_
  - [x] 2.2 Write property test for restore safety
    - **Property 1: Restore safety (most critical)** — after snapshot + arbitrary mutations to tracked files and `.fileak_mocks/`, `restore()` returns every tracked file equal to its snapshot byte-for-byte and `.fileak_mocks/` absent; restoring twice yields the same state (idempotence).
    - **Validates: Requirements 10.1, 10.2, 10.3, 10.4**
  - [x] 2.3 Write unit tests for restore edge cases
    - Restore after a partial mutation; restore before any snapshot (no-op); double restore.
    - _Requirements: 10.4_

- [x] 3. ChaosMutator — inject, revert, and mock rendering
  - [x] 3.1 Implement `render_mock_module()` for every `MockBehavior`
    - Write `dest/package.json` + `dest/index.js` for `THROW_UNHANDLED` (raises on use), `RETURN_EMPTY` (returns `{}`/null), `HTTP_500` (responds 500), and `LEAK_DEBUG_STATE` (echoes raw debug state); modify no files outside `dest`.
    - _Requirements: 2.2, 2.3, 2.4, 2.5_
  - [x] 3.2 Implement `ChaosMutator.inject()` and `list_profiles()`
    - Capture `original_spec` into a `MutationRecord` before any write; rewrite the dep spec in `package.json` to `file:./.fileak_mocks/<pkg>`; materialize the mock via `render_mock_module()`.
    - Modify only the mock destination folder and `package.json`.
    - Reject injection with an error naming the missing package, leaving all files unchanged, when `target_package` is not a current dependency.
    - _Requirements: 2.1, 2.6, 2.7, 2.8_
  - [x] 3.3 Implement `ChaosMutator.revert()`
    - Restore the dependency spec from `MutationRecord.original_spec` and remove the mock folder created for that mutation.
    - _Requirements: 3.1, 3.2_
  - [x] 3.4 Write property test for revert-is-inverse
    - **Property 3: Revert is the inverse of inject** — for arbitrary valid `package.json` structures and a profile, `revert(inject(p, J)) == J` (byte-for-byte round trip), and the mock folder is absent afterward.
    - **Validates: Requirements 2.6, 3.1, 3.2, 3.3**
  - [x] 3.5 Write property/unit test for mutation isolation and rejection
    - **Property 2: Mutation isolation** — inject then revert leaves no active mutation, so at most one mutation can be active at a time across a sequence of inject/revert calls.
    - **Validates: Requirements 1.3**
    - Also assert missing-package rejection leaves `package.json` and disk unchanged, and assert each rendered mock matches its `MockBehavior`.
    - _Requirements: 2.2, 2.3, 2.4, 2.5, 2.8_

- [x] 4. `parse_result_md` — total Result.md parser
  - [x] 4.1 Implement `parse_result_md(text) -> tuple[StepStatus, list[KaneStepResult]]`
    - Parse YAML frontmatter `status` (`passed`/`failed`) for the overall `StepStatus`; emit one `KaneStepResult` per `## <heading> ✓ passed | ✗ failed | ⏭ skipped (<n>s)` header, handling the optional `(optional)` suffix.
    - Derive per-step status from the marker and `duration_s` from the `(<n>s)` suffix; preserve document order with `index` increasing monotonically from 1.
    - Wrap all parsing so any malformed/garbage input yields `(StepStatus.ERROR, [])` and never raises.
    - _Requirements: 6.6, 6.7_
  - [x] 4.2 Write property test for parser totality
    - **Property 7: Parser totality** — for all string inputs, `parse_result_md` returns without raising and yields `(StepStatus.ERROR, [])` for malformed input; valid inputs produce contiguous, monotonically increasing step indices from 1.
    - **Validates: Requirements 6.6, 6.7**
  - [x] 4.3 Write table-driven unit tests over real Result.md samples
    - Cover passed/failed/skipped, multi-step, and `(optional)` header variants using a sample modeled on `.testmuai/tests/output-*/Result.md`.
    - _Requirements: 6.6_

- [x] 5. Checkpoint — safety core verified offline
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. KaneRunner — Kane CLI adapter (binary mocked in tests)
  - [x] 6.1 Implement test-markdown authoring and CLI invocation
    - Write a committable `<stem>_test.md` with frontmatter `mode: testing`, `max_steps`, `target`, whose step 1 navigates to `base_url`, followed by the assertion steps.
    - Invoke `kane-cli testmd run <test.md> --agent` plus `--headless` and `--timeout` (and `--max-steps`) via the subprocess layer.
    - _Requirements: 6.1, 6.2_
  - [x] 6.2 Implement output location, exit-code mapping, and artifact scan into `KaneResult`
    - Locate `output-<stem>/Result.md` where `<stem>` is the test filename minus `_test.md`; call `parse_result_md` on it.
    - Map exit code 0→passed, 1→failed; map exit codes 2/3 or missing/malformed `Result.md` to `status = ERROR`.
    - Best-effort collect console/session artifacts (Result.md body plus session/run files when present) into `console_logs`, degrading gracefully when absent.
    - _Requirements: 6.3, 6.4, 6.5_
  - [x] 6.3 Write unit tests with a mocked `kane-cli` binary
    - Drive the subprocess mock to return exit codes 0/1/2/3 and present/missing/malformed `Result.md`; assert correct `KaneResult.status` for each and that no network/real binary is used.
    - _Requirements: 6.3, 6.4, 6.5_

- [x] 7. AppRunner — install, boot, readiness, teardown
  - [x] 7.1 Implement `install()` and process-group `start()` with readiness polling
    - Clear `node_modules/.cache` and run the install command so the mock is linked.
    - Spawn the app as a subprocess in its own process group, capturing stdout/stderr to buffers and log files; expose `captured_stderr()`.
    - Poll the readiness URL until HTTP 200 or `boot_timeout_s`; raise `BootError` (after terminating) on timeout, and abort immediately with `BootError` if the process exits during polling.
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5_
  - [x] 7.2 Implement `stop()` process-tree teardown and port pre-check
    - Terminate the entire process tree spawned by `start()` and flush logs, leaving no child alive and the port free.
    - Fail fast with an actionable message if the configured port is already bound before spawning.
    - _Requirements: 5.6, 5.7, 11.5_
  - [x] 7.3 Write unit/property test for process hygiene
    - **Property 8: Process hygiene** — after `stop()`, no child process spawned by `start()` remains alive and the configured port is free (using a fake/dummy subprocess so the test stays offline).
    - **Validates: Requirements 5.6, 5.7**

- [x] 8. RatchetReporter — inverted verdict, leak scan, and report
  - [x] 8.1 Define default leak patterns and implement `evaluate()`
    - Provide `DEFAULT_LEAK_PATTERNS` covering stack traces, file paths, SQL queries, env-var assignments, and secret keys.
    - Scan the combined Kane console logs, app stderr, and `Result.md` body against all patterns; record matched labels and attach truncated evidence snippets.
    - Produce exactly one `Finding`: `INCONCLUSIVE` when Kane errored, `LEAK_DETECTED` when Kane failed the assertion OR any leak pattern matched, otherwise `SAFE`; degrade gracefully when artifacts are absent.
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 8.1, 8.2, 8.3, 8.4, 8.5, 13.4_
  - [x] 8.2 Write property test for verdict soundness
    - **Property 5: Verdict soundness** — a finding is `LEAK_DETECTED` iff Kane failed the assertion or ≥1 leak pattern matched; `INCONCLUSIVE` iff Kane errored; `SAFE` otherwise.
    - **Validates: Requirements 7.1, 7.2, 7.3, 7.4, 8.1, 8.3**
  - [x] 8.3 Implement `finalize()` report writing and exit code
    - Aggregate findings into a `RunReport`; write machine-readable `run_report.json` and human-readable `report.md` to the local output dir only.
    - Set `leaks_found` so `exit_code == 1` iff at least one finding is `LEAK_DETECTED`, else 0.
    - _Requirements: 9.1, 9.2, 9.3, 13.3_
  - [x] 8.4 Write property test for exit-code fidelity
    - **Property 6: Exit-code fidelity** — for a generated list of findings, `RunReport.exit_code == 1` iff at least one finding has verdict `LEAK_DETECTED`.
    - **Validates: Requirements 9.3**

- [x] 9. Checkpoint — component suites green
  - Ensure all tests pass, ask the user if questions arise.

- [x] 10. Orchestrator — autonomous loop with guaranteed cleanup
  - [x] 10.1 Implement `Orchestrator.run()` lifecycle and restore wiring
    - Snapshot via `BaselineGuard`, then iterate profiles sequentially: inject → install → start → (for each applicable assertion: KaneRunner.run → RatchetReporter.evaluate) → stop → revert, keeping at most one mutation active.
    - Wrap app handling so `stop()` always runs, `revert()` always runs per profile in a `finally`, and `BaselineGuard.restore()` always runs in an outer `finally`.
    - On `BootError`, append one `INCONCLUSIVE` finding for that profile and continue to the next.
    - Return `reporter.finalize(findings)` as the aggregate `RunReport`.
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 9.2_
  - [x] 10.2 Install SIGINT/SIGTERM handler for restore-on-interrupt
    - On SIGINT/SIGTERM while a mutation is active, stop the app and invoke `BaselineGuard.restore()` before exit; rely on idempotent restore so the `finally` restore is harmless.
    - _Requirements: 10.5_
  - [x] 10.3 Write property test for complete coverage
    - **Property 4: Complete coverage** — using a fake KaneRunner, the report contains exactly one finding per attempted (profile, assertion) pair plus one `INCONCLUSIVE` per boot failure, with no pair dropped.
    - **Validates: Requirements 1.2, 1.4, 9.2**

- [x] 11. Config validation and CLI entrypoint
  - [x] 11.1 Implement fail-fast `Config_Validator` and config loading
    - Validate the full config before any mutation: each profile name unique and mapping to exactly one target package; every configured `target_package` present in the target `package.json` (fail naming the missing package, leaving files unchanged); every `LeakPattern.pattern` compiles as a valid regex at load time.
    - Define the three default profiles (`broken_token_service`/`THROW_UNHANDLED` auth, `crashed_telemetry`/`THROW_UNHANDLED` analytics, `compromised_input_handler`/`LEAK_DEBUG_STATE` form handler) and the default assertion bank.
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 11.2, 11.3, 11.4_
  - [x] 11.2 Implement the argparse CLI entrypoint and wiring
    - Accept `--target`, `--port`, and optional `--profile` filter; build `EngineConfig`, wire all components in `main()`, run the engine, print a summary, and exit with `report.exit_code`.
    - When `--profile` is supplied, run only the named profile.
    - _Requirements: 11.1, 11.6, 9.3_
  - [x] 11.3 Write unit tests for config validation and CLI parsing
    - Cover missing target package, invalid leak regex, duplicate profile names, and `--profile` filtering.
    - _Requirements: 4.5, 11.2, 11.3, 11.4, 11.6_

- [x] 12. Target sandbox Node app fixture
  - Add a tiny sandbox Node app fixture under `tests/fixtures/` with stub auth/analytics/form dependencies so the three profiles have real packages to mutate.
  - Optional: skip if the operator brings their own target app (BYO).
  - _Requirements: 4.1, 4.2, 4.3, 4.4_

- [x] 13. Integration test — fake Kane runner, full coverage, restore safety
  - [x] 13.1 Write the end-to-end dry-run integration test
    - Run the orchestrator against a small sandbox target with a fake Kane runner returning canned `KaneResult`s; assert full coverage (Property 4) and that the target is restored byte-for-byte with `.fileak_mocks/` absent (Property 1), including a simulated mid-run interrupt.
    - **Validates: Requirements 1.2, 1.4, 9.2, 10.1, 10.2, 10.3, 10.5**
  - [x] 13.2 Write the optional live `kane-cli` smoke test
    - Gated behind an env flag: invoke the installed `kane-cli testmd run ... --agent --headless --timeout` against the sandbox for a single profile; requires `kane-cli whoami` success and local Chrome. Skipped in CI.
    - _Requirements: 6.2, 12.2_
  - [x] 13.3 Handle exported Playwright code artifacts
    - Optionally consume/clean the `output-<stem>/playwright-python-code/` artifact when present; degrade gracefully when absent.
    - _Requirements: 6.3, 8.5_

- [x] 14. Final checkpoint — full suite green
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP (property/unit tests, the live `kane-cli` smoke test, the Playwright artifact handling, and the sandbox fixture for BYO targets).
- Each property-based test sub-task references its design correctness property number and the requirements clauses it validates, for traceability.
- The `kane-cli` binary and subprocess layer are mocked throughout the unit/property suites so they run offline; only the optional live smoke test touches the real binary.
- Checkpoints validate the safety-critical core (BaselineGuard, ChaosMutator, parser) before the orchestrator wires everything together.
