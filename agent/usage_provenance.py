"""Provider-neutral usage provenance receipts and aggregation.

Only sanitized, immutable audit facts are retained. Provider response payloads,
headers, credentials, and arbitrary nested metadata are deliberately excluded.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from enum import Enum
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Optional


class UsageProvenance(str, Enum):
    """How a component's numeric usage was obtained."""

    MEASURED = "measured"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"
    MIXED = "mixed"

    @classmethod
    def coerce(cls, value: Any) -> "UsageProvenance":
        """Normalize external/internal labels without ever inventing certainty."""

        if isinstance(value, cls):
            return value
        try:
            return cls(str(value or "").strip().lower())
        except ValueError:
            return cls.UNKNOWN


_BACKEND_AUTHORITIES = frozenset(
    {"provider_response", "backend_observed", "delegated_child_backend"}
)
_SAFE_DETAIL_KEYS = frozenset(
    {
        "adapter",
        "api_mode",
        "cache_read_tokens",
        "cache_write_tokens",
        "child_component_count",
        "child_known_component_count",
        "child_provenance",
        "child_session_id",
        "child_unknown_component_count",
        "event_sequence",
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "status",
        "total_tokens",
    }
)


def _clean_text(value: Any, *, fallback: str = "", limit: int = 256) -> str:
    text = str(value or "").strip()
    return (text[:limit] if text else fallback)


def _nonnegative_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return int(number)


def _nonnegative_number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _safe_detail_value(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return value if math.isfinite(value) and value >= 0 else None
    if isinstance(value, str):
        return value[:256]
    return None


def _sanitize_details(details: Any) -> Mapping[str, Any]:
    """Retain only bounded scalar audit facts; never retain provider payloads."""
    if not isinstance(details, Mapping):
        return MappingProxyType({})
    clean: dict[str, Any] = {}
    for raw_key, raw_value in details.items():
        key = str(raw_key or "").strip()
        if key not in _SAFE_DETAIL_KEYS:
            continue
        value = _safe_detail_value(raw_value)
        if value is not None:
            clean[key] = value
    return MappingProxyType(clean)


def _normalize_provenance(value: Any) -> UsageProvenance:
    if isinstance(value, UsageProvenance):
        return value
    try:
        return UsageProvenance(str(value or "").strip().lower())
    except ValueError:
        return UsageProvenance.UNKNOWN


@dataclass(frozen=True)
class UsageComponentReceipt:
    """Immutable sanitized usage fact for one accepted response or event.

    ``authoritative`` is normalized fail-closed: it can remain true only for a
    measured numeric observation issued by an approved backend authority, bound
    to a non-empty session, and carrying the ID of the accepted response/event.
    """

    component_id: str
    session_id: str
    provenance: UsageProvenance
    total_tokens: Optional[int] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cache_read_tokens: Optional[int] = None
    cache_write_tokens: Optional[int] = None
    reasoning_tokens: Optional[int] = None
    source: str = "unspecified"
    authority: str = "unverified"
    authoritative: bool = False
    value: Optional[float] = None
    details: Mapping[str, Any] = field(default_factory=dict)
    accepted_event_id: Optional[str] = None
    reason: Optional[str] = None
    session_matches: bool = True

    def __post_init__(self) -> None:
        component_id = _clean_text(self.component_id)
        if not component_id:
            raise ValueError("usage component_id must be non-empty")
        session_id = _clean_text(self.session_id)
        source = _clean_text(self.source, fallback="unspecified", limit=128)
        authority = _clean_text(self.authority, fallback="unverified", limit=128)
        provenance = _normalize_provenance(self.provenance)
        accepted_event_id = _clean_text(self.accepted_event_id) or None
        reason = _clean_text(self.reason, limit=512) or None

        input_tokens = _nonnegative_int(self.input_tokens)
        output_tokens = _nonnegative_int(self.output_tokens)
        cache_read_tokens = _nonnegative_int(self.cache_read_tokens)
        cache_write_tokens = _nonnegative_int(self.cache_write_tokens)
        reasoning_tokens = _nonnegative_int(self.reasoning_tokens)
        total_tokens = _nonnegative_int(self.total_tokens)
        if total_tokens is None and (input_tokens is not None or output_tokens is not None):
            total_tokens = (input_tokens or 0) + (output_tokens or 0)
        value = _nonnegative_number(self.value)
        if value is None and total_tokens is not None:
            value = float(total_tokens)

        numeric_observed = value is not None or total_tokens is not None
        authoritative = bool(
            self.authoritative
            and provenance is UsageProvenance.MEASURED
            and authority in _BACKEND_AUTHORITIES
            and session_id
            and accepted_event_id
            and numeric_observed
            and self.session_matches
        )
        if provenance is UsageProvenance.UNKNOWN:
            total_tokens = None
            input_tokens = None
            output_tokens = None
            cache_read_tokens = None
            cache_write_tokens = None
            reasoning_tokens = None
            value = None
            authoritative = False

        object.__setattr__(self, "component_id", component_id)
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "authority", authority)
        object.__setattr__(self, "provenance", provenance)
        object.__setattr__(self, "accepted_event_id", accepted_event_id)
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "input_tokens", input_tokens)
        object.__setattr__(self, "output_tokens", output_tokens)
        object.__setattr__(self, "cache_read_tokens", cache_read_tokens)
        object.__setattr__(self, "cache_write_tokens", cache_write_tokens)
        object.__setattr__(self, "reasoning_tokens", reasoning_tokens)
        object.__setattr__(self, "total_tokens", total_tokens)
        object.__setattr__(self, "value", value)
        object.__setattr__(self, "authoritative", authoritative)
        object.__setattr__(self, "details", _sanitize_details(self.details))

    @property
    def known(self) -> bool:
        return self.provenance is not UsageProvenance.UNKNOWN and self.value is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "component_id": self.component_id,
            "source": self.source,
            "session_id": self.session_id,
            "provenance": self.provenance.value,
            "authority": self.authority,
            "authoritative": self.authoritative,
            "value": self.value,
            "details": dict(self.details),
            "accepted_event_id": self.accepted_event_id,
            "reason": self.reason,
            "session_matches": self.session_matches,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "reasoning_tokens": self.reasoning_tokens,
        }


