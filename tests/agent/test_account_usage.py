from types import SimpleNamespace

import pytest

from agent import account_usage


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, calls, payload):
        self.calls = calls
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, headers):
        self.calls.append({"url": url, "headers": headers})
        return _FakeResponse(self.payload)


@pytest.fixture
def codex_usage_payload():
    return {
        "plan_type": "plus",
        "rate_limit": {
            "primary_window": {
                "used_percent": 21,
                "reset_at": 1779846359,
            },
            "secondary_window": {
                "used_percent": 4,
                "reset_at": 1780230796,
            },
        },
        "credits": {"has_credits": False},
    }


def test_codex_usage_prefers_explicit_live_agent_credentials(monkeypatch, codex_usage_payload):
    calls = []
    monkeypatch.setattr(
        account_usage.httpx,
        "Client",
        lambda timeout: _FakeClient(calls, codex_usage_payload),
    )
    monkeypatch.setattr(
        account_usage,
        "resolve_codex_runtime_credentials",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("legacy auth should not be used")),
    )

    snapshot = account_usage.fetch_account_usage(
        "openai-codex",
        base_url="https://chatgpt.com/backend-api/codex",
        api_key="live-agent-token",
    )

    assert snapshot is not None
    assert snapshot.provider == "openai-codex"
    assert snapshot.plan == "Plus"
    assert [w.label for w in snapshot.windows] == ["Session", "Weekly"]
    assert snapshot.windows[0].used_percent == 21
    assert calls[0]["url"] == "https://chatgpt.com/backend-api/wham/usage"
    assert calls[0]["headers"]["Authorization"] == "Bearer live-agent-token"


def test_codex_usage_falls_back_to_native_credential_pool(monkeypatch, codex_usage_payload):
    calls = []
    monkeypatch.setattr(
        account_usage.httpx,
        "Client",
        lambda timeout: _FakeClient(calls, codex_usage_payload),
    )
    # Pool fallback fires only on AuthError (the documented "no creds" mode of
    # the resolver), NOT on arbitrary exceptions — see the transient-error guard
    # test below.
    monkeypatch.setattr(
        account_usage,
        "resolve_codex_runtime_credentials",
        lambda **kwargs: (_ for _ in ()).throw(
            account_usage.AuthError("no singleton auth", provider="openai-codex", code="codex_auth_missing")
        ),
    )

    pool_entry = SimpleNamespace(
        runtime_api_key="pooled-token",
        runtime_base_url="https://chatgpt.com/backend-api/codex",
    )
    pool = SimpleNamespace(select=lambda: pool_entry)

    import agent.credential_pool as credential_pool

    monkeypatch.setattr(credential_pool, "load_pool", lambda provider: pool)

    snapshot = account_usage.fetch_account_usage("openai-codex")

    assert snapshot is not None
    assert snapshot.windows[0].label == "Session"
    assert snapshot.windows[1].label == "Weekly"
    assert calls[0]["url"] == "https://chatgpt.com/backend-api/wham/usage"
    assert calls[0]["headers"]["Authorization"] == "Bearer pooled-token"
    # Pool creds have no account_id concept — the ChatGPT-Account-Id header must
    # be omitted rather than sent stale/wrong.
    assert "ChatGPT-Account-Id" not in calls[0]["headers"]


