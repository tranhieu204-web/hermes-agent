"""Unit tests for the usage-headroom fallback selector and effort mapping."""

import pytest
from gateway.fleet_safety import selector
from gateway.fleet_safety.selector import (
    select_best_lane,
    rank_fallback_chain,
    resolve_effort_from_map,
    get_lane_name,
    SelectedLane,
)
from gateway.fleet_safety.usage_verify import VerifiedUsage


def _mock_usage(used_percent=10.0, stale=False, suspect=False, reasons=None):
    return VerifiedUsage(
        provider="test",
        used_percent=used_percent,
        source="authoritative",
        stale=stale,
        suspect=suspect,
        reasons=reasons or [],
    )


def test_select_best_lane_ranking_by_headroom(monkeypatch):
    def fake_verify(provider, **kwargs):
        if "codex" in provider or "openai" in provider:
            return _mock_usage(used_percent=30.0)  # 70% headroom
        elif "anthropic" in provider or "claude" in provider:
            return _mock_usage(used_percent=15.0)  # 85% headroom
        elif "xai" in provider or "grok" in provider:
            return _mock_usage(used_percent=60.0)  # 40% headroom
        elif "antigravity" in provider or "gemini" in provider:
            return _mock_usage(used_percent=50.0)  # 50% headroom
        return _mock_usage(used_percent=50.0)

    monkeypatch.setattr(selector, "verified_usage_for", fake_verify)

    selected = select_best_lane(config={"fleet": {"switch_delta": 0.0}})
    assert selected.lane == "claude_code"
    assert selected.remaining_headroom == pytest.approx(85.0)
    assert not selected.is_fallback


@pytest.mark.parametrize(
    ("antigravity_headroom", "expected_lane"),
    [
        (79.999, "chatgpt_codex"),
        (80.0, "antigravity"),
    ],
)
def test_select_best_lane_preserves_exact_twenty_point_band(
    monkeypatch, antigravity_headroom, expected_lane
):
    def fake_verify(provider, **_kwargs):
        used = {
            "openai-codex": 40.0,
            "antigravity": 100.0 - antigravity_headroom,
        }.get(provider)
        return _mock_usage(used_percent=used)

    monkeypatch.setattr(selector, "verified_usage_for", fake_verify)
    selected = select_best_lane(
        config={
            "fleet": {
                "switch_delta_pct": 20.0,
                "lanes": {
                    "chatgpt_codex": {"enabled": True},
                    "claude_code": {"enabled": False},
                    "grok": {"enabled": False},
                    "antigravity": {"enabled": True},
                },
            }
        },
        current_provider="openai-codex",
    )

    assert selected.lane == expected_lane


def test_select_best_lane_all_lanes_below_floor_edge_case(monkeypatch):
    def fake_verify(provider, **kwargs):
        # All below their respective floors (floors: codex 8%, claude 2%, grok 5%, antigravity 5%)
        if "codex" in provider or "openai" in provider:
            return _mock_usage(used_percent=95.0)  # 5% headroom (< 8%)
        elif "anthropic" in provider or "claude" in provider:
            return _mock_usage(used_percent=99.0)  # 1% headroom (< 2%)
        elif "xai" in provider or "grok" in provider:
            return _mock_usage(used_percent=97.0)  # 3% headroom (< 5%)
        elif "antigravity" in provider or "gemini" in provider:
            return _mock_usage(used_percent=96.0)  # 4% headroom (< 5%)
        return _mock_usage(used_percent=98.0)

    monkeypatch.setattr(selector, "verified_usage_for", fake_verify)

    selected = select_best_lane(config={"fleet": {"switch_delta": 0.0}})
    # Requirement 3: Must fail closed when no eligible verified lane exists
    assert selected.lane == ""
    assert selected.provider == ""
    assert selected.is_fallback
    assert "no_eligible_lane" in selected.reason


def test_unverified_or_stale_attestation_treated_as_unknown(monkeypatch):
    def fake_verify(provider, **kwargs):
        if "codex" in provider or "openai" in provider:
            return _mock_usage(used_percent=80.0)  # 20% headroom (verified)
        elif "anthropic" in provider or "claude" in provider:
            return _mock_usage(used_percent=10.0, stale=True)  # stale attestation -> unknown
        elif "xai" in provider or "grok" in provider:
            return _mock_usage(used_percent=None)  # unverified -> unknown
        return _mock_usage(used_percent=None)

    monkeypatch.setattr(selector, "verified_usage_for", fake_verify)

    selected = select_best_lane(config={"fleet": {"switch_delta": 0.0}}, is_heavy=True)
    assert selected.lane == "chatgpt_codex"
    assert selected.remaining_headroom == pytest.approx(20.0)
    assert not selected.is_fallback


