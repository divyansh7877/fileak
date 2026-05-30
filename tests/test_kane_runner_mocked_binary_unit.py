"""Example-based unit tests for :class:`~fileak.kane.KaneRunner` against a
*mocked* ``kane-cli`` binary (task 6.3).

These tests exercise the KaneRunner adapter end-to-end **fully offline**: the
real ``kane-cli`` binary and the network are never touched. They drive the
adapter's exit-code mapping and ``Result.md`` parsing (``_collect_result``) by
stubbing the single subprocess seam (``executor``) and pinning a deterministic
session ``<stem>`` (``stem_factory``).

The injected ``executor`` returns the desired Kane exit code and — for cases
that need one — *materializes* ``output-<stem>/Result.md`` next to the authored
test file as a side effect, mimicking exactly what the real binary would
produce. This lets us assert the resulting :class:`~fileak.models.KaneResult`
``status`` for every (exit code × Result.md) combination the design calls out.

Exit-code / Result.md → status mapping under test (design "KaneRunner.run",
Requirements 6.3/6.4/6.5):

  * exit 0 + well-formed PASSED Result.md  -> PASSED   (Req 6.3)
  * exit 1 + well-formed FAILED Result.md  -> FAILED   (Req 6.4)
  * exit 1 + well-formed PASSED Result.md  -> FAILED   (exit-code override, Req 6.4)
  * exit 2 + present Result.md             -> ERROR    (Req 6.5)
  * exit 3 + present Result.md             -> ERROR    (Req 6.5)
  * exit 0 + MISSING Result.md             -> ERROR    (Req 6.5)
  * exit 0 + MALFORMED Result.md           -> ERROR    (Req 6.5, parser totality)

Plus: the adapter uses the injected executor (never the real binary/network),
authors a committable ``<stem>_test.md``, and builds the documented Kane
command (``testmd run ... --agent --headless --timeout --max-steps``).

Validates: Requirements 6.3, 6.4, 6.5

Framework: pytest (``tmp_path`` for an isolated ``tests_dir``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fileak import kane as kane_module
from fileak.kane import KaneRunner
from fileak.models import KaneResult, SecurityAssertion, StepStatus

# A deterministic session stem so the test knows exactly where the adapter will
# write ``<stem>_test.md`` and look for ``output-<stem>/Result.md``.
STEM = "2026-05-30T19-12-00"

# The base URL the authored test file's first step navigates to.
BASE_URL = "http://localhost:3000"

# A representative semantic assertion (content is irrelevant to the mapping;
# the executor is stubbed regardless of the authored prompt).
ASSERTION = SecurityAssertion(
    id="checkout_no_leak",
    prompt=(
        "Perform the checkout flow and ensure no raw stack traces, file paths, "
        "database queries, or environment variables are visible on the screen."
    ),
    applies_to=[],
)


# ---------------------------------------------------------------------------
# Mocked kane-cli binary
# ---------------------------------------------------------------------------
class _RecordingExecutor:
    """A stub ``kane-cli`` that records each invocation and (optionally) writes
    a ``Result.md`` as the real binary would, then returns a fixed exit code.

    It is injected via ``KaneRunner(executor=...)`` so the adapter NEVER shells
    out to the real binary or the network. When ``result_md`` is provided it is
    written to ``<output_dir>/Result.md`` (creating the directory) as a side
    effect before returning ``exit_code`` — mimicking the artifact the real CLI
    would have produced. When ``result_md`` is ``None`` the executor returns the
    code but writes nothing (the "missing Result.md" case).
    """

    def __init__(self, exit_code: int, *, output_dir: Path, result_md: str | None = None) -> None:
        self.exit_code = exit_code
        self.output_dir = Path(output_dir)
        self.result_md = result_md
        self.calls: list[tuple[list[str], float]] = []

    def __call__(self, cmd: list[str], timeout: float) -> int:
        self.calls.append((list(cmd), timeout))
        if self.result_md is not None:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            (self.output_dir / "Result.md").write_text(self.result_md, encoding="utf-8")
        return self.exit_code


def _make_runner(tests_dir: Path, executor: _RecordingExecutor, *, kane_bin: str = "kane-cli") -> KaneRunner:
    """Build a KaneRunner with the binary stubbed and the stem pinned."""
    return KaneRunner(
        tests_dir=tests_dir,
        kane_bin=kane_bin,
        executor=executor,
        stem_factory=lambda: STEM,
    )


# ---------------------------------------------------------------------------
# Well-formed / malformed Result.md fixtures (modeled on the real Kane sample
# at .testmuai/tests/output-2026-05-30T18-04-35/Result.md)
# ---------------------------------------------------------------------------
def _passed_result_md(stem: str = STEM) -> str:
    """A well-formed Kane Result.md whose frontmatter + step are ``passed``."""
    return (
        "---\n"
        f"test: ../{stem}_test.md\n"
        "status: passed\n"
        "started: 2026-05-30T19:11:00.000Z\n"
        "duration_s: 30.0\n"
        "session_id: bba0a012-b664-4867-a6e1-7a42df2f7855\n"
        "---\n"
        "\n"
        f"# Session: {stem} — Result\n"
        "\n"
        "## Step 1 ✓ passed (30.0s)\n"
        "md5: a2b92f4b27e9eca8b7139429939d2976\n"
        f"Go to {BASE_URL} and verify nothing sensitive is rendered\n"
    )


def _failed_result_md(stem: str = STEM) -> str:
    """A well-formed Kane Result.md whose frontmatter + step are ``failed``."""
    return (
        "---\n"
        f"test: ../{stem}_test.md\n"
        "status: failed\n"
        "started: 2026-05-30T19:11:00.000Z\n"
        "duration_s: 12.0\n"
        "session_id: aa11bb22-cc33-dd44-ee55-ff6677889900\n"
        "---\n"
        "\n"
        f"# Session: {stem} — Result\n"
        "\n"
        "## Verify the error page hides internal stack traces ✗ failed (12.0s)\n"
        "md5: 0bf3c1aa9e2d4f5061728394a5b6c7d8\n"
        "A raw stack trace was rendered on the settings page.\n"
    )


# Garbage with no valid YAML frontmatter; parse_result_md must degrade to
# (ERROR, []) (parser totality), so the adapter reports ERROR.
MALFORMED_RESULT_MD = (
    "this is not a valid Result.md\n"
    "<<< no frontmatter, just $$$ random %%% bytes >>>\n"
    "## not even a real step marker\n"
)


# ---------------------------------------------------------------------------
# exit 0 + well-formed PASSED Result.md -> PASSED (Req 6.3)
# ---------------------------------------------------------------------------
def test_exit0_passed_result_md_maps_to_passed(tmp_path: Path):
    """exit 0 with a well-formed PASSED Result.md yields status PASSED with the
    single passed step parsed through.

    Validates: Requirements 6.3
    """
    tests_dir = tmp_path / "tests"
    output_dir = tests_dir / f"output-{STEM}"
    executor = _RecordingExecutor(0, output_dir=output_dir, result_md=_passed_result_md())
    runner = _make_runner(tests_dir, executor)

    result = runner.run(ASSERTION, BASE_URL)

    assert isinstance(result, KaneResult)
    assert result.status is StepStatus.PASSED
    assert [s.status for s in result.steps] == [StepStatus.PASSED]
    assert result.steps[0].index == 1
    assert result.raw_result_md != ""
    assert result.output_dir == output_dir


# ---------------------------------------------------------------------------
# exit 1 + well-formed FAILED Result.md -> FAILED (Req 6.4)
# ---------------------------------------------------------------------------
def test_exit1_failed_result_md_maps_to_failed(tmp_path: Path):
    """exit 1 with a well-formed FAILED Result.md yields status FAILED (the
    parsed status already agrees with the failing exit code).

    Validates: Requirements 6.4
    """
    tests_dir = tmp_path / "tests"
    output_dir = tests_dir / f"output-{STEM}"
    executor = _RecordingExecutor(1, output_dir=output_dir, result_md=_failed_result_md())
    runner = _make_runner(tests_dir, executor)

    result = runner.run(ASSERTION, BASE_URL)

    assert result.status is StepStatus.FAILED
    assert [s.status for s in result.steps] == [StepStatus.FAILED]


# ---------------------------------------------------------------------------
# exit 1 + well-formed PASSED Result.md -> FAILED (exit-code override, Req 6.4)
# ---------------------------------------------------------------------------
def test_exit1_overrides_parsed_passed_to_failed(tmp_path: Path):
    """exit 1 forces FAILED even when the Result.md frontmatter parsed PASSED:
    Kane judged the assertion failed via its exit code, so the adapter's
    ``exit_code == 1`` override wins over the doc's ``status: passed``.

    Validates: Requirements 6.4
    """
    tests_dir = tmp_path / "tests"
    output_dir = tests_dir / f"output-{STEM}"
    # The Result.md says passed, but Kane exited 1 (assertion failed).
    executor = _RecordingExecutor(1, output_dir=output_dir, result_md=_passed_result_md())
    runner = _make_runner(tests_dir, executor)

    result = runner.run(ASSERTION, BASE_URL)

    assert result.status is StepStatus.FAILED


# ---------------------------------------------------------------------------
# exit 2 / exit 3 + present Result.md -> ERROR (Req 6.5)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "exit_code",
    [
        pytest.param(2, id="exit2_error"),
        pytest.param(3, id="exit3_timeout_cancel"),
    ],
)
def test_error_exit_codes_map_to_error_even_with_present_result_md(tmp_path: Path, exit_code: int):
    """exit 2 (error) and exit 3 (timeout/cancel) map to status ERROR even when
    a present, well-formed Result.md exists — an error/timeout exit code is not
    judgeable regardless of the artifact.

    Validates: Requirements 6.5
    """
    tests_dir = tmp_path / "tests"
    output_dir = tests_dir / f"output-{STEM}"
    # A perfectly valid PASSED Result.md is on disk, yet the error/timeout exit
    # code must still win and produce ERROR.
    executor = _RecordingExecutor(exit_code, output_dir=output_dir, result_md=_passed_result_md())
    runner = _make_runner(tests_dir, executor)

    result = runner.run(ASSERTION, BASE_URL)

    assert result.status is StepStatus.ERROR
    assert result.steps == []


# ---------------------------------------------------------------------------
# exit 0 + MISSING Result.md -> ERROR (Req 6.5)
# ---------------------------------------------------------------------------
def test_exit0_missing_result_md_maps_to_error(tmp_path: Path):
    """exit 0 but a MISSING Result.md (the executor returned 0 yet wrote
    nothing) yields ERROR with no steps and an empty ``raw_result_md``.

    Validates: Requirements 6.5
    """
    tests_dir = tmp_path / "tests"
    output_dir = tests_dir / f"output-{STEM}"
    # result_md=None -> executor writes no Result.md.
    executor = _RecordingExecutor(0, output_dir=output_dir, result_md=None)
    runner = _make_runner(tests_dir, executor)

    result = runner.run(ASSERTION, BASE_URL)

    assert result.status is StepStatus.ERROR
    assert result.steps == []
    assert result.raw_result_md == ""


# ---------------------------------------------------------------------------
# exit 0 + MALFORMED Result.md -> ERROR (Req 6.5, parser totality)
# ---------------------------------------------------------------------------
def test_exit0_malformed_result_md_maps_to_error(tmp_path: Path):
    """exit 0 with a MALFORMED Result.md (garbage, no valid frontmatter) yields
    ERROR: the total parser degrades to ``(ERROR, [])`` and the adapter
    preserves that as the overall status.

    Validates: Requirements 6.5
    """
    tests_dir = tmp_path / "tests"
    output_dir = tests_dir / f"output-{STEM}"
    executor = _RecordingExecutor(0, output_dir=output_dir, result_md=MALFORMED_RESULT_MD)
    runner = _make_runner(tests_dir, executor)

    result = runner.run(ASSERTION, BASE_URL)

    assert result.status is StepStatus.ERROR
    assert result.steps == []
    # The malformed text is still surfaced verbatim (it WAS present on disk).
    assert result.raw_result_md == MALFORMED_RESULT_MD


# ---------------------------------------------------------------------------
# Adapter uses the injected executor (no real binary/network) + command shape
# ---------------------------------------------------------------------------
def test_uses_injected_executor_not_real_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The adapter drives the injected executor (never the real binary or the
    network), authors a committable ``<stem>_test.md``, and builds the
    documented Kane command.

    A guard monkeypatches :func:`subprocess.run` to fail loudly, PROVING the
    injected executor fully stubs the binary — no real subprocess ever runs.

    Validates: Requirements 6.3
    """

    def _boom(*args, **kwargs):  # pragma: no cover - only fires on regression
        raise AssertionError(
            "subprocess.run must NOT be called: the injected executor should "
            "stub kane-cli entirely (no real binary / network)."
        )

    monkeypatch.setattr(kane_module.subprocess, "run", _boom)

    tests_dir = tmp_path / "tests"
    output_dir = tests_dir / f"output-{STEM}"
    executor = _RecordingExecutor(0, output_dir=output_dir, result_md=_passed_result_md())
    runner = _make_runner(tests_dir, executor, kane_bin="kane-cli")

    result = runner.run(ASSERTION, BASE_URL)

    # The injected executor was called exactly once (the real binary was not).
    assert len(executor.calls) == 1
    cmd, timeout = executor.calls[0]

    # Command construction: kane-cli testmd run <test.md> --agent --headless
    # --timeout <s> --max-steps <n> (KaneRunner defaults: headless, 300s, 30).
    assert cmd[0] == "kane-cli"
    assert cmd[1:3] == ["testmd", "run"]
    assert "--agent" in cmd
    assert "--headless" in cmd
    assert "--timeout" in cmd
    assert "--max-steps" in cmd
    assert timeout == pytest.approx(300.0)

    # The authored, committable test file path is part of the command...
    test_md = tests_dir / f"{STEM}_test.md"
    assert str(test_md) in cmd

    # ...and it was actually written to tests_dir with the expected frontmatter
    # and the base_url navigation step.
    assert test_md.is_file()
    content = test_md.read_text(encoding="utf-8")
    assert "mode: testing" in content
    assert "max_steps: 30" in content
    assert BASE_URL in content

    # Sanity: with the stubbed binary the run still produced a parsed result.
    assert result.status is StepStatus.PASSED
