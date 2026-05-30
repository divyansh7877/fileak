"""Well-formedness tests for the committable sandbox target fixture (task 12).

These guard the ``tests/fixtures/sandbox-shop`` fixture against rot and tie it
directly to the engine's default chaos profiles. The fixture is a *target* app
the engine could be pointed at: its ``package.json`` declares the three packages
the default profiles mutate (``next-auth``/``analytics``/``validator``), so
running ``build_default_config`` + ``validate_config`` against it proves the
fixture satisfies the default profiles' target packages.

Validates: Requirements 4.1, 4.2, 4.3, 4.4
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fileak.config import (
    DEFAULT_PROFILES,
    build_default_config,
    validate_config,
)
from fileak.models import MockBehavior

# The fixture lives next to this test module under ``fixtures/sandbox-shop``.
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "sandbox-shop"

# The three packages the default profiles mutate (design "Default Chaos
# Profiles"; see DEFAULT_PROFILES in fileak/config.py).
EXPECTED_DEPS = {"next-auth", "analytics", "validator"}


@pytest.fixture
def package_json() -> dict:
    """Parsed ``package.json`` of the sandbox fixture."""
    return json.loads(
        (FIXTURE_DIR / "package.json").read_text(encoding="utf-8")
    )


def test_fixture_directory_exists():
    """The sandbox fixture directory is present and committable."""
    assert FIXTURE_DIR.is_dir(), f"missing sandbox fixture at {FIXTURE_DIR}"


def test_package_json_parses_and_declares_target_deps(package_json: dict):
    """package.json parses and declares the three default-profile targets.

    Validates: Requirements 4.1, 4.2, 4.3, 4.4
    """
    deps = package_json.get("dependencies", {})
    assert isinstance(deps, dict)
    # Every package a default profile mutates must be a real dependency here.
    assert EXPECTED_DEPS.issubset(set(deps)), (
        f"fixture must declare {EXPECTED_DEPS}, found {set(deps)}"
    )


def test_package_json_has_start_script(package_json: dict):
    """package.json exposes a ``start`` script so AppRunner can boot the app."""
    scripts = package_json.get("scripts", {})
    assert scripts.get("start"), "fixture package.json must define a start script"
    assert package_json.get("private") is True


def test_package_lock_parses():
    """The minimal lockfile parses and is a second BaselineGuard-tracked file."""
    lock = json.loads(
        (FIXTURE_DIR / "package-lock.json").read_text(encoding="utf-8")
    )
    assert lock.get("lockfileVersion") == 3
    assert lock.get("name") == "sandbox-shop"
    assert isinstance(lock.get("packages"), dict)


def test_server_js_exists_and_uses_each_dep():
    """server.js exists and references all three mutated dependencies."""
    server = FIXTURE_DIR / "server.js"
    assert server.is_file(), "fixture must include a runnable-looking server.js"
    source = server.read_text(encoding="utf-8")
    for pkg in EXPECTED_DEPS:
        assert pkg in source, f"server.js should use the '{pkg}' dependency"


def test_vendored_stubs_present_for_each_dep():
    """Each mutated dep has a vendored stub so the app runs without npm install."""
    for pkg in EXPECTED_DEPS:
        stub = FIXTURE_DIR / "node_stubs" / pkg / "index.js"
        assert stub.is_file(), f"missing vendored stub for '{pkg}' at {stub}"


def test_default_config_validates_against_fixture():
    """build_default_config + validate_config pass against the fixture.

    This proves the fixture's package.json satisfies the three default profiles'
    target packages: each profile's ``target_package`` is a real dependency in
    the fixture, so fail-fast validation succeeds with no mutation.

    Validates: Requirements 4.1, 4.2, 4.3, 4.4
    """
    config = build_default_config(FIXTURE_DIR, port=3000)
    # Must not raise ConfigError — every default target package is declared.
    validate_config(config)


def test_default_profiles_map_to_fixture_deps():
    """The three default profiles target packages the fixture declares.

    Asserts the profile -> (package, behavior) wiring matches Requirements
    4.1-4.4 and that each target package exists in the fixture.

    Validates: Requirements 4.1, 4.2, 4.3, 4.4
    """
    by_name = {p.name: p for p in DEFAULT_PROFILES}
    deps = set(
        json.loads((FIXTURE_DIR / "package.json").read_text(encoding="utf-8"))[
            "dependencies"
        ]
    )

    # Requirement 4.1: the three named default profiles exist.
    assert set(by_name) == {
        "broken_token_service",
        "crashed_telemetry",
        "compromised_input_handler",
    }

    # Requirements 4.2-4.4: behavior per profile, and the target dep is in the
    # fixture so the profile has a real package to mutate.
    assert by_name["broken_token_service"].behavior is MockBehavior.THROW_UNHANDLED
    assert by_name["crashed_telemetry"].behavior is MockBehavior.THROW_UNHANDLED
    assert (
        by_name["compromised_input_handler"].behavior is MockBehavior.LEAK_DEBUG_STATE
    )

    for profile in DEFAULT_PROFILES:
        assert profile.target_package in deps, (
            f"profile {profile.name!r} targets {profile.target_package!r} "
            f"which is not declared in the fixture"
        )