def test_codex_usage_does_not_swap_to_pool_on_transient_resolver_error(monkeypatch, codex_usage_payload):
    """A transient refresh/network failure (non-AuthError) must NOT silently
    downgrade to a possibly-different pool account. It fails open (no snapshot)
    instead of reporting the wrong account's usage."""
    calls = []
    monkeypatch.setattr(
        account_usage.httpx,
        "Client",
        lambda timeout: _FakeClient(calls, codex_usage_payload),
    )
    monkeypatch.setattr(
        account_usage,
        "resolve_codex_runtime_credentials",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("refresh endpoint 503")),
    )

    pool_entry = SimpleNamespace(
        runtime_api_key="pooled-token-WRONG-ACCOUNT",
        runtime_base_url="https://chatgpt.com/backend-api/codex",
    )
    pool = SimpleNamespace(select=lambda: pool_entry)

    import agent.credential_pool as credential_pool

    # If the guard regressed, this pool would be consulted and return a snapshot
    # for the wrong account. It must NOT be.
    monkeypatch.setattr(credential_pool, "load_pool", lambda provider: pool)

    snapshot = account_usage.fetch_account_usage("openai-codex")

    assert snapshot is None
    assert calls == []  # HTTP usage endpoint never hit with a wrong-account token


def test_codex_usage_account_id_read_failure_keeps_singleton_token(monkeypatch, codex_usage_payload):
    """When the resolver succeeds but the separate account_id read raises, the
    working singleton token must still be used (best-effort account_id), NOT
    abandoned in favor of a header-less pool credential."""
    calls = []
    monkeypatch.setattr(
        account_usage.httpx,
        "Client",
        lambda timeout: _FakeClient(calls, codex_usage_payload),
    )
    monkeypatch.setattr(
        account_usage,
        "resolve_codex_runtime_credentials",
        lambda **kwargs: {
            "api_key": "singleton-token",
            "base_url": "https://chatgpt.com/backend-api/codex",
        },
    )
    monkeypatch.setattr(
        account_usage,
        "_read_codex_tokens",
        lambda *a, **k: (_ for _ in ()).throw(
            account_usage.AuthError("partial store", provider="openai-codex", code="codex_auth_invalid_shape")
        ),
    )

    import agent.credential_pool as credential_pool

    monkeypatch.setattr(
        credential_pool,
        "load_pool",
        lambda provider: (_ for _ in ()).throw(AssertionError("pool must not be consulted")),
    )

    snapshot = account_usage.fetch_account_usage("openai-codex")

    assert snapshot is not None
    assert calls[0]["headers"]["Authorization"] == "Bearer singleton-token"
    # account_id read failed → header omitted, but the singleton token is kept.
    assert "ChatGPT-Account-Id" not in calls[0]["headers"]


def test_codex_usage_treats_wham_used_percent_as_used_not_remaining(monkeypatch):
    """ChatGPT UI says "left"; /wham/usage.used_percent is already used."""
    payload = {
        "plan_type": "plus",
        "rate_limit": {
            "primary_window": {
                "used_percent": 85,
                "reset_at": 1779846359,
            },
            "secondary_window": {
                "used_percent": 14,
                "reset_at": 1780230796,
            },
        },
        "credits": {"has_credits": False},
    }
    calls = []
    monkeypatch.setattr(
        account_usage.httpx,
        "Client",
        lambda timeout: _FakeClient(calls, payload),
    )
    monkeypatch.setattr(
        account_usage,
        "resolve_codex_runtime_credentials",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("explicit auth should be used")),
    )

    snapshot = account_usage.fetch_account_usage(
        "openai-codex",
        base_url="https://chatgpt.com/backend-api/codex",
        api_key="live-agent-token",
    )

    assert snapshot is not None
    assert [window.used_percent for window in snapshot.windows] == [85, 14]
    rendered = "\n".join(account_usage.render_account_usage_lines(snapshot, markdown=True))
    assert "85% used" in rendered
    assert "14% used" in rendered
    assert "15% used" not in rendered
    assert "86% used" not in rendered


# ── Banked rate-limit reset credits (`/usage reset`) ─────────────────────────


class _FakeResetClient:
    """GET returns the usage payload; POST returns the consume payload."""

    def __init__(self, calls, usage_payload, consume_payload=None):
        self.calls = calls
        self.usage_payload = usage_payload
        self.consume_payload = consume_payload or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, headers):
        self.calls.append({"method": "GET", "url": url, "headers": headers})
        return _FakeResponse(self.usage_payload)

    def post(self, url, headers=None, json=None):
        self.calls.append({"method": "POST", "url": url, "headers": headers, "json": json})
        return _FakeResponse(self.consume_payload)