@dataclass(frozen=True)
class UsageAggregate:
    """Aggregate of immutable per-component receipts for one final session."""

    session_id: str
    components: tuple[UsageComponentReceipt, ...]
    total_tokens: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    reasoning_tokens: int
    provenance: UsageProvenance
    component_count: int
    known_component_count: int
    measured_component_count: int
    estimated_component_count: int
    unknown_component_count: int
    authoritative_component_count: int
    fully_measured: bool
    usage_verified: bool

    @property
    def headroom_verified(self) -> bool:
        """Usage receipts alone never prove a context-window headroom claim."""
        return False

    @property
    def known_total(self) -> float:
        return float(sum(component.value or 0.0 for component in self.components if component.known))

    @property
    def has_unknown_components(self) -> bool:
        return self.unknown_component_count > 0

    @property
    def measured_vs_unknown(self) -> str:
        if self.known_component_count and self.unknown_component_count:
            return "known_and_unknown"
        if self.known_component_count:
            return "known_only"
        if self.unknown_component_count:
            return "unknown_only"
        return "no_components"

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "total_tokens": self.total_tokens,
            "known_total": self.known_total,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "provenance": self.provenance.value,
            "component_count": self.component_count,
            "known_component_count": self.known_component_count,
            "measured_component_count": self.measured_component_count,
            "estimated_component_count": self.estimated_component_count,
            "unknown_component_count": self.unknown_component_count,
            "authoritative_component_count": self.authoritative_component_count,
            "fully_measured": self.fully_measured,
            "usage_verified": self.usage_verified,
            "components": [component.to_dict() for component in self.components],
        }


def _session_mismatch(component: UsageComponentReceipt, session_id: str) -> UsageComponentReceipt:
    return replace(
        component,
        provenance=UsageProvenance.UNKNOWN,
        total_tokens=None,
        input_tokens=None,
        output_tokens=None,
        cache_read_tokens=None,
        cache_write_tokens=None,
        reasoning_tokens=None,
        value=None,
        authoritative=False,
        reason=component.reason or "session_mismatch",
        session_matches=False,
    )


