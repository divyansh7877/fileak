"""Property-based test for BaselineGuard restore safety (task 2.2).

This is the single most safety-critical invariant in the engine: the target
repository must always be returned to its pre-run baseline, byte-for-byte, with
the injected ``.fileak_mocks/`` folder gone — no matter what mutations happened
in between, and no matter how many times ``restore()`` is called.

**Property 1 — Restore safety (most critical).** For an arbitrary set of tracked
files (present or absent at snapshot time) and arbitrary subsequent mutations to
those files and to ``.fileak_mocks/``, after ``snapshot()`` then ``restore()``:

  * every tracked file equals its snapshot byte-for-byte (or is absent if it was
    absent at snapshot time), and
  * the ``.fileak_mocks/`` folder does not exist, and
  * calling ``restore()`` a second time yields the exact same state
    (idempotence).

Validates: Requirements 10.1, 10.2, 10.3, 10.4

Framework: Hypothesis (per design "Property Test Library: Hypothesis (Python)").
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from fileak.baseline import MOCKS_DIRNAME, BaselineGuard

# Realistic tracked-file names the engine actually touches (design tracks
# ``package.json`` / ``package-lock.json``), plus a nested path to exercise
# parent-directory recreation on restore.
TRACKED_POOL = [
    "package.json",
    "package-lock.json",
    "tsconfig.json",
    "yarn.lock",
    "config/settings.json",
]

# Relative paths populated inside the injected mock folder; one is nested.
MOCK_FILE_POOL = ["index.js", "package.json", "data.bin", "nested/inner.txt"]


@st.composite
def restore_scenarios(draw):
    """Generate a complete restore scenario.

    Produces:
      * ``names``     — a non-empty, unique set of tracked files,
      * ``per_file``  — for each file an ``(initial, mutation)`` pair where
        ``initial`` is ``None`` (absent at snapshot) or the snapshot bytes, and
        ``mutation`` is ``("set", bytes)`` / ``("delete",)`` / ``("keep",)``,
      * ``mock_files``— files to drop inside ``.fileak_mocks/`` (may be empty),
      * ``make_empty_mock`` — whether to also create the mock dir when empty.
    """
    names = draw(
        st.lists(
            st.sampled_from(TRACKED_POOL),
            min_size=1,
            max_size=len(TRACKED_POOL),
            unique=True,
        )
    )
    per_file = []
    for _ in names:
        initial = draw(st.one_of(st.none(), st.binary(max_size=128)))
        mutation = draw(
            st.one_of(
                st.tuples(st.just("set"), st.binary(max_size=128)),
                st.just(("delete",)),
                st.just(("keep",)),
            )
        )
        per_file.append((initial, mutation))

    mock_files = draw(
        st.dictionaries(
            keys=st.sampled_from(MOCK_FILE_POOL),
            values=st.binary(max_size=64),
            max_size=len(MOCK_FILE_POOL),
        )
    )
    make_empty_mock = draw(st.booleans())
    return names, per_file, mock_files, make_empty_mock


def _read_state(target: Path, names: list[Path]) -> dict[Path, bytes | None]:
    """Snapshot the current on-disk bytes of each tracked file (``None`` if absent)."""
    state: dict[Path, bytes | None] = {}
    for name in names:
        path = target / name
        state[name] = path.read_bytes() if path.is_file() else None
    return state


@settings(deadline=None, max_examples=200)
@given(scenario=restore_scenarios())
def test_restore_returns_target_to_snapshot_and_is_idempotent(scenario):
    """Property 1: restore is byte-for-byte exact, mock-free, and idempotent.

    Validates: Requirements 10.1, 10.2, 10.3, 10.4
    """
    name_strs, per_file, mock_files, make_empty_mock = scenario
    names = [Path(n) for n in name_strs]

    # A fresh, isolated target dir per generated example (not a pytest fixture,
    # so Hypothesis re-runs cleanly across examples).
    with tempfile.TemporaryDirectory(prefix="fileak_restore_prop_") as td:
        target = Path(td)

        # --- 1. Lay down the initial (pre-snapshot) state. -------------------
        # ``expected`` is the exact state restore() must reproduce: bytes for
        # files present at snapshot time, ``None`` for files that were absent.
        expected: dict[Path, bytes | None] = {}
        for name, (initial, _mutation) in zip(names, per_file):
            path = target / name
            if initial is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(initial)
            expected[name] = initial

        # --- 2. Snapshot the baseline (Requirement 10.1). --------------------
        guard = BaselineGuard(target, names)
        guard.snapshot()

        # --- 3. Apply arbitrary mutations to tracked files. ------------------
        for name, (_initial, mutation) in zip(names, per_file):
            path = target / name
            if mutation[0] == "set":
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(mutation[1])
            elif mutation[0] == "delete" and path.exists():
                path.unlink()
            # "keep" leaves the file exactly as it was.

        # --- 3b. Inject a populated (or empty) .fileak_mocks/ folder. --------
        mock_dir = target / MOCKS_DIRNAME
        if mock_files or make_empty_mock:
            mock_dir.mkdir(parents=True, exist_ok=True)
            for rel, content in mock_files.items():
                mock_path = mock_dir / rel
                mock_path.parent.mkdir(parents=True, exist_ok=True)
                mock_path.write_bytes(content)

        # --- 4. Restore and assert byte-for-byte equality + no mock dir. -----
        guard.restore()

        for name, want in expected.items():
            path = target / name
            if want is None:
                assert not path.exists(), (
                    f"{name} was absent at snapshot time but exists after restore"
                )
            else:
                assert path.read_bytes() == want, (
                    f"{name} was not restored byte-for-byte"
                )
        assert not mock_dir.exists(), ".fileak_mocks/ still present after restore"

        first_state = _read_state(target, names)

        # --- 5. Restore again: state must be identical (idempotence, 10.4). --
        guard.restore()
        second_state = _read_state(target, names)

        assert second_state == first_state, "restore() was not idempotent"
        assert not mock_dir.exists(), ".fileak_mocks/ reappeared after 2nd restore"
