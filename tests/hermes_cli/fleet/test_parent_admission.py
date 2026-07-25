from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from hermes_cli.fleet.capacity import BridgeUsageAdapter
from hermes_cli.fleet.config import parse_fleet_config
from hermes_cli.fleet.inspection import build_inspection_payload
from hermes_cli.fleet.service import FleetService
from hermes_cli.fleet.state import FleetStore
from hermes_cli.fleet.types import (
    AdapterKind,
    LaneProfile,
    OverageState,
    ParentLeaseHandle,
    ParentPin,
    Qualification,
    ReasonCode,
    RoutePurpose,
    TaskSpec,
)


NOW = datetime(2026, 7, 24, 9, 30, tzinfo=timezone.utc)
CAPABILITIES = frozenset({"workspace_write", "shell"})


def _profiles() -> tuple[LaneProfile, ...]:
    return (
        LaneProfile(
            lane_id="chatgpt_codex",
            order=0,
            adapter_kind=AdapterKind.NATIVE_PROVIDER,
            provider_id="openai-codex",
            ordered_models=("gpt-parent",),
            supported_efforts=("low", "high"),
            capabilities=CAPABILITIES,
            allowed_auth_kinds=frozenset({"oauth_subscription"}),
            supports_parent_session=True,
        ),
        LaneProfile(
            lane_id="grok",
            order=1,
            adapter_kind=AdapterKind.NATIVE_PROVIDER,
            provider_id="xai-oauth",
            ordered_models=("grok-parent",),
            supported_efforts=("low", "high"),
            capabilities=CAPABILITIES,
            allowed_auth_kinds=frozenset({"oauth_subscription"}),
            supports_parent_session=True,
        ),
    )


def _qualification(profile: LaneProfile, *, canary: str = "") -> Qualification:
    return Qualification(
        qualified=True,
        captured_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        auth_kind="oauth_subscription",
        auth_source=f"{profile.provider_id}:oauth_subscription",
        overage_disabled=True,
        provider_id=profile.provider_id,
        models=profile.ordered_models,
        efforts=profile.supported_efforts,
        fast_off_supported=True,
        capabilities=profile.capabilities,
        evidence_id=f"qualification:{profile.lane_id}:{canary}",
        detail=f"safe route proof {canary}",
        subscription_only_proven=True,
        paid_fallback_absent=True,
        overage_state=OverageState.OFF,
    )


def _bridge(path: Path, codex: str = "80.000", grok: str = "80.000") -> None:
    lanes = {}
    for lane_id, remaining in (
        ("chatgpt_codex", codex),
        ("grok", grok),
    ):
        lanes[lane_id] = {
            "used_pct": str(Decimal("100.000") - Decimal(remaining)),
            "remaining_pct": remaining,
            "confidence": "high",
            "overage_disabled": True,
            "comparability_group": "subscription-weekly",
            "quota_window_id": "2026-W30",
            "measurement_kind": "measured",
        }
    path.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "captured_at": NOW.isoformat(),
                "lanes": lanes,
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )


def _service(
    tmp_path: Path,
    *,
    store_path: Path | None = None,
    bridge_path: Path | None = None,
    canary: str = "",
) -> FleetService:
    bridge = bridge_path or (tmp_path / "usage.json")
    if not bridge.exists():
        _bridge(bridge)
    profiles = _profiles()
    config = parse_fleet_config(
        {
            "fleet": {
                "enabled": True,
                "parent_desktop_enabled": True,
                "bridge_usage_file": str(bridge),
                "lanes": {
                    "chatgpt_codex": {"enabled": True},
                    "grok": {"enabled": True},
                },
            }
        }
    )
    return FleetService(
        config=config,
        store=FleetStore(store_path or (tmp_path / "fleet" / "state.db")),
        profiles=profiles,
        qualifications={
            profile.lane_id: _qualification(profile, canary=canary)
            for profile in profiles
        },
        adapters={},
        capacity_source=BridgeUsageAdapter(bridge),
        now=lambda: NOW,
        owner_uuid="parent-service-owner",
    )


def _task(session_id: str = "stored-session-1") -> TaskSpec:
    return TaskSpec(
        task_id=session_id,
        cwd=Path("."),
        required_capabilities=CAPABILITIES,
        reservation_pct=Decimal("5.000"),
    )


def _admit(
    service: FleetService,
    *,
    lineage: str = "lineage-1",
    session_id: str = "stored-session-1",
    preferred_lane_id: str | None = None,
    preferred_provider_id: str | None = None,
    preferred_model_id: str | None = None,
):
    return service.admit_parent(
        profile_id="default",
        lineage_root_id=lineage,
        session_id=session_id,
        task=_task(session_id),
        preferred_lane_id=preferred_lane_id,
        preferred_provider_id=preferred_provider_id,
        preferred_model_id=preferred_model_id,
    )


