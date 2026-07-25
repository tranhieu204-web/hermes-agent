from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import json

import pytest

from hermes_cli.fleet.inspection import build_fleet_service, build_inspection_payload
from hermes_cli.fleet.live_capacity import LiveUsageAdapter
from hermes_cli.fleet.capacity import BridgeUsageAdapter
from hermes_cli.fleet.types import (
    CapacityRead,
    CapacitySnapshot,
    Confidence,
    Freshness,
    HealthRead,
    LaneHealth,
    MeasurementKind,
    OverageState,
    Qualification,
    ReasonCode,
    TaskSpec,
)


NOW = datetime(2026, 7, 25, 8, 0, tzinfo=timezone.utc)
TASK = TaskSpec(
    task_id="composition-route",
    cwd=Path("."),
    required_capabilities=frozenset({"workspace_write", "shell"}),
    reservation_pct=Decimal("0"),
)


class _Doctor:
    def qualify(self, profiles):
        result = {}
        for profile in profiles:
            result[profile.lane_id] = Qualification(
                qualified=True,
                captured_at=NOW,
                expires_at=NOW + timedelta(minutes=5),
                auth_kind=next(iter(profile.allowed_auth_kinds)),
                auth_source=f"{profile.lane_id}:subscription",
                overage_disabled=True,
                provider_id=profile.provider_id,
                models=profile.ordered_models,
                efforts=profile.supported_efforts,
                fast_off_supported=True,
                capabilities=profile.capabilities,
                executable=(
                    "C:/tools/agy.exe"
                    if profile.lane_id == "antigravity"
                    else None
                ),
                evidence_id=f"qualification:{profile.lane_id}",
                subscription_only_proven=True,
                paid_fallback_absent=True,
                overage_state=OverageState.OFF,
                parent_session_proven=profile.lane_id == "antigravity",
            )
        return result


def _read(lane_id: str, remaining: str | None, health: LaneHealth) -> CapacityRead:
    health_read = HealthRead(
        status=health,
        captured_at=NOW,
        read_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        freshness=Freshness.FRESH,
        source_id=f"health:{lane_id}",
    )
    if remaining is None:
        return CapacityRead(
            None,
            ReasonCode.CAPACITY_MISSING,
            health=health_read,
        )
    remaining_pct = Decimal(remaining)
    return CapacityRead(
        CapacitySnapshot(
            lane_id=lane_id,
            used_pct=Decimal("100") - remaining_pct,
            remaining_pct=remaining_pct,
            reserved_pct=Decimal("0"),
            effective_remaining_pct=remaining_pct,
            source_kind="authoritative_live_usage",
            source_id=f"live:{lane_id}",
            captured_at=NOW,
            read_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
            freshness=Freshness.FRESH,
            confidence=Confidence.HIGH,
            schema_version="live-1",
            overage_disabled=True,
            comparability_group="subscription-weekly",
            quota_window_id="2026-W30",
            measurement_kind=MeasurementKind.MEASURED,
        ),
        None,
        health=health_read,
    )


class _CapacitySource:
    def __init__(self, reads):
        self.reads = reads

    def read(self, lane_id, **_kwargs):
        return self.reads[lane_id]


