from __future__ import annotations

import hashlib
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli.fleet.adapters import live_routes
from hermes_cli.fleet.adapters.native_provider import NativeProviderAdapter
from hermes_cli.fleet.capacity import BridgeUsageAdapter
from hermes_cli.fleet.config import parse_fleet_config
from hermes_cli.fleet.native_child import _runtime_identity
from hermes_cli.fleet.service import FleetService
from hermes_cli.fleet.state import FleetStore
from hermes_cli.fleet.types import (
    AdapterKind,
    AdapterRequest,
    CapacityRead,
    CapacitySnapshot,
    Confidence,
    Freshness,
    LaneInputs,
    LaneProfile,
    MeasurementKind,
    OverageState,
    Qualification,
    ReasonCode,
    TaskSpec,
)
from hermes_cli.subcommands.fleet import fleet_command


NOW = datetime(2026, 7, 24, tzinfo=timezone.utc)
CAPABILITIES = frozenset({"workspace_write", "shell"})


def _profile() -> LaneProfile:
    return LaneProfile(
        lane_id="chatgpt_codex",
        order=0,
        adapter_kind=AdapterKind.NATIVE_PROVIDER,
        provider_id="openai-codex",
        ordered_models=("m1", "m2"),
        supported_efforts=("low", "high"),
        capabilities=CAPABILITIES,
        allowed_auth_kinds=frozenset({"oauth_subscription"}),
    )


def _qualification(profile: LaneProfile) -> Qualification:
    return Qualification(
        qualified=True,
        captured_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        auth_kind="oauth_subscription",
        auth_source="openai-codex:oauth_subscription",
        overage_disabled=True,
        provider_id=profile.provider_id,
        models=profile.ordered_models,
        efforts=profile.supported_efforts,
        fast_off_supported=True,
        capabilities=profile.capabilities,
        evidence_id="qualification:e2e",
        detail=(
            "observed subscription OAuth route with billing isolation"
        ),
        subscription_only_proven=True,
        paid_fallback_absent=True,
        overage_state=OverageState.OFF,
    )


def _snapshot(lane_id: str = "chatgpt_codex") -> CapacitySnapshot:
    return CapacitySnapshot(
        lane_id=lane_id,
        used_pct=Decimal("10"),
        remaining_pct=Decimal("90"),
        reserved_pct=Decimal("0"),
        effective_remaining_pct=Decimal("90"),
        source_kind="bridge_file",
        source_id=f"bridge:{lane_id}#hash",
        captured_at=NOW,
        read_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        freshness=Freshness.FRESH,
        confidence=Confidence.HIGH,
        schema_version="1",
        overage_disabled=True,
        comparability_group="subscription-weekly",
        quota_window_id="2026-W30",
        measurement_kind=MeasurementKind.MEASURED,
    )


def _candidate(*, concurrency: int = 1) -> LaneInputs:
    profile = _profile()
    return LaneInputs(
        profile=profile,
        enabled=True,
        adapter_found=True,
        qualification=_qualification(profile),
        capacity=CapacityRead(_snapshot(), None),
        max_concurrency=concurrency,
        reserve_floor_pct=Decimal("0"),
    )


def _task(task_id: str, cwd: Path) -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        cwd=cwd,
        required_capabilities=CAPABILITIES,
        reservation_pct=Decimal("5"),
        prompt_fingerprint=f"sha256:{task_id}",
    )


def _bridge(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "captured_at": "2026-07-24T00:00:00Z",
                "lanes": {
                    "chatgpt_codex": {
                        "used_pct": "10.000",
                        "remaining_pct": "90.000",
                        "confidence": "high",
                        "overage_disabled": True,
                        "comparability_group": "subscription-weekly",
                        "quota_window_id": "2026-W30",
                        "measurement_kind": "measured",
                    }
                },
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )


