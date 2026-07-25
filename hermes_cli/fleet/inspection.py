"""Shared read-only fleet inspection and serialization.

The CLI and dashboard REST endpoint both use this module so eligibility is
evaluated once and every surface receives the same reason-coded payload.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from hermes_cli.config import load_config_readonly
from hermes_constants import get_hermes_home

from .adapters.live_routes import live_adapters
from .capacity import BridgeUsageAdapter
from .config import parse_fleet_config
from .live import FleetQualificationDoctor
from .live_capacity import LiveUsageAdapter
from .profiles import ordered_profiles
from .service import FleetService
from .state import FleetStore
from .types import (
    CapacitySnapshot,
    Freshness,
    LaneEvaluation,
    ReasonCode,
    RoutePurpose,
    RouteDecision,
    TaskSpec,
)


SCHEMA_VERSION = 1
DEFAULT_CAPABILITIES = frozenset({"workspace_write", "shell"})
_MODEL_LABELS = {
    "claude-opus-4-8": "Claude Opus 4.8",
    "gemini-3.1-pro-high": "Gemini 3.1 Pro (High)",
    "gemini-3.1-pro-low": "Gemini 3.1 Pro (Low)",
    "gemini-3.6-flash-high": "Gemini 3.6 Flash (High)",
    "gemini-3.6-flash-medium": "Gemini 3.6 Flash (Medium)",
    "gemini-3.6-flash-low": "Gemini 3.6 Flash (Low)",
    "gemini-3.5-flash-high": "Gemini 3.5 Flash (High)",
    "gemini-3.5-flash-medium": "Gemini 3.5 Flash (Medium)",
    "gemini-3.5-flash-low": "Gemini 3.5 Flash (Low)",
    "gpt-5.6-sol": "GPT-5.6 Sol",
    "grok-4.5": "Grok 4.5",
}


def build_fleet_service(
    *,
    config_data: Mapping[str, Any] | None = None,
    doctor: FleetQualificationDoctor | None = None,
    adapters: Mapping[str, object] | None = None,
    capacity_source: object | None = None,
    store_path: Path | None = None,
    now=None,
) -> FleetService:
    """Build the live service from read-only, attributable qualifications."""

    config = parse_fleet_config(
        load_config_readonly() if config_data is None else config_data
    )
    profiles = ordered_profiles()
    bridge_capacity_source = BridgeUsageAdapter(config.bridge_usage_file)
    live_capacity_source = capacity_source or LiveUsageAdapter(
        bridge_capacity_source
    )

    def bridge_billing_status(lane_id: str) -> Mapping[str, object]:
        """Map fresh bridge capacity overage flags into doctor billing evidence."""
        read_at = now() if now is not None else None
        snapshot = bridge_capacity_source.read(lane_id, now=read_at).snapshot
        if (
            snapshot is None
            or snapshot.freshness is not Freshness.FRESH
            or snapshot.overage_disabled is None
        ):
            return {}
        return {
            "overage_state": (
                "off" if snapshot.overage_disabled else "on"
            )
        }

    live_doctor = doctor or FleetQualificationDoctor(
        billing_status=bridge_billing_status
    )
    qualifications = live_doctor.qualify(profiles)
    return FleetService(
        config=config,
        store=FleetStore(store_path or (get_hermes_home() / "fleet" / "state.db")),
        profiles=profiles,
        qualifications=qualifications,
        adapters=dict(
            live_adapters(qualifications=qualifications)
            if adapters is None
            else adapters
        ),
        capacity_source=live_capacity_source,
        now=now,
        require_verified_health=True,
    )


def serialize_capacity(
    snapshot: CapacitySnapshot | None,
) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    source_hash = snapshot.source_id.rpartition("#")[2]
    return {
        "lane_id": snapshot.lane_id,
        "source_kind": snapshot.source_kind,
        "source_id": snapshot.source_id,
        "source_hash": source_hash,
        "captured_at": snapshot.captured_at.isoformat(),
        "read_at": snapshot.read_at.isoformat(),
        "expires_at": snapshot.expires_at.isoformat(),
        "freshness": snapshot.freshness.value,
        "confidence": snapshot.confidence.value,
        "schema_version": snapshot.schema_version,
        "used_pct": str(snapshot.used_pct),
        "remaining_pct": str(snapshot.remaining_pct),
        "reserved_pct": str(snapshot.reserved_pct),
        "effective_remaining_pct": str(snapshot.effective_remaining_pct),
        "overage_disabled": snapshot.overage_disabled,
        "comparability_group": snapshot.comparability_group,
        "quota_window_id": snapshot.quota_window_id,
        "measurement_kind": snapshot.measurement_kind.value,
    }


def serialize_evaluation(
    item: LaneEvaluation,
    *,
    purpose: RoutePurpose = RoutePurpose.TASK_WORKER,
) -> dict[str, Any]:
    """Serialize one authoritative lane evaluation for every UI surface."""

    enabled = ReasonCode.LANE_DISABLED not in item.reasons
    return {
        "lane_id": item.lane_id,
        "enabled": enabled,
        "eligible": item.eligible,
        "selectable": item.eligible or item.fallback_eligible,
        "fallback_eligible": item.fallback_eligible,
        "route_purpose": purpose.value,
        "supports_task_worker": item.profile.supports_task_worker,
        "supports_parent_session": item.profile.supports_parent_session,
        "reasons": [reason.value for reason in item.reasons],
        "adapter_kind": item.profile.adapter_kind.value,
        "provider_id": item.profile.provider_id,
        "model_id": item.selected_model,
        "model_label": _MODEL_LABELS.get(
            item.selected_model or "",
            item.selected_model,
        ),
        "effort": item.selected_effort,
        "qualification_evidence_id": item.qualification_evidence_id,
        "qualification_detail": item.qualification_detail,
        "capacity": serialize_capacity(item.capacity),
    }


def serialize_evaluations(
    items: Sequence[LaneEvaluation],
    *,
    lane: str | None = None,
    purpose: RoutePurpose = RoutePurpose.TASK_WORKER,
) -> list[dict[str, Any]]:
    return [
        serialize_evaluation(item, purpose=purpose)
        for item in items
        if lane is None or item.lane_id == lane
    ]


def serialize_selected(decision: RouteDecision) -> dict[str, Any] | None:
    match = next(
        (item for item in decision.evaluations if item.lane_id == decision.lane_id),
        None,
    )
    return serialize_evaluation(match) if match is not None else None


def inspection_task(command: str, *, reservation_pct: Decimal) -> TaskSpec:
    return TaskSpec(
        task_id=f"read-only-{command}",
        cwd=Path.cwd(),
        required_capabilities=DEFAULT_CAPABILITIES,
        reservation_pct=reservation_pct,
    )


def build_inspection_payload(
    service: FleetService,
    *,
    command: str = "doctor",
    lane: str | None = None,
) -> dict[str, Any]:
    """Return the stable payload used by ``fleet doctor/status`` and REST."""

    if command not in {"doctor", "status"}:
        raise ValueError(f"unsupported inspection command: {command}")

    task = inspection_task(
        command,
        reservation_pct=service.config.default_reservation_pct,
    )
    evaluations = service.inspect(task, lane_id=lane)
    parent_evaluations = service.inspect_parent(task, lane_id=lane)
    visible = tuple(
        item for item in evaluations if lane is None or item.lane_id == lane
    )
    visible_parent = tuple(
        item
        for item in parent_evaluations
        if lane is None or item.lane_id == lane
    )
    has_route = any(item.eligible for item in visible)
    has_parent_route = (
        service.config.enabled
        and service.config.parent_desktop_enabled
        and any(item.eligible for item in visible_parent)
    )
    try:
        pin_state = service.store.pin_state_summary()
    except (AttributeError, OSError):
        pin_state = {
            "task_worker": {"total": 0, "by_lane": {}, "by_status": {}},
            "desktop_parent": {"total": 0, "by_lane": {}, "by_status": {}},
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "ok": True if command == "status" else has_route,
        "enabled": service.config.enabled,
        "capacity_source": {
            "path": str(service.config.bridge_usage_file),
            "kind": "native_hermes_home",
            "adapter": type(service.capacity_source).__name__,
        },
        "reason": (
            ReasonCode.MET.value if has_route else ReasonCode.NO_ELIGIBLE_LANE.value
        ),
        "evaluations": serialize_evaluations(visible),
        "pin_state": pin_state,
        "purposes": {
            RoutePurpose.TASK_WORKER.value: {
                "route_purpose": RoutePurpose.TASK_WORKER.value,
                "enabled": service.config.enabled,
                "eligible": has_route,
                "evaluations": serialize_evaluations(
                    visible,
                    purpose=RoutePurpose.TASK_WORKER,
                ),
            },
            RoutePurpose.DESKTOP_PARENT.value: {
                "route_purpose": RoutePurpose.DESKTOP_PARENT.value,
                "enabled": service.config.parent_desktop_enabled,
                "eligible": has_parent_route,
                "evaluations": serialize_evaluations(
                    visible_parent,
                    purpose=RoutePurpose.DESKTOP_PARENT,
                ),
            },
        },
    }
