"""Unit + property tests for AppRunner process hygiene (task 7.3).

**Property 8 — Process hygiene.** After :meth:`AppRunner.stop`, no child
process spawned by :meth:`AppRunner.start` remains alive and the configured
port is free.

Validates: Requirements 5.6, 5.7

These tests are fully **offline**: no real subprocess is spawned, no socket is
bound, and no network request is made. Every externally-uncertain operation is
funnelled through :class:`AppRunner`'s injectable seams:

* ``spawn``      — returns a :class:`FakeProcess` modelling a small process
  *tree* (the leader plus an arbitrary number of fake children) and records it
  in a shared box so the stateful ``port_probe`` can consult its liveness.
* ``terminate``  — the teardown seam ``stop()`` invokes; here it marks the
  whole fake tree dead. (The default teardown calls :func:`os.killpg` against a
  real PID, which cannot model a fake tree and could signal an unrelated real
  process — so we inject a tree-aware seam instead.)
* ``port_probe`` — a stateful closure: the port reads *free* before anything is
  spawned (``start()`` runs its port pre-check *before* spawning), *bound* while
  the fake leader is alive, and *free* again once it is terminated.
* ``http_probe`` — returns ``200`` immediately so ``start()`` reaches readiness
  on the first poll without any real HTTP.
* ``install_runner`` / ``sleep`` / ``monotonic`` — stubbed so install always
  succeeds and the poll clock is deterministic.

Framework: Hypothesis (per design "Property Test Library: Hypothesis (Python)").
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from fileak.app_runner import AppRunner
from fileak.models import AppHandle

# --------------------------------------------------------------------------
# Fakes / seams (kept local to the test module)
# --------------------------------------------------------------------------

_LEADER_PID = 4242
_CHILD_PID_BASE = 5000


class FakeChild:
    """A fake child process in the spawned tree.

    Models only what "is this child still alive?" requires: a ``pid`` and a
    ``poll()`` returning ``None`` while alive and an exit code once killed.
    """

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self._returncode: int | None = None

    def poll(self) -> int | None:
        return self._returncode

    def mark_dead(self) -> None:
        if self._returncode is None:
            self._returncode = 0

    def alive(self) -> bool:
        return self._returncode is None


class FakeProcess:
    """Minimal stand-in for a spawned process *tree* (no real OS process).

    Exposes the surface :class:`AppRunner` relies on — ``pid``, ``poll()``
    (``None`` while alive, exit code once dead), ``wait()``, and ``send_signal``
    — and additionally models a set of fake child processes. Tearing the leader
    down (``mark_dead``) propagates to every child, so "no child spawned by
    ``start()`` remains alive" is a meaningful, checkable claim.
    """

    def __init__(self, pid: int = _LEADER_PID, child_count: int = 0) -> None:
        self.pid = pid
        self.children = [
            FakeChild(_CHILD_PID_BASE + i) for i in range(child_count)
        ]
        self._returncode: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self._returncode

    def wait(self, timeout: float | None = None) -> int:
        # Reap is a no-op for the fake; report a clean exit code.
        return 0 if self._returncode is None else self._returncode

    def send_signal(self, sig: int) -> None:
        # Fallback teardown path: any signal collapses the whole tree.
        self.mark_dead()

    def mark_dead(self) -> None:
        """Mark the leader and every child as no longer alive (tree teardown)."""
        self.terminated = True
        if self._returncode is None:
            self._returncode = 0
        for child in self.children:
            child.mark_dead()

    def alive(self) -> bool:
        return self._returncode is None

    def tree_members(self) -> list[object]:
        """The leader plus all children — the whole process group."""
        return [self, *self.children]


def _make_clock():
    """Return a monotonically increasing fake clock so polling never stalls."""
    state = {"t": 0.0}

    def monotonic() -> float:
        state["t"] += 0.01
        return state["t"]

    return monotonic


def build_app_runner(spawned_box: dict, logs_dir: Path, *, child_count: int = 0,
                     port: int = 3000) -> AppRunner:
    """Construct a real :class:`AppRunner` with every external seam stubbed.

    ``spawned_box`` is a shared mutable cell: ``spawn`` records the
    :class:`FakeProcess` it created under the key ``"proc"`` so the stateful
    ``port_probe`` can decide bound-vs-free from the tree's liveness.

    Port semantics modelled (Requirement 5.7):
      * before ``start()`` spawns anything → box empty → port **free**,
      * while the fake leader is alive       → port **bound**,
      * after ``stop()`` kills the tree      → port **free** again.
    """

    def install_runner(cmd, cwd):
        return (0, "")  # install always succeeds

    def spawn(cmd, cwd, log_fh):
        proc = FakeProcess(pid=_LEADER_PID, child_count=child_count)
        spawned_box["proc"] = proc
        return proc

    def http_probe(url):
        return 200  # ready on the first poll — no real HTTP

    def terminate(proc):
        # The seam stop() invokes: collapse the entire fake tree (no real
        # signals / no os.killpg against a real PID).
        proc.mark_dead()

    def port_probe(host, port_arg):
        proc = spawned_box.get("proc")
        # Bound only while a spawned leader is still alive; free otherwise.
        return proc is not None and proc.alive()

    return AppRunner(
        Path(logs_dir),  # target_dir (unused offline)
        ["npm", "run", "start"],
        ["npm", "install"],
        port,
        readiness_path="/",
        boot_timeout_s=5.0,
        spawn=spawn,
        install_runner=install_runner,
        http_probe=http_probe,
        terminate=terminate,
        port_probe=port_probe,
        sleep=lambda _s: None,
        monotonic=_make_clock(),
        logs_dir=Path(logs_dir),
    )


def _assert_tree_dead_and_port_free(runner: AppRunner, proc: FakeProcess,
                                    port: int) -> None:
    """Assert Property 8 postconditions: whole tree dead AND port free."""
    # (a) the leader and EVERY child report not-alive (poll() != None).
    for member in proc.tree_members():
        assert member.poll() is not None, (
            f"member pid={getattr(member, 'pid', '?')} still alive after stop()"
        )
    # (b) the port now reads free (Requirement 5.7).
    assert runner._port_probe("127.0.0.1", port) is False, (
        "port still reported bound after stop()"
    )


# --------------------------------------------------------------------------
# Unit tests
# --------------------------------------------------------------------------


def test_stop_leaves_no_child_alive_and_frees_port():
    """start() then stop(): whole spawned tree is dead and the port is free.

    Validates: Requirements 5.6, 5.7
    """
    with tempfile.TemporaryDirectory(prefix="fileak_hygiene_unit_") as td:
        spawned: dict = {}
        port = 3000
        runner = build_app_runner(spawned, Path(td), child_count=3, port=port)

        # Port must read FREE before start (pre-check runs before spawn).
        assert runner._port_probe("127.0.0.1", port) is False

        handle = runner.start()

        # start() returned a live handle carrying the fake leader's pid.
        assert isinstance(handle, AppHandle)
        assert handle.pid == _LEADER_PID
        assert handle.base_url == f"http://localhost:{port}"

        proc: FakeProcess = spawned["proc"]
        # While alive, the port is considered bound.
        assert proc.alive() is True
        assert runner._port_probe("127.0.0.1", port) is True

        runner.stop()

        # (a) leader + all 3 children dead; (b) port free.
        _assert_tree_dead_and_port_free(runner, proc, port)
        assert proc.terminated is True


def test_stop_is_idempotent_and_keeps_state_clean():
    """Calling stop() repeatedly never raises and the tree stays dead / port free.

    Validates: Requirements 5.6, 5.7
    """
    with tempfile.TemporaryDirectory(prefix="fileak_hygiene_idem_") as td:
        spawned: dict = {}
        port = 4100
        runner = build_app_runner(spawned, Path(td), child_count=2, port=port)

        runner.start()
        proc: FakeProcess = spawned["proc"]

        runner.stop()
        _assert_tree_dead_and_port_free(runner, proc, port)

        # Second and third stop() are no-ops: must not raise, state unchanged.
        runner.stop()
        runner.stop()
        _assert_tree_dead_and_port_free(runner, proc, port)


def test_stop_before_start_is_a_noop():
    """stop() with nothing started never raises (idempotent teardown).

    Validates: Requirements 5.6, 5.7
    """
    with tempfile.TemporaryDirectory(prefix="fileak_hygiene_noop_") as td:
        spawned: dict = {}
        runner = build_app_runner(spawned, Path(td), child_count=0)
        # No start() was called; stop() must be a harmless no-op.
        runner.stop()
        assert "proc" not in spawned


# --------------------------------------------------------------------------
# Property test — Property 8 over arbitrary tree sizes
# --------------------------------------------------------------------------


@settings(deadline=None, max_examples=100)
@given(child_count=st.integers(min_value=0, max_value=10))
def test_process_hygiene_for_arbitrary_tree_size(child_count):
    """Property 8: for ANY tree size, stop() kills all of it and frees the port.

    For a spawned tree of ``child_count`` (0..10) fake children, after
    ``start()`` then ``stop()`` every process (leader + children) reports
    not-alive and the configured port reads free.

    Validates: Requirements 5.6, 5.7
    """
    # Fresh, isolated state per generated example (no shared state, no fixture)
    # so Hypothesis re-runs cleanly across examples.
    with tempfile.TemporaryDirectory(prefix="fileak_hygiene_prop_") as td:
        spawned: dict = {}
        port = 3000
        runner = build_app_runner(
            spawned, Path(td), child_count=child_count, port=port
        )

        # Pre-start invariant: port free before anything is spawned.
        assert runner._port_probe("127.0.0.1", port) is False

        handle = runner.start()
        assert isinstance(handle, AppHandle)
        assert handle.pid == _LEADER_PID

        proc: FakeProcess = spawned["proc"]
        assert len(proc.children) == child_count
        # All members alive immediately after a successful start.
        assert all(m.poll() is None for m in proc.tree_members())

        runner.stop()

        # Postcondition: the ENTIRE tree is dead and the port is free.
        _assert_tree_dead_and_port_free(runner, proc, port)
