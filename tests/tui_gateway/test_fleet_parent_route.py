from __future__ import annotations

import os
import sys
import tempfile
import threading
import types
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

os.environ.setdefault(
    "HERMES_HOME",
    str(Path(tempfile.gettempdir()) / "hermes-fleet-parent-route-tests"),
)

from hermes_cli.fleet.types import (
    AdapterKind,
    ParentAdmission,
    ParentLeaseHandle,
    ParentTurnAcquisition,
    ParentPin,
    ReasonCode,
    RoutePurpose,
)
from tui_gateway import server
from tui_gateway import fleet_parent


@pytest.fixture(autouse=True)
def _clean_gateway_sessions(monkeypatch):
    monkeypatch.setattr(server, "_schedule_agent_build", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)
    monkeypatch.setattr(server, "_completion_cwd", lambda params=None: str(Path.cwd()))
    server._sessions.clear()
    yield
    for session in list(server._sessions.values()):
        try:
            server._teardown_session(session)
        except Exception:
            pass
    server._sessions.clear()


def _pin(
    *,
    lane_id: str = "grok",
    provider_id: str = "xai-oauth",
    model_id: str = "grok-4.5",
) -> ParentPin:
    return ParentPin(
        profile_id="default",
        lineage_root_id="stored-root",
        session_id="stored-root",
        purpose=RoutePurpose.DESKTOP_PARENT,
        lane_id=lane_id,
        adapter_kind=AdapterKind.NATIVE_PROVIDER,
        provider_id=provider_id,
        model_id=model_id,
        effort="max",
        fast_mode=False,
        qualification_evidence_hash="evidence-hash",
        route_identity="sha256:route",
        selection_reason=ReasonCode.ROTATION,
        status="pinned",
    )


def test_session_create_fleet_auto_admits_before_deferred_build_and_returns_grok(
    monkeypatch,
):
    events: list[str] = []

    def admit(**kwargs):
        events.append("admit")
        pin = _pin()
        return ParentAdmission(
            reason=ReasonCode.MET,
            pin=ParentPin(
                **{
                    **pin.__dict__,
                    "lineage_root_id": kwargs["lineage_root_id"],
                    "session_id": kwargs["session_id"],
                }
            ),
        )

    monkeypatch.setattr(server, "_admit_fleet_parent_session", admit)
    monkeypatch.setattr(
        server,
        "_schedule_agent_build",
        lambda sid: events.append("build"),
    )

    response = server._methods["session.create"](
        "request-1",
        {
            "cols": 80,
            "source": "desktop",
            "model_source": "fleet_auto",
        },
    )

    assert events == ["admit", "build"]
    assert "error" not in response
    result = response["result"]
    assert result["info"]["model"] == "grok-4.5"
    assert result["info"]["provider"] == "xai-oauth"
    assert result["info"]["fleet_lane_id"] == "grok"
    assert result["info"]["fleet_adapter_kind"] == "native_provider"
    assert result["info"]["fleet_route_purpose"] == "desktop_parent"
    assert result["info"]["fleet_selection_reason"] == "ROTATION"
    assert result["info"]["fleet_pin_status"] == "pinned"
    session = server._sessions[result["session_id"]]
    assert session["model_override"] == {
        "model": "grok-4.5",
        "provider": "xai-oauth",
    }
    assert session["create_service_tier_override"] == ""


def test_session_create_manual_model_bypasses_auto_admission(monkeypatch):
    monkeypatch.setattr(
        server,
        "_admit_fleet_parent_session",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("manual model selection must bypass fleet admission")
        ),
    )

    response = server._methods["session.create"](
        "request-manual",
        {
            "cols": 80,
            "source": "desktop",
            "model_source": "manual",
            "model": "chosen-model",
            "provider": "chosen-provider",
        },
    )

    assert "error" not in response
    info = response["result"]["info"]
    assert info["model"] == "chosen-model"
    assert info["provider"] == "chosen-provider"
    assert info["model_source"] == "manual"
    assert info["fleet_selection_reason"] == "MANUAL_OVERRIDE"


