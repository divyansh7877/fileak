#!/usr/bin/env python3
"""Run a fileak chaos experiment against the vulnerable-shop target app.

This wires the real fileak engine against ``examples/vulnerable-shop`` with three
chaos profiles tuned to that app's dependencies:

    broken_token_service       jsonwebtoken  THROW_UNHANDLED   -> stack-trace leak
    crashed_telemetry          uuid          THROW_UNHANDLED   -> graceful (safe)
    compromised_input_handler  validator     LEAK_DEBUG_STATE  -> debug-state leak

Two modes:

* default (real Kane): drives the actual ``kane-cli`` agent + Chrome against the
  booted, mutated app for each assertion. Requires ``kane-cli whoami`` to
  succeed and local Chrome. Costs Kane credits and minutes.

* ``--fake-kane``: a fully offline, FREE, deterministic mode. Instead of the
  browser agent, it performs a plain HTTP GET of the page and feeds the response
  body to the SAME ``RatchetReporter`` leak-pattern scan the real run uses. This
  proves the fault-injection mechanics and populates the dashboard with zero
  credits, and is a good smoke test before the live run.

Either way the engine guarantees the target is restored to baseline afterward.
The aggregate report is written to ``--output-dir`` (default
``examples/.chaos_output``) as ``run_report.json`` + ``report.md``.

Usage::

    python examples/run_chaos.py --target examples/vulnerable-shop --port 3000
    python examples/run_chaos.py --target examples/vulnerable-shop --fake-kane
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Make the repo root importable when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fileak.app_runner import AppRunner
from fileak.baseline import BaselineGuard
from fileak.chaos import ChaosMutator
from fileak.config import index_profiles, validate_config
from fileak.kane import KaneRunner
from fileak.models import (
    ChaosProfile,
    EngineConfig,
    KaneResult,
    LeakPattern,
    MockBehavior,
    SecurityAssertion,
    StepStatus,
)
from fileak.orchestrator import Orchestrator
from fileak.reporter import DEFAULT_LEAK_PATTERNS, RatchetReporter

# --- Chaos profiles tuned to vulnerable-shop's real dependencies. ----------
PROFILES = [
    ChaosProfile(
        name="broken_token_service",
        target_package="jsonwebtoken",
        behavior=MockBehavior.THROW_UNHANDLED,
        mock_template="auth_verify_throws",
        description="JWT sign/verify throws mid-flow; the app leaks a stack trace.",
    ),
    ChaosProfile(
        name="crashed_telemetry",
        target_package="uuid",
        behavior=MockBehavior.THROW_UNHANDLED,
        mock_template="global_throw",
        description="uuid throws; telemetry should degrade gracefully (safe).",
    ),
    ChaosProfile(
        name="compromised_input_handler",
        target_package="validator",
        behavior=MockBehavior.LEAK_DEBUG_STATE,
        mock_template="echo_debug",
        description="validator echoes raw debug state; the app renders it (leak).",
    ),
]

# Semantic assertions handed to Kane (and used as the experiment axis in both
# modes). checkout_no_leak applies to all profiles; settings_graceful_failure
# only to the two THROW_UNHANDLED profiles.
ASSERTIONS = [
    SecurityAssertion(
        id="homepage_no_leak",
        prompt=(
            "Go to the homepage. Inspect the entire page. Verify that no raw "
            "stack traces, file paths, source code, database queries, secret "
            "keys, or environment variables are visible anywhere on the screen."
        ),
        applies_to=[],  # all profiles
    ),
    SecurityAssertion(
        id="graceful_degradation",
        prompt=(
            "Inspect the homepage while a backend service is degraded. Verify "
            "the affected section shows a generic, user-friendly message rather "
            "than crashing or exposing raw system objects, stack traces, or "
            "internal state."
        ),
        applies_to=["broken_token_service", "crashed_telemetry"],
    ),
]

# Extra leak pattern for demo secrets in addition to DEFAULT_LEAK_PATTERNS.
EXTRA_LEAK_PATTERNS = [
    LeakPattern("demo_secret", r"s3cr3t-[\w-]+", "high"),
]


class HttpFakeKaneRunner:
    """Offline stand-in for Kane: GET the page and expose it for leak scanning.

    The real KaneRunner drives a browser agent; this simply fetches ``base_url``
    over HTTP and returns the response body as the ``console_logs`` /
    ``raw_result_md`` of a ``KaneResult`` so the RatchetReporter's leak-pattern
    scan runs against the actual (possibly broken) page content. Status is
    PASSED unless the fetch fails (ERROR), so the verdict is driven purely by the
    leak-pattern scan — exactly the signal a clean live pass would rely on.
    """

    def __init__(self, output_base: Path) -> None:
        self._output_base = output_base
        self.calls: list[tuple[str, str]] = []

    def run(self, assertion: SecurityAssertion, base_url: str) -> KaneResult:
        self.calls.append((assertion.id, base_url))
        output_dir = self._output_base / f"output-{assertion.id}"
        try:
            with urllib.request.urlopen(base_url, timeout=10) as resp:  # noqa: S310
                body = resp.read().decode("utf-8", errors="replace")
            status = StepStatus.PASSED
        except (urllib.error.URLError, OSError) as exc:
            body = f"fetch error: {exc}"
            status = StepStatus.ERROR
        return KaneResult(
            status=status,
            steps=[],
            console_logs=body,
            output_dir=output_dir,
            raw_result_md=body,
        )


def build_config(target: Path, port: int) -> EngineConfig:
    return EngineConfig(
        target_dir=target,
        start_cmd=["npm", "run", "start"],
        install_cmd=["npm", "install"],
        port=port,
        readiness_path="/health",
        boot_timeout_s=90.0,
        profiles=PROFILES,
        assertions=ASSERTIONS,
        tracked_files=[Path("package.json"), Path("package-lock.json")],
    )


def reinstall(target: Path) -> None:
    """Relink real dependencies after the run so node_modules stays healthy.

    The engine restores package.json/package-lock.json byte-for-byte and removes
    the mock folder, but node_modules may still hold the last profile's linked
    mock. A final install repoints everything at the real packages so the target
    app is immediately runnable again.
    """
    try:
        subprocess.run(
            ["npm", "install", "--no-audit", "--no-fund"],
            cwd=str(target),
            check=False,
            capture_output=True,
            timeout=180,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a fileak chaos experiment.")
    parser.add_argument("--target", type=Path, default=Path("examples/vulnerable-shop"))
    parser.add_argument("--port", type=int, default=3000)
    parser.add_argument(
        "--fake-kane",
        action="store_true",
        help="Offline HTTP-scan mode (free, deterministic) instead of real Kane.",
    )
    parser.add_argument("--output-dir", type=Path, default=Path("examples/.chaos_output"))
    args = parser.parse_args(argv)

    target = args.target.resolve()
    leak_patterns = DEFAULT_LEAK_PATTERNS + EXTRA_LEAK_PATTERNS

    config = build_config(target, args.port)
    validate_config(config, leak_patterns)

    output_base = args.output_dir.resolve()
    output_base.mkdir(parents=True, exist_ok=True)

    kane = (
        HttpFakeKaneRunner(output_base)
        if args.fake_kane
        else KaneRunner(step_timeout_s=240.0)
    )

    orchestrator = Orchestrator(
        config=config,
        mutator=ChaosMutator(config.target_dir, index_profiles(config.profiles)),
        runner=AppRunner(
            config.target_dir,
            config.start_cmd,
            config.install_cmd,
            config.port,
            config.readiness_path,
            config.boot_timeout_s,
        ),
        kane=kane,
        reporter=RatchetReporter(leak_patterns, output_dir=output_base),
        guard=BaselineGuard(config.target_dir, config.tracked_files),
    )

    mode = "fake-kane (offline)" if args.fake_kane else "real kane-cli"
    print(f"fileak: starting chaos run against {target} [{mode}]")
    report = orchestrator.run()

    # Leave node_modules healthy for repeat runs.
    reinstall(target)

    print(
        f"fileak: {report.leaks_found} leak(s) across "
        f"{len(report.profiles_run)} profile(s); exit code {report.exit_code}."
    )
    print(f"fileak: report written to {output_base}/run_report.json and report.md")
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
