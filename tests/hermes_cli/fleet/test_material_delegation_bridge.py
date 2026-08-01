from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from hermes_cli.fleet.adapters.live_routes import (
    AntigravityAdapter,
    ClaudeCodeAdapter,
)
from hermes_cli.fleet.adapters.native_provider import NativeProviderAdapter
from hermes_cli.fleet.config import parse_fleet_config
from hermes_cli.fleet.profiles import ordered_profiles
from hermes_cli.fleet.service import FleetService
from hermes_cli.fleet.state import FleetStore
from hermes_cli.fleet.types import (
    CapacityRead,
    CapacitySnapshot,
    AdapterRequest,
    Confidence,
    Freshness,
    HealthRead,
    LaneHealth,
    MeasurementKind,
    OverageState,
    Qualification,
    ReasonCode,
    TaskSpec,
)
from tools.fleet_delegation import execute_material_route, plan_material_routes
from tools.release_review_launch import launch_fleet_owned_material_review
from tools.release_review_ledger import ReleaseReviewLedger


NOW = datetime(2026, 7, 30, 7, 0, tzinfo=timezone.utc)
AGY_RECEIPT = (
    "authMethod=consumer\n"
    "daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent\n"
    "I0730 model_config_manager.go:272] "
    'Propagating selected model override to backend: label="Gemini 3.1 Pro (High)"\n'
)


class _Capacity:
    def read(self, lane_id, *, now, reserved_pct):
        return CapacityRead(
            CapacitySnapshot(
                lane_id=lane_id,
                used_pct=Decimal("56"),
                remaining_pct=Decimal("44"),
                reserved_pct=reserved_pct,
                effective_remaining_pct=Decimal("44") - reserved_pct,
                source_kind="stale-historical-console",
                source_id=f"historical:{lane_id}",
                captured_at=NOW - timedelta(days=2),
                read_at=now,
                expires_at=NOW - timedelta(days=1),
                freshness=Freshness.STALE,
                confidence=Confidence.LOW,
                schema_version="plans-1",
                overage_disabled=True,
                measurement_kind=MeasurementKind.UNKNOWN,
            ),
            ReasonCode.CAPACITY_STALE,
            health=HealthRead(
                status=LaneHealth.UP,
                captured_at=NOW,
                read_at=now,
                expires_at=NOW + timedelta(minutes=5),
                freshness=Freshness.FRESH,
                source_id=f"health:{lane_id}",
            ),
        )


def _qualification(profile):
    return Qualification(
        qualified=True,
        captured_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
        auth_kind=next(iter(profile.allowed_auth_kinds)),
        auth_source=(
            f"{profile.provider_id}:subscription"
            if profile.adapter_kind.value == "native_provider"
            else f"{profile.lane_id}:subscription"
        ),
        overage_disabled=True,
        provider_id=profile.provider_id,
        models=profile.ordered_models,
        efforts=profile.supported_efforts,
        fast_off_supported=True,
        capabilities=profile.capabilities,
        executable=(
            str(Path(sys.executable).resolve())
            if profile.adapter_kind.value == "external_cli"
            else None
        ),
        version=f"synthetic-{profile.lane_id}",
        evidence_id=f"qualification:{profile.lane_id}",
        subscription_only_proven=True,
        paid_fallback_absent=True,
        overage_state=OverageState.OFF,
    )


def _service(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    profiles = ordered_profiles()[:4]
    qualifications = {profile.lane_id: _qualification(profile) for profile in profiles}
    native_calls = []
    process_calls = []

    def native(**request):
        native_calls.append(request)
        return {
            "ok": True,
            "provider_id": request["provider_id"],
            "model_id": request["model"],
            "effort": request["effort"],
            "auth_kind": "oauth_subscription",
            "auth_source": f"{request['provider_id']}:subscription",
            "fallback_enabled": False,
            "fast_mode": False,
            "output": f"{request['provider_id']} complete",
        }

    def process(argv, **_kwargs):
        process_calls.append(argv)
        if "--log-file" in argv:
            log_path = Path(argv[argv.index("--log-file") + 1])
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(AGY_RECEIPT, encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="antigravity complete", stderr="")
        model_id = argv[argv.index("--model") + 1]
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "result": "claude complete",
                    "session_id": "synthetic-claude-session",
                    "num_turns": 1,
                    "modelUsage": {model_id: {}},
                }
            ),
            stderr="",
        )

    adapters = {
        "chatgpt_codex": NativeProviderAdapter(native),
        "claude_code": ClaudeCodeAdapter(sys.executable, run_process=process),
        "grok": NativeProviderAdapter(native),
        "antigravity": AntigravityAdapter(sys.executable, run_process=process),
    }
    config = parse_fleet_config(
        {
            "fleet": {
                "enabled": True,
                "lanes": {
                    profile.lane_id: {
                        "enabled": True,
                        "max_concurrency": 1,
                        "reserve_floor_pct": 0,
                    }
                    for profile in profiles
                },
            }
        }
    )
    service = FleetService(
        config=config,
        store=FleetStore(tmp_path / "fleet-state.db"),
        profiles=profiles,
        qualifications=qualifications,
        adapters=adapters,
        capacity_source=_Capacity(),
        now=lambda: NOW,
        require_verified_health=True,
    )
    return service, native_calls, process_calls


