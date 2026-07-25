from gateway.fleet_safety import integration
from gateway.fleet_safety.usage_verify import VerifiedUsage
from gateway.fleet_safety.wallet_cap import WalletAction


def test_wallet_fallback_decision_is_fail_closed_without_duplicate_router(
    monkeypatch,
):
    monkeypatch.setattr(
        integration,
        "_load_fleet_safety_config",
        lambda: {
            "wallet_cap": {
                "enabled": True,
                "provider_caps": {"openai-codex": 80},
                "on_cap": "fallback_provider",
            }
        },
    )
    monkeypatch.setattr(
        integration,
        "verified_usage_for",
        lambda *_args, **_kwargs: VerifiedUsage(
            provider="openai-codex",
            used_percent=95,
            source="authoritative",
            stale=False,
            suspect=False,
        ),
    )
    monkeypatch.setattr(
        integration,
        "select_best_lane",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("wallet API must not invoke the duplicate router")
        ),
    )

    decision = integration.decide_routing(
        "openai-codex",
        is_heavy=True,
        now=0,
    )

    assert decision.action is WalletAction.FALLBACK_PROVIDER
    assert decision.fallback_provider is None
    assert decision.fallback_model is None


def test_legacy_selector_helper_receives_full_config_projection(monkeypatch):
    full_config = {
        "fleet": {"switch_delta_pct": 20.0},
        "agent": {"reasoning_effort": {"chatgpt_codex": "max"}},
        "fleet_safety": {"wallet_cap": {"enabled": False}},
    }
    captured = {}
    expected = object()
    monkeypatch.setattr(integration, "_load_full_config", lambda: full_config)

    def select(config, **_kwargs):
        captured["config"] = config
        return expected

    monkeypatch.setattr(integration, "select_best_lane", select)

    assert integration.select_best_lane_for() is expected
    assert captured["config"] is full_config
