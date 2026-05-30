"""Property-based test for exit-code fidelity (task 8.4).

The engine's process exit code is the single bit a CI pipeline keys off of: a
run that detected at least one leak must exit non-zero, and a run that found
none must exit zero. That contract lives in two places that must agree:

* :attr:`fileak.models.RunReport.exit_code` returns ``1`` iff
  ``leaks_found > 0`` (the raw property), and
* :meth:`fileak.reporter.RatchetReporter.finalize` sets ``leaks_found`` to the
  number of ``LEAK_DETECTED`` findings, so the report it returns exits ``1``
  iff at least one finding is ``LEAK_DETECTED`` (the wired-up behavior,
  Requirements 9.2, 9.3).

**Property 6 — Exit-code fidelity.** For a generated list of findings,
``RunReport.exit_code == 1`` iff at least one finding has verdict
``LEAK_DETECTED``.

Validates: Requirements 9.3

Oracle strategy
---------------
The expected answer is computed independently of the production code by simply
asking the generated finding list "does ANY finding have verdict
``LEAK_DETECTED``?" (``any(f.verdict == Verdict.LEAK_DETECTED ...)``). That
boolean is the ground truth the exit code must mirror.

Two complementary properties keep both layers honest:

1. **RunReport level (no disk).** Build a :class:`RunReport` directly, setting
   ``leaks_found`` exactly the way ``finalize`` does (count of
   ``LEAK_DETECTED``), and assert ``exit_code == 1`` iff any finding leaked.
   This isolates the pure ``exit_code`` arithmetic and needs no reporter/disk.

2. **Through finalize (temp output dir).** Run the real
   ``RatchetReporter.finalize`` so the count-and-exit wiring is exercised
   end-to-end, asserting both ``leaks_found`` equals the number of
   ``LEAK_DETECTED`` findings AND the returned report's ``exit_code`` matches
   the oracle. ``finalize`` WRITES ``run_report.json`` + ``report.md``, so each
   Hypothesis example points the reporter at a FRESH
   :class:`tempfile.TemporaryDirectory` that is cleaned up immediately — keeping
   the property fast and isolated despite touching disk.

The Finding generator draws verdicts from the full :class:`Verdict` enum and
``kane_status`` from the full :class:`StepStatus` enum, with simple ids, and the
finding-list length includes the EMPTY list (which must yield ``exit_code ==
0``). Mixing all three verdicts means zero, one, and many ``LEAK_DETECTED``
findings are all reachable.

Framework: Hypothesis (per design "Property Test Library: Hypothesis (Python)").
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from fileak.models import (
    Finding,
    RunReport,
    StepStatus,
    Verdict,
)
from fileak.reporter import DEFAULT_LEAK_PATTERNS, RatchetReporter

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


@st.composite
def findings(draw: st.DrawFn) -> Finding:
    """Generate a :class:`Finding` with an arbitrary verdict and kane status.

    The verdict is drawn from the full :class:`Verdict` enum (so ``SAFE``,
    ``LEAK_DETECTED``, and ``INCONCLUSIVE`` are all reachable) and the
    ``kane_status`` from the full :class:`StepStatus` enum. Profile/assertion
    ids are kept simple — exit-code fidelity depends only on the verdicts, not
    on the ids or evidence — so the rest of the finding is left at its
    dataclass defaults.
    """
    verdict = draw(st.sampled_from(list(Verdict)))
    kane_status = draw(st.sampled_from(list(StepStatus)))
    profile_name = draw(
        st.sampled_from(
            ["broken_token_service", "crashed_telemetry", "compromised_input_handler"]
        )
    )
    assertion_id = draw(
        st.sampled_from(["checkout_no_leak", "settings_graceful_failure"])
    )
    return Finding(
        profile_name=profile_name,
        assertion_id=assertion_id,
        verdict=verdict,
        kane_status=kane_status,
    )


# A list of findings, INCLUDING the empty list (min_size=0). The empty list is
# the boundary case that must produce exit_code 0.
_findings_list = st.lists(findings(), min_size=0, max_size=12)


def _any_leak(finding_list: list[Finding]) -> bool:
    """Independent oracle: does ANY finding have verdict ``LEAK_DETECTED``?"""
    return any(f.verdict == Verdict.LEAK_DETECTED for f in finding_list)


def _count_leaks(finding_list: list[Finding]) -> int:
    """Independent oracle: number of ``LEAK_DETECTED`` findings."""
    return sum(1 for f in finding_list if f.verdict == Verdict.LEAK_DETECTED)


# ---------------------------------------------------------------------------
# Property 1: RunReport.exit_code arithmetic (no disk)
# ---------------------------------------------------------------------------


@settings(deadline=None, max_examples=300)
@given(finding_list=_findings_list)
def test_exit_code_fidelity_runreport_level(finding_list):
    """Property 6: ``RunReport.exit_code == 1`` iff any finding leaked.

    Builds a :class:`RunReport` directly, computing ``leaks_found`` the way
    ``finalize`` does (count of ``LEAK_DETECTED`` findings), then asserts the
    derived ``exit_code`` is ``1`` exactly when at least one finding has verdict
    ``LEAK_DETECTED`` and ``0`` otherwise — including the empty-list boundary.

    Validates: Requirements 9.3
    """
    leaks_found = _count_leaks(finding_list)
    report = RunReport(
        findings=finding_list,
        profiles_run=[],
        leaks_found=leaks_found,
        started_at="2026-05-30T18:04:35+00:00",
        duration_s=0.0,
    )

    expected_leak = _any_leak(finding_list)

    # The exit code is 1 iff at least one finding leaked, 0 otherwise.
    assert report.exit_code == (1 if expected_leak else 0)
    # Equivalently, exit_code is non-zero iff leaks_found is non-zero.
    assert (report.exit_code == 1) == (report.leaks_found > 0)
    assert (report.exit_code == 1) == expected_leak


# ---------------------------------------------------------------------------
# Property 2: Through RatchetReporter.finalize (temp output dir, touches disk)
# ---------------------------------------------------------------------------


@settings(deadline=None, max_examples=100)
@given(finding_list=_findings_list)
def test_exit_code_fidelity_through_finalize(finding_list):
    """Property 6: ``finalize`` wires ``leaks_found`` so the exit code is sound.

    Runs the real :meth:`RatchetReporter.finalize` (which counts
    ``LEAK_DETECTED`` findings and writes the reports) and asserts the returned
    report's ``leaks_found`` equals the number of ``LEAK_DETECTED`` findings and
    its ``exit_code`` is ``1`` iff at least one finding leaked (Requirements
    9.2, 9.3). A fresh temp dir per example isolates the report writes.

    Validates: Requirements 9.3
    """
    expected_leak = _any_leak(finding_list)
    expected_count = _count_leaks(finding_list)

    # finalize() WRITES run_report.json + report.md; use a fresh temp dir per
    # example so writes are isolated and cleaned up (keeps the property fast).
    with tempfile.TemporaryDirectory() as tmp:
        output_dir = Path(tmp)
        reporter = RatchetReporter(DEFAULT_LEAK_PATTERNS, output_dir=output_dir)
        report = reporter.finalize(finding_list)

        # finalize aggregates exactly the findings it is given (no drop/synth).
        assert report.leaks_found == expected_count
        # Exit-code fidelity: 1 iff at least one LEAK_DETECTED finding.
        assert report.exit_code == (1 if expected_leak else 0)

        # The written JSON must agree with the in-memory report's exit code.
        written = json.loads(
            (output_dir / "run_report.json").read_text(encoding="utf-8")
        )
        assert written["exit_code"] == report.exit_code
        assert written["leaks_found"] == expected_count


# ---------------------------------------------------------------------------
# Targeted example-based tests (boundary intents)
# ---------------------------------------------------------------------------


def _finding(verdict: Verdict) -> Finding:
    return Finding(
        profile_name="broken_token_service",
        assertion_id="checkout_no_leak",
        verdict=verdict,
        kane_status=StepStatus.PASSED,
    )


def test_empty_findings_exit_code_zero():
    """No findings -> no leaks -> exit code 0 (the empty-list boundary).

    Validates: Requirements 9.3
    """
    with tempfile.TemporaryDirectory() as tmp:
        reporter = RatchetReporter(DEFAULT_LEAK_PATTERNS, output_dir=Path(tmp))
        report = reporter.finalize([])
        assert report.leaks_found == 0
        assert report.exit_code == 0


def test_only_safe_and_inconclusive_exit_code_zero():
    """SAFE + INCONCLUSIVE findings but no leak -> exit code 0.

    Validates: Requirements 9.3
    """
    with tempfile.TemporaryDirectory() as tmp:
        reporter = RatchetReporter(DEFAULT_LEAK_PATTERNS, output_dir=Path(tmp))
        report = reporter.finalize(
            [_finding(Verdict.SAFE), _finding(Verdict.INCONCLUSIVE)]
        )
        assert report.leaks_found == 0
        assert report.exit_code == 0


def test_single_leak_among_safe_exit_code_one():
    """A single LEAK_DETECTED among SAFE/INCONCLUSIVE -> exit code 1.

    Validates: Requirements 9.3
    """
    with tempfile.TemporaryDirectory() as tmp:
        reporter = RatchetReporter(DEFAULT_LEAK_PATTERNS, output_dir=Path(tmp))
        report = reporter.finalize(
            [
                _finding(Verdict.SAFE),
                _finding(Verdict.LEAK_DETECTED),
                _finding(Verdict.INCONCLUSIVE),
            ]
        )
        assert report.leaks_found == 1
        assert report.exit_code == 1


def test_multiple_leaks_exit_code_one():
    """Many LEAK_DETECTED findings -> leaks_found counts them, exit code 1.

    Validates: Requirements 9.3
    """
    with tempfile.TemporaryDirectory() as tmp:
        reporter = RatchetReporter(DEFAULT_LEAK_PATTERNS, output_dir=Path(tmp))
        report = reporter.finalize(
            [_finding(Verdict.LEAK_DETECTED) for _ in range(3)]
        )
        assert report.leaks_found == 3
        assert report.exit_code == 1