def test_make_agent_exact_fleet_route_disables_all_fallback_and_priority(
    monkeypatch,
):
    captured: dict = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setitem(sys.modules, "run_agent", types.SimpleNamespace(AIAgent=FakeAgent))
    monkeypatch.setattr(
        "tui_gateway.synthetic_turn.maybe_build_synthetic_agent",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(server, "_load_cfg", lambda: {})
    monkeypatch.setattr(server, "_parse_tui_skills_env", lambda: [])
    monkeypatch.setattr(server, "_load_provider_routing", lambda: {})
    monkeypatch.setattr(
        server,
        "_resolve_runtime_with_fallback",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("exact fleet route must not enter fallback resolver")
        ),
    )
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **kwargs: {
            "provider": "xai-oauth",
            "requested_provider": "xai-oauth",
            "base_url": "https://api.x.ai/v1",
            "api_key": "oauth-access-token",
            "api_mode": "codex_responses",
            "source": "credential_pool",
        },
    )

    server._make_agent(
        "sid",
        "stored-root",
        model_override={"model": "grok-4.5", "provider": "xai-oauth"},
        service_tier_override="priority",
        exact_route={
            "model": "grok-4.5",
            "provider": "xai-oauth",
            "fleet_lane_id": "grok",
            "fleet_route_identity": "sha256:route",
        },
    )

    assert captured["model"] == "grok-4.5"
    assert captured["provider"] == "xai-oauth"
    assert captured["fallback_model"] is None
    assert captured["service_tier"] is None
    assert captured["base_url"] == "https://api.x.ai/v1"


def test_first_activity_persists_safe_fleet_lineage_and_route_metadata(
    monkeypatch,
):
    captured: dict = {}

    class FakeDb:
        def create_session(self, session_id, **kwargs):
            captured["session_id"] = session_id
            captured.update(kwargs)

    route = {
        "model_source": "fleet_auto",
        "fleet_lineage_root_id": "stored-root",
        "fleet_lane_id": "grok",
        "fleet_adapter_kind": "native_provider",
        "fleet_route_purpose": "desktop_parent",
        "fleet_selection_reason": "ROTATION",
        "fleet_pin_status": "pinned",
        "fleet_route_identity": "sha256:route",
        "model": "grok-4.5",
        "provider": "xai-oauth",
        "reasoning_effort": "max",
        "fast": False,
        "display_label": "Grok · Fleet",
    }
    monkeypatch.setattr(server, "_get_db", lambda: FakeDb())

    server._ensure_session_db_row(
        {
            "session_key": "stored-root",
            "source": "desktop",
            "model_override": {
                "model": "grok-4.5",
                "provider": "xai-oauth",
            },
            "fleet_parent_route": route,
            "model_source": "fleet_auto",
            "explicit_cwd": False,
        }
    )

    model_config = captured["model_config"]
    assert model_config["fleet_lineage_root_id"] == "stored-root"
    assert model_config["fleet_lane_id"] == "grok"
    assert model_config["fleet_adapter_kind"] == "native_provider"
    assert model_config["fleet_route_identity"] == "sha256:route"
    assert model_config["model_source"] == "fleet_auto"
    assert "qualification_evidence_hash" not in model_config


