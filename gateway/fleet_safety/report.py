"""Structured, side-effect-neutral report for a guard evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from agent.usage_provenance import UsageAggregate, UsageProvenance
from gateway.fleet_safety.deadloop_guard import GuardEvaluationResult, Trip


def _reason_value(trip: Any) -> str:
    reason = getattr(trip, "trip_reason", None) or getattr(trip, "reason", None)
    return str(getattr(reason, "value", reason) or "runaway")


@dataclass(frozen=True)
class HardStopReport:
    """Serializable decision receipt; it asserts no enforcement outcome."""

    session_id: str
    reason: str
    detail: str
    provider: str
    model: str
    effort: str
    runtime_seconds: float
    detector_tokens: int
    detector_calls: int
    detector_token_provenance: UsageProvenance
    last_state: str | None
    usage: UsageAggregate
    decision: str = "hard_stop_required"
    enforcement_outcome: str = "not_asserted"

    @property
    def usage_provenance(self) -> UsageProvenance:
        return self.usage.provenance

    @property
    def known_component_count(self) -> int:
        return self.usage.known_component_count

    @property
    def unknown_component_count(self) -> int:
        return self.usage.unknown_component_count

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "fleet_safety_hard_stop_report",
            "decision": self.decision,
            "enforcement_outcome": self.enforcement_outcome,
            "session_id": self.session_id,
            "reason": self.reason,
            "detail": self.detail,
            "provider": self.provider,
            "model": self.model,
            "effort": self.effort,
            "runtime_seconds": self.runtime_seconds,
            "detector_tokens": self.detector_tokens,
            "detector_calls": self.detector_calls,
            "detector_token_provenance": self.detector_token_provenance.value,
            "last_state": self.last_state,
            "usage_provenance": self.usage.provenance.value,
            "known_component_count": self.usage.known_component_count,
            "unknown_component_count": self.usage.unknown_component_count,
            "fully_measured": self.usage.fully_measured,
            "has_unknown_components": self.usage.has_unknown_components,
            "measured_vs_unknown": self.usage.measured_vs_unknown,
            "usage_verified": self.usage.usage_verified,
            "headroom_verified": self.usage.headroom_verified,
            "usage_receipt": self.usage.to_dict(),
        }


def build_hard_stop_report(evaluation: GuardEvaluationResult | Trip) -> HardStopReport:
    """Bind a structured report to the immutable guard evaluation."""

    return HardStopReport(
        session_id=evaluation.session_id,
        reason=evaluation.reason.value,
        detail=evaluation.detail,
        provider=evaluation.provider,
        model=evaluation.model,
        effort=evaluation.effort,
        runtime_seconds=evaluation.runtime_seconds,
        detector_tokens=evaluation.estimated_tokens,
        detector_calls=evaluation.estimated_calls,
        detector_token_provenance=evaluation.token_count_provenance,
        last_state=evaluation.last_state,
        usage=evaluation.usage,
    )


def _reason_value(trip: Any) -> str:
    reason = getattr(trip, "trip_reason", None) or getattr(trip, "reason", None)
    return str(getattr(reason, "value", reason) or "runaway")


def _usage_lines(trip: Any) -> list[str]:
    usage = getattr(trip, "usage", None)
    provenance = getattr(usage, "provenance", None)
    quality = str(
        getattr(provenance, "value", provenance)
        or getattr(trip, "usage_quality", "unknown")
        or "unknown"
    ).lower()
    if quality not in {"measured", "estimated", "mixed", "unknown"}:
        quality = "unknown"

    def _dimension(name: str) -> int:
        direct = int(getattr(trip, name, 0) or 0)
        return direct if direct else int(getattr(usage, name, 0) or 0)

    lines = [
        f"Usage provenance: {quality}",
        f"Input tokens: {_dimension('input_tokens')}",
        f"Output tokens: {_dimension('output_tokens')}",
        f"Cache read tokens: {_dimension('cache_read_tokens')}",
        f"Cache write tokens: {_dimension('cache_write_tokens')}",
        f"Reasoning tokens: {_dimension('reasoning_tokens')}",
    ]
    cost_status = str(getattr(trip, "cost_status", "unknown") or "unknown")
    cost_source = str(getattr(trip, "cost_source", "none") or "none")
    if cost_status == "unknown":
        lines.append("Cost: unknown")
    else:
        cost = float(getattr(trip, "cost", 0.0) or 0.0)
        lines.append(f"Cost: {cost:.6f} ({cost_status}; {cost_source})")
    lines.extend(
        [
            f"Model calls: {int(getattr(trip, 'estimated_calls', 0) or 0)}",
            f"Runtime seconds: {float(getattr(trip, 'runtime_seconds', 0.0) or 0.0):.1f}",
        ]
    )
    return lines


def _running_line(trip: Any) -> Optional[str]:
    provider = str(getattr(trip, "provider", "") or "")
    model = str(getattr(trip, "model", "") or "")
    effort = str(getattr(trip, "effort", "") or "")
    parts = [part for part in (provider, model) if part]
    if effort:
        parts.append(f"effort={effort}")
    return f"Running: {' / '.join(parts)}" if parts else None


def _extension_lines(trip: Any) -> list[str]:
    lines: list[str] = []
    grant = int(getattr(trip, "extension_grant_size", 0) or 0)
    revision = int(getattr(trip, "extension_revision", 0) or 0)
    expires_at = float(getattr(trip, "extension_expires_at", 0.0) or 0.0)
    if grant > 0:
        lines.append(f"Extension grant: {grant} model calls")
    if revision > 0:
        lines.append(f"Extension revision: {revision}")
    if expires_at > 0:
        try:
            expires_text = datetime.fromtimestamp(
                expires_at, tz=timezone.utc
            ).isoformat().replace("+00:00", "Z")
        except (OverflowError, OSError, ValueError):
            expires_text = "unknown"
        lines.append(f"Extension expires at: {expires_text}")
    return lines


def format_continuation_report(trip: Any) -> str:
    """Build a checkpoint notice that never claims a stop or termination."""

    lines = [
        "Extension checkpoint",
        f"Cause: {_reason_value(trip)}",
        f"Status: {str(getattr(trip, 'detail', '') or 'resource checkpoint crossed')}",
    ]
    running = _running_line(trip)
    if running:
        lines.append(running)
    lines.extend(_usage_lines(trip))
    lines.extend(_extension_lines(trip))
    lines.extend(
        [
            "Work remaining: unknown",
            "Action: continuation remains active",
            "Continuing by default. Send STOP or /stop to cancel.",
        ]
    )
    return "\n".join(lines)


def format_safety_stop_report(
    trip: Any,
    *,
    interrupt_request_accepted: bool,
) -> str:
    """Report a stop request without claiming termination or lease release."""

    lines = [
        "Safety stop requested",
        f"Cause: {_reason_value(trip)}",
        f"Evidence: {str(getattr(trip, 'detail', '') or 'verified safety condition')}",
    ]
    running = _running_line(trip)
    if running:
        lines.append(running)
    lines.extend(_usage_lines(trip))
    lines.extend(
        [
            f"Interrupt request accepted: {'yes' if interrupt_request_accepted else 'no'}",
            "Lease: retained until generation-safe gateway unwind",
        ]
    )
    return "\n".join(lines)


def format_kill_report(
    trip: Any,
    interrupt_request_accepted: Optional[bool] = None,
) -> str:
    """Backward-compatible name for the truthful safety-stop formatter."""

    return format_safety_stop_report(
        trip,
        interrupt_request_accepted=bool(interrupt_request_accepted),
    )


__all__ = [
    "HardStopReport",
    "build_hard_stop_report",
    "format_continuation_report",
    "format_kill_report",
    "format_safety_stop_report",
]