def _non_authoritative_measured(component: UsageComponentReceipt) -> UsageComponentReceipt:
    return replace(
        component,
        provenance=UsageProvenance.UNKNOWN,
        total_tokens=None,
        input_tokens=None,
        output_tokens=None,
        cache_read_tokens=None,
        cache_write_tokens=None,
        reasoning_tokens=None,
        value=None,
        authoritative=False,
        reason=component.reason or "non_authoritative_measured_usage",
    )


def aggregate_usage(
    session_id: str,
    components: Iterable[UsageComponentReceipt],
) -> UsageAggregate:
    """Aggregate receipts without provider-name or model-name conditionals."""
    bound_session_id = _clean_text(session_id)
    if not bound_session_id:
        raise ValueError("usage aggregate session_id must be non-empty")

    normalized: list[UsageComponentReceipt] = []
    seen: set[str] = set()
    for component in components:
        if component.component_id in seen:
            continue
        seen.add(component.component_id)
        if component.session_id != bound_session_id:
            component = _session_mismatch(component, bound_session_id)
        elif component.provenance is UsageProvenance.MEASURED and not component.authoritative:
            component = _non_authoritative_measured(component)
        normalized.append(component)

    measured = sum(c.provenance is UsageProvenance.MEASURED for c in normalized)
    estimated = sum(c.provenance is UsageProvenance.ESTIMATED for c in normalized)
    unknown = sum(c.provenance is UsageProvenance.UNKNOWN for c in normalized)
    authoritative = sum(c.authoritative for c in normalized)
    count = len(normalized)
    known = measured + estimated

    if count == 0 or unknown == count:
        provenance = UsageProvenance.UNKNOWN
    elif unknown or (measured and estimated):
        provenance = UsageProvenance.MIXED
    elif estimated:
        provenance = UsageProvenance.ESTIMATED
    else:
        provenance = UsageProvenance.MEASURED

    fully_measured = bool(count and measured == count and authoritative == count)
    return UsageAggregate(
        session_id=bound_session_id,
        components=tuple(normalized),
        total_tokens=sum(c.total_tokens or 0 for c in normalized if c.known),
        input_tokens=sum(c.input_tokens or 0 for c in normalized if c.known),
        output_tokens=sum(c.output_tokens or 0 for c in normalized if c.known),
        cache_read_tokens=sum(c.cache_read_tokens or 0 for c in normalized if c.known),
        cache_write_tokens=sum(c.cache_write_tokens or 0 for c in normalized if c.known),
        reasoning_tokens=sum(c.reasoning_tokens or 0 for c in normalized if c.known),
        provenance=provenance,
        component_count=count,
        known_component_count=known,
        measured_component_count=measured,
        estimated_component_count=estimated,
        unknown_component_count=unknown,
        authoritative_component_count=authoritative,
        fully_measured=fully_measured,
        usage_verified=fully_measured,
    )


def unknown_usage(session_id: str, *, reason: str = "usage_unavailable") -> UsageAggregate:
    return aggregate_usage(
        session_id,
        (
            UsageComponentReceipt(
                component_id="usage:unknown",
                source="usage_fallback",
                session_id=session_id,
                provenance=UsageProvenance.UNKNOWN,
                authority="unverified",
                authoritative=False,
                reason=reason,
            ),
        ),
    )


def _component_from_mapping(
    raw: Mapping[str, Any],
    *,
    default_session_id: str,
    default_component_id: str = "usage:component",
    default_source: str = "usage_mapping",
    default_authority: str = "unverified",
    default_accepted_event_id: str | None = None,
) -> UsageComponentReceipt:
    details = raw.get("details") if isinstance(raw.get("details"), Mapping) else {}
    accepted_event_id = (
        raw.get("accepted_event_id")
        or raw.get("accepted_response_id")
        or raw.get("response_id")
        or raw.get("event_id")
        or default_accepted_event_id
    )
    provenance = _normalize_provenance(raw.get("provenance"))
    authority = _clean_text(raw.get("authority"), fallback=default_authority)
    authoritative = raw.get("authoritative") is True
    if "authoritative" not in raw and authority in _BACKEND_AUTHORITIES:
        authoritative = provenance is UsageProvenance.MEASURED
    return UsageComponentReceipt(
        component_id=_clean_text(raw.get("component_id"), fallback=default_component_id),
        source=_clean_text(raw.get("source"), fallback=default_source),
        session_id=_clean_text(raw.get("session_id"), fallback=default_session_id),
        provenance=provenance,
        authority=authority,
        authoritative=authoritative,
        value=raw.get("value"),
        details=details,
        accepted_event_id=_clean_text(accepted_event_id) or None,
        reason=_clean_text(raw.get("reason"), limit=512) or None,
        session_matches=raw.get("session_matches") is not False,
        input_tokens=raw.get("input_tokens"),
        output_tokens=raw.get("output_tokens"),
        total_tokens=raw.get("total_tokens"),
        cache_read_tokens=raw.get("cache_read_tokens"),
        cache_write_tokens=raw.get("cache_write_tokens"),
        reasoning_tokens=raw.get("reasoning_tokens"),
    )