def _usage_payload_with_resets(primary_used, secondary_used, banked):
    return {
        "plan_type": "plus",
        "rate_limit": {
            "primary_window": {"used_percent": primary_used, "reset_at": 1779846359},
            "secondary_window": {"used_percent": secondary_used, "reset_at": 1780230796},
        },
        "rate_limit_reset_credits": {"available_count": banked},
        "credits": {"has_credits": False},
    }


def test_usage_snapshot_shows_banked_resets_hint(monkeypatch):
    calls = []
    monkeypatch.setattr(
        account_usage.httpx,
        "Client",
        lambda timeout: _FakeResetClient(calls, _usage_payload_with_resets(21, 4, 2)),
    )

    snapshot = account_usage.fetch_account_usage(
        "openai-codex",
        base_url="https://chatgpt.com/backend-api/codex",
        api_key="live-agent-token",
    )

    assert snapshot is not None
    rendered = "\n".join(account_usage.render_account_usage_lines(snapshot))
    assert "You have 2 resets banked - use /usage reset to activate" in rendered


def test_usage_snapshot_hides_reset_hint_when_none_banked(monkeypatch, codex_usage_payload):
    calls = []
    monkeypatch.setattr(
        account_usage.httpx,
        "Client",
        lambda timeout: _FakeResetClient(calls, codex_usage_payload),
    )

    snapshot = account_usage.fetch_account_usage(
        "openai-codex",
        base_url="https://chatgpt.com/backend-api/codex",
        api_key="live-agent-token",
    )

    assert snapshot is not None
    rendered = "\n".join(account_usage.render_account_usage_lines(snapshot))
    assert "banked" not in rendered


def test_redeem_blocked_when_limits_not_exhausted(monkeypatch):
    calls = []
    monkeypatch.setattr(
        account_usage.httpx,
        "Client",
        lambda timeout: _FakeResetClient(calls, _usage_payload_with_resets(60, 30, 2)),
    )

    result = account_usage.redeem_codex_reset_credit(
        base_url="https://chatgpt.com/backend-api/codex",
        api_key="live-agent-token",
    )

    assert result.status == "not_exhausted"
    assert not result.redeemed
    assert "--force" in result.message
    assert "60% used" in result.message
    assert result.available_count == 2
    # The consume endpoint must never be hit — the credit is protected.
    assert [c["method"] for c in calls] == ["GET"]


def test_redeem_force_bypasses_exhaustion_guard(monkeypatch):
    calls = []
    monkeypatch.setattr(
        account_usage.httpx,
        "Client",
        lambda timeout: _FakeResetClient(
            calls,
            _usage_payload_with_resets(60, 30, 2),
            consume_payload={"code": "reset", "windows_reset": 2},
        ),
    )

    result = account_usage.redeem_codex_reset_credit(
        base_url="https://chatgpt.com/backend-api/codex",
        api_key="live-agent-token",
        force=True,
    )

    assert result.redeemed
    assert result.windows_reset == 2
    assert result.available_count == 1  # 2 banked - 1 spent
    assert "1 banked reset remaining" in result.message
    post = [c for c in calls if c["method"] == "POST"][0]
    assert post["url"] == "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume"
    assert post["json"]["redeem_request_id"]  # idempotency key present
    assert "credit_id" not in post["json"]


def test_redeem_allowed_without_force_when_window_exhausted(monkeypatch):
    calls = []
    monkeypatch.setattr(
        account_usage.httpx,
        "Client",
        lambda timeout: _FakeResetClient(
            calls,
            _usage_payload_with_resets(100, 42, 1),
            consume_payload={"code": "reset", "windows_reset": 2},
        ),
    )

    result = account_usage.redeem_codex_reset_credit(
        base_url="https://chatgpt.com/backend-api/codex",
        api_key="live-agent-token",
    )

    assert result.redeemed
    assert result.available_count == 0
    assert "0 banked resets remaining" in result.message


