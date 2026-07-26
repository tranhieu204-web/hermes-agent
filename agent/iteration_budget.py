"""Thread-safe renewable iteration budget for the outer provider loop.

The initial allowance remains the configured ``max_iterations``.  At the real
outer-loop exhaustion checkpoint, one additional provider slot may be granted
for a new terminal-event sequence or verified-progress sequence.  Renewals are
one slot at a time and capped to one additional initial-size block, so replayed
polls cannot renew and total provider-loop work remains bounded.

Checkpoint receipts are policy decisions only.  They intentionally claim no
session stop, lock release, or other lifecycle side effect; callers own those
routes.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Optional


class IterationCheckpointOutcome(str, Enum):
    """Atomic outcome returned by :meth:`IterationBudget.checkpoint`."""

    EXTENDED = "extended"
    DENIED_VERIFIED_NO_PROGRESS = "denied_verified_no_progress"
    DENIED_NO_NEW_EVIDENCE = "denied_no_new_evidence"
    DENIED_EXTENSION_LIMIT = "denied_extension_limit"
    DENIED_SESSION_MISMATCH = "denied_session_mismatch"
    RACE_LOST = "race_lost"
    STALE_GENERATION = "stale_generation"
    NOT_EXHAUSTED = "not_exhausted"


@dataclass(frozen=True)
class IterationCheckpointReceipt:
    """Structured, immutable evidence receipt for one checkpoint decision."""

    outcome: IterationCheckpointOutcome
    granted: bool
    evidence: str
    owner_id: Optional[str]
    generation: int
    session_id: Optional[str]
    evidence_session_id: Optional[str]
    event_sequence: int
    progress_sequence: int
    no_progress_streak: int
    no_progress_limit: int
    extension_calls: int
    extension_limit: int
    used: int
    max_total: int
    hard_max_total: int
    claimed_side_effects: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["outcome"] = self.outcome.value
        value["claimed_side_effects"] = list(self.claimed_side_effects)
        return value


class IterationBudget:
    """Atomic provider-call allowance with bounded evidence-based renewal.

    ``consume`` remains bool-returning for backward compatibility.  The outer
    loop supplies an owner and generation so a concurrent claimant that loses
    the final-slot race cannot spend a renewal earned by the winner.
    """

    def __init__(
        self,
        max_total: int,
        *,
        extension_limit: Optional[int] = None,
    ) -> None:
        initial = max(0, int(max_total))
        limit = initial if extension_limit is None else max(0, int(extension_limit))
        self._initial_max_total = initial
        self._max_total = initial
        self._hard_max_total = initial + limit
        self._extension_limit = limit
        self._extension_calls = 0
        self._used = 0
        self._generation = 0
        self._generation_owner: Optional[str] = None
        self._final_slot_owner: Optional[str] = None
        self._last_granted_event_sequence = 0
        self._last_granted_progress_sequence = 0
        self._lock = threading.Lock()
        self._receipts: deque[IterationCheckpointReceipt] = deque(maxlen=128)

    def consume(
        self,
        *,
        owner_id: Optional[str] = None,
        expected_generation: Optional[int] = None,
    ) -> bool:
        """Atomically claim one available slot.

        When an extension generation has an owner, only that owner may consume
        its granted slot.  Legacy callers that omit owner/generation retain the
        original bool-returning behavior before any owned extension exists.
        """

        with self._lock:
            if (
                expected_generation is not None
                and expected_generation != self._generation
            ):
                return False
            if (
                self._generation_owner is not None
                and owner_id != self._generation_owner
            ):
                return False
            if self._used >= self._max_total:
                return False
            self._used += 1
            if self._used == self._max_total:
                self._final_slot_owner = owner_id
            return True

    def checkpoint(
        self,
        *,
        owner_id: Optional[str],
        expected_generation: int,
        session_id: Optional[str],
        evidence_session_id: Optional[str],
        event_sequence: Optional[int],
        progress_sequence: Optional[int],
        no_progress_streak: int,
        no_progress_limit: int,
    ) -> IterationCheckpointReceipt:
        """Evaluate and, when justified, grant one renewable provider slot.

        Ordering is deliberate: generation/final-slot ownership is resolved
        under the same lock as exhaustion, then verified no-progress wins over
        extension.  Only fresh terminal/progress sequences can renew.
        """

        with self._lock:
            event_seq = max(0, int(event_sequence or 0))
            progress_seq = max(0, int(progress_sequence or 0))
            streak = max(0, int(no_progress_streak or 0))
            limit = max(1, int(no_progress_limit or 1))

            def _receipt(
                outcome: IterationCheckpointOutcome,
                *,
                granted: bool = False,
                evidence: str,
            ) -> IterationCheckpointReceipt:
                receipt = IterationCheckpointReceipt(
                    outcome=outcome,
                    granted=granted,
                    evidence=evidence,
                    owner_id=owner_id,
                    generation=self._generation,
                    session_id=session_id,
                    evidence_session_id=evidence_session_id,
                    event_sequence=event_seq,
                    progress_sequence=progress_seq,
                    no_progress_streak=streak,
                    no_progress_limit=limit,
                    extension_calls=self._extension_calls,
                    extension_limit=self._extension_limit,
                    used=self._used,
                    max_total=self._max_total,
                    hard_max_total=self._hard_max_total,
                )
                self._receipts.append(receipt)
                return receipt

            if expected_generation != self._generation:
                return _receipt(
                    IterationCheckpointOutcome.STALE_GENERATION,
                    evidence="generation_changed",
                )

            if (
                self._generation_owner is not None
                and owner_id != self._generation_owner
            ):
                return _receipt(
                    IterationCheckpointOutcome.RACE_LOST,
                    evidence="extension_generation_owned_by_other_runner",
                )

            if self._used < self._max_total:
                return _receipt(
                    IterationCheckpointOutcome.NOT_EXHAUSTED,
                    evidence="slot_already_available",
                )

            if (
                self._final_slot_owner is not None
                and owner_id != self._final_slot_owner
            ):
                return _receipt(
                    IterationCheckpointOutcome.RACE_LOST,
                    evidence="final_slot_owned_by_other_runner",
                )

            if (
                evidence_session_id
                and session_id
                and evidence_session_id != session_id
            ):
                return _receipt(
                    IterationCheckpointOutcome.DENIED_SESSION_MISMATCH,
                    evidence="session_mismatch",
                )

            # A verified dead loop always wins over both unknown and verified
            # progress counters.  The checkpoint denies continuation; it does
            # not claim that any stop/release side effect occurred.
            if streak >= limit:
                return _receipt(
                    IterationCheckpointOutcome.DENIED_VERIFIED_NO_PROGRESS,
                    evidence="verified_no_progress",
                )

            if self._extension_calls >= self._extension_limit:
                return _receipt(
                    IterationCheckpointOutcome.DENIED_EXTENSION_LIMIT,
                    evidence="hard_extension_ceiling",
                )

            evidence = ""
            if progress_seq > self._last_granted_progress_sequence:
                evidence = "verified_progress"
            elif event_seq > self._last_granted_event_sequence:
                evidence = "unknown_terminal_event"
            if not evidence:
                return _receipt(
                    IterationCheckpointOutcome.DENIED_NO_NEW_EVIDENCE,
                    evidence="no_new_terminal_event",
                )

            self._extension_calls += 1
            self._max_total += 1
            self._generation += 1
            self._generation_owner = owner_id
            self._final_slot_owner = None
            self._last_granted_event_sequence = event_seq
            self._last_granted_progress_sequence = progress_seq
            return _receipt(
                IterationCheckpointOutcome.EXTENDED,
                granted=True,
                evidence=evidence,
            )

    def refund(self) -> bool:
        """Return one slot after a provider call that should not count."""

        with self._lock:
            if self._used <= 0:
                return False
            was_exhausted = self._used >= self._max_total
            self._used -= 1
            if was_exhausted:
                self._final_slot_owner = None
            return True

    @property
    def remaining(self) -> int:
        with self._lock:
            return max(0, self._max_total - self._used)

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    @property
    def max_total(self) -> int:
        with self._lock:
            return self._max_total

    @property
    def initial_max_total(self) -> int:
        return self._initial_max_total

    @property
    def hard_max_total(self) -> int:
        return self._hard_max_total

    @property
    def extension_limit(self) -> int:
        return self._extension_limit

    @property
    def extension_calls(self) -> int:
        with self._lock:
            return self._extension_calls

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def receipts(self) -> tuple[IterationCheckpointReceipt, ...]:
        with self._lock:
            return tuple(self._receipts)

    @property
    def last_receipt(self) -> Optional[IterationCheckpointReceipt]:
        with self._lock:
            return self._receipts[-1] if self._receipts else None
