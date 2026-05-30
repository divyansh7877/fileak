"""Property-based test for RatchetReporter verdict soundness (task 8.2).

The ``RatchetReporter`` is the engine's *inverted* verdict oracle: a Kane
assertion that "passes" (no leak visible) is GOOD, while a Kane assertion that
"fails", or that surfaces raw leak indicators in the captured logs/output, is a
DETECTED vulnerability. ``evaluate`` must collapse three inputs — Kane's overall
``StepStatus`` and the combined captured text (Kane console logs + app stderr +
``Result.md`` body) — into exactly one :class:`~fileak.models.Finding` with a
sound :class:`~fileak.models.Verdict`.

**Property 5 — Verdict soundness.** A finding is ``LEAK_DETECTED`` iff Kane
failed the assertion OR at least one leak pattern matched the captured
logs/output; ``INCONCLUSIVE`` iff Kane errored; ``SAFE`` otherwise.

Validates: Requirements 7.1, 7.2, 7.3, 7.4, 8.1, 8.3

Oracle strategy
---------------
Rather than trying to *guarantee* that generated "clean" text never matches any
of the six :data:`DEFAULT_LEAK_PATTERNS` (which is fragile for arbitrary
unicode), the property computes its expected answer by **independently
re-scanning the exact same combined haystack with the exact same patterns**.
This keeps the property robust for ARBITRARY generated text (``st.text()``)
while still being a genuinely independent reimplementation of the verdict logic:

* The oracle rebuilds the haystack the way ``evaluate`` does — ``console_logs +
  "\\n" + app_stderr + "\\n" + raw_result_md`` with ``None``/missing fields
  degrading to ``""`` (Requirements 8.1, 8.5) — and decides ``any_match`` via
  ``re.search`` over :data:`DEFAULT_LEAK_PATTERNS` (Requirement 8.3).
* The verdict oracle is a separate hand-written mapping:
  ``ERROR -> INCONCLUSIVE``; else ``FAILED or any_match -> LEAK_DETECTED``; else
  ``SAFE`` (Requirements 7.2, 7.3, 7.4).

The component strategy mixes ``None`` (graceful-degradation path, Req 8.5),
provably-clean lowercase text, broad ``st.text()`` (unicode/control chars), and
injected known-leak fragments so BOTH the leak and no-leak branches are
exercised across all four ``StepStatus`` values.

A handful of targeted example-based tests pin the four corner intents:
ERROR+leaky -> INCONCLUSIVE, PASSED+clean -> SAFE, PASSED+leaky -> LEAK_DETECTED
(leak despite a pass), and FAILED+clean -> LEAK_DETECTED (failed assertion).

Framework: Hypothesis (per design "Property Test Library: Hypothesis (Python)").
"""

from __future__ import annotations

import re
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from fileak.models import (
    Finding,
    KaneResult,
    SecurityAssertion,
    StepStatus,
    Verdict,
)
from fileak.reporter import DEFAULT_LEAK_PATTERNS, RatchetReporter

# ---------------------------------------------------------------------------
# Shared fixtures / oracle helpers
# ---------------------------------------------------------------------------

# Compile the SAME default patterns the reporter uses so the oracle's notion of
# "a leak matched" is byte-identical to the reporter's scan (Requirement 8.3).
_ORACLE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (lp.label, re.compile(lp.pattern)) for lp in DEFAULT_LEAK_PATTERNS
]

# A single reporter reused across examples. evaluate() is pure w.r.t. the
# reporter's state (only finalize() touches timing/disk), so reuse is safe and
# avoids recompiling the patterns on every Hypothesis example.
_REPORTER = RatchetReporter(DEFAULT_LEAK_PATTERNS)

# output_dir is passed straight through to Finding.output_dir and never touched
# by evaluate(), so any placeholder path works (no disk writes occur here).
_OUTPUT_DIR = Path("unused_output_dir")

_ASSERTION = SecurityAssertion(
    id="checkout_no_stacktrace",
    prompt="Confirm no stack traces, paths, or secrets are visible.",
    applies_to=[],
)
_PROFILE_NAME = "broken_token_service"


