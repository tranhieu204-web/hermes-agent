"""Tests for hermes_cli/fallback_config.py — fallback entry API-key resolution."""

from hermes_cli.fallback_config import resolve_entry_api_key


class TestResolveEntryApiKey:
    def test_inline_api_key_wins(self, monkeypatch):
        monkeypatch.setenv("FB_KEY", "env-key")
        entry = {"provider": "custom", "api_key": "inline-key", "key_env": "FB_KEY"}
        assert resolve_entry_api_key(entry) == "inline-key"

    def test_key_env_resolves_from_environment(self, monkeypatch):
        monkeypatch.setenv("FB_KEY", "env-key")
        assert resolve_entry_api_key({"key_env": "FB_KEY"}) == "env-key"

    def test_api_key_env_alias(self, monkeypatch):
        monkeypatch.setenv("FB_ALIAS_KEY", "alias-key")
        assert resolve_entry_api_key({"api_key_env": "FB_ALIAS_KEY"}) == "alias-key"

    def test_unset_env_var_returns_none(self, monkeypatch):
        monkeypatch.delenv("FB_MISSING", raising=False)
        # None (not "") lets resolve_runtime_provider fall through to the
        # provider's standard credential resolution.
        assert resolve_entry_api_key({"key_env": "FB_MISSING"}) is None

    def test_empty_env_var_returns_none(self, monkeypatch):
        monkeypatch.setenv("FB_EMPTY", "   ")
        assert resolve_entry_api_key({"key_env": "FB_EMPTY"}) is None

    def test_no_key_fields_returns_none(self):
        assert resolve_entry_api_key({"provider": "openrouter", "model": "glm"}) is None

    def test_non_dict_returns_none(self):
        assert resolve_entry_api_key(None) is None
        assert resolve_entry_api_key("nope") is None  # type: ignore[arg-type]

    def test_whitespace_inline_key_falls_through_to_env(self, monkeypatch):
        monkeypatch.setenv("FB_KEY", "env-key")
        entry = {"api_key": "   ", "key_env": "FB_KEY"}
        assert resolve_entry_api_key(entry) == "env-key"


class TestGetFallbackChainRanking:
    def test_disabled_fleet_preserves_configured_chain_without_ranking(self, monkeypatch):
        from hermes_cli.fallback_config import get_fallback_chain

        monkeypatch.setattr(
            "gateway.fleet_safety.selector.rank_fallback_chain",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("disabled Fleet must not invoke the ranker")
            ),
        )
        configured = [
            {"provider": "paid-provider", "model": "paid-model"},
            {"provider": "safe-provider", "model": "safe-model"},
        ]

        assert get_fallback_chain(
            {
                "fleet": {"enabled": False},
                "fallback_providers": configured,
            }
        ) == configured

    def test_enabled_fleet_ranker_exception_fails_closed(self, monkeypatch):
        from hermes_cli.fallback_config import get_fallback_chain

        monkeypatch.setattr(
            "gateway.fleet_safety.selector.rank_fallback_chain",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                RuntimeError("safety engine unavailable")
            ),
        )

        assert get_fallback_chain(
            {
                "fleet": {"enabled": True},
                "fallback_providers": [
                    {"provider": "paid-provider", "model": "paid-model"},
                ],
            }
        ) == []

    def test_get_fallback_chain_routes_through_rank_fallback_chain(self, monkeypatch):
        from hermes_cli.fallback_config import get_fallback_chain
        from gateway.fleet_safety.usage_verify import VerifiedUsage

        def fake_verified(provider, **kwargs):
            if provider == "chatgpt_codex":
                return VerifiedUsage(provider=provider, used_percent=15.0, source="cache", stale=False, suspect=False)
            return VerifiedUsage(provider=provider, used_percent=50.0, source="cache", stale=False, suspect=False)

        monkeypatch.setattr("gateway.fleet_safety.selector.verified_usage_for", fake_verified)
        cfg = {
            "fleet": {"enabled": True},
            "fallback_providers": [
                {"provider": "grok", "model": "grok-4.5"},
                {"provider": "chatgpt_codex", "model": "gpt-5.6-sol"},
            ]
        }
        chain = get_fallback_chain(cfg)
        assert len(chain) >= 2
        # codex has higher headroom (~85%) than grok (~50%) so rank_fallback_chain orders codex first
        assert chain[0]["provider"] == "chatgpt_codex"
