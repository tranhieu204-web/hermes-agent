from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli.fleet.adapters.live_routes import (
    AntigravityAdapter,
    ClaudeCodeAdapter,
    live_adapters,
)
from hermes_cli.fleet.adapters.native_provider import NativeProviderAdapter
from hermes_cli.fleet.capacity import BridgeUsageAdapter
from hermes_cli.fleet.live import FleetQualificationDoctor, _PROOF_CACHE_SCHEMA
from hermes_cli.fleet.profiles import profile_map
from hermes_cli.fleet.types import AdapterRequest, OverageState, Qualification, TaskSpec
from hermes_cli.subcommands.fleet import _default_service


NOW = datetime(2026, 7, 24, tzinfo=timezone.utc)
DISPLAY_MODEL_LABEL = "Gemini 3.1 Pro (High)"
MISMATCH_MODEL_LABEL = "Gemini 3.6 Flash (High)"
CANONICAL_MODEL_ID = "gemini-3.1-pro-high"
_LIVE_RECEIPT_MATCH = (
    "Starting new conversation\n"
    "Created conversation 11111111-1111-1111-1111-111111111111\n"
    "authMethod=consumer\n"
    "daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent\n"
    "I0724 11:02:16.509256 40296 model_config_manager.go:272] "
    f'Propagating selected model override to backend: label="{DISPLAY_MODEL_LABEL}"\n'
)
_LIVE_RECEIPT_MISMATCH = (
    "authMethod=consumer\n"
    "daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent\n"
    "I0724 11:02:16.509256 40296 model_config_manager.go:272] "
    f'Propagating selected model override to backend: label="{MISMATCH_MODEL_LABEL}"\n'
)


def _agy_version_models_command(stdout_models: str = CANONICAL_MODEL_ID):
    def run(argv):
        if argv[1] == "--version":
            return 0, "agy 1.1.6", ""
        if argv[1] == "models":
            return 0, stdout_models, ""
        raise AssertionError(argv)

    return run


def _claude_probe_stdout(model_id: str, *, result: str = "pong") -> str:
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": result,
            "session_id": "22222222-2222-2222-2222-222222222222",
            "num_turns": 1,
            "modelUsage": {model_id: {}},
        }
    )


def _probe_process_factory(log_text: str, *, returncode: int = 0, stdout: str = "pong"):
    """Serve both external live probes: agy (log receipt) and claude (JSON)."""

    def process(argv, **_kwargs):
        if "--log-file" not in argv:
            model_id = argv[argv.index("--model") + 1]
            return SimpleNamespace(
                returncode=returncode,
                stdout=_claude_probe_stdout(model_id),
                stderr="",
            )
        log_path = Path(argv[argv.index("--log-file") + 1])
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(log_text, encoding="utf-8")
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    return process


def test_live_doctor_qualifies_exact_subscription_routes_from_receipts(tmp_path, monkeypatch):
    profiles = profile_map()
    commands = []
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))

    def run(argv):
        commands.append(tuple(argv))
        executable = Path(argv[0]).stem
        if executable == "claude" and tuple(argv[1:3]) == ("auth", "status"):
            return 0, (
                '{"loggedIn":true,"authMethod":"claude.ai",'
                '"apiProvider":"firstParty","email":"never-record@example.com",'
                '"org":"never-record"}'
            ), ""
        if executable == "claude":
            return 0, "2.1.217 (Claude Code)", ""
        if executable == "agy" and argv[1] == "--version":
            return 0, "agy 1.2.3", ""
        if executable == "agy" and argv[1] == "models":
            return 0, "gemini-3.1-pro-high\nGemini 3.6 Flash", ""
        raise AssertionError(argv)

    doctor = FleetQualificationDoctor(
        auth_status=lambda provider: {
            "logged_in": True,
            "auth_mode": "chatgpt" if provider == "openai-codex" else "oauth_device_code",
            "source": "pool:test",
        },
        claude_oauth_status=lambda: {
            "logged_in": True,
            "auth_mode": "claude_code_oauth",
            "source": "claude_code_credentials_file",
        },
        which=lambda name: f"C:/tools/{name}.exe",
        command=run,
        run_process=_probe_process_factory(_LIVE_RECEIPT_MATCH),
        environment={},
        now=lambda: NOW,
        proof_cache_dir=home / "fleet" / "evidence" / "agy",
    )

    qualifications = doctor.qualify(profiles.values())

    assert qualifications["chatgpt_codex"].qualified
    assert (
        qualifications["chatgpt_codex"].auth_source
        == "openai-codex:oauth_subscription"
    )
    assert qualifications["chatgpt_codex"].models == ("gpt-5.6-sol",)
    assert qualifications["chatgpt_codex"].efforts[-2:] == ("max", "ultra")
    assert qualifications["chatgpt_codex"].subscription_only_proven is True
    assert qualifications["chatgpt_codex"].paid_fallback_absent is True
    assert qualifications["chatgpt_codex"].overage_disabled is False
    assert qualifications["chatgpt_codex"].overage_state is OverageState.UNKNOWN
    assert qualifications["grok"].models == ("grok-4.5",)
    assert qualifications["grok"].efforts[-2:] == ("max", "ultra")
    assert qualifications["claude_code"].qualified
    assert qualifications["claude_code"].models == (
        "claude-fable-5",
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-haiku-4-5",
    )
    assert qualifications["claude_code"].efforts == (
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    )
    assert qualifications["claude_code"].parent_session_proven is True
    assert qualifications["claude_code"].fast_off_supported
    assert "never-record" not in qualifications["claude_code"].detail
    assert qualifications["antigravity"].models == ("gemini-3.1-pro-high",)
    assert qualifications["antigravity"].efforts == ("low", "medium", "high")
    assert qualifications["antigravity"].qualified
    assert qualifications["antigravity"].parent_session_proven is True
    # claude is version-checked only — never a `models`/`auth` subcommand (a
    # bare `claude models` would run a billed inference on the word "models").
    claude_commands = [
        command for command in commands if Path(command[0]).stem == "claude"
    ]
    assert len(claude_commands) == 1
    assert Path(claude_commands[0][0]).name == "claude.exe"
    assert claude_commands[0][1:] == ("--version",)
    assert any(command[1:] == ("models",) for command in commands)
    assert not any(Path(command[0]).stem == "agy" and "auth" in command for command in commands)
    assert "served-model receipt" in qualifications["antigravity"].detail  # prose pinned out-of-lease; see live.py note