def test_four_material_reviews_use_four_distinct_real_fleet_adapters(
    tmp_path, monkeypatch
):
    service, native_calls, process_calls = _service(tmp_path, monkeypatch)
    plan = plan_material_routes(
        service,
        task_count=4,
        cwd=tmp_path,
        prompt_fingerprint="sha256:candidate-prompt",
    )

    assert not plan.degraded_route_capacity
    assert [route.lane_id for route in plan.assignments] == [
        "chatgpt_codex",
        "claude_code",
        "grok",
        "antigravity",
    ]
    assert len({route.effective_execution_identity for route in plan.assignments}) == 4

    results = [
        execute_material_route(
            service,
            route,
            task_id=f"material-{index}",
            cwd=tmp_path,
            prompt=f"Review candidate deadbeef using lens {index}",
            candidate_hash="deadbeef",
            review_lens=f"lens-{index}",
        )
        for index, route in enumerate(plan.assignments)
    ]

    assert all(result["ok"] for result in results)
    assert len(native_calls) == 2
    assert len(process_calls) == 2
    agy_call = next(argv for argv in process_calls if "--log-file" in argv)
    assert agy_call[0] == str(Path(sys.executable).resolve())
    assert agy_call[agy_call.index("--model") + 1] == "Gemini 3.1 Pro (High)"
    audits = service.store.audit()
    committed = [
        event for event in audits if event["event_type"] == "MATERIAL_RESULT_COMMITTED"
    ]
    assert len(committed) == 4
    identities = {
        event["decision"]["effective_execution_identity"] for event in committed
    }
    assert identities == {
        route.effective_execution_identity for route in plan.assignments
    }
    for result in results:
        public_receipt = json.dumps(result["route_receipt"], sort_keys=True)
        assert str(Path(sys.executable).resolve()) not in public_receipt
        assert "executable" not in public_receipt


def test_owned_claude_material_run_uses_argv_allowlisted_environment_and_keeps_raw_io_in_memory(
    tmp_path, monkeypatch
):
    service, _native_calls, _process_calls = _service(tmp_path, monkeypatch)
    adapter = service.adapters["claude_code"]
    calls: list[tuple[list[str], dict[str, object]]] = []

    class FakeProcess:
        pid = 4812
        returncode = 0

        def communicate(self, prompt, timeout=None):
            assert prompt == "review exact candidate"
            assert timeout == 12
            return (
                json.dumps({
                    "type": "result", "subtype": "success", "is_error": False,
                    "result": "approved", "session_id": "synthetic-owned-session",
                    "num_turns": 1,
                    "modelUsage": {service.profiles[1].ordered_models[0]: {}},
                }),
                "RAW_STDERR_SHOULD_NOT_BE_PERSISTED",
            )

    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return FakeProcess()

    adapter._popen = popen
    profile = service.profiles[1]
    run = adapter.start_owned_material(
        AdapterRequest(
            task_id="owned-claude", cwd=tmp_path, prompt="review exact candidate",
            profile=profile, model=profile.ordered_models[0], effort="high", timeout_seconds=12,
        ),
        service.qualifications["claude_code"],
    )
    assert run.process.pid == 4812
    argv, kwargs = calls[0]
    assert argv[0] == str(Path(sys.executable).resolve())
    assert kwargs["shell"] is False
    assert "RAW_STDERR_SHOULD_NOT_BE_PERSISTED" not in json.dumps(kwargs, default=str)
    assert "OPENAI_API_KEY" not in kwargs["env"]
    assert run.finish().ok


