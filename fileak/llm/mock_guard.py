"""Static safety validation for LLM-generated mock JavaScript.

This is the single most security-critical module in the LLM layer: when the
planner runs in free-form mode, an Anthropic model authors the mock module's
JavaScript, and that code is later ``npm install``-ed and **executed** inside the
target app. Before any generated source is allowed to touch disk, it must pass
both gates here:

1. **Denylist scan** (:func:`scan_mock_source`) — a conservative static check
   that rejects the dangerous capabilities a chaos mock has no business using:
   spawning processes, filesystem writes, outbound network, dynamic code eval,
   process exit, and requiring anything other than the package being mocked.
2. **Syntax gate** (:func:`syntax_check`) — ``node --check`` so malformed JS is
   rejected before install.

:func:`validate_mock_source` runs both and raises :class:`MockGuardError` (with
the specific reason) on any failure. Callers MUST drop a profile whose mock fails
validation and log the reason — never silently run it.

Containment beyond this module is provided by the engine itself: mocks are written
only under ``.fileak_mocks/``, only ``package.json`` is mutated, the target is
local-only, and :class:`~fileak.baseline.BaselineGuard` restores byte-for-byte on
every exit path. This module is the *pre-execution* gate; those are the
*containment* guarantees.

Important: a static denylist is a strong speed-bump, NOT a sandbox. It raises the
bar against an LLM accidentally (or a prompt-injected payload deliberately)
emitting dangerous code, but it cannot prove safety of arbitrary JavaScript. The
feature is therefore gated behind an explicit opt-in and intended only for a
local sandbox target the operator controls and can afford to have restored.

Stdlib only: ``re``, ``subprocess``, ``shutil``, ``tempfile``, ``pathlib``.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


class MockGuardError(Exception):
    """Raised when LLM-generated mock source fails a safety/syntax gate.

    The message names the specific violation (e.g. the banned token and a short
    context snippet, or the syntax error) so the caller can log exactly why a
    profile was dropped.
    """


@dataclass(frozen=True)
class DenyRule:
    """A single denylist rule: a compiled pattern plus a human reason."""

    label: str
    pattern: re.Pattern[str]
    reason: str


def _rule(label: str, source: str, reason: str) -> DenyRule:
    return DenyRule(label, re.compile(source), reason)


# Conservative denylist. Each rule targets a capability a deliberately-broken
# *dependency mock* never legitimately needs. The goal is defense-in-depth, not
# a proof of safety: combined with the syntax gate, the engine's write/restore
# containment, and the local-only constraint, it makes a dangerous generated
# mock very unlikely to run.
_DENY_RULES: tuple[DenyRule, ...] = (
    # --- process / OS execution -------------------------------------------
    _rule("child_process", r"""require\(\s*['"]child_process['"]""",
          "spawning OS processes is forbidden in a mock"),
    _rule("child_process_import", r"""\bfrom\s+['"]child_process['"]""",
          "spawning OS processes is forbidden in a mock"),
    _rule("exec_family", r"\b(execSync|execFileSync|spawnSync|exec|execFile|spawn|fork)\s*\(",
          "process execution APIs are forbidden in a mock"),
    _rule("process_exit", r"\bprocess\s*\.\s*(exit|abort|kill)\b",
          "a mock must not terminate the host process"),
    _rule("process_binding", r"\bprocess\s*\.\s*(binding|dlopen)\b",
          "low-level process bindings are forbidden"),

    # --- filesystem writes -------------------------------------------------
    _rule("fs_module", r"""require\(\s*['"](fs|fs/promises|node:fs)['"]""",
          "filesystem access is forbidden in a mock"),
    _rule("fs_import", r"""\bfrom\s+['"](fs|fs/promises|node:fs)['"]""",
          "filesystem access is forbidden in a mock"),

    # --- outbound network --------------------------------------------------
    _rule("net_module", r"""require\(\s*['"](net|dgram|tls|http|https|node:net|node:http|node:https)['"]""",
          "network access is forbidden in a mock"),
    _rule("net_import", r"""\bfrom\s+['"](net|dgram|tls|http|https)['"]""",
          "network access is forbidden in a mock"),
    _rule("fetch", r"\b(fetch|XMLHttpRequest|WebSocket)\s*\(",
          "outbound network calls are forbidden in a mock"),

    # --- dynamic code execution -------------------------------------------
    _rule("eval", r"\beval\s*\(", "dynamic code evaluation is forbidden"),
    _rule("function_ctor", r"\bnew\s+Function\s*\(",
          "the Function constructor is forbidden"),
    _rule("vm_module", r"""require\(\s*['"](vm|node:vm)['"]""",
          "the vm module is forbidden"),

    # --- other escape hatches ---------------------------------------------
    _rule("module_constructor", r"""require\(\s*['"](module|node:module)['"]""",
          "requiring the module loader is forbidden"),
    _rule("global_process_env_write",
          r"\bprocess\s*\.\s*env\s*\[[^\]]+\]\s*=", "writing process.env is forbidden"),
)

