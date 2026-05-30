# Requirements Document

## Introduction

The Autonomous Dependency Fault-Injection & Leak Detection Engine ("the engine", working name `fileak`) is a local, developer-facing Python CLI that autonomously hunts for security and information-leak regressions which only surface when an application's dependencies misbehave. Rather than writing brittle, hardcoded end-to-end assertions, the engine injects controlled "chaos" into a target sandbox app — swapping a real npm dependency for a deliberately broken local mock — boots the mutated app, and drives [Kane CLI](https://www.testmuai.com/kane-cli/) against it with semantic, natural-language security assertions. Kane's vision-and-DOM-aware agent judges whether the broken state leaks sensitive system information or fails gracefully, and the engine inverts that judgment into a security verdict.

These requirements are derived from the approved design document (`design.md`) and are scoped to the hackathon MVP described there: a single-pass, sequential loop over three default chaos profiles, with a small assertion bank and a guaranteed restore-to-baseline. Parallelism and multi-pass iteration are explicitly out of scope. Acceptance criteria are written to be traceable to the design's eight correctness properties (restore safety, mutation isolation, revert-is-inverse, complete coverage, verdict soundness, exit-code fidelity, parser totality, process hygiene).

## Glossary

- **Fault_Injection_Engine**: The overall local CLI tool that orchestrates a chaos run; also referred to as "the engine".
- **Orchestrator**: The deterministic outer control loop that sequences all other components and guarantees cleanup.
- **Chaos_Mutator**: The component that injects a single chaos profile (swapping a dependency for a mock) and reverts it precisely.
- **App_Runner**: The component that clears cache, installs, boots the target app subprocess, polls readiness, and tears it down.
- **Kane_Runner**: The adapter over the Kane CLI binary that writes test markdown, invokes the CLI, and parses results.
- **Ratchet_Reporter**: The verdict engine that maps Kane results plus log scanning into security findings and aggregates the report.
- **Baseline_Guard**: The safety component that snapshots tracked files and restores them unconditionally.
- **Config_Validator**: The logic that validates the engine configuration before any mutation occurs.
- **Target_App**: The sandbox application under test, controlled by the operator.
- **Chaos_Profile**: A named experiment defining which dependency to break and how (a `MockBehavior`).
- **Mock_Module**: The deliberately broken local dependency module materialized on disk for a profile.
- **MockBehavior**: The misbehavior kind of a mock: `THROW_UNHANDLED`, `RETURN_EMPTY`, `HTTP_500`, or `LEAK_DEBUG_STATE`.
- **MutationRecord**: The record capturing exactly what an injection changed, enabling a precise revert.
- **Security_Assertion**: A semantic, natural-language instruction handed to Kane CLI.
- **Leak_Pattern**: A labeled regular expression used to flag sensitive data in logs or output.
- **Finding**: The result for a single (profile, assertion) pair: a verdict plus evidence.
- **Verdict**: One of `SAFE`, `LEAK_DETECTED`, or `INCONCLUSIVE`.
- **Run_Report**: The aggregate of all findings, written as JSON and Markdown, carrying the run exit code.
- **Result.md**: The Kane CLI output file (`output-<stem>/Result.md`) containing status frontmatter and per-step markers.
- **Baseline**: The pre-run snapshot state of the target repository's tracked files.

## Requirements

### Requirement 1: Autonomous chaos run lifecycle

**User Story:** As a developer running a sandbox app, I want the engine to autonomously run a single-pass chaos loop over a fixed set of profiles, so that I can detect dependency-induced security regressions in one command without writing brittle assertions.

#### Acceptance Criteria

1. WHEN a run is started against a Target_App, THE Orchestrator SHALL process each configured Chaos_Profile sequentially following the lifecycle inject → install → boot → assert → stop → revert.
2. WHEN processing a Chaos_Profile, THE Orchestrator SHALL evaluate every Security_Assertion applicable to that profile against the running Target_App.
3. WHILE a run is in progress, THE Orchestrator SHALL keep at most one Chaos_Profile mutation active at any instant.
4. WHEN all configured profiles have been processed, THE Orchestrator SHALL return a Run_Report covering every attempted (profile, assertion) pair.

