"""BaselineGuard — snapshot and unconditional restore.

This is the most safety-critical component of the engine. Because the engine
mutates a real target repository (``package.json`` / ``package-lock.json`` and a
``.fileak_mocks/`` folder), the single most important invariant is that the
target is always returned to its pre-run baseline — even on crash or Ctrl-C.

``BaselineGuard`` snapshots the exact files the engine may touch into a private
temp store and restores them byte-for-byte. Restoration also removes the
injected ``.fileak_mocks/`` folder. ``restore()`` is idempotent and a no-op when
``snapshot()`` has not run, so it is safe to call from a loop ``finally`` block
and from a SIGINT/SIGTERM signal handler.

Stdlib only: ``pathlib``, ``shutil``, ``tempfile``.

Formal specification (from design.md):

``restore()``
    Preconditions: ``snapshot()`` has run at least once (else no-op).
    Postconditions: every tracked file equals its snapshot byte-for-byte; the
    ``.fileak_mocks/`` folder is absent. Safe to call repeatedly (idempotent).
    Loop invariant: after restoring file *k*, files 1..k match their snapshots.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

#: Name of the injected mock folder that ``restore()`` must remove.
MOCKS_DIRNAME = ".fileak_mocks"


@dataclass
class _SnapshotEntry:
    """One tracked file's snapshot record.

    ``stored_path`` holds the byte-for-byte backup copy when the tracked file
    existed at snapshot time; it is ``None`` when the file was absent, in which
    case ``restore()`` must ensure the file remains absent.
    """

    relative_path: Path
    stored_path: Path | None


class BaselineGuard:
    """Snapshots tracked files and restores them unconditionally.

    Args:
        target_dir: The target application directory. Tracked files are
            resolved relative to this directory, and the ``.fileak_mocks/``
            folder removed on restore lives directly under it.
        tracked_files: Paths (typically relative, e.g. ``package.json`` and
            ``package-lock.json``) of the files to snapshot and restore. Each is
            resolved against ``target_dir``.
    """

    def __init__(self, target_dir: Path, tracked_files: list[Path]) -> None:
        self.target_dir = Path(target_dir)
        self.tracked_files = [Path(f) for f in tracked_files]
        self._store_dir: Path | None = None
        self._entries: list[_SnapshotEntry] = []
        self._snapshotted = False

    def snapshot(self) -> None:
        """Copy every tracked file into a private temp store.

        Tracked files that do not exist at snapshot time are recorded as absent
        so that ``restore()`` can return the target to that exact state. Calling
        ``snapshot()`` again replaces any previous snapshot.
        """
        store_dir = Path(tempfile.mkdtemp(prefix="fileak_baseline_"))
        entries: list[_SnapshotEntry] = []

        for index, relative_path in enumerate(self.tracked_files):
            source = self.target_dir / relative_path
            if source.is_file():
                # Use an index-based filename in the store to avoid collisions
                # and path-traversal issues from nested or absolute entries.
                stored_path = store_dir / f"{index}.bak"
                # shutil.copyfile copies raw file contents (binary), giving a
                # byte-for-byte backup independent of text encoding.
                shutil.copyfile(source, stored_path)
                entries.append(_SnapshotEntry(relative_path, stored_path))
            else:
                # File absent at snapshot time -> restore must keep it absent.
                entries.append(_SnapshotEntry(relative_path, None))

        self._store_dir = store_dir
        self._entries = entries
        self._snapshotted = True

    def restore(self) -> None:
        """Restore tracked files to their snapshot and remove ``.fileak_mocks/``.

        Writes each tracked file back byte-for-byte. Files that were absent at
        snapshot time are removed if they now exist. Finally, the
        ``.fileak_mocks/`` folder under ``target_dir`` is removed so it does not
        exist afterward.

        This is a no-op if ``snapshot()`` has not run, and it is idempotent:
        repeated calls produce the same restored state.
        """
        if not self._snapshotted:
            return

        for entry in self._entries:
            destination = self.target_dir / entry.relative_path
            if entry.stored_path is None:
                # The file did not exist at snapshot time; ensure it is absent.
                self._remove_path(destination)
            else:
                # Recreate any parent directory that may have been removed,
                # then write the snapshot bytes back verbatim.
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(entry.stored_path, destination)

        # Remove the injected mock folder so it does not exist after restore.
        self._remove_path(self.target_dir / MOCKS_DIRNAME)

    @staticmethod
    def _remove_path(path: Path) -> None:
        """Remove a file or directory if present; no-op when already absent."""
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.exists() or path.is_symlink():
            path.unlink()
