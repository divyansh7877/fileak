"""End-to-end *dry-run* integration test for the fileak engine (task 13.1).

This exercises the real :class:`~fileak.orchestrator.Orchestrator` driving the
real safety-critical components — :class:`~fileak.chaos.ChaosMutator`,
:class:`~fileak.baseline.BaselineGuard`, and :class:`~fileak.reporter.RatchetReporter`
— against a small sandbox target, using a FAKE Kane runner and a real
:class:`~fileak.app_runner.AppRunner` whose externally-uncertain seams are all
injected so the run is fully **offline and deterministic** (no real process,
network, npm, or Kane binary is touched).

It validates two design correctness properties end-to-end:

* **Property 4 — Complete coverage** (Requirements 1.2, 1.4, 9.2): the report
  contains exactly one finding per attempted (profile, assertion) pair.
* **Property 1 — Restore safety** (Requirements 10.1, 10.2, 10.3, 10.5): after a
  run — including one interrupted mid-mutation — every tracked file equals its
  pre-run bytes and ``.fileak_mocks/`` does not exist under the target.

Plus exit-code fidelity and report emission (Requirements 9.x).

Validates: Requirements 1.2, 1.4, 9.2, 10.1, 10.2, 10.3, 10.5
"""

from __future__ import annotations

import json
import signal
from dataclasses import dataclass
from pathlib import Path

import pytest

from fileak.app_runner import AppRunner
from fileak.baseline import BaselineGuard
from fileak.chaos import ChaosMutator
from fileak.config import build_default_config, index_profiles, validate_config
from fileak.models import KaneResult, SecurityAssertion, StepStatus, Verdict
from fileak.orchestrator import Orchestrator
from fileak.reporter import DEFAULT_LEAK_PATTERNS, RatchetReporter

# The default profiles/assertions produce this exact (profile, assertion) matrix
# (design "Default Chaos Profiles" + "Default Semantic Assertion Bank"):
#   checkout_no_leak           -> applies to ALL three profiles            (3)
#   settings_graceful_failure  -> broken_token_service + crashed_telemetry (2)
# => 5 attempted pairs total (Property 4 / Requirement 9.2).
EXPECTED_PAIRS = {
    ("broken_token_service", "checkout_no_leak"),
    ("broken_token_service", "settings_graceful_failure"),
    ("crashed_telemetry", "checkout_no_leak"),
    ("crashed_telemetry", "settings_graceful_failure"),
    ("compromised_input_handler", "checkout_no_leak"),
}

# A clean Kane log for a graceful pass — must NOT trip any DEFAULT_LEAK_PATTERN
# (no stack trace, file path, SQL, env-var assignment, or secret key).
_CLEAN_LOGS = "checkout completed successfully; no raw system objects shown"

# A leaky Kane log: a Node-style unhandled error + stack frame with a deep file
# path — trips the stack_trace, node_stack, and file_path leak patterns so the
# real RatchetReporter's pattern scan is exercised alongside the FAILED status.
_LEAKY_LOGS = (
    "Unhandled Error: token verification failed\n"
    "    at verify (/srv/app/node_modules/next-auth/index.js:42:13)\n"
)


# --------------------------------------------------------------------------
# Fakes / seams (kept local to the test module)
# --------------------------------------------------------------------------


class FakeProcess:
    """Minimal stand-in for a spawned subprocess.

    Exposes the surface :class:`AppRunner` relies on: ``pid``, ``poll()``
    (``None`` while alive, exit code once dead), and ``wait()``. The injected
    ``terminate`` seam flips it to "dead" so process-tree teardown is modelled
    without any real OS process.
    """

    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self._returncode: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self._returncode

    def wait(self, timeout: float | None = None) -> int:
        return 0 if self._returncode is None else self._returncode

    def mark_dead(self) -> None:
        self.terminated = True
        self._returncode = 0


