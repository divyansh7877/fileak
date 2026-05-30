"""Example-based unit tests for BaselineGuard restore edge cases (task 2.3).

These complement the property-based test in ``test_baseline_restore_property.py``
with concrete, easy-to-read examples of the trickier restore scenarios:

  1. Restore after a *partial* mutation — only some tracked files are changed,
     others are left untouched, and a ``.fileak_mocks/`` folder exists. After
     ``restore()`` every tracked file is back byte-for-byte and the mock folder
     is gone.
  2. Restore *before* any snapshot — calling ``restore()`` without ever calling
     ``snapshot()`` is a safe no-op: it does not raise, does not alter or delete
     existing files, and does not error on a missing ``.fileak_mocks/`` folder.
  3. Double restore — calling ``restore()`` twice produces the same state
     (idempotence).

Validates: Requirements 10.4

Framework: pytest (uses the ``tmp_path`` fixture for an isolated target dir).
"""

from __future__ import annotations

from pathlib import Path

from fileak.baseline import MOCKS_DIRNAME, BaselineGuard


def _write(path: Path, content: bytes) -> None:
    """Write ``content`` to ``path``, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def test_restore_after_partial_mutation(tmp_path: Path) -> None:
    """Only some tracked files mutate; restore returns all to baseline + drops mocks.

    Validates: Requirements 10.4
    """
    target = tmp_path

    # Two tracked files plus a third absent at snapshot time.
    pkg = target / "package.json"
    lock = target / "package-lock.json"
    absent = target / "tsconfig.json"

    pkg_baseline = b'{"name": "demo", "dependencies": {"left-pad": "^1.0.0"}}'
    lock_baseline = b'{"lockfileVersion": 3}\n'
    _write(pkg, pkg_baseline)
    _write(lock, lock_baseline)
    # ``absent`` is intentionally not created.

    tracked = [Path("package.json"), Path("package-lock.json"), Path("tsconfig.json")]
    guard = BaselineGuard(target, tracked)
    guard.snapshot()

    # --- Partial mutation -------------------------------------------------
    # Mutate only package.json (as inject() would), leave package-lock.json
    # untouched, and materialize a populated .fileak_mocks/ folder.
    pkg.write_bytes(b'{"name": "demo", "dependencies": {"left-pad": "file:./.fileak_mocks/left-pad"}}')

    mock_dir = target / MOCKS_DIRNAME / "left-pad"
    _write(mock_dir / "package.json", b'{"name": "left-pad", "main": "index.js"}')
    _write(mock_dir / "index.js", b"module.exports = () => { throw new Error('boom'); };")

    # Sanity: state really did change before restore.
    assert pkg.read_bytes() != pkg_baseline
    assert (target / MOCKS_DIRNAME).exists()

    # --- Restore ----------------------------------------------------------
    guard.restore()

    # The mutated file is back byte-for-byte.
    assert pkg.read_bytes() == pkg_baseline
    # The untouched file is unchanged.
    assert lock.read_bytes() == lock_baseline
    # The file absent at snapshot time is still absent.
    assert not absent.exists()
    # The injected mock folder is gone entirely.
    assert not (target / MOCKS_DIRNAME).exists()


def test_restore_before_any_snapshot_is_a_safe_noop(tmp_path: Path) -> None:
    """restore() without a prior snapshot() must not raise or touch anything.

    Validates: Requirements 10.4
    """
    target = tmp_path

    pkg = target / "package.json"
    lock = target / "package-lock.json"
    pkg_content = b'{"name": "untouched"}'
    lock_content = b'{"lockfileVersion": 3}'
    _write(pkg, pkg_content)
    _write(lock, lock_content)

    guard = BaselineGuard(target, [Path("package.json"), Path("package-lock.json")])

    # No snapshot() call — restore() must be a no-op and must not raise, even
    # though .fileak_mocks/ does not exist.
    assert not (target / MOCKS_DIRNAME).exists()
    guard.restore()

    # Existing files are completely untouched.
    assert pkg.read_bytes() == pkg_content
    assert lock.read_bytes() == lock_content
    # Still no mock folder, and no spurious files were created.
    assert not (target / MOCKS_DIRNAME).exists()


def test_double_restore_is_idempotent(tmp_path: Path) -> None:
    """Calling restore() twice yields the same state as calling it once.

    Validates: Requirements 10.4
    """
    target = tmp_path

    pkg = target / "package.json"
    absent = target / "package-lock.json"  # absent at snapshot time
    pkg_baseline = b'{"name": "demo", "version": "1.0.0"}'
    _write(pkg, pkg_baseline)

    tracked = [Path("package.json"), Path("package-lock.json")]
    guard = BaselineGuard(target, tracked)
    guard.snapshot()

    # Mutate the present file, create the absent one, and inject a mock folder.
    pkg.write_bytes(b'{"name": "demo", "version": "9.9.9-mutated"}')
    _write(absent, b'{"lockfileVersion": 3}')
    _write(target / MOCKS_DIRNAME / "left-pad" / "index.js", b"throw new Error('x');")

    # First restore brings everything back to baseline.
    guard.restore()
    first_pkg = pkg.read_bytes()
    first_absent_exists = absent.exists()
    first_mock_exists = (target / MOCKS_DIRNAME).exists()

    assert first_pkg == pkg_baseline
    assert first_absent_exists is False
    assert first_mock_exists is False

    # Second restore must not raise and must reproduce the identical state.
    guard.restore()

    assert pkg.read_bytes() == first_pkg == pkg_baseline
    assert absent.exists() == first_absent_exists is False
    assert (target / MOCKS_DIRNAME).exists() == first_mock_exists is False
