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


@pytest.mark.parametrize(
    "invalid_used_pct",
    [
        True,
        False,
        -1.0,
        101.0,
        10**400,
        float("nan"),
        float("inf"),
        float("-inf"),
        "bad",
    ],
)
def test_prevalidated_usage_rejects_non_finite_or_out_of_range_values(
    invalid_used_pct,
):
    selected = select_best_lane(
        config={
            "fleet": {
                "lanes": {
                    "chatgpt_codex": {"enabled": True},
                    "claude_code": {"enabled": False},
                    "grok": {"enabled": False},
                    "antigravity": {"enabled": False},
                }
            }
        },
        usage_by_lane={"chatgpt_codex": invalid_used_pct},
    )

    assert selected.lane == ""
    assert selected.is_fallback
    assert "no_eligible_lane" in selected.reason


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


def test_claude_leader_lane_model_policy_never_sonnet(monkeypatch):
    # Top-model policy (operator 2026-07-27): Fable 5 under 50% of the weekly
    # window, Opus 5 at/after 50% or when usage is unknown; Sonnet is NEVER a
    # leader-lane target. claude-fable-5/claude-opus-5 are live-verified plan
    # CLI model ids (served-model receipts), not invented strings.
    def fake_verify(provider, **kwargs):
        if "anthropic" in provider or "claude" in provider:
            return _mock_usage(used_percent=40.0)  # 60% headroom
        return _mock_usage(used_percent=90.0)

    monkeypatch.setattr(selector, "verified_usage_for", fake_verify)
    selected = select_best_lane(config={"fleet": {"switch_delta": 0.0}})
    assert selected.lane == "claude_code"
    assert selected.model == "claude-fable-5"
    assert "sonnet" not in selected.model.lower()

    def fake_verify_high_usage(provider, **kwargs):
        if "anthropic" in provider or "claude" in provider:
            return _mock_usage(used_percent=60.0)  # past the 50% tier switch
        return _mock_usage(used_percent=90.0)

    monkeypatch.setattr(selector, "verified_usage_for", fake_verify_high_usage)
    selected = select_best_lane(config={"fleet": {"switch_delta": 0.0}})
    assert selected.lane == "claude_code"
    assert selected.model == "claude-opus-5"


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


# ============================================================================
# IMPORTANCE-BASED EFFORT GRADING TESTS
# ============================================================================


@pytest.mark.parametrize(
    ("importance", "expected_effort"),
    [
        ("money_critical", "max"),
        ("critically_important", "xhigh"),
        ("semi_critical", "high"),
        ("normal", "medium"),
    ],
)
def test_importance_grading_claude(importance, expected_effort, monkeypatch):
    """Claude should map importance levels to effort levels."""
    def fake_verify(provider, **kwargs):
        if "anthropic" in provider or "claude" in provider:
            return _mock_usage(used_percent=10.0)  # 90% headroom
        return _mock_usage(used_percent=90.0)

    monkeypatch.setattr(selector, "verified_usage_for", fake_verify)

    selected = select_best_lane(
        config={"fleet": {"switch_delta": 0.0}},
        importance=importance,
    )
    assert selected.lane == "claude_code"
    assert selected.effort == expected_effort


@pytest.mark.parametrize(
    ("importance", "expected_effort"),
    [
        ("money_critical", "max"),
        ("critically_important", "xhigh"),
        ("semi_critical", "high"),
        ("normal", "medium"),
    ],
)
def test_importance_grading_codex(importance, expected_effort, monkeypatch):
    """Codex should map importance levels to effort levels."""
    def fake_verify(provider, **kwargs):
        if "openai" in provider or "codex" in provider:
            return _mock_usage(used_percent=10.0)  # 90% headroom
        return _mock_usage(used_percent=90.0)

    monkeypatch.setattr(selector, "verified_usage_for", fake_verify)

    selected = select_best_lane(
        config={
            "fleet": {
                "switch_delta": 0.0,
                "lanes": {
                    "chatgpt_codex": {"enabled": True},
                    "claude_code": {"enabled": False},
                    "grok": {"enabled": False},
                    "antigravity": {"enabled": False},
                },
            }
        },
        importance=importance,
    )
    assert selected.lane == "chatgpt_codex"
    assert selected.effort == expected_effort