def test_resolve_effort_from_map_and_bounded_ladders():
    effort_map = {
        "gpt-5.6-sol": "max",
        "chatgpt_codex": "xhigh",
        "claude_code": "high",
        "grok": "xhigh",       # grok ladder max is high
        "antigravity": "max",  # antigravity ladder max is high
    }

    # 1. Exact model match (codex ladder supports max)
    assert resolve_effort_from_map(effort_map, model="gpt-5.6-sol") == "max"
    # 2. Canonical lane match (claude ladder supports high)
    assert resolve_effort_from_map(effort_map, model="claude-sonnet-4-6") == "high"
    # 3. Provider match bounded by Grok ladder (xhigh clamped to high)
    assert resolve_effort_from_map(effort_map, provider="xai-oauth") == "high"
    # 4. Provider match bounded by Antigravity ladder (max clamped to high)
    assert resolve_effort_from_map(effort_map, provider="antigravity") == "high"
    # 5. Default lane fallback when map is empty/invalid
    assert resolve_effort_from_map(None, provider="openai-codex") == "xhigh"
    assert resolve_effort_from_map(None, provider="xai-oauth") == "high"


def test_no_invented_claude_model_ids(monkeypatch):
    # Model IDs must come from validated config/registry, not invented strings like claude-fable-5
    def fake_verify(provider, **kwargs):
        if "anthropic" in provider or "claude" in provider:
            return _mock_usage(used_percent=40.0)  # 60% headroom
        return _mock_usage(used_percent=90.0)

    monkeypatch.setattr(selector, "verified_usage_for", fake_verify)
    selected = select_best_lane(config={"fleet": {"switch_delta": 0.0}})
    assert selected.lane == "claude_code"
    assert selected.model == "claude-sonnet-4-6"  # Real configured top model, not invented string


def test_disabled_lane_never_selected_or_in_fallback(monkeypatch):
    def fake_verify(provider, **kwargs):
        return _mock_usage(used_percent=10.0)  # 90% headroom for all

    monkeypatch.setattr(selector, "verified_usage_for", fake_verify)
    cfg = {
        "fleet": {
            "switch_delta": 0.0,
            "lanes": {
                "claude_code": {"enabled": False},
                "chatgpt_codex": {"enabled": False},
                "grok": {"enabled": False},
                "antigravity": {"enabled": False},
            },
        }
    }
    # When all lanes disabled in config, must fail closed with no_eligible_lane
    selected = select_best_lane(config=cfg)
    assert selected.lane == ""
    assert selected.is_fallback
    assert "no_eligible_lane" in selected.reason

    chain = [
        {"provider": "openai-codex", "model": "gpt-5.6-sol"},
        {"provider": "anthropic", "model": "claude-sonnet-4-6"},
    ]
    ranked = rank_fallback_chain(chain, config=cfg)
    assert ranked == []


def test_graceful_degrade_provider_down(monkeypatch):
    # When primary provider is walled (100% used), exclude below-floor entries and rank remaining
    def fake_verify(provider, **kwargs):
        if "codex" in provider or "openai" in provider:
            return _mock_usage(used_percent=100.0)  # walled / exhausted (< 8% floor)
        elif "anthropic" in provider or "claude" in provider:
            return _mock_usage(used_percent=60.0)  # available (40% hr >= 2% floor)
        elif "xai" in provider or "grok" in provider:
            return _mock_usage(used_percent=50.0)  # available (50% hr >= 5% floor)
        return _mock_usage(used_percent=50.0)

    monkeypatch.setattr(selector, "verified_usage_for", fake_verify)

    chain = [
        {"provider": "openai-codex", "model": "gpt-5.6-sol"},
        {"provider": "anthropic", "model": "claude-sonnet-4-6"},
        {"provider": "xai-oauth", "model": "grok-4.5"},
    ]
    ranked = rank_fallback_chain(chain, config={"fleet": {"switch_delta": 0.0}})

    # Only eligible entries (grok and anthropic) returned; codex (0% hr < floor) excluded
    assert len(ranked) == 2
    assert ranked[0]["provider"] == "xai-oauth"
    assert ranked[1]["provider"] == "anthropic"


def test_e2e_temp_hermes_home_integration_path(tmp_path, monkeypatch):
    """E2E-style test reading config from HERMES_HOME and proving selection path."""
    import yaml
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir(parents=True)
    cfg_file = hermes_home / "config.yaml"
    cfg_data = {
        "fleet": {
            "switch_delta": 20.0,
            "lanes": {
                "chatgpt_codex": {"enabled": True, "reserve_floor_pct": 10.0},
                "claude_code": {"enabled": True, "reserve_floor_pct": 5.0},
            },
        },
        "agent": {
            "reasoning_effort": {
                "chatgpt_codex": "xhigh",
                "claude_code": "high",
                "grok": "xhigh",  # Should bound to high for Grok
            }
        }
    }
    cfg_file.write_text(yaml.dump(cfg_data), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    def fake_verify(provider, **kwargs):
        if "anthropic" in provider or "claude" in provider:
            return _mock_usage(used_percent=20.0)  # 80% headroom
        elif "codex" in provider or "openai" in provider:
            return _mock_usage(used_percent=50.0)  # 50% headroom
        return _mock_usage(used_percent=90.0)

    monkeypatch.setattr(selector, "verified_usage_for", fake_verify)

    from hermes_cli.config import load_config
    loaded_cfg = load_config()
    selected = select_best_lane(config=loaded_cfg)
    assert selected.lane == "claude_code"
    assert selected.remaining_headroom == 80.0
    assert selected.effort == "high"