def test_live_doctor_requires_exact_agy_model_list_qualification():
    def run(argv):
        if argv[1] == "--version":
            return 0, "agy 1.2.3", ""
        if argv[1] == "models":
            return 0, "gemini-3.6-flash", ""
        raise AssertionError(argv)

    doctor = FleetQualificationDoctor(
        which=lambda _: "C:/tools/agy.exe",
        command=run,
        environment={},
        now=lambda: NOW,
    )

    qualification = doctor.qualify((profile_map()["antigravity"],))[
        "antigravity"
    ]

    assert not qualification.qualified
    assert qualification.detail == "required exact model absent from agy models"


def test_live_doctor_discovers_native_windows_agy_outside_path(tmp_path, monkeypatch):
    agy = tmp_path / "agy" / "bin" / "agy.exe"
    agy.parent.mkdir(parents=True)
    agy.write_bytes(b"qualified executable")
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    commands = []

    def run(argv):
        commands.append(tuple(argv))
        if argv[1] == "--version":
            return 0, "agy 1.2.3", ""
        if argv[1] == "models":
            return 0, "gemini-3.1-pro-high", ""
        raise AssertionError(argv)

    doctor = FleetQualificationDoctor(
        which=lambda _: None,
        command=run,
        run_process=_probe_process_factory(_LIVE_RECEIPT_MATCH),
        environment={"LOCALAPPDATA": str(tmp_path)},
        now=lambda: NOW,
        platform_name="nt",
        proof_cache_dir=home / "fleet" / "evidence" / "agy",
    )

    qualification = doctor.qualify((profile_map()["antigravity"],))[
        "antigravity"
    ]

    assert qualification.qualified
    assert qualification.executable == str(agy.resolve())
    assert commands == [
        (str(agy.resolve()), "--version"),
        (str(agy.resolve()), "models"),
    ]
    assert live_adapters(qualifications={"antigravity": qualification})[
        "antigravity"
    ].executable == str(agy.resolve())


def test_live_doctor_rejects_forbidden_api_key_without_exposing_value():
    doctor = FleetQualificationDoctor(
        auth_status=lambda _: {"logged_in": True, "auth_mode": "chatgpt", "source": "pool:test"},
        which=lambda name: f"C:/tools/{name}.exe",
        command=lambda argv: (0, "ok", ""),
        environment={"OPENAI_API_KEY": "do-not-expose-this"},
        now=lambda: NOW,
    )

    qualification = doctor.qualify((profile_map()["chatgpt_codex"],))["chatgpt_codex"]

    assert not qualification.qualified
    assert "OPENAI_API_KEY" in qualification.detail
    assert "do-not-expose-this" not in qualification.detail


