"""Property-based test for ``parse_result_md`` totality (task 4.2).

The Kane ``Result.md`` parser is the one place where untrusted, possibly
corrupt external text crosses into the engine. The design therefore makes it a
*total* function: it must never raise, whatever bytes Kane (or a damaged disk,
or a half-written file) hands it. Malformed input degrades to a sentinel
``(StepStatus.ERROR, [])`` and well-formed input yields steps whose ``index``
values are contiguous and monotonically increasing from 1.

**Property 7 — Parser totality.** For *all* string inputs ``parse_result_md``
returns without raising and yields a ``(StepStatus, list[KaneStepResult])``
2-tuple. For malformed input (no valid frontmatter ``status: passed|failed``
block) it returns exactly ``(StepStatus.ERROR, [])``. For valid input the
returned step indices are exactly ``[1, 2, ..., N]`` (contiguous, strictly
increasing from 1) and the overall status mirrors the frontmatter ``status``.

Validates: Requirements 6.6, 6.7

Strategy notes
--------------
Four complementary tests cover the property:

* ``test_parser_never_raises_on_arbitrary_text`` — broad ``st.text()`` (unicode,
  control chars, the lot) confirms the never-raise / well-typed-result guarantee
  and the universal index invariant.
* ``test_parser_never_raises_on_resultmd_like_fragments`` — adversarial
  *structured* inputs assembled from ``---`` fences, ``status:`` lines, and
  ``## ... passed/failed/skipped`` headers, so the real parsing paths (not just
  garbage) are stressed.
* ``test_malformed_input_yields_error`` — inputs constructed to *guarantee* no
  valid frontmatter must return exactly ``(StepStatus.ERROR, [])``.
* ``test_valid_input_has_contiguous_monotonic_indices_from_one`` — well-formed
  documents (valid frontmatter + N generated step headers) must yield exactly N
  steps indexed ``[1..N]`` with the overall status matching the frontmatter.

Framework: Hypothesis (per design "Property Test Library: Hypothesis (Python)").
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from fileak.models import KaneStepResult, StepStatus
from fileak.result_parser import parse_result_md

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

# Per-step marker word -> expected StepStatus (mirrors the parser's _STATUS_MAP).
_EXPECTED_STEP_STATUS: dict[str, StepStatus] = {
    "passed": StepStatus.PASSED,
    "failed": StepStatus.FAILED,
    "skipped": StepStatus.SKIPPED,
}


def _assert_total(text: str) -> tuple[StepStatus, list[KaneStepResult]]:
    """Assert the universal totality shape for ``text`` and return the result.

    Holds for EVERY input: the call never raises (enforced by the framework —
    an exception fails the test), the result is a 2-tuple of a ``StepStatus``
    and a ``list`` of ``KaneStepResult``, and the step indices are always the
    contiguous run ``[1..N]`` regardless of how malformed the body was.
    """
    result = parse_result_md(text)

    assert isinstance(result, tuple) and len(result) == 2, (
        "parse_result_md must always return a 2-tuple"
    )
    status, steps = result
    assert isinstance(status, StepStatus)
    assert isinstance(steps, list)
    for step in steps:
        assert isinstance(step, KaneStepResult)
    assert [s.index for s in steps] == list(range(1, len(steps) + 1)), (
        "step indices must be contiguous and monotonically increasing from 1"
    )
    return status, steps


# ---------------------------------------------------------------------------
# Strategies: adversarial Result.md-like fragments (stress real parse paths)
# ---------------------------------------------------------------------------

# A grab-bag of lines that look like the pieces of a real Result.md, so the
# generated text repeatedly trips the frontmatter scan and the step-header
# regex instead of being pure noise.
_RESULTMD_FRAGMENT = st.one_of(
    st.just("---"),
    st.sampled_from(
        [
            "status: passed",
            "status: failed",
            "status: skipped",
            "status: maybe",
            "status:",
            'status: "PASSED"',
            "status: Failed",
            "test: ../demo_test.md",
            "duration_s: 64.1",
        ]
    ),
    st.sampled_from(
        [
            "## Step 1 \u2713 passed (64.1s)",
            "## heading \u2717 failed",
            "## thing \u23ED skipped (1.2s)",
            "## no marker here",
            "## passed",
            "## weird passed (optional) (3s)",
            "# Session: x \u2014 Result",
            "md5: a2b92f4b27e9eca8b",
            "Go to https://example.com",
            "",
        ]
    ),
    st.text(max_size=40),
)

_resultmd_like_text = st.lists(_RESULTMD_FRAGMENT, max_size=25).map(
    lambda fragments: "\n".join(fragments)
)


# ---------------------------------------------------------------------------
# Strategies: guaranteed-malformed input (must yield (ERROR, []))
# ---------------------------------------------------------------------------

# A single line that is guaranteed NOT to be a standalone `---` fence. Newlines
# are flattened to spaces so each element really is one line.
_non_fence_line = (
    st.text(max_size=40)
    .map(lambda s: s.replace("\r", " ").replace("\n", " "))
    .filter(lambda line: line.strip() != "---")
)


def _no_frontmatter() -> st.SearchStrategy[str]:
    """Text containing no standalone ``---`` fence at all -> no frontmatter."""
    return st.lists(_non_fence_line, max_size=12).map(lambda ls: "\n".join(ls))


@st.composite
def _open_fence_no_close(draw) -> str:
    """An opening ``---`` fence but no closing fence -> frontmatter unterminated."""
    body = draw(st.lists(_non_fence_line, max_size=8))
    return "\n".join(["---"] + body)


@st.composite
def _bad_status_block(draw) -> str:
    """Valid ``---`` fences but the frontmatter has no ``status: passed|failed``.

    Either there is no ``status`` key, or it carries a value outside the
    accepted ``{passed, failed}`` set (e.g. ``skipped``, ``maybe``, empty), so
    the parser must return ``(ERROR, [])``. A real-looking step header is added
    after the closing fence to confirm steps are NOT parsed once frontmatter is
    rejected.
    """
    invalid_status = draw(
        st.sampled_from(
            [
                "status: maybe",
                "status: skipped",
                "status: error",
                "status: pass",
                "status: 200",
                "status:",
                'status: ""',
                "stat: passed",
                "test: passed",
            ]
        )
    )
    other = draw(
        st.lists(
            st.sampled_from(
                [
                    "test: ../foo_test.md",
                    "started: 2026-01-01T00:00:00.000Z",
                    "duration_s: 1.0",
                    "session_id: abc-123",
                    "",
                ]
            ),
            max_size=4,
        )
    )
    block = list(other)
    if draw(st.booleans()):
        pos = draw(st.integers(min_value=0, max_value=len(block)))
        block.insert(pos, invalid_status)
    lines = (
        ["---"]
        + block
        + ["---", "", "# Session: x \u2014 Result", "## Step 1 \u2713 passed (1s)"]
    )
    return "\n".join(lines)


_malformed_text = st.one_of(
    _no_frontmatter(),
    _open_fence_no_close(),
    _bad_status_block(),
)


# ---------------------------------------------------------------------------
# Strategies: well-formed Result.md documents
# ---------------------------------------------------------------------------

# Clean ASCII heading text: letters, digits, spaces. Heading variety is not the
# point of this property (indices/count/overall status are), and the breadth
# tests above already exercise unicode and control characters.
_VALID_HEADING = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ",
    max_size=24,
)

_STEP_ICONS = ["", "\u2713", "\u2717", "\u23ED"]
_DURATIONS = ["0", "1", "1.5", "12.34", "64.1", "300", "0.0"]
_BODY_POOL = [
    "",
    "md5: a2b92f4b27e9eca8b",
    "Go to https://example.com",
    "step body text",
    "- detail item",
    "info: none",
]


@st.composite
def _valid_step(draw) -> tuple[str, StepStatus]:
    """Generate one valid ``## <heading> <icon?> <status> (optional)? (<n>s)?``
    header line and the ``StepStatus`` it should parse to.
    """
    word = draw(st.sampled_from(["passed", "failed", "skipped"]))
    heading = draw(_VALID_HEADING).strip()
    icon = draw(st.sampled_from(_STEP_ICONS))

    parts = ["##"]
    if heading:
        parts.append(heading)
    if icon:
        parts.append(icon)
    parts.append(word)
    line = " ".join(parts)

    if draw(st.booleans()):
        line += " (optional)"
    if draw(st.booleans()):
        line += " (" + draw(st.sampled_from(_DURATIONS)) + "s)"

    return line, _EXPECTED_STEP_STATUS[word]


@st.composite
def _valid_result_md(draw) -> tuple[str, StepStatus, list[StepStatus]]:
    """Assemble a well-formed Result.md document.

    Returns ``(text, expected_overall_status, expected_step_statuses)`` where
    the body contains exactly ``len(expected_step_statuses)`` step headers in
    document order.
    """
    base = draw(st.sampled_from(["passed", "failed"]))
    rendered = draw(
        st.sampled_from([base, base.upper(), base.capitalize(), f'"{base}"', f"'{base}'"])
    )
    expected_overall = StepStatus.PASSED if base == "passed" else StepStatus.FAILED

    other_fm = draw(
        st.lists(
            st.sampled_from(
                [
                    "test: ../demo_test.md",
                    "started: 2026-05-30T18:03:06.324Z",
                    "duration_s: 64.1",
                    "session_id: bba0a012-b664",
                ]
            ),
            max_size=4,
            unique=True,
        )
    )
    block = list(other_fm)
    block.insert(draw(st.integers(min_value=0, max_value=len(block))), "status: " + rendered)

    steps = draw(st.lists(_valid_step(), min_size=0, max_size=6))

    lines = ["---"] + block + ["---", "", "# Session: demo \u2014 Result", ""]
    expected_statuses: list[StepStatus] = []
    for header_line, status in steps:
        lines.append(header_line)
        expected_statuses.append(status)
        for _ in range(draw(st.integers(min_value=0, max_value=2))):
            lines.append(draw(st.sampled_from(_BODY_POOL)))

    return "\n".join(lines), expected_overall, expected_statuses


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@settings(deadline=None, max_examples=400)
@given(text=st.text())
def test_parser_never_raises_on_arbitrary_text(text):
    """Property 7: arbitrary strings never raise; result is well-typed with
    contiguous indices.

    Validates: Requirements 6.6, 6.7
    """
    _assert_total(text)


@settings(deadline=None, max_examples=400)
@given(text=_resultmd_like_text)
def test_parser_never_raises_on_resultmd_like_fragments(text):
    """Property 7: adversarial Result.md-shaped inputs stress the real parsing
    paths yet still never raise and stay well-typed.

    Validates: Requirements 6.6, 6.7
    """
    _assert_total(text)


@settings(deadline=None, max_examples=300)
@given(text=_malformed_text)
def test_malformed_input_yields_error(text):
    """Property 7: input lacking a valid ``status: passed|failed`` frontmatter
    block returns exactly ``(StepStatus.ERROR, [])``.

    Validates: Requirements 6.6, 6.7
    """
    status, steps = _assert_total(text)
    assert status is StepStatus.ERROR
    assert steps == []


@settings(deadline=None, max_examples=300)
@given(doc=_valid_result_md())
def test_valid_input_has_contiguous_monotonic_indices_from_one(doc):
    """Property 7: well-formed documents yield N steps indexed ``[1..N]`` with
    the overall status matching the frontmatter ``status``.

    Validates: Requirements 6.6, 6.7
    """
    text, expected_overall, expected_statuses = doc
    status, steps = _assert_total(text)

    assert status is expected_overall, "overall status must mirror frontmatter"
    assert len(steps) == len(expected_statuses), (
        "one step must be emitted per generated step header"
    )
    assert [s.index for s in steps] == list(range(1, len(expected_statuses) + 1)), (
        "indices must be contiguous and strictly increasing from 1"
    )
    assert [s.status for s in steps] == expected_statuses, (
        "per-step status must match the generated marker word"
    )