def _build_haystack(
    console_logs: str | None,
    app_stderr: str | None,
    raw_result_md: str | None,
) -> str:
    """Reproduce ``RatchetReporter.evaluate``'s haystack EXACTLY.

    Mirrors ``(kane.console_logs or "") + "\\n" + (app_stderr or "") + "\\n" +
    (kane.raw_result_md or "")`` so the oracle scans precisely the text the
    reporter scans, including the graceful ``None -> ""`` degradation
    (Requirements 8.1, 8.5).
    """
    cl = console_logs or ""
    se = app_stderr or ""
    md = raw_result_md or ""
    return cl + "\n" + se + "\n" + md


def _oracle_labels(haystack: str) -> list[str]:
    """Deduped matched labels in pattern-declaration order (reporter behavior).

    The reporter appends a label for every match grouped by pattern, then
    dedupes preserving first-seen order; that is equivalent to "label of each
    pattern that matches at least once, in declaration order", which is what
    this computes via ``re.search`` (Requirement 8.3).
    """
    return [label for label, regex in _ORACLE_PATTERNS if regex.search(haystack)]


def _expected_verdict(status: StepStatus, any_match: bool) -> Verdict:
    """Independent reimplementation of the inverted verdict mapping.

    Requirements 7.2 (ERROR -> INCONCLUSIVE), 7.3 (FAILED or any leak ->
    LEAK_DETECTED), 7.4 (otherwise SAFE).
    """
    if status == StepStatus.ERROR:
        return Verdict.INCONCLUSIVE
    if status == StepStatus.FAILED or any_match:
        return Verdict.LEAK_DETECTED
    return Verdict.SAFE


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Known leak fragments, each crafted to match at least one DEFAULT_LEAK_PATTERN.
# (Some intentionally match more than one — the oracle accounts for that.)
_LEAKY_FRAGMENTS = [
    "at handler (/srv/app/server.js:42:13)",        # stack_trace (+ file_path)
    "Error: connection refused by upstream",         # node_stack
    "TypeError: cannot read property of undefined",   # node_stack
    "/usr/local/lib/node_modules/pkg/index.js",       # file_path
    "SELECT id, email FROM users WHERE id = 1",       # sql_query
    "API_KEY=sk_live_abcdef0123456789",               # env_var (+ secret_key)
    "password: hunter2",                              # secret_key
    "token = abc.def.ghi",                            # secret_key
]

# Provably-clean text: lowercase letters + spaces only. None of the six default
# patterns can match this alphabet (no capitals/digits/'/'/':'/'='/'(').
_clean_text = st.text(alphabet="abcdefghijklmnopqrstuvwxyz ", max_size=80)

# A haystack component: None (degradation path), clean text, broad unicode text,
# a known leak fragment, or clean text concatenated with a leak fragment so both
# verdict branches are reliably hit.
_component = st.one_of(
    st.none(),
    _clean_text,
    st.text(max_size=80),
    st.sampled_from(_LEAKY_FRAGMENTS),
    st.tuples(_clean_text, st.sampled_from(_LEAKY_FRAGMENTS)).map(
        lambda parts: parts[0] + " " + parts[1]
    ),
)


# ---------------------------------------------------------------------------
# Property test
# ---------------------------------------------------------------------------


