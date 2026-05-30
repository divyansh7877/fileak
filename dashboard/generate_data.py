#!/usr/bin/env python3
"""Generate the dashboard's static data file (``dashboard/data.js``).

Combines two sources into a single ``window.FILEAK_DATA`` object the static
dashboard reads (no backend, no fetch/CORS issues — works from ``file://``):

1. The pytest suite outcome (offline tests only), captured via pytest's built-in
   JUnit XML so no extra plugin is needed.
2. The latest chaos ``run_report.json`` produced by ``examples/run_chaos.py``,
   enriched with each profile's target package / behavior metadata.

Usage::

    python dashboard/generate_data.py
    python dashboard/generate_data.py --chaos-report examples/.chaos_output/run_report.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "examples"))


def _profile_metadata() -> dict[str, dict]:
    """Return ``{profile_name: {target_package, behavior, description}}``.

    Imported from the chaos example so the dashboard stays in sync with the
    actual experiment definition rather than duplicating it.
    """
    try:
        import run_chaos  # type: ignore

        return {
            p.name: {
                "target_package": p.target_package,
                "behavior": p.behavior.value,
                "description": p.description,
            }
            for p in run_chaos.PROFILES
        }
    except Exception:
        return {}


def run_pytest_junit() -> dict:
    """Run the offline test suite and parse its JUnit XML into a summary."""
    with tempfile.TemporaryDirectory() as td:
        xml_path = Path(td) / "junit.xml"
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "--ignore=tests/test_live_kane_smoke.py",
                f"--junit-xml={xml_path}",
                "-q",
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        if not xml_path.is_file():
            return {
                "total": 0,
                "passed": 0,
                "failed": 0,
                "skipped": 0,
                "duration_s": 0.0,
                "files": [],
                "error": proc.stdout[-2000:] + proc.stderr[-2000:],
            }
        return _parse_junit(xml_path)


def _classify(case: ET.Element) -> str:
    """Map a JUnit <testcase> to passed/failed/skipped/error."""
    for child in case:
        tag = child.tag.lower()
        if tag in ("failure", "error"):
            return "failed"
        if tag == "skipped":
            return "skipped"
    return "passed"


def _suite_name(classname: str) -> str:
    """Derive a readable test-file/module name from a JUnit classname.

    pytest classnames look like ``tests.test_chaos_revert_property`` (and may
    append ``.ClassName``); we keep the ``test_*`` module segment.
    """
    parts = classname.split(".")
    for part in parts:
        if part.startswith("test_"):
            return part
    return parts[-1] if parts else classname


def _parse_junit(xml_path: Path) -> dict:
    """Parse JUnit XML into totals + per-file grouping with each test outcome."""
    root = ET.parse(xml_path).getroot()
    # JUnit root may be <testsuites> wrapping <testsuite>, or a single suite.
    suites = root.findall("testsuite") or [root]

    files: dict[str, dict] = {}
    total = passed = failed = skipped = 0
    duration = 0.0

    for suite in suites:
        duration += float(suite.get("time", 0.0) or 0.0)
        for case in suite.findall("testcase"):
            outcome = _classify(case)
            fname = _suite_name(case.get("classname", ""))
            entry = files.setdefault(
                fname,
                {"name": fname, "passed": 0, "failed": 0, "skipped": 0, "tests": []},
            )
            entry[outcome] = entry.get(outcome, 0) + 1
            entry["tests"].append(
                {
                    "name": case.get("name", "?"),
                    "outcome": outcome,
                    "time": round(float(case.get("time", 0.0) or 0.0), 3),
                }
            )
            total += 1
            if outcome == "passed":
                passed += 1
            elif outcome == "skipped":
                skipped += 1
            else:
                failed += 1

    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "duration_s": round(duration, 2),
        "files": sorted(files.values(), key=lambda f: f["name"]),
    }


def load_chaos(report_path: Path) -> dict:
    """Load a chaos run_report.json and enrich profiles with metadata."""
    if not report_path.is_file():
        return {}
    data = json.loads(report_path.read_text(encoding="utf-8"))
    meta = _profile_metadata()
    # Prefer the LLM plan's profile metadata when present (it carries rationale
    # and the actual LLM-chosen package/behavior), falling back to the static
    # example profiles.
    plan = _load_optional_json(report_path.parent / "llm_plan.json")
    plan_meta = {}
    if plan:
        for p in plan.get("profiles", []):
            plan_meta[p["name"]] = {
                "target_package": p.get("target_package"),
                "behavior": p.get("behavior"),
                "description": p.get("description"),
                "rationale": p.get("rationale", ""),
                "custom_source": p.get("custom_source"),
            }
    data["profiles"] = [
        {"name": name, **(plan_meta.get(name) or meta.get(name, {}))}
        for name in data.get("profiles_run", [])
    ]
    return data


def _load_optional_json(path: Path) -> dict | None:
    """Read a JSON file if it exists, else None (never raises)."""
    try:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    return None


# Max chars of any single raw artifact embedded in the dashboard (keeps data.js
# from ballooning if a report is huge).
_MAX_ARTIFACT_CHARS = 60000


def _read_text(path: Path) -> str | None:
    """Read a text file (truncated), or None if absent/unreadable."""
    try:
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            if len(text) > _MAX_ARTIFACT_CHARS:
                text = text[:_MAX_ARTIFACT_CHARS] + "\n… [truncated]"
            return text
    except OSError:
        pass
    return None


def _collect_artifacts(out_dir: Path) -> list[dict]:
    """Gather the raw run artifacts as {name, kind, content} for the viewer.

    Surfaces exactly the files that drive a run so the dashboard can show the
    real text/JSON flowing through the pipeline: the human-readable report, the
    machine report, the LLM plan, and the fix suggestions. Each is returned with
    its language hint for syntax-friendly rendering.
    """
    specs = [
        ("report.md", "markdown", "Human-readable run report"),
        ("run_report.json", "json", "Machine-readable findings + exit code"),
        ("llm_plan.json", "json", "LLM-authored chaos plan (profiles + assertions)"),
        ("fix_suggestions.json", "json", "LLM remediation advice"),
    ]
    artifacts: list[dict] = []
    for fname, kind, desc in specs:
        content = _read_text(out_dir / fname)
        if content is not None:
            artifacts.append(
                {"name": fname, "kind": kind, "description": desc, "content": content}
            )
    return artifacts


def _llm_prompts(out_dir: Path) -> dict | None:
    """Load the captured LLM prompt log if the run wrote one (real-time panel)."""
    return _load_optional_json(out_dir / "llm_prompts.json")


def build_payload(chaos_report: Path, tests: dict) -> dict:
    """Assemble the full dashboard payload from a chaos report + test summary.

    Pure data assembly (no pytest run, no file writes) so it can be called
    cheaply on every watch tick. ``tests`` is passed in so the (slow) suite is
    run at most once per invocation/watch session.
    """
    chaos = load_chaos(chaos_report)
    out_dir = chaos_report.parent
    plan = _load_optional_json(out_dir / "llm_plan.json")
    advice = _load_optional_json(out_dir / "fix_suggestions.json")
    artifacts = _collect_artifacts(out_dir)
    prompts = _llm_prompts(out_dir)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tests": tests,
        "chaos": chaos,
        "plan": plan,
        "advice": advice,
        "artifacts": artifacts,
        "prompts": prompts,
    }


def write_outputs(payload: dict, out: Path) -> Path:
    """Write data.js (file:// fallback) + data.json (fetch/polling). Returns json path."""
    out.write_text(
        "window.FILEAK_DATA = " + json.dumps(payload, indent=2) + ";\n",
        encoding="utf-8",
    )
    json_out = out.with_suffix(".json")
    json_out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return json_out


def _empty_tests() -> dict:
    return {"total": 0, "passed": 0, "failed": 0, "skipped": 0, "duration_s": 0.0, "files": []}


def _watch(chaos_report: Path, out: Path, tests: dict, interval: float) -> int:
    """Rebuild the dashboard data whenever the chaos output changes.

    Polls the chaos output directory's mtimes; on any change (a new run writing
    run_report.json / llm_plan.json / fix_suggestions.json / llm_prompts.json),
    rebuilds the payload and rewrites data.js + data.json. The test summary is
    captured once at start and reused, so watch ticks stay fast.
    """
    out_dir = chaos_report.parent
    watched = [
        chaos_report,
        out_dir / "llm_plan.json",
        out_dir / "fix_suggestions.json",
        out_dir / "llm_prompts.json",
    ]

    def signature() -> tuple:
        sig = []
        for p in watched:
            try:
                sig.append((p.name, p.stat().st_mtime_ns))
            except OSError:
                sig.append((p.name, 0))
        return tuple(sig)

    print(f"dashboard: watching {out_dir} (every {interval}s) — Ctrl-C to stop")
    last = None
    try:
        while True:
            sig = signature()
            if sig != last:
                payload = build_payload(chaos_report, tests)
                write_outputs(payload, out)
                leaks = payload.get("chaos", {}).get("leaks_found", 0)
                stamp = datetime.now().strftime("%H:%M:%S")
                print(f"dashboard: [{stamp}] refreshed ({leaks} leak(s))")
                last = sig
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\ndashboard: watch stopped")
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate dashboard data.js/.json")
    parser.add_argument(
        "--chaos-report",
        type=Path,
        default=REPO_ROOT / "examples" / ".chaos_output" / "run_report.json",
    )
    parser.add_argument(
        "--out", type=Path, default=Path(__file__).resolve().parent / "data.js"
    )
    parser.add_argument(
        "--skip-tests",
        action="store_true",
        help="Skip running pytest (reuse nothing; emit empty test summary).",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Keep running, rebuilding data when a new chaos run writes output.",
    )
    parser.add_argument(
        "--interval", type=float, default=2.0,
        help="Watch poll interval in seconds (default 2).",
    )
    args = parser.parse_args(argv)

    print("dashboard: running test suite..." if not args.skip_tests else "dashboard: skipping tests")
    tests = _empty_tests()
    if not args.skip_tests:
        tests = run_pytest_junit()

    if args.watch:
        # Build once immediately, then poll for changes.
        payload = build_payload(args.chaos_report, tests)
        write_outputs(payload, args.out)
        print(f"dashboard: initial build done ({tests['passed']}/{tests['total']} tests)")
        return _watch(args.chaos_report, args.out, tests, args.interval)

    payload = build_payload(args.chaos_report, tests)
    json_out = write_outputs(payload, args.out)
    print(
        f"dashboard: wrote {args.out.name} + {json_out.name} "
        f"({tests['passed']}/{tests['total']} tests passed, "
        f"{payload.get('chaos', {}).get('leaks_found', 0)} leak(s))"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
