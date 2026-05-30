"""Remediation advisor — Claude proposes fixes for detected leaks.

After a chaos run, this feeds each ``LEAK_DETECTED`` finding (its profile, the
broken dependency, the leak indicators, and truncated evidence) plus the relevant
target source back to the model and asks for a concrete, minimal fix: what to
change and why, framed as defensive error handling (e.g. "stop rendering
``err.stack``; show a generic message").

The advisor is advisory only — it never edits files. Its output is attached to
the report/dashboard so a developer can act on it. Evidence is already truncated
by the reporter and kept local; the advisor does not transmit secrets beyond the
already-truncated snippets the operator chose to scan locally.

Like the planner, it reads the API key from the environment / ``.env`` and caches
nothing sensitive. Requires the ``anthropic`` SDK only when actually invoked.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from fileak.llm.planner import (
    DEFAULT_MODEL,
    PlannerError,
    _load_dotenv_key,
    collect_source_excerpts,
)

_MAX_TOKENS = 3000
_MAX_EVIDENCE_PER_FINDING = 6


@dataclass
class FixSuggestion:
    """A remediation proposal for one leaking (profile, assertion) finding."""

    profile_name: str
    assertion_id: str
    target_package: str
    summary: str
    fix: str
    severity: str = "medium"

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_name": self.profile_name,
            "assertion_id": self.assertion_id,
            "target_package": self.target_package,
            "summary": self.summary,
            "fix": self.fix,
            "severity": self.severity,
        }


@dataclass
class AdvisorResult:
    suggestions: list[FixSuggestion] = field(default_factory=list)
    model: str = DEFAULT_MODEL

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "suggestions": [s.to_dict() for s in self.suggestions],
        }


_SYSTEM_PROMPT = """You are a secure-coding remediation advisor. Given a detected \
information-leak vulnerability (surfaced when a dependency misbehaved) and the \
relevant application source, propose the minimal, concrete fix that stops the leak \
while keeping the app functional. Focus on defensive error handling: never render \
raw error objects, stack traces, file paths, or internal/debug state to users; \
show generic user-facing messages and log details server-side instead.

Return ONLY valid JSON. No prose outside the JSON."""


def _leak_findings(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        f
        for f in report.get("findings", [])
        if f.get("verdict") == "leak_detected"
    ]


def _profile_pkg_map(report: dict[str, Any]) -> dict[str, str]:
    """Map profile_name -> target_package using the report's profiles block."""
    out: dict[str, str] = {}
    for p in report.get("profiles", []):
        if isinstance(p, dict) and p.get("name"):
            out[p["name"]] = p.get("target_package", "")
    return out


def _build_prompt(
    findings: list[dict[str, Any]],
    pkg_map: dict[str, str],
    sources: dict[str, str],
) -> str:
    items = []
    for f in findings:
        items.append(
            {
                "profile_name": f.get("profile_name"),
                "assertion_id": f.get("assertion_id"),
                "target_package": pkg_map.get(f.get("profile_name", ""), ""),
                "leak_indicators": f.get("leak_indicators", []),
                "evidence": (f.get("evidence", []) or [])[:_MAX_EVIDENCE_PER_FINDING],
            }
        )
    src_block = "\n\n".join(
        f"--- {name} ---\n{body}" for name, body in sources.items()
    ) or "(no source files available)"

    return f"""These chaos experiments caused the app to LEAK sensitive internals \
to the page. For each finding, propose a minimal fix.

Detected leaks:
{json.dumps(items, indent=2)}

Relevant application source:
{src_block}

Return JSON:
{{
  "suggestions": [
    {{
      "profile_name": "...",
      "assertion_id": "...",
      "target_package": "...",
      "summary": "one line: what leaks and where",
      "fix": "concrete change to make (reference the function/section); keep it minimal",
      "severity": "high|medium|low"
    }}
  ]
}}

Provide one suggestion per finding above."""


class RemediationAdvisor:
    """Asks Claude for concrete fixes for the leaks in a chaos report."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        *,
        api_key: Optional[str] = None,
    ) -> None:
        self.model = model
        self._api_key = api_key
        # Prompts sent on the most recent advise() call, for the dashboard panel.
        self.prompt_log: list[dict] = []

    def advise(self, report: dict[str, Any], target_dir: Path) -> AdvisorResult:
        """Return fix suggestions for every LEAK finding in ``report``.

        Returns an empty result (no API call) when there are no leaks. Reads a
        few target source excerpts for context. Raises :class:`PlannerError`
        on API/parse failure (reusing the planner's error type).
        """
        findings = _leak_findings(report)
        if not findings:
            return AdvisorResult(model=self.model)

        pkg_map = _profile_pkg_map(report)
        sources = collect_source_excerpts(target_dir)
        raw = self._call_model(findings, pkg_map, sources)

        result = AdvisorResult(model=self.model)
        for item in raw.get("suggestions", []):
            result.suggestions.append(
                FixSuggestion(
                    profile_name=str(item.get("profile_name", "")),
                    assertion_id=str(item.get("assertion_id", "")),
                    target_package=str(item.get("target_package", "")),
                    summary=str(item.get("summary", ""))[:300],
                    fix=str(item.get("fix", ""))[:1200],
                    severity=str(item.get("severity", "medium")),
                )
            )
        return result

    def _client(self):
        key = self._api_key or _load_dotenv_key()
        if not key:
            raise PlannerError(
                "ANTHROPIC_API_KEY not set. Add it to a .env file or export it."
            )
        try:
            import anthropic
        except ImportError as exc:
            raise PlannerError(
                "the 'anthropic' package is required (pip install anthropic)."
            ) from exc
        return anthropic.Anthropic(api_key=key)

    def _call_model(self, findings, pkg_map, sources) -> dict[str, Any]:
        from fileak.llm.planner import _extract_json

        client = self._client()
        prompt = _build_prompt(findings, pkg_map, sources)
        try:
            resp = client.messages.create(
                model=self.model,
                max_tokens=_MAX_TOKENS,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:
            raise PlannerError(f"Anthropic API call failed: {exc}") from exc
        text = "".join(
            b.text for b in resp.content if getattr(b, "type", "") == "text"
        )
        self.prompt_log.append(
            {
                "stage": "advise",
                "model": self.model,
                "system": _SYSTEM_PROMPT,
                "user": prompt,
                "response_excerpt": text[:4000],
            }
        )
        return _extract_json(text)
