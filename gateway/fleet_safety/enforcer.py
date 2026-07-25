"""Orchestration for guard evaluation results (continuation vs hard stop)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Protocol

from gateway.fleet_safety.deadloop_guard import GuardOutcome, GuardEvaluationResult, Trip
from gateway.fleet_safety.report import format_continuation_report, format_kill_report


def is_stop_command(user_text: str) -> bool:
    """Exact trimmed standalone STOP or /stop matching."""
    if not user_text:
        return False
    s = str(user_text).strip().lower()
    return s in {"stop", "/stop"}


class KillActions(Protocol):
    def interrupt(self, session_id: str, reason: str) -> bool:
        ...

    def release_lease(self, session_id: str) -> bool:
        ...

    def notify(self, text: str) -> bool:
        ...


@dataclass
class EnforcementResult:
    session_id: str
    reason: str
    interrupted: bool = False
    lease_released: bool = False
    notified: bool = False
    report: str = ""
    errors: List[str] = field(default_factory=list)

    @property
    def killed(self) -> bool:
        """True only if the loop was actually stopped (interrupt or lease-release landed)."""
        return self.interrupted or self.lease_released


class GuardEnforcer:
    def __init__(self, actions: KillActions) -> None:
        self._actions = actions

    def enforce(self, trip: GuardEvaluationResult | Trip) -> EnforcementResult:
        session_id = getattr(trip, "session_id", "")
        outcome = getattr(trip, "outcome", None)
        is_hard_stop = getattr(trip, "is_hard_stop", outcome == GuardOutcome.VERIFIED_HARD_STOP)
        reason_str = getattr(getattr(trip, "trip_reason", None), "value", getattr(getattr(trip, "reason", None), "value", "runaway"))

        # Continuation notice path — NEVER interrupts, releases lease, or claims a stop
        if not is_hard_stop or outcome == GuardOutcome.CONTINUATION_NOTICE:
            report_text = format_continuation_report(trip)
            result = EnforcementResult(
                session_id=session_id,
                reason=reason_str,
                interrupted=False,
                lease_released=False,
                report=report_text,
            )
            try:
                result.notified = bool(self._actions.notify(report_text))
            except Exception as e:
                result.errors.append(f"notify failed: {e}")
            return result

        # Hard stop path — only for verified no-progress or non-recoverable error streaks
        report_text = format_kill_report(trip)
        result = EnforcementResult(
            session_id=session_id,
            reason=reason_str,
            report=report_text,
        )
        kill_reason = f"fleet-safety hard stop: {reason_str} — {getattr(trip, 'detail', '')}"

        try:
            result.interrupted = bool(self._actions.interrupt(session_id, kill_reason))
        except Exception as e:
            result.errors.append(f"interrupt failed: {e}")

        try:
            result.lease_released = bool(self._actions.release_lease(session_id))
        except Exception as e:
            result.errors.append(f"release_lease failed: {e}")

        try:
            result.notified = bool(self._actions.notify(result.report))
        except Exception as e:
            result.errors.append(f"notify failed: {e}")

        return result
