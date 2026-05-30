"""Optional *live* ``kane-cli`` smoke test (task 13.2).

Unlike the fully-offline dry-run integration test
(:mod:`tests.test_integration_dry_run`), this test drives the **real**
``kane-cli`` binary, the **real** local Chrome, and the **real** network. It
therefore CANNOT run in CI or during offline verification and is **SKIPPED by
default**. It only executes when an operator explicitly opts in.

What it validates
-----------------
A single semantic assertion is driven through the REAL
:class:`~fileak.kane.KaneRunner` against an already-running sandbox app. The
point of a smoke test is to confirm the integration plumbing is intact end to
end (Requirements 6.2, 12.2):

* ``kane-cli testmd run <stem>_test.md --agent --headless --timeout <s>`` is
  actually invoked (the ``--agent`` flag is mandatory for parseable output, and
  ``--headless`` + ``--timeout`` are required for an automated run — Requirement
  6.2).
* A committable ``<stem>_test.md`` is written and an ``output-<stem>/`` directory
  is produced next to it, whose ``Result.md`` is parsed into a
  :class:`~fileak.models.KaneResult` (Requirement 6.3).
* The returned ``KaneResult`` carries a concrete ``status`` (``PASSED`` /
  ``FAILED`` / ``ERROR`` are all acceptable for a smoke test — the goal is that
  the binary ran and a result was produced/parsed, not a particular verdict).

How to run it
-------------
The whole test is gated behind the ``FILEAK_LIVE_KANE`` environment variable and
several runtime preconditions. Nothing here touches ``kane-cli``, Chrome, or the
network unless ``FILEAK_LIVE_KANE`` is set::

    # 1. Log in to Kane out-of-band so `kane-cli whoami` succeeds (Req 12.2):
    kane-cli login ...

    # 2. Start the sandbox app yourself (the smoke test does NOT boot it — it
    #    stays focused on the Kane integration) and note its base URL:
    cd sandbox-shop && npm run start   # serving e.g. http://localhost:3000

    # 3. Run the smoke test, pointing it at the running app:
    FILEAK_LIVE_KANE=1 FILEAK_LIVE_BASE_URL=http://localhost:3000 \
        python -m pytest tests/test_live_kane_smoke.py -v -s

Preconditions checked at runtime (each skips with a clear reason when unmet):

* ``FILEAK_LIVE_KANE`` is set (module-level gate).
* ``kane-cli`` is on ``PATH`` (``shutil.which``).
* ``kane-cli whoami`` exits 0 (login completed — Requirement 12.2).
* The operator has started a reachable sandbox app; its base URL is taken from
  ``FILEAK_LIVE_BASE_URL`` (default ``http://localhost:3000``) and must answer an
  HTTP request before we spend Kane/LLM budget on it.

Generated artifacts (the ``<stem>_test.md`` and ``output-<stem>/`` directory) are
written under pytest's ``tmp_path`` so they are cleaned up automatically and
never pollute the repo's ``.testmuai/tests`` folder.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from fileak.kane import KaneRunner
from fileak.models import KaneResult, SecurityAssertion, StepStatus

# --- Module-level gate -----------------------------------------------------
# The entire module is skipped unless the operator explicitly opts in. This is
# what guarantees a normal `pytest` run (and CI / offline verification) NEVER
# invokes kane-cli, Chrome, or the network.
LIVE_FLAG = "FILEAK_LIVE_KANE"

pytestmark = pytest.mark.skipif(
    not os.environ.get(LIVE_FLAG),
    reason=(
        "live kane-cli smoke test; set FILEAK_LIVE_KANE=1 to run "
        "(requires kane-cli login + local Chrome + a running sandbox app)"
    ),
)

#: Where the operator's running sandbox app is reachable. The smoke test does
#: NOT boot the app itself — it assumes the operator started it — so this is the
#: single knob needed to point Kane at the target.
BASE_URL_ENV = "FILEAK_LIVE_BASE_URL"
DEFAULT_BASE_URL = "http://localhost:3000"

#: A bounded per-run Kane timeout (seconds) for the smoke test, kept modest so a
#: stuck agent cannot stall an interactive demo run.
SMOKE_TIMEOUT_S = 180.0

#: How long to wait when probing the operator-provided base URL for liveness.
_PROBE_TIMEOUT_S = 5.0

#: A single, simple assertion for the smoke test: navigate and do a trivial
#: visual check. The point is to exercise the real KaneRunner plumbing, not to
#: assert a particular security verdict.
SMOKE_ASSERTION = SecurityAssertion(
    id="smoke_page_loads",
    prompt=(
        "Confirm the page has loaded and is showing content. Verify that no raw "
        "stack traces, file paths, database queries, or environment variables "
        "are visible anywhere on the screen."
    ),
    applies_to=[],
)


def _whoami_succeeds(kane_bin: str) -> bool:
    """Return True iff ``kane-cli whoami`` exits 0 (login completed, Req 12.2).

    Best-effort: any failure to even launch the binary (missing, crash, hang)
    is treated as "not logged in" so the test skips with a clear reason rather
    than erroring.
    """
    try:
        completed = subprocess.run(
            [kane_bin, "whoami"],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _base_url_is_reachable(base_url: str) -> bool:
    """Return True iff an HTTP request to ``base_url`` gets any response.

    We only need to confirm the operator actually started the sandbox app
    before we spend real Kane/LLM budget on it; any HTTP status (even 4xx/5xx)
    counts as "reachable". A connection error / timeout means it is not up.
    """
    try:
        with urllib.request.urlopen(base_url, timeout=_PROBE_TIMEOUT_S):  # noqa: S310
            return True
    except urllib.error.HTTPError:
        # The server answered (e.g. 404/500) — it is up and reachable.
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


@pytest.fixture
def kane_bin() -> str:
    """The ``kane-cli`` executable, or skip if it is not on PATH."""
    found = shutil.which("kane-cli")
    if found is None:
        pytest.skip("kane-cli not found on PATH; install @testmuai/kane-cli to run")
    return found


@pytest.fixture
def logged_in(kane_bin: str) -> str:
    """Require a completed Kane login (``kane-cli whoami`` exits 0, Req 12.2)."""
    if not _whoami_succeeds(kane_bin):
        pytest.skip(
            "`kane-cli whoami` failed; run `kane-cli login` first (Requirement 12.2)"
        )
    return kane_bin


@pytest.fixture
def base_url() -> str:
    """The operator's already-running sandbox base URL, or skip if not reachable.

    The smoke test deliberately does NOT boot the app; the operator starts it
    and points the test at it via ``FILEAK_LIVE_BASE_URL`` (default
    ``http://localhost:3000``).
    """
    url = os.environ.get(BASE_URL_ENV, DEFAULT_BASE_URL)
    if not _base_url_is_reachable(url):
        pytest.skip(
            f"sandbox app not reachable at {url}; start it and/or set "
            f"{BASE_URL_ENV} (the smoke test does not boot the app itself)"
        )
    return url


def test_live_kane_smoke(
    logged_in: str,
    base_url: str,
    tmp_path: Path,
):
    """Drive ONE assertion through the real KaneRunner against a live sandbox.

    Validates Requirements 6.2 and 12.2: with login completed, the engine
    invokes ``kane-cli testmd run <test.md> --agent --headless --timeout`` and
    parses ``output-<stem>/Result.md`` into a :class:`KaneResult`. Any concrete
    ``StepStatus`` (PASSED/FAILED/ERROR) is acceptable for a smoke test — the
    point is that the binary actually ran and a result was produced/parsed.
    """
    tests_dir = tmp_path / "testmuai" / "tests"

    runner = KaneRunner(
        tests_dir=tests_dir,
        kane_bin=logged_in,
        headless=True,
        step_timeout_s=SMOKE_TIMEOUT_S,
    )

    result = runner.run(SMOKE_ASSERTION, base_url)

    # The real binary was invoked and a result was parsed (Requirement 6.3).
    assert isinstance(result, KaneResult)
    assert isinstance(result.status, StepStatus)

    # A committable <stem>_test.md was authored (Requirement 6.1/6.2)...
    written_tests = list(tests_dir.glob("*_test.md"))
    assert written_tests, "KaneRunner did not author a <stem>_test.md file"

    # ...and an output-<stem>/ directory was produced next to it (Requirement 6.3).
    # When Kane errored before producing output (status ERROR), the directory may
    # be absent — that is still a valid smoke outcome, so only assert the dir when
    # Kane got far enough to produce a result.
    if result.status is not StepStatus.ERROR:
        assert result.output_dir.is_dir(), (
            f"expected Kane output dir at {result.output_dir}"
        )
