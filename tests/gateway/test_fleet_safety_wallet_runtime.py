from __future__ import annotations

import pytest

from gateway.fleet_safety.usage_verify import VerifiedUsage
from gateway.fleet_safety.wallet_cap import WalletAction
from gateway.fleet_safety import wallet_runtime as runtime


def _usage(percent: float | None, *, source: str = "authoritative", stale: bool = False):
    return VerifiedUsage(
        provider="p", used_percent=percent, source=source, stale=stale,
        suspect=False, authoritative_percent=percent if source == "authoritative" else None,
        cached_percent=None, reasons=[],
    )


def _cfg(monkeypatch, **wallet):
    raw = {
        "enabled": True, "soft_percent": 80, "hard_percent": 90,
        "on_unknown": "fallback", "downgrade_floor": "medium",
        "heavy_providers": [], **wallet,
    }
    monkeypatch.setattr(runtime, "_config", lambda: ({"fleet_safety": {"wallet_cap": raw}}, raw))


def test_operator_hard_cap_preserves_route_and_downgrades(monkeypatch):
    _cfg(monkeypatch)
    plan = runtime.wallet_preflight(
        runtime.WalletCallDescriptor("p", "m", "high", True, runtime.CallOrigin.OPERATOR_EXPLICIT),
        verified_usage=_usage(100), now=1,
    )
    assert (plan.provider, plan.model) == ("p", "m")
    assert plan.action is WalletAction.DOWNGRADE_EFFORT
    assert plan.reasoning_override["effort"] == "medium"
    with pytest.raises(TypeError):
        plan.reasoning_override["effort"] = "high"


def test_automatic_unknown_blocks_before_transport_without_fallback(monkeypatch):
    _cfg(monkeypatch)
    with pytest.raises(runtime.WalletCapBlocked):
        runtime.wallet_preflight(
            runtime.WalletCallDescriptor("p", "m", "high", True, runtime.CallOrigin.AUTOMATIC_CHILD),
            verified_usage=_usage(None, source="none", stale=True), now=1,
        )


def test_fresh_authoritative_divergence_remains_policy_usable(monkeypatch):
    _cfg(monkeypatch)
    usage = _usage(95)
    object.__setattr__(usage, "suspect", True)
    plan = runtime.wallet_preflight(
        runtime.WalletCallDescriptor("p", "m", "high", True, runtime.CallOrigin.OPERATOR_EXPLICIT),
        verified_usage=usage, now=1,
    )
    assert plan.warning["used_percent"] == 95
    assert plan.action is WalletAction.DOWNGRADE_EFFORT


def test_non_heavy_call_skips_usage_fetch(monkeypatch):
    _cfg(monkeypatch)
    monkeypatch.setattr(runtime, "verified_usage_for", lambda *a, **k: pytest.fail("usage fetch"))
    plan = runtime.wallet_preflight(runtime.WalletCallDescriptor("p", "m"), now=1)
    assert plan.action is WalletAction.ALLOW


def test_legacy_threshold_override_survives_merged_new_defaults():
    from gateway.fleet_safety.wallet_cap import WalletCapConfig

    cfg = WalletCapConfig.from_config({
        "cap_percent": 87,
        "hard_percent": 90,
        "downgrade_percent": 77,
        "soft_percent": 80,
    })
    assert cfg.cap_percent == 87
    assert cfg.downgrade_percent == 77