def test_parent_admission_atomically_persists_pin_advances_cursor_and_audits(
    tmp_path,
):
    service = _service(tmp_path)

    result = _admit(service)

    assert result.reason is ReasonCode.MET
    assert isinstance(result.pin, ParentPin)
    assert result.pin.purpose is RoutePurpose.DESKTOP_PARENT
    assert result.pin.lane_id == "chatgpt_codex"
    assert result.pin.selection_reason is ReasonCode.ROTATION
    assert result.pin.fast_mode is False
    assert service.store.rotation_cursor(
        purpose=RoutePurpose.DESKTOP_PARENT
    ) == 1
    events = service.store.audit(task_id="parent:default:lineage-1")
    assert [event["event_type"] for event in events] == ["PARENT_ROUTE_SELECTED"]
    assert events[0]["reason_code"] == ReasonCode.ROTATION.value


def test_exact_parent_preference_is_authoritative_and_pinned(tmp_path):
    service = _service(tmp_path)

    result = _admit(
        service,
        preferred_lane_id="grok",
        preferred_provider_id="xai-oauth",
        preferred_model_id="grok-parent",
    )

    assert result.reason is ReasonCode.MET
    assert result.pin is not None
    assert result.pin.lane_id == "grok"
    assert result.pin.provider_id == "xai-oauth"
    assert result.pin.model_id == "grok-parent"
    assert result.pin.selection_reason is ReasonCode.MANUAL_OVERRIDE


@pytest.mark.parametrize(
    ("lane_id", "provider_id", "model_id"),
    [
        ("antigravity", "antigravity-subscription", "gemini-3.1-pro-high"),
        ("grok", "wrong-provider", "grok-parent"),
        ("grok", "xai-oauth", "unqualified-model"),
    ],
)
def test_unavailable_or_mismatched_parent_preference_fails_closed(
    tmp_path,
    lane_id,
    provider_id,
    model_id,
):
    service = _service(tmp_path)

    result = _admit(
        service,
        preferred_lane_id=lane_id,
        preferred_provider_id=provider_id,
        preferred_model_id=model_id,
    )

    assert result.reason is ReasonCode.NO_ELIGIBLE_LANE
    assert result.pin is None
    assert service.resolve_parent_pin(
        profile_id="default",
        lineage_root_id="lineage-1",
    ) is None
    assert service.store.rotation_cursor(
        purpose=RoutePurpose.DESKTOP_PARENT
    ) == 0


def test_repeated_admission_returns_original_without_selector_invocation(
    tmp_path, monkeypatch
):
    service = _service(tmp_path)
    first = _admit(service)

    monkeypatch.setattr(
        "hermes_cli.fleet.state.select_lane",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("selector must not run for an existing lineage")
        ),
    )
    repeated = _admit(service, session_id="compression-tip")

    assert repeated.pin == first.pin
    assert service.store.rotation_cursor(
        purpose=RoutePurpose.DESKTOP_PARENT
    ) == 1


def test_32_concurrent_admissions_for_one_lineage_pin_once_and_advance_once(
    tmp_path,
):
    store_path = tmp_path / "fleet" / "state.db"
    bridge_path = tmp_path / "usage.json"
    _bridge(bridge_path)

    def attempt(index: int):
        service = _service(
            tmp_path,
            store_path=store_path,
            bridge_path=bridge_path,
        )
        return _admit(service, session_id=f"stored-{index}")

    with ThreadPoolExecutor(max_workers=16) as executor:
        results = list(executor.map(attempt, range(32)))

    pins = {result.pin for result in results}
    assert len(pins) == 1
    store = FleetStore(store_path)
    assert store.rotation_cursor(purpose=RoutePurpose.DESKTOP_PARENT) == 1
    assert len(
        [
            event
            for event in store.audit(task_id="parent:default:lineage-1")
            if event["event_type"] == "PARENT_ROUTE_SELECTED"
        ]
    ) == 1


def test_capacity_reversal_and_compression_tip_do_not_move_parent_pin(tmp_path):
    bridge = tmp_path / "usage.json"
    _bridge(bridge, codex="80.000", grok="60.000")
    service = _service(tmp_path, bridge_path=bridge)
    first = _admit(service)

    _bridge(bridge, codex="0.000", grok="100.000")
    repeated = _admit(service, session_id="compression-tip")
    resolved = service.resolve_parent_pin(
        profile_id="default",
        lineage_root_id="lineage-1",
    )

    assert repeated.pin == first.pin
    assert resolved == first.pin


