"""Core data models for the fileak engine.

All enums and dataclasses are defined here exactly as specified in the
design document's "Data Models" section. These types are stdlib-only
(``dataclasses``, ``enum``, ``pathlib``) and carry no behavior beyond the
``RunReport.exit_code`` property.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class BootError(Exception):
    """Raised when the target app fails to become ready within the timeout
    or its process exits during readiness polling.

    Used by ``AppRunner`` and handled by the ``Orchestrator`` to record an
    ``INCONCLUSIVE`` finding for the affected profile.
    """


class MockBehavior(Enum):
    """How a mocked dependency misbehaves."""

    THROW_UNHANDLED = "throw_unhandled"   # raise an unhandled exception on use
    RETURN_EMPTY = "return_empty"         # return {} / null for every call
    HTTP_500 = "http_500"                 # respond 500 Internal Server Error
    LEAK_DEBUG_STATE = "leak_debug_state"  # echo raw internal/debug objects


@dataclass(frozen=True)
class ChaosProfile:
    """A named chaos experiment: which dependency to break and how."""

    name: str                 # e.g. "broken_token_service"
    target_package: str       # npm package to replace, e.g. "next-auth"
    behavior: MockBehavior
    mock_template: str        # template id used to render the mock module
    description: str
    # Optional LLM-authored mock module source (CommonJS index.js). When set
    # (free-form planner mode), the ChaosMutator writes THIS validated source as
    # the mock's index.js instead of rendering a built-in MockBehavior template;
    # ``behavior`` then serves only as a label/category for reporting. Must have
    # passed fileak.llm.mock_guard.validate_mock_source before reaching inject.
    # Default None preserves the built-in template path (backward compatible).
    custom_source: str | None = None
    # Free-text rationale from the planner explaining why this experiment was
    # proposed (surfaced in the dashboard; never affects execution).
    rationale: str = ""


@dataclass
class MutationRecord:
    """Exactly what inject() changed, so revert() is precise."""

    profile_name: str
    target_package: str
    original_spec: str        # original version spec from package.json
    mock_path: Path           # folder created for the mock
    package_json: Path


@dataclass(frozen=True)
class SecurityAssertion:
    """A semantic, natural-language assertion handed to Kane CLI."""

    id: str                   # e.g. "checkout_no_stacktrace"
    prompt: str               # the NL instruction(s) for Kane
    applies_to: list[str]     # profile names this assertion is relevant for


@dataclass
class AppHandle:
    pid: int
    base_url: str
    log_path: Path


class StepStatus(Enum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"       # ⏭ — soft/optional step that did not run
    ERROR = "error"           # Kane could not complete (exit 2/3, missing/malformed Result.md)


@dataclass
class KaneStepResult:
    index: int
    status: StepStatus
    text: str
    duration_s: float


@dataclass
class KaneResult:
    """Parsed from Kane's Result.md + best-effort captured artifacts."""

    status: StepStatus              # overall status from Result.md frontmatter
    steps: list[KaneStepResult]
    console_logs: str               # best-effort: Result.md body + any session/run artifacts
    output_dir: Path
    raw_result_md: str


@dataclass(frozen=True)
class LeakPattern:
    """A regex + label used to flag sensitive data in logs/UI text."""

    label: str                # e.g. "stack_trace", "file_path", "sql_query"
    pattern: str              # regex source
    severity: str             # "high" | "medium" | "low"


class Verdict(Enum):
    SAFE = "safe"                   # graceful failure, no leak -> good
    LEAK_DETECTED = "leak_detected"  # sensitive state exposed -> bad
    INCONCLUSIVE = "inconclusive"   # Kane errored / could not judge


@dataclass
class Finding:
    profile_name: str
    assertion_id: str
    verdict: Verdict
    kane_status: StepStatus
    leak_indicators: list[str] = field(default_factory=list)  # matched labels
    evidence: list[str] = field(default_factory=list)          # snippets
    output_dir: Path | None = None


@dataclass
class RunReport:
    findings: list[Finding]
    profiles_run: list[str]
    leaks_found: int
    started_at: str
    duration_s: float

    @property
    def exit_code(self) -> int:
        return 1 if self.leaks_found > 0 else 0


@dataclass
class EngineConfig:
    target_dir: Path
    start_cmd: list[str]              # e.g. ["npm", "run", "start"]
    install_cmd: list[str]           # e.g. ["npm", "install"]
    port: int = 3000
    readiness_path: str = "/"
    boot_timeout_s: float = 60.0
    profiles: list[ChaosProfile] = field(default_factory=list)
    assertions: list[SecurityAssertion] = field(default_factory=list)
    tracked_files: list[Path] = field(default_factory=list)

    def profile_names(self) -> list[str]:
        """Return the configured chaos profile names, in declaration order.

        This is the experiment axis the ``Orchestrator`` iterates over (design
        "Main Orchestration Loop": ``for profile_name in
        self.config.profile_names()``).
        """
        return [p.name for p in self.profiles]

    def assertions_for(self, profile_name: str) -> list[SecurityAssertion]:
        """Return the assertions applicable to ``profile_name``, in order.

        An assertion applies to a profile when its ``applies_to`` list is empty
        (the validation rule "an empty list means applies to all profiles") or
        explicitly names the profile. Document order is preserved so the
        orchestrator evaluates assertions deterministically.
        """
        return [
            a
            for a in self.assertions
            if not a.applies_to or profile_name in a.applies_to
        ]
