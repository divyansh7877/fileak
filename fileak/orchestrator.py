"""Orchestrator — the autonomous chaos control loop.

This is Component 1 from the design: the deterministic outer controller that
owns the experiment matrix (chaos profiles × the assertions assigned to each),
sequences every other component, and *guarantees cleanup*. It contains no AI of
its own — all semantic judgement is delegated to Kane CLI via the
:class:`~fileak.kane.KaneRunner`; the orchestrator just drives the lifecycle and
aggregates the results.

Lifecycle per profile (strictly sequential, design "Main Orchestration Loop"
and the one-profile sequence diagram)::

    inject → install → start → (for each applicable assertion:
        KaneRunner.run → RatchetReporter.evaluate, append Finding)
        → stop → revert

Cleanup is layered with nested ``finally`` blocks so the safety invariants hold
no matter where a failure occurs (design "Profile isolation with mandatory
restore", Requirement 10):

* ``runner.stop()`` ALWAYS runs after a successful ``start()`` (inner
  ``finally``), so no app process is left running for a profile.
* ``mutator.revert(record)`` ALWAYS runs per profile (per-profile ``finally``),
  so at most one mutation is ever active (Requirement 1.3) and one profile can
  never corrupt the next.
* ``guard.restore()`` ALWAYS runs in an OUTER ``finally`` (Requirement 10.2),
  returning the target to baseline byte-for-byte even on an unexpected
  exception.

A :class:`~fileak.models.BootError` raised by ``install()`` or ``start()`` is
caught per profile: the orchestrator records exactly ONE ``INCONCLUSIVE``
finding for that profile (design "Error Handling — Scenario 1", Requirement 1.4)
and the loop continues to the next profile (after the per-profile ``revert``).

This module implements **task 10.1** (the loop + restore wiring). The SIGINT/
SIGTERM handler install is **task 10.2**; here it is a documented hook
(:meth:`Orchestrator._install_signal_handlers`) called at exactly the point the
design pseudocode installs it, plus the per-profile state
(``_active_record`` / ``_active_runner``) that handler will need, so 10.2 slots
in without restructuring the loop.

Stdlib only (no third-party deps).
"""

from __future__ import annotations

import os
import signal
from types import FrameType
from typing import Callable, Optional, Union

from .app_runner import AppRunner
from .baseline import BaselineGuard
from .chaos import ChaosMutator
from .kane import KaneRunner
from .models import (
    BootError,
    EngineConfig,
    Finding,
    MutationRecord,
    RunReport,
    StepStatus,
    Verdict,
)
from .reporter import RatchetReporter

#: assertion_id stamped onto the synthetic INCONCLUSIVE finding recorded when a
#: profile fails to boot (install/start raised BootError). It is not a real
#: assertion id from the bank; the leading underscore keeps it from colliding
#: with operator-authored assertion ids.
BOOT_FAILURE_ASSERTION_ID = "_boot_failure"

#: Cap (chars) on the boot-failure reason embedded as evidence in the synthetic
#: INCONCLUSIVE finding, so a noisy BootError message (which may carry an
#: install-log tail) cannot bloat the report.
_BOOT_REASON_MAX_CHARS = 500


