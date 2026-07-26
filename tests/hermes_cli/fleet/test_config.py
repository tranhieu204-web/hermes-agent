from __future__ import annotations

from decimal import Decimal

import pytest

from hermes_cli.config import DEFAULT_CONFIG
from hermes_cli.fleet.config import FleetConfigError, parse_fleet_config
from hermes_cli.fleet.profiles import ordered_profiles
from hermes_cli.fleet.types import AdapterKind, Confidence
from hermes_cli.fleet.usage_paths import default_native_usage_path


def test_defaults_are_disabled_conservative_and_documented_in_main_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    config = parse_fleet_config({})

    assert config.enabled is False
    assert config.parent_desktop_enabled is False
    assert config.bridge_usage_file == default_native_usage_path()
    assert config.bridge_usage_file.as_posix().endswith("/fleet/usage-weekly.json")
    assert config.switch_delta_pct == Decimal("20.000")
    assert config.minimum_confidence is Confidence.HIGH
    assert config.rotation_without_fresh_capacity is False
    assert config.lease_ttl_seconds == 1800
    assert config.default_reservation_pct == Decimal("5.000")
    assert config.lanes["chatgpt_codex"].enabled is True
    assert config.lanes["claude_code"].enabled is False
    assert DEFAULT_CONFIG["fleet"]["enabled"] is False
    assert DEFAULT_CONFIG["fleet"]["parent_desktop_enabled"] is False
    assert DEFAULT_CONFIG["fleet"]["rotation_without_fresh_capacity"] is False
    assert DEFAULT_CONFIG["fleet"]["bridge_usage_file"] == ""


def test_deprecated_stale_capacity_flag_is_accepted_but_ignored():
    config = parse_fleet_config(
        {"fleet": {"rotation_without_fresh_capacity": True}}
    )

    assert config.rotation_without_fresh_capacity is False


def test_parent_desktop_admission_has_an_explicit_default_off_gate():
    config = parse_fleet_config(
        {"fleet": {"enabled": True, "parent_desktop_enabled": True}}
    )

    assert config.enabled is True
    assert config.parent_desktop_enabled is True


def test_profiles_are_fixed_order_and_truthful_for_current_live_lanes():
    profiles = ordered_profiles()

    assert [profile.lane_id for profile in profiles] == [
        "chatgpt_codex",
        "claude_code",
        "grok",
        "antigravity",
        "kimi",
    ]
    assert profiles[0].adapter_kind is AdapterKind.NATIVE_PROVIDER
    assert profiles[0].provider_id == "openai-codex"
    assert profiles[0].supported_efforts[-2:] == ("max", "ultra")
    assert profiles[0].selected_effort == "max"
    assert profiles[0].supports_task_worker
    assert profiles[0].supports_parent_session
    assert profiles[1].adapter_kind is AdapterKind.NATIVE_PROVIDER
    assert profiles[1].provider_id == "anthropic"
    assert profiles[1].executable is None
    assert profiles[1].supported_efforts == ("low", "medium", "high", "max")
    assert profiles[1].selected_effort == "high"
    assert profiles[1].supports_task_worker
    assert profiles[1].supports_parent_session
    assert profiles[1].ordered_models == ("claude-opus-4-8",)
    assert "sonnet" not in " ".join(profiles[1].ordered_models).lower()
    assert profiles[2].provider_id == "xai-oauth"
    assert profiles[2].supported_efforts[-2:] == ("max", "ultra")
    assert profiles[2].selected_effort == "max"
    assert profiles[2].supports_parent_session
    antigravity = profiles[3]
    allowed_gemini_models = {
        "gemini-3.1-pro-high",
        "gemini-3.1-pro-low",
        "gemini-3.6-flash-high",
        "gemini-3.6-flash-medium",
        "gemini-3.6-flash-low",
        "gemini-3.5-flash-high",
        "gemini-3.5-flash-medium",
        "gemini-3.5-flash-low",
    }
    assert antigravity.lane_id == "antigravity"
    assert antigravity.provider_id == "antigravity-subscription"
    assert antigravity.adapter_kind is AdapterKind.EXTERNAL_CLI
    assert antigravity.implemented
    assert antigravity.executable == "agy"
    assert antigravity.ordered_models
    assert antigravity.ordered_models[0] == "gemini-3.1-pro-high"
    assert set(antigravity.ordered_models) <= allowed_gemini_models
    assert antigravity.supported_efforts == ("low", "medium", "high")
    assert antigravity.selected_effort == "medium"
    assert antigravity.allowed_auth_kinds == frozenset({"cli_subscription"})
    assert antigravity.supports_task_worker
    assert antigravity.supports_parent_session
    assert not profiles[4].implemented
    assert not profiles[4].supports_task_worker
    assert not profiles[4].supports_parent_session


@pytest.mark.parametrize(
    ("fleet", "message"),
    [
        ({"switch_delta_pct": 19.999}, "switch_delta_pct"),
        ({"switch_delta_pct": 20.001}, "switch_delta_pct"),
        ({"minimum_confidence": "low"}, "minimum_confidence"),
        ({"lease_ttl_seconds": 0}, "lease_ttl_seconds"),
        ({"lease_ttl_seconds": True}, "lease_ttl_seconds"),
        ({"default_reservation_pct": -1}, "default_reservation_pct"),
        (
            {"lanes": {"chatgpt_codex": {"max_concurrency": 0}}},
            "max_concurrency",
        ),
        (
            {"lanes": {"chatgpt_codex": {"reserve_floor_pct": 101}}},
            "reserve_floor_pct",
        ),
        ({"lanes": {"unknown": {"enabled": True}}}, "unknown lane"),
        (
            {"lanes": {"chatgpt_codex": {"unexpected": True}}},
            "unknown lane option",
        ),
        ({"lanes": {"kimi": {"enabled": True}}}, "deferred"),
        ({"api_key": "secret"}, "credential"),
        (
            {"lanes": {"claude_code": {"auth_token": "secret"}}},
            "credential",
        ),
    ],
)
def test_invalid_or_billing_sensitive_config_fails_closed(fleet, message):
    with pytest.raises(FleetConfigError, match=message):
        parse_fleet_config({"fleet": fleet})
