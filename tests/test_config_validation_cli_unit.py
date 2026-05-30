"""Example-based unit tests for fail-fast config validation and CLI parsing.

This covers **task 11.3**: the ``Config_Validator``
(:func:`fileak.config.validate_config`, Requirements 4.5, 11.2, 11.3, 11.4) and
the argparse CLI surface (:func:`fileak.__main__._build_parser`,
:func:`fileak.__main__._apply_profile_filter`, :func:`fileak.__main__.main`,
Requirements 11.6, 11.2).

Where the property suites assert universal invariants elsewhere, these are
concrete example-based unit tests pinning down the exact fail-fast behaviour:

CONFIG VALIDATION (read-only, fails fast BEFORE any mutation — Req 11.2):
  * missing target package fails naming the package, touching no files (Req 11.3),
  * an uncompilable leak regex fails naming the bad pattern (Req 11.4),
  * duplicate profile names fail naming the duplicate (Req 4.5),
  * a fully valid default config passes without raising,
  * an assertion referencing an undefined profile fails naming it,
  * a missing / non-JSON ``package.json`` fails with a clear message.

CLI PARSING:
  * ``_build_parser`` parses ``--target``/``--port``/``--profile`` (default port
    3000; ``--target`` required),
  * ``_apply_profile_filter`` narrows profiles AND assertions to one profile
    (Req 11.6),
  * an unknown ``--profile`` raises naming the available profiles,
  * ``main()`` fails fast on a config error returning ``EXIT_CONFIG_ERROR`` (2)
    WITHOUT running the engine and WITHOUT mutating disk (Req 11.2),
  * ``main()`` with an unknown ``--profile`` returns 2.

Every test stays fully OFFLINE: only the config-error / parsing code paths of
``main()`` are exercised, which return before the engine is ever constructed,
so no subprocess, network, npm, or Kane binary is touched.

Validates: Requirements 4.5, 11.2, 11.3, 11.4, 11.6

Framework: pytest (``tmp_path``, ``capsys``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fileak.__main__ import (
    EXIT_CONFIG_ERROR,
    _apply_profile_filter,
    _build_parser,
    main,
)
from fileak.config import (
    ConfigError,
    DEFAULT_ASSERTIONS,
    DEFAULT_PROFILES,
    build_default_config,
    validate_config,
)
from fileak.models import (
    ChaosProfile,
    EngineConfig,
    LeakPattern,
    MockBehavior,
    SecurityAssertion,
)

# The default profiles target these three packages (design "Default Chaos
# Profiles"); a valid sandbox package.json must declare all of them.
_DEFAULT_TARGET_PACKAGES = ["next-auth", "analytics", "validator"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_package_json(target_dir: Path, dependencies: dict[str, str]) -> Path:
    """Write a minimal ``package.json`` with ``dependencies`` into ``target_dir``.

    Returns the path to the written file. Mirrors the integration suite's
    sandbox fixture so validation has a real-ish manifest to read.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "name": "sandbox-shop",
        "version": "1.0.0",
        "private": True,
        "scripts": {"start": "node server.js"},
        "dependencies": dict(dependencies),
    }
    package_json = target_dir / "package.json"
    package_json.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return package_json


def _valid_sandbox(tmp_path: Path) -> Path:
    """Create a sandbox target whose package.json declares all default packages.

    Validating a :func:`build_default_config` against this directory passes
    every check (unique names, valid regexes, defined assertion targets, and
    present target packages).
    """
    target = tmp_path / "sandbox-shop"
    _write_package_json(target, {pkg: "^1.0.0" for pkg in _DEFAULT_TARGET_PACKAGES})
    return target


# ---------------------------------------------------------------------------
# CONFIG VALIDATION — missing target package (Requirement 11.3)
# ---------------------------------------------------------------------------


def test_validate_config_missing_target_package_fails_naming_it(tmp_path: Path):
    """A profile whose target package is absent from package.json fails with a
    ConfigError naming the missing package, leaving all files unchanged.

    Validates: Requirements 11.2, 11.3
    """
    target = tmp_path / "app"
    # package.json declares some deps, but NOT the profile's target package.
    package_json = _write_package_json(target, {"react": "^18.0.0"})
    original_bytes = package_json.read_bytes()

    config = build_default_config(target)
    config.profiles = [
        ChaosProfile(
            name="broken_token_service",
            target_package="next-auth",  # not present in package.json
            behavior=MockBehavior.THROW_UNHANDLED,
            mock_template="auth_verify_throws",
            description="Auth verify() throws mid-flow",
        )
    ]
    # Keep only the all-profiles assertion so the failure is specifically the
    # missing package, not an undefined-profile reference.
    config.assertions = [a for a in DEFAULT_ASSERTIONS if not a.applies_to]

    with pytest.raises(ConfigError) as exc_info:
        validate_config(config)

    # The message names the offending package.
    assert "next-auth" in str(exc_info.value)

    # Validation is read-only: package.json is byte-for-byte unchanged and no
    # mock folder was created.
    assert package_json.read_bytes() == original_bytes
    assert not (target / ".fileak_mocks").exists()


