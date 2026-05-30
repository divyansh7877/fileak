"""Property-based test for Orchestrator complete coverage (task 10.3).

The orchestrator's defining guarantee is that it leaves *no experiment behind*:
for a generated experiment matrix (chaos profiles × the assertions applicable to
each) and an arbitrary set of profiles that fail to boot, the aggregate
``RunReport`` must account for every attempted pair exactly once — one
:class:`~fileak.models.Finding` per (booted profile, applicable assertion) pair,
plus exactly one ``INCONCLUSIVE`` finding per boot-failed profile (and *none* of
that profile's assertion pairs, since it never reached the assertion loop).

**Property 4 — Complete coverage.** Using a fake Kane runner, the report
contains exactly one finding per attempted (profile, assertion) pair plus one
``INCONCLUSIVE`` per boot failure, with no pair dropped or duplicated.

Validates: Requirements 1.2, 1.4, 9.2

Offline / deterministic design
------------------------------
This drives the REAL :class:`~fileak.orchestrator.Orchestrator` against the real
safety-critical components (:class:`~fileak.chaos.ChaosMutator`,
:class:`~fileak.baseline.BaselineGuard`, :class:`~fileak.reporter.RatchetReporter`)
and the REAL :class:`~fileak.app_runner.AppRunner` with every externally-uncertain
seam stubbed (no real process, network, npm, or Kane binary), mirroring the
offline approach in ``tests/test_integration_dry_run.py``.

Per-profile boot failure mechanism
----------------------------------
The orchestrator calls ``mutator.inject(profile_name)`` immediately before
``runner.install()`` / ``runner.start()``. We wrap the real ``ChaosMutator`` in a
:class:`RecordingMutator` whose ``inject`` records the *active* profile name into
a shared mutable ``box`` (and still delegates to the real inject so revert /
restore stay honest). The shared ``AppRunner`` then drives boot failure purely
from that box: its injected ``spawn`` seam returns a process whose ``poll()``
already reports an exit code when the active profile is in the boot-failure set,
so ``AppRunner.start`` aborts readiness polling immediately and raises
:class:`~fileak.models.BootError` (its "process exited during polling" path) —
exactly the install/boot failure the orchestrator turns into one ``INCONCLUSIVE``
finding. For booting profiles the spawned process is "alive" and ``http_probe``
answers 200 on the first poll, so the app is ready immediately. The same box lets
the fake Kane runner record which profile each ``run`` call belonged to (its
signature only receives the assertion + base_url), so we can assert Kane was
invoked exactly once per booted pair and never for a boot-failed profile.

Framework: Hypothesis (per design "Property Test Library: Hypothesis (Python)").
"""

from __future__ import annotations

import json
import signal
import tempfile
from collections import Counter
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from fileak.app_runner import AppRunner
from fileak.baseline import BaselineGuard
from fileak.chaos import ChaosMutator
from fileak.config import index_profiles, validate_config
from fileak.models import (
    ChaosProfile,
    EngineConfig,
    KaneResult,
    MockBehavior,
    SecurityAssertion,
    StepStatus,
    Verdict,
)
from fileak.orchestrator import BOOT_FAILURE_ASSERTION_ID, Orchestrator
from fileak.reporter import DEFAULT_LEAK_PATTERNS, RatchetReporter

# A clean Kane log for a graceful pass — must NOT trip any DEFAULT_LEAK_PATTERN
# (no stack trace, file path, SQL, env-var assignment, or secret key), so a
# booted (profile, assertion) pair always resolves to a non-leak SAFE verdict.
_CLEAN_LOGS = "checkout completed successfully; no raw system objects shown"
_CLEAN_RESULT_MD = "---\nstatus: passed\n---\n"


# --------------------------------------------------------------------------
# Fakes / seams (kept local to the test module)
# --------------------------------------------------------------------------


