"""Total parser for Kane CLI ``Result.md`` output files.

This module provides :func:`parse_result_md`, a *total* function: it never
raises on any string input. It extracts the overall run status from the YAML
frontmatter and one :class:`~fileak.models.KaneStepResult` per body step
header.

The parser is deliberately stdlib-only (``re``) and hand-rolls a minimal
frontmatter scan rather than depending on PyYAML, per the design.

Result.md shape (see ``.testmuai/tests/output-*/Result.md``)::

    ---
    test: ../<stem>_test.md
    status: passed            # passed | failed
    started: 2026-05-30T18:03:06.324Z
    duration_s: 64.1
    session_id: <uuid>
    ---

    # Session: <stem> — Result

    ## <step heading> ✓ passed (64.1s)
    <optional body lines>

Step header markers use unicode icons — ✓ (U+2713), ✗ (U+2717),
⏭ (U+23ED) — but the parser keys off the status *word*
(``passed``/``failed``/``skipped``) so it is robust to a missing icon. The
heading text is arbitrary (it is NOT necessarily ``Step N``). A soft-failing
step may carry an ``(optional)`` suffix, e.g.
``## Heading ✓ passed (optional) (1.2s)``.
"""

from __future__ import annotations

import re

from fileak.models import KaneStepResult, StepStatus

# Map the marker word to a per-step StepStatus.
_STATUS_MAP: dict[str, StepStatus] = {
    "passed": StepStatus.PASSED,
    "failed": StepStatus.FAILED,
    "skipped": StepStatus.SKIPPED,
}

# A body step header:  ## <heading> <icon?> <status> (optional)? (<n>s)?
# - The heading is non-greedy so the trailing marker is matched against the
#   END of the line; this lets the engine find the real marker even when the
#   heading text itself contains a status word.
# - The icon (✓ U+2713 / ✗ U+2717 / ⏭ U+23ED, with an optional U+FE0F
#   variation selector) is optional for robustness.
# - The (optional) suffix and the (<n>s) duration suffix are both optional.
_STEP_HEADER_RE = re.compile(
    r"^##\s+(?P<heading>.*?)\s*"
    r"(?:[\u2713\u2717\u23ED]\uFE0F?\s*)?"
    r"(?P<status>passed|failed|skipped)"
    r"(?:\s+\(optional\))?"
    r"(?:\s*\(\s*(?P<dur>\d+(?:\.\d+)?)\s*s\s*\))?"
    r"\s*$",
    re.IGNORECASE,
)

# A frontmatter `status:` line, e.g. `status: passed` (quotes tolerated).
_STATUS_LINE_RE = re.compile(r"^\s*status\s*:\s*(?P<value>\S+)", re.IGNORECASE)


def _parse_frontmatter_status(text: str) -> str | None:
    """Return the lowercased frontmatter ``status`` value if it is one of
    ``{"passed", "failed"}``, else ``None``.

    The frontmatter is the block delimited by a leading ``---`` line and the
    next ``---`` line. Missing frontmatter, a missing ``status`` key, or a
    ``status`` value outside ``{passed, failed}`` all yield ``None`` (which the
    caller maps to ``StepStatus.ERROR``).
    """
    lines = text.splitlines()

    # Skip leading blank lines, then require an opening `---` fence.
    i = 0
    while i < len(lines) and lines[i].strip() == "":
        i += 1
    if i >= len(lines) or lines[i].strip() != "---":
        return None
    i += 1
    block_start = i

    # Find the closing `---` fence.
    closing = None
    while i < len(lines):
        if lines[i].strip() == "---":
            closing = i
            break
        i += 1
    if closing is None:
        return None

    # Scan the frontmatter block for the first `status:` key.
    for line in lines[block_start:closing]:
        m = _STATUS_LINE_RE.match(line)
        if m:
            value = m.group("value").strip().strip("\"'").lower()
            if value in ("passed", "failed"):
                return value
            return None
    return None


def _parse_steps(text: str) -> list[KaneStepResult]:
    """Emit one :class:`KaneStepResult` per recognized body step header,
    preserving document order with ``index`` increasing monotonically from 1.
    """
    steps: list[KaneStepResult] = []
    index = 1
    for line in text.splitlines():
        m = _STEP_HEADER_RE.match(line)
        if m is None:
            continue
        status_word = m.group("status").lower()
        step_status = _STATUS_MAP[status_word]
        heading = m.group("heading").strip()
        dur = m.group("dur")
        duration_s = float(dur) if dur else 0.0
        steps.append(
            KaneStepResult(
                index=index,
                status=step_status,
                text=heading,
                duration_s=duration_s,
            )
        )
        index += 1
    return steps


def parse_result_md(text: str) -> tuple[StepStatus, list[KaneStepResult]]:
    """Parse a Kane ``Result.md`` document into an overall status + step list.

    Postconditions:
    - The overall :class:`StepStatus` equals the frontmatter ``status``
      (``passed`` -> ``PASSED``, ``failed`` -> ``FAILED``).
    - One :class:`KaneStepResult` is emitted per body step header, with status
      derived from the ``✓ passed`` / ``✗ failed`` / ``⏭ skipped`` marker and
      ``duration_s`` from the ``(<n>s)`` suffix (``0.0`` when absent).
    - Steps preserve document order; ``index`` increases monotonically from 1.
    - Malformed/garbage input yields ``(StepStatus.ERROR, [])`` and the
      function NEVER raises (parser totality — Property 7).
    """
    try:
        if not isinstance(text, str):
            return (StepStatus.ERROR, [])
        status = _parse_frontmatter_status(text)
        if status is None:
            return (StepStatus.ERROR, [])
        overall = StepStatus.PASSED if status == "passed" else StepStatus.FAILED
        steps = _parse_steps(text)
        return (overall, steps)
    except Exception:
        # Totality guarantee: any unexpected failure degrades to ERROR.
        return (StepStatus.ERROR, [])