def test_redeem_refuses_when_no_credits_banked(monkeypatch):
    calls = []
    monkeypatch.setattr(
        account_usage.httpx,
        "Client",
        lambda timeout: _FakeResetClient(calls, _usage_payload_with_resets(100, 100, 0)),
    )

    result = account_usage.redeem_codex_reset_credit(
        base_url="https://chatgpt.com/backend-api/codex",
        api_key="live-agent-token",
    )

    assert result.status == "no_credits_banked"
    assert [c["method"] for c in calls] == ["GET"]


def test_redeem_nothing_to_reset_reports_credit_not_spent(monkeypatch):
    calls = []
    monkeypatch.setattr(
        account_usage.httpx,
        "Client",
        lambda timeout: _FakeResetClient(
            calls,
            _usage_payload_with_resets(100, 100, 3),
            consume_payload={"code": "nothing_to_reset"},
        ),
    )

    result = account_usage.redeem_codex_reset_credit(
        base_url="https://chatgpt.com/backend-api/codex",
        api_key="live-agent-token",
    )

    assert result.status == "nothing_to_reset"
    assert not result.redeemed
    assert "NOT spent" in result.message
    assert result.available_count == 3


def test_redeem_missing_credentials_reports_unavailable(monkeypatch):
    monkeypatch.setattr(
        account_usage,
        "_resolve_codex_usage_credentials",
        lambda base_url, api_key: (_ for _ in ()).throw(RuntimeError("no creds")),
    )

    result = account_usage.redeem_codex_reset_credit()

    assert result.status == "unavailable"
    assert "hermes auth" in result.message


# ── Anthropic OAuth usage window (claude_code lane capacity measurement) ───


@pytest.fixture
def anthropic_oauth_usage_payload():
    """Sample OAuth usage endpoint response with windows and extra_usage."""
    return {
        "five_hour": {
            "utilization": 0.15,
            "resets_at": "2026-07-26T22:00:00Z",
        },
        "seven_day": {
            "utilization": 0.35,
            "resets_at": "2026-08-02T00:00:00Z",
        },
        "seven_day_opus": {
            "utilization": 0.20,
            "resets_at": "2026-08-02T00:00:00Z",
        },
        "seven_day_sonnet": {
            "utilization": 0.45,
            "resets_at": "2026-08-02T00:00:00Z",
        },
        "extra_usage": {
            "is_enabled": True,
            "used_credits": 125.50,
            "monthly_limit": 500.00,
            "currency": "USD",
        },
    }


def test_anthropic_oauth_usage_window_fetch(monkeypatch, anthropic_oauth_usage_payload):
    """Verify Anthropic usage window fetch via OAuth endpoint."""
    calls = []

    class _AnthropicFakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, headers):
            calls.append({"url": url, "headers": headers})
            return _FakeResponse(anthropic_oauth_usage_payload)

    monkeypatch.setattr(
        account_usage.httpx,
        "Client",
        lambda timeout: _AnthropicFakeClient(),
    )
    # Use the read-only resolver mock
    monkeypatch.setattr(
        account_usage,
        "_resolve_anthropic_token_readonly",
        lambda: "oauth-token-example",
    )
    monkeypatch.setattr(
        account_usage,
        "_is_oauth_token",
        lambda t: True,
    )

    snapshot = account_usage._fetch_anthropic_usage_window()

    assert snapshot is not None
    assert snapshot.provider == "anthropic"
    assert snapshot.source == "oauth_usage_api"
    # Verify windows were parsed correctly
    assert len(snapshot.windows) == 4
    labels = [w.label for w in snapshot.windows]
    assert labels == ["Current session", "Current week", "Opus week", "Sonnet week"]
    # Verify utilization was converted to percentage (0.15 -> 15%)
    assert snapshot.windows[0].used_percent == 15.0
    assert snapshot.windows[1].used_percent == 35.0
    assert snapshot.windows[2].used_percent == 20.0
    assert snapshot.windows[3].used_percent == 45.0
    # Verify extra_usage details
    assert "Extra usage: 125.50 / 500.00 USD" in snapshot.details
    # Verify endpoint and authorization header
    assert calls[0]["url"] == "https://api.anthropic.com/api/oauth/usage"
    assert calls[0]["headers"]["Authorization"] == "Bearer oauth-token-example"