@dataclass
class _Adapter:
    calls: int = 0

    def execute(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("adapter must not execute without an eligible lane")


def _service(tmp_path, reads, *, adapters=None):
    return build_fleet_service(
        config_data={
            "fleet": {
                "enabled": True,
                "switch_delta_pct": 20.0,
                "lanes": {
                    "chatgpt_codex": {"enabled": True},
                    "claude_code": {"enabled": True},
                    "grok": {"enabled": True},
                    "antigravity": {"enabled": True},
                    "kimi": {"enabled": False},
                },
            }
        },
        doctor=_Doctor(),
        adapters=adapters
        or {
            lane_id: object()
            for lane_id in (
                "chatgpt_codex",
                "claude_code",
                "grok",
                "antigravity",
            )
        },
        capacity_source=_CapacitySource(reads),
        store_path=tmp_path / "fleet" / "state.db",
        now=lambda: NOW,
    )


def test_composition_root_selects_up_high_headroom_lane_and_rejects_down(tmp_path):
    service = _service(
        tmp_path,
        {
            "chatgpt_codex": _read("chatgpt_codex", "90", LaneHealth.UP),
            "claude_code": _read("claude_code", "99", LaneHealth.DOWN),
            "grok": _read("grok", "100", LaneHealth.DOWN),
            "antigravity": _read("antigravity", "85", LaneHealth.UP),
            "kimi": _read("kimi", None, LaneHealth.UNKNOWN),
        },
    )

    decision = service.plan(TASK)

    assert decision.lane_id == "chatgpt_codex"
    by_lane = {item.lane_id: item for item in decision.evaluations}
    assert by_lane["chatgpt_codex"].eligible
    assert by_lane["antigravity"].eligible
    assert not by_lane["grok"].eligible
    assert not by_lane["claude_code"].eligible
    assert ReasonCode.HEALTH_DOWN in by_lane["grok"].reasons


def test_composition_root_prefers_fresh_authoritative_usage_over_stale_file(
    tmp_path, monkeypatch
):
    bridge = tmp_path / "fleet" / "usage-weekly.json"
    bridge.parent.mkdir(parents=True)
    bridge.write_text(
        json.dumps(
            {
                "checked_at": "2026-07-01T00:00:00Z",
                "plans": [
                    {
                        "label": "ChatGPT Pro · Codex",
                        "weekly_pct_used": 99,
                        "checked_at": "2026-07-01T00:00:00Z",
                        "health_status": "UP",
                        "health_checked_at": "2026-07-25T08:00:00Z",
                        "overage_disabled": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    fetched_providers = []

    def fetch_usage(provider):
        fetched_providers.append(provider)
        return SimpleNamespace(
            available=provider == "openai-codex",
            fetched_at=NOW,
            windows=(
                SimpleNamespace(
                    label="Weekly",
                    used_percent=10,
                    reset_at=None,
                ),
            ),
        )

    monkeypatch.setattr(
        "agent.account_usage.fetch_account_usage",
        fetch_usage,
    )
    service = build_fleet_service(
        config_data={
            "fleet": {
                "enabled": True,
                "bridge_usage_file": str(bridge),
                "lanes": {
                    "chatgpt_codex": {"enabled": True},
                    "claude_code": {"enabled": False},
                    "grok": {"enabled": False},
                    "antigravity": {"enabled": False},
                    "kimi": {"enabled": False},
                },
            }
        },
        doctor=_Doctor(),
        adapters={"chatgpt_codex": object()},
        store_path=tmp_path / "state.db",
        now=lambda: NOW,
    )

    decision = service.plan(TASK)

    assert decision.lane_id == "chatgpt_codex"
    codex = next(
        item
        for item in decision.evaluations
        if item.lane_id == "chatgpt_codex"
    )
    assert codex.capacity is not None
    assert codex.capacity.used_pct == Decimal("10.000")
    assert codex.capacity.source_kind == "authoritative_account_usage"
    assert fetched_providers == ["openai-codex"]


@pytest.mark.parametrize(
    ("lane_id", "windows", "expected_used"),
    [
        (
            "chatgpt_codex",
            (("Session", 95), ("Weekly", 10)),
            Decimal("10.000"),
        ),
        (
            "claude_code",
            (
                ("Current session", 99),
                ("Current week", 20),
                ("Opus week", 30),
                ("Sonnet week", 95),
            ),
            Decimal("30.000"),
        ),
    ],
)
def test_live_usage_uses_only_lane_relevant_weekly_windows(
    tmp_path, lane_id, windows, expected_used
):
    adapter = LiveUsageAdapter(
        BridgeUsageAdapter(tmp_path / "missing-usage.json"),
        fetch_usage=lambda _provider: SimpleNamespace(
            available=True,
            fetched_at=NOW,
            windows=tuple(
                SimpleNamespace(label=label, used_percent=used)
                for label, used in windows
            ),
        ),
    )

    read = adapter.read(lane_id, now=NOW)

    assert read.snapshot is not None
    assert read.snapshot.used_pct == expected_used


def test_lane_scoped_inspection_fetches_requested_live_usage_once(tmp_path):
    bridge = tmp_path / "usage-weekly.json"
    bridge.write_text(
        json.dumps(
            {
                "checked_at": "2026-07-25T08:00:00Z",
                "plans": [
                    {
                        "label": "ChatGPT Pro · Codex",
                        "weekly_pct_used": 25,
                        "checked_at": "2026-07-25T08:00:00Z",
                        "health_status": "UP",
                        "health_checked_at": "2026-07-25T08:00:00Z",
                        "overage_disabled": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    fetched_providers = []

    def fetch_usage(provider):
        fetched_providers.append(provider)
        return SimpleNamespace(
            available=True,
            fetched_at=NOW,
            windows=(SimpleNamespace(label="Weekly", used_percent=25),),
        )

    live_capacity = LiveUsageAdapter(
        BridgeUsageAdapter(bridge),
        fetch_usage=fetch_usage,
        cache_ttl=timedelta(seconds=30),
    )
    service = build_fleet_service(
        config_data={
            "fleet": {
                "enabled": True,
                "lanes": {
                    "chatgpt_codex": {"enabled": True},
                    "claude_code": {"enabled": True},
                    "grok": {"enabled": True},
                    "antigravity": {"enabled": True},
                    "kimi": {"enabled": False},
                },
            }
        },
        doctor=_Doctor(),
        adapters={
            lane_id: object()
            for lane_id in (
                "chatgpt_codex",
                "claude_code",
                "grok",
                "antigravity",
            )
        },
        capacity_source=live_capacity,
        store_path=tmp_path / "state.db",
        now=lambda: NOW,
    )

    payload = build_inspection_payload(service, command="doctor", lane="chatgpt_codex")

    assert payload["evaluations"][0]["lane_id"] == "chatgpt_codex"
    assert fetched_providers == ["openai-codex"]


def test_live_usage_falls_back_to_bridge_without_upgrading_evidence(tmp_path):
    bridge = tmp_path / "usage-weekly.json"
    bridge.write_text(
        json.dumps(
            {
                "checked_at": "2026-07-25T08:00:00Z",
                "plans": [
                    {
                        "label": "ChatGPT Pro · Codex",
                        "weekly_pct_used": 25,
                        "checked_at": "2026-07-25T08:00:00Z",
                        "health_status": "UP",
                        "health_checked_at": "2026-07-25T08:00:00Z",
                        "comparability_group": "subscription-weekly",
                        "quota_window_id": "2026-W30",
                        "measurement_kind": "measured",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    adapter = LiveUsageAdapter(
        BridgeUsageAdapter(bridge),
        fetch_usage=lambda _provider: None,
    )

    read = adapter.read("chatgpt_codex", now=NOW)

    assert read.snapshot is not None
    assert read.snapshot.source_kind == "bridge_plans"
    assert read.snapshot.used_pct == Decimal("25.000")
    assert read.health is not None
    assert read.health.status is LaneHealth.UP


def test_authoritative_usage_does_not_override_independent_down_health(tmp_path):
    bridge = tmp_path / "usage-weekly.json"
    bridge.write_text(
        json.dumps(
            {
                "checked_at": "2026-07-01T00:00:00Z",
                "plans": [
                    {
                        "label": "ChatGPT Pro · Codex",
                        "weekly_pct_used": 90,
                        "checked_at": "2026-07-01T00:00:00Z",
                        "health_status": "DOWN",
                        "health_checked_at": "2026-07-25T08:00:00Z",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    adapter = LiveUsageAdapter(
        BridgeUsageAdapter(bridge),
        fetch_usage=lambda _provider: SimpleNamespace(
            available=True,
            fetched_at=NOW,
            windows=(SimpleNamespace(label="Weekly", used_percent=10),),
        ),
    )

    read = adapter.read("chatgpt_codex", now=NOW)

    assert read.snapshot is not None
    assert read.snapshot.source_kind == "authoritative_account_usage"
    assert read.health is not None
    assert read.health.status is LaneHealth.DOWN


@pytest.mark.parametrize(
    ("antigravity_remaining", "expected_lane", "switch_applied"),
    [
        ("79.999", "chatgpt_codex", False),
        ("80.000", "antigravity", True),
    ],
)
def test_composition_root_uses_exact_twenty_point_switch_threshold(
    tmp_path, antigravity_remaining, expected_lane, switch_applied
):
    service = _service(
        tmp_path,
        {
            "chatgpt_codex": _read("chatgpt_codex", "60", LaneHealth.UP),
            "claude_code": _read("claude_code", None, LaneHealth.DOWN),
            "grok": _read("grok", None, LaneHealth.DOWN),
            "antigravity": _read(
                "antigravity", antigravity_remaining, LaneHealth.UP
            ),
            "kimi": _read("kimi", None, LaneHealth.UNKNOWN),
        },
    )

    decision = service.plan(TASK)

    assert decision.lane_id == expected_lane
    assert decision.switch_applied is switch_applied


def test_composition_root_all_unknown_creates_no_pin_and_executes_no_adapter(
    tmp_path,
):
    adapter = _Adapter()
    lanes = (
        "chatgpt_codex",
        "claude_code",
        "grok",
        "antigravity",
        "kimi",
    )
    service = _service(
        tmp_path,
        {lane_id: _read(lane_id, None, LaneHealth.UNKNOWN) for lane_id in lanes},
        adapters={lane_id: adapter for lane_id in lanes},
    )

    before = service.store.pin_state_summary()
    result = service.run(TASK, prompt="must remain inert")
    after = service.store.pin_state_summary()

    assert result.reason is ReasonCode.NO_ELIGIBLE_LANE
    assert result.pin is None
    assert adapter.calls == 0
    assert after == before