def test_service_owned_external_acquisition_reserves_then_releases_one_claude_lease(
    tmp_path, monkeypatch
):
    service, _native_calls, _process_calls = _service(tmp_path, monkeypatch)
    route = plan_material_routes(
        service, task_count=1, cwd=tmp_path, prompt_fingerprint="sha256:owned-service",
        preferred_lane_ids=("claude_code",),
    ).assignments[0]

    class FakeProcess:
        pid = 4813
        returncode = 0

        def communicate(self, prompt, timeout=None):
            return (
                json.dumps({
                    "type": "result", "subtype": "success", "is_error": False,
                    "result": "owned complete", "session_id": "synthetic-owned-service",
                    "num_turns": 1,
                    "modelUsage": {service.profiles[1].ordered_models[0]: {}},
                }),
                "",
            )

    service.adapters["claude_code"]._popen = lambda *_args, **_kwargs: FakeProcess()
    execution = service.acquire_owned_external(
        TaskSpec(
            task_id="owned-service", cwd=tmp_path, required_capabilities=frozenset({"workspace_read", "shell"}),
            reservation_pct=service.config.default_reservation_pct, prompt_fingerprint="sha256:owned-service",
        ),
        prompt="review exact candidate", preferred_lane_id=route.lane_id,
    )
    assert not hasattr(execution, "reason")
    run = execution.adapter.start_owned_material(execution.request, execution.qualification)
    result = service.finalize_owned_external(execution, run.finish())
    assert result.ok
    events = service.store.audit()
    event_types = [event["event_type"] for event in events if event["task_id"] == "owned-service"]
    assert event_types.count("EXECUTION_STARTED") == 1
    assert event_types.count("EXECUTION_COMPLETED") == 1


def test_sealed_owned_material_ingress_binds_dual_fences_before_external_execution(
    tmp_path, monkeypatch
):
    from tools import async_delegation as ad

    hermes_home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    ad._reset_for_tests()
    try:
        service, _native_calls, _process_calls = _service(tmp_path, monkeypatch)
        route = plan_material_routes(
            service, task_count=1, cwd=tmp_path, prompt_fingerprint="sha256:sealed-owned",
            preferred_lane_ids=("claude_code",),
        ).assignments[0]

        class FakeProcess:
            pid = 4820
            returncode = 0

            def communicate(self, prompt, timeout=None):
                return (json.dumps({
                    "type": "result", "subtype": "success", "is_error": False,
                    "result": "approved", "session_id": "sealed-owned-session", "num_turns": 1,
                    "modelUsage": {route.model_id: {}},
                }), "PRIVATE_STDERR")

        service.adapters["claude_code"]._popen = lambda *_args, **_kwargs: FakeProcess()
        ledger = ReleaseReviewLedger(hermes_home / "release-review-ledger.db")
        packet = ledger.record_recovery_packet({
            "schema_version": 1, "candidate_hash": "a" * 64, "environment_fingerprint": "env-a",
            "normalized_scope": "release tests", "failure_fingerprint": "failed:one",
            "normalized_task": "review candidate", "failed_set": ["one"], "reproducer": ["pytest", "one"],
            "versions": {"python": "3.13"}, "attempted_remedy_hash": "none", "verified_facts": ["reproduced"],
            "unresolved_questions": ["review"], "quarantined": [], "redaction_attestation": True,
        })
        attempt = ledger.admit_recovery_attempt(
            packet_hash=packet["packet_hash"], candidate_hash="a" * 64, environment_fingerprint="env-a",
            normalized_scope="release tests", failure_fingerprint="failed:one", normalized_task="review candidate",
            mode="STANDARD", ordinal=1, owner="codex", effective_route_identity=route.effective_execution_identity,
            lens="runtime",
        )
        result = launch_fleet_owned_material_review(
            ledger, fleet_service=service, cwd=str(tmp_path), attempt_id=attempt["attempt_id"],
            fence_token=attempt["fence_token"],
            dispatch_kwargs={"goal": "review", "context": "candidate", "toolsets": None, "role": "reviewer",
                             "model": route.model_id, "session_key": "test", "max_async_children": 1},
            candidate_hash="a" * 64, scope="release tests", lane="claude_code", model=route.model_id,
            prompt="review exact candidate", deadline_seconds=60, output_path="out.json",
            environment_fingerprint="env-a", evidence_fingerprint="evidence-a",
            effective_route_identity=route.effective_execution_identity, review_lens="runtime",
            preflight={key: {"status": "verified", "evidence": "test", **({"authenticated": True, "method": "proof", "endpoint": "/health"} if key == "health" else {})}
                       for key in ("target", "install", "restart", "rollback", "health")},
        )
        assert result["status"] == "launched"
        deadline = time.monotonic() + 3
        while ad.active_count() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ad.active_count() == 0
        with sqlite3.connect(hermes_home / "release-review-ledger.db") as conn:
            states = conn.execute(
                "SELECT r.state, p.saga_state, a.state FROM release_review_receipts r "
                "JOIN workflow_material_route_plans p ON p.receipt_id=r.receipt_id "
                "JOIN workflow_recovery_attempts a ON a.attempt_id=p.attempt_id"
            ).fetchone()
        assert states == ("completed", "TERMINAL", "COMMITTED")
        with sqlite3.connect(hermes_home / "state.db") as conn:
            stored = conn.execute("SELECT result_json FROM async_delegations").fetchone()[0]
        assert "PRIVATE_STDERR" not in stored
    finally:
        ad._reset_for_tests()