def test_anthropic_oauth_usage_returns_none_when_no_token(monkeypatch):
    """Usage fetch returns None when no token is available."""
    monkeypatch.setattr(
        account_usage,
        "_resolve_anthropic_token_readonly",
        lambda: None,
    )

    snapshot = account_usage._fetch_anthropic_usage_window()

    assert snapshot is None


def test_anthropic_oauth_usage_returns_unavailable_for_non_oauth_token(monkeypatch):
    """Usage fetch returns unavailable message for non-OAuth tokens (e.g., API keys)."""
    monkeypatch.setattr(
        account_usage,
        "resolve_anthropic_token",
        lambda: "sk-ant-v1-non-oauth-key",
    )
    monkeypatch.setattr(
        account_usage,
        "_is_oauth_token",
        lambda t: False,
    )

    snapshot = account_usage._fetch_anthropic_usage_window()

    assert snapshot is not None
    assert snapshot.provider == "anthropic"
    assert snapshot.unavailable_reason is not None
    assert "OAuth-backed" in snapshot.unavailable_reason


def test_anthropic_account_usage_calls_usage_window(monkeypatch, anthropic_oauth_usage_payload):
    """Verify _fetch_anthropic_account_usage delegates to _fetch_anthropic_usage_window."""
    calls = []

    class _AnthropicFakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, headers):
            calls.append({"url": url, "headers": headers})
            return _FakeResponse(anthropic_oauth_usage_payload)

    monkeypatch.setattr(
        account_usage.httpx,
        "Client",
        lambda timeout: _AnthropicFakeClient(),
    )
    monkeypatch.setattr(
        account_usage,
        "resolve_anthropic_token",
        lambda: "oauth-token-example",
    )
    monkeypatch.setattr(
        account_usage,
        "_is_oauth_token",
        lambda t: True,
    )

    # Call through the public entry point
    snapshot = account_usage._fetch_anthropic_account_usage()

    # Should have called the usage endpoint (delegated to usage_window)
    assert snapshot is not None
    assert snapshot.provider == "anthropic"
    assert len(snapshot.windows) == 4
    assert calls[0]["url"] == "https://api.anthropic.com/api/oauth/usage"


def test_anthropic_oauth_usage_with_percentage_already_scaled(monkeypatch):
    """Verify that utilization values already scaled as percentages (0-100) are handled."""
    payload = {
        "five_hour": {
            "utilization": 85.0,  # Already a percentage, not 0-1
            "resets_at": "2026-07-26T22:00:00Z",
        },
    }
    calls = []

    class _AnthropicFakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, headers):
            calls.append({"url": url, "headers": headers})
            return _FakeResponse(payload)

    monkeypatch.setattr(
        account_usage.httpx,
        "Client",
        lambda timeout: _AnthropicFakeClient(),
    )
    monkeypatch.setattr(
        account_usage,
        "resolve_anthropic_token",
        lambda: "oauth-token-example",
    )
    monkeypatch.setattr(
        account_usage,
        "_is_oauth_token",
        lambda t: True,
    )

    snapshot = account_usage._fetch_anthropic_usage_window()

    assert snapshot is not None
    # Utilization is already > 1, so it's used as-is
    assert snapshot.windows[0].used_percent == 85.0