### Requirement 2: Chaos injection

**User Story:** As a developer, I want the engine to swap a real npm dependency for a deliberately broken local mock, so that I can observe how the application behaves when that dependency misbehaves.

#### Acceptance Criteria

1. WHEN injecting a Chaos_Profile, THE Chaos_Mutator SHALL rewrite the target dependency spec in `package.json` to `file:./.fileak_mocks/<package>` and materialize a Mock_Module on disk.
2. WHEN rendering a Mock_Module with behavior `THROW_UNHANDLED`, THE Chaos_Mutator SHALL produce a module that raises an unhandled exception on use.
3. WHEN rendering a Mock_Module with behavior `RETURN_EMPTY`, THE Chaos_Mutator SHALL produce a module that returns an empty object or null for every call.
4. WHEN rendering a Mock_Module with behavior `HTTP_500`, THE Chaos_Mutator SHALL produce a module that responds with HTTP 500 Internal Server Error.
5. WHEN rendering a Mock_Module with behavior `LEAK_DEBUG_STATE`, THE Chaos_Mutator SHALL produce a module that echoes raw internal debug state.
6. WHEN injecting a Chaos_Profile, THE Chaos_Mutator SHALL capture the original dependency spec into a MutationRecord before writing any change.
7. WHILE injecting a Chaos_Profile, THE Chaos_Mutator SHALL modify only the Mock_Module destination folder and `package.json`.
8. IF a Chaos_Profile's target package is not a current dependency in `package.json`, THEN THE Chaos_Mutator SHALL reject the injection with an error naming the missing package and SHALL leave all files unchanged.

### Requirement 3: Precise revert

**User Story:** As a developer, I want each injected mutation reverted precisely, so that one profile cannot corrupt the next and the repository stays trustworthy between profiles.

#### Acceptance Criteria

1. WHEN reverting a MutationRecord, THE Chaos_Mutator SHALL restore the target dependency spec in `package.json` to the original spec captured in that record.
2. WHEN reverting a MutationRecord, THE Chaos_Mutator SHALL remove the Mock_Module folder created for that mutation.
3. FOR ALL valid `package.json` contents, applying inject and then revert SHALL produce `package.json` content equal to the original content (round-trip property).

### Requirement 4: Default chaos profiles

**User Story:** As a developer, I want a set of ready-made chaos profiles, so that I can run meaningful experiments without authoring profiles from scratch.

#### Acceptance Criteria

1. THE Fault_Injection_Engine SHALL provide three default Chaos_Profiles named `broken_token_service`, `crashed_telemetry`, and `compromised_input_handler`.
2. THE `broken_token_service` profile SHALL apply `THROW_UNHANDLED` behavior to the configured authentication dependency.
3. THE `crashed_telemetry` profile SHALL apply `THROW_UNHANDLED` behavior to the configured analytics dependency.
4. THE `compromised_input_handler` profile SHALL apply `LEAK_DEBUG_STATE` behavior to the configured form-handling dependency.
5. THE Config_Validator SHALL require each Chaos_Profile name to be unique within a configuration and to map to exactly one target package.

### Requirement 5: Target application lifecycle

**User Story:** As a developer, I want the engine to install, boot, and tear down the target app cleanly, so that each profile runs against a freshly mutated app and no server processes are left orphaned.

#### Acceptance Criteria

1. WHEN preparing a profile, THE App_Runner SHALL clear the `node_modules/.cache` directory and run the configured install command so the Mock_Module is linked.
2. WHEN starting the Target_App, THE App_Runner SHALL spawn the app as a subprocess in its own process group and capture stdout and stderr to buffers and log files.
3. WHILE waiting for readiness, THE App_Runner SHALL poll the configured readiness URL until it returns HTTP 200 or the configured boot timeout elapses.
4. IF the Target_App does not return HTTP 200 within `boot_timeout_s`, THEN THE App_Runner SHALL terminate the process and raise a BootError.
5. IF the Target_App process exits during readiness polling, THEN THE App_Runner SHALL abort polling immediately and raise a BootError.
6. WHEN stopping the Target_App, THE App_Runner SHALL terminate the entire process tree spawned by the start operation.
7. WHEN the stop operation completes, THE App_Runner SHALL leave no child process spawned by start alive and SHALL leave the configured port free.