def test_alias_lanes_for_same_backing_route_do_not_satisfy_independence(
    tmp_path, monkeypatch
):
    service, _native_calls, _process_calls = _service(tmp_path, monkeypatch)
    canonical = service.profiles[0]
    alias = replace(canonical, lane_id="chatgpt_codex_alias", order=1)
    canonical_config = service.config.lanes[canonical.lane_id]
    service.profiles = (canonical, alias)
    service.config = replace(
        service.config,
        lanes={
            canonical.lane_id: canonical_config,
            alias.lane_id: canonical_config,
        },
    )
    service.qualifications[alias.lane_id] = replace(
        service.qualifications[canonical.lane_id],
        evidence_id="qualification:refreshed-alias-receipt",
    )
    service.adapters[alias.lane_id] = service.adapters[canonical.lane_id]

    plan = plan_material_routes(
        service,
        task_count=2,
        cwd=tmp_path,
        prompt_fingerprint="sha256:alias-collapse",
        preferred_lane_ids=(canonical.lane_id, alias.lane_id),
    )

    assert plan.degraded_route_capacity
    assert [route.lane_id for route in plan.assignments] == [canonical.lane_id]
    assert [route.lane_id for route in plan.unavailable] == [alias.lane_id]
    assert (
        plan.assignments[0].effective_execution_identity
        == plan.unavailable[0].effective_execution_identity
    )


def test_concurrent_same_lane_execution_allows_only_one_worker(
    tmp_path, monkeypatch
):
    service, _native_calls, _process_calls = _service(tmp_path, monkeypatch)
    route = plan_material_routes(
        service,
        task_count=1,
        cwd=tmp_path,
        prompt_fingerprint="sha256:lease-contention",
        preferred_lane_ids=("chatgpt_codex",),
    ).assignments[0]
    started = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def blocking_native(**request):
        calls.append(request["model"])
        started.set()
        assert release.wait(5)
        return {
            "ok": True,
            "provider_id": request["provider_id"],
            "model_id": request["model"],
            "effort": request["effort"],
            "auth_kind": "oauth_subscription",
            "auth_source": f"{request['provider_id']}:subscription",
            "fallback_enabled": False,
            "fast_mode": False,
            "output": "first worker complete",
        }

    service.adapters[route.lane_id] = NativeProviderAdapter(blocking_native)
    first: dict[str, object] = {}

    def run_first() -> None:
        first.update(
            execute_material_route(
                service,
                route,
                task_id="concurrent-first",
                cwd=tmp_path,
                prompt="first review",
                candidate_hash="deadbeef",
                review_lens="runtime",
            )
        )

    worker = threading.Thread(target=run_first)
    worker.start()
    assert started.wait(5)
    second = execute_material_route(
        service,
        route,
        task_id="concurrent-second",
        cwd=tmp_path,
        prompt="second review",
        candidate_hash="deadbeef",
        review_lens="runtime",
    )
    release.set()
    worker.join(5)

    assert not worker.is_alive()
    assert first["ok"] is True
    assert second["ok"] is False
    assert second["reason"] == ReasonCode.NO_ELIGIBLE_LANE.value
    assert calls == [route.model_id]


