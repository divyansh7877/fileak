"""Example-based, table-driven unit tests for ``parse_result_md`` (task 4.3).

These complement the totality *property* test in
``test_result_parser_totality_property.py`` (Property 7) with concrete,
realistic ``Result.md`` documents modeled on the real Kane output sample at
``.testmuai/tests/output-2026-05-30T18-04-35/Result.md``. Where the property
test asserts universal invariants (never raises, contiguous indices), these
tests pin down exact parsing of real-world document shapes:

  * a passed single-step doc (modeled byte-for-byte on the real sample),
  * a failed doc with a ``✗ failed`` step,
  * a ``⏭ skipped`` step variant,
  * a multi-step doc mixing passed/failed/skipped in document order,
  * an ``(optional)`` header suffix variant, and
  * the ACTUAL on-disk sample file (skipped when not present).

For each case we assert the overall :class:`StepStatus`, the step count, and
the per-step index / status / duration (exact where the duration is explicit).

Validates: Requirements 6.6

Framework: pytest (``pytest.mark.parametrize`` for the table-driven cases).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fileak.models import StepStatus
from fileak.result_parser import parse_result_md

# Path to the real on-disk Kane sample this suite is modeled on.
_REAL_SAMPLE = Path(".testmuai/tests/output-2026-05-30T18-04-35/Result.md")


# ---------------------------------------------------------------------------
# Realistic Result.md samples (modeled on the real sample's exact shape)
# ---------------------------------------------------------------------------

# 1. Passed single-step doc — a near-verbatim copy of the real sample.
PASSED_SINGLE_STEP = """\
---
test: ../2026-05-30T18-04-35_test.md
status: passed
started: 2026-05-30T18:03:06.324Z
duration_s: 64.1
session_id: bba0a012-b664-4867-a6e1-7a42df2f7855
---

# Session: 2026-05-30T18-04-35 — Result

## Step 1 ✓ passed (64.1s)
md5: a2b92f4b27e9eca8b7139429939d2976
Go to https://example.com and assert the page title contains 'Example'
"""

# 2. Failed doc — overall failed frontmatter with a ✗ failed step whose heading
#    is arbitrary natural-language text (NOT "Step N").
FAILED_DOC = """\
---
test: ../2026-05-30T19-10-00_test.md
status: failed
started: 2026-05-30T19:09:01.000Z
duration_s: 12.5
session_id: aa11bb22-cc33-dd44-ee55-ff6677889900
---

# Session: 2026-05-30T19-10-00 — Result

## Verify the error page hides internal stack traces ✗ failed (12.5s)
md5: 0bf3c1aa9e2d4f5061728394a5b6c7d8
A raw stack trace was rendered on the settings page.
"""

# 3. Skipped step variant — a soft step that did not run (⏭ skipped).
SKIPPED_DOC = """\
---
test: ../2026-05-30T20-00-00_test.md
status: passed
started: 2026-05-30T19:59:00.000Z
duration_s: 3.2
session_id: 12340000-0000-4000-8000-000000000000
---

# Session: 2026-05-30T20-00-00 — Result

## Optional telemetry probe ⏭ skipped (3.2s)
md5: deadbeefdeadbeefdeadbeefdeadbeef
Telemetry endpoint unavailable; step skipped.
"""

# 4. Multi-step doc — 3+ steps mixing passed/failed/skipped in document order.
MULTI_STEP_DOC = """\
---
test: ../2026-05-30T21-30-00_test.md
status: failed
started: 2026-05-30T21:29:00.000Z
duration_s: 42.0
session_id: 99998888-7777-6666-5555-444433332222
---

# Session: 2026-05-30T21-30-00 — Result

## Navigate to the checkout page ✓ passed (10.0s)
md5: 1111111111111111aaaaaaaaaaaaaaaa
Go to https://example.com/checkout

## Verify no database query is leaked ✗ failed (25.5s)
md5: 2222222222222222bbbbbbbbbbbbbbbb
A SQL query string appeared in the page body.

## Cleanup temporary session state ⏭ skipped (6.5s)
md5: 3333333333333333cccccccccccccccc
Cleanup not required for this run.
"""

# 5. (optional) header variant — the same passed step but the header carries an
#    ``(optional)`` suffix before the ``(<n>s)`` duration, as soft steps do.
OPTIONAL_HEADER_DOC = """\
---
test: ../2026-05-30T22-15-00_test.md
status: passed
started: 2026-05-30T22:14:00.000Z
duration_s: 1.2
session_id: abcdef00-1234-4567-89ab-cdef01234567
---

# Session: 2026-05-30T22-15-00 — Result

