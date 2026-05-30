"""Property-based test for ChaosMutator revert-is-inverse (task 3.4).

The single correctness guarantee that lets one chaos profile run after another
without corrupting the target repo is that ``revert`` is the *exact* inverse of
``inject``: after injecting a broken mock and then reverting it, ``package.json``
must come back byte-for-byte and the materialized mock folder must be gone.

**Property 3 — Revert is the inverse of inject.** For an arbitrary valid
``package.json`` structure ``J`` (with the profile's target package present in
either ``dependencies`` or ``devDependencies``) and a profile ``p`` targeting it,
``revert(inject(p, J)) == J`` byte-for-byte, and the mock folder
``<target>/.fileak_mocks/<pkg>`` does not exist afterward.

Validates: Requirements 2.6, 3.1, 3.2, 3.3

Why the baseline must be canonical
----------------------------------
``ChaosMutator._write_manifest`` persists ``package.json`` with
``json.dump(manifest, indent=2)`` plus a trailing newline. The inject→revert
round-trip therefore reproduces the *original bytes* only when those bytes are
already in that canonical serialization (2-space indent, trailing ``\\n``, the
in-memory dict's key order). So this test establishes the baseline by writing the
generated structure with the very same ``json.dumps(obj, indent=2) + "\\n"`` form
the mutator uses, snapshots those exact on-disk bytes, and then asserts the
round-trip reproduces them.

Each generated example runs in its own ``tempfile.TemporaryDirectory`` (not a
pytest fixture) so Hypothesis re-runs cleanly across examples.

Framework: Hypothesis (per design "Property Test Library: Hypothesis (Python)").
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from fileak.chaos import MOCKS_DIRNAME, ChaosMutator
from fileak.models import ChaosProfile, MockBehavior

# Top-level package.json keys this test manages explicitly; arbitrary "extra"
# keys are drawn from outside this set so they never clash with the structural
# entries below.
_RESERVED_KEYS = {"name", "version", "scripts", "dependencies", "devDependencies"}

# Filesystem-safe npm-ish package names: lowercase, no leading dot, no slashes,
# so ``<target>/.fileak_mocks/<pkg>`` is always a single safe folder name.
_package_name = st.from_regex(r"[a-z][a-z0-9-]{0,15}", fullmatch=True)

# Realistic dependency version specs. None of these can collide with the
# ``file:./.fileak_mocks/<pkg>`` spec inject writes, so inject always changes the
# value (keeping the round-trip non-trivial).
_version_spec = st.one_of(
    st.sampled_from(
        ["^1.0.0", "~2.3.4", "1.2.3", ">=1.0.0 <2.0.0", "*", "latest", "next", "0.0.0"]
    ),
    st.from_regex(r"[\^~]?[0-9]{1,2}\.[0-9]{1,2}\.[0-9]{1,2}", fullmatch=True),
)

# Arbitrary JSON-serializable values for the "other" top-level keys, so the
# generated manifests look like real-world package.json files with extra cruft.
_json_scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-1_000_000, max_value=1_000_000),
    st.floats(allow_nan=False, allow_infinity=False, width=64),
    st.text(max_size=20),
)
_json_values = st.recursive(
    _json_scalars,
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(st.text(min_size=1, max_size=8), children, max_size=4),
    ),
    max_leaves=10,
)


@st.composite
def package_json_and_profile(draw):
    """Generate ``(manifest_dict, target_package, profile)``.

    The manifest is an arbitrary valid ``package.json`` structure that always
    contains ``target_package`` in exactly one of ``dependencies`` /
    ``devDependencies`` (so injection is never rejected, and revert round-trips
    through the same map). The ``target_in_dev`` axis exercises both the
    ``dependencies`` and ``devDependencies`` revert paths.
    """
    # Whether the target package lives in devDependencies (vs dependencies).
    target_in_dev = draw(st.booleans())

    # A unique pool of package names; the first becomes the target, the rest are
    # scattered across the two dependency maps as "noise" deps.
    names = draw(st.lists(_package_name, min_size=1, max_size=8, unique=True))
    target_package = names[0]

    deps: dict[str, str] = {}
    devdeps: dict[str, str] = {}
    for name in names[1:]:
        if draw(st.booleans()):
            deps[name] = draw(_version_spec)
        else:
            devdeps[name] = draw(_version_spec)

    # Place the target in its chosen map (guaranteed present, single map only).
    if target_in_dev:
        devdeps[target_package] = draw(_version_spec)
    else:
        deps[target_package] = draw(_version_spec)

    # Assemble the structural entries, then optional metadata + arbitrary extras.
    entries: list[tuple[str, object]] = []
    if deps:
        entries.append(("dependencies", deps))
    if devdeps:
        entries.append(("devDependencies", devdeps))
    if draw(st.booleans()):
        entries.append(("name", draw(st.text(min_size=1, max_size=20))))
    if draw(st.booleans()):
        entries.append(("version", draw(_version_spec)))
    if draw(st.booleans()):
        entries.append(
            (
                "scripts",
                draw(
                    st.dictionaries(
                        st.text(min_size=1, max_size=10),
                        st.text(max_size=20),
                        max_size=4,
                    )
                ),
            )
        )
    extras = draw(
        st.dictionaries(
            st.text(min_size=1, max_size=12).filter(lambda k: k not in _RESERVED_KEYS),
            _json_values,
            max_size=4,
        )
    )
    entries.extend(extras.items())

    # Shuffle the top-level key order so the test does not depend on any fixed
    # ordering; canonical baseline capture handles whatever order results.
    entries = draw(st.permutations(entries))
    manifest = {key: value for key, value in entries}

    behavior = draw(st.sampled_from(list(MockBehavior)))
    profile = ChaosProfile(
        name="revert_roundtrip",
        target_package=target_package,
        behavior=behavior,
        mock_template="revert_roundtrip_template",
        description="round-trip property profile",
    )
    return manifest, target_package, profile


def _write_canonical(package_json: Path, manifest: dict) -> bytes:
    """Write ``manifest`` in the mutator's canonical form and return the bytes.

    Mirrors ``ChaosMutator._write_manifest`` exactly (``json.dump(..., indent=2)``
    + trailing ``"\\n"``), then reads the on-disk bytes back so the returned value
    is the literal byte sequence the round-trip must reproduce.
    """
    package_json.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return package_json.read_bytes()


@settings(deadline=None, max_examples=200)
@given(scenario=package_json_and_profile())
def test_revert_is_byte_for_byte_inverse_of_inject(scenario):
    """Property 3: ``revert(inject(p, J)) == J`` byte-for-byte; mock folder gone.

    Validates: Requirements 2.6, 3.1, 3.2, 3.3
    """
    manifest, target_package, profile = scenario

    # Fresh, isolated target dir per example (not a fixture) so Hypothesis
    # re-runs without cross-example state.
    with tempfile.TemporaryDirectory(prefix="fileak_revert_prop_") as td:
        target = Path(td)
        package_json = target / "package.json"

        # --- 1. Establish the canonical baseline bytes. ----------------------
        baseline_bytes = _write_canonical(package_json, manifest)

        mutator = ChaosMutator(target, {profile.name: profile})
        mock_path = target / MOCKS_DIRNAME / target_package

        # --- 2. Inject: package.json changes and the mock is materialized. ---
        record = mutator.inject(profile.name)

        assert record.target_package == target_package
        assert package_json.read_bytes() != baseline_bytes, (
            "inject() should have rewritten the dependency spec"
        )
        assert mock_path.is_dir(), "inject() should materialize the mock folder"
        assert record.mock_path == mock_path

        # --- 3. Revert: package.json restored exactly; mock folder removed. --
        mutator.revert(record)

        assert package_json.read_bytes() == baseline_bytes, (
            "revert(inject(J)) did not reproduce package.json byte-for-byte"
        )
        assert not mock_path.exists(), (
            "mock folder must be absent after revert (Requirement 3.2)"
        )