### Requirement 6: Kane CLI integration

**User Story:** As a developer, I want the engine to drive Kane CLI with committable test markdown and parse its results, so that semantic assertions execute against the app and outcomes are machine-consumable.

#### Acceptance Criteria

1. WHEN running a Security_Assertion, THE Kane_Runner SHALL write a committable `<stem>_test.md` file with frontmatter `mode: testing`, `max_steps`, and `target`, whose first step navigates to the Target_App base URL.
2. WHEN invoking Kane CLI for an automated run, THE Kane_Runner SHALL execute `kane-cli testmd run <test.md> --agent` and SHALL include the `--headless` and `--timeout` flags.
3. WHEN a Kane run completes, THE Kane_Runner SHALL locate and parse `output-<stem>/Result.md`, where `<stem>` is the test filename without the `_test.md` suffix.
4. THE Kane_Runner SHALL map Kane exit code 0 to passed and exit code 1 to failed.
5. IF Kane exits with code 2 or code 3, or `Result.md` is missing or malformed, THEN THE Kane_Runner SHALL return a KaneResult with status `ERROR`.
6. WHEN parsing `Result.md`, THE parse function SHALL emit one step result per step header, with status derived from the `✓ passed` / `✗ failed` / `⏭ skipped` marker and duration derived from the `(<n>s)` suffix, preserving document order with monotonically increasing indices from 1.
7. FOR ALL string inputs, THE parse function SHALL return a result without raising, yielding status `ERROR` and an empty step list for malformed input.

### Requirement 7: Inverted security verdict mapping

**User Story:** As a developer, I want Kane's pass/fail mapped into inverted security verdicts, so that a graceful failure reads as safe while a leak reads as a detected vulnerability.

#### Acceptance Criteria

1. WHEN evaluating a KaneResult, THE Ratchet_Reporter SHALL produce exactly one Finding whose verdict is `SAFE`, `LEAK_DETECTED`, or `INCONCLUSIVE`.
2. IF the KaneResult status is `ERROR`, THEN THE Ratchet_Reporter SHALL assign the verdict `INCONCLUSIVE`.
3. WHILE the KaneResult status is not `ERROR`, IF Kane failed the assertion OR at least one Leak_Pattern matched the captured logs and output, THEN THE Ratchet_Reporter SHALL assign the verdict `LEAK_DETECTED`.
4. WHILE the KaneResult status is not `ERROR`, WHEN Kane passed the assertion and no Leak_Pattern matched, THE Ratchet_Reporter SHALL assign the verdict `SAFE`.

### Requirement 8: Leak-pattern scanning of logs and output

**User Story:** As a developer, I want the engine to scan logs and output for sensitive-data indicators, so that leaks are caught even when Kane judges the page as passing.

#### Acceptance Criteria

1. WHEN evaluating a Finding, THE Ratchet_Reporter SHALL scan the combined Kane console logs, Target_App stderr, and `Result.md` body against all configured Leak_Patterns.
2. THE Fault_Injection_Engine SHALL provide default Leak_Patterns covering stack traces, file paths, SQL queries, environment variable assignments, and secret keys.
3. WHEN a Leak_Pattern matches the scanned text, THE Ratchet_Reporter SHALL record the pattern label as a leak indicator and attach a matched evidence snippet to the Finding.
4. THE Ratchet_Reporter SHALL truncate each attached evidence snippet to the configured maximum length.
5. IF Kane session or log artifacts are absent, THEN THE Ratchet_Reporter SHALL evaluate using the available text and complete without raising.

### Requirement 9: Reporting and exit code