@settings(deadline=None, max_examples=400)
@given(
    status=st.sampled_from(list(StepStatus)),
    console_logs=_component,
    app_stderr=_component,
    raw_result_md=_component,
)
def test_verdict_soundness(status, console_logs, app_stderr, raw_result_md):
    """Property 5: the verdict matches the inverted mapping for arbitrary text.

    For every ``StepStatus`` and arbitrary combination of console logs, app
    stderr, and ``Result.md`` body, ``evaluate`` returns exactly one Finding
    (Req 7.1) whose verdict is ``INCONCLUSIVE`` iff Kane errored (Req 7.2),
    ``LEAK_DETECTED`` iff Kane failed OR a leak pattern matched the combined
    haystack (Reqs 7.3, 8.1, 8.3), and ``SAFE`` otherwise (Req 7.4). The
    leak indicators reflect exactly the matching pattern labels regardless of
    status (the reporter scans independently of the Kane status).

    Validates: Requirements 7.1, 7.2, 7.3, 7.4, 8.1, 8.3
    """
    kane = KaneResult(
        status=status,
        steps=[],
        console_logs=console_logs,        # may be None -> exercises Req 8.5
        output_dir=_OUTPUT_DIR,
        raw_result_md=raw_result_md,      # may be None -> exercises Req 8.5
    )

    finding = _REPORTER.evaluate(_PROFILE_NAME, _ASSERTION, kane, app_stderr)

    # Independent oracle over the EXACT same haystack + patterns.
    haystack = _build_haystack(console_logs, app_stderr, raw_result_md)
    expected_labels = _oracle_labels(haystack)
    expected_verdict = _expected_verdict(status, any_match=bool(expected_labels))

    # Exactly one Finding, addressed to the right (profile, assertion) pair and
    # echoing Kane's status (Requirement 7.1).
    assert isinstance(finding, Finding)
    assert finding.profile_name == _PROFILE_NAME
    assert finding.assertion_id == _ASSERTION.id
    assert finding.kane_status == status

    # The verdict is sound (Requirements 7.2, 7.3, 7.4).
    assert finding.verdict == expected_verdict

    # Leak indicators correspond to the matching pattern labels from the same
    # scan the reporter performed (Requirements 8.1, 8.3) — recorded regardless
    # of Kane status.
    assert finding.leak_indicators == expected_labels


# ---------------------------------------------------------------------------
# Targeted example-based tests (the four corner intents)
# ---------------------------------------------------------------------------


def test_error_status_with_leaky_text_is_inconclusive():
    """ERROR dominates: even with obvious leaks the verdict is INCONCLUSIVE.

    Validates: Requirements 7.1, 7.2
    """
    kane = KaneResult(
        status=StepStatus.ERROR,
        steps=[],
        console_logs="Error: boom\nat fn (/a/b/c/d.js:1:2)",
        output_dir=_OUTPUT_DIR,
        raw_result_md="",
    )
    finding = _REPORTER.evaluate(_PROFILE_NAME, _ASSERTION, kane, "SELECT x FROM y")
    assert finding.verdict == Verdict.INCONCLUSIVE
    # Indicators are still recorded even when the verdict is inconclusive.
    assert finding.leak_indicators


def test_passed_status_with_clean_text_is_safe():
    """PASSED + clean graceful-failure text -> SAFE (no leak, Kane passed).

    Validates: Requirements 7.1, 7.4
    """
    kane = KaneResult(
        status=StepStatus.PASSED,
        steps=[],
        console_logs="the page rendered fine and showed a friendly message",
        output_dir=_OUTPUT_DIR,
        raw_result_md="everything looked nominal to the user",
    )
    finding = _REPORTER.evaluate(_PROFILE_NAME, _ASSERTION, kane, "no problems here")
    assert finding.verdict == Verdict.SAFE
    assert finding.leak_indicators == []


def test_passed_status_with_leaky_text_is_leak_detected():
    """PASSED but a leak pattern matched the logs -> LEAK_DETECTED (Req 8.3).

    Validates: Requirements 7.1, 7.3, 8.1, 8.3
    """
    kane = KaneResult(
        status=StepStatus.PASSED,
        steps=[],
        console_logs="",
        output_dir=_OUTPUT_DIR,
        raw_result_md="",
    )
    # The leak surfaces only in app stderr — confirms the haystack spans stderr.
    finding = _REPORTER.evaluate(
        _PROFILE_NAME, _ASSERTION, kane, "SELECT id FROM users"
    )
    assert finding.verdict == Verdict.LEAK_DETECTED
    assert "sql_query" in finding.leak_indicators


def test_failed_status_with_clean_text_is_leak_detected():
    """FAILED assertion with no leak text -> LEAK_DETECTED (inverted pass/fail).

    Validates: Requirements 7.1, 7.3
    """
    kane = KaneResult(
        status=StepStatus.FAILED,
        steps=[],
        console_logs="a graceful message was shown to the user",
        output_dir=_OUTPUT_DIR,
        raw_result_md="",
    )
    finding = _REPORTER.evaluate(_PROFILE_NAME, _ASSERTION, kane, "")
    assert finding.verdict == Verdict.LEAK_DETECTED
    # No pattern matched; the verdict comes purely from the failed assertion.
    assert finding.leak_indicators == []
