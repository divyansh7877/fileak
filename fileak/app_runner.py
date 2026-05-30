"""AppRunner — target application lifecycle manager.

The :class:`AppRunner` is the engine's background subprocess manager. It clears
the package cache, runs the install command (so the injected mock is linked),
boots the target app on a local port in its own process group, polls a readiness
URL, and tears the process tree down cleanly.

Scope of this module so far (task 7.1 — *install + process-group start with
readiness polling*):

* :meth:`AppRunner.install` — clear ``node_modules/.cache`` and run the
  configured install command; raise :class:`~fileak.models.BootError` on a
  non-zero exit (design "Error Handling — Scenario 1": a failed install is a
  boot failure the orchestrator records as ``INCONCLUSIVE``).
* :meth:`AppRunner.start` — spawn the app as a subprocess in its own process
  group (``start_new_session=True`` so the whole tree can be signalled later),
  capture stdout/stderr to a log file, and poll the readiness URL until HTTP 200
  or ``boot_timeout_s``. On timeout the process is terminated and a
  :class:`BootError` is raised; if the process exits *during* polling, polling
  aborts immediately with a :class:`BootError`.
* :meth:`AppRunner.captured_stderr` — best-effort read-back of the captured
  process output (never raises).
* :meth:`AppRunner.stop` — full process-*tree* teardown (SIGTERM then SIGKILL
  via :func:`os.killpg` against the whole process group, then reap) so that
  after ``stop`` no child spawned by :meth:`start` is left alive and the port is
  released (task 7.2 / Requirements 5.6, 5.7). Idempotent and never-raising — it
  is called from ``finally`` / timeout / signal paths.

Scope added in task 7.2 — *port pre-check + process-tree teardown*:

* :meth:`AppRunner.start` now performs a **port pre-check first** (before opening
  the log or spawning anything): if the configured port is already bound it
  fails fast with an actionable :class:`~fileak.models.BootError` naming the port
  and suggesting ``--port`` (design "Error Handling — Scenario 5"; Requirement
  11.5 / 5.5 fail-fast). The probe is funnelled through the injectable
  ``port_probe`` seam so offline tests can report "free" without binding a real
  socket.

Formal specification — ``AppRunner.start`` (from design.md)::

    def start(self) -> AppHandle:
        proc = spawn(start_cmd, cwd=target_dir, stdout=log, stderr=log,
                     new_process_group=True)
        deadline = now() + boot_timeout_s
        while now() < deadline:
            ASSERT proc.is_alive()      # if it dies, abort immediately
            if http_get(readiness_url).status == 200:
                return AppHandle(proc.pid, base_url, log_path)
            sleep(0.5)
        self.stop()
        raise BootError(...)

    Preconditions: install completed; ``port`` is free.
    Postconditions: returns a live ``AppHandle`` whose ``base_url`` answers 200,
    OR raises ``BootError`` after terminating the process.
    Loop invariant: the spawned process is alive on every poll iteration; if it
    dies, polling aborts immediately with ``BootError``.

Testability / offline design
----------------------------
To keep the unit/property/integration suites fully offline (no real servers, no
network) the externally-uncertain operations are funnelled through single,
injectable seams (keyword-only constructor args; they do not affect the public
design interface):

* ``spawn``         — ``(cmd, cwd, log_fh) -> proc`` (proc exposes ``.pid`` /
  ``.poll()``); default uses :class:`subprocess.Popen` with
  ``start_new_session=True``.
* ``install_runner``— ``(cmd, cwd) -> (returncode, combined_output)``; default
  wraps :func:`subprocess.run`.
* ``http_probe``    — ``(url) -> status_int | None``; default uses
  :func:`urllib.request.urlopen` and treats connection errors as "not ready".
* ``terminate``     — ``(proc) -> None`` process-tree teardown; default signals
  the process group (SIGTERM → SIGKILL).
* ``port_probe``    — ``(host, port) -> bool`` returns ``True`` when something is
  already listening on ``(host, port)``; default uses :mod:`socket` to attempt a
  short-timeout TCP connect to localhost. Stubbed to "free" in offline tests.
* ``sleep`` / ``monotonic`` — the poll clock, injectable so timeouts are
  deterministic in tests.

Stdlib only: ``subprocess``, ``os``, ``signal``, ``socket``, ``time``,
``urllib.request``, ``shutil``, ``tempfile``, ``pathlib``.
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from .models import AppHandle, BootError

#: A callable that spawns the app and returns a process handle. The handle MUST
#: expose ``.pid`` and ``.poll()`` (``None`` while alive, exit code once dead).
#: ``log_fh`` is an open, writable file object the child's stdout/stderr is
#: redirected to.
SpawnFn = Callable[[list[str], Path, "object"], "object"]

#: A callable that runs the install command and returns
#: ``(returncode, combined_output)``.
InstallRunner = Callable[[list[str], Path], "tuple[int, str]"]

#: A callable that probes a URL and returns its HTTP status code, or ``None``
#: when the endpoint is unreachable / not yet accepting connections.
HttpProbe = Callable[[str], Optional[int]]

#: A callable that terminates a spawned process (and its tree). Best-effort and
#: idempotent: it must not raise if the process is already gone.
TerminateFn = Callable[["object"], None]

#: A callable that reports whether ``(host, port)`` is already bound (something
#: is listening). Returns ``True`` when the port is in use, ``False`` when free.
PortProbeFn = Callable[[str, int], bool]

#: Injectable poll clock seams (default to the stdlib equivalents).
SleepFn = Callable[[float], None]
MonotonicFn = Callable[[], float]

#: Default interval between readiness polls (design pseudocode: ``sleep(0.5)``).
_DEFAULT_POLL_INTERVAL_S = 0.5

#: Per-probe connect/read timeout for the default HTTP readiness check, so a
#: hung connection cannot stall a single poll iteration.
_READINESS_PROBE_TIMEOUT_S = 2.0

#: Connect timeout (seconds) for the default TCP port pre-check. Short so the
#: fail-fast probe cannot stall start(); a refused connection returns instantly.
_PORT_PROBE_TIMEOUT_S = 0.5

#: Grace period (seconds) the default teardown waits after SIGTERM before
#: escalating to SIGKILL.
_TERMINATION_GRACE_S = 5.0

#: Poll interval (seconds) while waiting for a signalled process to exit.
_TERMINATION_POLL_S = 0.1

#: Cap (chars) on the install-output tail embedded in a failure message so a
#: noisy install log cannot produce an unwieldy exception.
_INSTALL_ERROR_TAIL_CHARS = 4000

#: Subdirectory of ``node_modules`` cleared before install so the freshly
#: linked mock is not shadowed by a stale build cache.
_CACHE_RELATIVE = ("node_modules", ".cache")


def _tail(text: str, max_chars: int) -> str:
    """Return at most the last ``max_chars`` characters of ``text``.

    Used to keep install-failure messages bounded; prefixes an elision marker
    when the text was truncated.
    """
    if len(text) <= max_chars:
        return text
    return "...\n" + text[-max_chars:]


class AppRunner:
    """Background subprocess lifecycle manager for the target app.

    Args:
        target_dir: The target application directory. Install and start commands
            run with this as their working directory; ``node_modules/.cache``
            under it is cleared before install.
        start_cmd: Command (argv list) that boots the app, e.g.
            ``["npm", "run", "start"]``.
        install_cmd: Command (argv list) that installs dependencies, e.g.
            ``["npm", "install"]``.
        port: Local TCP port the app is expected to listen on. ``base_url`` is
            ``http://localhost:<port>``.
        readiness_path: Path appended to ``base_url`` to form the readiness URL
            (defaults to ``"/"``).
        boot_timeout_s: Maximum time to wait for the app to answer HTTP 200
            before terminating it and raising :class:`BootError`.

    Keyword-only test seams (do not affect the public design interface):
        spawn: ``(cmd, cwd, log_fh) -> proc`` used instead of the default
            :class:`subprocess.Popen` spawn.
        install_runner: ``(cmd, cwd) -> (returncode, output)`` used instead of
            the default :func:`subprocess.run` install.
        http_probe: ``(url) -> status | None`` used instead of the default
            :func:`urllib.request.urlopen` probe.
        terminate: ``(proc) -> None`` used instead of the default process-group
            teardown.
        port_probe: ``(host, port) -> bool`` used instead of the default
            :mod:`socket` TCP pre-check; returns ``True`` when the port is
            already bound. Stubbed to "free" in offline tests so a fake spawn
            need not bind a real port.
        sleep / monotonic: poll-clock seams (default :func:`time.sleep` /
            :func:`time.monotonic`).
        poll_interval_s: Interval between readiness polls (default ``0.5``).
        logs_dir: Directory captured logs are written to. Defaults to a private
            temp directory created on first :meth:`start` so the target repo
            stays clean (restore-safety).
    """

    def __init__(
        self,
        target_dir: Path,
        start_cmd: list[str],
        install_cmd: list[str],
        port: int,
        readiness_path: str = "/",
        boot_timeout_s: float = 60.0,
        *,
        spawn: Optional[SpawnFn] = None,
        install_runner: Optional[InstallRunner] = None,
        http_probe: Optional[HttpProbe] = None,
        terminate: Optional[TerminateFn] = None,
        port_probe: Optional[PortProbeFn] = None,
        sleep: Optional[SleepFn] = None,
        monotonic: Optional[MonotonicFn] = None,
        poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
        logs_dir: Optional[Path] = None,
    ) -> None:
        self.target_dir = Path(target_dir)
        self.start_cmd = list(start_cmd)
        self.install_cmd = list(install_cmd)
        self.port = port
        self.readiness_path = readiness_path
        self.boot_timeout_s = boot_timeout_s
        self.poll_interval_s = poll_interval_s

        self.base_url = f"http://localhost:{port}"
        self.readiness_url = self._build_readiness_url(self.base_url, readiness_path)

        # Injectable seams (default to the real, side-effecting implementations).
        self._spawn = spawn or self._default_spawn
        self._run_install = install_runner or self._default_install_runner
        self._http_probe = http_probe or self._default_http_probe
        self._terminate = terminate or self._default_terminate
        self._port_probe = port_probe or self._default_port_probe
        self._sleep = sleep or time.sleep
        self._monotonic = monotonic or time.monotonic

        # Mutable runtime state, populated by start()/stop().
        self._logs_dir: Optional[Path] = Path(logs_dir) if logs_dir else None
        self._proc: Optional[object] = None
        self._log_fh = None
        self._log_path: Optional[Path] = None
        self._start_count = 0
        # Termination tunables (kept as instance attrs so task 7.2 can adjust).
        self._termination_grace_s = _TERMINATION_GRACE_S

    # -- URL helpers --------------------------------------------------------

    @staticmethod
    def _build_readiness_url(base_url: str, readiness_path: str) -> str:
        """Join ``base_url`` and ``readiness_path`` into a readiness URL.

        Tolerates a leading/trailing slash on either side so e.g. ``"/"``,
        ``"health"``, and ``"/health"`` all produce a well-formed URL.
        """
        path = (readiness_path or "/").lstrip("/")
        return base_url.rstrip("/") + "/" + path

    # -- install (task 7.1) -------------------------------------------------

    def install(self) -> None:
        """Clear the package cache and run the install command.

        Removes ``<target_dir>/node_modules/.cache`` (so a stale build cache
        cannot shadow the freshly linked mock) and then runs ``install_cmd`` with
        ``cwd=target_dir`` via the (injectable) install runner.

        Raises:
            BootError: If the install command exits non-zero. Per the design's
                "Error Handling — Scenario 1", a failed install is a boot failure
                the orchestrator records as ``INCONCLUSIVE``; the message names
                the command, the working directory, and includes a bounded tail
                of the captured install output.
        """
        self._clear_cache()

        returncode, output = self._run_install(self.install_cmd, self.target_dir)
        if returncode != 0:
            tail = _tail(output or "", _INSTALL_ERROR_TAIL_CHARS)
            raise BootError(
                f"install command {self.install_cmd!r} failed with exit code "
                f"{returncode} in {self.target_dir}"
                + (f"\n--- install output (tail) ---\n{tail}" if tail else "")
            )

    def _clear_cache(self) -> None:
        """Remove ``node_modules/.cache`` under the target dir if it exists.

        Best-effort and idempotent: a missing cache directory is not an error.
        """
        cache_dir = self.target_dir.joinpath(*_CACHE_RELATIVE)
        if cache_dir.is_dir() and not cache_dir.is_symlink():
            shutil.rmtree(cache_dir, ignore_errors=True)

    def _default_install_runner(
        self, cmd: list[str], cwd: Path
    ) -> "tuple[int, str]":
        """Run ``cmd`` in ``cwd`` capturing combined stdout/stderr.

        Returns ``(returncode, combined_output)``. This is the single subprocess
        seam for install, stubbed in offline tests.
        """
        completed = subprocess.run(
            cmd,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        return completed.returncode, completed.stdout or ""

    # -- start + readiness polling (task 7.1) ------------------------------

    def start(self) -> AppHandle:
        """Spawn the app in its own process group and block until ready.

        Opens a capture log file, spawns ``start_cmd`` (stdout+stderr redirected
        to the log; new process group so the whole tree can be signalled later),
        and polls the readiness URL every ``poll_interval_s`` until it answers
        HTTP 200 or ``boot_timeout_s`` elapses.

        Returns:
            An :class:`~fileak.models.AppHandle` (pid, ``base_url``, log path)
            once the readiness URL answers 200.

        Raises:
            BootError: If the configured port is already bound (fail-fast,
                before spawning), if the process exits during polling (aborts
                immediately), if readiness is not reached within
                ``boot_timeout_s`` (after terminating the process), or if the
                process could not be spawned.
        """
        # Port pre-check (Requirement 11.5 / 5.5, design "Scenario 5"): fail
        # fast *before* opening the log or spawning anything if the port is
        # already bound, with an actionable message suggesting a different port.
        self._check_port_free()

        log_path = self._new_log_path()
        try:
            log_fh = open(log_path, "w", encoding="utf-8")
        except OSError as exc:  # pragma: no cover - extremely unlikely
            raise BootError(f"could not open app log file {log_path}: {exc}") from exc

        try:
            proc = self._spawn(self.start_cmd, self.target_dir, log_fh)
        except OSError as exc:
            # Spawn failed (e.g. command not found): close the log handle so it
            # is not leaked, then surface a boot failure.
            self._log_fh = log_fh
            self._log_path = log_path
            self._close_log()
            raise BootError(
                f"failed to spawn app with {self.start_cmd!r} in "
                f"{self.target_dir}: {exc}"
            ) from exc

        # Record runtime state so stop()/captured_stderr() can find them.
        self._proc = proc
        self._log_fh = log_fh
        self._log_path = log_path

        deadline = self._monotonic() + self.boot_timeout_s
        while self._monotonic() < deadline:
            # Loop invariant: the process must be alive on every poll. If it has
            # exited, abort immediately (design: ASSERT proc.is_alive()).
            if self._poll(proc) is not None:
                self.stop()
                raise BootError(
                    f"app process (pid before exit) terminated during readiness "
                    f"polling before {self.readiness_url} answered 200"
                )

            if self._http_probe(self.readiness_url) == 200:
                return AppHandle(
                    pid=int(getattr(proc, "pid", -1)),
                    base_url=self.base_url,
                    log_path=log_path,
                )

            self._sleep(self.poll_interval_s)

        # Timed out: terminate the (still-running) process and report failure.
        self.stop()
        raise BootError(
            f"app not ready within {self.boot_timeout_s}s "
            f"(readiness URL {self.readiness_url} never answered 200)"
        )

    # -- port pre-check (task 7.2) -----------------------------------------

    def _check_port_free(self) -> None:
        """Fail fast if the configured port is already bound before spawning.

        Runs at the very start of :meth:`start` (before opening the log or
        spawning the target) so a port collision is reported clearly without
        leaving anything running. Probes localhost via the (injectable)
        ``port_probe`` seam; if it reports the port is in use, raises a
        :class:`~fileak.models.BootError` naming the port and pointing the
        operator at ``--port`` (design "Error Handling — Scenario 5",
        Requirement 11.5 / 5.5).
        """
        try:
            in_use = bool(self._port_probe("127.0.0.1", self.port))
        except Exception:
            # A misbehaving probe must not mask the real boot path; treat an
            # unexpected probe error as "could not confirm bound" and proceed.
            in_use = False
        if in_use:
            raise BootError(
                f"port {self.port} is already in use on localhost; the target "
                f"app cannot be started on {self.base_url}. Free the port or "
                f"choose another with --port."
            )

    @staticmethod
    def _default_port_probe(host: str, port: int) -> bool:
        """Return ``True`` when something is already listening on ``(host, port)``.

        Attempts a short-timeout TCP connect to ``(host, port)``. A successful
        connect means a listener is present (port in use → ``True``); a refused
        connection / timeout means the port is free (``False``). Resolves the
        host so both IPv4 and IPv6 loopback are checked, and never raises — an
        unexpected error degrades to "free" so it cannot false-positive and
        block a legitimate boot.
        """
        try:
            addr_infos = socket.getaddrinfo(
                host, port, proto=socket.IPPROTO_TCP
            )
        except OSError:
            return False
        for family, socktype, proto, _canonname, sockaddr in addr_infos:
            sock = None
            try:
                sock = socket.socket(family, socktype, proto)
                sock.settimeout(_PORT_PROBE_TIMEOUT_S)
                if sock.connect_ex(sockaddr) == 0:
                    return True  # connection succeeded → something is listening
            except OSError:
                # Probe error on this address: treat as not-bound and try others.
                continue
            finally:
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
        return False

    @staticmethod
    def _poll(proc: object) -> Optional[int]:
        """Return the process exit code, or ``None`` if it is still running.

        Defensive wrapper around ``proc.poll()`` so a fake/dummy process without
        ``poll`` is treated as still alive rather than raising.
        """
        poll = getattr(proc, "poll", None)
        if poll is None:
            return None
        return poll()

    def _default_spawn(self, cmd: list[str], cwd: Path, log_fh: object):
        """Spawn the app as a subprocess in its own session/process group.

        ``start_new_session=True`` makes the child a session+group leader so the
        entire process tree shares its pgid and can be torn down with a single
        :func:`os.killpg` in :meth:`stop`. stdout and stderr are merged into the
        capture log file.
        """
        return subprocess.Popen(
            cmd,
            cwd=str(cwd),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    def _default_http_probe(self, url: str) -> Optional[int]:
        """Return the HTTP status for ``url`` or ``None`` when not yet reachable.

        A 2xx/3xx response yields its status code; a non-2xx HTTP response yields
        its code via :class:`urllib.error.HTTPError`; a connection error (server
        not up yet) yields ``None`` so polling simply continues.
        """
        try:
            with urllib.request.urlopen(
                url, timeout=_READINESS_PROBE_TIMEOUT_S
            ) as resp:
                # ``status`` (3.9+) / ``getcode()`` both return the HTTP code.
                return getattr(resp, "status", None) or resp.getcode()
        except urllib.error.HTTPError as exc:
            # The server answered, just not 2xx (e.g. 404/500). Return the code
            # so the caller can decide; only 200 counts as ready.
            return exc.code
        except (urllib.error.URLError, OSError):
            # Connection refused / DNS / reset: app not accepting connections yet.
            return None

    # -- captured output ----------------------------------------------------

    def captured_stderr(self) -> str:
        """Return the captured process output (stdout+stderr), best-effort.

        Flushes the live log handle (if any) and reads the capture log back from
        disk. Never raises: a missing/unreadable log degrades to ``""``. The log
        persists after :meth:`stop`, so callers (the reporter) can read it even
        after a boot failure.
        """
        if self._log_fh is not None:
            try:
                self._log_fh.flush()
            except (ValueError, OSError):
                pass
        if self._log_path is None:
            return ""
        try:
            return Path(self._log_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    # -- stop / teardown (task 7.2) ----------------------------------------

    def stop(self) -> None:
        """Terminate the spawned process tree and flush/close the capture log.

        Full process-*tree* teardown (Requirements 5.6, 5.7): signals the whole
        process group (SIGTERM, then SIGKILL after a grace period) via the
        (injectable) terminate seam and reaps the child, so that on return no
        child spawned by :meth:`start` is left alive and the port is released.
        Because :meth:`_default_spawn` starts the app in its own session, the
        child's pgid equals its pid and a single :func:`os.killpg` reaches every
        descendant (no orphaned dev servers).

        Idempotent and never-raising: it is invoked from ``finally`` / timeout /
        signal paths and from :meth:`start`'s failure paths, and is safe to call
        repeatedly or when nothing was ever started. The capture log path is
        retained so :meth:`captured_stderr` can still read it after ``stop``.
        """
        proc = self._proc
        if proc is not None:
            try:
                self._terminate(proc)
            except Exception:
                # Teardown must never raise (called from finally/timeout paths).
                pass
            finally:
                self._proc = None
        # Close the write handle but keep ``_log_path`` so captured_stderr()
        # can still read the log after stop().
        self._close_log()

    def _default_terminate(self, proc: object) -> None:
        """Signal the process group SIGTERM, then SIGKILL after a grace period.

        POSIX process-group teardown: because :meth:`_default_spawn` starts a new
        session, the child's pgid equals its pid and a single :func:`os.killpg`
        reaches the whole tree (no orphaned dev servers). Best-effort and
        idempotent — a process that is already gone is not an error.
        """
        if self._poll(proc) is not None:
            return  # already exited
        pid = getattr(proc, "pid", None)
        if pid is None:
            return

        self._signal_group(proc, pid, signal.SIGTERM)

        # Wait for a graceful exit, then escalate to SIGKILL if still alive.
        deadline = self._monotonic() + self._termination_grace_s
        while self._monotonic() < deadline:
            if self._poll(proc) is not None:
                break
            self._sleep(_TERMINATION_POLL_S)
        else:
            self._signal_group(proc, pid, signal.SIGKILL)

        # Reap the child so it does not linger as a zombie.
        wait = getattr(proc, "wait", None)
        if wait is not None:
            try:
                wait(timeout=self._termination_grace_s)
            except Exception:
                pass

    @staticmethod
    def _signal_group(proc: object, pid: int, sig: int) -> None:
        """Send ``sig`` to ``pid``'s process group, falling back to the process.

        Uses :func:`os.killpg` (POSIX) to reach the whole tree; if that is
        unavailable or fails (already-dead, permissions, non-POSIX), it falls
        back to signalling the process directly. Never raises.
        """
        killpg = getattr(os, "killpg", None)
        getpgid = getattr(os, "getpgid", None)
        if killpg is not None and getpgid is not None:
            try:
                killpg(getpgid(pid), sig)
                return
            except (ProcessLookupError, PermissionError, OSError):
                pass
        # Fallback: signal just the process (non-POSIX or killpg failure).
        try:
            send_signal = getattr(proc, "send_signal", None)
            if send_signal is not None:
                send_signal(sig)
        except Exception:
            pass

    # -- log file management ------------------------------------------------

    def _new_log_path(self) -> Path:
        """Allocate a fresh capture-log path under the (lazy) logs directory.

        The logs directory defaults to a private temp dir created on first use
        so captured logs never pollute the target repo (restore-safety).
        """
        if self._logs_dir is None:
            self._logs_dir = Path(tempfile.mkdtemp(prefix="fileak_applog_"))
        else:
            self._logs_dir.mkdir(parents=True, exist_ok=True)
        self._start_count += 1
        return self._logs_dir / f"app-{self._start_count}.log"

    def _close_log(self) -> None:
        """Close the live capture-log write handle if open (keeps the path)."""
        if self._log_fh is not None:
            try:
                self._log_fh.flush()
            except (ValueError, OSError):
                pass
            try:
                self._log_fh.close()
            except (ValueError, OSError):
                pass
            self._log_fh = None