def _native_payload(**request: object) -> dict[str, object]:
    return {
        "ok": True,
        "provider_id": request["provider_id"],
        "model_id": request["model"],
        "effort": request["effort"],
        "auth_kind": "oauth_subscription",
        "auth_source": "openai-codex:oauth_subscription",
        "fallback_enabled": False,
        "fast_mode": False,
        "output": "fake completed",
    }


def _service(home: Path, bridge: Path) -> FleetService:
    profile = _profile()
    config = parse_fleet_config(
        {
            "fleet": {
                "enabled": True,
                "bridge_usage_file": str(bridge),
                "lanes": {
                    "chatgpt_codex": {
                        "enabled": True,
                        "max_concurrency": 1,
                        "reserve_floor_pct": 0,
                    }
                },
            }
        }
    )
    return FleetService(
        config=config,
        store=FleetStore(home / "fleet" / "state.db"),
        profiles=(profile,),
        qualifications={profile.lane_id: _qualification(profile)},
        adapters={profile.lane_id: NativeProviderAdapter(_native_payload)},
        capacity_source=BridgeUsageAdapter(bridge, max_age=timedelta(hours=2)),
        now=lambda: NOW,
        owner_uuid="e2e-owner",
    )


def test_32_concurrent_selectors_create_one_active_lease_without_lock_errors(
    tmp_path,
):
    store = FleetStore(tmp_path / "fleet" / "state.db")
    task = _task("shared-task", tmp_path)

    def acquire(_: int):
        return store.acquire(
            task,
            (_candidate(concurrency=32),),
            owner_uuid=str(uuid.uuid4()),
            ttl_seconds=60,
            now=NOW,
        )

    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(acquire, range(32)))

    assert sum(result.reason is ReasonCode.MET for result in results) == 1
    assert sum(
        result.reason is ReasonCode.LEASE_CONFLICT for result in results
    ) == 31
    assert store.lane_usage("chatgpt_codex", now=NOW) == (
        1,
        Decimal("5"),
    )


def test_clean_hermes_home_run_creates_only_state_and_task_artifact(
    tmp_path, monkeypatch, capsys
):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    bridge = tmp_path / "usage-weekly.json"
    _bridge(bridge)
    task_file = home / "task.txt"
    task_file.write_text("bounded fake task", encoding="utf-8")
    args = SimpleNamespace(
        fleet_action="run",
        task_file=str(task_file),
        cwd=str(tmp_path),
        task_id="clean-home-task",
        json=True,
    )

    assert fleet_command(args, service=_service(home, bridge)) == 0
    capsys.readouterr()

    assert {
        path.relative_to(home).as_posix()
        for path in home.rglob("*")
        if path.is_file()
    } == {"task.txt", "fleet/state.db"}


def test_bridge_hash_is_unchanged_by_plan_doctor_and_status(
    tmp_path, capsys
):
    home = tmp_path / "hermes-home"
    bridge = tmp_path / "usage-weekly.json"
    _bridge(bridge)
    task_file = tmp_path / "task.txt"
    task_file.write_text("read-only task", encoding="utf-8")
    before = hashlib.sha256(bridge.read_bytes()).hexdigest()
    service = _service(home, bridge)

    for action in ("plan", "doctor", "status"):
        args = SimpleNamespace(
            fleet_action=action,
            task_file=str(task_file),
            cwd=str(tmp_path),
            json=True,
            lane=None,
        )
        assert fleet_command(args, service=service) == 0
        capsys.readouterr()

    assert hashlib.sha256(bridge.read_bytes()).hexdigest() == before
    assert not service.store.path.exists()


def test_injected_transaction_crash_is_atomic(tmp_path):
    store = FleetStore(tmp_path / "fleet" / "state.db")
    task = _task("atomic-task", tmp_path)

    with pytest.raises(RuntimeError, match="injected transaction failure"):
        store.acquire(
            task,
            (_candidate(),),
            owner_uuid="owner",
            ttl_seconds=60,
            now=NOW,
            inject_failure=True,
        )

    assert store.read_pin(task.task_id) is None
    assert store.read_live_lease(task.task_id, now=NOW) is None
    assert store.rotation_cursor() == 0
    assert store.audit(task_id=task.task_id) == []