def test_route_plan_fails_degraded_instead_of_reusing_same_model(
    tmp_path, monkeypatch
):
    service, _native_calls, _process_calls = _service(tmp_path, monkeypatch)
    first = plan_material_routes(
        service,
        task_count=1,
        cwd=tmp_path,
        prompt_fingerprint="sha256:first",
        preferred_lane_ids=["chatgpt_codex"],
    ).assignments[0]

    plan = plan_material_routes(
        service,
        task_count=1,
        cwd=tmp_path,
        prompt_fingerprint="sha256:retry",
        preferred_lane_ids=["chatgpt_codex"],
        exclude_effective_identities=[first.effective_execution_identity],
    )

    assert plan.degraded_route_capacity
    assert plan.assignments == ()
    assert plan.unavailable[0].lane_id == "chatgpt_codex"


def test_preferred_lane_cannot_override_an_existing_task_pin(
    tmp_path, monkeypatch
):
    service, _native_calls, _process_calls = _service(tmp_path, monkeypatch)
    plan = plan_material_routes(
        service,
        task_count=2,
        cwd=tmp_path,
        prompt_fingerprint="sha256:pin-test",
    )
    first, second = plan.assignments[:2]
    initial = execute_material_route(
        service,
        first,
        task_id="immutable-pin",
        cwd=tmp_path,
        prompt="first review",
        candidate_hash="deadbeef",
        review_lens="spec",
    )
    assert initial["ok"]

    reroute = service.run(
        task=TaskSpec(
            task_id="immutable-pin",
            cwd=tmp_path,
            required_capabilities=frozenset({"workspace_read", "shell"}),
            reservation_pct=Decimal("1"),
        ),
        prompt="must not reroute",
        preferred_lane_id=second.lane_id,
    )

    assert not reroute.ok
    assert reroute.reason is ReasonCode.PINNED_LANE_UNAVAILABLE
    assert reroute.pin is not None
    assert reroute.pin.lane_id == first.lane_id


def test_preferred_lane_rejects_same_lane_pin_identity_drift(
    tmp_path, monkeypatch
):
    service, _native_calls, _process_calls = _service(tmp_path, monkeypatch)
    route = plan_material_routes(
        service,
        task_count=1,
        cwd=tmp_path,
        prompt_fingerprint="sha256:identity-drift",
    ).assignments[0]
    initial = execute_material_route(
        service,
        route,
        task_id="drifted-pin",
        cwd=tmp_path,
        prompt="first review",
        candidate_hash="deadbeef",
        review_lens="runtime",
    )
    assert initial["ok"]

    with closing(sqlite3.connect(service.store.path)) as connection:
        with connection:
            connection.execute(
                "UPDATE tasks SET model_id='stale-model' WHERE task_id='drifted-pin'"
            )

    reroute = service.run(
        TaskSpec(
            task_id="drifted-pin",
            cwd=tmp_path,
            required_capabilities=frozenset({"workspace_read", "shell"}),
            reservation_pct=Decimal("1"),
        ),
        prompt="must fail closed",
        preferred_lane_id=route.lane_id,
    )

    assert not reroute.ok
    assert reroute.reason is ReasonCode.PINNED_LANE_UNAVAILABLE


def test_material_receipt_rejects_adapter_identity_mismatch(
    tmp_path, monkeypatch
):
    service, _native_calls, _process_calls = _service(tmp_path, monkeypatch)
    route = plan_material_routes(
        service,
        task_count=1,
        cwd=tmp_path,
        prompt_fingerprint="sha256:adapter-mismatch",
    ).assignments[0]
    original = service.adapters[route.lane_id]

    class _WrongProviderAdapter:
        def execute(self, request, qualification):
            result = original.execute(request, qualification)
            return result.__class__(
                ok=result.ok,
                reason=result.reason,
                provider_id="wrong-provider",
                model_id=result.model_id,
                auth_kind=result.auth_kind,
                adapter_kind=result.adapter_kind,
                output=result.output,
                metadata=result.metadata,
            )

    service.adapters[route.lane_id] = _WrongProviderAdapter()
    result = execute_material_route(
        service,
        route,
        task_id="adapter-mismatch",
        cwd=tmp_path,
        prompt="review",
        candidate_hash="deadbeef",
        review_lens="identity",
    )

    assert not result["ok"]
    assert result["reason"] == ReasonCode.PROVIDER_MISMATCH.value
    assert result["route_receipt"]["status"] == "FAILED"