class FakeProcess:
    """Minimal stand-in for a spawned subprocess.

    Exposes the surface :class:`AppRunner` relies on: ``pid``, ``poll()`` and
    ``wait()``. When constructed with ``dead=True`` its ``poll()`` returns a
    non-``None`` exit code from the very first call, so ``AppRunner.start``
    treats it as "exited during readiness polling" and raises ``BootError`` —
    this is how we model a per-profile boot failure with no real process.
    """

    def __init__(self, pid: int = 4242, *, dead: bool = False) -> None:
        self.pid = pid
        self._returncode: int | None = 1 if dead else None
        self.terminated = False

    def poll(self) -> int | None:
        return self._returncode

    def wait(self, timeout: float | None = None) -> int:
        return 0 if self._returncode is None else self._returncode

    def mark_dead(self) -> None:
        self.terminated = True
        if self._returncode is None:
            self._returncode = 0


class RecordingMutator:
    """Delegate to a real :class:`ChaosMutator`, recording the active profile.

    ``inject`` writes the active profile name into the shared ``box`` *and*
    delegates to the real mutator so the actual ``package.json`` rewrite + mock
    materialization (and therefore the matching revert/restore) still happen.
    The box is what the ``AppRunner`` spawn seam and the fake Kane runner consult
    to know which profile is currently mutated.
    """

    def __init__(self, inner: ChaosMutator, box: dict) -> None:
        self._inner = inner
        self._box = box

    def inject(self, profile_name: str):
        self._box["active"] = profile_name
        return self._inner.inject(profile_name)

    def revert(self, record) -> None:
        self._inner.revert(record)
        self._box["active"] = None

    def list_profiles(self) -> list[str]:
        return self._inner.list_profiles()


class FakeKaneRunner:
    """A canned Kane runner: ``run(assertion, base_url) -> KaneResult``.

    Always returns ``PASSED`` + clean logs (so the reporter yields ``SAFE``), and
    records each call as ``(active_profile, assertion_id)`` by reading the shared
    ``box`` — its signature receives only the assertion + base_url, so the box is
    how it learns which profile the call belongs to.
    """

    def __init__(self, box: dict, output_base: Path) -> None:
        self._box = box
        self._output_base = output_base
        self.calls: list[tuple[str | None, str]] = []

    def run(self, assertion: SecurityAssertion, base_url: str) -> KaneResult:
        self.calls.append((self._box["active"], assertion.id))
        return KaneResult(
            status=StepStatus.PASSED,
            steps=[],
            console_logs=_CLEAN_LOGS,
            output_dir=self._output_base / f"output-{assertion.id}",
            raw_result_md=_CLEAN_RESULT_MD,
        )


def _make_clock():
    """Return a monotonically increasing fake clock so polling never stalls."""
    state = {"t": 0.0}

    def monotonic() -> float:
        state["t"] += 0.01
        return state["t"]

    return monotonic


def _build_app_runner(
    config: EngineConfig, logs_dir: Path, box: dict, boot_failures: set[str]
) -> AppRunner:
    """Construct the REAL AppRunner with offline seams + per-profile boot failure.

    ``spawn`` consults the shared ``box`` (set by :class:`RecordingMutator`): when
    the active profile is in ``boot_failures`` it returns an already-exited
    process so ``start`` raises ``BootError``; otherwise it returns a live process
    and ``http_probe`` reports ready on the first poll.
    """

    def install_runner(cmd, cwd):
        return (0, "")  # install always succeeds

    def spawn(cmd, cwd, log_fh):
        return FakeProcess(dead=box["active"] in boot_failures)

    def http_probe(url):
        return 200  # ready on the first poll (only reached for live processes)

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
# Strategy: an arbitrary experiment matrix + boot-failure set
# --------------------------------------------------------------------------


