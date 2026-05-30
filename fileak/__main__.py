"""CLI entrypoint for the ``fileak`` fault-injection & leak-detection engine.

This module implements **task 11.2**: the argparse-based command-line interface
that wires every component together and runs one autonomous chaos loop, exactly
as sketched in the design's "Example Usage — CLI entrypoint".

``fileak`` is a LOCAL-ONLY developer tool (design "Security Considerations",
Requirement 13.1). It must only ever be pointed at a sandbox app the operator
controls, because it deliberately injects broken/insecure dependency mocks. It
exposes no network listener of its own.

Command-line options (Requirement 11.1)::

    python -m fileak --target ./sandbox-shop --port 3000
    python -m fileak --target ./sandbox-shop --profile broken_token_service

* ``--target`` (required) — the target app directory containing ``package.json``.
* ``--port`` (default ``3000``) — the local port the app boots on.
* ``--profile`` (optional) — run ONLY the named chaos profile (Requirement
  11.6); an unknown name fails fast with a clear, non-zero exit.

Fail-fast contract (Requirements 11.2, 11.3, 9.3):

* The config is validated via :func:`~fileak.config.validate_config` BEFORE any
  component is constructed or any file is mutated. On :class:`ConfigError` the
  actionable message is printed to stderr and the process exits ``2`` WITHOUT
  running the engine — nothing on disk is changed.
* When ``--profile`` is supplied, the config's profile list is narrowed to that
  one profile BEFORE validation, so validation still confirms that profile's
  target package exists (and an unknown profile name is rejected first).
* The process exit code is ``report.exit_code`` — ``1`` iff at least one leak
  was detected, else ``0`` (Requirement 9.3).

Stdlib only: ``argparse``, ``sys``, ``pathlib``.
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
from pathlib import Path

from fileak.app_runner import AppRunner
from fileak.baseline import BaselineGuard
from fileak.chaos import ChaosMutator
from fileak.config import (
    ConfigError,
    build_default_config,
    index_profiles,
    validate_config,
)
from fileak.kane import KaneRunner
from fileak.models import EngineConfig, RunReport
from fileak.orchestrator import Orchestrator
from fileak.reporter import DEFAULT_LEAK_PATTERNS, RatchetReporter

#: Exit code returned on a fail-fast configuration error (invalid config,
#: missing target package, unknown --profile). Distinct from the leak-detected
#: exit code (1) so automation can tell "misconfigured" from "leak found".
EXIT_CONFIG_ERROR = 2

#: Exit code returned when the engine itself raises an unexpected error. The
#: orchestrator still guarantees restore-to-baseline via its own ``finally``
#: (Requirement 10.2); this code signals the run did not complete cleanly.
EXIT_ENGINE_ERROR = 3


def _build_parser() -> argparse.ArgumentParser:
    """Construct the argparse parser for the ``fileak`` CLI.

    Defines ``--target`` (required), ``--port`` (default 3000), and the
    optional ``--profile`` filter (Requirement 11.1). The description makes the
    local-only nature of the tool explicit (design "Security Considerations").
    """
    parser = argparse.ArgumentParser(
        prog="fileak",
        description=(
            "Autonomous dependency fault-injection & leak-detection engine. "
            "A LOCAL-ONLY developer tool that injects controlled chaos into a "
            "sandbox app's dependencies, boots the mutated app, and drives Kane "
            "CLI with semantic security assertions to detect information leaks. "
            "It deliberately injects broken/insecure mocks, so only ever point "
            "it at a sandbox app you control - never production."
        ),
    )
    parser.add_argument(
        "--target",
        type=Path,
        required=True,
        help=(
            "Path to the target application directory (must contain a "
            "package.json). The engine mutates this app's dependencies and "
            "always restores them to baseline afterward."
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=3000,
        help="Local port the target app boots on (default: 3000).",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help=(
            "Run only the named chaos profile instead of all configured "
            "profiles. Useful while iterating on a single experiment."
        ),
    )
    return parser


def _apply_profile_filter(config: EngineConfig, profile_name: str) -> None:
    """Narrow ``config.profiles`` to the single named profile (Req. 11.6).

    Filtering happens BEFORE validation so the validator still confirms the
    chosen profile's target package exists in ``package.json``.

    Both the profile list AND the assertion bank are narrowed so the filtered
    config stays self-consistent: a default assertion may declare an
    ``applies_to`` that names other profiles (e.g. ``settings_graceful_failure``
    targets ``crashed_telemetry``), which would otherwise fail validation once
    those profiles are dropped. We keep only assertions applicable to the chosen
    profile (via :meth:`EngineConfig.assertions_for`, which already honours the
    "empty applies_to means all profiles" rule) and rewrite each kept
    assertion's ``applies_to`` to reference only the retained profile.

    Raises:
        ConfigError: If ``profile_name`` is not a configured profile. The
            message lists the available profile names so the operator can fix
            the ``--profile`` argument. Raised before any mutation occurs.
    """
    matching = [p for p in config.profiles if p.name == profile_name]
    if not matching:
        available = ", ".join(repr(p.name) for p in config.profiles) or "(none)"
        raise ConfigError(
            f"unknown chaos profile {profile_name!r}. Available profiles: "
            f"{available}. Pass one of these to --profile, or omit --profile "
            f"to run them all."
        )

    # Keep only the assertions applicable to the chosen profile, and rewrite
    # their applies_to so they reference only the retained profile (avoids the
    # validator rejecting references to now-dropped profiles).
    applicable = config.assertions_for(profile_name)
    config.assertions = [
        dataclasses.replace(a, applies_to=[profile_name]) for a in applicable
    ]
    config.profiles = matching


def print_summary(report: RunReport) -> None:
    """Print a concise, human-readable summary of the run to stdout.

    Renders a one-line headline ("N leaks across M profiles"), the profiles
    that were run, and a per-finding breakdown of any leaks detected. The
    machine-readable ``run_report.json`` and full ``report.md`` are written by
    the reporter; this is the at-a-glance terminal summary.
    """
    profile_count = len(report.profiles_run)
    profiles = ", ".join(report.profiles_run) if report.profiles_run else "(none)"

    leak_word = "leak" if report.leaks_found == 1 else "leaks"
    profile_word = "profile" if profile_count == 1 else "profiles"

    print(
        f"fileak: {report.leaks_found} {leak_word} across "
        f"{profile_count} {profile_word}."
    )
    print(f"  Profiles run: {profiles}")
    print(f"  Findings: {len(report.findings)}")

    # List the leak-detected findings so the operator sees what tripped without
    # opening the report files.
    leak_findings = [
        f for f in report.findings if f.verdict.value == "leak_detected"
    ]
    if leak_findings:
        print("  Leaks detected:")
        for f in leak_findings:
            indicators = (
                ", ".join(f.leak_indicators) if f.leak_indicators else "(no pattern)"
            )
            print(f"    - {f.profile_name} / {f.assertion_id}: {indicators}")

    print(f"  Exit code: {report.exit_code}")


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, validate the config, run the engine, and return a code.

    This mirrors the design's "Example Usage — CLI entrypoint" exactly:
    build the config, wire all six components into an :class:`Orchestrator`,
    run it, print a summary, and return ``report.exit_code`` (Requirement 9.3).

    Fail-fast ordering (Requirements 11.2, 11.3): the optional ``--profile``
    filter is applied and then the FULL config is validated BEFORE any
    component is constructed or any file mutated. A :class:`ConfigError` prints
    its actionable message to stderr and returns :data:`EXIT_CONFIG_ERROR`
    (``2``) without touching disk.

    Args:
        argv: Argument list to parse (defaults to ``sys.argv[1:]`` when
            ``None``). Accepting an explicit list keeps ``main`` unit-testable.

    Returns:
        ``report.exit_code`` on a completed run (``1`` iff a leak was detected,
        else ``0``); :data:`EXIT_CONFIG_ERROR` on a config error; or
        :data:`EXIT_ENGINE_ERROR` on an unexpected engine failure.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Build the default config from --target/--port. This does NOT touch disk;
    # validation (below) is the first thing that reads package.json.
    config = build_default_config(args.target, args.port)

    # Fail-fast validation BEFORE constructing or mutating anything
    # (Requirements 11.2, 11.3). Narrow to the requested profile first
    # (Requirement 11.6) so validation still checks that profile's package.
    try:
        if args.profile is not None:
            _apply_profile_filter(config, args.profile)
        validate_config(config, DEFAULT_LEAK_PATTERNS)
    except ConfigError as exc:
        print(f"fileak: configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    # Wire all components exactly as in the design's "CLI entrypoint" Example
    # Usage. Only constructed AFTER validation passes, so a bad config never
    # reaches the mutating components.
    engine = Orchestrator(
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
        kane=KaneRunner(),
        reporter=RatchetReporter(DEFAULT_LEAK_PATTERNS),
        guard=BaselineGuard(config.target_dir, config.tracked_files),
    )

    # Run the engine. The orchestrator guarantees restore-to-baseline in its own
    # finally block (Requirement 10.2) even on an unexpected error, so here we
    # only need to surface a catastrophic failure with a non-zero exit code.
    try:
        report = engine.run()
    except Exception as exc:  # noqa: BLE001 - top-level guard, report and exit
        print(
            f"fileak: engine run failed: {exc}. "
            f"The target app was restored to baseline.",
            file=sys.stderr,
        )
        return EXIT_ENGINE_ERROR

    print_summary(report)
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