# ---------------------------------------------------------------------------
# CONFIG VALIDATION — invalid leak regex (Requirement 11.4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_pattern",
    ["(", "[unclosed", "a(b", "*invalid"],
    ids=["unclosed_group", "unclosed_class", "open_group", "nothing_to_repeat"],
)
def test_validate_config_invalid_leak_regex_fails_naming_pattern(
    tmp_path: Path, bad_pattern: str
):
    """A leak pattern whose regex source does not compile fails with a
    ConfigError naming the offending pattern label and source.

    Validates: Requirements 11.2, 11.4
    """
    target = _valid_sandbox(tmp_path)
    config = build_default_config(target)

    leak_patterns = [
        LeakPattern("stack_trace", r"at\s+\w+", "high"),  # valid
        LeakPattern("bad_pattern", bad_pattern, "high"),  # uncompilable
    ]

    with pytest.raises(ConfigError) as exc_info:
        validate_config(config, leak_patterns)

    message = str(exc_info.value)
    # The message names the offending label and its source.
    assert "bad_pattern" in message
    assert bad_pattern in message


# ---------------------------------------------------------------------------
# CONFIG VALIDATION — duplicate profile names (Requirement 4.5)
# ---------------------------------------------------------------------------


def test_validate_config_duplicate_profile_names_fails_naming_duplicate(
    tmp_path: Path,
):
    """Two profiles sharing a name fail with a ConfigError naming the duplicate.

    The two target packages are both present in package.json so the failure is
    specifically about the duplicate name (validation checks unique names first
    anyway, per the documented order).

    Validates: Requirements 4.5, 11.2
    """
    target = tmp_path / "app"
    _write_package_json(target, {"next-auth": "^4.0.0", "analytics": "^0.8.0"})

    config = build_default_config(target)
    config.profiles = [
        ChaosProfile(
            name="dup_profile",
            target_package="next-auth",
            behavior=MockBehavior.THROW_UNHANDLED,
            mock_template="auth_verify_throws",
            description="first",
        ),
        ChaosProfile(
            name="dup_profile",  # same name -> duplicate
            target_package="analytics",
            behavior=MockBehavior.THROW_UNHANDLED,
            mock_template="global_throw",
            description="second",
        ),
    ]
    config.assertions = [a for a in DEFAULT_ASSERTIONS if not a.applies_to]

    with pytest.raises(ConfigError) as exc_info:
        validate_config(config)

    assert "dup_profile" in str(exc_info.value)


# ---------------------------------------------------------------------------
# CONFIG VALIDATION — happy path + remaining rules
# ---------------------------------------------------------------------------


def test_validate_config_valid_default_config_passes(tmp_path: Path):
    """A default config against a package.json declaring next-auth, analytics,
    and validator passes validation without raising.

    Validates: Requirements 11.2, 11.3
    """
    target = _valid_sandbox(tmp_path)
    config = build_default_config(target)

    # Should not raise.
    validate_config(config)


def test_validate_config_assertion_referencing_undefined_profile_fails(
    tmp_path: Path,
):
    """An assertion whose applies_to names a profile that is not defined fails
    with a ConfigError naming the undefined profile.

    Validates: Requirements 11.2
    """
    target = _valid_sandbox(tmp_path)
    config = build_default_config(target)
    config.assertions = [
        SecurityAssertion(
            id="ghost_assertion",
            prompt="check something",
            applies_to=["does_not_exist"],
        )
    ]

    with pytest.raises(ConfigError) as exc_info:
        validate_config(config)

    message = str(exc_info.value)
    assert "does_not_exist" in message
    assert "ghost_assertion" in message


def test_validate_config_missing_package_json_fails_clearly(tmp_path: Path):
    """A target directory without a package.json fails with a clear ConfigError.

    Validates: Requirements 11.2, 11.3
    """
    target = tmp_path / "empty-app"
    target.mkdir()  # no package.json written

    config = build_default_config(target)

    with pytest.raises(ConfigError) as exc_info:
        validate_config(config)

    assert "package.json" in str(exc_info.value)


def test_validate_config_invalid_json_package_json_fails_clearly(tmp_path: Path):
    """A package.json that is not valid JSON fails with a clear ConfigError.

    Validates: Requirements 11.2, 11.3
    """
    target = tmp_path / "broken-json-app"
    target.mkdir()
    (target / "package.json").write_text("{ not valid json ", encoding="utf-8")

    config = build_default_config(target)

    with pytest.raises(ConfigError) as exc_info:
        validate_config(config)

    assert "package.json" in str(exc_info.value)


# ---------------------------------------------------------------------------
# CLI PARSING — argparse surface
# ---------------------------------------------------------------------------