@st.composite
def coverage_scenarios(draw):
    """Generate ``(profiles, assertions, boot_failures)``.

    * 1..5 profiles, each named ``profile_<i>`` targeting a DISTINCT package
      ``pkg_<i>`` (distinct packages => injection always succeeds) with an
      arbitrary :class:`MockBehavior`.
    * 1..5 assertions named ``assertion_<j>`` whose ``applies_to`` is either
      empty (applies to ALL profiles) or an arbitrary non-empty subset of the
      profile names — making the (profile, assertion) matrix non-trivial.
    * ``boot_failures`` — an arbitrary (possibly empty) subset of profile names
      that should fail to boot.
    """
    n_profiles = draw(st.integers(min_value=1, max_value=5))
    n_assertions = draw(st.integers(min_value=1, max_value=5))

    profile_names = [f"profile_{i}" for i in range(n_profiles)]
    packages = [f"pkg_{i}" for i in range(n_profiles)]

    profiles = [
        ChaosProfile(
            name=profile_names[i],
            target_package=packages[i],
            behavior=draw(st.sampled_from(list(MockBehavior))),
            mock_template="coverage_template",
            description="complete-coverage property profile",
        )
        for i in range(n_profiles)
    ]

    assertions: list[SecurityAssertion] = []
    for j in range(n_assertions):
        if draw(st.booleans()):
            applies_to: list[str] = []  # empty => applies to all profiles
        else:
            applies_to = draw(
                st.lists(
                    st.sampled_from(profile_names),
                    min_size=1,
                    max_size=n_profiles,
                    unique=True,
                )
            )
        assertions.append(
            SecurityAssertion(
                id=f"assertion_{j}",
                prompt="inspect the UI for leaked system state",
                applies_to=applies_to,
            )
        )

    boot_failures = set(
        draw(
            st.lists(
                st.sampled_from(profile_names),
                max_size=n_profiles,
                unique=True,
            )
        )
    )
    return profiles, assertions, boot_failures


def _applicable(profile_name: str, assertions: list[SecurityAssertion]) -> list[str]:
    """Assertion ids applicable to ``profile_name`` (independent oracle).

    Mirrors the design's applicability rule directly (empty ``applies_to`` means
    "all profiles") rather than calling the model under test, so the expected
    coverage is computed independently of ``EngineConfig.assertions_for``.
    """
    return [
        a.id for a in assertions if not a.applies_to or profile_name in a.applies_to
    ]