**User Story:** As a developer, I want machine-readable and human-readable reports plus a meaningful exit code, so that I can consume results both in automation and by eye.

#### Acceptance Criteria

1. WHEN finalizing a run, THE Ratchet_Reporter SHALL write a machine-readable `run_report.json` and a human-readable `report.md`.
2. THE Run_Report SHALL contain exactly one Finding per attempted (profile, assertion) pair, plus one `INCONCLUSIVE` Finding per profile boot failure.
3. THE Run_Report exit code SHALL equal 1 if at least one Finding has verdict `LEAK_DETECTED`, and SHALL equal 0 otherwise.

### Requirement 10: Restore safety

**User Story:** As a developer, I want the target repository always returned to baseline, so that the tool never leaves a deliberately broken or insecure dependency wired into my project.

#### Acceptance Criteria

1. WHEN a run starts, THE Baseline_Guard SHALL snapshot every tracked file before any mutation occurs.
2. WHEN a run ends for any reason — success, boot failure, Kane error, unhandled exception, or SIGINT/SIGTERM — THE Baseline_Guard SHALL restore every tracked file to equal its snapshot byte-for-byte.
3. WHEN restoring, THE Baseline_Guard SHALL remove the `.fileak_mocks/` folder so that it does not exist after the run.
4. WHEN the restore operation is invoked more than once, THE Baseline_Guard SHALL produce the same restored state on each invocation (idempotence).
5. IF a SIGINT or SIGTERM is received while a mutation is active, THEN THE Orchestrator SHALL stop the Target_App and invoke the Baseline_Guard restore operation before the process exits.

### Requirement 11: CLI usability and fail-fast configuration validation

**User Story:** As an operator, I want clear command-line options and fail-fast validation, so that misconfiguration is caught before any file on disk is changed.

#### Acceptance Criteria

1. THE Fault_Injection_Engine SHALL accept a `--target` option for the target app directory, a `--port` option for the local port, and an optional `--profile` option that filters the run to a single Chaos_Profile.
2. WHEN a run is requested, THE Config_Validator SHALL validate the full configuration before any mutation occurs.
3. IF a configured target package is not present in the Target_App `package.json`, THEN THE Config_Validator SHALL fail with a message naming the missing package and SHALL leave all files unchanged.
4. IF a configured Leak_Pattern is not a valid regular expression, THEN THE Config_Validator SHALL fail at configuration load time before any mutation.
5. IF the configured port is already bound by another process, THEN THE App_Runner SHALL fail with an actionable message before spawning the Target_App.
6. WHERE the `--profile` filter is supplied, THE Orchestrator SHALL run only the named Chaos_Profile.

### Requirement 12: Prerequisites and credential delegation

**User Story:** As an operator, I want the engine to depend on out-of-band Kane authentication and a supported toolchain, so that the run relies on the CLI's stored credentials and the engine never handles secrets itself.

#### Acceptance Criteria

1. THE Fault_Injection_Engine SHALL require Node.js version 18 or later and a locally installed Google Chrome.
2. WHEN a run is requested, THE Fault_Injection_Engine SHALL require that `kane-cli login` has been completed such that `kane-cli whoami` succeeds.
3. THE Fault_Injection_Engine SHALL rely on Kane CLI's stored credentials for all Kane operations.
4. THE Fault_Injection_Engine SHALL keep credentials out of environment variables it sets, generated test files, and reports.

### Requirement 13: Local-only security constraints

**User Story:** As a security-conscious operator, I want the engine constrained to local, controlled environments with safe evidence handling, so that deliberately injected vulnerabilities and captured secrets cannot cause harm.

#### Acceptance Criteria

1. THE Fault_Injection_Engine SHALL operate as a local-only tool that exposes no network listener of its own.
2. THE Chaos_Mutator SHALL point a mutated dependency only at a local `file:` path and SHALL avoid referencing any published package.
3. THE Ratchet_Reporter SHALL write reports only to the local output directory under the spec.
4. THE Ratchet_Reporter SHALL keep evidence snippets local by truncating them and writing them only to local reports.
