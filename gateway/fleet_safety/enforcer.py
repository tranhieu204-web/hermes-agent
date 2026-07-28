"""Notify-or-stop orchestration for a fleet-safety evaluation.

The enforcer turns a :class:`~gateway.fleet_safety.deadloop_guard.Trip` into
three side effects, in a fixed order, through an injected :class:`KillActions`
seam:

  1. **interrupt** the running agent loop and confirm its worker drained.
  2. **release_lease** only after execution is confirmed stopped.
  3. **notify** the originating route with the enforcement report.

Every step is best-effort and isolated: a failure in one is captured in the
:class:`EnforcementResult` and does not prevent the others. The enforcer never
raises into the housekeeping loop. It is deterministic — no clock, no I/O of
its own; all effects come from the injected callables — which is what makes it
unit-testable with a fake ``KillActions``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Protocol

from gateway.fleet_safety.deadloop_guard import GuardOutcome, Trip
from gateway.fleet_safety.report import format_continuation_report, format_kill_report


class KillActions(Protocol):
    def interrupt(self, session_id: str, reason: str) -> bool:
        """Request a stop and return True only after execution drained."""
        ...

    # Retained for adapter compatibility only. GuardEnforcer deliberately never
    # calls this: the gateway's generation-owned finally path releases leases.
    def release_lease(self, session_id: str) -> bool:
        ...

    def notify(self, text: str) -> bool:
        """Deliver ``text`` to the origin. Return True if delivered."""
        ...


@dataclass
class EnforcementResult:
    session_id: str
    reason: str
    stop_requested: bool = False
    interrupted: bool = False
    interrupt_pending: bool = False
    lease_released: bool = False
    notified: bool = False
    report: str = ""
    errors: List[str] = field(default_factory=list)

    @property
    def killed(self) -> bool:
        """Interrupt acceptance is not confirmation that a worker terminated."""
        return False

    @property
    def terminated(self) -> bool:
        return False


class GuardEnforcer:
    def __init__(self, actions: KillActions) -> None:
        self._actions = actions

    def enforce(self, trip: Trip) -> EnforcementResult:
        result = EnforcementResult(
            session_id=trip.session_id,
            reason=trip.reason.value,
        )

        if trip.outcome is GuardOutcome.CONTINUATION_NOTICE:
            result.report = format_continuation_report(trip)
            try:
                result.notified = bool(self._actions.notify(result.report))
            except Exception as e:
                result.errors.append(f"notify failed: {e}")
            return result

        stop_reason = f"fleet-safety stop: {trip.reason.value} — {trip.detail}"

        if not is_hard_stop or outcome == GuardOutcome.CONTINUATION_NOTICE:
            report = format_continuation_report(trip)
            result = EnforcementResult(
                session_id=session_id,
                reason=reason_str,
                report=report,
            )
            try:
                result.notified = bool(self._actions.notify(report))
            except Exception as exc:
                result.errors.append(f"notify failed: {exc}")
            return result

        result = EnforcementResult(session_id=session_id, reason=reason_str)
        request_reason = (
            f"fleet-safety stop request: {reason_str} — "
            f"{str(getattr(trip, 'detail', '') or '')}"
        )
        try:
            result.stop_requested = bool(
                self._actions.interrupt(trip.session_id, stop_reason)
            )
            # Backward-compatible field name: this means the cooperative
            # interrupt request was accepted, not that execution terminated.
            result.interrupted = result.stop_requested
            result.interrupt_pending = bool(
                getattr(self._actions, "interrupt_pending", False)
            )
        except Exception as e:  # never let a kill failure escape into housekeeping
            result.errors.append(f"interrupt failed: {e}")

        # The enforcer never releases a lease directly. The gateway worker's
        # generation-safe unwind owns that lifecycle transition.
        result.report = format_kill_report(
            trip,
            interrupt_request_accepted=result.stop_requested,
        )

        # Report even if the interrupt/lease steps failed — a kill that could
        # not fully land is exactly what an operator most needs to hear about.
        try:
            result.notified = bool(self._actions.notify(result.report))
        except Exception as exc:
            result.errors.append(f"notify failed: {exc}")
        return result


__all__ = [
    "EnforcementResult",
    "GuardEnforcer",
    "KillActions",
    "is_stop_command",
]
