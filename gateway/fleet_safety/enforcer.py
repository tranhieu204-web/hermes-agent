"""Orchestration for checkpoint notices and verified safety-stop requests."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Protocol

from gateway.fleet_safety.deadloop_guard import GuardOutcome, GuardEvaluationResult, Trip
from gateway.fleet_safety.report import (
    format_continuation_report,
    format_safety_stop_report,
)


def is_stop_command(user_text: str) -> bool:
    """Exact trimmed standalone STOP or /stop matching."""
    if not user_text:
        return False
    return str(user_text).strip().casefold() in {"stop", "/stop"}


class KillActions(Protocol):
    def interrupt(self, session_id: str, reason: str) -> bool:
        ...

    # Retained for adapter compatibility only. GuardEnforcer deliberately never
    # calls this: the gateway's generation-owned finally path releases leases.
    def release_lease(self, session_id: str) -> bool:
        ...

    def notify(self, text: str) -> bool:
        ...


@dataclass
class EnforcementResult:
    session_id: str
    reason: str
    stop_requested: bool = False
    interrupted: bool = False  # compatibility: cooperative request accepted
    terminated: bool = False
    lease_released: bool = False
    notified: bool = False
    report: str = ""
    errors: List[str] = field(default_factory=list)

    @property
    def killed(self) -> bool:
        """True only with explicit confirmation that execution terminated."""
        return self.terminated


class GuardEnforcer:
    def __init__(self, actions: KillActions) -> None:
        self._actions = actions

    def enforce(self, trip: GuardEvaluationResult | Trip) -> EnforcementResult:
        session_id = str(getattr(trip, "session_id", "") or "")
        outcome = getattr(trip, "outcome", None)
        is_hard_stop = bool(
            getattr(
                trip,
                "is_hard_stop",
                outcome == GuardOutcome.VERIFIED_HARD_STOP,
            )
        )
        reason_obj = getattr(trip, "trip_reason", None) or getattr(trip, "reason", None)
        reason_str = str(getattr(reason_obj, "value", reason_obj) or "runaway")

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
                self._actions.interrupt(session_id, request_reason)
            )
            result.interrupted = result.stop_requested
        except Exception as exc:
            result.errors.append(f"interrupt failed: {exc}")

        # The report is intentionally constructed after the effect attempt so it
        # can state the actual request result. No direct lease release occurs.
        result.report = format_safety_stop_report(
            trip,
            interrupt_request_accepted=result.stop_requested,
        )
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
