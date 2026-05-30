"""Fail-fast configuration validation and default engine configuration.

This module implements the ``Config_Validator`` (Requirements 4.5, 11.2, 11.3,
11.4) plus the engine's built-in defaults: the three default chaos profiles
(Requirements 4.1-4.4), the default semantic assertion bank, and a
``build_default_config`` helper that wires an :class:`~fileak.models.EngineConfig`
for a target sandbox app (used by the CLI in task 11.2).

The cardinal rule of this module is *fail fast and read-only*: validation runs
the FULL configuration check BEFORE any mutation occurs (Requirement 11.2) and
NEVER writes to disk. It only reads ``<target_dir>/package.json`` to confirm
every configured ``target_package`` is a real dependency. On any problem it
raises :class:`ConfigError` with an actionable message naming the offending
item, leaving every file on disk unchanged (Requirement 11.3, design
"Error Handling — Scenario 4").

Validation covers four checks (design "Validation Rules"):

1. Each :class:`~fileak.models.ChaosProfile` name is unique within the config
   and therefore maps to exactly one target package (Requirement 4.5).
2. Every configured ``target_package`` is present in the target
   ``package.json`` under ``dependencies`` or ``devDependencies``
   (Requirement 11.3). A missing package fails with a message naming it.
3. Every :class:`~fileak.models.LeakPattern` ``pattern`` compiles as a valid
   regular expression (Requirement 11.4).
4. Every :class:`~fileak.models.SecurityAssertion` ``applies_to`` entry
   references a defined profile name (an empty list means "all profiles").

Stdlib only: ``json``, ``re``, ``pathlib``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from fileak.models import (
    ChaosProfile,
    EngineConfig,
    LeakPattern,
    MockBehavior,
    SecurityAssertion,
)
from fileak.reporter import DEFAULT_LEAK_PATTERNS

__all__ = [
    "ConfigError",
    "DEFAULT_PROFILES",
    "DEFAULT_ASSERTIONS",
    "DEFAULT_TRACKED_FILES",
    "DEFAULT_INSTALL_CMD",
    "DEFAULT_START_CMD",
    "build_default_config",
    "index_profiles",
    "validate_config",
]


class ConfigError(Exception):
    """Raised when an :class:`EngineConfig` fails fail-fast validation.

    The message always names the offending item — the missing package, the
    duplicate profile name, the uncompilable regex source, or the undefined
    profile referenced by an assertion — so the operator can fix the config.
    Raising this leaves every file on disk unchanged (validation is read-only).
    """


# --- Default chaos profiles (Requirements 4.1-4.4) -------------------------
#
# The three default profiles, matching the design's "Default Chaos Profiles"
# table and "Example Usage" config.py. The ``target_package`` names are
# ILLUSTRATIVE placeholders (design: "Sandbox package names remain illustrative
# placeholders"); the operator overrides them to match real deps present in the
# target app's package.json.
DEFAULT_PROFILES: list[ChaosProfile] = [
    # broken_token_service: THROW_UNHANDLED on the authentication dependency
    # (Requirement 4.2).
    ChaosProfile(
        name="broken_token_service",
        target_package="next-auth",
        behavior=MockBehavior.THROW_UNHANDLED,
        mock_template="auth_verify_throws",
        description="Auth verify() throws mid-flow",
    ),
    # crashed_telemetry: THROW_UNHANDLED on the analytics dependency
    # (Requirement 4.3).
    ChaosProfile(
        name="crashed_telemetry",
        target_package="analytics",
        behavior=MockBehavior.THROW_UNHANDLED,
        mock_template="global_throw",
        description="Analytics init throws globally",
    ),
    # compromised_input_handler: LEAK_DEBUG_STATE on the form-handling
    # dependency (Requirement 4.4).
    ChaosProfile(
        name="compromised_input_handler",
        target_package="validator",
        behavior=MockBehavior.LEAK_DEBUG_STATE,
        mock_template="echo_debug",
        description="Form util echoes raw debug state",
    ),
]


# --- Default semantic assertion bank ---------------------------------------
#
# Reproduced verbatim from the design's "Default Semantic Assertion Bank".
# ``checkout_no_leak`` applies to all profiles (empty ``applies_to``);
# ``settings_graceful_failure`` applies only to the two THROW_UNHANDLED
# profiles where a backend service going down is the relevant scenario.
DEFAULT_ASSERTIONS: list[SecurityAssertion] = [
    SecurityAssertion(
        id="checkout_no_leak",
        prompt=(
            "Perform the checkout flow. Inspect the user interface. "
            "Ensure no raw stack traces, file paths, database queries, or "
            "environment variables are visible anywhere on the screen."
        ),
        applies_to=[],  # all profiles
    ),
    SecurityAssertion(
        id="settings_graceful_failure",
        prompt=(
            "Attempt to navigate to the settings page while a backend "
            "service is down. Verify the application fails gracefully with a "
            "generic user-friendly error message, rather than crashing or "
            "exposing raw system objects."
        ),
        applies_to=["broken_token_service", "crashed_telemetry"],
    ),
]


#: Tracked files the BaselineGuard snapshots/restores (design "Example Usage").
#: Relative to the target dir; the guard resolves them against ``target_dir``.
DEFAULT_TRACKED_FILES: list[Path] = [
    Path("package.json"),
    Path("package-lock.json"),
]

#: Default install/start commands for an npm-based target app (design
#: "Example Usage").
DEFAULT_INSTALL_CMD: list[str] = ["npm", "install"]
DEFAULT_START_CMD: list[str] = ["npm", "run", "start"]


def index_profiles(profiles: list[ChaosProfile]) -> dict[str, ChaosProfile]:
    """Index a profile list into a ``name -> ChaosProfile`` mapping.

    Used to construct the :class:`~fileak.chaos.ChaosMutator` (design
    "CLI entrypoint": ``index_profiles(config.profiles)``). This does NOT
    validate uniqueness — call :func:`validate_config` first; on a duplicate
    name a later profile would silently overwrite an earlier one here.
    """
    return {p.name: p for p in profiles}


def build_default_config(
    target_dir: Path | str,
    port: int = 3000,
    *,
    readiness_path: str = "/",
    boot_timeout_s: float = 60.0,
    profiles: list[ChaosProfile] | None = None,
    assertions: list[SecurityAssertion] | None = None,
    install_cmd: list[str] | None = None,
    start_cmd: list[str] | None = None,
    tracked_files: list[Path] | None = None,
) -> EngineConfig:
    """Build a default :class:`EngineConfig` for a target sandbox app.

    Wires the default profiles, assertion bank, tracked files, and npm
    install/start commands per the design's "Example Usage". The CLI
    (task 11.2) calls this to assemble the config from ``--target``/``--port``.

    The returned config is NOT validated here — the caller runs
    :func:`validate_config` before any mutation occurs (Requirement 11.2). The
    leak patterns are owned by the reporter (:data:`DEFAULT_LEAK_PATTERNS`) and
    validated separately by :func:`validate_config`.

    Args:
        target_dir: The target application directory.
        port: Local port the app boots on (default 3000).
        readiness_path: HTTP path polled for readiness (default ``"/"``).
        boot_timeout_s: Readiness timeout in seconds (default 60).
        profiles: Chaos profiles to run; defaults to :data:`DEFAULT_PROFILES`.
        assertions: Assertion bank; defaults to :data:`DEFAULT_ASSERTIONS`.
        install_cmd: Install command; defaults to :data:`DEFAULT_INSTALL_CMD`.
        start_cmd: Start command; defaults to :data:`DEFAULT_START_CMD`.
        tracked_files: Files the BaselineGuard protects; defaults to
            :data:`DEFAULT_TRACKED_FILES`.

    Returns:
        A populated :class:`EngineConfig` (defensively copied default lists so
        callers cannot mutate the module-level defaults).
    """
    return EngineConfig(
        target_dir=Path(target_dir),
        start_cmd=list(start_cmd) if start_cmd is not None else list(DEFAULT_START_CMD),
        install_cmd=(
            list(install_cmd) if install_cmd is not None else list(DEFAULT_INSTALL_CMD)
        ),
        port=port,
        readiness_path=readiness_path,
        boot_timeout_s=boot_timeout_s,
        profiles=list(profiles) if profiles is not None else list(DEFAULT_PROFILES),
        assertions=(
            list(assertions) if assertions is not None else list(DEFAULT_ASSERTIONS)
        ),
        tracked_files=(
            list(tracked_files)
            if tracked_files is not None
            else list(DEFAULT_TRACKED_FILES)
        ),
    )


def validate_config(
    config: EngineConfig,
    leak_patterns: list[LeakPattern] | None = None,
) -> None:
    """Validate the FULL engine config before any mutation occurs (fail fast).

    This is the ``Config_Validator`` (Requirement 11.2). It runs every check
    read-only and raises :class:`ConfigError` with an actionable message on the
    first failing category, leaving all files on disk unchanged (Requirement
    11.3, design "Error Handling — Scenario 4").

    Checks (design "Validation Rules"):

    1. Profile names are unique within the config, so each name maps to exactly
       one target package (Requirement 4.5).
    2. Every :class:`LeakPattern` ``pattern`` compiles as a valid regex
       (Requirement 11.4) — validated at load time before any mutation.
    3. Every :class:`SecurityAssertion` ``applies_to`` entry references a
       defined profile name; an empty list means "all profiles".
    4. Every configured ``target_package`` is present in the target
       ``package.json`` under ``dependencies``/``devDependencies``
       (Requirement 11.3) — checked last because it reads from disk.

    Args:
        config: The :class:`EngineConfig` to validate.
        leak_patterns: Leak patterns to validate; defaults to the reporter's
            :data:`DEFAULT_LEAK_PATTERNS`.

    Raises:
        ConfigError: If any check fails. The message names the offending item.
    """
    if leak_patterns is None:
        leak_patterns = DEFAULT_LEAK_PATTERNS

    # Cheap, file-free checks first so pure-config mistakes surface before any
    # disk read. None of these touch the filesystem.
    _validate_unique_profile_names(config.profiles)
    _validate_leak_patterns(leak_patterns)
    _validate_assertion_targets(config.profiles, config.assertions)

    # Disk-reading check last: confirm every target package is a real
    # dependency (Requirement 11.3). Still read-only — no file is written.
    _validate_target_packages_present(config.target_dir, config.profiles)


def _validate_unique_profile_names(profiles: list[ChaosProfile]) -> None:
    """Reject duplicate profile names (Requirement 4.5).

    A unique name guarantees each profile maps to exactly one target package.
    The message names every duplicated profile name.
    """
    seen: set[str] = set()
    duplicates: list[str] = []
    for profile in profiles:
        if profile.name in seen and profile.name not in duplicates:
            duplicates.append(profile.name)
        seen.add(profile.name)

    if duplicates:
        names = ", ".join(repr(name) for name in duplicates)
        raise ConfigError(
            f"duplicate chaos profile name(s): {names}. Each profile name must "
            f"be unique within a configuration and map to exactly one target "
            f"package."
        )


def _validate_leak_patterns(leak_patterns: list[LeakPattern]) -> None:
    """Reject leak patterns whose regex source does not compile (Req. 11.4).

    Compiles every pattern at config-load time so an invalid regex fails fast
    before any mutation. The message names the offending label and source.
    """
    for lp in leak_patterns:
        try:
            re.compile(lp.pattern)
        except re.error as exc:
            raise ConfigError(
                f"invalid leak pattern {lp.label!r}: regex source "
                f"{lp.pattern!r} does not compile ({exc})."
            ) from exc


def _validate_assertion_targets(
    profiles: list[ChaosProfile],
    assertions: list[SecurityAssertion],
) -> None:
    """Reject assertions referencing undefined profile names.

    Per the design validation rule, ``applies_to`` must reference only defined
    profile names; an empty list means "applies to all profiles". The message
    names the assertion and the undefined profile(s) it references.
    """
    defined = {p.name for p in profiles}
    for assertion in assertions:
        unknown = [name for name in assertion.applies_to if name not in defined]
        if unknown:
            names = ", ".join(repr(name) for name in unknown)
            raise ConfigError(
                f"assertion {assertion.id!r} references undefined profile "
                f"name(s): {names}. 'applies_to' must reference defined profile "
                f"names (an empty list means it applies to all profiles)."
            )


def _validate_target_packages_present(
    target_dir: Path,
    profiles: list[ChaosProfile],
) -> None:
    """Confirm every profile's target package is a real dependency (Req. 11.3).

    Reads ``<target_dir>/package.json`` (read-only) and checks each profile's
    ``target_package`` against the union of ``dependencies`` and
    ``devDependencies``. A missing package fails with a message naming it,
    leaving all files unchanged (design "Error Handling — Scenario 4").
    """
    declared = _read_declared_dependencies(target_dir)

    missing: list[str] = []
    for profile in profiles:
        if profile.target_package not in declared and profile.target_package not in missing:
            missing.append(profile.target_package)

    if missing:
        names = ", ".join(repr(name) for name in missing)
        package_json = Path(target_dir) / "package.json"
        raise ConfigError(
            f"configured target package(s) not present in {package_json}: "
            f"{names}. Add the package to 'dependencies'/'devDependencies', or "
            f"point the profile at a package that exists. No files were changed."
        )


def _read_declared_dependencies(target_dir: Path) -> set[str]:
    """Return the set of dependency names declared in ``package.json``.

    Reads ``<target_dir>/package.json`` (read-only) and unions the keys of its
    ``dependencies`` and ``devDependencies`` maps. Raises :class:`ConfigError`
    with a clear message if the file is missing, unreadable, or not valid JSON.
    """
    package_json = Path(target_dir) / "package.json"
    if not package_json.is_file():
        raise ConfigError(
            f"package.json not found at {package_json}. Pass a valid target "
            f"directory (--target) that contains a package.json."
        )

    try:
        text = package_json.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(
            f"could not read {package_json}: {exc}."
        ) from exc

    try:
        manifest = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"could not parse {package_json} as JSON: {exc}."
        ) from exc

    if not isinstance(manifest, dict):
        raise ConfigError(
            f"{package_json} is not a JSON object."
        )

    declared: set[str] = set()
    for map_name in ("dependencies", "devDependencies"):
        deps = manifest.get(map_name)
        if isinstance(deps, dict):
            declared.update(deps.keys())
    return declared