def test_live_claude_adapter_executes_cli_and_never_calls_native_inference(tmp_path):
    native_calls = []

    def forbidden_native_runner(**kwargs):
        native_calls.append(kwargs)
        raise AssertionError("native Anthropic inference must remain disabled")

    profile = profile_map()["claude_code"]
    executable = str(Path(sys.executable).resolve())
    qualification = Qualification(
        qualified=True,
        captured_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        auth_kind="cli_subscription",
        auth_source="anthropic:claude-plan-cli-live-receipt",
        overage_disabled=True,
        provider_id="anthropic",
        models=profile.ordered_models,
        efforts=profile.supported_efforts,
        fast_off_supported=True,
        capabilities=profile.capabilities,
        executable=executable,
        version="synthetic-claude-code",
        evidence_id="synthetic-cli-qualification",
        subscription_only_proven=True,
        paid_fallback_absent=True,
        overage_state=OverageState.OFF,
    )
    adapters = live_adapters(
        native_runner=forbidden_native_runner,
        qualifications={"claude_code": qualification},
    )
    adapter = adapters["claude_code"]
    assert isinstance(adapter, ClaudeCodeAdapter)
    assert not isinstance(adapter, NativeProviderAdapter)

    process_calls = []

    def process(argv, **kwargs):
        process_calls.append((argv, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=_claude_probe_stdout(
                profile.ordered_models[0], result="claude CLI complete"
            ),
            stderr="",
        )

    adapter._run_process = process
    request = AdapterRequest(
        task_id="claude-cli-isolation",
        cwd=tmp_path,
        prompt="bounded Claude CLI task",
        profile=profile,
        model=profile.ordered_models[0],
        effort="high",
        timeout_seconds=17,
    )

    result = adapter.execute(request, qualification)

    assert result.ok
    assert result.output == "claude CLI complete"
    assert native_calls == []
    assert len(process_calls) == 1
    argv, kwargs = process_calls[0]
    assert argv == [
        executable,
        "-p",
        "--model",
        "claude-fable-5",
        "--effort",
        "high",
        "--output-format",
        "json",
    ]
    assert kwargs["input"] == "bounded Claude CLI task"
    assert kwargs["timeout"] == 17
    assert kwargs["shell"] is False


@pytest.mark.parametrize(
    "lane_id",
    ["chatgpt_codex", "claude_code", "grok", "antigravity"],
)
def test_default_service_qualifies_and_executes_each_live_lane(
    tmp_path, monkeypatch, lane_id
):
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    bridge = tmp_path / "usage-weekly.json"
    labels = {
        "chatgpt_codex": "ChatGPT Pro · Codex",
        "claude_code": "Claude Max 20x",
        "grok": "SuperGrok",
        "antigravity": "Google AI · Antigravity",
    }
    bridge.write_text(
        json.dumps(
            {
                "checked_at": "2026-07-24T00:00:00Z",
                "source": "controlled-test",
                "plans": [
                    {
                        "label": labels[lane_id],
                        "agents": [],
                        "weekly_pct_used": 10,
                        "resets": "weekly",
                        "checked_at": "2026-07-24T00:00:00Z",
                        "health_status": "UP",
                        "health_checked_at": "2026-07-24T00:00:00Z",
                        "comparability_group": "subscription-weekly",
                        "quota_window_id": "subscription-weekly",
                        "measurement_kind": "measured",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    def command(argv):
        if argv[1] == "--version":
            return 0, "2.1.217 (Claude Code)", ""
        if argv[1] == "auth":
            return 0, '{"loggedIn":true,"authMethod":"claude.ai","apiProvider":"firstParty"}', ""
        if argv[1] == "models":
            return 0, "gemini-3.1-pro-high", ""
        raise AssertionError(argv)

    doctor = FleetQualificationDoctor(
        auth_status=lambda provider: {
            "logged_in": True,
            "auth_mode": "chatgpt" if provider == "openai-codex" else "oauth_device_code",
            "source": "pool:test",
        },
        claude_oauth_status=lambda: {
            "logged_in": True,
            "auth_mode": "claude_code_oauth",
            "source": "claude_code_credentials_file",
        },
        which=lambda _: sys.executable,
        command=command,
        run_process=_probe_process_factory(_LIVE_RECEIPT_MATCH),
        environment={},
        billing_status=lambda _: {"overage_state": "off"},
        now=lambda: NOW,
        proof_cache_dir=home / "fleet" / "evidence" / "agy",
    )

    def native(**kwargs):
        auth_source = (
            "anthropic:claude_code_oauth"
            if kwargs["provider_id"] == "anthropic"
            else f"{kwargs['provider_id']}:oauth_subscription"
        )
        return {
            "ok": True,
            "provider_id": kwargs["provider_id"],
            "model_id": kwargs["model"],
            "effort": kwargs["effort"],
            "auth_kind": "oauth_subscription",
            "auth_source": auth_source,
            "fallback_enabled": False,
            "fast_mode": False,
            "output": "native complete",
        }

    process_calls = []

    def process(argv, **kwargs):
        process_calls.append(argv)
        if "--output-format" in argv:
            model = argv[argv.index("--model") + 1]
            stdout = _claude_probe_stdout(model, result="claude complete")
        else:
            log_path = Path(argv[argv.index("--log-file") + 1])
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(_LIVE_RECEIPT_MATCH, encoding="utf-8")
            stdout = "antigravity complete"
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    adapters = {
        "chatgpt_codex": NativeProviderAdapter(native),
        "claude_code": ClaudeCodeAdapter(sys.executable, run_process=process),
        "grok": NativeProviderAdapter(native),
        "antigravity": AntigravityAdapter(sys.executable, run_process=process),
    }
    config = {
        "fleet": {
            "enabled": True,
            "bridge_usage_file": str(bridge),
            "lanes": {
                lane: {"enabled": lane == lane_id}
                for lane in profile_map()
            },
        }
    }
    service = _default_service(
        config_data=config,
        doctor=doctor,
        adapters=adapters,
        capacity_source=BridgeUsageAdapter(bridge),
        store_path=tmp_path / "state.db",
        now=lambda: NOW,
    )
    result = service.run(
        TaskSpec(
            task_id=f"task-{lane_id}",
            cwd=tmp_path,
            required_capabilities=frozenset({"workspace_write", "shell"}),
            reservation_pct=Decimal("5"),
        ),
        prompt="bounded test task",
    )

    assert result.ok
    assert result.pin is not None
    assert result.pin.lane_id == lane_id
    if lane_id == "claude_code":
        assert len(process_calls) == 1
        argv = process_calls[0]
        assert argv[:2] == [str(Path(sys.executable).resolve()), "-p"]
        assert argv[argv.index("--model") + 1] == "claude-fable-5"
        assert "--effort" in argv
        assert argv[-2:] == ["--output-format", "json"]
        assert result.adapter_result.adapter_kind.value == "external_cli"
        assert result.adapter_result.provider_id == "anthropic"
        assert result.adapter_result.output == "claude complete"
    if lane_id == "antigravity":
        assert len(process_calls) == 1
        argv = process_calls[0]
        assert argv[:3] == [
            str(Path(sys.executable).resolve()),
            "-p",
            "bounded test task",
        ]
        assert argv[argv.index("--model") + 1] == "Gemini 3.1 Pro (High)"
        assert "--effort" not in argv
        assert Path(argv[argv.index("--log-file") + 1]).is_absolute()
        assert argv[-2:] == ["--print-timeout", "1800s"]
        route_proof = result.adapter_result.metadata["route_proof"]
        assert route_proof["requested_model_id"] == "gemini-3.1-pro-high"
        # The AGY receipt is built from the client's own pre-flight
        # "Propagating selected model override" line, so it proves the
        # requested/selected model and NOT a served identity.  The ordinary
        # execute() proof keeps its historical served_model_* aliases (their
        # shape predates this candidate) but must now name the evidence class
        # truthfully alongside them.
        assert "model_qualification" not in route_proof
        assert route_proof["model_evidence_source"] == (
            "agy client model-override propagation log line"
        )
        assert route_proof["model_evidence_kind"] == "requested_selected_propagation"
        assert route_proof["served_model_evidence"] == "NOT_PROVEN"
        assert route_proof["served_model_proven"] is False
        assert route_proof["requested_selected_model_id"] == "gemini-3.1-pro-high"
        assert route_proof["requested_selected_model_label"] == "Gemini 3.1 Pro (High)"
        assert "served_model_id" not in route_proof
        assert "served_model_label" not in route_proof


def test_claude_lane_never_qualifies_without_live_plan_cli_receipt(
    tmp_path, monkeypatch
):
    """Executable/version/OAuth evidence alone must fail closed for claude."""

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))

    def failing_probe(argv, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="boom")

    doctor = FleetQualificationDoctor(
        claude_oauth_status=lambda: {
            "logged_in": True,
            "auth_mode": "claude_code_oauth",
            "source": "claude_code_credentials_file",
        },
        which=lambda _: "C:/tools/claude.exe",
        command=lambda argv: (0, "2.1.217 (Claude Code)", ""),
        run_process=failing_probe,
        environment={},
        now=lambda: NOW,
    )

    qualification = doctor.qualify((profile_map()["claude_code"],))[
        "claude_code"
    ]

    assert qualification.qualified is False
    assert qualification.models == ()
    assert "receipt" in qualification.detail.lower()


def test_claude_adapter_rejects_cli_error_envelope(tmp_path):
    """A payload with is_error/failed subtype must never ship as output."""

    profile = profile_map()["claude_code"]
    executable = str(Path(sys.executable).resolve())
    qualification = Qualification(
        qualified=True,
        captured_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        auth_kind="cli_subscription",
        auth_source="anthropic:claude-plan-cli-live-receipt",
        overage_disabled=True,
        provider_id="anthropic",
        models=profile.ordered_models,
        efforts=profile.supported_efforts,
        fast_off_supported=True,
        capabilities=profile.capabilities,
        executable=executable,
        version="synthetic-claude-code",
        evidence_id="synthetic-cli-qualification",
        subscription_only_proven=True,
        paid_fallback_absent=True,
        overage_state=OverageState.OFF,
    )
    adapter = ClaudeCodeAdapter(executable)
    adapter._run_process = lambda argv, **kwargs: SimpleNamespace(
        returncode=0,
        stdout=json.dumps(
            {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "result": "something broke",
                "session_id": "33333333-3333-3333-3333-333333333333",
                "modelUsage": {profile.ordered_models[0]: {}},
            }
        ),
        stderr="",
    )
    request = AdapterRequest(
        task_id="claude-cli-error-envelope",
        cwd=tmp_path,
        prompt="bounded Claude CLI task",
        profile=profile,
        model=profile.ordered_models[0],
        effort="high",
        timeout_seconds=17,
    )

    result = adapter.execute(request, qualification)

    assert not result.ok
    assert result.metadata["cli_receipt"]["status"] == "cli_reported_error"


def test_catalog_model_list_alone_never_qualifies_antigravity(tmp_path, monkeypatch):
    """Static `agy models` evidence must fail closed without a live receipt."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    doctor = FleetQualificationDoctor(
        which=lambda _: "C:/tools/agy.exe",
        command=_agy_version_models_command(f"{CANONICAL_MODEL_ID}\nGemini 3.6 Flash"),
        # No run_process / live probe injection: catalog-only must not qualify.
        environment={},
        now=lambda: NOW,
    )

    qualification = doctor.qualify((profile_map()["antigravity"],))["antigravity"]

    assert qualification.qualified is False
    assert qualification.models == ()
    assert qualification.efforts == ()
    assert qualification.capabilities == frozenset()
    assert qualification.parent_session_proven is False
    assert "live" in qualification.detail.lower() or "receipt" in qualification.detail.lower()
    blob = json.dumps(qualification.__dict__, default=str)
    assert "never-record" not in blob
    assert "sk-" not in blob


def test_live_served_model_label_mismatch_fails_closed_and_sanitized(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    secret = "AGY_SECRET_CANARY_live_mismatch"
    doctor = FleetQualificationDoctor(
        which=lambda _: "C:/tools/agy.exe",
        command=_agy_version_models_command(CANONICAL_MODEL_ID),
        run_process=_probe_process_factory(_LIVE_RECEIPT_MISMATCH + f"\n{secret}\n"),
        environment={},
        now=lambda: NOW,
        proof_cache_dir=home / "fleet" / "evidence" / "agy",
    )

    qualification = doctor.qualify((profile_map()["antigravity"],))["antigravity"]

    assert qualification.qualified is False
    assert qualification.models == ()
    assert qualification.efforts == ()
    assert qualification.capabilities == frozenset()
    assert qualification.parent_session_proven is False
    assert qualification.subscription_only_proven is False
    assert qualification.paid_fallback_absent is False
    assert qualification.detail == "live selected-model receipt mismatch"
    payload = json.dumps(qualification.__dict__, default=str)
    assert secret not in payload
    assert MISMATCH_MODEL_LABEL not in payload
    assert not list(home.rglob("*.log"))


def test_live_proof_cache_rejects_requested_identity_synthesized_as_served(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    evidence = home / "fleet" / "evidence" / "agy"
    executable = str(Path("C:/tools/agy.exe").resolve())
    evidence.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    (evidence / "doctor-live-proof.json").write_text(
        json.dumps(
            {
                "schema_version": _PROOF_CACHE_SCHEMA,
                "lane_id": "antigravity",
                "executable": executable,
                "version": "agy 1.1.6",
                "canonical_model_id": CANONICAL_MODEL_ID,
                "requested_selected_model_id": "gemini-3.6-flash-high",
                "requested_selected_model_label": MISMATCH_MODEL_LABEL,
                "status": "matched",
                "captured_at": NOW.isoformat(),
                "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    calls = []

    def process(argv, **kwargs):
        calls.append(tuple(argv))
        return _probe_process_factory(_LIVE_RECEIPT_MATCH)(argv, **kwargs)

    doctor = FleetQualificationDoctor(
        which=lambda _: executable,
        command=_agy_version_models_command(),
        run_process=process,
        environment={},
        now=lambda: NOW,
        proof_cache_dir=evidence,
    )

    qualification = doctor.qualify((profile_map()["antigravity"],))["antigravity"]

    assert qualification.qualified is True
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("auth_method", "other_auth", id="wrong-auth"),
        pytest.param("auth_method", None, id="missing-auth"),
        pytest.param("endpoint_kind", "other_endpoint", id="wrong-endpoint"),
        pytest.param("endpoint_kind", None, id="missing-endpoint"),
    ],
)
def test_live_proof_cache_requires_consumer_cloud_code_route(
    tmp_path, monkeypatch, field, value
):
    home = tmp_path / "home"
    evidence = home / "fleet" / "evidence" / "agy"
    executable = str(Path("C:/tools/agy.exe").resolve())
    evidence.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    payload = {
        "schema_version": _PROOF_CACHE_SCHEMA,
        "lane_id": "antigravity",
        "executable": executable,
        "version": "agy 1.1.6",
        "canonical_model_id": CANONICAL_MODEL_ID,
        "requested_selected_model_id": CANONICAL_MODEL_ID,
        "requested_selected_model_label": DISPLAY_MODEL_LABEL,
        "auth_method": "consumer",
        "endpoint_kind": "antigravity_cloud_code",
        "status": "matched",
        "captured_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
    }
    if value is None:
        payload.pop(field)
    else:
        payload[field] = value
    (evidence / "doctor-live-proof.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    calls = []

    def process(argv, **kwargs):
        calls.append(tuple(argv))
        return _probe_process_factory("")(argv, **kwargs)

    doctor = FleetQualificationDoctor(
        which=lambda _: executable,
        command=_agy_version_models_command(),
        run_process=process,
        environment={},
        now=lambda: NOW,
        proof_cache_dir=evidence,
    )

    qualification = doctor.qualify((profile_map()["antigravity"],))["antigravity"]

    assert len(calls) == 1
    assert qualification.qualified is False
    assert qualification.subscription_only_proven is False


def test_live_proof_cache_ignores_extra_models(tmp_path, monkeypatch):
    home = tmp_path / "home"
    evidence = home / "fleet" / "evidence" / "agy"
    executable = str(Path("C:/tools/agy.exe").resolve())
    evidence.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    payload = {
        "schema_version": _PROOF_CACHE_SCHEMA,
        "lane_id": "antigravity",
        "executable": executable,
        "version": "agy 1.1.6",
        "canonical_model_id": CANONICAL_MODEL_ID,
        "requested_selected_model_id": CANONICAL_MODEL_ID,
        "requested_selected_model_label": DISPLAY_MODEL_LABEL,
        "auth_method": "consumer",
        "endpoint_kind": "antigravity_cloud_code",
        "status": "matched",
        "qualified_models": [CANONICAL_MODEL_ID, "gemini-3.6-flash-high"],
        "captured_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
    }
    (evidence / "doctor-live-proof.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )

    def unexpected_command(argv):
        raise AssertionError(f"cache-only ran command: {argv}")

    def unexpected_process(argv, **_kwargs):
        raise AssertionError(f"cache-only ran provider: {argv}")

    doctor = FleetQualificationDoctor(
        which=lambda _: executable,
        command=unexpected_command,
        run_process=unexpected_process,
        environment={},
        now=lambda: NOW,
        proof_cache_dir=evidence,
    )

    profile = replace(
        profile_map()["antigravity"],
        ordered_models=(CANONICAL_MODEL_ID, "gemini-3.6-flash-high"),
    )
    qualification = doctor.qualify((profile,), allow_live_probe=False)["antigravity"]

    assert qualification.qualified is True
    assert qualification.models == (CANONICAL_MODEL_ID,)


def test_live_doctor_qualifies_only_the_model_proven_by_live_receipt(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    profile = replace(
        profile_map()["antigravity"],
        ordered_models=(CANONICAL_MODEL_ID, "gemini-3.6-flash-high"),
    )
    doctor = FleetQualificationDoctor(
        which=lambda _: "C:/tools/agy.exe",
        command=_agy_version_models_command(
            f"{CANONICAL_MODEL_ID}\ngemini-3.6-flash-high"
        ),
        run_process=_probe_process_factory(_LIVE_RECEIPT_MATCH),
        environment={},
        now=lambda: NOW,
        proof_cache_dir=home / "fleet" / "evidence" / "agy",
    )

    qualification = doctor.qualify((profile,))["antigravity"]

    assert qualification.qualified is True
    assert qualification.models == (CANONICAL_MODEL_ID,)

    # Warm-cache probe should match cold-cache behavior
    warm_qualification = doctor.qualify((profile,), allow_live_probe=False)["antigravity"]
    assert warm_qualification.qualified is True
    assert warm_qualification.models == (CANONICAL_MODEL_ID,)


@pytest.mark.parametrize(
    ("log_text", "returncode", "stdout", "detail_fragment"),
    [
        ("", 0, "pong", "missing"),
        ("unrelated line only", 0, "pong", "missing"),
        (
            "Propagating selected model override to backend: label=\n",
            0,
            "pong",
            "malformed",
        ),
        (
            _LIVE_RECEIPT_MATCH,
            0,
            "",
            "empty",
        ),
        (
            _LIVE_RECEIPT_MATCH,
            7,
            "pong",
            "nonzero",
        ),
        (
            (
                f'Propagating selected model override to backend: label="{DISPLAY_MODEL_LABEL}"\n'
                "daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent\n"
            ),
            0,
            "pong",
            "auth",
        ),
        (
            (
                f'Propagating selected model override to backend: label="{DISPLAY_MODEL_LABEL}"\n'
                "authMethod=oauth_service_account\n"
                "daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent\n"
            ),
            0,
            "pong",
            "auth",
        ),
        (
            (
                f'Propagating selected model override to backend: label="{DISPLAY_MODEL_LABEL}"\n'
                "authMethod=consumer\n"
            ),
            0,
            "pong",
            "endpoint",
        ),
        (
            _LIVE_RECEIPT_MATCH + "\nGEMINI_API_KEY=should-not-qualify\n",
            0,
            "pong",
            "api-key",
        ),
        (
            (
                f'Propagating selected model override to backend: label="{DISPLAY_MODEL_LABEL}"\n'
                f'Propagating selected model override to backend: label="{MISMATCH_MODEL_LABEL}"\n'
                "authMethod=consumer\n"
                "daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent\n"
            ),
            0,
            "pong",
            "mismatch",
        ),
    ],
)
def test_live_receipt_failure_modes_fail_closed(
    tmp_path, monkeypatch, log_text, returncode, stdout, detail_fragment
):
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    doctor = FleetQualificationDoctor(
        which=lambda _: "C:/tools/agy.exe",
        command=_agy_version_models_command(),
        run_process=_probe_process_factory(
            log_text, returncode=returncode, stdout=stdout
        ),
        environment={},
        now=lambda: NOW,
        proof_cache_dir=home / "fleet" / "evidence" / "agy",
    )

    qualification = doctor.qualify((profile_map()["antigravity"],))["antigravity"]

    assert qualification.qualified is False
    assert qualification.parent_session_proven is False
    assert qualification.models == ()
    assert detail_fragment in qualification.detail.lower()
    assert "GEMINI_API_KEY=should-not-qualify" not in qualification.detail
    assert not list(home.rglob("*.log"))


def test_live_exact_consumer_subscription_receipt_qualifies(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    calls = []

    def process(argv, **kwargs):
        calls.append(tuple(argv))
        return _probe_process_factory(_LIVE_RECEIPT_MATCH)(argv, **kwargs)

    doctor = FleetQualificationDoctor(
        which=lambda _: "C:/tools/agy.exe",
        command=_agy_version_models_command(),
        run_process=process,
        environment={},
        now=lambda: NOW,
        proof_cache_dir=home / "fleet" / "evidence" / "agy",
    )

    qualification = doctor.qualify((profile_map()["antigravity"],))["antigravity"]

    assert qualification.qualified is True
    assert qualification.models == (CANONICAL_MODEL_ID,)
    assert qualification.efforts == ("low", "medium", "high")
    assert qualification.parent_session_proven is True
    assert qualification.subscription_only_proven is True
    assert qualification.paid_fallback_absent is True
    assert qualification.auth_kind == "cli_subscription"
    assert "live" in qualification.detail.lower()
    assert "served-model receipt" in qualification.detail  # prose pinned out-of-lease
    assert len(calls) == 1
    argv = list(calls[0])
    assert argv[argv.index("--model") + 1] == DISPLAY_MODEL_LABEL
    assert "--log-file" in argv
    assert not list(home.rglob("*.log"))


def test_live_proof_cache_prevents_repeated_probe_within_ttl(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    calls = []

    def process(argv, **kwargs):
        calls.append(1)
        return _probe_process_factory(_LIVE_RECEIPT_MATCH)(argv, **kwargs)

    kwargs = dict(
        which=lambda _: "C:/tools/agy.exe",
        command=_agy_version_models_command(),
        run_process=process,
        environment={},
        now=lambda: NOW,
        proof_cache_dir=home / "fleet" / "evidence" / "agy",
    )
    first = FleetQualificationDoctor(**kwargs).qualify(
        (profile_map()["antigravity"],)
    )["antigravity"]
    second = FleetQualificationDoctor(**kwargs).qualify(
        (profile_map()["antigravity"],)
    )["antigravity"]

    def unexpected_command(argv):
        raise AssertionError(f"cache-only qualification ran command: {argv}")

    def unexpected_process(argv, **_kwargs):
        raise AssertionError(f"cache-only qualification ran provider: {argv}")

    cached_only = FleetQualificationDoctor(
        which=lambda _: "C:/tools/agy.exe",
        command=unexpected_command,
        run_process=unexpected_process,
        environment={},
        now=lambda: NOW,
        proof_cache_dir=home / "fleet" / "evidence" / "agy",
    ).qualify(
        (profile_map()["antigravity"],),
        allow_live_probe=False,
    )["antigravity"]

    assert first.qualified and second.qualified and cached_only.qualified
    assert cached_only.models == (CANONICAL_MODEL_ID,)
    assert len(calls) == 1
    cache_files = list((home / "fleet" / "evidence" / "agy").glob("*.json"))
    assert cache_files
    cache_blob = cache_files[0].read_text(encoding="utf-8")
    assert "authMethod=consumer" not in cache_blob
    assert "streamGenerateContent" not in cache_blob
    assert "pong" not in cache_blob


def test_live_doctor_timeout_and_unreadable_probe_fail_closed(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))

    def timeout_process(argv, **_kwargs):
        log_path = Path(argv[argv.index("--log-file") + 1])
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(_LIVE_RECEIPT_MATCH, encoding="utf-8")
        raise subprocess.TimeoutExpired(cmd=argv, timeout=1)

    doctor = FleetQualificationDoctor(
        which=lambda _: "C:/tools/agy.exe",
        command=_agy_version_models_command(),
        run_process=timeout_process,
        environment={},
        now=lambda: NOW,
        proof_cache_dir=home / "fleet" / "evidence" / "agy",
    )
    qualification = doctor.qualify((profile_map()["antigravity"],))["antigravity"]
    assert qualification.qualified is False
    assert "timeout" in qualification.detail.lower()
    assert not list(home.rglob("*.log"))


def test_serialized_doctor_marks_antigravity_ineligible_on_mismatch(
    tmp_path, monkeypatch
):
    from hermes_cli.fleet.inspection import (
        build_fleet_service,
        build_inspection_payload,
    )

    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    bridge = tmp_path / "usage.json"
    bridge.write_text(
        json.dumps(
            {
                "checked_at": "2026-07-24T00:00:00Z",
                "source": "controlled-test",
                "plans": [
                    {
                        "label": "Google AI · Antigravity",
                        "agents": [],
                        "weekly_pct_used": 10,
                        "resets": "weekly",
                        "checked_at": "2026-07-24T00:00:00Z",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    doctor = FleetQualificationDoctor(
        auth_status=lambda _provider: {"logged_in": False},
        claude_oauth_status=lambda: {"logged_in": False},
        which=lambda name: "C:/tools/agy.exe" if name == "agy" else None,
        command=_agy_version_models_command(),
        run_process=_probe_process_factory(_LIVE_RECEIPT_MISMATCH),
        environment={},
        now=lambda: NOW,
        proof_cache_dir=home / "fleet" / "evidence" / "agy",
    )
    service = build_fleet_service(
        config_data={
            "fleet": {
                "enabled": True,
                "parent_desktop_enabled": True,
                "bridge_usage_file": str(bridge),
                "lanes": {
                    "chatgpt_codex": {"enabled": False},
                    "claude_code": {"enabled": False},
                    "grok": {"enabled": False},
                    "antigravity": {"enabled": True},
                    "kimi": {"enabled": False},
                },
            }
        },
        doctor=doctor,
        adapters={"antigravity": object()},
        store_path=tmp_path / "fleet" / "state.db",
        now=lambda: NOW,
    )
    payload = build_inspection_payload(service, command="doctor")
    antigravity = next(
        item for item in payload["evaluations"] if item["lane_id"] == "antigravity"
    )
    assert antigravity["eligible"] is False
    assert antigravity["selectable"] is False
    assert antigravity["qualification_detail"] == "live selected-model receipt mismatch"


def test_mismatch_qualification_does_not_create_antigravity_pin(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    bridge = tmp_path / "usage-weekly.json"
    bridge.write_text(
        json.dumps(
            {
                "checked_at": "2026-07-24T00:00:00Z",
                "source": "controlled-test",
                "plans": [
                    {
                        "label": "Google AI · Antigravity",
                        "agents": [],
                        "weekly_pct_used": 10,
                        "resets": "weekly",
                        "checked_at": "2026-07-24T00:00:00Z",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    store_path = tmp_path / "state.db"
    doctor = FleetQualificationDoctor(
        auth_status=lambda _provider: {"logged_in": False},
        claude_oauth_status=lambda: {"logged_in": False},
        which=lambda _: sys.executable,
        command=_agy_version_models_command(),
        run_process=_probe_process_factory(_LIVE_RECEIPT_MISMATCH),
        environment={},
        billing_status=lambda _: {"overage_state": "off"},
        now=lambda: NOW,
        proof_cache_dir=home / "fleet" / "evidence" / "agy",
    )
    service = _default_service(
        config_data={
            "fleet": {
                "enabled": True,
                "bridge_usage_file": str(bridge),
                "lanes": {
                    lane: {"enabled": lane == "antigravity"} for lane in profile_map()
                },
            }
        },
        doctor=doctor,
        adapters={
            "antigravity": AntigravityAdapter(
                sys.executable, run_process=_probe_process_factory(_LIVE_RECEIPT_MATCH)
            )
        },
        store_path=store_path,
        now=lambda: NOW,
    )
    result = service.run(
        TaskSpec(
            task_id="task-antigravity-mismatch",
            cwd=tmp_path,
            required_capabilities=frozenset({"workspace_write", "shell"}),
            reservation_pct=Decimal("5"),
        ),
        prompt="bounded test task",
    )
    assert not result.ok
    assert result.pin is None
    summary = service.store.pin_state_summary()
    assert summary["task_worker"]["total"] == 0
    assert summary["task_worker"]["by_lane"].get("antigravity", 0) == 0


def test_existing_pin_records_unchanged_when_doctor_mismatches(
    tmp_path, monkeypatch
):
    from hermes_cli.fleet.state import FleetStore
    from hermes_cli.fleet.types import AdapterKind, TaskPin

    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    store_path = tmp_path / "state.db"
    store = FleetStore(store_path)
    existing = TaskPin(
        task_id="already-pinned",
        lane_id="antigravity",
        adapter_kind=AdapterKind.EXTERNAL_CLI,
        provider_id="antigravity-subscription",
        model_id=CANONICAL_MODEL_ID,
        effort="high",
        fast_mode=False,
        cwd_fingerprint="cwd-fp",
        status="pinned",
    )
    with store._connect() as conn:  # noqa: SLF001 - controlled unit seed
        conn.execute(
            """
            INSERT INTO tasks(
                task_id, lane_id, adapter_kind, provider_id, model_id, effort,
                fast_mode, cwd_fingerprint, status, created_at, updated_at,
                terminal_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                existing.task_id,
                existing.lane_id,
                existing.adapter_kind.value,
                existing.provider_id,
                existing.model_id,
                existing.effort,
                0,
                existing.cwd_fingerprint,
                existing.status,
                NOW.isoformat(),
                NOW.isoformat(),
                None,
            ),
        )
    before = store.read_pin("already-pinned")
    doctor = FleetQualificationDoctor(
        which=lambda _: "C:/tools/agy.exe",
        command=_agy_version_models_command(),
        run_process=_probe_process_factory(_LIVE_RECEIPT_MISMATCH),
        environment={},
        now=lambda: NOW,
        proof_cache_dir=home / "fleet" / "evidence" / "agy",
    )
    doctor.qualify((profile_map()["antigravity"],))
    after = store.read_pin("already-pinned")
    assert before is not None and after is not None
    assert after == before


def test_native_lanes_unchanged_when_antigravity_probe_injected(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    probe_calls = []

    def process(argv, **kwargs):
        probe_calls.append(argv)
        return _probe_process_factory(_LIVE_RECEIPT_MATCH)(argv, **kwargs)

    doctor = FleetQualificationDoctor(
        auth_status=lambda provider: {
            "logged_in": True,
            "auth_mode": "chatgpt" if provider == "openai-codex" else "oauth_device_code",
            "source": "pool:test",
        },
        claude_oauth_status=lambda: {
            "logged_in": True,
            "auth_mode": "claude_code_oauth",
            "source": "claude_code_credentials_file",
        },
        which=lambda name: f"C:/tools/{name}.exe",
        command=_agy_version_models_command(),
        run_process=process,
        environment={},
        now=lambda: NOW,
        proof_cache_dir=tmp_path / "home" / "fleet" / "evidence" / "agy",
    )
    qualifications = doctor.qualify(profile_map().values())
    assert qualifications["chatgpt_codex"].qualified is True
    assert qualifications["claude_code"].qualified is True
    assert qualifications["grok"].qualified is True
    assert qualifications["antigravity"].qualified is True
    # One agy live probe plus ONE claude plan-CLI probe (lead model only —
    # trailing models ride the lead route proof + per-turn receipts).
    agy_probes = [argv for argv in probe_calls if "--log-file" in argv]
    claude_probes = [argv for argv in probe_calls if "--log-file" not in argv]
    assert len(agy_probes) == 1
    assert len(claude_probes) == 1
    assert claude_probes[0][claude_probes[0].index("--model") + 1] == (
        profile_map()["claude_code"].ordered_models[0]
    )


# ---------------------------------------------------------------------------
# Truthful model evidence.
#
# Antigravity's `Propagating selected model override to backend: label="..."`
# line is emitted by the CLIENT before the request leaves.  It proves what was
# REQUESTED/SELECTED and propagated — it is not a provider-returned served
# identity.  Claude Code's `--output-format json` envelope, by contrast, is a
# provider response artifact whose `modelUsage` does bind served identity.
# These two classes of evidence must never be published under the same name.
# ---------------------------------------------------------------------------


def _agy_receipt(tmp_path, text=_LIVE_RECEIPT_MATCH):
    from hermes_cli.fleet.adapters.live_routes import inspect_agy_subscription_receipt

    log_path = tmp_path / "agy-evidence.log"
    log_path.write_text(text, encoding="utf-8")
    return inspect_agy_subscription_receipt(
        log_path,
        canonical_model_id=CANONICAL_MODEL_ID,
        expected_display_label=DISPLAY_MODEL_LABEL,
    )


def test_agy_receipt_declares_requested_selected_evidence_not_served_identity(tmp_path):
    """RED for R5/blocker 4: propagation was published as served-model identity."""
    receipt = _agy_receipt(tmp_path)
    assert receipt["status"] == "matched"
    assert receipt["model_evidence_kind"] == "requested_selected_propagation"
    assert receipt["served_model_proven"] is False
    assert receipt["served_model_evidence"] == "NOT_PROVEN"
    # The structural identity check still binds the requested id.
    assert receipt["requested_selected_model_id"] == CANONICAL_MODEL_ID
    assert receipt["requested_selected_model_label"] == DISPLAY_MODEL_LABEL


def test_claude_payload_receipt_declares_served_response_evidence():
    """The Claude lane really does have provider-returned served identity."""
    from hermes_cli.fleet.adapters.live_routes import inspect_claude_cli_payload

    check = inspect_claude_cli_payload(
        {
            "type": "result", "subtype": "success", "is_error": False,
            "result": "ok", "session_id": "s", "num_turns": 1,
            "modelUsage": {"claude-opus-5": {}},
        },
        canonical_model_id="claude-opus-5",
    )
    assert check["status"] == "matched"
    assert check["model_evidence_kind"] == "served_response_envelope"
    assert check["served_model_proven"] is True
    assert check["served_model_evidence"] == "PROVEN"


def test_agy_ordinary_route_proof_declares_propagation_evidence_alongside_aliases(
    tmp_path, monkeypatch
):
    """The ordinary AGY proof must name its evidence class truthfully.

    Scope note: the historical bare ``served_model_*`` aliases are retained on
    this ordinary ``execute()`` path because their published shape predates
    this candidate and is pinned by a baseline test outside the leased path
    set.  What this candidate fixes here is the *claim*: the proof now declares
    ``NOT_PROVEN`` and names the requested/selected model as such, so an alias
    can no longer be read as provider-returned identity.  The candidate's own
    owned-material rail omits the aliases entirely — see
    ``test_owned_material_route_proof_publishes_no_bare_served_model_claim``
    in ``tests/hermes_cli/fleet/test_material_delegation_bridge.py``.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    profile = profile_map()["antigravity"]
    executable = str(Path(sys.executable).resolve())

    def _run(argv, **_kwargs):
        log_path = Path(argv[argv.index("--log-file") + 1])
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(_LIVE_RECEIPT_MATCH, encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="antigravity complete", stderr="")

    adapter = AntigravityAdapter(executable, run_process=_run)
    qualification = Qualification(
        qualified=True,
        captured_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        auth_kind="cli_subscription",
        auth_source="google:antigravity-live-receipt",
        overage_disabled=True,
        provider_id=profile.provider_id,
        models=(CANONICAL_MODEL_ID,),
        efforts=profile.supported_efforts,
        fast_off_supported=True,
        capabilities=profile.capabilities,
        executable=executable,
        version="synthetic-agy",
        evidence_id="synthetic-agy-qualification",
        subscription_only_proven=True,
        paid_fallback_absent=True,
        overage_state=OverageState.OFF,
    )
    result = adapter.execute(
        AdapterRequest(
            task_id="agy-evidence", cwd=tmp_path, prompt="review",
            profile=profile, model=CANONICAL_MODEL_ID, effort="high",
            fast_mode=False, timeout_seconds=30,
        ),
        qualification,
    )
    assert result.ok, result.reason
    proof = result.metadata["route_proof"]
    # Truthful separation: requested/selected is named as such, and the served
    # claim is explicitly NOT_PROVEN rather than silently asserted.
    assert proof["served_model_proven"] is False
    assert proof["requested_selected_model_id"] == CANONICAL_MODEL_ID
    assert proof["served_model_evidence"] == "NOT_PROVEN"
    assert proof["model_evidence_kind"] == "requested_selected_propagation"
    assert "model_qualification" not in proof
    # The proven subscription route stays enabled and unpaid.
    assert proof["auth_kind"] == qualification.auth_kind
    assert proof["fallback_enabled"] is False
    assert proof["fast_mode"] is False
