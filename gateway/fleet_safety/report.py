"""Human-readable report formatting for runaway guard evaluations.

Plain text builders — no HTML, no clock, no I/O.
"""

from __future__ import annotations

from typing import Any


def _humanize_tokens(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(int(n))


def format_continuation_report(trip: Any) -> str:
    """Build a plain-text continuation notice. Soft notices NEVER say dead loop/killed/hard-stopped."""
    if hasattr(trip, "notice_text") and trip.notice_text:
        return trip.notice_text

    runtime_min = getattr(trip, "runtime_seconds", 0.0) / 60.0
    model_bits = " / ".join(b for b in (getattr(trip, "provider", ""), getattr(trip, "model", "")) if b)
    detail = getattr(trip, "detail", "")

    lines = [
        "⚠️ Agent Session Threshold Notice",
        f"• session: {getattr(trip, 'session_id', '')}",
    ]
    if model_bits:
        lines.append(f"• running: {model_bits}")
    lines.append(f"• status: {detail}")
    lines.append(
        f"• usage: {_humanize_tokens(getattr(trip, 'estimated_tokens', 0))} tokens "
        f"over {getattr(trip, 'estimated_calls', 0)} model calls, {runtime_min:.1f} min runtime"
    )
    lines.append("Continuing by default. Reply STOP or /stop to cancel.")
    return "\n".join(lines)


def format_kill_report(trip: Any) -> str:
    """Build the hard-stop report message for a verified runaway trip."""
    runtime_min = getattr(trip, "runtime_seconds", 0.0) / 60.0
    model_bits = " / ".join(b for b in (getattr(trip, "provider", ""), getattr(trip, "model", ""), getattr(trip, "effort", "") and f"effort={trip.effort}") if b)
    reason_val = getattr(getattr(trip, "trip_reason", None), "value", getattr(getattr(trip, "reason", None), "value", "runaway"))
    detail = getattr(trip, "detail", "")

    lines = [
        "🛑 Fleet safety: agent session HARD-STOPPED",
        f"• session: {getattr(trip, 'session_id', '')}",
    ]
    if model_bits:
        lines.append(f"• running: {model_bits}")
    lines.append(f"• verified cause: {reason_val} — {detail}")
    lines.append(
        f"• spend: {_humanize_tokens(getattr(trip, 'estimated_tokens', 0))} tokens "
        f"over {getattr(trip, 'estimated_calls', 0)} model calls, {runtime_min:.1f} min runtime"
    )
    if getattr(trip, "last_state", None):
        lines.append(f"• last state: {trip.last_state}")
    lines.append("• action: turn interrupted + lease released. No human action required.")
    return "\n".join(lines)