def test_secret_canary_absent_from_state_audit_stdout_and_stderr(
    tmp_path, monkeypatch, capsys
):
    canary = "FLEET_SECRET_CANARY_8c1f64"
    monkeypatch.setenv("OPENAI_API_KEY", canary)
    home = tmp_path / "hermes-home"
    bridge = tmp_path / "usage-weekly.json"
    _bridge(bridge)
    task_file = tmp_path / "task.txt"
    task_file.write_text(f"process without persisting {canary}", encoding="utf-8")
    service = _service(home, bridge)
    args = SimpleNamespace(
        fleet_action="run",
        task_file=str(task_file),
        cwd=str(tmp_path),
        task_id="secret-task",
        json=True,
    )

    assert fleet_command(args, service=service) == 0
    captured = capsys.readouterr()
    audit_text = json.dumps(service.store.audit(), sort_keys=True)

    assert canary not in captured.out
    assert canary not in captured.err
    assert canary not in audit_text
    assert canary.encode() not in service.store.path.read_bytes()


def test_native_child_honors_cwd_stdin_and_is_killed_on_timeout(
    tmp_path, monkeypatch
):
    fake_child = tmp_path / "fake_native_child.py"
    fake_child.write_text(
        "\n".join(
            (
                "import json, os, sys, time",
                "request = json.loads(sys.stdin.read())",
                "with open('child-observed.json', 'w', encoding='utf-8') as handle:",
                "    json.dump({'cwd': os.getcwd(), 'request': request}, handle)",
                "time.sleep(30)",
            )
        ),
        encoding="utf-8",
    )
    workdir = tmp_path / "isolated-workdir"
    workdir.mkdir()
    monkeypatch.setattr(live_routes, "_NATIVE_CHILD", fake_child)
    started = time.monotonic()

    with pytest.raises(TimeoutError, match="timed out"):
        live_routes.run_native_hermes_child(
            provider_id="openai-codex",
            model="m1",
            effort="high",
            prompt="stdin-only prompt",
            cwd=workdir,
            timeout_seconds=10.0,
            fallback_enabled=False,
            fast_mode=False,
        )

    assert time.monotonic() - started < 15
    observed = json.loads(
        (workdir / "child-observed.json").read_text(encoding="utf-8")
    )
    assert Path(observed["cwd"]).resolve() == workdir.resolve()
    assert observed["request"] == {
        "provider_id": "openai-codex",
        "model": "m1",
        "effort": "high",
        "prompt": "stdin-only prompt",
    }


def test_runtime_auth_source_is_derived_and_token_presence_is_not_rejected():
    for provider, source in (
        ("openai-codex", "manual:device_code"),
        ("xai-oauth", "device_code"),
    ):
        runtime = {
            "provider": provider,
            "requested_provider": provider,
            "api_mode": "codex_responses",
            "source": source,
            "api_key": "populated-oauth-access-token",
            "credential_pool": SimpleNamespace(provider=provider),
        }
        assert _runtime_identity(runtime, provider) == (
            "oauth_subscription",
            f"{provider}:oauth_subscription",
        )


def test_native_adapter_fails_closed_on_auth_source_mismatch(tmp_path):
    profile = _profile()
    qualification = _qualification(profile)
    payload = _native_payload(
        provider_id=profile.provider_id,
        model="m1",
        effort="low",
    )
    payload["auth_source"] = "openai-codex:different-source"
    adapter = NativeProviderAdapter(lambda **_: payload)
    request = AdapterRequest(
        task_id="mismatch-task",
        cwd=tmp_path,
        prompt="fake",
        profile=profile,
        model="m1",
        effort="low",
    )

    result = adapter.execute(request, qualification)

    assert not result.ok
    assert result.reason is ReasonCode.CREDENTIAL_MISMATCH