def test_session_info_keeps_authoritative_fleet_route_after_agent_build(
    monkeypatch,
):
    route = {
        "model_source": "fleet_auto",
        "fleet_lineage_root_id": "stored-root",
        "fleet_lane_id": "grok",
        "fleet_adapter_kind": "native_provider",
        "fleet_route_purpose": "desktop_parent",
        "fleet_selection_reason": "ROTATION",
        "fleet_pin_status": "pinned",
        "fleet_route_identity": "sha256:route",
        "model": "grok-4.5",
        "provider": "xai-oauth",
        "reasoning_effort": "max",
        "fast": False,
        "display_label": "Grok · Fleet",
    }
    agent = types.SimpleNamespace(
        model="grok-4.5",
        provider="xai-oauth",
        reasoning_config={"enabled": True, "effort": "max"},
        service_tier=None,
        session_id="stored-root",
        tools=[],
    )
    monkeypatch.setattr(server, "_load_cfg", lambda: {})

    info = server._session_info(
        agent,
        {
            "session_key": "stored-root",
            "fleet_parent_route": route,
            "running": False,
        },
    )

    assert info["model_source"] == "fleet_auto"
    assert info["fleet_lane_id"] == "grok"
    assert info["fleet_route_identity"] == "sha256:route"
    assert info["display_label"] == "Grok · Fleet"


def test_resume_rebuilds_route_from_authoritative_lineage_pin():
    pin = _pin()

    class FakeStore:
        def read_parent_pin(self, profile_id, lineage_root_id):
            assert profile_id == "default"
            assert lineage_root_id == "stored-root"
            return pin

    route = fleet_parent.restore_parent_route(
        {
            "model_config": {
                "model_source": "fleet_auto",
                "fleet_lineage_root_id": "stored-root",
                "fleet_route_identity": "sha256:route",
            }
        },
        profile_id="default",
        store=FakeStore(),
    )

    assert route is not None
    assert route["model"] == "grok-4.5"
    assert route["provider"] == "xai-oauth"
    assert route["fleet_lineage_root_id"] == "stored-root"


def test_compression_tip_resume_resolves_original_lineage_pin():
    pin = _pin()
    reads: list[tuple[str, str]] = []

    class FakeStore:
        def read_parent_pin(self, profile_id, lineage_root_id):
            reads.append((profile_id, lineage_root_id))
            return pin

    route = fleet_parent.restore_parent_route(
        {
            "id": "compressed-tip",
            "model_config": {
                "model_source": "fleet_auto",
                "fleet_lineage_root_id": "stored-root",
                "fleet_route_identity": "sha256:route",
            },
        },
        profile_id="default",
        store=FakeStore(),
    )

    assert reads == [("default", "stored-root")]
    assert route is not None
    assert route["fleet_lineage_root_id"] == "stored-root"
    assert route["fleet_route_identity"] == "sha256:route"
    assert route["model"] == "grok-4.5"


def test_session_info_event_uses_owning_transport_with_route_truth(monkeypatch):
    frames: list[dict] = []

    class CaptureTransport:
        def write(self, frame):
            frames.append(frame)
            return True

        def close(self):
            return None

    route = {
        **fleet_parent.parent_route_metadata(_pin()),
        "fleet_profile_id": "default",
    }
    session = {
        "agent": None,
        "fleet_parent_route": route,
        "model_source": "fleet_auto",
        "session_key": "stored-root",
        "transport": CaptureTransport(),
    }
    server._sessions["transport-route"] = session
    monkeypatch.setattr(server, "_load_cfg", lambda: {})

    agent = types.SimpleNamespace(
        model="grok-4.5",
        provider="xai-oauth",
        reasoning_config={"enabled": True, "effort": "max"},
        service_tier=None,
        session_id="stored-root",
        tools=[],
    )
    server._emit(
        "session.info",
        "transport-route",
        server._session_info(agent, session),
    )

    assert len(frames) == 1
    frame = frames[0]
    assert frame["params"]["session_id"] == "transport-route"
    assert frame["params"]["payload"]["model_source"] == "fleet_auto"
    assert frame["params"]["payload"]["fleet_lane_id"] == "grok"
    assert frame["params"]["payload"]["fleet_route_identity"] == "sha256:route"
    assert frame["params"]["payload"]["display_label"] == "Grok · Fleet"


