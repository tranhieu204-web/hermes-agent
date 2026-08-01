from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from hermes_cli.fleet.policy import evaluate_lane
from hermes_cli.fleet.types import (
    AdapterKind,
    CapacityRead,
    CapacitySnapshot,
    Confidence,
    Freshness,
    HealthRead,
    LaneHealth,
    LaneInputs,
    LaneProfile,
    MeasurementKind,
    OverageState,
    Qualification,
    ReasonCode,
    RoutePurpose,
    TaskSpec,
)
from hermes_cli.fleet.usage_refresh import refresh_usage_document


NOW = datetime(2026, 7, 30, 7, 0, tzinfo=timezone.utc)


def test_stale_console_usage_is_explicitly_unknown_after_fresh_health_probe(
    tmp_path, monkeypatch
):
    path = tmp_path / "usage-weekly.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "plans-1",
                "checked_at": "2026-07-30T07:00:00Z",
                "plans": [
                    {"label": "ChatGPT Pro · Codex", "weekly_pct_used": 10},
                    {"label": "Claude Max 20x", "weekly_pct_used": 20},
                    {
                        "label": "SuperGrok",
                        "weekly_pct_used": 11,
                        "checked_at": "2026-07-28T00:00:00Z",
                        "measurement_kind": "measured",
                        "comparability_group": "subscription-weekly",
                        "quota_window_id": "subscription-weekly",
                    },
                    {
                        "label": "Google AI · Antigravity",
                        "weekly_pct_used": 56,
                        "checked_at": "2026-07-28T00:00:00Z",
                        "measurement_kind": "measured",
                        "comparability_group": "subscription-weekly",
                        "quota_window_id": "subscription-weekly",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "hermes_cli.fleet.usage_refresh._probe_console_lane_health",
        lambda lane_id: (True, f"{lane_id} healthy"),
    )

    report = refresh_usage_document(
        path=path,
        mirror_path=None,
        fetch_usage=lambda _provider: None,
        now=NOW,
    )

    assert report.ok
    rows = {
        row["label"]: row
        for row in json.loads(path.read_text(encoding="utf-8"))["plans"]
    }
    for label, historical_pct in (
        ("SuperGrok", 11),
        ("Google AI · Antigravity", 56),
    ):
        row = rows[label]
        assert row["weekly_pct_used"] == historical_pct
        assert row["measurement_kind"] == "unknown"
        assert row["usage_status"] == "STALE_UNKNOWN"
        assert row["health_status"] == "UP"
        assert "comparability_group" not in row
        assert "quota_window_id" not in row


# ---------------------------------------------------------------------------
# Ordinary task-worker capacity policy.
#
# The Accelerator's material bridge is NOT entitled to relax fleet-wide routing
# policy.  `build_fleet_service` — the only production FleetService — always
# sets require_verified_health=True, so any change here changes every ordinary
# task-worker route.  These tests pin the parent behaviour: when verified health
# is required, capacity that is stale, missing, or non-comparable is a HARD
# gate, not advisory rotation input.
# ---------------------------------------------------------------------------


_ROUTING_TASK = TaskSpec(
    task_id="ordinary-worker",
    cwd=Path("."),
    required_capabilities=frozenset({"workspace_read", "shell"}),
    reservation_pct=Decimal("5.000"),
)


def _routing_profile(lane_id: str = "chatgpt_codex") -> LaneProfile:
    return LaneProfile(
        lane_id=lane_id,
        order=0,
        adapter_kind=AdapterKind.NATIVE_PROVIDER,
        provider_id=f"{lane_id}-provider",
        ordered_models=("m1", "m2"),
        supported_efforts=("low", "high"),
        capabilities=frozenset({"workspace_read", "shell"}),
        allowed_auth_kinds=frozenset({"oauth_subscription"}),
    )


def _routing_qualification(profile: LaneProfile) -> Qualification:
    return Qualification(
        qualified=True,
        captured_at=NOW - timedelta(minutes=5),
        expires_at=NOW + timedelta(hours=1),
        auth_kind="oauth_subscription",
        auth_source=f"{profile.lane_id}:subscription",
        overage_disabled=True,
        provider_id=profile.provider_id,
        models=profile.ordered_models,
        efforts=profile.supported_efforts,
        fast_off_supported=True,
        capabilities=profile.capabilities,
        evidence_id=f"qualification:{profile.lane_id}",
        subscription_only_proven=True,
        paid_fallback_absent=True,
        overage_state=OverageState.OFF,
    )


def _routing_snapshot(lane_id, **overrides):
    base = dict(
        lane_id=lane_id,
        used_pct=Decimal("40.000"),
        remaining_pct=Decimal("60.000"),
        reserved_pct=Decimal("0"),
        effective_remaining_pct=Decimal("60.000"),
        source_kind="bridge_file",
        source_id=f"bridge:{lane_id}#hash",
        captured_at=NOW - timedelta(minutes=5),
        read_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        freshness=Freshness.FRESH,
        confidence=Confidence.HIGH,
        schema_version="1",
        overage_disabled=True,
        comparability_group="subscription-weekly",
        quota_window_id="2026-W31",
        measurement_kind=MeasurementKind.MEASURED,
    )
    base.update(overrides)
    return CapacitySnapshot(**base)


def _routing_inputs(*, snapshot, require_verified_health=True, reason=None):
    profile = _routing_profile()
    return LaneInputs(
        profile=profile,
        enabled=True,
        adapter_found=True,
        qualification=_routing_qualification(profile),
        capacity=CapacityRead(
            snapshot,
            reason,
            health=HealthRead(
                status=LaneHealth.UP,
                captured_at=NOW,
                read_at=NOW,
                expires_at=NOW + timedelta(minutes=5),
                freshness=Freshness.FRESH,
                source_id=f"health:{profile.lane_id}",
            ),
        ),
        max_concurrency=1,
        reserve_floor_pct=Decimal("0"),
        require_verified_health=require_verified_health,
    )


def test_verified_health_worker_routing_rejects_fresh_but_healthy_baseline_is_eligible():
    """Control: fresh, measured, comparable capacity routes normally."""
    evaluation = evaluate_lane(
        _routing_inputs(snapshot=_routing_snapshot("chatgpt_codex")),
        _ROUTING_TASK,
        now=NOW,
    )
    assert evaluation.eligible
    assert evaluation.reasons == (ReasonCode.MET,)


@pytest.mark.parametrize(
    ("snapshot_overrides", "capacity_reason", "expected"),
    [
        ({"freshness": Freshness.STALE}, None, ReasonCode.USAGE_STALE),
        ({"confidence": Confidence.LOW}, None, ReasonCode.USAGE_STALE),
        (
            {"measurement_kind": MeasurementKind.UNKNOWN},
            None,
            ReasonCode.USAGE_NOT_COMPARABLE,
        ),
        ({"comparability_group": ""}, None, ReasonCode.USAGE_NOT_COMPARABLE),
        ({"quota_window_id": ""}, None, ReasonCode.USAGE_NOT_COMPARABLE),
        (None, ReasonCode.CAPACITY_STALE, ReasonCode.CAPACITY_STALE),
        (None, None, ReasonCode.CAPACITY_MISSING),
    ],
)
def test_stale_missing_or_non_comparable_usage_hard_blocks_ordinary_task_workers(
    snapshot_overrides, capacity_reason, expected
):
    """Parent behaviour: under verified health these are HARD reasons, not advisory.

    Regression guard for the Accelerator scope drift that made them advisory
    fleet-wide in order to satisfy one material-bridge fixture.
    """
    snapshot = (
        None if snapshot_overrides is None
        else _routing_snapshot("chatgpt_codex", **snapshot_overrides)
    )
    evaluation = evaluate_lane(
        _routing_inputs(snapshot=snapshot, reason=capacity_reason),
        _ROUTING_TASK,
        now=NOW,
    )
    assert not evaluation.eligible
    assert expected in evaluation.reasons


def test_capacity_freshness_stays_advisory_when_verified_health_is_not_required():
    """Services that do not require verified health keep deterministic rotation."""
    evaluation = evaluate_lane(
        _routing_inputs(
            snapshot=_routing_snapshot("chatgpt_codex", freshness=Freshness.STALE),
            require_verified_health=False,
        ),
        _ROUTING_TASK,
        now=NOW,
    )
    assert evaluation.eligible
    assert ReasonCode.USAGE_STALE in evaluation.reasons


def test_desktop_parent_routing_is_unaffected_by_the_worker_capacity_gate():
    """The hard capacity gate is a task-worker rule only."""
    profile = replace(_routing_profile(), supports_parent_session=True)
    inputs = replace(
        _routing_inputs(
            snapshot=_routing_snapshot("chatgpt_codex", freshness=Freshness.STALE)
        ),
        profile=profile,
        qualification=replace(
            _routing_qualification(profile), parent_session_proven=True
        ),
    )
    evaluation = evaluate_lane(
        inputs, _ROUTING_TASK, now=NOW, purpose=RoutePurpose.DESKTOP_PARENT
    )
    assert evaluation.eligible
    assert ReasonCode.USAGE_STALE in evaluation.reasons