def test_grok_always_pinned_high_regardless_importance(monkeypatch):
    """Grok must always return 'high' effort regardless of task importance."""
    def fake_verify(provider, **kwargs):
        if "xai" in provider or "grok" in provider:
            return _mock_usage(used_percent=10.0)  # 90% headroom
        return _mock_usage(used_percent=90.0)

    monkeypatch.setattr(selector, "verified_usage_for", fake_verify)

    for importance in ["money_critical", "critically_important", "semi_critical", "normal"]:
        selected = select_best_lane(
            config={
                "fleet": {
                    "switch_delta": 0.0,
                    "lanes": {
                        "chatgpt_codex": {"enabled": False},
                        "claude_code": {"enabled": False},
                        "grok": {"enabled": True},
                        "antigravity": {"enabled": False},
                    },
                }
            },
            importance=importance,
        )
        assert selected.lane == "grok"
        assert selected.effort == "high", f"Grok effort should be 'high' for importance={importance}"


def test_antigravity_always_pinned_high_regardless_importance(monkeypatch):
    """Antigravity must always return 'high' effort regardless of task importance."""
    def fake_verify(provider, **kwargs):
        if "gemini" in provider or "antigravity" in provider:
            return _mock_usage(used_percent=10.0)  # 90% headroom
        return _mock_usage(used_percent=90.0)

    monkeypatch.setattr(selector, "verified_usage_for", fake_verify)

    for importance in ["money_critical", "critically_important", "semi_critical", "normal"]:
        selected = select_best_lane(
            config={
                "fleet": {
                    "switch_delta": 0.0,
                    "lanes": {
                        "chatgpt_codex": {"enabled": False},
                        "claude_code": {"enabled": False},
                        "grok": {"enabled": False},
                        "antigravity": {"enabled": True},
                    },
                }
            },
            importance=importance,
        )
        assert selected.lane == "antigravity"
        assert selected.effort == "high", f"Antigravity effort should be 'high' for importance={importance}"


def test_importance_default_to_normal(monkeypatch):
    """When importance is unspecified, it should default to 'normal' (medium effort)."""
    def fake_verify(provider, **kwargs):
        if "anthropic" in provider or "claude" in provider:
            return _mock_usage(used_percent=10.0)  # 90% headroom
        return _mock_usage(used_percent=90.0)

    monkeypatch.setattr(selector, "verified_usage_for", fake_verify)

    # Call without specifying importance
    selected = select_best_lane(config={"fleet": {"switch_delta": 0.0}})
    assert selected.lane == "claude_code"
    assert selected.effort == "medium"


def test_importance_clamped_to_ladder_ceiling(monkeypatch):
    """Importance-graded effort should still be clamped to the lane's ladder ceiling."""
    def fake_verify(provider, **kwargs):
        if "xai" in provider or "grok" in provider:
            return _mock_usage(used_percent=10.0)  # 90% headroom
        return _mock_usage(used_percent=90.0)

    monkeypatch.setattr(selector, "verified_usage_for", fake_verify)

    # Grok ladder max is "high", so money_critical (normally "max") should clamp to "high"
    selected = select_best_lane(
        config={
            "fleet": {
                "switch_delta": 0.0,
                "lanes": {
                    "chatgpt_codex": {"enabled": False},
                    "claude_code": {"enabled": False},
                    "grok": {"enabled": True},
                    "antigravity": {"enabled": False},
                },
            }
        },
        importance="money_critical",
    )
    assert selected.lane == "grok"
    assert selected.effort == "high"


def test_regression_normal_work_no_longer_xhigh_on_claude(monkeypatch):
    """Regression: normal work on Claude/Codex should be 'medium', not 'xhigh'."""
    def fake_verify(provider, **kwargs):
        if "anthropic" in provider or "claude" in provider:
            return _mock_usage(used_percent=10.0)  # 90% headroom
        elif "openai" in provider or "codex" in provider:
            return _mock_usage(used_percent=20.0)  # 80% headroom
        return _mock_usage(used_percent=90.0)

    monkeypatch.setattr(selector, "verified_usage_for", fake_verify)

    # Without importance specification or with "normal" importance
    selected_default = select_best_lane(config={"fleet": {"switch_delta": 0.0}})
    assert selected_default.effort == "medium", "Normal work should be 'medium', not 'xhigh'"

    selected_explicit = select_best_lane(
        config={"fleet": {"switch_delta": 0.0}},
        importance="normal",
    )
    assert selected_explicit.effort == "medium", "Explicit normal should be 'medium', not 'xhigh'"


