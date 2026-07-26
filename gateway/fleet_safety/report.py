"""Structured, side-effect-neutral report for a guard evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent.usage_provenance import UsageAggregate, UsageProvenance
from gateway.fleet_safety.deadloop_guard import GuardEvaluationResult, Trip


def _humanize_tokens(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(int(n))


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


def format_kill_report(trip: GuardEvaluationResult | Trip) -> str:
    """Render the structured decision without claiming effects have landed."""

    report = build_hard_stop_report(trip)
    runtime_min = report.runtime_seconds / 60.0
    model_bits = " / ".join(
        bit
        for bit in (
            report.provider,
            report.model,
            report.effort and f"effort={report.effort}",
        )
        if bit
    )
    lines = [
        "🛑 Dead-loop guard: HARD-STOP REQUIRED",
        f"• session: {report.session_id}",
    ]
    if model_bits:
        lines.append(f"• running: {model_bits}")
    lines.append(f"• reason: {report.reason} — {report.detail}")
    lines.append(
        f"• detector counter ({report.detector_token_provenance.value}): "
        f"{_humanize_tokens(report.detector_tokens)} tokens over "
        f"{report.detector_calls} model calls, {runtime_min:.1f} min runtime"
    )
    lines.append(
        f"• usage provenance: {report.usage_provenance.value}; "
        f"known components: {report.known_component_count}; "
        f"unknown components: {report.unknown_component_count}"
    )
    lines.append(
        f"• measured-vs-unknown: {report.usage.measured_vs_unknown}; "
        f"usage verified: {str(report.usage.usage_verified).lower()}; "
        f"headroom verified: {str(report.usage.headroom_verified).lower()}"
    )
    if report.last_state:
        lines.append(f"• last state: {report.last_state}")
    lines.append(
        f"• decision: {report.decision}; enforcement outcome: "
        f"{report.enforcement_outcome.replace('_', ' ')}"
    )
    return "\n".join(lines)


__all__ = ["HardStopReport", "build_hard_stop_report", "format_kill_report"]
