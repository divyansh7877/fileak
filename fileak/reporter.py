"""Verdict engine: leak-pattern scanning + inverted security verdicts.

This module implements Component 5 (``RatchetReporter``) from the design. The
reporter translates a Kane CLI result plus best-effort captured logs into a
single security :class:`~fileak.models.Finding`, scanning the combined text
for sensitive-data leak indicators.

The core idea is the *inverted* pass/fail semantics: a Kane assertion that
"passes" (no leak visible) is a GOOD outcome, while a Kane assertion that
"fails", or that surfaces raw leak indicators in the captured logs/output, is a
DETECTED vulnerability. See the design's "RatchetReporter.evaluate (the
ratchet)" pseudocode and Requirements 7.1-7.4, 8.1-8.5, and 13.4.

This module is stdlib-only (``re``). The leak-pattern regexes are precompiled
in :meth:`RatchetReporter.__init__` so each ``evaluate`` call is a tight scan.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from fileak.models import (
    Finding,
    KaneResult,
    LeakPattern,
    RunReport,
    SecurityAssertion,
    StepStatus,
    Verdict,
)

# Maximum length of an individual evidence snippet. Evidence is kept LOCAL and
# truncated so a leaked secret/env value is never copied wholesale into a
# report (Requirements 8.4, 13.4).
MAX_EVIDENCE_SNIPPET_LEN = 200

# Maximum number of evidence snippets attached to a single Finding. Caps the
# size of a Finding even when a pattern matches many times (design uses
# ``evidence[:MAX_EVIDENCE]``).
MAX_EVIDENCE = 20

# Default LOCAL output directory for written reports. Reports are written ONLY
# under this local directory (Requirement 13.3); evidence snippets are kept
# local and truncated (Requirements 8.4, 13.4). Injectable via the
# ``RatchetReporter`` constructor so tests can point it at a temp dir.
DEFAULT_OUTPUT_DIR = Path("fileak_output")

# Default leak patterns covering stack traces, Node error prefixes, file
# paths, SQL queries, environment-variable assignments, and secret keys
# (Requirement 8.2). Copied verbatim from the design's "Default Leak Patterns"
# section. These are validated as compilable regexes at config-load time
# (task 11.1); the reporter also degrades defensively (see ``__init__``).
DEFAULT_LEAK_PATTERNS: list[LeakPattern] = [
    LeakPattern("stack_trace", r"at\s+\w+.*\(.*:\d+:\d+\)", "high"),
    LeakPattern("node_stack", r"\b(Error|TypeError|ReferenceError):\s", "high"),
    LeakPattern("file_path", r"(/[\w.-]+){3,}|[A-Za-z]:\\(?:[\w.-]+\\){2,}", "medium"),
    LeakPattern("sql_query", r"\b(SELECT|INSERT|UPDATE|DELETE)\b\s+.*\b(FROM|INTO|SET)\b", "high"),
    LeakPattern("env_var", r"\b[A-Z][A-Z0-9_]{3,}=(?:[^\s]+)", "high"),
    LeakPattern("secret_key", r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*\S+", "high"),
]


def _dedupe(labels: list[str]) -> list[str]:
    """Return ``labels`` with duplicates removed, preserving first-seen order.

    The design's ``dedupe(matched)`` collapses repeated pattern labels into a
    stable list of distinct leak indicators for the Finding.
    """
    seen: set[str] = set()
    out: list[str] = []
    for label in labels:
        if label not in seen:
            seen.add(label)
            out.append(label)
    return out


class RatchetReporter:
    """Maps Kane results + captured logs into inverted security verdicts.

    Constructor per design: ``RatchetReporter(leak_patterns)``. The leak
    patterns are precompiled once so each :meth:`evaluate` call performs a
    tight scan over the combined haystack.

    ``output_dir`` is keyword-only and defaults to a LOCAL directory
    (:data:`DEFAULT_OUTPUT_DIR`); :meth:`finalize` writes ``run_report.json``
    and ``report.md`` there and nowhere else (Requirement 13.3). Tests inject
    a temp dir via this argument.

    The run start is recorded at construction time: ``started_at`` (ISO-8601
    string) and a monotonic baseline used to compute ``duration_s`` in
    :meth:`finalize`, since ``finalize`` itself receives no timing.
    """

    def __init__(
        self,
        leak_patterns: list[LeakPattern],
        *,
        output_dir: Path = DEFAULT_OUTPUT_DIR,
    ) -> None:
        self.leak_patterns = leak_patterns
        self.output_dir = Path(output_dir)
        # Record the run start so finalize() can compute duration without
        # being handed timing. started_at is a human/machine ISO-8601 string;
        # the monotonic baseline avoids wall-clock skew when measuring elapsed.
        self.started_at = datetime.now(timezone.utc).isoformat()
        self._start_monotonic = time.monotonic()
        # Precompile each pattern (design: "leak_patterns precompiled").
        # Invalid regexes are validated/rejected at config-load time
        # (task 11.1); here we are defensive — a pattern that fails to compile
        # is skipped rather than allowed to crash evaluate().
        self._compiled: list[tuple[LeakPattern, re.Pattern[str]]] = []
        for lp in leak_patterns:
            try:
                self._compiled.append((lp, re.compile(lp.pattern)))
            except re.error:
                # Defensive: skip an uncompilable pattern (should not happen
                # once config validation lands in task 11.1).
                continue

    @staticmethod
    def _snippet(match: re.Match[str]) -> str:
        """Return a truncated evidence snippet for a regex match.

        Keeps evidence LOCAL and bounded by truncating the matched text to
        :data:`MAX_EVIDENCE_SNIPPET_LEN` characters (Requirements 8.4, 13.4).
        """
        text = match.group(0) or ""
        if len(text) > MAX_EVIDENCE_SNIPPET_LEN:
            text = text[:MAX_EVIDENCE_SNIPPET_LEN]
        return text

    def evaluate(
        self,
        profile_name: str,
        assertion: SecurityAssertion,
        kane: KaneResult,
        app_stderr: str,
    ) -> Finding:
        """Combine Kane's status with leak-pattern scanning into one Finding.

        Builds a haystack from the combined Kane console logs, the target app
        stderr, and the ``Result.md`` body (Requirement 8.1), scans it against
        every configured leak pattern (Requirement 8.3), and produces exactly
        one :class:`Finding` (Requirement 7.1):

        - ``INCONCLUSIVE`` when Kane errored (Requirement 7.2).
        - ``LEAK_DETECTED`` when Kane failed the assertion OR any leak pattern
          matched (Requirements 7.3, 8.3).
        - ``SAFE`` otherwise — Kane passed and no leak matched (Requirement
          7.4).

        Degrades gracefully when artifacts are absent: missing/None-ish text
        is treated as empty and the method never raises (Requirement 8.5).
        """
        # Degrade gracefully when artifacts are absent (Requirement 8.5):
        # treat any missing/None-ish field as an empty string.
        console_logs = kane.console_logs or ""
        raw_result_md = kane.raw_result_md or ""
        stderr = app_stderr or ""

        # Combined Kane console logs + app stderr + Result.md body
        # (Requirement 8.1).
        haystack = console_logs + "\n" + stderr + "\n" + raw_result_md

        matched: list[str] = []
        evidence: list[str] = []
        for lp, regex in self._compiled:
            for m in regex.finditer(haystack):
                matched.append(lp.label)
                evidence.append(self._snippet(m))

        # Inverted verdict mapping (Requirements 7.2, 7.3, 7.4).
        if kane.status == StepStatus.ERROR:
            verdict = Verdict.INCONCLUSIVE
        elif kane.status == StepStatus.FAILED or matched:
            verdict = Verdict.LEAK_DETECTED
        else:
            verdict = Verdict.SAFE

        return Finding(
            profile_name=profile_name,
            assertion_id=assertion.id,
            verdict=verdict,
            kane_status=kane.status,
            leak_indicators=_dedupe(matched),
            evidence=evidence[:MAX_EVIDENCE],
            output_dir=kane.output_dir,
        )

    def finalize(self, findings: list[Finding]) -> RunReport:
        """Aggregate ``findings`` into a :class:`RunReport` and write reports.

        Aggregation (Requirements 9.2, 9.3):

        - ``leaks_found`` = number of findings whose verdict is
          ``LEAK_DETECTED``. The :attr:`RunReport.exit_code` property already
          returns ``1`` iff ``leaks_found > 0``, so setting this count is
          sufficient for the exit-code contract (Requirement 9.3).
        - ``profiles_run`` = the distinct profile names appearing in
          ``findings``, in first-seen order.
        - ``started_at`` is the ISO timestamp captured at construction;
          ``duration_s`` is the elapsed monotonic time since then.

        This method does NOT drop or synthesize findings — it aggregates
        exactly the list it is given (the orchestrator is responsible for
        building one Finding per attempted (profile, assertion) pair plus one
        ``INCONCLUSIVE`` per boot failure, Requirement 9.2).

        Writes a machine-readable ``run_report.json`` and a human-readable
        ``report.md`` to the LOCAL output directory only (Requirements 9.1,
        13.3) and returns the :class:`RunReport`.
        """
        leaks_found = sum(
            1 for f in findings if f.verdict == Verdict.LEAK_DETECTED
        )

        profiles_run: list[str] = []
        seen: set[str] = set()
        for f in findings:
            if f.profile_name not in seen:
                seen.add(f.profile_name)
                profiles_run.append(f.profile_name)

        duration_s = max(0.0, time.monotonic() - self._start_monotonic)

        report = RunReport(
            findings=findings,
            profiles_run=profiles_run,
            leaks_found=leaks_found,
            started_at=self.started_at,
            duration_s=duration_s,
        )

        # Write reports to the LOCAL output dir only (Requirements 9.1, 13.3).
        # Create the dir on demand; never write anywhere else.
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._write_json(report)
        self._write_markdown(report)

        return report

    @staticmethod
    def _enum_value(value: object) -> object:
        """Return ``value.value`` for an enum, else ``value`` unchanged.

        Defensive helper so serialization never crashes on an unexpected type
        (Requirement: never crash on serialization).
        """
        if isinstance(value, Enum):
            return value.value
        return value

    @classmethod
    def _finding_to_dict(cls, finding: Finding) -> dict[str, object]:
        """Convert a :class:`Finding` into a JSON-serializable dict.

        Enums (``Verdict``, ``StepStatus``) become their ``.value`` strings
        and the ``output_dir`` :class:`~pathlib.Path` becomes ``str`` (or
        ``None``). Robust to missing/odd values.
        """
        output_dir = finding.output_dir
        return {
            "profile_name": finding.profile_name,
            "assertion_id": finding.assertion_id,
            "verdict": cls._enum_value(finding.verdict),
            "kane_status": cls._enum_value(finding.kane_status),
            "leak_indicators": list(finding.leak_indicators or []),
            "evidence": list(finding.evidence or []),
            "output_dir": str(output_dir) if output_dir is not None else None,
        }

    def _write_json(self, report: RunReport) -> None:
        """Serialize ``report`` to ``run_report.json`` in the output dir.

        Enums are converted to their ``.value`` and Paths to ``str`` so the
        output is valid JSON (``json.dump(indent=2)``). The ``exit_code``
        property is included for convenient automation consumption.
        """
        payload = {
            "started_at": report.started_at,
            "duration_s": report.duration_s,
            "profiles_run": list(report.profiles_run),
            "leaks_found": report.leaks_found,
            "exit_code": report.exit_code,
            "findings": [self._finding_to_dict(f) for f in report.findings],
        }
        json_path = self.output_dir / "run_report.json"
        with json_path.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, default=str)

    def _write_markdown(self, report: RunReport) -> None:
        """Write a human-readable ``report.md`` summary to the output dir.

        Renders a title, run timestamp/duration, profiles run, and total leaks
        found, followed by one section per finding (profile, assertion id,
        verdict, kane_status, leak_indicators, and truncated evidence).
        Evidence is already truncated by :meth:`evaluate` and is kept LOCAL
        (Requirements 8.4, 13.4).
        """
        lines: list[str] = []
        lines.append("# fileak run report")
        lines.append("")
        lines.append(f"- Started at: {report.started_at}")
        lines.append(f"- Duration: {report.duration_s:.2f}s")
        profiles = ", ".join(report.profiles_run) if report.profiles_run else "(none)"
        lines.append(f"- Profiles run: {profiles}")
        lines.append(f"- Total leaks found: {report.leaks_found}")
        lines.append(f"- Exit code: {report.exit_code}")
        lines.append("")
        lines.append("## Findings")
        lines.append("")

        if not report.findings:
            lines.append("_No findings recorded._")
        else:
            for i, f in enumerate(report.findings, start=1):
                verdict = self._enum_value(f.verdict)
                kane_status = self._enum_value(f.kane_status)
                lines.append(f"### {i}. {f.profile_name} / {f.assertion_id}")
                lines.append("")
                lines.append(f"- Verdict: `{verdict}`")
                lines.append(f"- Kane status: `{kane_status}`")
                indicators = (
                    ", ".join(f.leak_indicators) if f.leak_indicators else "(none)"
                )
                lines.append(f"- Leak indicators: {indicators}")
                if f.evidence:
                    lines.append("- Evidence (truncated, local-only):")
                    for snippet in f.evidence:
                        # Render each snippet inline-code in a list item; strip
                        # newlines so a single snippet stays on one line.
                        flat = str(snippet).replace("\n", " ").replace("\r", " ")
                        lines.append(f"  - `{flat}`")
                else:
                    lines.append("- Evidence: (none)")
                lines.append("")

        md_path = self.output_dir / "report.md"
        md_path.write_text("\n".join(lines), encoding="utf-8")
