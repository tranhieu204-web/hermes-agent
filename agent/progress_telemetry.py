"""Provider-neutral terminal events and progress telemetry.

The terminal event is the narrow contract shared by provider adapters, tool
execution, conversation guardrails, and gateway delivery.  Telemetry mutates
only once for each stable event identity.
"""

from __future__ import annotations

import enum
import hashlib
import json
import re
import threading
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, replace
from functools import wraps
from typing import Any

from agent.usage_provenance import (
    UsageAggregate,
    UsageComponentReceipt,
    UsageProvenance,
    aggregate_usage,
    usage_components_from_mapping,
)


class UsageSourceQuality(str, enum.Enum):
    """Quality indicator for usage counters.

    RESTORED 2026-07-28. This class and :class:`CanonicalUsage` below were
    dropped by the mass-merge ``af469dfea`` ("reconcile all unmerged feature
    branches"): two independent features each authored
    ``agent/progress_telemetry.py``, and the merge took the terminal-event
    version wholesale while keeping the OTHER feature's caller in
    ``agent/conversation_loop.py:1180``. The result did not import, so every
    Hermes conversation died at startup. Verbatim from ``1e40783ba``
    ("fix(fleet-safety): wire measured usage and event-idempotent guard"),
    the commit that introduced BOTH halves.
    """

    MEASURED = "measured"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CanonicalUsage:
    """Normalized provider-neutral usage counters."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    cost: float = 0.0
    model_requests: int = 0
    quality: UsageSourceQuality = UsageSourceQuality.UNKNOWN

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )

    @property
    def is_empty(self) -> bool:
        return not any(
            (
                self.input_tokens,
                self.output_tokens,
                self.cache_read_tokens,
                self.cache_write_tokens,
                self.reasoning_tokens,
                self.cost,
                self.model_requests,
            )
        )


class Retryability(str, enum.Enum):
    """Explicit retryability tri-state supplied by the event producer."""

    RETRYABLE = "retryable"
    NON_RETRYABLE = "non_retryable"
    UNKNOWN = "unknown"


class TerminalStatus(str, enum.Enum):
    """Terminal status of one provider or tool call."""

    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"


class AttemptOutcome(str, enum.Enum):
    """Evidence-based semantic outcome of one distinct terminal event."""

    VERIFIED_PROGRESS = "verified_progress"
    VERIFIED_NO_PROGRESS = "verified_no_progress"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class TerminalEvent:
    """Provider-neutral terminal event envelope."""

    event_id: str
    call_id: str
    adapter: str
    source: str
    status: TerminalStatus
    result: Any
    retryability: Retryability = Retryability.UNKNOWN
    error_code: int | None = None
    failure_signature: str | None = None
    event_sequence: int | None = None
    usage: Mapping[str, Any] | None = None
    session_id: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("event_id", "call_id", "adapter", "source"):
            if not str(getattr(self, field_name) or "").strip():
                raise ValueError(f"terminal event {field_name} must be nonempty")
        object.__setattr__(self, "status", TerminalStatus(self.status))
        object.__setattr__(self, "retryability", Retryability(self.retryability))
        if self.event_sequence is not None:
            object.__setattr__(self, "event_sequence", int(self.event_sequence))


@dataclass(frozen=True)
class RecordedTerminalEvent:
    """Result of recording an event; replays return the original outcome."""

    event_id: str
    outcome: AttemptOutcome
    replayed: bool = False
    result: Any = None
    event_sequence: int | None = None
    snapshot: Mapping[str, Any] | None = None


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_ISO_TIMESTAMP_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"
)
_TIMESTAMP_KEYS = frozenset(
    {
        "clock",
        "current_time",
        "heartbeat",
        "heartbeat_at",
        "now",
        "time",
        "timestamp",
        "ts",
    }
)
_TIMESTAMP_SUFFIXES = ("_at", "_time", "_timestamp")
_STRUCTURED_TIME_RE = re.compile(
    r"(?i)\b(clock|current_time|heartbeat(?:_at)?|now|time|timestamp|ts)\s*[:=]\s*([^\s,;]+)"
)
_RETRYABLE_ERROR_RE = re.compile(
    r"(?i)\b(timeout|timed out|temporar(?:y|ily)|transient|rate limit|too many requests|connection reset|unavailable)\b"
)
_NON_RETRYABLE_ERROR_RE = re.compile(
    r"(?i)\b(auth(?:entication|orization)?|unauthorized|forbidden|invalid credential|permission denied)\b"
)


def classify_retryability(
    *,
    status_code: int | None = None,
    error: Any = None,
    explicit: Retryability | str | None = None,
) -> Retryability:
    """Classify retryability without guessing when producer evidence is absent."""

    if explicit is not None:
        return Retryability(explicit)
    try:
        code = int(status_code) if status_code is not None else None
    except (TypeError, ValueError):
        code = None
    if code in {401, 403}:
        return Retryability.NON_RETRYABLE
    if code == 429 or (code is not None and 500 <= code <= 599):
        return Retryability.RETRYABLE
    text = str(error or "")
    if _NON_RETRYABLE_ERROR_RE.search(text):
        return Retryability.NON_RETRYABLE
    if _RETRYABLE_ERROR_RE.search(text):
        return Retryability.RETRYABLE
    if code is not None and 400 <= code <= 499:
        return Retryability.NON_RETRYABLE
    return Retryability.UNKNOWN


def extract_error_code(result: Any) -> int | None:
    """Extract an HTTP-like status from structured tool/provider output."""

    value = result
    if isinstance(result, str):
        try:
            value = json.loads(result)
        except (TypeError, ValueError):
            return None

    def _walk(candidate: Any) -> int | None:
        if isinstance(candidate, Mapping):
            for key in ("status_code", "status", "http_status", "code"):
                raw = candidate.get(key)
                try:
                    code = int(raw)
                except (TypeError, ValueError):
                    continue
                if 100 <= code <= 599:
                    return code
            for key in ("error", "response", "details"):
                code = _walk(candidate.get(key))
                if code is not None:
                    return code
        return None

    return _walk(value)


def _is_timestamp_key(key: str) -> bool:
    lowered = key.strip().lower()
    return lowered in _TIMESTAMP_KEYS or lowered.endswith(_TIMESTAMP_SUFFIXES)


def _normalize_structured(value: Any, *, key: str = "") -> Any:
    if _is_timestamp_key(key):
        return "<TIMESTAMP>"
    if isinstance(value, Mapping):
        return {
            str(child_key): _normalize_structured(child_value, key=str(child_key))
            for child_key, child_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_normalize_structured(item) for item in value]
    if isinstance(value, str):
        return _ISO_TIMESTAMP_RE.sub("<TIMESTAMP>", _ANSI_RE.sub("", value))
    return value


def normalize_result(result: Any) -> str:
    """Canonicalize result data without erasing numeric business identifiers."""

    if result is None:
        return ""
    value = result
    if isinstance(result, str):
        text = _ANSI_RE.sub("", result).strip()
        try:
            value = json.loads(text)
        except (TypeError, ValueError):
            text = _ISO_TIMESTAMP_RE.sub("<TIMESTAMP>", text)
            return _STRUCTURED_TIME_RE.sub(lambda m: f"{m.group(1)}=<TIMESTAMP>", text)
    normalized = _normalize_structured(value)
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"), default=str)


def canonical_args_hash(args: Mapping[str, Any] | None) -> str:
    normalized = _normalize_structured(args or {})
    payload = json.dumps(normalized, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _synchronized(method):
    """Serialize telemetry mutation/snapshots across tool worker threads."""

    @wraps(method)
    def _wrapped(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return _wrapped


class ProgressTelemetry:
    """Turn-scoped progress telemetry with bounded terminal-event dedupe."""

    def __init__(
        self,
        session_id: str = "",
        context_id: str = "",
        *,
        dedupe_capacity: int = 2048,
        fingerprint_capacity: int = 128,
    ) -> None:
        self.session_id = str(session_id or "").strip()
        self.context_id = str(context_id or self.session_id).strip()
        self._dedupe_capacity = max(1, int(dedupe_capacity))
        self._fingerprint_capacity = max(1, int(fingerprint_capacity))
        self._lock = threading.RLock()
        self._frozen = False
        self._turn_generation = 0
        # Usage is session-scoped, unlike attempt/progress counters and event
        # replay caches which reset for each user turn.
        self._usage_components: OrderedDict[str, UsageComponentReceipt] = OrderedDict()
        self._pending_usage_component_ids: set[str] = set()
        self.reset_for_turn()

    @property
    def frozen(self) -> bool:
        return self._frozen

    def _ensure_mutable(self) -> None:
        if self._frozen:
            raise RuntimeError("progress telemetry ledger is frozen")

    @_synchronized
    def freeze(self) -> None:
        """Seal this session ledger at a committed lineage boundary."""

        self._frozen = True

    @_synchronized
    def bind_session_id(self, session_id: str) -> None:
        """Bind once to the final nonempty session before any event is accepted."""

        self._ensure_mutable()
        bound = str(session_id or "").strip()
        if not bound:
            raise ValueError("progress telemetry session_id must be nonempty")
        if self.session_id and self.session_id != bound:
            raise RuntimeError("progress telemetry is already bound to another session")
        if self._usage_components:
            raise RuntimeError("cannot bind progress telemetry after usage receipts")
        self.session_id = bound
        if not self.context_id:
            self.context_id = bound

    @_synchronized
    def reset_for_turn(
        self, cumulative_usage: "CanonicalUsage | None" = None
    ) -> None:
        """Reset per-turn event evidence and snapshot the usage baseline.

        RESTORED 2026-07-28: the ``cumulative_usage`` parameter was lost in the
        mass-merge ``af469dfea``. ``agent/conversation_loop.py:1180`` calls this
        WITH a usage snapshot, so the merged no-arg signature raised TypeError
        immediately after the ImportError above it. Semantics are taken from
        ``1e40783ba``: fleet-safety telemetry is turn-scoped while provider
        accounting is session-cumulative, so the caller passes the authoritative
        session counters before any call in this turn can advance them, and the
        guard measures this turn as (current - baseline). Defaulted to None so
        any caller that does not track usage keeps working.
        """
        self._ensure_mutable()
        self._usage_baseline = cumulative_usage or CanonicalUsage()
        self._turn_generation += 1
        self.attempt_seq = 0
        self.progress_seq = 0
        self.failure_seq = 0
        self.failure_streak = 0
        self.no_progress_streak = 0
        self.last_event_id: str | None = None
        self.last_call_id: str | None = None
        self.last_adapter: str | None = None
        self.last_source: str | None = None
        self.last_result_hash: str | None = None
        self.last_attempt_key: str | None = None
        self.last_outcome = AttemptOutcome.UNKNOWN
        self.last_retryability = Retryability.UNKNOWN
        self.last_error_code: int | None = None
        self.last_event_sequence: int | None = None
        self.last_status: TerminalStatus | None = None

        self.last_usage: Mapping[str, Any] | None = None
        self.last_session_id: str | None = None
        self._seen_events: OrderedDict[str, RecordedTerminalEvent] = OrderedDict()
        self._fingerprint_counts: OrderedDict[str, int] = OrderedDict()

    @_synchronized
    def _usage_aggregate(self) -> UsageAggregate:
        if not self.session_id:
            raise RuntimeError("progress telemetry must be bound before usage is observed")
        return aggregate_usage(self.session_id, self._usage_components.values())

    @property
    def usage_aggregate(self) -> UsageAggregate:
        """Immutable usage snapshot for the final bound session."""

        return self._usage_aggregate()

    @_synchronized
    def _merge_usage_receipt(
        self,
        receipt: UsageComponentReceipt,
        *,
        register_pending: bool = False,
        complete_pending: bool = False,
    ) -> UsageAggregate:
        if register_pending and complete_pending:
            raise ValueError("usage component cannot be registered and completed together")
        if not complete_pending or receipt.component_id not in self._pending_usage_component_ids:
            self._ensure_mutable()
        if not self.session_id:
            raise RuntimeError("progress telemetry must be bound before usage is observed")
        # Normalize fail-closed at acceptance time. Mismatched or
        # unauthoritative measured facts remain UNKNOWN in the immutable ledger.
        receipt = aggregate_usage(self.session_id, (receipt,)).components[0]
        existing = self._usage_components.get(receipt.component_id)
        if register_pending:
            if receipt.provenance is not UsageProvenance.UNKNOWN:
                raise ValueError("pending usage component must begin UNKNOWN")
            if existing is not None and existing != receipt:
                raise RuntimeError("pending usage component identity already exists")
            self._usage_components[receipt.component_id] = receipt
            self._pending_usage_component_ids.add(receipt.component_id)
        elif complete_pending and receipt.component_id in self._pending_usage_component_ids:
            self._usage_components[receipt.component_id] = receipt
            self._pending_usage_component_ids.discard(receipt.component_id)
        elif existing is None or existing == receipt:
            self._usage_components[receipt.component_id] = receipt
        elif (
            existing.provenance is UsageProvenance.UNKNOWN
            and receipt.provenance is not UsageProvenance.UNKNOWN
        ):
            # Legacy non-reserved callers may still refine an unknown component
            # with terminal numeric evidence under the same identity.
            self._usage_components[receipt.component_id] = receipt
        else:
            # Conflicting duplicate evidence never silently replaces known
            # usage. Preserve the component identity and fail its truth closed.
            self._usage_components[receipt.component_id] = UsageComponentReceipt(
                component_id=receipt.component_id,
                source=receipt.source,
                session_id=self.session_id,
                provenance=UsageProvenance.UNKNOWN,
                authority=receipt.authority,
                accepted_event_id=receipt.accepted_event_id,
                reason="conflicting_component_update",
            )
        self._usage_components.move_to_end(receipt.component_id)
        return self._usage_aggregate()

    @_synchronized
    def record_usage_receipt(self, receipt: UsageComponentReceipt) -> UsageAggregate:
        """Accept one immutable sanitized receipt through the common merge rail."""
        return self._merge_usage_receipt(receipt)

    @_synchronized
    def record_usage_component(
        self,
        *,
        component_id: str,
        session_id: str,
        provenance: UsageProvenance | str,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        total_tokens: int | None = None,
        cache_read_tokens: int | None = None,
        cache_write_tokens: int | None = None,
        reasoning_tokens: int | None = None,
        source: str = "model_response",
        authority: str = "provider_response",
        authoritative: bool | None = None,
        accepted_event_id: str | None = None,
        details: Mapping[str, Any] | None = None,
        reason: str | None = None,
        provider_payload: Any = None,
        pending: bool = False,
        complete_pending: bool = False,
    ) -> UsageAggregate:
        """Record one sanitized component; raw payloads are intentionally ignored."""

        del provider_payload
        if not self.session_id:
            raise RuntimeError("progress telemetry must be bound before usage is observed")
        return self._merge_usage_receipt(
            UsageComponentReceipt(
                component_id=component_id,
                source=source,
                session_id=session_id,
                provenance=provenance,
                authority=authority,
                authoritative=(
                    UsageProvenance(provenance) is UsageProvenance.MEASURED
                    if authoritative is None
                    else authoritative
                ),
                accepted_event_id=accepted_event_id or component_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
                reasoning_tokens=reasoning_tokens,
                details=details or {},
                reason=reason,
            ),
            register_pending=pending,
            complete_pending=complete_pending,
        )

    @_synchronized
    def record_usage_mapping(
        self,
        usage: Mapping[str, Any],
        *,
        default_component_id: str,
        session_id: str | None,
    ) -> UsageAggregate:
        """Sanitize an event usage envelope and merge every child receipt."""

        if not self.session_id:
            raise RuntimeError("progress telemetry must be bound before usage is observed")
        receipts = usage_components_from_mapping(
            usage,
            default_component_id=default_component_id,
            fallback_session_id=session_id,
        )
        for receipt in receipts:
            self._merge_usage_receipt(receipt)
        return self._usage_aggregate()

    @_synchronized
    def get_activity_snapshot(self) -> dict[str, Any]:
        """Return the production observation shape without reclassifying it."""

        usage = self._usage_aggregate()
        return {
            "session_id": self.session_id,
            "context_id": self.context_id,
            "turn_generation": self._turn_generation,
            "attempt_seq": self.attempt_seq,
            "progress_seq": self.progress_seq,
            "failure_seq": self.failure_seq,
            "failure_streak": self.failure_streak,
            "no_progress_streak": self.no_progress_streak,
            "last_event_id": self.last_event_id,
            "last_event_sequence": self.last_event_sequence,
            "last_call_id": self.last_call_id,
            "last_adapter": self.last_adapter,
            "last_source": self.last_source,
            "last_status": self.last_status.value if self.last_status else None,
            # Activity summaries cross diagnostics/gateway boundaries. Keep
            # only the deterministic fingerprint; raw tool/model results may
            # contain prompts, credentials, or private business data.
            "last_result_hash": self.last_result_hash,
            "last_retryability": self.last_retryability.value,
            "last_error_code": self.last_error_code,
            "last_usage": self.last_usage,
            "last_session_id": self.last_session_id,
            "usage": usage.to_dict(),
            "usage_provenance": usage.provenance.value,
            "known_component_count": usage.known_component_count,
            "unknown_component_count": usage.unknown_component_count,
            "usage_verified": usage.usage_verified,
            "headroom_verified": usage.headroom_verified,
            "is_non_retryable_failure": (
                self.last_status is TerminalStatus.FAILURE
                and self.last_retryability is Retryability.NON_RETRYABLE
            ),
        }

    @_synchronized
    def record_attempt_completion(
        self,
        tool_name: str,
        args: Mapping[str, Any] | None,
        result: Any,
        *,
        is_failure: bool,
        failure_sig: str | None = None,
        retryability: Retryability | str | None = None,
        error_code: int | None = None,
        event_id: str | None = None,
        event_sequence: int | None = None,
        call_id: str | None = None,
        adapter: str = "unknown",
        source: str = "tool_executor",
        usage: Mapping[str, Any] | None = None,
        session_id: str | None = None,
        verified_progress: bool = False,
    ) -> RecordedTerminalEvent:
        """Production-compatible completion seam layered on TerminalEvent."""

        code = error_code if error_code is not None else extract_error_code(result)
        classified = classify_retryability(
            status_code=code,
            error=result,
            explicit=retryability,
        )
        stable_call_id = str(call_id or "").strip()
        stable_event_id = str(event_id or "").strip()
        if not stable_event_id:
            stable_event_id = (
                f"{source}:{stable_call_id or tool_name}:{self.attempt_seq + 1}"
            )
        if not stable_call_id:
            stable_call_id = stable_event_id
        return self.record_terminal_event(
            TerminalEvent(
                event_id=stable_event_id,
                call_id=stable_call_id,
                adapter=adapter or "unknown",
                source=source or "tool_executor",
                status=(TerminalStatus.FAILURE if is_failure else TerminalStatus.SUCCESS),
                result=result,
                retryability=classified,
                error_code=code,
                failure_signature=failure_sig,
                event_sequence=event_sequence,
                usage=usage,
                session_id=session_id or self.session_id,
            ),
            tool_name=tool_name,
            args=args,
            verified_progress=verified_progress,
        )

    @_synchronized
    def record_terminal_event(
        self,
        event: TerminalEvent,
        *,
        tool_name: str,
        args: Mapping[str, Any] | None = None,
        verified_progress: bool = False,
    ) -> RecordedTerminalEvent:
        self._ensure_mutable()
        if not self.session_id:
            raise RuntimeError("progress telemetry must be bound to a session before events")
        prior = self._seen_events.get(event.event_id)
        if prior is not None:
            self._seen_events.move_to_end(event.event_id)
            # The persisted marker remains payload-free, while the immediate
            # caller still receives the duplicate event's current result so
            # executor semantics are unchanged.
            return replace(prior, replayed=True, result=event.result)

        result_hash = hashlib.sha256(normalize_result(event.result).encode("utf-8")).hexdigest()
        attempt_key = f"{tool_name}:{canonical_args_hash(args)}"
        fingerprint = f"{attempt_key}:{result_hash}"
        prior_count = self._fingerprint_counts.get(fingerprint, 0)

        if event.status is not TerminalStatus.SUCCESS:
            outcome = AttemptOutcome.UNKNOWN
        elif verified_progress and prior_count == 0:
            outcome = AttemptOutcome.VERIFIED_PROGRESS
        elif prior_count:
            outcome = AttemptOutcome.VERIFIED_NO_PROGRESS
        else:
            outcome = AttemptOutcome.UNKNOWN

        self.attempt_seq += 1
        if event.status is TerminalStatus.FAILURE:
            self.failure_seq += 1
            if event.retryability is Retryability.NON_RETRYABLE:
                self.failure_streak += 1
            else:
                self.failure_streak = 0
        elif event.status is TerminalStatus.SUCCESS:
            self.failure_streak = 0

        if outcome is AttemptOutcome.VERIFIED_PROGRESS:
            self.progress_seq += 1
            self.no_progress_streak = 0
            self.failure_streak = 0
            self._fingerprint_counts.clear()
        elif outcome is AttemptOutcome.VERIFIED_NO_PROGRESS:
            self.no_progress_streak = max(self.no_progress_streak, prior_count + 1)

        if event.status is TerminalStatus.SUCCESS:
            self._fingerprint_counts[fingerprint] = prior_count + 1
            self._fingerprint_counts.move_to_end(fingerprint)
            while len(self._fingerprint_counts) > self._fingerprint_capacity:
                self._fingerprint_counts.popitem(last=False)

        self.last_event_id = event.event_id
        self.last_call_id = event.call_id
        self.last_adapter = event.adapter
        self.last_source = event.source
        self.last_result_hash = result_hash
        self.last_attempt_key = attempt_key
        self.last_outcome = outcome
        self.last_retryability = event.retryability
        self.last_error_code = event.error_code
        self.last_event_sequence = (
            event.event_sequence if event.event_sequence is not None else self.attempt_seq
        )
        self.last_status = event.status

        if isinstance(event.usage, Mapping):
            event_receipts = usage_components_from_mapping(
                event.usage,
                default_component_id=f"terminal-event:{event.event_id}",
                fallback_session_id=event.session_id,
                default_source=event.source,
                default_authority="backend_observed",
                default_accepted_event_id=event.event_id,
            )
            for receipt in event_receipts:
                self._merge_usage_receipt(receipt)
            self.last_usage = aggregate_usage(self.session_id, event_receipts).to_dict()
        else:
            self.last_usage = None
        # The telemetry identity is immutable. A producer-supplied mismatch is
        # represented in the usage receipt, never propagated as a new session.
        self.last_session_id = self.session_id
        recorded = RecordedTerminalEvent(
            event.event_id,
            outcome,
            # Replay markers carry identity and sanitized snapshots only. Raw
            # provider/tool payloads can contain prompts, credentials, or
            # private business data and must not persist in telemetry state.
            result=None,
            event_sequence=self.last_event_sequence,
            snapshot=self.get_activity_snapshot(),
        )
        self._seen_events[event.event_id] = recorded
        self._seen_events.move_to_end(event.event_id)
        while len(self._seen_events) > self._dedupe_capacity:
            self._seen_events.popitem(last=False)
        return recorded


__all__ = [
    "AttemptOutcome",
    "ProgressTelemetry",
    "RecordedTerminalEvent",
    "Retryability",
    "TerminalEvent",
    "TerminalStatus",
    "canonical_args_hash",
    "classify_retryability",
    "extract_error_code",
    "normalize_result",
]