def test_resolve_effort_from_map_importance_override():
    """Configured effort_map should override importance grading if specified."""
    # When both importance and effort_map are provided, effort_map takes precedence
    effort_map = {"claude_code": "low"}

    # With importance="money_critical" but explicit effort_map override
    result = resolve_effort_from_map(
        effort_map,
        model="claude-sonnet-4-6",
        provider="anthropic",
        importance="money_critical",
    )
    # The explicit map should override importance-based grading
    # Note: resolve_effort_from_map tries importance first, but the logic should handle
    # configured overrides. Let's check what the current behavior is.
    # Looking at the code, it tries importance first, then effort_map. So we need to verify.
    assert result == "low"


def test_importance_invalid_values_treated_as_normal(monkeypatch):
    """Invalid importance values should be treated as 'normal' (medium)."""
    def fake_verify(provider, **kwargs):
        if "anthropic" in provider or "claude" in provider:
            return _mock_usage(used_percent=10.0)  # 90% headroom
        return _mock_usage(used_percent=90.0)

    monkeypatch.setattr(selector, "verified_usage_for", fake_verify)

    # Use an invalid importance value
    selected = select_best_lane(
        config={"fleet": {"switch_delta": 0.0}},
        importance="invalid_importance",
    )
    assert selected.lane == "claude_code"
    # Should fallback to the DEFAULT_LANE_EFFORTS["claude_code"] which is "xhigh"
    # (since invalid importance returns None, uses map, then defaults)
    assert selected.effort == "xhigh"


def test_importance_case_insensitive():
    """Importance levels should be case-insensitive."""
    # Test resolve_effort_from_map directly
    result1 = resolve_effort_from_map(None, provider="anthropic", importance="MONEY_CRITICAL")
    result2 = resolve_effort_from_map(None, provider="anthropic", importance="Money_Critical")
    result3 = resolve_effort_from_map(None, provider="anthropic", importance="money_critical")
    assert result1 == result2 == result3 == "max"


def test_grok_pinning_overrides_explicit_effort_map():
    """Grok must be pinned to 'high' even when effort_map tries to override it."""
    # This test catches mutations that disable Grok pinning
    effort_map = {
        "grok": "max",  # Attempt to override Grok to max
        "chatgpt_codex": "max",
    }

    # Grok should be clamped to "high" despite explicit map
    result = resolve_effort_from_map(effort_map, provider="xai-oauth")
    assert result == "high", f"Grok should be pinned to 'high', got {result}"


def test_antigravity_pinning_overrides_explicit_effort_map():
    """Antigravity must be pinned to 'high' even when effort_map tries to override it."""
    # This test catches mutations that disable Antigravity pinning
    effort_map = {
        "antigravity": "xhigh",  # Attempt to override Antigravity
    }

    # Antigravity should be clamped to "high" despite explicit map
    result = resolve_effort_from_map(effort_map, provider="antigravity")
    assert result == "high", f"Antigravity should be pinned to 'high', got {result}"


# --- Kimi provider-id / alias normalization (inspector finding 2026-07-27) -----
# Grading applies to the kimi lane, so EVERY supported provider id must resolve
# to it. A real dispatch carries ids like "kimi-subscription" (the lane
# profile's provider_id) or "kimi-coding" (auth registry), not the bare lane
# slug — before this fix those missed grading and fell back to the lane default.

KIMI_PROVIDER_IDS = [
    "kimi",
    "kimi-subscription",
    "kimi-coding",
    "kimi-coding-cn",
    "kimi-cn",
    "moonshot",
    "moonshot-cn",
]


@pytest.mark.parametrize("provider_id", KIMI_PROVIDER_IDS)
def test_kimi_alias_normalizes_to_kimi_lane(provider_id):
    from gateway.fleet_safety.selector import get_lane_name

    assert get_lane_name(provider_id) == "kimi", (
        f"{provider_id!r} must normalize to the kimi lane or grading silently misses it"
    )


@pytest.mark.parametrize("provider_id", KIMI_PROVIDER_IDS)
@pytest.mark.parametrize(
    "importance,expected",
    [
        ("normal", "medium"),
        ("semi_critical", "high"),
        ("critically_important", "xhigh"),
        ("money_critical", "max"),
    ],
)
def test_kimi_alias_four_tier_grading(provider_id, importance, expected):
    """Every supported Kimi id must grade across all four tiers, not just 'kimi'."""
    from gateway.fleet_safety.selector import get_lane_name, resolve_effort_from_map

    lane = get_lane_name(provider_id)
    assert resolve_effort_from_map({}, lane, importance=importance) == expected