def test_compute_host_turn_frame_carries_exact_parent_route():
    route = {
        **fleet_parent.parent_route_metadata(_pin()),
        "fleet_profile_id": "default",
    }
    session = {
        "history_lock": threading.Lock(),
        "history": [],
        "history_version": 0,
        "attached_images": [],
        "session_key": "stored-root",
        "cols": 80,
        "cwd": str(Path.cwd()),
        "profile_home": "",
        "model_override": {
            "model": "grok-4.5",
            "provider": "xai-oauth",
        },
        "create_reasoning_override": {"enabled": True, "effort": "max"},
        "create_service_tier_override": "",
        "fleet_parent_route": route,
        "source": "desktop",
    }

    frame = server._compute_host_turn_frame(
        "request-transport",
        "runtime-sid",
        session,
        "hello",
    )

    assert frame["fleet_parent_route"] == route
    assert frame["model_override"] == {
        "model": "grok-4.5",
        "provider": "xai-oauth",
    }
    assert frame["service_tier_override"] == ""


def test_parent_turn_guard_releases_lease_and_cools_future_admissions_on_failure():
    pin = _pin()
    lease = ParentLeaseHandle(
        profile_id="default",
        lineage_root_id="stored-root",
        lane_id="grok",
        owner_uuid="owner",
        generation=1,
        reserved_pct=Decimal("5.000"),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
    )
    calls: list[tuple] = []

    class FakeStore:
        def release_parent_turn(self, handle, *, outcome, now):
            calls.append(("release", handle, outcome))
            return True

        def set_cooldown(self, lane_id, *, until, reason, now):
            calls.append(("cooldown", lane_id, reason, until > now))

    class FakeConfig:
        default_reservation_pct = Decimal("5.000")
        lease_ttl_seconds = 1800

    class FakeService:
        config = FakeConfig()
        store = FakeStore()

        def acquire_parent_turn(self, **kwargs):
            return ParentTurnAcquisition(
                reason=ReasonCode.MET,
                pin=pin,
                lease=lease,
            )

    guard = fleet_parent.acquire_parent_turn_guard(
        {
            **fleet_parent.parent_route_metadata(pin),
            "fleet_profile_id": "default",
        },
        cwd=Path.cwd(),
        service=FakeService(),
        start_heartbeat=False,
    )
    guard.release(outcome="failed", provider_failure=True)

    assert calls[0] == ("release", lease, "failed")
    assert calls[1][:3] == ("cooldown", "grok", "PARENT_PROVIDER_FAILURE")
    assert calls[1][3] is True


def test_fleet_pinned_session_rejects_mid_session_model_switch():
    session = {"fleet_parent_route": fleet_parent.parent_route_metadata(_pin())}

    with pytest.raises(ValueError, match="Fleet-pinned session"):
        server._apply_model_switch("sid", session, "gpt-5")


