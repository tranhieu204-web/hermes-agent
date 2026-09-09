from types import SimpleNamespace

import pytest

from hermes_cli.subscription_policy import (
    SubscriptionOnlyError,
    assert_request_permit,
    issue_test_permit,
    validate_route_intent,
)


SAFE = {
    "subscription_only": True,
    "model": {"provider": "openai-codex", "default": "gpt-6-astra"},
}


@pytest.mark.parametrize(
    "provider,kwargs",
    [
        ("auto", {}),
        ("copilot", {}),
        ("openai", {}),
        ("openai-codex", {"api_key": "synthetic-key"}),
        ("openai-codex", {"base_url": "https://proxy.invalid/v1"}),
        ("xai-oauth", {"api_mode": "chat_completions"}),
    ],
)
def test_route_intent_fails_before_any_resolution(provider, kwargs):
    touched = []
    with pytest.raises(SubscriptionOnlyError):
        validate_route_intent(
            provider,
            model="gpt-6-astra",
            config=SAFE,
            before_resolution=lambda: touched.append("resolved"),
            **kwargs,
        )
    assert touched == []


def test_live_route_is_hold_without_authoritative_same_account_receipt():
    with pytest.raises(SubscriptionOnlyError, match="AUTHORITATIVE_.*_CHARGE_SOURCE_UNAVAILABLE"):
        validate_route_intent(
            "openai-codex", model="gpt-6-astra", config=SAFE,
        ).require_live_permit()


def test_permit_binds_route_client_auth_generation_and_expiry():
    clock = [1000.0]
    client = object()
    permit = issue_test_permit(
        provider="openai-codex",
        endpoint="https://chatgpt.com/backend-api/codex",
        api_mode="codex_responses",
        model="gpt-6-astra",
        auth_generation="generation-one",
        client=client,
        expires_at=1010.0,
    )
    agent = SimpleNamespace(
        provider="openai-codex",
        base_url="https://chatgpt.com/backend-api/codex",
        api_mode="codex_responses",
        model="gpt-6-astra",
        _subscription_auth_generation="generation-one",
        _subscription_route_permit=permit,
    )

    assert_request_permit(agent, client, now=lambda: clock[0])
    with pytest.raises(SubscriptionOnlyError, match="client"):
        assert_request_permit(agent, object(), now=lambda: clock[0])
    clock[0] = 1011.0
    with pytest.raises(SubscriptionOnlyError, match="expired"):
        assert_request_permit(agent, client, now=lambda: clock[0])


def test_unregistered_transport_fails_closed():
    with pytest.raises(SubscriptionOnlyError, match="registered"):
        validate_route_intent(
            "openai-codex",
            model="gpt-6-astra",
            api_mode="new_transport",
            config=SAFE,
        )