class FakeKaneRunner:
    """A canned Kane runner: ``run(assertion, base_url) -> KaneResult``.

    Returns deterministic, profile-agnostic results keyed by assertion id so the
    orchestrator wiring (which stamps the profile name onto each finding) is what
    we actually exercise:

    * ``checkout_no_leak`` -> ``PASSED`` + clean logs  => verdict ``SAFE``.
    * ``settings_graceful_failure`` -> ``FAILED`` + leaky logs
      => verdict ``LEAK_DETECTED`` (via Kane's failed judgement *and* the leak
      patterns, exercising both reporter paths).

    For the interrupt scenario it can raise :class:`KeyboardInterrupt` on the
    Nth call to simulate Ctrl-C arriving mid-run while a mutation is active.
    """

    def __init__(self, output_base: Path, *, raise_on_call: int | None = None) -> None:
        self._output_base = output_base
        self._raise_on_call = raise_on_call
        self.calls: list[tuple[str, str]] = []

    def run(self, assertion: SecurityAssertion, base_url: str) -> KaneResult:
        self.calls.append((assertion.id, base_url))
        if self._raise_on_call is not None and len(self.calls) == self._raise_on_call:
            # Simulate SIGINT arriving mid-mutation (design "Scenario 3").
            raise KeyboardInterrupt("simulated mid-run interrupt")

        output_dir = self._output_base / f"output-{assertion.id}"
        if assertion.id == "settings_graceful_failure":
            return KaneResult(
                status=StepStatus.FAILED,
                steps=[],
                console_logs=_LEAKY_LOGS,
                output_dir=output_dir,
                raw_result_md="---\nstatus: failed\n---\n",
            )
        return KaneResult(
            status=StepStatus.PASSED,
            steps=[],
            console_logs=_CLEAN_LOGS,
            output_dir=output_dir,
            raw_result_md="---\nstatus: passed\n---\n",
        )


def _make_clock():
    """Return a monotonically increasing fake clock so polling never stalls."""
    state = {"t": 0.0}

    def monotonic() -> float:
        state["t"] += 0.01
        return state["t"]

    return monotonic


def build_app_runner(config, logs_dir: Path) -> AppRunner:
    """Construct the REAL AppRunner with every external seam stubbed offline.

    No real subprocess is spawned, no port is bound, and no HTTP request is made:
    install succeeds, the app is "ready" immediately, the port is free, and
    teardown marks the fake process dead.
    """

    def install_runner(cmd, cwd):
        return (0, "")  # install always succeeds

    def spawn(cmd, cwd, log_fh):
        return FakeProcess()  # a live fake process

    def http_probe(url):
        return 200  # ready on the first poll

    def terminate(proc):
        proc.mark_dead()  # clean teardown, no real signals

    def port_probe(host, port):
        return False  # port is free

    return AppRunner(
        config.target_dir,
        config.start_cmd,
        config.install_cmd,
        config.port,
        readiness_path=config.readiness_path,
        boot_timeout_s=config.boot_timeout_s,
        spawn=spawn,
        install_runner=install_runner,
        http_probe=http_probe,
        terminate=terminate,
        port_probe=port_probe,
        sleep=lambda _s: None,
        monotonic=_make_clock(),
        logs_dir=logs_dir,
    )


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _restore_signal_handlers():
    """Save/restore SIGINT/SIGTERM around each test.

    ``Orchestrator.run`` installs interrupt handlers on the main thread; restore
    pytest's originals afterward so signal state never leaks between tests.
    """
    saved = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for sig, handler in saved.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError, TypeError):
                pass