def test_prompt_submit_uses_parent_turn_guard_and_skips_config_swap(
    monkeypatch, tmp_path
):
    class InlineThread:
        def __init__(self, target=None, daemon=None, args=(), kwargs=None):
            self._target = target
            self._args = args
            self._kwargs = kwargs or {}

        def start(self):
            self._target(*self._args, **self._kwargs)

    events: list[tuple] = []
    sync_calls: list[str] = []

    class Guard:
        def release(self, *, outcome, provider_failure):
            events.append(("release", outcome, provider_failure))

    monkeypatch.setattr(server.threading, "Thread", InlineThread)
    monkeypatch.setattr(server, "_emit", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_wire_callbacks", lambda sid: None)
    monkeypatch.setattr(
        server,
        "_sync_agent_model_with_config",
        lambda sid, session: sync_calls.append(sid),
    )
    monkeypatch.setattr(server, "_session_cwd", lambda session: str(tmp_path))
    monkeypatch.setattr(server, "_register_session_cwd", lambda session: None)
    monkeypatch.setattr(server, "_tts_stream_begin", lambda: None)
    monkeypatch.setattr(
        server, "_sync_session_key_after_compress", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(server, "_get_usage", lambda agent: {})
    monkeypatch.setattr(
        fleet_parent,
        "acquire_parent_turn_guard",
        lambda route, cwd: events.append(("acquire", route["model"], cwd))
        or Guard(),
    )

    agent = types.SimpleNamespace(
        session_id="stored-root",
        model="grok-4.5",
        provider="xai-oauth",
        clear_interrupt=lambda: None,
        run_conversation=lambda *args, **kwargs: {"final_response": "ok"},
    )
    session = {
        "agent": agent,
        "session_key": "stored-root",
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": True,
        "attached_images": [],
        "image_counter": 0,
        "cols": 80,
        "slash_worker": None,
        "show_reasoning": False,
        "tool_progress_mode": "all",
        "inflight_turn": None,
        "fleet_parent_route": fleet_parent.parent_route_metadata(_pin()),
    }

    server._run_prompt_submit("rid", "sid", session, "hello")

    assert events[0] == ("acquire", "grok-4.5", tmp_path)
    assert events[-1] == ("release", "complete", False)
    assert sync_calls == []


def test_no_eligible_parent_fails_closed_without_session_or_build(monkeypatch):
    built: list[str] = []
    monkeypatch.setattr(
        server,
        "_admit_fleet_parent_session",
        lambda **kwargs: ParentAdmission(
            reason=ReasonCode.NO_ELIGIBLE_LANE,
            pin=None,
        ),
    )
    monkeypatch.setattr(
        server,
        "_schedule_agent_build",
        lambda sid: built.append(sid),
    )

    response = server._methods["session.create"](
        "request-denied",
        {
            "source": "desktop",
            "model_source": "fleet_auto",
        },
    )

    assert response["error"]["code"] == 5033
    assert "NO_ELIGIBLE_LANE" in response["error"]["message"]
    assert server._sessions == {}
    assert built == []


@pytest.mark.parametrize(
    ("provider", "runtime_patch", "message"),
    [
        (
            "xai-oauth",
            {"source": "env:XAI_API_KEY"},
            "auth-source substitution",
        ),
        (
            "xai-oauth",
            {"base_url": "https://proxy.example/v1"},
            "custom endpoint",
        ),
        (
            "xai-oauth",
            {"provider": "openrouter"},
            "provider substitution",
        ),
    ],
)
def test_exact_runtime_rejects_api_key_custom_endpoint_and_provider_substitution(
    monkeypatch,
    provider,
    runtime_patch,
    message,
):
    runtime = {
        "provider": provider,
        "requested_provider": provider,
        "base_url": "https://api.x.ai/v1",
        "api_key": "oauth-access-token",
        "api_mode": "codex_responses",
        "source": "credential_pool",
        **runtime_patch,
    }
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **kwargs: runtime,
    )

    with pytest.raises(RuntimeError, match=message):
        fleet_parent.resolve_exact_native_runtime(
            {"provider": provider, "model": "grok-4.5"}
        )


def test_exact_claude_parent_uses_only_live_claude_code_oauth(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("Claude parent must not use the generic runtime resolver")
        ),
    )
    monkeypatch.setattr(
        "agent.anthropic_adapter.read_claude_code_credentials",
        lambda: {
            "accessToken": "sk-ant-oat01-live-claude-code",
            "expiresAt": 4_102_444_800_000,
            "source": "claude_code_credentials_file",
        },
    )

    runtime = fleet_parent.resolve_exact_native_runtime(
        {"provider": "anthropic", "model": "claude-opus-4-8"}
    )

    assert runtime == {
        "provider": "anthropic",
        "requested_provider": "anthropic",
        "base_url": "https://api.anthropic.com",
        "api_key": "sk-ant-oat01-live-claude-code",
        "api_mode": "anthropic_messages",
        "source": "claude_code_credentials_file",
    }