# ── Read-only resolver tests (proves no refresh/write side effects) ───


def test_readonly_resolver_uses_env_vars_only(monkeypatch):
    """Verify read-only resolver returns env vars without attempting refresh or writes."""
    import hermes_cli.auth as auth_mod

    # Set only env vars, no credential file.
    monkeypatch.setenv("ANTHROPIC_TOKEN", "oauth-env-token")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

    # Spy on write functions — assert they're never called.
    write_pool_calls = []

    def spy_write_pool(*args, **kwargs):
        write_pool_calls.append((args, kwargs))
        raise AssertionError("write_credential_pool must not be called from readonly resolver")

    monkeypatch.setattr(auth_mod, "write_credential_pool", spy_write_pool)

    token = account_usage._resolve_anthropic_token_readonly()

    assert token == "oauth-env-token"
    assert write_pool_calls == [], "Pool write must not occur"


def test_readonly_resolver_reads_claude_code_creds_without_refresh(monkeypatch):
    """Verify read-only resolver reads credentials without refresh or write side effects."""
    from agent import anthropic_adapter
    import hermes_cli.auth as auth_mod

    creds = {"accessToken": "claude-stored-token", "refreshToken": "refresh-xxx"}

    def fake_read_creds():
        return creds

    def fake_is_valid(c):
        return True

    monkeypatch.setattr(
        anthropic_adapter,
        "read_claude_code_credentials",
        fake_read_creds,
    )
    monkeypatch.setattr(
        anthropic_adapter,
        "is_claude_code_token_valid",
        fake_is_valid,
    )
    # Ensure env vars are not set.
    monkeypatch.delenv("ANTHROPIC_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    # Spy on writers — assert they're never called.
    write_pool_calls = []

    def spy_write_pool(*args, **kwargs):
        write_pool_calls.append((args, kwargs))
        raise AssertionError("write_credential_pool must not be called")

    monkeypatch.setattr(auth_mod, "write_credential_pool", spy_write_pool)

    token = account_usage._resolve_anthropic_token_readonly()

    assert token == "claude-stored-token"
    assert write_pool_calls == [], "Pool write must not occur"


def test_readonly_resolver_returns_none_on_expired_claude_code_token(monkeypatch):
    """Verify expired token → None without refresh POST (mutation-sensitive test).

    This test FAILS if someone reintroduces _refresh_oauth_token() into the resolver.
    The spy ensures any refresh attempt is immediately detected.
    """
    from agent import anthropic_adapter
    import hermes_cli.auth as auth_mod

    creds = {"accessToken": "expired-token", "refreshToken": "refresh-xxx"}

    def fake_read_creds():
        return creds

    def fake_is_valid(c):
        return False  # Token is expired

    monkeypatch.setattr(
        anthropic_adapter,
        "read_claude_code_credentials",
        fake_read_creds,
    )
    monkeypatch.setattr(
        anthropic_adapter,
        "is_claude_code_token_valid",
        fake_is_valid,
    )
    # Ensure env vars are not set.
    monkeypatch.delenv("ANTHROPIC_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    # MUTATION-SENSITIVE: spy on _refresh_oauth_token() to detect if someone reintroduces it.
    # This test MUST FAIL if refresh is called, so we spy and assert zero calls.
    refresh_calls = []

    def spy_refresh(creds):
        refresh_calls.append(creds)
        raise AssertionError("_refresh_oauth_token must not be called in readonly resolver")

    monkeypatch.setattr(anthropic_adapter, "_refresh_oauth_token", spy_refresh)

    # Also spy on pool writes.
    write_pool_calls = []

    def spy_write_pool(*args, **kwargs):
        write_pool_calls.append((args, kwargs))
        raise AssertionError("write_credential_pool must not be called")

    monkeypatch.setattr(auth_mod, "write_credential_pool", spy_write_pool)

    token = account_usage._resolve_anthropic_token_readonly()

    assert token is None, "Expired token must return None, not attempt refresh"
    assert refresh_calls == [], "Refresh must not be called (MUTATION-SENSITIVE)"
    assert write_pool_calls == [], "Pool write must not occur"


def test_usage_window_fetch_fails_closed_on_http_error(monkeypatch, anthropic_oauth_usage_payload):
    """Verify usage window fetch returns None on HTTP error (fail-closed)."""
    calls = []

    class _FailingClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, headers):
            calls.append({"url": url, "headers": headers})
            # Simulate HTTP 401 Unauthorized
            import httpx

            response = SimpleNamespace()
            response.status_code = 401

            def raise_for_status():
                raise httpx.HTTPStatusError("401 Unauthorized", request=None, response=response)

            response.raise_for_status = raise_for_status
            return response

    monkeypatch.setattr(
        account_usage.httpx,
        "Client",
        lambda timeout: _FailingClient(),
    )
    monkeypatch.setattr(
        account_usage,
        "_resolve_anthropic_token_readonly",
        lambda: "oauth-token",
    )
    monkeypatch.setattr(
        account_usage,
        "_is_oauth_token",
        lambda t: True,
    )

    snapshot = account_usage._fetch_anthropic_usage_window()

    assert snapshot is None, "HTTP error must return None (fail-closed)"
    assert len(calls) == 1, "Should have attempted one API call"


def test_usage_window_fetch_fails_closed_on_json_parse_error(monkeypatch):
    """Verify usage window fetch returns None on JSON parse error (fail-closed)."""
    calls = []

    class _BadJsonClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, headers):
            calls.append({"url": url, "headers": headers})
            response = SimpleNamespace()

            def raise_for_status():
                pass

            def bad_json():
                raise ValueError("Invalid JSON")

            response.raise_for_status = raise_for_status
            response.json = bad_json
            return response

    monkeypatch.setattr(
        account_usage.httpx,
        "Client",
        lambda timeout: _BadJsonClient(),
    )
    monkeypatch.setattr(
        account_usage,
        "_resolve_anthropic_token_readonly",
        lambda: "oauth-token",
    )
    monkeypatch.setattr(
        account_usage,
        "_is_oauth_token",
        lambda t: True,
    )

    snapshot = account_usage._fetch_anthropic_usage_window()

    assert snapshot is None, "JSON parse error must return None (fail-closed)"
    assert len(calls) == 1, "Should have attempted one API call"