def usage_components_from_mapping(
    raw: Mapping[str, Any],
    *,
    default_component_id: str,
    fallback_session_id: str | None,
    default_source: str = "usage_mapping",
    default_authority: str = "unverified",
    default_accepted_event_id: str | None = None,
) -> tuple[UsageComponentReceipt, ...]:
    """Return sanitized immutable receipts from one internal usage envelope."""
    session_id = _clean_text(fallback_session_id)
    raw_components = raw.get("components")
    if isinstance(raw_components, (list, tuple)):
        components = tuple(
            _component_from_mapping(
                item,
                default_session_id=session_id,
                default_component_id=f"{default_component_id}:{index}",
                default_source=default_source,
                default_authority=default_authority,
                default_accepted_event_id=default_accepted_event_id,
            )
            for index, item in enumerate(raw_components)
            if isinstance(item, Mapping)
        )
        if components:
            return components
    return (
        _component_from_mapping(
            raw,
            default_session_id=session_id,
            default_component_id=default_component_id,
            default_source=default_source,
            default_authority=default_authority,
            default_accepted_event_id=default_accepted_event_id,
        ),
    )


def usage_aggregate_from_mapping(
    session_id: str,
    raw: Any,
    *,
    default_component_id: str = "usage:aggregate",
    fallback_session_id: str | None = None,
    missing_reason: str = "usage_missing",
    default_source: str = "usage_envelope",
    default_authority: str = "unverified",
    default_accepted_event_id: str | None = None,
) -> UsageAggregate:
    """Normalize a trusted internal usage envelope; arbitrary fields are dropped."""
    if isinstance(raw, UsageAggregate):
        return aggregate_usage(session_id, raw.components)
    if not isinstance(raw, Mapping):
        return unknown_usage(session_id, reason=missing_reason)
    return aggregate_usage(
        session_id,
        usage_components_from_mapping(
            raw,
            default_component_id=default_component_id,
            fallback_session_id=fallback_session_id or session_id,
            default_source=default_source,
            default_authority=default_authority,
            default_accepted_event_id=default_accepted_event_id,
        ),
    )


class DelegatedUsageReceiptSink:
    """Dispatch-bound append-only ownership handle for child usage receipts.

    The progress ledger is the live aggregate while its exact session remains
    mutable. A completion that arrives after that ledger was frozen is retained
    as UNKNOWN in this separate sink instead of rewriting frozen history or
    rebinding attribution to the parent's newer live session.
    """

    def __init__(self, *, parent_session_id: str, telemetry: Any, session_db: Any = None):
        parent_session_id = _clean_text(parent_session_id)
        if not parent_session_id:
            raise ValueError("delegated usage receipt sink requires a parent session_id")
        self.parent_session_id = parent_session_id
        self.telemetry = telemetry
        self.session_db = session_db
        self._receipts: dict[str, UsageComponentReceipt] = {}
        self._lock = __import__("threading").RLock()

    @property
    def receipts(self) -> tuple[UsageComponentReceipt, ...]:
        with self._lock:
            return tuple(self._receipts.values())

    def append(self, receipt: UsageComponentReceipt) -> UsageComponentReceipt:
        """Append one completion once; replay returns the original receipt."""

        with self._lock:
            existing = self._receipts.get(receipt.component_id)
            if existing is not None:
                return existing

            telemetry = self.telemetry
            if telemetry is None:
                reason = "dispatch_parent_ledger_missing"
            elif getattr(telemetry, "session_id", "") != self.parent_session_id:
                reason = "dispatch_parent_ledger_mismatch"
            elif getattr(telemetry, "frozen", False):
                reason = "dispatch_parent_ledger_frozen"
            else:
                telemetry.record_usage_receipt(receipt)
                recorded = next(
                    component
                    for component in telemetry.usage_aggregate.components
                    if component.component_id == receipt.component_id
                )
                self._persist(recorded)
                self._receipts[recorded.component_id] = recorded
                return recorded

            retained = replace(
                receipt,
                provenance=UsageProvenance.UNKNOWN,
                total_tokens=None,
                input_tokens=None,
                output_tokens=None,
                cache_read_tokens=None,
                cache_write_tokens=None,
                reasoning_tokens=None,
                value=None,
                authoritative=False,
                reason=reason,
            )
            self._persist(retained)
            self._receipts[retained.component_id] = retained
            return retained

    def _persist(self, receipt: UsageComponentReceipt) -> None:
        recorder = getattr(self.session_db, "record_delegated_usage_receipt", None)
        if callable(recorder):
            recorder(
                parent_session_id=self.parent_session_id,
                receipt=receipt.to_dict(),
            )