def test_exact_claude_parent_never_falls_back_to_anthropic_api_key(
    monkeypatch,
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "paid-api-key-must-not-be-used")
    monkeypatch.setattr(
        "agent.anthropic_adapter.read_claude_code_credentials",
        lambda: None,
    )

    with pytest.raises(RuntimeError, match="Claude Code OAuth credential"):
        fleet_parent.resolve_exact_native_runtime(
            {"provider": "anthropic", "model": "claude-opus-4-8"}
        )


def test_native_claude_parent_route_has_truthful_fleet_label():
    route = fleet_parent.parent_route_metadata(
        _pin(
            lane_id="claude_code",
            provider_id="anthropic",
            model_id="claude-opus-4-8",
        )
    )

    assert route["provider"] == "anthropic"
    assert route["fleet_adapter_kind"] == "native_provider"
    assert route["display_label"] == "Claude · Fleet"


def test_external_antigravity_parent_has_truthful_route_label():
    pin = _pin(
        lane_id="antigravity",
        provider_id="antigravity-subscription",
        model_id="gemini-3.1-pro-high",
    )
    pin = ParentPin(
        **{
            **pin.__dict__,
            "adapter_kind": AdapterKind.EXTERNAL_CLI,
        }
    )

    route = fleet_parent.parent_route_metadata(pin)

    assert route["provider"] == "antigravity-subscription"
    assert route["fleet_adapter_kind"] == "external_cli"
    assert (
        route["display_label"]
        == "Antigravity · Gemini 3.1 Pro High · external CLI"
    )


def test_external_antigravity_label_survives_the_session_info_api(monkeypatch):
    pin = _pin(
        lane_id="antigravity",
        provider_id="antigravity-subscription",
        model_id="gemini-3.1-pro-high",
    )
    pin = ParentPin(
        **{
            **pin.__dict__,
            "adapter_kind": AdapterKind.EXTERNAL_CLI,
        }
    )
    route = fleet_parent.parent_route_metadata(pin)
    agent = types.SimpleNamespace(
        model="gemini-3.1-pro-high",
        provider="antigravity-subscription",
        reasoning_config={"enabled": True, "effort": "max"},
        service_tier=None,
        session_id="stored-root",
        tools=[],
    )
    monkeypatch.setattr(server, "_load_cfg", lambda: {})

    info = server._session_info(
        agent,
        {
            "session_key": "stored-root",
            "fleet_parent_route": route,
            "running": False,
        },
    )

    assert (
        info["display_label"]
        == "Antigravity · Gemini 3.1 Pro High · external CLI"
    )
    assert info["model_source"] == "fleet_auto"
    assert info["fleet_adapter_kind"] == "external_cli"


def test_external_kimi_route_remains_parent_ineligible():
    pin = _pin(
        lane_id="kimi",
        provider_id="kimi-subscription",
        model_id="external",
    )
    pin = ParentPin(
        **{
            **pin.__dict__,
            "adapter_kind": AdapterKind.EXTERNAL_CLI,
        }
    )

    with pytest.raises(ValueError, match="unsupported external fleet parent"):
        fleet_parent.parent_route_metadata(pin)


def test_make_agent_uses_distinct_external_parent_driver(monkeypatch, tmp_path):
    expected = object()
    route = {
        "model_source": "fleet_auto",
        "fleet_profile_id": "default",
        "fleet_lineage_root_id": "lineage-antigravity",
        "fleet_lane_id": "antigravity",
        "fleet_adapter_kind": "external_cli",
        "fleet_route_purpose": "desktop_parent",
        "fleet_route_identity": "sha256:route",
        "model": "gemini-3.1-pro-high",
        "provider": "antigravity-subscription",
        "reasoning_effort": "medium",
        "display_label": "Antigravity · Gemini 3.1 Pro High · external CLI",
    }
    build = lambda **kwargs: expected
    monkeypatch.setattr(
        "tui_gateway.external_parent.build_external_parent_driver",
        build,
    )

    agent = server._make_agent(
        "live-id",
        "stored-id",
        session_id="stored-id",
        session_db=object(),
        model_override={
            "model": "gemini-3.1-pro-high",
            "provider": "antigravity-subscription",
        },
        exact_route=route,
        cwd_override=str(tmp_path),
    )

    assert agent is expected


