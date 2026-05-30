"""KaneRunner — adapter over the Kane CLI binary (``kane-cli``).

The :class:`KaneRunner` renders a semantic :class:`~fileak.models.SecurityAssertion`
into a committable Kane *test markdown* file, shells out to
``kane-cli testmd run <test.md> --agent`` against the running target app, and
(in task 6.2) parses the produced ``output-<stem>/Result.md`` + captured
artifacts into a :class:`~fileak.models.KaneResult`.

Scope of this module so far (task 6.1 — *test-markdown authoring & CLI
invocation*):

* Compose a committable ``<stem>_test.md`` with frontmatter
  ``mode: testing`` / ``max_steps`` / ``target`` (the browser), whose first step
  navigates to ``base_url`` followed by the assertion's natural-language steps
  (see :meth:`KaneRunner._render_test_md`). The ``<stem>`` is a timestamp-based
  session name, e.g. ``2026-05-30T19-12-00``.
* Build and invoke the Kane CLI command
  ``kane-cli testmd run <test.md> --agent`` plus ``--headless`` (when headless),
  ``--timeout <int seconds>``, and ``--max-steps`` (see
  :meth:`KaneRunner._build_command` and :meth:`KaneRunner._exec`).

The *output location, exit-code mapping, and artifact scan* into a
``KaneResult`` are deliberately isolated behind :meth:`KaneRunner._collect_result`
so task 6.2 can slot in without touching the authoring/invocation code.

Testability / offline design
----------------------------
Per the design, "these invocation details are isolated behind the KaneRunner
adapter". To keep the unit/property suites fully offline (no real binary, no
network):

* The subprocess call is funnelled through a single seam, :meth:`_exec`, which
  wraps :func:`subprocess.run`. An ``executor`` callable can be injected via the
  constructor to stub the binary entirely (used by task 6.3's mocked-binary
  tests and the integration test).
* The timestamp ``<stem>`` generator is injectable via ``stem_factory`` so tests
  can pin a deterministic session name.

Stdlib only: ``subprocess``, ``datetime``, ``pathlib``.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .models import KaneResult, SecurityAssertion, StepStatus
from .result_parser import parse_result_md

#: A callable that executes a Kane CLI command and returns its process exit
#: code. Injected for tests so the real binary is never invoked offline.
Executor = Callable[[list[str], float], int]

#: A callable returning a fresh timestamp-based session ``<stem>`` (no
#: ``_test.md`` suffix). Injected for deterministic tests.
StemFactory = Callable[[], str]

#: Kane CLI exit code used for a timeout / cancellation (0 pass, 1 fail,
#: 2 error, 3 timeout/cancel). A subprocess-level timeout is surfaced as this
#: code so the (task 6.2) mapping treats our own timeout exactly like Kane's.
_TIMEOUT_EXIT_CODE = 3

#: ``strftime`` format for the session stem, e.g. ``2026-05-30T19-12-00``.
#: Colons are replaced by hyphens in the time portion so the stem is a safe
#: filename on every platform.
_STEM_TIME_FORMAT = "%Y-%m-%dT%H-%M-%S"

#: Filename of the Kane result document inside ``output-<stem>/``.
_RESULT_MD_NAME = "Result.md"

#: File extensions treated as readable text artifacts during the best-effort
#: console/session scan. Anything else (screenshots, binaries) is skipped.
#: ``.py`` is included so the exported Playwright Python code Kane optionally
#: writes under ``playwright-python-code/`` (e.g. ``test.py``) is *consumed*
#: by the scan too — the leak scan can then see it like any other artifact
#: (task 13.3, Requirements 6.3/8.5). All files remain bounded by the per-file
#: and total byte caps below, so a large export cannot blow up the scan.
_TEXT_ARTIFACT_SUFFIXES = (".md", ".ndjson", ".log", ".txt", ".json", ".py")

#: Directory name (under ``output-<stem>/``) holding Kane's optionally-exported
#: Playwright Python code (``test.py`` + ``requirements.txt`` + ``.env.example``).
#: Present only when Kane emits it; consumed by :func:`read_artifacts` and
#: removable via :func:`clean_playwright_code`.
PLAYWRIGHT_CODE_DIR_NAME = "playwright-python-code"

#: Skip cache/internal directories during the artifact walk. ``.internal``
#: holds Kane's step caches/screenshots; ``node_modules`` etc. should never
#: appear here but are guarded defensively.
_SKIP_DIR_NAMES = frozenset({".internal", "node_modules", ".git"})

#: Per-file read cap (bytes). Keeps the scan bounded so a stray large text
#: file cannot blow up memory; artifacts of interest are tiny.
_MAX_ARTIFACT_BYTES = 256 * 1024

#: Total cap (bytes) across all concatenated artifacts for one run.
_MAX_TOTAL_ARTIFACT_BYTES = 1024 * 1024


def timestamp_session_name() -> str:
    """Return a filesystem-safe, timestamp-based session stem.

    Example: ``2026-05-30T19-12-00``. Used as the default ``<stem>`` for a Kane
    test file (``<stem>_test.md``) and its sibling output dir
    (``output-<stem>/``).
    """
    return datetime.now().strftime(_STEM_TIME_FORMAT)


def _read_text_or_none(path: Path) -> Optional[str]:
    """Return the UTF-8 text of ``path`` or ``None`` if it is absent/unreadable.

    Best-effort and total: any error (missing file, permission, decode failure)
    degrades to ``None`` rather than raising. ``errors="replace"`` keeps decoding
    resilient to stray bytes in otherwise-text artifacts.
    """
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def read_artifacts(output_dir: Path) -> str:
    """Best-effort concatenation of readable text artifacts under ``output_dir``.

    Per the design, ``console_logs`` is the ``Result.md`` body plus any session/
    run files captured by Kane (e.g. ``actions.ndjson``, run logs, exported code
    under the output dir). This walks ``output_dir`` for small text files
    (:data:`_TEXT_ARTIFACT_SUFFIXES`), skipping cache/internal directories
    (:data:`_SKIP_DIR_NAMES`, e.g. ``.internal`` screenshots/caches) and any file
    larger than :data:`_MAX_ARTIFACT_BYTES`, and concatenates their contents with
    a small ``===== <relpath> =====`` separator for readability.

    Because ``.py`` is a tracked text suffix, the exported Playwright Python code
    Kane optionally writes under ``playwright-python-code/`` (``test.py`` etc.) is
    *consumed* here when present and made available to the leak scan, and is
    simply absent (contributing nothing) when Kane did not emit it — degrading
    gracefully either way (task 13.3, Requirements 6.3/8.5).

    Totality: every filesystem operation is guarded so this NEVER raises. When
    ``output_dir`` is missing or holds no readable text, it returns ``""``. The
    scan is bounded by per-file and total byte caps so a stray large artifact
    cannot exhaust memory.
    """
    if not output_dir.is_dir():
        return ""

    parts: list[str] = []
    total = 0
    try:
        # Sort for deterministic ordering; Result.md naturally sorts ahead of
        # most siblings but order is otherwise best-effort.
        paths = sorted(output_dir.rglob("*"))
    except OSError:
        return ""

    for path in paths:
        if total >= _MAX_TOTAL_ARTIFACT_BYTES:
            break
        try:
            # Skip anything inside a cache/internal directory.
            if any(part in _SKIP_DIR_NAMES for part in path.relative_to(output_dir).parts[:-1]):
                continue
            if path.name in _SKIP_DIR_NAMES:
                continue
            if not path.is_file():
                continue
            if path.suffix.lower() not in _TEXT_ARTIFACT_SUFFIXES:
                continue
            if path.stat().st_size > _MAX_ARTIFACT_BYTES:
                continue
        except OSError:
            continue

        text = _read_text_or_none(path)
        if text is None:
            continue

        try:
            rel = path.relative_to(output_dir)
        except ValueError:
            rel = path
        chunk = f"===== {rel} =====\n{text}"
        parts.append(chunk)
        total += len(chunk)

    return "\n".join(parts)


def clean_playwright_code(output_dir: Path) -> bool:
    """Opt-in, best-effort removal of the exported Playwright code directory.

    Kane optionally writes ``output-<stem>/playwright-python-code/`` (an exported
    Playwright Python harness: ``test.py``, ``requirements.txt``, ``.env.example``).
    Operators who do not want this generated code to accumulate between runs can
    call this to remove just that subdirectory, leaving ``Result.md`` and the
    ``.internal/`` replay cache untouched.

    This is **opt-in cleanup**: the engine never calls it automatically during a
    run, so it cannot race the reporter's artifact scan (which *consumes* the
    exported code via :func:`read_artifacts`). Cleanup is meant to run after the
    reporter has already evaluated a result.

    Graceful degradation (task 13.3, Requirement 8.5): when
    ``playwright-python-code/`` is absent (Kane did not emit it, or it was
    already cleaned) this is a no-op. Every filesystem operation is guarded so
    the function NEVER raises — a permission error or odd filesystem state
    degrades to ``False`` rather than propagating.

    Args:
        output_dir: A Kane ``output-<stem>/`` directory.

    Returns:
        ``True`` if an existing ``playwright-python-code/`` directory was
        removed; ``False`` if it was absent or could not be removed.
    """
    target = output_dir / PLAYWRIGHT_CODE_DIR_NAME
    try:
        if not target.is_dir():
            return False
    except OSError:
        return False
    try:
        shutil.rmtree(target)
    except OSError:
        return False
    # Confirm removal; a partial failure leaves it present -> report False.
    try:
        return not target.exists()
    except OSError:
        return False


class KaneRunner:
    """Adapter over the Kane CLI binary.

    Renders a semantic assertion into a committable Kane test markdown file,
    invokes ``kane-cli testmd run <test.md> --agent`` against ``base_url``, and
    (task 6.2) parses ``output-<stem>/Result.md`` + captured artifacts into a
    :class:`~fileak.models.KaneResult`.

    Args:
        tests_dir: Directory the committable ``<stem>_test.md`` files are written
            into. Created on demand. Defaults to ``.testmuai/tests`` to match the
            workspace layout. ``output-<stem>/`` is produced by Kane next to the
            test file.
        kane_bin: The Kane CLI executable name/path (npm ``@testmuai/kane-cli``).
        target_browser: The ``target`` browser written into the test frontmatter
            (e.g. ``"chrome"``).
        max_steps: Upper bound on agent steps; written into the frontmatter and
            passed as ``--max-steps``.
        headless: When true, the run adds ``--headless`` to the Kane command.
        step_timeout_s: Per-run timeout in seconds; passed to Kane as
            ``--timeout`` (as an int) and used as the subprocess timeout.

    Keyword-only test seams (do not affect the public design interface):
        executor: Optional ``(cmd, timeout) -> exit_code`` callable used instead
            of :func:`subprocess.run`, so tests can stub the binary offline.
        stem_factory: Optional ``() -> str`` callable producing the session
            ``<stem>``, so tests can pin a deterministic name.
    """

    def __init__(
        self,
        tests_dir: Path = Path(".testmuai/tests"),
        kane_bin: str = "kane-cli",
        target_browser: str = "chrome",
        max_steps: int = 30,
        headless: bool = True,
        step_timeout_s: float = 300.0,
        *,
        executor: Optional[Executor] = None,
        stem_factory: Optional[StemFactory] = None,
    ) -> None:
        self.tests_dir = Path(tests_dir)
        self.kane_bin = kane_bin
        self.target_browser = target_browser
        self.max_steps = max_steps
        self.headless = headless
        self.step_timeout_s = step_timeout_s
        self._executor = executor
        self._stem_factory = stem_factory or timestamp_session_name

    # -- Public API ---------------------------------------------------------

    def run(self, assertion: SecurityAssertion, base_url: str) -> KaneResult:
        """Author a Kane test file, invoke Kane against ``base_url``, and return
        a :class:`~fileak.models.KaneResult`.

        Lifecycle (matching ``design.md`` "KaneRunner.run"):

        1. Compute a timestamp ``<stem>`` and write a committable
           ``<stem>_test.md`` whose step 1 navigates to ``base_url`` followed by
           the assertion steps.
        2. Build ``kane-cli testmd run <test.md> --agent [--headless]
           --timeout <s> --max-steps <n>`` and execute it via :meth:`_exec`.
        3. Locate ``output-<stem>/`` (next to the test file) and delegate to
           :meth:`_collect_result` to map the exit code and parse artifacts.

        Steps 1-2 are implemented here (task 6.1); step 3's parsing/exit-code
        mapping/artifact scan lives in :meth:`_collect_result` (task 6.2).
        """
        stem = self._make_stem()
        test_md = self._write_test_md(stem, base_url, assertion)

        cmd = self._build_command(test_md)
        exit_code = self._exec(cmd, self.step_timeout_s)

        # output-<stem>/ lives NEXT TO the test file; <stem> is the test
        # filename minus the "_test.md" suffix.
        output_dir = test_md.parent / f"output-{stem}"
        return self._collect_result(exit_code, output_dir, test_md)

    # -- Test-markdown authoring (task 6.1) --------------------------------

    def _make_stem(self) -> str:
        """Return the session ``<stem>`` for this run via the (injectable)
        stem factory.
        """
        return self._stem_factory()

    def _render_test_md(
        self, stem: str, base_url: str, assertion: SecurityAssertion
    ) -> str:
        """Render the committable Kane test markdown text.

        Produces frontmatter (``mode: testing`` / ``max_steps`` / ``target``)
        followed by a ``# Session: <stem>`` heading, a Step 1 that navigates to
        ``base_url`` and waits for load, and a Step 2 carrying the assertion's
        natural-language prompt. Matches the "Example Generated Kane Test File"
        in ``design.md``.

        This is a pure function of its inputs (no I/O) so tests can assert the
        exact text without writing a file or invoking a binary.
        """
        prompt = assertion.prompt.strip("\n")
        return (
            "---\n"
            "mode: testing\n"
            f"max_steps: {self.max_steps}\n"
            f"target: {self.target_browser}\n"
            "---\n"
            "\n"
            f"# Session: {stem}\n"
            "\n"
            "## Step 1\n"
            f"Go to {base_url} and wait for the page to load.\n"
            "\n"
            "## Step 2\n"
            f"{prompt}\n"
        )

    def _write_test_md(
        self, stem: str, base_url: str, assertion: SecurityAssertion
    ) -> Path:
        """Write ``<stem>_test.md`` into ``tests_dir`` and return its path.

        Creates ``tests_dir`` (and parents) on demand. The file is committable
        and human-readable; its content is produced by :meth:`_render_test_md`.
        """
        self.tests_dir.mkdir(parents=True, exist_ok=True)
        test_md = self.tests_dir / f"{stem}_test.md"
        test_md.write_text(
            self._render_test_md(stem, base_url, assertion),
            encoding="utf-8",
        )
        return test_md

    # -- CLI invocation (task 6.1) -----------------------------------------

    def _build_command(self, test_md: Path) -> list[str]:
        """Build the Kane CLI argv for an automated run.

        Always includes ``--agent`` (MANDATORY: without it Kane renders an
        interactive TUI whose output cannot be parsed). Adds ``--headless`` when
        configured, an integer ``--timeout`` (seconds), and ``--max-steps``.
        """
        cmd = [self.kane_bin, "testmd", "run", str(test_md), "--agent"]
        if self.headless:
            cmd.append("--headless")
        cmd += ["--timeout", str(int(self.step_timeout_s))]
        cmd += ["--max-steps", str(self.max_steps)]
        return cmd

    def _exec(self, cmd: list[str], timeout: float) -> int:
        """Execute a Kane CLI command and return its process exit code.

        This is the single subprocess seam for the adapter. When an ``executor``
        was injected (tests), it is used instead of :func:`subprocess.run` so the
        real binary is never invoked offline. A subprocess-level timeout is
        surfaced as exit code ``3`` (timeout/cancel) so the task-6.2 mapping
        treats it exactly like Kane's own timeout code.

        Exit codes follow Kane's convention: 0 pass, 1 fail, 2 error,
        3 timeout/cancel.
        """
        if self._executor is not None:
            return int(self._executor(cmd, timeout))
        try:
            completed = subprocess.run(cmd, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            return _TIMEOUT_EXIT_CODE
        return completed.returncode

    # -- Result collection (task 6.2 slot) ---------------------------------

    def _collect_result(
        self, exit_code: int, output_dir: Path, test_md: Path
    ) -> KaneResult:
        """Map ``exit_code`` and parse ``output-<stem>/`` artifacts into a
        :class:`~fileak.models.KaneResult` (**task 6.2**).

        Behavior (matching the ``design.md`` "KaneRunner.run" tail and
        Requirements 6.3/6.4/6.5):

        * Read ``output_dir / "Result.md"`` (``output_dir`` is already
          ``test_md.parent / "output-<stem>"``).
        * **Not judgeable** — when ``exit_code`` is 2 or 3 (auth/setup/Chrome
          error, timeout/cancel) OR ``Result.md`` is absent — return a
          ``KaneResult`` with ``status = ERROR``, empty ``steps``, and
          ``raw_result_md = ""`` when the file is missing.
        * Otherwise parse via :func:`fileak.result_parser.parse_result_md`.
          A malformed ``Result.md`` parses to ``StepStatus.ERROR`` (Req 6.5),
          which is preserved as the overall status.
        * Exit code 0 -> the parsed status (``PASSED`` for a well-formed pass);
          exit code 1 -> force ``StepStatus.FAILED`` when Kane judged the
          assertion failed but the parsed status was not already ``FAILED``
          (Req 6.4 / design ``if exit_code == 1 and status != FAILED``).
        * ``console_logs`` is a best-effort concatenation of the Result.md body
          plus any session/run text artifacts under ``output_dir`` via
          :func:`read_artifacts`, degrading to ``""`` when absent.

        This method never raises on missing/malformed artifacts.
        """
        result_md = _read_text_or_none(output_dir / _RESULT_MD_NAME)
        console = read_artifacts(output_dir)

        # exit codes 2/3 (error, timeout/cancel) or a missing Result.md are not
        # judgeable -> ERROR with no steps and empty raw text when absent.
        if exit_code in (2, 3) or result_md is None:
            return KaneResult(
                status=StepStatus.ERROR,
                steps=[],
                console_logs=console,
                output_dir=output_dir,
                raw_result_md=result_md or "",
            )

        # Result.md present: parse frontmatter status + per-step markers.
        # Malformed input degrades to (ERROR, []) per parser totality (Req 6.5).
        status, steps = parse_result_md(result_md)

        # Exit code 1 means Kane judged the assertion failed; force FAILED unless
        # the parse already said so (design: exit_code == 1 and status != FAILED).
        # A malformed Result.md (status == ERROR) stays ERROR — Req 6.5 says a
        # missing OR malformed Result.md is ERROR regardless of exit code.
        if exit_code == 1 and status not in (StepStatus.FAILED, StepStatus.ERROR):
            status = StepStatus.FAILED

        return KaneResult(
            status=status,
            steps=steps,
            console_logs=console,
            output_dir=output_dir,
            raw_result_md=result_md,
        )
