from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli.fleet.adapters.live_routes import (
    AntigravityAdapter,
    live_adapters,
)
from hermes_cli.fleet.adapters.native_provider import NativeProviderAdapter
from hermes_cli.fleet.capacity import BridgeUsageAdapter
from hermes_cli.fleet.live import FleetQualificationDoctor
from hermes_cli.fleet.profiles import profile_map
from hermes_cli.fleet.types import OverageState, TaskSpec
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


def _probe_process_factory(log_text: str, *, returncode: int = 0, stdout: str = "pong"):
    def process(argv, **_kwargs):
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
    assert qualifications["claude_code"].models == ("claude-opus-4-8",)
    assert qualifications["claude_code"].efforts == (
        "low",
        "medium",
        "high",
        "max",
    )
    assert qualifications["claude_code"].fast_off_supported
    assert "never-record" not in qualifications["claude_code"].detail
    assert qualifications["antigravity"].models == ("gemini-3.1-pro-high",)
    assert qualifications["antigravity"].efforts == ("low", "medium", "high")
    assert qualifications["antigravity"].qualified
    assert qualifications["antigravity"].parent_session_proven is True
    assert not any(Path(command[0]).stem == "claude" for command in commands)
    assert any(command[1:] == ("models",) for command in commands)
    assert not any(Path(command[0]).stem == "agy" and "auth" in command for command in commands)
    assert "served-model receipt" in qualifications["antigravity"].detail


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
            stdout = json.dumps(
                {"result": "claude complete", "modelUsage": {model: {}}}
            )
        else:
            log_path = Path(argv[argv.index("--log-file") + 1])
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(
                (
                    "I0724 11:02:16.509256 40296 model_config_manager.go:272] "
                    "Propagating selected model override to backend: "
                    'label="Gemini 3.1 Pro (High)"'
                ),
                encoding="utf-8",
            )
            stdout = "antigravity complete"
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    adapters = {
        "chatgpt_codex": NativeProviderAdapter(native),
        "claude_code": NativeProviderAdapter(native),
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
        assert process_calls == []
        assert result.adapter_result.adapter_kind.value == "native_provider"
        assert result.adapter_result.provider_id == "anthropic"
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
        assert route_proof["model_qualification"] == "agy models"
        assert route_proof["served_model_id"] == "gemini-3.1-pro-high"
        assert route_proof["served_model_label"] == "Gemini 3.1 Pro (High)"


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
    assert qualification.detail == "live served-model receipt mismatch"
    payload = json.dumps(qualification.__dict__, default=str)
    assert secret not in payload
    assert MISMATCH_MODEL_LABEL not in payload
    assert not list(home.rglob("*.log"))


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
    assert "served-model receipt" in qualification.detail
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

    assert first.qualified and second.qualified
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
    assert antigravity["qualification_detail"] == "live served-model receipt mismatch"


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
    assert len(probe_calls) == 1
