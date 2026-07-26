"""Plain-text fleet-safety reports with explicit usage provenance."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional


def _reason_value(trip: Any) -> str:
    reason = getattr(trip, "trip_reason", None) or getattr(trip, "reason", None)
    return str(getattr(reason, "value", reason) or "runaway")


def _usage_lines(trip: Any) -> list[str]:
    quality = str(getattr(trip, "usage_quality", "unknown") or "unknown").lower()
    if quality not in {"measured", "estimated", "unknown"}:
        quality = "unknown"
    lines = [
        f"Usage provenance: {quality}",
        f"Input tokens: {int(getattr(trip, 'input_tokens', 0) or 0)}",
        f"Output tokens: {int(getattr(trip, 'output_tokens', 0) or 0)}",
        f"Cache read tokens: {int(getattr(trip, 'cache_read_tokens', 0) or 0)}",
        f"Cache write tokens: {int(getattr(trip, 'cache_write_tokens', 0) or 0)}",
        f"Reasoning tokens: {int(getattr(trip, 'reasoning_tokens', 0) or 0)}",
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
    """Build a post-action report without claiming termination or lease release."""
    lines = [
        "Safety stop requested",
        f"Cause: {_reason_value(trip)}",
        f"Evidence: {str(getattr(trip, 'detail', '') or 'verified no-progress condition')}",
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
    "format_continuation_report",
    "format_kill_report",
    "format_safety_stop_report",
]