@pytest.fixture
def sandbox_target(tmp_path: Path) -> Path:
    """A small sandbox target dir with a real-ish package.json + lockfile.

    Declares the three default profiles' target packages (``next-auth``,
    ``analytics``, ``validator``) so all profiles have a real dependency to
    mutate.
    """
    target = tmp_path / "sandbox-shop"
    target.mkdir()

    package_json = {
        "name": "sandbox-shop",
        "version": "1.0.0",
        "private": True,
        "scripts": {"start": "node server.js"},
        "dependencies": {
            "next-auth": "^4.24.5",
            "analytics": "^0.8.1",
            "validator": "^13.11.0",
        },
    }
    (target / "package.json").write_text(
        json.dumps(package_json, indent=2) + "\n", encoding="utf-8"
    )
    (target / "package-lock.json").write_text(
        json.dumps(
            {
                "name": "sandbox-shop",
                "version": "1.0.0",
                "lockfileVersion": 3,
                "requires": True,
                "packages": {},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return target


@dataclass
class DryRunResult:
    """Everything a test needs to assert about one orchestrator run."""

    report: object
    target_dir: Path
    output_dir: Path
    original_bytes: dict[Path, bytes]
    fake_kane: FakeKaneRunner


def _build_engine(sandbox_target: Path, tmp_path: Path, *, raise_on_call=None):
    """Wire a fully-offline Orchestrator + the FakeKaneRunner used to drive it."""
    config = build_default_config(sandbox_target, port=3000)
    # Mirror real CLI usage: fail-fast validation before any mutation.
    validate_config(config)

    output_dir = tmp_path / "out"
    fake_kane = FakeKaneRunner(tmp_path / "kane", raise_on_call=raise_on_call)

    orchestrator = Orchestrator(
        config=config,
        mutator=ChaosMutator(config.target_dir, index_profiles(config.profiles)),
        runner=build_app_runner(config, tmp_path / "applogs"),
        kane=fake_kane,
        reporter=RatchetReporter(DEFAULT_LEAK_PATTERNS, output_dir=output_dir),
        guard=BaselineGuard(config.target_dir, config.tracked_files),
    )
    return orchestrator, config, output_dir, fake_kane


def _capture_tracked_bytes(config) -> dict[Path, bytes]:
    """Read the raw bytes of every tracked file before the run (for restore)."""
    originals: dict[Path, bytes] = {}
    for rel in config.tracked_files:
        path = config.target_dir / rel
        originals[path] = path.read_bytes()
    return originals


@pytest.fixture
def dry_run(sandbox_target: Path, tmp_path: Path) -> DryRunResult:
    """Run the full dry-run loop once and return everything needed to assert."""
    orchestrator, config, output_dir, fake_kane = _build_engine(
        sandbox_target, tmp_path
    )
    original_bytes = _capture_tracked_bytes(config)

    report = orchestrator.run()

    return DryRunResult(
        report=report,
        target_dir=config.target_dir,
        output_dir=output_dir,
        original_bytes=original_bytes,
        fake_kane=fake_kane,
    )


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


def test_full_run_has_exactly_one_finding_per_pair(dry_run: DryRunResult):
    """Property 4 — complete coverage (Requirements 1.2, 1.4, 9.2).

    The report holds exactly one finding per attempted (profile, assertion)
    pair: 5 findings, no pair dropped or duplicated.
    """
    findings = dry_run.report.findings
    pairs = [(f.profile_name, f.assertion_id) for f in findings]

    # The fake Kane was driven once per attempted pair.
    assert len(dry_run.fake_kane.calls) == 5

    assert len(findings) == 5
    assert len(pairs) == len(set(pairs)), "a (profile, assertion) pair appeared twice"
    assert set(pairs) == EXPECTED_PAIRS


def test_full_run_restores_target_byte_for_byte(dry_run: DryRunResult):
    """Property 1 — restore safety (Requirements 10.1, 10.2, 10.3).

    After a complete run every tracked file equals its pre-run bytes and the
    injected ``.fileak_mocks/`` folder is gone.
    """
    for path, original in dry_run.original_bytes.items():
        assert path.read_bytes() == original, f"{path} not restored byte-for-byte"

    assert not (dry_run.target_dir / ".fileak_mocks").exists()


def test_full_run_exit_code_and_reports_written(dry_run: DryRunResult):
    """Exit-code fidelity + report emission (Requirements 9.1, 9.2, 9.3).

    ``settings_graceful_failure`` yields two ``LEAK_DETECTED`` findings, so the
    run exits 1, and both report artifacts are written to the output dir.
    """
    report = dry_run.report

    verdicts = [f.verdict for f in report.findings]
    leaks = [f for f in report.findings if f.verdict == Verdict.LEAK_DETECTED]
    safes = [f for f in report.findings if f.verdict == Verdict.SAFE]

    # Inverted semantics: failed/leaky -> LEAK_DETECTED; clean pass -> SAFE.
    assert len(leaks) == 2
    assert len(safes) == 3
    assert all(f.assertion_id == "settings_graceful_failure" for f in leaks)
    # The leaky logs tripped the real reporter's pattern scan, too.
    assert all(f.leak_indicators for f in leaks)
    assert Verdict.INCONCLUSIVE not in verdicts

    assert report.exit_code == 1
    assert report.leaks_found == 2

    json_path = dry_run.output_dir / "run_report.json"
    md_path = dry_run.output_dir / "report.md"
    assert json_path.is_file()
    assert md_path.is_file()

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["exit_code"] == 1
    assert payload["leaks_found"] == 2
    assert len(payload["findings"]) == 5


def test_mid_run_interrupt_restores_baseline(sandbox_target: Path, tmp_path: Path):
    """Restore safety under interrupt (Requirements 10.2, 10.3, 10.5 / Property 1).

    A ``KeyboardInterrupt`` raised during the 2nd profile's Kane run (while that
    profile's mutation is active) must propagate out of ``run()`` while its
    nested ``finally`` blocks still stop the app, revert the mutation, and
    restore the target byte-for-byte with ``.fileak_mocks/`` absent.
    """
    orchestrator, config, _output_dir, fake_kane = _build_engine(
        sandbox_target, tmp_path, raise_on_call=3
    )
    original_bytes = _capture_tracked_bytes(config)

    with pytest.raises(KeyboardInterrupt):
        orchestrator.run()

    # The interrupt fired mid-run: only the first three assertions were attempted
    # (profile 1's two assertions + profile 2's first), confirming a mutation was
    # active when the interrupt arrived.
    assert len(fake_kane.calls) == 3

    # The outer/per-profile finally blocks still restored everything.
    for path, original in original_bytes.items():
        assert path.read_bytes() == original, f"{path} not restored after interrupt"
    assert not (config.target_dir / ".fileak_mocks").exists()