def test_stale_composer_sonnet_default_is_forced_to_backend_admission(monkeypatch):
    events: list[str] = []

    def admit(**kwargs):
        events.append("admit")
        pin = _pin(
            lane_id="chatgpt_codex",
            provider_id="openai-codex",
            model_id="gpt-5.6-sol",
        )
        return ParentAdmission(
            reason=ReasonCode.MET,
            pin=ParentPin(
                **{
                    **pin.__dict__,
                    "lineage_root_id": kwargs["lineage_root_id"],
                    "session_id": kwargs["session_id"],
                }
            ),
        )

    monkeypatch.setattr(server, "_fleet_parent_gates_open", lambda **kwargs: True)
    monkeypatch.setattr(server, "_admit_fleet_parent_session", admit)
    monkeypatch.setattr(
        server,
        "_schedule_agent_build",
        lambda sid: events.append("build"),
    )

    response = server._methods["session.create"](
        "request-stale-sonnet",
        {
            "cols": 80,
            "source": "desktop",
            "model_source": "default",
            "model": "claude-sonnet-4-6",
            "provider": "anthropic",
            "reasoning_effort": "max",
        },
    )

    assert events == ["admit", "build"]
    assert "error" not in response
    result = response["result"]
    info = result["info"]
    assert info["model"] == "gpt-5.6-sol"
    assert info["provider"] == "openai-codex"
    assert info["model_source"] == "fleet_auto"
    assert info["fleet_lane_id"] == "chatgpt_codex"
    assert info["display_label"] == "Codex · Fleet"
    session = server._sessions[result["session_id"]]
    assert session["model_override"] == {
        "model": "gpt-5.6-sol",
        "provider": "openai-codex",
    }


def test_manual_sonnet_is_rejected_when_fleet_parent_is_on(monkeypatch):
    monkeypatch.setattr(server, "_fleet_parent_gates_open", lambda **kwargs: True)
    monkeypatch.setattr(
        server,
        "_admit_fleet_parent_session",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("sonnet manual must not reach admission")
        ),
    )

    response = server._methods["session.create"](
        "request-manual-sonnet",
        {
            "source": "desktop",
            "model_source": "manual",
            "model": "claude-sonnet-4-6",
            "provider": "anthropic",
        },
    )

    assert response["error"]["code"] == 4004
    assert "Sonnet" in response["error"]["message"]
    assert "claude-opus-4-8" in response["error"]["message"]
    assert server._sessions == {}


def test_resume_pin_stability_ignores_stale_composer_model():
    pin = _pin(
        lane_id="chatgpt_codex",
        provider_id="openai-codex",
        model_id="gpt-5.6-sol",
    )

    class FakeStore:
        def read_parent_pin(self, profile_id, lineage_root_id):
            return pin

    route = fleet_parent.restore_parent_route(
        {
            "model_config": {
                "model_source": "fleet_auto",
                "fleet_lineage_root_id": "stored-root",
                "fleet_route_identity": "sha256:route",
                # Stale UI-ish fields must not reselect Sonnet on resume.
                "model": "claude-sonnet-4-6",
                "provider": "anthropic",
            }
        },
        profile_id="default",
        store=FakeStore(),
    )

    assert route is not None
    assert route["model"] == "gpt-5.6-sol"
    assert route["provider"] == "openai-codex"
    assert route["fleet_lane_id"] == "chatgpt_codex"
