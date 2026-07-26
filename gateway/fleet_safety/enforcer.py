"""Kill-and-report orchestration for a tripped session.

The enforcer turns a :class:`~gateway.fleet_safety.deadloop_guard.Trip` into
three side effects, in a fixed order, through an injected :class:`KillActions`
seam:

  1. **interrupt** the running agent loop — the hard stop (not a warning).
  2. **release_lease** so the session's turn lease is freed and the slot is
     reusable.
  3. **notify** Telegram + desktop with the kill report — never silent.

Every step is best-effort and isolated: a failure in one is captured in the
:class:`EnforcementResult` and does not prevent the others. The enforcer never
raises into the housekeeping loop. It is deterministic — no clock, no I/O of
its own; all effects come from the injected callables — which is what makes it
unit-testable with a fake ``KillActions``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Protocol

from gateway.fleet_safety.deadloop_guard import Trip
from gateway.fleet_safety.report import format_kill_report


class KillActions(Protocol):
    """The three effects the enforcer needs. The live wiring adapts the real
    gateway registries to this; tests pass a fake."""

    def interrupt(self, session_id: str, reason: str) -> bool:
        """Hard-stop the agent loop for ``session_id``. Return True if a live
        agent was found and interrupted."""
        ...

    def release_lease(self, session_id: str) -> bool:
        """Release the session's turn lease. Return True if one was held."""
        ...

    def notify(self, text: str) -> bool:
        """Deliver ``text`` to Telegram + desktop. Return True if delivered."""
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
        """Whether the running loop accepted the interrupt signal.

        Releasing a turn lease only makes the slot reusable; it does not stop
        generation and therefore must never manufacture a successful-kill
        claim.
        """
        return self.interrupted


class GuardEnforcer:
    def __init__(self, actions: KillActions) -> None:
        self._actions = actions

    def enforce(self, trip: Trip) -> EnforcementResult:
        result = EnforcementResult(
            session_id=trip.session_id,
            reason=trip.reason.value,
            report=format_kill_report(trip),
        )
        kill_reason = f"dead-loop guard: {trip.reason.value} — {trip.detail}"

        try:
            result.interrupted = bool(self._actions.interrupt(trip.session_id, kill_reason))
        except Exception as e:  # never let a kill failure escape into housekeeping
            result.errors.append(f"interrupt failed: {e}")

        try:
            result.lease_released = bool(self._actions.release_lease(trip.session_id))
        except Exception as e:
            result.errors.append(f"release_lease failed: {e}")

        # Report even if the interrupt/lease steps failed — a kill that could
        # not fully land is exactly what an operator most needs to hear about.
        try:
            result.notified = bool(self._actions.notify(result.report))
        except Exception as e:
            result.errors.append(f"notify failed: {e}")

        return result
