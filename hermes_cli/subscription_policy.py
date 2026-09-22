"""Fail-closed subscription routing and per-physical-request permits.

The policy validates caller intent before any credential, catalog, client, or
network capability is touched.  Production permit issuance deliberately stays
closed until an authoritative same-account subscription charge receipt exists.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional


_ROUTES = {
    "openai-codex": ("codex_responses", "https://chatgpt.com/backend-api/codex"),
    "xai-oauth": ("codex_responses", "https://api.x.ai/v1"),
}


class SubscriptionOnlyError(RuntimeError):
    """The requested operation is not proven subscription-only."""


def _normalized(value: Any) -> str:
    return str(value or "").strip().lower()


def _config(config: Optional[Mapping[str, Any]]) -> Mapping[str, Any]:
    if config is not None:
        if not isinstance(config, Mapping):
            raise SubscriptionOnlyError("SUBSCRIPTION_ONLY_ROUTE: configuration has an invalid shape")
        return config
    try:
        from hermes_cli.config import get_active_config_parse_failure, load_config_readonly

        loaded = load_config_readonly()
        if get_active_config_parse_failure() is not None or not isinstance(loaded, Mapping):
            raise ValueError("invalid configuration")
        return loaded
    except SubscriptionOnlyError:
        raise
    except Exception as exc:
        raise SubscriptionOnlyError(
            "SUBSCRIPTION_ONLY_ROUTE: configuration policy is unavailable"
        ) from exc


def subscription_only_enabled(config: Optional[Mapping[str, Any]] = None) -> bool:
    return _config(config).get("subscription_only") is True


@dataclass(frozen=True)
class RoutePermit:
    provider: str
    endpoint: str
    api_mode: str
    model: str
    auth_generation: str
    client_identity: int
    expires_at: float
    source: str


@dataclass(frozen=True)
class RouteDecision:
    provider: str
    endpoint: str
    api_mode: str
    model: str
    enabled: bool

    def require_live_permit(self) -> RoutePermit:
        if not self.enabled:
            raise SubscriptionOnlyError("SUBSCRIPTION_ONLY_ROUTE: policy is not enabled")
        code = "AUTHORITATIVE_XAI_CHARGE_SOURCE_UNAVAILABLE" if self.provider == "xai-oauth" else (
            "AUTHORITATIVE_OPENAI_CHARGE_SOURCE_UNAVAILABLE"
        )
        raise SubscriptionOnlyError(f"NOT_VERIFIED: {code}")


def validate_route_intent(
    provider: Any,
    *,
    model: Any,
    api_key: Any = None,
    base_url: Any = None,
    api_mode: Any = None,
    config: Optional[Mapping[str, Any]] = None,
    before_resolution: Optional[Callable[[], None]] = None,
) -> RouteDecision:
    """Validate literal route intent without resolving aliases or credentials."""
    cfg = _config(config)
    enabled = cfg.get("subscription_only") is True
    normalized_provider = _normalized(provider)
    if not enabled:
        if before_resolution is not None:
            before_resolution()
        return RouteDecision(
            normalized_provider, _normalized(base_url), _normalized(api_mode), str(model or ""), False
        )
    if normalized_provider not in _ROUTES:
        raise SubscriptionOnlyError(
            f"SUBSCRIPTION_ONLY_ROUTE: provider {normalized_provider or 'auto'!r} is not allowed"
        )
    expected_mode, expected_endpoint = _ROUTES[normalized_provider]
    if api_key not in (None, ""):
        raise SubscriptionOnlyError("SUBSCRIPTION_ONLY_ROUTE: explicit API keys are forbidden")
    if base_url not in (None, "") and _normalized(base_url).rstrip("/") != expected_endpoint.lower().rstrip("/"):
        raise SubscriptionOnlyError("SUBSCRIPTION_ONLY_ROUTE: endpoint is not canonical")
    if api_mode not in (None, "") and _normalized(api_mode) != expected_mode:
        raise SubscriptionOnlyError("SUBSCRIPTION_ONLY_ROUTE: transport is not registered")
    if not str(model or "").strip():
        raise SubscriptionOnlyError("SUBSCRIPTION_ONLY_ROUTE: model must be explicit")
    if before_resolution is not None:
        before_resolution()
    return RouteDecision(normalized_provider, expected_endpoint, expected_mode, str(model).strip(), True)


def issue_test_permit(
    *, provider: str, endpoint: str, api_mode: str, model: str,
    auth_generation: str, client: Any, expires_at: float,
) -> RoutePermit:
    """Construct a synthetic permit for offline behavior tests only."""
    return RoutePermit(
        provider=_normalized(provider), endpoint=_normalized(endpoint).rstrip("/"),
        api_mode=_normalized(api_mode), model=str(model).strip(),
        auth_generation=str(auth_generation), client_identity=id(client),
        expires_at=float(expires_at), source="synthetic-test-only",
    )


def invalidate_route_permit(agent: Any) -> None:
    agent._subscription_route_permit = None


def assert_request_permit(
    agent: Any, client: Any, *, now: Callable[[], float] = time.time,
) -> None:
    """Assert the exact active permit immediately before a physical send."""
    permit = getattr(agent, "_subscription_route_permit", None)
    if permit is None:
        if subscription_only_enabled():
            raise SubscriptionOnlyError("SUBSCRIPTION_ONLY_ROUTE: live request permit is missing")
        return
    if not isinstance(permit, RoutePermit):
        raise SubscriptionOnlyError("SUBSCRIPTION_ONLY_ROUTE: live request permit is malformed")
    expected = (
        _normalized(getattr(agent, "provider", "")),
        _normalized(getattr(agent, "base_url", "")).rstrip("/"),
        _normalized(getattr(agent, "api_mode", "")),
        str(getattr(agent, "model", "") or "").strip(),
        str(getattr(agent, "_subscription_auth_generation", "") or ""),
    )
    actual = (
        permit.provider, permit.endpoint, permit.api_mode, permit.model, permit.auth_generation,
    )
    if actual != expected:
        raise SubscriptionOnlyError("SUBSCRIPTION_ONLY_ROUTE: permit route binding changed")
    if permit.client_identity != id(client):
        raise SubscriptionOnlyError("SUBSCRIPTION_ONLY_ROUTE: permit client binding changed")
    if now() >= permit.expires_at:
        raise SubscriptionOnlyError("SUBSCRIPTION_ONLY_ROUTE: permit expired")


def guard_client_capability(agent: Any, *, client: Any = None) -> None:
    """Fail before credential adoption or client construction under a held route."""
    if not subscription_only_enabled():
        return
    permit = getattr(agent, "_subscription_route_permit", None)
    if client is None or permit is None:
        validate_route_intent(
            getattr(agent, "provider", ""), model=getattr(agent, "model", ""),
            base_url=getattr(agent, "base_url", ""), api_mode=getattr(agent, "api_mode", ""),
        ).require_live_permit()
    assert_request_permit(agent, client)


__all__ = [
    "RouteDecision", "RoutePermit", "SubscriptionOnlyError", "assert_request_permit",
    "guard_client_capability", "invalidate_route_permit", "issue_test_permit",
    "subscription_only_enabled", "validate_route_intent",
]
