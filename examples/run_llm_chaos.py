#!/usr/bin/env python3
"""LLM-driven chaos run: Claude plans the chaos, Kane judges it, Claude advises fixes.

Pipeline:

  1. PLAN     — ChaosPlanner asks Claude to author N chaos experiments for the
                target (which dep to break, how, and tailored assertions). With
                ``--allow-custom-code`` the model also writes the mock JS, each
                validated by the mock-guard safety gate before it can run.
  2. RUN      — the existing fileak Orchestrator injects each mock, boots the
                app, drives Kane (real or ``--fake-kane``), inverts the verdict,
                and ALWAYS restores the target to baseline.
  3. ADVISE   — for each detected leak, RemediationAdvisor asks Claude for a
                concrete minimal fix.
  4. EMIT     — writes run_report.json + report.md (reporter) plus llm_plan.json
                and fix_suggestions.json for the dashboard.

The LLM never judges leaks — Kane does. Requires ANTHROPIC_API_KEY in the env or
a local .env (loaded automatically). Use ``--fake-kane`` to develop the LLM
pipeline offline/free (HTTP page scan instead of the browser agent).

Usage::

    python examples/run_llm_chaos.py --target examples/vulnerable-shop --port 3000
    python examples/run_llm_chaos.py --target examples/vulnerable-shop --fake-kane \
        --count 6 --allow-custom-code
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fileak.app_runner import AppRunner
from fileak.baseline import BaselineGuard
from fileak.chaos import ChaosMutator
from fileak.config import index_profiles, validate_config
from fileak.kane import KaneRunner
from fileak.llm.advisor import RemediationAdvisor
from fileak.llm.planner import DEFAULT_MODEL, ChaosPlanner, PlannerError
from fileak.models import EngineConfig, KaneResult, LeakPattern, SecurityAssertion, StepStatus
from fileak.orchestrator import Orchestrator
from fileak.reporter import DEFAULT_LEAK_PATTERNS, RatchetReporter

EXTRA_LEAK_PATTERNS = [LeakPattern("demo_secret", r"s3cr3t-[\w-]+", "high")]


class HttpFakeKaneRunner:
    """Offline stand-in for Kane: GET the page, expose body for leak scanning."""

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
            body, status = f"fetch error: {exc}", StepStatus.ERROR
        return KaneResult(status, [], body, output_dir, body)


def reinstall(target: Path) -> None:
    try:
        subprocess.run(
            ["npm", "install", "--no-audit", "--no-fund"],
            cwd=str(target), check=False, capture_output=True, timeout=180,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LLM-driven fileak chaos run.")
    parser.add_argument("--target", type=Path, default=Path("examples/vulnerable-shop"))
    parser.add_argument("--port", type=int, default=3000)
    parser.add_argument("--count", type=int, default=5, help="experiments to plan")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--allow-custom-code", action="store_true",
                        help="let the LLM author mock JS (validated by the safety gate)")
    parser.add_argument("--fake-kane", action="store_true",
                        help="offline HTTP-scan mode instead of real Kane")
    parser.add_argument("--no-advice", action="store_true", help="skip remediation step")
    parser.add_argument("--refresh-plan", action="store_true", help="ignore cached plan")
    parser.add_argument("--output-dir", type=Path, default=Path("examples/.chaos_output"))
    args = parser.parse_args(argv)

    target = args.target.resolve()
    output_base = args.output_dir.resolve()
    output_base.mkdir(parents=True, exist_ok=True)
    leak_patterns = DEFAULT_LEAK_PATTERNS + EXTRA_LEAK_PATTERNS

    # 1. PLAN -----------------------------------------------------------------
    print(f"fileak: planning chaos with {args.model} "
          f"(custom_code={'on' if args.allow_custom_code else 'off'})...")
    planner = ChaosPlanner(
        model=args.model,
        allow_custom_code=args.allow_custom_code,
        cache_dir=output_base / "plans",
    )
    try:
        plan = planner.plan(target, count=args.count, refresh=args.refresh_plan)
    except PlannerError as exc:
        print(f"fileak: planning failed: {exc}", file=sys.stderr)
        return 2

    print(f"fileak: planned {len(plan.profiles)} experiment(s); "
          f"{len(plan.dropped)} dropped. {plan.summary}")
    for d in plan.dropped:
        print(f"  - dropped {d.name} ({d.target_package}): {d.reason}")

    (output_base / "llm_plan.json").write_text(
        json.dumps(plan.to_dict(), indent=2), encoding="utf-8"
    )
    # Capture the planner's prompts for the dashboard's LLM prompts panel.
    prompt_log = list(getattr(planner, "prompt_log", []))

    # 2. RUN ------------------------------------------------------------------
    config = EngineConfig(
        target_dir=target,
        start_cmd=["npm", "run", "start"],
        install_cmd=["npm", "install"],
        port=args.port,
        readiness_path="/health",
        boot_timeout_s=90.0,
        profiles=plan.profiles,
        assertions=plan.assertions,
        tracked_files=[Path("package.json"), Path("package-lock.json")],
    )
    validate_config(config, leak_patterns)

    kane = (
        HttpFakeKaneRunner(output_base)
        if args.fake_kane
        else KaneRunner(step_timeout_s=240.0)
    )
    orchestrator = Orchestrator(
        config=config,
        mutator=ChaosMutator(config.target_dir, index_profiles(config.profiles)),
        runner=AppRunner(
            config.target_dir, config.start_cmd, config.install_cmd,
            config.port, config.readiness_path, config.boot_timeout_s,
        ),
        kane=kane,
        reporter=RatchetReporter(leak_patterns, output_dir=output_base),
        guard=BaselineGuard(config.target_dir, config.tracked_files),
    )

    mode = "fake-kane (offline)" if args.fake_kane else "real kane-cli"
    print(f"fileak: running {len(plan.profiles)} experiment(s) [{mode}]...")
    report = orchestrator.run()
    reinstall(target)
    print(f"fileak: {report.leaks_found} leak(s) across {len(report.profiles_run)} "
          f"profile(s); exit code {report.exit_code}.")

    # 3. ADVISE ---------------------------------------------------------------
    if not args.no_advice and report.leaks_found > 0:
        print("fileak: requesting remediation advice...")
        report_dict = json.loads((output_base / "run_report.json").read_text("utf-8"))
        # enrich with profile->package metadata for the advisor
        report_dict["profiles"] = [
            {"name": p.name, "target_package": p.target_package,
             "behavior": p.behavior.value, "description": p.description,
             "rationale": p.rationale}
            for p in plan.profiles
        ]
        try:
            advisor = RemediationAdvisor(model=args.model)
            advice = advisor.advise(report_dict, target)
            (output_base / "fix_suggestions.json").write_text(
                json.dumps(advice.to_dict(), indent=2), encoding="utf-8"
            )
            prompt_log.extend(getattr(advisor, "prompt_log", []))
            print(f"fileak: {len(advice.suggestions)} fix suggestion(s) written.")
            for s in advice.suggestions:
                print(f"  - [{s.severity}] {s.profile_name}: {s.summary}")
        except PlannerError as exc:
            print(f"fileak: remediation step failed (non-fatal): {exc}", file=sys.stderr)

    # Persist the LLM prompt log (plan + advise) for the dashboard prompts panel.
    (output_base / "llm_prompts.json").write_text(
        json.dumps({"model": args.model, "calls": prompt_log}, indent=2),
        encoding="utf-8",
    )

    print(f"fileak: artifacts in {output_base}")
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