def _truncate(text: str, max_chars: int) -> str:
    """Return at most ``max_chars`` characters of ``text`` (ellipsis-suffixed)."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "…"


def inconclusive(profile_name: str, reason: str) -> Finding:
    """Build the synthetic ``INCONCLUSIVE`` finding for a profile boot failure.

    Used when ``install()`` or ``start()`` raises
    :class:`~fileak.models.BootError`: the profile produced no Kane judgement, so
    rather than a false ``SAFE`` the orchestrator records one ``INCONCLUSIVE``
    finding for the profile (design "Scenario 1", Requirement 1.4 / 9.2). The
    boot-failure ``reason`` is attached as a single truncated evidence snippet
    so the report explains why the profile could not be judged.
    """
    return Finding(
        profile_name=profile_name,
        assertion_id=BOOT_FAILURE_ASSERTION_ID,
        verdict=Verdict.INCONCLUSIVE,
        kane_status=StepStatus.ERROR,
        leak_indicators=[],
        evidence=[_truncate(reason, _BOOT_REASON_MAX_CHARS)] if reason else [],
        output_dir=None,
    )


class Orchestrator:
    """The autonomous control loop wiring all components together.

    Owns the experiment matrix (profiles × assertions), sequences
    inject/install/boot/assert/stop/revert per profile, guarantees cleanup via
    layered ``finally`` blocks, and aggregates every :class:`Finding` into a
    :class:`~fileak.models.RunReport`.

    Args:
        config: The validated :class:`~fileak.models.EngineConfig`. Its
            :meth:`~fileak.models.EngineConfig.profile_names` /
            :meth:`~fileak.models.EngineConfig.assertions_for` drive the loop.
        mutator: Injects/reverts a single chaos profile (one mutation at a time).
        runner: Installs, boots, and tears down the target app.
        kane: Adapter that drives Kane CLI for one assertion.
        reporter: Maps Kane results into findings and finalizes the report.
        guard: Snapshots tracked files and restores baseline unconditionally.
    """

    def __init__(
        self,
        config: EngineConfig,
        mutator: ChaosMutator,
        runner: AppRunner,
        kane: KaneRunner,
        reporter: RatchetReporter,
        guard: BaselineGuard,
    ) -> None:
        self.config = config
        self.mutator = mutator
        self.runner = runner
        self.kane = kane
        self.reporter = reporter
        self.guard = guard

        # Per-run/-profile state the SIGINT/SIGTERM handler (task 10.2) needs to
        # stop the app and revert the active mutation before exit. Populated as
        # the loop progresses and cleared on teardown so they always reflect the
        # currently-active mutation/runner (at most one mutation active at a
        # time, Requirement 1.3).
        self._active_record: Optional[MutationRecord] = None
        self._active_runner: Optional[AppRunner] = None

        # Previous SIGINT/SIGTERM handlers, saved by _install_signal_handlers so
        # the original disposition could be restored if desired. Maps the signal
        # number to whatever signal.signal() returned at install time (a
        # callable, signal.SIG_DFL, signal.SIG_IGN, or None).
        self._previous_handlers: dict[int, Union[Callable, int, None]] = {}

    def run(self) -> RunReport:
        """Execute the full chaos loop and return an aggregate report.

        Snapshots the baseline, then iterates the configured profiles
        sequentially, running the inject → install → start → assert → stop →
        revert lifecycle for each and appending one :class:`Finding` per
        attempted (profile, assertion) pair (plus one ``INCONCLUSIVE`` per boot
        failure). The target is restored to baseline before returning, even on
        error.

        Returns:
            The aggregate :class:`~fileak.models.RunReport` from
            ``reporter.finalize(findings)``.

        Loop invariants (design):
            * At the top of each profile iteration the target is at baseline
              (no active mutation).
            * At most one mutation is active at any instant (Requirement 1.3).
            * ``findings`` holds exactly one entry per attempted
              (profile, assertion) pair, plus one ``INCONCLUSIVE`` per boot
              failure (Requirement 1.2 / 1.4 / 9.2).
        """
        findings: list[Finding] = []

        # Snapshot BEFORE any mutation so restore() can always return to
        # baseline (Requirement 10.1). The OUTER finally below guarantees a
        # restore on every exit path (success, BootError, unexpected exception).
        self.guard.snapshot()
        # task 10.2 wires SIGINT/SIGTERM here; today this is a documented no-op
        # hook so the install point matches the design pseudocode exactly.
        self._install_signal_handlers()
        try:
            for profile_name in self.config.profile_names():
                record = self.mutator.inject(profile_name)
                # Track the active mutation/runner for the signal handler (10.2)
                # and to make "at most one mutation active" explicit.
                self._active_record = record
                self._active_runner = self.runner
                try:
                    # install() / start() may raise BootError; both are handled
                    # below as a single INCONCLUSIVE finding for this profile.
                    self.runner.install()
                    handle = self.runner.start()  # blocks until ready
                    try:
                        for assertion in self.config.assertions_for(profile_name):
                            kane = self.kane.run(assertion, handle.base_url)
                            finding = self.reporter.evaluate(
                                profile_name,
                                assertion,
                                kane,
                                self.runner.captured_stderr(),
                            )
                            findings.append(finding)
                    finally:
                        # Always stop the app once started, before reverting.
                        self.runner.stop()
                except BootError as exc:
                    # Install/boot failure: record ONE INCONCLUSIVE finding for
                    # this profile and continue to the next (design Scenario 1).
                    findings.append(inconclusive(profile_name, reason=str(exc)))
                finally:
                    # Always revert this profile's mutation so at most one
                    # mutation is ever active and the next profile starts clean.
                    self.mutator.revert(record)
                    self._active_record = None
                    self._active_runner = None
        finally:
            # Outer safety net: restore the target to baseline no matter what
            # (Requirement 10.2). restore() is idempotent, so a later signal-
            # handler restore (task 10.2) is harmless.
            self.guard.restore()

        # Aggregate every finding into the RunReport (writes report; sets
        # leaks_found so exit_code == 1 iff any LEAK_DETECTED).
        return self.reporter.finalize(findings)

    def _install_signal_handlers(self) -> None:
        """Install SIGINT/SIGTERM handlers for restore-on-interrupt.

        Registers a handler for :data:`signal.SIGINT` and :data:`signal.SIGTERM`
        (task 10.2, Requirement 10.5) that, when an interrupt arrives mid-run,
        performs a best-effort cleanup — stop the active app
        (:attr:`_active_runner`) and invoke :meth:`guard.restore` — *before* the
        process exits, so the operator's repo is returned to baseline rather
        than left with a deliberately broken dependency wired in (design "Error
        Handling — Scenario 3").

        Because :meth:`BaselineGuard.restore` is idempotent and
        :meth:`AppRunner.stop` is idempotent/never-raising, a handler-driven
        cleanup makes the outer ``finally`` restore in :meth:`run` harmless: the
        cleanup simply runs at least once and the second call is a no-op.

        Process-exit strategy: after cleanup the handler **re-raises the default
        behaviour** by restoring the previously-installed handler and re-sending
        the same signal to this process. This guarantees the process actually
        terminates with conventional semantics (e.g. SIGINT → ``KeyboardInterrupt``
        / 130, SIGTERM → 143) *and* lets :meth:`run`'s ``finally`` blocks run on
        the way out (stop / revert / restore) — all of which are idempotent, so
        the double cleanup is safe. We deliberately avoid ``os._exit`` because it
        would skip those ``finally`` blocks.

        Main-thread constraint: Python only allows :func:`signal.signal` to be
        called from the main thread; from a worker thread it raises
        ``ValueError``. The orchestrator may legitimately run on a worker thread
        (e.g. inside a test harness), so installation is guarded: if it cannot
        register handlers we skip silently. This is safe because the outer
        ``finally`` restore in :meth:`run` still protects the repo on normal
        and exceptional exits — only the interrupt-time fast path is unavailable
        off the main thread, and a worker thread does not receive these signals
        directly anyway.
        """
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                previous = signal.signal(signum, self._handle_interrupt)
            except (ValueError, OSError):
                # ValueError: not on the main thread (signal handlers can only
                # be installed from the main thread). OSError: signal not
                # supported on this platform. Either way, skip installation —
                # the outer finally restore in run() still protects the repo.
                continue
            self._previous_handlers[signum] = previous

    def _handle_interrupt(self, signum: int, frame: Optional[FrameType]) -> None:
        """Signal handler: clean up, then re-raise the signal's default action.

        Best-effort cleanup (stop the active app, restore baseline) followed by
        re-sending ``signum`` under its previous (typically default) handler so
        the process terminates with conventional signal semantics. Both cleanup
        steps are wrapped so a cleanup error cannot prevent exit.
        """
        self._cleanup_on_interrupt()

        # Re-raise the signal's default behaviour: restore the prior handler and
        # re-send the signal to ourselves so the process exits normally and
        # run()'s finally blocks (idempotent stop/revert/restore) still execute.
        previous = self._previous_handlers.get(signum, signal.SIG_DFL)
        try:
            signal.signal(signum, previous)
        except (ValueError, OSError):
            # Couldn't restore the prior handler; fall back to a clean exit so
            # the process still terminates after cleanup.
            raise SystemExit(128 + signum)

        if signum == signal.SIGINT:
            # Surface SIGINT as KeyboardInterrupt so it propagates through run()'s
            # try/finally exactly like a normal Ctrl-C would.
            raise KeyboardInterrupt

        # For SIGTERM (and any other handled signal) re-send it now that the
        # default disposition is restored, so the process exits with the
        # conventional 128+signum status after the finally blocks unwind.
        os.kill(os.getpid(), signum)

    def _cleanup_on_interrupt(self) -> None:
        """Best-effort interrupt cleanup: stop the active app, restore baseline.

        Both steps are individually guarded so a failure in one does not prevent
        the other (and does not prevent the handler from exiting the process).
        ``restore()`` is idempotent, so running it here and again in
        :meth:`run`'s outer ``finally`` is safe.
        """
        runner = self._active_runner
        if runner is not None:
            try:
                runner.stop()
            except Exception:
                # Stop is best-effort; never let a teardown error block restore.
                pass
        try:
            self.guard.restore()
        except Exception:
            # Restore is best-effort here; the outer finally restore in run()
            # gets another (idempotent) chance on the way out.
            pass