## Best-effort accessibility sweep ✓ passed (optional) (1.2s)
md5: 4444444444444444dddddddddddddddd
No critical accessibility violations found.
"""


# Each table row: (case_id, document, expected_overall, expected_steps) where
# expected_steps is a list of (index, status, duration_s) tuples in document
# order. Durations are exact because every header here states an explicit
# ``(<n>s)`` suffix.
_PARSE_CASES = [
    pytest.param(
        PASSED_SINGLE_STEP,
        StepStatus.PASSED,
        [(1, StepStatus.PASSED, 64.1)],
        id="passed_single_step",
    ),
    pytest.param(
        FAILED_DOC,
        StepStatus.FAILED,
        [(1, StepStatus.FAILED, 12.5)],
        id="failed_doc",
    ),
    pytest.param(
        SKIPPED_DOC,
        StepStatus.PASSED,
        [(1, StepStatus.SKIPPED, 3.2)],
        id="skipped_step_variant",
    ),
    pytest.param(
        MULTI_STEP_DOC,
        StepStatus.FAILED,
        [
            (1, StepStatus.PASSED, 10.0),
            (2, StepStatus.FAILED, 25.5),
            (3, StepStatus.SKIPPED, 6.5),
        ],
        id="multi_step_mixed",
    ),
    pytest.param(
        OPTIONAL_HEADER_DOC,
        StepStatus.PASSED,
        [(1, StepStatus.PASSED, 1.2)],
        id="optional_header_variant",
    ),
]


@pytest.mark.parametrize("document, expected_overall, expected_steps", _PARSE_CASES)
def test_parse_result_md_samples(document, expected_overall, expected_steps):
    """Realistic Result.md samples parse to the expected overall status and
    per-step index/status/duration, in document order.

    Validates: Requirements 6.6
    """
    overall, steps = parse_result_md(document)

    assert overall is expected_overall
    assert len(steps) == len(expected_steps)

    # Indices must be contiguous and monotonically increasing from 1.
    assert [s.index for s in steps] == list(range(1, len(expected_steps) + 1))

    for step, (exp_index, exp_status, exp_duration) in zip(steps, expected_steps):
        assert step.index == exp_index
        assert step.status is exp_status
        assert step.duration_s == pytest.approx(exp_duration)


def test_passed_single_step_captures_heading_text():
    """The passed single-step sample captures the arbitrary heading text and
    exact duration (modeled on the real on-disk sample).

    Validates: Requirements 6.6
    """
    overall, steps = parse_result_md(PASSED_SINGLE_STEP)

    assert overall is StepStatus.PASSED
    assert len(steps) == 1

    (step,) = steps
    assert step.index == 1
    assert step.status is StepStatus.PASSED
    assert step.duration_s == pytest.approx(64.1)
    # Heading text is captured (here it is "Step 1", but the parser keys off the
    # status word, not the heading — see the multi-step natural-language cases).
    assert step.text == "Step 1"


def test_failed_doc_captures_natural_language_heading():
    """A failed doc whose step heading is free-form text still parses to a
    FAILED step and preserves the heading text verbatim.

    Validates: Requirements 6.6
    """
    overall, steps = parse_result_md(FAILED_DOC)

    assert overall is StepStatus.FAILED
    (step,) = steps
    assert step.status is StepStatus.FAILED
    assert step.text == "Verify the error page hides internal stack traces"


def test_optional_header_variant_preserves_status_and_duration():
    """An ``(optional)`` header suffix does not disturb status or duration
    parsing; the trailing ``(<n>s)`` is still read correctly.

    Validates: Requirements 6.6
    """
    overall, steps = parse_result_md(OPTIONAL_HEADER_DOC)

    assert overall is StepStatus.PASSED
    (step,) = steps
    assert step.status is StepStatus.PASSED
    assert step.duration_s == pytest.approx(1.2)
    assert step.text == "Best-effort accessibility sweep"


def test_multi_step_preserves_document_order():
    """A multi-step doc yields per-step statuses and durations in document
    order with contiguous indices.

    Validates: Requirements 6.6
    """
    overall, steps = parse_result_md(MULTI_STEP_DOC)

    assert overall is StepStatus.FAILED
    assert [s.status for s in steps] == [
        StepStatus.PASSED,
        StepStatus.FAILED,
        StepStatus.SKIPPED,
    ]
    assert [s.duration_s for s in steps] == pytest.approx([10.0, 25.5, 6.5])
    assert [s.index for s in steps] == [1, 2, 3]


@pytest.mark.skipif(
    not _REAL_SAMPLE.is_file(),
    reason=f"real Kane sample not present at {_REAL_SAMPLE}",
)
def test_real_on_disk_sample_parses_to_passed_single_step():
    """The ACTUAL on-disk Kane sample parses to overall PASSED with exactly one
    PASSED step of duration 64.1.

    Validates: Requirements 6.6
    """
    text = _REAL_SAMPLE.read_text(encoding="utf-8")
    overall, steps = parse_result_md(text)

    assert overall is StepStatus.PASSED
    assert len(steps) == 1

    (step,) = steps
    assert step.index == 1
    assert step.status is StepStatus.PASSED
    assert step.duration_s == pytest.approx(64.1)
