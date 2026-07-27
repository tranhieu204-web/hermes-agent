"""Request-time wallet-cap plans shared by every production send path."""
from __future__ import annotations

import enum
import logging
import time
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Optional, Tuple

from gateway.fleet_safety.selector import rank_fallback_chain
from gateway.fleet_safety.usage_verify import VerifiedUsage, verified_usage_for
from gateway.fleet_safety.wallet_cap import RoutingRequest, WalletAction, WalletCap, WalletCapConfig

logger = logging.getLogger(__name__)


class CallOrigin(str, enum.Enum):
    OPERATOR_EXPLICIT = "operator_explicit"
    AUTOMATIC_BACKGROUND = "automatic_background"
    AUTOMATIC_CHILD = "automatic_child"
    UNKNOWN = "unknown"


class WalletCapBlocked(RuntimeError):
    """Raised before transport when automatic heavy work has no safe route."""


@dataclass(frozen=True)
class WalletCallDescriptor:
    provider: str
    model: str
    effort: str = "medium"
    is_heavy: bool = False
    origin: CallOrigin = CallOrigin.UNKNOWN
    session_id: str = ""
    turn_id: str = ""
    request_id: str = ""
    fallback_entries: Tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class WalletCallPlan:
    provider: str
    model: str
    effort: str
    action: WalletAction
    reason: str
    usage_fingerprint: Tuple[Any, ...]
    reasoning_override: Mapping[str, Any]
    warning: Mapping[str, Any]
    selected_fallback: Optional[Mapping[str, Any]] = None


def _config() -> tuple[dict, dict]:
    from hermes_cli.config import load_config
    full = load_config() or {}
    fleet = full.get("fleet_safety") or {}
    return full, fleet.get("wallet_cap") or {}


def _fingerprint(usage: VerifiedUsage) -> Tuple[Any, ...]:
    return (
        usage.provider, usage.source, usage.used_percent, usage.stale,
        usage.suspect, usage.authoritative_percent, usage.cached_percent,
        tuple(usage.reasons),
    )


def wallet_preflight(
    descriptor: WalletCallDescriptor,
    *,
    now: Optional[float] = None,
    verified_usage: Optional[VerifiedUsage] = None,
) -> WalletCallPlan:
    now = time.time() if now is None else now
    full, raw = _config()
    cfg = WalletCapConfig.from_config(raw)
    heavy_providers = {
        str(value).strip().lower() for value in raw.get("heavy_providers", [])
        if isinstance(value, str) and value.strip()
    }
    heavy = descriptor.is_heavy or descriptor.provider.strip().lower() in heavy_providers
    empty = MappingProxyType({})
    if not cfg.enabled or not heavy:
        return WalletCallPlan(descriptor.provider, descriptor.model, descriptor.effort,
                              WalletAction.ALLOW, "wallet cap not applicable", (), empty, empty)
    usage = verified_usage or verified_usage_for(
        descriptor.provider,
        now=now,
        max_age_seconds=float(raw.get("max_verified_age_seconds", 900) or 900),
        divergence_points=float(raw.get("divergence_points", 15) or 15),
    )
    usable = usage.used_percent if usage.source == "authoritative" and not usage.stale else None
    decision = WalletCap(cfg).decide(
        RoutingRequest(descriptor.provider, descriptor.effort, True), usable
    )
    selected = None
    action, effort = decision.action, decision.effort
    operator = descriptor.origin in {CallOrigin.OPERATOR_EXPLICIT, CallOrigin.UNKNOWN}
    if action is WalletAction.FALLBACK_PROVIDER:
        if operator:
            action = WalletAction.DOWNGRADE_EFFORT
            effort = WalletCap(cfg).decide(
                RoutingRequest(descriptor.provider, descriptor.effort, True),
                cfg.downgrade_percent,
            ).effort
        else:
            ranked = rank_fallback_chain(
                [dict(entry) for entry in descriptor.fallback_entries], full,
                is_heavy=True, now=now,
            )
            ranked = [entry for entry in ranked if str(entry.get("provider", "")).lower() != descriptor.provider.lower() or str(entry.get("model", "")) != descriptor.model]
            if not ranked:
                raise WalletCapBlocked(
                    f"wallet cap blocks automatic call: {descriptor.provider}/{descriptor.model}; no eligible fallback"
                )
            selected = MappingProxyType(dict(ranked[0]))
    provider = str(selected.get("provider") or descriptor.provider) if selected else descriptor.provider
    model = str(selected.get("model") or descriptor.model) if selected else descriptor.model
    override = MappingProxyType({"effort": effort}) if effort != descriptor.effort else empty
    warning = MappingProxyType({
        "origin": descriptor.origin.value,
        "provider": descriptor.provider,
        "model": descriptor.model,
        "action": action.value,
        "reason": decision.reason,
        "used_percent": usage.used_percent,
        "usage_source": usage.source,
    })
    if action is not WalletAction.ALLOW or descriptor.origin is CallOrigin.UNKNOWN:
        logger.warning("wallet preflight: %s", dict(warning))
    return WalletCallPlan(provider, model, effort, action, decision.reason,
                          _fingerprint(usage), override, warning, selected)


def wallet_revalidate(plan: WalletCallPlan, descriptor: WalletCallDescriptor, *, now: Optional[float] = None) -> WalletCallPlan:
    """Re-run policy immediately before send; caller rebuilds if plan changed."""
    return wallet_preflight(descriptor, now=now)
