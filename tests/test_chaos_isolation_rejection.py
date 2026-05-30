"""Property/unit tests for ChaosMutator mutation isolation, missing-package
rejection, and per-behavior mock content (task 3.5).

This module complements ``test_chaos_revert_property.py`` (which proves
``revert`` is the byte-for-byte inverse of ``inject`` — Property 3). Here we
cover three distinct guarantees that keep a sequential chaos run trustworthy:

1. **Property 2 — Mutation isolation (Requirement 1.3).** Across an arbitrary
   sequence of paired ``inject``/``revert`` calls over profiles targeting
   *distinct* dependencies, at most one mutation is ever active at a time: after
   every ``revert`` there is no active mutation (no ``package.json`` dependency
   points at ``file:./.fileak_mocks/...`` and no per-package mock folder
   exists), and while injected exactly one package points at its mock and
   exactly one mock folder exists. Demonstrated both with a concrete
   ``inject(A) → revert → inject(B) → revert`` example and a Hypothesis-generated
   sequence.

2. **Missing-package rejection (Requirement 2.8).** When a profile's
   ``target_package`` is absent from ``package.json``, ``inject`` raises
   ``ValueError`` naming the missing package and leaves the repo *byte-for-byte*
   unchanged — ``package.json`` bytes are identical and no ``.fileak_mocks/``
   folder is created.

3. **Mock content matches behavior (Requirements 2.2–2.5).** Each of the four
   :class:`MockBehavior` values renders a distinct ``index.js`` whose content
   reflects the misbehavior, and the mock ``package.json`` carries a ``fileak``
   metadata block whose ``behavior`` equals the ``MockBehavior`` value with
   ``main`` pointing at ``index.js``.

Validates: Requirements 1.3, 2.2, 2.3, 2.4, 2.5, 2.8

Framework: pytest for example-based tests (``tmp_path`` fixture) and Hypothesis
for the generated inject/revert sequence (per design "Property Test Library:
Hypothesis (Python)"), each Hypothesis example using its own
``tempfile.TemporaryDirectory`` so re-runs stay isolated.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from fileak.chaos import (
    MOCK_PACKAGE_VERSION,
    MOCKS_DIRNAME,
    ChaosMutator,
    render_mock_module,
)
from fileak.models import ChaosProfile, MockBehavior

# --- Profiles, each targeting a DISTINCT dependency -------------------------
# Mutation isolation is about not letting two mutations coexist, so the profiles
# deliberately target different packages (and span all four behaviors and both
# dependency maps once written into the manifest below).
ISOLATION_PROFILES: dict[str, ChaosProfile] = {
    "broken_token_service": ChaosProfile(
        name="broken_token_service",
        target_package="next-auth",
        behavior=MockBehavior.THROW_UNHANDLED,
        mock_template="auth_verify_throws",
        description="Auth verify() throws mid-flow",
    ),
    "crashed_telemetry": ChaosProfile(
        name="crashed_telemetry",
        target_package="analytics",
        behavior=MockBehavior.RETURN_EMPTY,
        mock_template="empty_telemetry",
        description="Telemetry returns empty objects",
    ),
    "compromised_input_handler": ChaosProfile(
        name="compromised_input_handler",
        target_package="formik",
        behavior=MockBehavior.LEAK_DEBUG_STATE,
        mock_template="leak_state",
        description="Form util leaks raw debug state",
    ),
    "flaky_gateway": ChaosProfile(
        name="flaky_gateway",
        target_package="http-proxy",
        behavior=MockBehavior.HTTP_500,
        mock_template="gateway_500",
        description="Gateway responds 500",
    ),
}

#: A baseline ``package.json`` containing every isolation target — split across
#: ``dependencies`` and ``devDependencies`` so the isolation invariant is checked
#: across both maps — plus noise deps that must never be touched.
def _baseline_manifest() -> dict:
    return {
        "name": "sandbox-shop",
        "version": "1.0.0",
        "scripts": {"start": "node server.js"},
        "dependencies": {
            "next-auth": "^4.24.0",
            "analytics": "~0.8.1",
            "react": "^18.2.0",  # noise — never a target
        },
        "devDependencies": {
            "formik": "2.4.5",
            "http-proxy": "^1.18.1",
            "typescript": "^5.3.0",  # noise — never a target
        },
    }


# Behavior -> substrings the rendered ``index.js`` must contain. These are the
# *functional* markers of each misbehavior, kept as robust substring checks
# rather than brittle exact-string matches (per task guidance).
_INDEX_MARKERS: dict[MockBehavior, list[str]] = {
    MockBehavior.THROW_UNHANDLED: ["throw new Error", "THROW_UNHANDLED"],
    MockBehavior.RETURN_EMPTY: ["return {}", "RETURN_EMPTY"],
    MockBehavior.HTTP_500: ["500", "Internal Server Error", "HTTP_500"],
    MockBehavior.LEAK_DEBUG_STATE: ["process.env", "LEAK_DEBUG_STATE"],
}


def _write_manifest(package_json: Path, manifest: dict) -> None:
    """Write ``manifest`` to ``package_json`` (canonical 2-space + newline form)."""
    package_json.parent.mkdir(parents=True, exist_ok=True)
    package_json.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def _active_mock_packages(package_json: Path) -> list[str]:
    """Return the packages whose ``package.json`` spec points at a local mock.

    A package is "actively mutated" when its dependency spec is a
    ``file:./.fileak_mocks/<pkg>`` path. Both dependency maps are scanned.
    """
    manifest = json.loads(package_json.read_text(encoding="utf-8"))
    prefix = f"file:./{MOCKS_DIRNAME}/"
    active: list[str] = []
    for map_name in ("dependencies", "devDependencies"):
        deps = manifest.get(map_name)
        if isinstance(deps, dict):
            for pkg, spec in deps.items():
                if isinstance(spec, str) and spec.startswith(prefix):
                    active.append(pkg)
    return active


def _present_mock_folders(target: Path) -> list[str]:
    """Return the per-package mock folder names under ``.fileak_mocks/``.

    ``revert`` removes only the per-package folder (``.fileak_mocks/<pkg>``), so
    "active mutation" is modeled by these per-package folders — the parent
    ``.fileak_mocks/`` directory may linger empty and that is not a mutation.
    """
    mocks_dir = target / MOCKS_DIRNAME
    if not mocks_dir.is_dir():
        return []
    return sorted(p.name for p in mocks_dir.iterdir() if p.is_dir())


def _assert_mock_matches_behavior(mock_path: Path, behavior: MockBehavior) -> None:
    """Assert the rendered mock at ``mock_path`` matches ``behavior`` (Req 2.2–2.5)."""
    index_js = (mock_path / "index.js").read_text(encoding="utf-8")
    for marker in _INDEX_MARKERS[behavior]:
        assert marker in index_js, (
            f"{behavior.value} index.js missing expected marker {marker!r}"
        )

    manifest = json.loads((mock_path / "package.json").read_text(encoding="utf-8"))
    assert manifest["main"] == "index.js"
    assert manifest["version"] == MOCK_PACKAGE_VERSION
    assert manifest["fileak"]["mock"] is True
    assert manifest["fileak"]["behavior"] == behavior.value


# ---------------------------------------------------------------------------
# 1. Mutation isolation (Property 2, Requirement 1.3)
# ---------------------------------------------------------------------------
def test_inject_revert_keeps_at_most_one_mutation_active_example(tmp_path: Path) -> None:
    """Property 2: inject(A) → revert → inject(B) → revert is isolated.

    At every instant at most one mutation is active: exactly one while injected,
    none after each revert. The mock rendered during each injection also matches
    its profile's behavior.

    Validates: Requirements 1.3
    """
    target = tmp_path
    package_json = target / "package.json"
    _write_manifest(package_json, _baseline_manifest())

    mutator = ChaosMutator(target, ISOLATION_PROFILES)

    # Baseline: no mutation active before anything runs.
    assert _active_mock_packages(package_json) == []
    assert _present_mock_folders(target) == []

    # inject(A) → exactly one active mutation.
    profile_a = ISOLATION_PROFILES["broken_token_service"]
    record_a = mutator.inject("broken_token_service")
    assert _active_mock_packages(package_json) == [profile_a.target_package]
    assert _present_mock_folders(target) == [profile_a.target_package]
    _assert_mock_matches_behavior(record_a.mock_path, profile_a.behavior)

    # revert(A) → no active mutation.
    mutator.revert(record_a)
    assert _active_mock_packages(package_json) == []
    assert _present_mock_folders(target) == []

    # inject(B) → again exactly one, and it is B (not A — A is fully gone).
    profile_b = ISOLATION_PROFILES["compromised_input_handler"]
    record_b = mutator.inject("compromised_input_handler")
    assert _active_mock_packages(package_json) == [profile_b.target_package]
    assert _present_mock_folders(target) == [profile_b.target_package]
    _assert_mock_matches_behavior(record_b.mock_path, profile_b.behavior)

    # revert(B) → back to no active mutation.
    mutator.revert(record_b)
    assert _active_mock_packages(package_json) == []
    assert _present_mock_folders(target) == []


@settings(deadline=None, max_examples=150)
@given(
    sequence=st.lists(
        st.sampled_from(sorted(ISOLATION_PROFILES)),
        min_size=0,
        max_size=12,
    )
)
def test_inject_revert_sequence_keeps_at_most_one_mutation_active(sequence):
    """Property 2: over any inject→revert sequence, ≤1 mutation is active.

    For an arbitrary sequence of profile names, each ``inject`` is immediately
    paired with its ``revert``. After every ``inject`` exactly one package points
    at its mock and exactly one mock folder exists; after every ``revert`` none
    do. Since injects and reverts are paired, at most one mutation is ever active
    at a time regardless of sequence length.

    Validates: Requirements 1.3
    """
    # Fresh, isolated target dir per example (not a fixture) so Hypothesis
    # re-runs cleanly across examples.
    with tempfile.TemporaryDirectory(prefix="fileak_isolation_") as td:
        target = Path(td)
        package_json = target / "package.json"
        _write_manifest(package_json, _baseline_manifest())

        mutator = ChaosMutator(target, ISOLATION_PROFILES)

        # Invariant holds before the sequence starts.
        assert _active_mock_packages(package_json) == []
        assert _present_mock_folders(target) == []

        for name in sequence:
            profile = ISOLATION_PROFILES[name]

            record = mutator.inject(name)

            # While injected: exactly one mutation active, and it is this one.
            assert _active_mock_packages(package_json) == [profile.target_package]
            assert _present_mock_folders(target) == [profile.target_package]
            assert len(_present_mock_folders(target)) <= 1
            _assert_mock_matches_behavior(record.mock_path, profile.behavior)

            mutator.revert(record)

            # After revert: no mutation active.
            assert _active_mock_packages(package_json) == []
            assert _present_mock_folders(target) == []


# ---------------------------------------------------------------------------
# 2. Missing-package rejection (Requirement 2.8)
# ---------------------------------------------------------------------------
def test_inject_rejects_missing_package_and_leaves_disk_unchanged(tmp_path: Path) -> None:
    """Requirement 2.8: injecting a non-dependency is rejected, disk untouched.

    Build a ``package.json`` WITHOUT the profile's target package, snapshot the
    exact bytes and the directory listing, then assert ``inject`` raises
    ``ValueError`` naming the missing package while leaving ``package.json``
    byte-for-byte identical and creating no ``.fileak_mocks/`` folder.

    Validates: Requirements 2.8
    """
    target = tmp_path
    package_json = target / "package.json"

    # The target package "ghost-dep" is deliberately NOT present in either map.
    manifest = {
        "name": "sandbox-shop",
        "version": "1.0.0",
        "dependencies": {"react": "^18.2.0"},
        "devDependencies": {"typescript": "^5.3.0"},
    }
    _write_manifest(package_json, manifest)

    profile = ChaosProfile(
        name="ghost_profile",
        target_package="ghost-dep",
        behavior=MockBehavior.THROW_UNHANDLED,
        mock_template="ghost_tmpl",
        description="targets a package that does not exist",
    )
    mutator = ChaosMutator(target, {profile.name: profile})

    # Snapshot exact bytes + directory listing BEFORE attempting injection.
    bytes_before = package_json.read_bytes()
    listing_before = sorted(os.listdir(target))

    with pytest.raises(ValueError) as excinfo:
        mutator.inject("ghost_profile")

    # The error message must name the missing package.
    assert "ghost-dep" in str(excinfo.value)

    # package.json is byte-for-byte unchanged...
    assert package_json.read_bytes() == bytes_before
    # ...no mock folder was created...
    assert not (target / MOCKS_DIRNAME).exists()
    # ...and the directory listing is identical (nothing else written either).
    assert sorted(os.listdir(target)) == listing_before


# ---------------------------------------------------------------------------
# 3. Mock content matches behavior (Requirements 2.2–2.5)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("behavior", list(MockBehavior))
def test_rendered_mock_content_matches_behavior(tmp_path: Path, behavior: MockBehavior) -> None:
    """Requirements 2.2–2.5: each behavior renders matching index.js + metadata.

    Render a mock for ``behavior`` via ``render_mock_module`` and assert:
      * ``index.js`` contains the behavior's functional markers
        (THROW_UNHANDLED throws an Error; RETURN_EMPTY returns ``{}``; HTTP_500
        carries 500 / "Internal Server Error" handling; LEAK_DEBUG_STATE echoes
        env/debug state), and
      * the mock ``package.json`` ``fileak.behavior`` equals the behavior value
        with ``main`` set to ``index.js``.

    Validates: Requirements 2.2, 2.3, 2.4, 2.5
    """
    dest = tmp_path / "mock-pkg"
    render_mock_module(dest, behavior, template=f"{behavior.value}_template")

    # Both files exist (postcondition of render_mock_module).
    assert (dest / "index.js").is_file()
    assert (dest / "package.json").is_file()

    # index.js + package.json metadata match the requested behavior.
    _assert_mock_matches_behavior(dest, behavior)


def test_each_behavior_renders_distinct_index_js(tmp_path: Path) -> None:
    """Requirements 2.2–2.5: the four behaviors produce four distinct modules.

    A sanity check that no two behaviors collapse to the same ``index.js`` body —
    each misbehavior must be individually observable.

    Validates: Requirements 2.2, 2.3, 2.4, 2.5
    """
    bodies: dict[MockBehavior, str] = {}
    for behavior in MockBehavior:
        dest = tmp_path / behavior.value
        render_mock_module(dest, behavior, template="distinct_tmpl")
        bodies[behavior] = (dest / "index.js").read_text(encoding="utf-8")

    # All four rendered bodies are mutually distinct.
    rendered = list(bodies.values())
    assert len(set(rendered)) == len(rendered), "two behaviors rendered identical index.js"

    # THROW_UNHANDLED is the only behavior whose module throws an Error directly.
    assert "throw new Error" in bodies[MockBehavior.THROW_UNHANDLED]
    assert "throw new Error" not in bodies[MockBehavior.RETURN_EMPTY]
    assert "throw new Error" not in bodies[MockBehavior.LEAK_DEBUG_STATE]