def test_parser_parses_target_port_and_profile():
    """The parser reads --target as a Path, --port as an int, and --profile as
    the supplied name.

    Validates: Requirements 11.6
    """
    parser = _build_parser()
    args = parser.parse_args(
        ["--target", "./sandbox", "--port", "4000", "--profile", "broken_token_service"]
    )

    assert args.target == Path("./sandbox")
    assert args.port == 4000
    assert args.profile == "broken_token_service"


def test_parser_port_defaults_to_3000_and_profile_optional():
    """--port defaults to 3000 and --profile defaults to None when omitted.

    Validates: Requirements 11.6
    """
    parser = _build_parser()
    args = parser.parse_args(["--target", "./sandbox"])

    assert args.target == Path("./sandbox")
    assert args.port == 3000
    assert args.profile is None


def test_parser_target_is_required():
    """Omitting the required --target makes argparse exit (SystemExit)."""
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


# ---------------------------------------------------------------------------
# CLI — --profile filtering (Requirement 11.6)
# ---------------------------------------------------------------------------


def test_apply_profile_filter_narrows_profiles_and_assertions(tmp_path: Path):
    """_apply_profile_filter narrows the config to exactly the named profile and
    keeps only the assertions applicable to it, rewriting their applies_to to
    reference only that profile.

    ``broken_token_service`` is applicable to both default assertions
    (``checkout_no_leak`` applies to all, ``settings_graceful_failure`` lists
    it), so both are retained but re-pointed at the single retained profile.

    Validates: Requirements 11.6
    """
    target = _valid_sandbox(tmp_path)
    config = build_default_config(target)

    _apply_profile_filter(config, "broken_token_service")

    # Exactly one profile remains, and it is the requested one.
    assert [p.name for p in config.profiles] == ["broken_token_service"]

    # Every retained assertion references only the retained profile (so the
    # filtered config still validates).
    for assertion in config.assertions:
        assert assertion.applies_to == ["broken_token_service"]

    # The filtered config validates cleanly (target package present).
    validate_config(config)


def test_apply_profile_filter_drops_inapplicable_assertions(tmp_path: Path):
    """Filtering to ``compromised_input_handler`` keeps only the all-profiles
    assertion (``checkout_no_leak``) and drops ``settings_graceful_failure``,
    which does not apply to that profile.

    Validates: Requirements 11.6
    """
    target = _valid_sandbox(tmp_path)
    config = build_default_config(target)

    _apply_profile_filter(config, "compromised_input_handler")

    assert [p.name for p in config.profiles] == ["compromised_input_handler"]
    assert [a.id for a in config.assertions] == ["checkout_no_leak"]
    for assertion in config.assertions:
        assert assertion.applies_to == ["compromised_input_handler"]


def test_apply_profile_filter_unknown_profile_raises_listing_available(
    tmp_path: Path,
):
    """An unknown --profile name raises ConfigError listing the available
    profile names so the operator can correct the argument.

    Validates: Requirements 11.6, 11.2
    """
    target = _valid_sandbox(tmp_path)
    config = build_default_config(target)

    with pytest.raises(ConfigError) as exc_info:
        _apply_profile_filter(config, "no_such_profile")

    message = str(exc_info.value)
    assert "no_such_profile" in message
    # Lists available default profile names.
    for profile in DEFAULT_PROFILES:
        assert profile.name in message


# ---------------------------------------------------------------------------
# CLI — main() fail-fast on config error (Requirement 11.2)
# ---------------------------------------------------------------------------


def test_main_fails_fast_on_missing_package_returns_config_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    """main() returns EXIT_CONFIG_ERROR (2) WITHOUT running the engine when the
    target package.json is missing the default packages.

    The validation failure happens before any component is constructed, so
    nothing on disk is mutated (no ``.fileak_mocks/``) and an actionable message
    is printed to stderr.

    Validates: Requirements 11.2, 11.3
    """
    target = tmp_path / "app"
    # package.json deliberately omits next-auth/analytics/validator.
    _write_package_json(target, {"react": "^18.0.0"})

    rc = main(["--target", str(target), "--port", "3000"])

    assert rc == EXIT_CONFIG_ERROR

    # Nothing was mutated: no mock folder created by the fail-fast path.
    assert not (target / ".fileak_mocks").exists()

    # An actionable config-error message was printed to stderr.
    captured = capsys.readouterr()
    assert "configuration error" in captured.err
    # The message names at least one of the missing default packages.
    assert any(pkg in captured.err for pkg in _DEFAULT_TARGET_PACKAGES)


def test_main_unknown_profile_returns_config_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    """main() with an unknown --profile returns EXIT_CONFIG_ERROR (2) via the
    ConfigError path, before any engine wiring.

    Validates: Requirements 11.2, 11.6
    """
    target = _valid_sandbox(tmp_path)

    rc = main(["--target", str(target), "--profile", "no_such_profile"])

    assert rc == EXIT_CONFIG_ERROR
    assert not (target / ".fileak_mocks").exists()

    captured = capsys.readouterr()
    assert "configuration error" in captured.err
    assert "no_such_profile" in captured.err