def capture_delegated_usage_receipt_sink(
    parent_agent: Any,
    *,
    parent_session_id: str | None = None,
    telemetry: Any = None,
) -> DelegatedUsageReceiptSink:
    """Capture immutable usage ownership before constructing delegated children."""

    if parent_session_id is None:
        parent_session_id = getattr(parent_agent, "session_id", "")
    if telemetry is None:
        telemetry = getattr(parent_agent, "_progress_telemetry", None)
    return DelegatedUsageReceiptSink(
        parent_session_id=str(parent_session_id or "").strip(),
        telemetry=telemetry,
        session_db=getattr(parent_agent, "_session_db", None),
    )


def delegated_child_usage_receipt(
    *,
    parent_session_id: str,
    component_id: str,
    accepted_event_id: str,
    child_session_id: str,
    child_usage: Any,
) -> UsageComponentReceipt:
    """Create one parent-bound receipt from a child's sanitized usage ledger."""
    parent_session_id = _clean_text(parent_session_id)
    child_session_id = _clean_text(child_session_id)
    if not parent_session_id:
        raise ValueError("parent session_id must be established before child receipt")

    if not child_session_id or child_usage is None:
        return UsageComponentReceipt(
            component_id=component_id,
            source="delegated_child_completion",
            session_id=parent_session_id,
            provenance=UsageProvenance.UNKNOWN,
            authority="delegated_child_backend",
            authoritative=False,
            accepted_event_id=accepted_event_id,
            reason="child_session_or_usage_missing",
            details={"child_session_id": child_session_id},
        )

    child_aggregate = usage_aggregate_from_mapping(child_session_id, child_usage)
    details = {
        "child_session_id": child_session_id,
        "child_component_count": child_aggregate.component_count,
        "child_known_component_count": child_aggregate.known_component_count,
        "child_unknown_component_count": child_aggregate.unknown_component_count,
        "child_provenance": child_aggregate.provenance.value,
    }
    if not child_aggregate.fully_measured:
        return UsageComponentReceipt(
            component_id=component_id,
            source="delegated_child_completion",
            session_id=parent_session_id,
            provenance=UsageProvenance.UNKNOWN,
            authority="delegated_child_backend",
            authoritative=False,
            accepted_event_id=accepted_event_id,
            reason="child_usage_not_backend_measured",
            details=details,
        )

    return UsageComponentReceipt(
        component_id=component_id,
        source="delegated_child_completion",
        session_id=parent_session_id,
        provenance=UsageProvenance.MEASURED,
        authority="delegated_child_backend",
        authoritative=True,
        accepted_event_id=accepted_event_id,
        value=child_aggregate.known_total,
        total_tokens=child_aggregate.total_tokens,
        input_tokens=child_aggregate.input_tokens,
        output_tokens=child_aggregate.output_tokens,
        cache_read_tokens=child_aggregate.cache_read_tokens,
        cache_write_tokens=child_aggregate.cache_write_tokens,
        reasoning_tokens=child_aggregate.reasoning_tokens,
        details=details,
    )
