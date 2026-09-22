from types import SimpleNamespace

import httpx
import pytest

from agent.client_lifecycle import ClientLifecycleMixin
from agent.codex_runtime import run_codex_stream
from hermes_cli.subscription_policy import SubscriptionOnlyError


def test_oauth_refresh_is_denied_before_resolver(monkeypatch):
    events = []

    class Agent(ClientLifecycleMixin):
        api_mode = "codex_responses"
        provider = "xai-oauth"
        model = "grok-4.6"
        api_key = "synthetic-oauth"
        base_url = "https://api.x.ai/v1"

    def deny_before_capability(_agent, **_kwargs):
        events.append("policy")
        raise SubscriptionOnlyError("NOT_VERIFIED: test hold")

    monkeypatch.setattr("hermes_cli.subscription_policy.invalidate_route_permit", lambda _agent: events.append("invalidate"))
    monkeypatch.setattr("hermes_cli.subscription_policy.guard_client_capability", deny_before_capability)
    monkeypatch.setattr(
        "hermes_cli.auth.resolve_xai_oauth_runtime_credentials",
        lambda **_kwargs: events.append("resolver"),
    )

    with pytest.raises(SubscriptionOnlyError, match="test hold"):
        Agent()._try_refresh_codex_client_credentials()
    assert events == ["invalidate", "policy"]


def test_each_codex_physical_retry_reasserts_and_expiry_blocks_send(monkeypatch):
    events = []

    def assert_permit(_agent, _client):
        events.append("permit")
        if events.count("permit") == 2:
            raise SubscriptionOnlyError("SUBSCRIPTION_ONLY_ROUTE: permit expired")

    class Responses:
        def create(self, **_kwargs):
            events.append("send")
            raise httpx.ConnectError("synthetic retry")

    agent = SimpleNamespace(
        provider="openai-codex",
        model="gpt-6-astra",
        session_id="synthetic",
        _interrupt_requested=False,
        _active_codex_stream_request_token=None,
        _current_api_request_id=None,
        _fallback_index=0,
        is_subagent=False,
        show_commentary=False,
        interim_assistant_callback=None,
        _client_log_context=lambda: "synthetic",
    )
    client = SimpleNamespace(responses=Responses())
    monkeypatch.setattr("hermes_cli.subscription_policy.assert_request_permit", assert_permit)
    monkeypatch.setattr(
        "agent.relay_llm.stream",
        lambda api_kwargs, opener, **_kwargs: opener(api_kwargs),
    )

    with pytest.raises(SubscriptionOnlyError, match="expired"):
        run_codex_stream(agent, {"model": "gpt-6-astra", "input": []}, client=client)
    assert events == ["permit", "send", "permit"]


def test_implicit_model_switch_is_denied_before_route_or_credentials(monkeypatch):
    import hermes_cli.model_switch as model_switch
    import hermes_cli.subscription_policy as policy

    events = []
    original_validate = policy.validate_route_intent
    safe = {"subscription_only": True}
    monkeypatch.setattr(policy, "subscription_only_enabled", lambda: True)
    monkeypatch.setattr(
        policy,
        "validate_route_intent",
        lambda provider, **kwargs: original_validate(provider, config=safe, **kwargs),
    )
    monkeypatch.setattr(
        model_switch, "_route_from_model_input", lambda _state: events.append("route"),
    )
    monkeypatch.setattr(
        model_switch, "_resolve_switch_credentials", lambda _state: events.append("credentials"),
    )

    with pytest.raises(SubscriptionOnlyError, match="provider 'auto'"):
        model_switch.switch_model(
            "some-alias", "openai-codex", "gpt-6-astra",
            current_base_url="https://chatgpt.com/backend-api/codex",
        )
    assert events == []