def _write_sandbox(target: Path, packages: list[str]) -> None:
    """Write a real-ish sandbox ``package.json`` + lockfile declaring ``packages``."""
    target.mkdir(parents=True, exist_ok=True)
    package_json = {
        "name": "sandbox-shop",
        "version": "1.0.0",
        "private": True,
        "scripts": {"start": "node server.js"},
        "dependencies": {pkg: "^1.0.0" for pkg in packages},
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


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _restore_signal_handlers():
    """Save/restore SIGINT/SIGTERM around the test (belt-and-suspenders).

    ``Orchestrator.run`` installs interrupt handlers on the main thread; the
    per-example try/finally inside the test restores between Hypothesis
    examples, and this fixture restores pytest's originals after the test.
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


# --------------------------------------------------------------------------
# Property 4 — complete coverage
# --------------------------------------------------------------------------


@settings(deadline=None, max_examples=50)
@given(scenario=coverage_scenarios())
def test_report_has_exactly_one_finding_per_attempted_pair(scenario):
    """Property 4: complete coverage of the experiment matrix.

    For an arbitrary experiment matrix and boot-failure set, the report's
    findings (as a multiset of ``(profile, assertion_id)``) equal exactly the
    expected pairs: one per (booted profile, applicable assertion), plus one
    ``(profile, "_boot_failure")`` ``INCONCLUSIVE`` per boot-failed profile, with
    no pair dropped or duplicated and Kane invoked once per booted pair.

    Validates: Requirements 1.2, 1.4, 9.2
    """
    profiles, assertions, boot_failures = scenario

    # Fresh, isolated workspace per example (not a fixture) so Hypothesis
    # re-runs cleanly across examples — tmp_path can't be used per-example.
    with tempfile.TemporaryDirectory(prefix="fileak_coverage_prop_") as td:
        root = Path(td)
        target = root / "sandbox-shop"
        _write_sandbox(target, [p.target_package for p in profiles])

        config = EngineConfig(
            target_dir=target,
            start_cmd=["npm", "run", "start"],
            install_cmd=["npm", "install"],
            port=3000,
            readiness_path="/",
            boot_timeout_s=5.0,
            profiles=profiles,
            assertions=assertions,
            tracked_files=[Path("package.json"), Path("package-lock.json")],
        )
        # Mirror real CLI usage: fail-fast validation before any mutation. The
        # generated config is always valid (unique names, distinct present
        # packages, assertions referencing defined profiles).
        validate_config(config)

        box: dict = {"active": None}
        output_dir = root / "out"
        fake_kane = FakeKaneRunner(box, root / "kane")

        orchestrator = Orchestrator(
            config=config,
            mutator=RecordingMutator(
                ChaosMutator(config.target_dir, index_profiles(config.profiles)),
                box,
            ),
            runner=_build_app_runner(config, root / "applogs", box, boot_failures),
            kane=fake_kane,
            reporter=RatchetReporter(DEFAULT_LEAK_PATTERNS, output_dir=output_dir),
            guard=BaselineGuard(config.target_dir, config.tracked_files),
        )

        # --- Compute the EXPECTED coverage independently. --------------------
        expected_pairs: list[tuple[str, str]] = []
        expected_kane_calls: list[tuple[str, str]] = []
        for profile in profiles:
            if profile.name in boot_failures:
                # Boot failure => exactly one INCONCLUSIVE finding, no assertion
                # pairs, and Kane is never invoked for this profile.
                expected_pairs.append((profile.name, BOOT_FAILURE_ASSERTION_ID))
            else:
                for assertion_id in _applicable(profile.name, assertions):
                    expected_pairs.append((profile.name, assertion_id))
                    expected_kane_calls.append((profile.name, assertion_id))

        # --- Run, restoring signal handlers around each example's run(). -----
        saved = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
        try:
            report = orchestrator.run()
        finally:
            for sig, handler in saved.items():
                try:
                    signal.signal(sig, handler)
                except (ValueError, OSError, TypeError):
                    pass

        # --- Assert Property 4. ----------------------------------------------
        actual_pairs = [(f.profile_name, f.assertion_id) for f in report.findings]

        # The findings multiset equals the expected multiset exactly: no pair
        # dropped, none duplicated (Requirements 1.2, 1.4, 9.2).
        assert Counter(actual_pairs) == Counter(expected_pairs)

        # Exactly one INCONCLUSIVE finding per boot-failed profile, each stamped
        # with the synthetic boot-failure assertion id, covering exactly the
        # boot-failed profiles.
        inconclusive = [f for f in report.findings if f.verdict == Verdict.INCONCLUSIVE]
        assert len(inconclusive) == len(boot_failures)
        assert all(f.assertion_id == BOOT_FAILURE_ASSERTION_ID for f in inconclusive)
        assert {f.profile_name for f in inconclusive} == boot_failures

        # Each booted (profile, assertion) pair appears exactly once (no dup).
        booted_actual = [
            p for p in actual_pairs if p[1] != BOOT_FAILURE_ASSERTION_ID
        ]
        assert len(booted_actual) == len(set(booted_actual))

        # Kane was invoked exactly once per booted pair and zero times for any
        # boot-failed profile's assertions.
        assert Counter(fake_kane.calls) == Counter(expected_kane_calls)
        assert all(name not in boot_failures for name, _ in fake_kane.calls)

        # Bonus restore-safety check: the injected mock folder is gone.
        assert not (config.target_dir / ".fileak_mocks").exists()