#: Matches a CommonJS ``require('x')`` / ``require("x")`` call, capturing the
#: requested module specifier so we can confirm a mock only requires *itself*
#: (the package it stands in for) or pure relative paths within its own folder.
_REQUIRE_RE = re.compile(r"""require\(\s*['"]([^'"]+)['"]\s*\)""")

#: Matches ``import ... from 'x'`` ES-module specifiers (the mock should be CJS,
#: but we scan defensively in case the model emits ESM).
_IMPORT_FROM_RE = re.compile(r"""\bimport\b[^;\n]*?\bfrom\s+['"]([^'"]+)['"]""")
_IMPORT_BARE_RE = re.compile(r"""\bimport\s+['"]([^'"]+)['"]""")

#: Hard cap on generated mock size (chars). A legitimate broken stand-in is
#: small; anything larger is rejected as suspicious and to bound install time.
_MAX_MOCK_CHARS = 8000


@dataclass
class GuardReport:
    """Outcome of a non-raising scan: ``ok`` plus any collected violations."""

    ok: bool
    violations: list[str] = field(default_factory=list)


def _allowed_specifier(spec: str, package: str) -> bool:
    """Return True if a require/import specifier is permitted for ``package``.

    Allowed: a relative path (``./x``, ``../x`` — resolved within the mock's own
    folder by the containment layer) or the package being mocked itself (and its
    subpaths, e.g. ``validator/lib/isEmail``). Everything else — core modules,
    other third-party packages — is denied.
    """
    if spec.startswith("."):
        return True
    if spec == package or spec.startswith(package + "/"):
        return True
    return False


def scan_mock_source(source: str, package: str) -> GuardReport:
    """Statically scan ``source`` against the denylist; never raises.

    Returns a :class:`GuardReport`. ``ok`` is True only when no denylist rule
    fires, every ``require``/``import`` specifier is allowed for ``package``
    (relative or the mocked package itself), and the source is within the size
    cap. Each violation string names the rule and a short context snippet.
    """
    violations: list[str] = []

    if not isinstance(source, str) or not source.strip():
        return GuardReport(False, ["empty or non-string mock source"])
    if len(source) > _MAX_MOCK_CHARS:
        violations.append(
            f"mock source exceeds {_MAX_MOCK_CHARS} chars ({len(source)})"
        )

    for rule in _DENY_RULES:
        m = rule.pattern.search(source)
        if m:
            snippet = source[max(0, m.start() - 20) : m.end() + 20].replace("\n", " ")
            violations.append(f"[{rule.label}] {rule.reason} — near: …{snippet}…")

    # Every module specifier must be the mocked package itself or relative.
    for regex in (_REQUIRE_RE, _IMPORT_FROM_RE, _IMPORT_BARE_RE):
        for m in regex.finditer(source):
            spec = m.group(1)
            if not _allowed_specifier(spec, package):
                violations.append(
                    f"[require] mock may only require {package!r} or relative "
                    f"paths, not {spec!r}"
                )

    return GuardReport(ok=not violations, violations=violations)


def syntax_check(source: str) -> GuardReport:
    """Validate ``source`` parses as JavaScript via ``node --check``; never raises.

    Writes the source to a temp ``.js`` file and runs ``node --check``. When
    Node is unavailable the check is skipped (reported ``ok`` with a note), since
    the install step will surface a syntax error anyway and the denylist scan is
    the primary safety gate. A non-zero ``node --check`` is a hard failure.
    """
    node = shutil.which("node")
    if node is None:
        return GuardReport(True, ["node not found; syntax check skipped"])

    with tempfile.TemporaryDirectory(prefix="fileak_mockcheck_") as td:
        js = Path(td) / "candidate.js"
        js.write_text(source, encoding="utf-8")
        try:
            proc = subprocess.run(
                [node, "--check", str(js)],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return GuardReport(False, [f"node --check could not run: {exc}"])

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        first = detail[0] if detail else "unknown syntax error"
        return GuardReport(False, [f"node --check failed: {first}"])
    return GuardReport(True, [])


def validate_mock_source(source: str, package: str) -> None:
    """Run the full safety gate on a generated mock; raise on any failure.

    Runs :func:`scan_mock_source` (denylist + specifier check + size cap) then
    :func:`syntax_check` (``node --check``). On any violation raises
    :class:`MockGuardError` whose message enumerates every reason, so the caller
    can log exactly why the profile was dropped and continue with the rest.

    Args:
        source: The candidate mock ``index.js`` source authored by the LLM.
        package: The npm package this mock stands in for (used to allow the mock
            to ``require`` only itself / relative paths).

    Raises:
        MockGuardError: If the source trips the denylist, requires a disallowed
            module, exceeds the size cap, or fails ``node --check``.
    """
    scan = scan_mock_source(source, package)
    if not scan.ok:
        raise MockGuardError(
            "generated mock failed safety scan:\n  - "
            + "\n  - ".join(scan.violations)
        )
    syntax = syntax_check(source)
    if not syntax.ok:
        raise MockGuardError(
            "generated mock failed syntax check:\n  - "
            + "\n  - ".join(syntax.violations)
        )