def test_parent_turn_lease_is_owner_generation_safe_and_expiry_keeps_pin(
    tmp_path,
):
    service = _service(tmp_path)
    admission = _admit(service)
    assert admission.pin is not None

    turn = service.acquire_parent_turn(
        profile_id="default",
        lineage_root_id="lineage-1",
        task=_task(),
    )
    assert isinstance(turn.lease, ParentLeaseHandle)
    stale = replace(turn.lease, owner_uuid="stale-owner")
    renewed = service.store.heartbeat_parent(
        turn.lease,
        ttl_seconds=90,
        now=NOW,
    )

    assert renewed is not None
    assert service.store.heartbeat_parent(
        stale, ttl_seconds=90, now=NOW
    ) is None
    assert not service.store.release_parent_turn(
        stale, outcome="failed", now=NOW
    )
    assert service.store.reap_expired(now=NOW + timedelta(seconds=91)) == 1
    assert service.resolve_parent_pin(
        profile_id="default", lineage_root_id="lineage-1"
    ) == admission.pin


def test_unavailable_parent_pin_fails_closed_without_selecting_an_alternate(
    tmp_path,
):
    service = _service(tmp_path)
    admission = _admit(service)
    assert admission.pin is not None
    service.qualifications.pop(admission.pin.lane_id)

    turn = service.acquire_parent_turn(
        profile_id="default",
        lineage_root_id="lineage-1",
        task=_task(),
    )

    assert turn.reason is ReasonCode.PINNED_LANE_UNAVAILABLE
    assert turn.pin == admission.pin
    assert turn.lease is None
    assert service.store.rotation_cursor(
        purpose=RoutePurpose.DESKTOP_PARENT
    ) == 1


def test_parent_audit_excludes_prompt_token_credential_and_environment_values(
    tmp_path,
):
    canary = "PARENT_SECRET_CANARY_83cf"
    service = _service(tmp_path, canary=canary)

    result = _admit(service)
    serialized = json.dumps(service.store.audit(), sort_keys=True)

    assert result.pin is not None
    assert canary not in serialized
    for forbidden in ("prompt", "token", "credential", "environment"):
        assert forbidden not in serialized.lower()


def test_injected_parent_transaction_failure_has_no_partials(tmp_path):
    service = _service(tmp_path)

    with pytest.raises(RuntimeError, match="injected parent transaction"):
        service.store.admit_parent(
            profile_id="default",
            lineage_root_id="lineage-1",
            session_id="stored-session-1",
            task=_task(),
            candidates=service._inputs(
                NOW, purpose=RoutePurpose.DESKTOP_PARENT
            ),
            now=NOW,
            inject_failure=True,
        )

    assert service.store.read_parent_pin("default", "lineage-1") is None
    assert service.store.rotation_cursor(
        purpose=RoutePurpose.DESKTOP_PARENT
    ) == 0
    assert service.store.audit() == []


def test_inspection_serializes_parent_and_worker_eligibility_separately(
    tmp_path,
):
    service = _service(tmp_path)

    payload = build_inspection_payload(service, command="status")

    worker = payload["purposes"]["task_worker"]
    parent = payload["purposes"]["desktop_parent"]
    assert worker["route_purpose"] == RoutePurpose.TASK_WORKER.value
    assert parent["route_purpose"] == RoutePurpose.DESKTOP_PARENT.value
    assert parent["enabled"] is True
    assert parent["evaluations"][0]["supports_parent_session"] is True
    capacity = parent["evaluations"][0]["capacity"]
    assert capacity["comparability_group"] == "subscription-weekly"
    assert capacity["quota_window_id"] == "2026-W30"
    assert capacity["measurement_kind"] == "measured"
    assert payload["pin_state"] == {
        "task_worker": {"total": 0, "by_lane": {}, "by_status": {}},
        "desktop_parent": {"total": 0, "by_lane": {}, "by_status": {}},
    }
    assert not service.store.path.exists()


def test_inspection_reports_redacted_parent_pin_state(tmp_path):
    service = _service(tmp_path)
    admission = service.admit_parent(
        profile_id="default",
        lineage_root_id="lineage-status",
        session_id="session-status",
        task=_task("session-status"),
    )
    assert admission.pin is not None

    payload = build_inspection_payload(service, command="status")

    assert payload["pin_state"]["desktop_parent"] == {
        "total": 1,
        "by_lane": {"chatgpt_codex": 1},
        "by_status": {"pinned": 1},
    }
    assert payload["pin_state"]["task_worker"]["total"] == 0
    assert "lineage-status" not in str(payload["pin_state"])