def test_usage_window_fetch_uses_readonly_resolver_not_full_resolver(monkeypatch, anthropic_oauth_usage_payload):
    """Verify _fetch_anthropic_usage_window uses _resolve_anthropic_token_readonly, not resolve_anthropic_token."""
    calls = []

    class _AnthropicFakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, headers):
            calls.append({"url": url, "headers": headers})
            return _FakeResponse(anthropic_oauth_usage_payload)

    # Mock httpx
    monkeypatch.setattr(
        account_usage.httpx,
        "Client",
        lambda timeout: _AnthropicFakeClient(),
    )

    # Mock the readonly resolver to track calls
    readonly_calls = []

    def tracked_readonly():
        readonly_calls.append(True)
        return "oauth-token-from-readonly"

    monkeypatch.setattr(
        account_usage,
        "_resolve_anthropic_token_readonly",
        tracked_readonly,
    )

    # Mock resolve_anthropic_token to ensure it's NOT called
    monkeypatch.setattr(
        account_usage,
        "resolve_anthropic_token",
        lambda: (_ for _ in ()).throw(AssertionError("full resolver must not be called")),
    )

    monkeypatch.setattr(
        account_usage,
        "_is_oauth_token",
        lambda t: True,
    )

    snapshot = account_usage._fetch_anthropic_usage_window()

    assert snapshot is not None
    assert len(readonly_calls) == 1, "Read-only resolver must be called exactly once"
    assert len(calls) == 1, "API endpoint must be called once"
