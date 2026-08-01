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

import pytest

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
from tools.fleet_delegation import (
    dispatch_material_review,
    execute_material_route,
    plan_material_routes,
)
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
    """Capacity a real admissible lane must have.

    The live fleet service always sets ``require_verified_health=True``, so a
    material route is subject to exactly the same task-worker capacity policy as
    ordinary routing: fresh, measured, comparable evidence.  A fixture that
    routes on stale/unknown capacity would only pass by weakening fleet-wide
    policy — see ``_StaleCapacity`` for the fail-closed counterpart.
    """

    def read(self, lane_id, *, now, reserved_pct):
        return CapacityRead(
            CapacitySnapshot(
                lane_id=lane_id,
                used_pct=Decimal("56"),
                remaining_pct=Decimal("44"),
                reserved_pct=reserved_pct,
                effective_remaining_pct=Decimal("44") - reserved_pct,
                source_kind="bridge_file",
                source_id=f"bridge:{lane_id}#hash",
                captured_at=NOW - timedelta(minutes=5),
                read_at=now,
                expires_at=NOW + timedelta(hours=1),
                freshness=Freshness.FRESH,
                confidence=Confidence.HIGH,
                schema_version="plans-1",
                overage_disabled=True,
                comparability_group="subscription-weekly",
                quota_window_id="2026-W31",
                measurement_kind=MeasurementKind.MEASURED,
            ),
            None,
            health=HealthRead(
                status=LaneHealth.UP,
                captured_at=NOW,
                read_at=now,
                expires_at=NOW + timedelta(minutes=5),
                freshness=Freshness.FRESH,
                source_id=f"health:{lane_id}",
            ),
        )


class _StaleCapacity:
    """Stale historical console usage: transport healthy, quota unknowable."""

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


def _service(tmp_path, monkeypatch, capacity_source=None):
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
        capacity_source=capacity_source or _Capacity(),
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
    monkeypatch.setattr(
        "tools.fleet_delegation.create_owner_death_containment", _FakeContainment
    )
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


# ---------------------------------------------------------------------------
# Owned-material ownership / orphan safety.
#
# The lifecycle claim under test is narrow and falsifiable: no provider prompt
# may reach an executable argv, and no external child may exist unowned, before
# BOTH durable rails (review ledger route plan + async submission outbox) hold a
# provisional ownership record for it.  Every failure after child creation must
# terminate that exact child and finalize exactly one Fleet lease.
# ---------------------------------------------------------------------------


class _RecordingProcess:
    """Exact-child stand-in that records termination of itself only."""

    def __init__(self, pid, payload, *, stderr=""):
        self.pid = pid
        self.returncode = 0
        self._payload = payload
        self._stderr = stderr
        self.terminated = 0
        self.killed = 0
        self.communicated = 0

    def communicate(self, prompt=None, timeout=None):
        self.communicated += 1
        return (self._payload, self._stderr)

    def poll(self):
        return None if not (self.terminated or self.killed) else -15

    def terminate(self):
        self.terminated += 1

    def kill(self):
        self.killed += 1


class _FakeContainment:
    """Stand-in for kernel owner-death containment in fake-process tests.

    The real containment assigns an OS process handle to a Job Object, which a
    `_RecordingProcess` stub does not have — production correctly REFUSES such
    a child rather than downgrading.  These tests exercise the surrounding
    lifecycle, so they inject a double.  The real guarantee is proven
    separately by the out-of-process owner-death test, which spawns genuine
    OS processes and kills a real owner.
    """

    kind = "test_double"
    available = True
    detail = "injected test double; real guarantee proven out-of-process"

    def __init__(self):
        self.adopted = []
        self.closed = 0

    def popen_kwargs(self):
        return {}

    def adopt(self, process):
        self.adopted.append(process)
        return True

    def close(self):
        self.closed += 1

    def public_facts(self):
        return {
            "owner_death_containment": self.kind,
            "owner_death_containment_available": True,
            "owner_death_containment_detail": self.detail,
        }


def _material_harness(tmp_path, monkeypatch, *, lane="claude_code", pid=5150):
    """Build one real sealed-ingress launch environment on a temp HERMES_HOME."""
    hermes_home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    containment = _FakeContainment()
    monkeypatch.setattr(
        "tools.fleet_delegation.create_owner_death_containment",
        lambda: containment,
    )
    service, _native_calls, _process_calls = _service(tmp_path, monkeypatch)
    route = plan_material_routes(
        service, task_count=1, cwd=tmp_path,
        prompt_fingerprint=f"sha256:harness-{lane}", preferred_lane_ids=(lane,),
    ).assignments[0]

    if lane == "antigravity":
        payload = "antigravity complete"
    else:
        payload = json.dumps({
            "type": "result", "subtype": "success", "is_error": False,
            "result": "approved", "session_id": "harness-session", "num_turns": 1,
            "modelUsage": {route.model_id: {}},
        })
    child = _RecordingProcess(pid, payload)
    spawned = []

    def _popen(argv, **_kwargs):
        spawned.append(list(argv))
        if "--log-file" in argv:
            log_path = Path(argv[argv.index("--log-file") + 1])
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(AGY_RECEIPT, encoding="utf-8")
        return child

    service.adapters[lane]._popen = _popen

    ledger = ReleaseReviewLedger(hermes_home / "release-review-ledger.db")
    packet = ledger.record_recovery_packet({
        "schema_version": 1, "candidate_hash": "b" * 64, "environment_fingerprint": "env-b",
        "normalized_scope": "release tests", "failure_fingerprint": "failed:one",
        "normalized_task": "review candidate", "failed_set": ["one"], "reproducer": ["pytest", "one"],
        "versions": {"python": "3.13"}, "attempted_remedy_hash": "none",
        "verified_facts": ["reproduced"], "unresolved_questions": ["review"],
        "quarantined": [], "redaction_attestation": True,
    })
    attempt = ledger.admit_recovery_attempt(
        packet_hash=packet["packet_hash"], candidate_hash="b" * 64,
        environment_fingerprint="env-b", normalized_scope="release tests",
        failure_fingerprint="failed:one", normalized_task="review candidate",
        mode="STANDARD", ordinal=1, owner="opus",
        effective_route_identity=route.effective_execution_identity, lens="runtime",
    )
    launch = dict(
        fleet_service=service, cwd=str(tmp_path), attempt_id=attempt["attempt_id"],
        fence_token=attempt["fence_token"],
        dispatch_kwargs={"goal": "review", "context": "candidate", "toolsets": None,
                         "role": "reviewer", "model": route.model_id,
                         "session_key": "test", "max_async_children": 1},
        candidate_hash="b" * 64, scope="release tests", lane=lane, model=route.model_id,
        prompt="review exact candidate", deadline_seconds=60, output_path="out.json",
        environment_fingerprint="env-b", evidence_fingerprint="evidence-b",
        effective_route_identity=route.effective_execution_identity, review_lens="runtime",
        preflight={
            key: {"status": "verified", "evidence": "test",
                  **({"authenticated": True, "method": "proof", "endpoint": "/health"}
                     if key == "health" else {})}
            for key in ("target", "install", "restart", "rollback", "health")
        },
    )
    return SimpleNamespace(
        service=service, route=route, ledger=ledger, ledger_path=hermes_home / "release-review-ledger.db",
        hermes_home=hermes_home, child=child, spawned=spawned, launch=launch,
        containment=containment,
    )


def _drain(module, seconds=3):
    deadline = time.monotonic() + seconds
    while module.active_count() and time.monotonic() < deadline:
        time.sleep(0.01)
    return module.active_count()


def _lease_finalizations(service, *, task_prefix="material-external-"):
    return [
        event for event in service.store.audit()
        if str(event["task_id"]).startswith(task_prefix)
        and event["event_type"] in {"EXECUTION_COMPLETED", "EXECUTION_FAILED"}
    ]


def _column_value(db_path, table, column, *, extra=""):
    """Read one column defensively so a missing column reads as None, not an error."""
    with closing(sqlite3.connect(db_path)) as conn:
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            return None
        row = conn.execute(f"SELECT {column} FROM {table} {extra}").fetchone()
        return None if row is None else row[0]


def _saga_row(ledger_path):
    with closing(sqlite3.connect(ledger_path)) as conn:
        return conn.execute(
            "SELECT saga_state, external_handle_id, external_pid "
            "FROM workflow_material_route_plans"
        ).fetchone()


def test_material_prompt_reaches_no_argv_before_both_rails_hold_provisional_ownership(
    tmp_path, monkeypatch
):
    """RED for F1/R2: the AGY prompt is in argv at Popen before any durable bind."""
    from tools import async_delegation as ad

    ad._reset_for_tests()
    try:
        harness = _material_harness(tmp_path, monkeypatch, lane="antigravity", pid=5151)
        observed = {}
        adapter = harness.service.adapters["antigravity"]
        inner = adapter._popen

        def _observing_popen(argv, **kwargs):
            observed["saga_state"] = _column_value(
                harness.ledger_path, "workflow_material_route_plans", "saga_state"
            )
            observed["ledger_provisional"] = _column_value(
                harness.ledger_path, "workflow_material_route_plans", "provisional_handle_id"
            )
            observed["outbox_provisional"] = _column_value(
                harness.hermes_home / "state.db", "async_delegations",
                "external_provisional_handle_id",
            )
            observed["prompt_in_argv"] = "review exact candidate" in list(argv)
            return inner(argv, **kwargs)

        adapter._popen = _observing_popen

        result = launch_fleet_owned_material_review(harness.ledger, **harness.launch)
        assert result["status"] == "launched"
        assert _drain(ad) == 0

        # The prompt legitimately appears in the AGY argv, but only AFTER both
        # durable rails already own the pending child.
        assert observed["prompt_in_argv"] is True
        assert observed["saga_state"] == "STARTING"
        assert observed["ledger_provisional"]
        assert observed["outbox_provisional"]
        assert observed["ledger_provisional"] == observed["outbox_provisional"]
    finally:
        ad._reset_for_tests()


def test_registration_failure_after_child_creation_kills_child_and_finalizes_one_lease(
    tmp_path, monkeypatch
):
    """RED for R1: inspector 1 measured child_killed=False, fleet_lease_finalized=0."""
    from tools import async_delegation as ad
    from tools.process_registry import process_registry

    ad._reset_for_tests()
    try:
        harness = _material_harness(tmp_path, monkeypatch, pid=5152)

        def _boom(*_args, **_kwargs):
            raise RuntimeError("injected registry failure")

        monkeypatch.setattr(process_registry, "register_owned_argv_process", _boom)

        result = launch_fleet_owned_material_review(harness.ledger, **harness.launch)
        assert result["status"] == "launched"
        assert _drain(ad) == 0

        assert harness.child.terminated + harness.child.killed >= 1
        assert harness.child.communicated == 0
        assert len(_lease_finalizations(harness.service)) == 1
        saga = _saga_row(harness.ledger_path)
        assert saga is not None and saga[0] in {"STARTING", "TERMINAL"}
        # No owned handle/PID was ever recorded for a child that never bound.
        assert not saga[1]
        assert not saga[2]
    finally:
        ad._reset_for_tests()


def test_ledger_owned_binding_failure_kills_child_and_finalizes_one_lease(
    tmp_path, monkeypatch
):
    from tools import async_delegation as ad
    from tools import release_review_ledger as rrl

    ad._reset_for_tests()
    try:
        harness = _material_harness(tmp_path, monkeypatch, pid=5153)

        def _boom(self, *_args, **_kwargs):
            raise RuntimeError("injected ledger owned-bind failure")

        monkeypatch.setattr(rrl.ReleaseReviewLedger, "bind_material_owned_handle", _boom)

        result = launch_fleet_owned_material_review(harness.ledger, **harness.launch)
        assert result["status"] == "launched"
        assert _drain(ad) == 0

        assert harness.child.terminated + harness.child.killed >= 1
        assert harness.child.communicated == 0
        assert len(_lease_finalizations(harness.service)) == 1
    finally:
        ad._reset_for_tests()


def test_outbox_owned_binding_failure_kills_child_and_finalizes_one_lease(
    tmp_path, monkeypatch
):
    from tools import async_delegation as ad

    ad._reset_for_tests()
    try:
        harness = _material_harness(tmp_path, monkeypatch, pid=5154)
        monkeypatch.setattr(ad, "bind_material_external_handle", lambda *_a, **_k: False)

        result = launch_fleet_owned_material_review(harness.ledger, **harness.launch)
        assert result["status"] == "launched"
        assert _drain(ad) == 0

        assert harness.child.terminated + harness.child.killed >= 1
        assert harness.child.communicated == 0
        assert len(_lease_finalizations(harness.service)) == 1
    finally:
        ad._reset_for_tests()


def test_cancel_between_child_creation_and_registration_terminates_exact_child(
    tmp_path, monkeypatch
):
    """RED for meta-audit C4: cancel in that window was silently lost."""
    from tools import async_delegation as ad
    from tools import fleet_delegation as fd

    ad._reset_for_tests()
    try:
        harness = _material_harness(tmp_path, monkeypatch, pid=5155)
        runners = []
        original_factory = fd.OwnedMaterialRunner.factory

        def _capturing_factory(self, delegation_id, receipt_id):
            runners.append(self)
            return original_factory(self, delegation_id, receipt_id)

        monkeypatch.setattr(fd.OwnedMaterialRunner, "factory", _capturing_factory)

        adapter = harness.service.adapters["claude_code"]
        inner = adapter._popen

        def _cancel_then_spawn(argv, **kwargs):
            process = inner(argv, **kwargs)
            # Owner death / explicit cancel arriving after the child exists but
            # before it is registered.
            runners[0].interrupt()
            return process

        adapter._popen = _cancel_then_spawn

        result = launch_fleet_owned_material_review(harness.ledger, **harness.launch)
        assert result["status"] == "launched"
        assert _drain(ad) == 0

        assert harness.child.terminated + harness.child.killed >= 1
        assert harness.child.communicated == 0
        assert len(_lease_finalizations(harness.service)) == 1
    finally:
        ad._reset_for_tests()


def test_adapter_finish_failure_still_finalizes_exactly_one_lease(
    tmp_path, monkeypatch
):
    """RED for meta-audit C5: an exception after binding leaked a second lease."""
    from tools import async_delegation as ad
    from hermes_cli.fleet.adapters import live_routes

    ad._reset_for_tests()
    try:
        harness = _material_harness(tmp_path, monkeypatch, pid=5156)

        def _boom(self, run, *, timeout_seconds=None):
            raise RuntimeError("injected adapter finish failure")

        monkeypatch.setattr(
            live_routes._SubscriptionCliAdapter, "_finish_owned_material_run", _boom
        )

        result = launch_fleet_owned_material_review(harness.ledger, **harness.launch)
        assert result["status"] == "launched"
        assert _drain(ad) == 0

        assert len(_lease_finalizations(harness.service)) == 1
        assert harness.child.terminated + harness.child.killed >= 1
    finally:
        ad._reset_for_tests()


def test_completion_bookkeeping_failure_after_finish_still_finalizes_one_lease(
    tmp_path, monkeypatch
):
    """RED for the C5 class one step later than `run.finish()`.

    `complete_owned_argv_process` runs after a successful finish and before the
    lease is released.  When it raised, the exception escaped the runner with
    the Fleet lease still held — the same leak as R1, on a path neither
    inspector exercised.  The child has already exited here, so the contract is
    exactly-one finalization and no attempt to signal a dead process.
    """
    from tools import async_delegation as ad
    from tools.process_registry import process_registry

    ad._reset_for_tests()
    try:
        harness = _material_harness(tmp_path, monkeypatch, pid=5157)

        def _boom(*_args, **_kwargs):
            raise RuntimeError("injected completion bookkeeping failure")

        monkeypatch.setattr(
            process_registry, "complete_owned_argv_process", _boom
        )

        result = launch_fleet_owned_material_review(harness.ledger, **harness.launch)
        assert result["status"] == "launched"
        assert _drain(ad) == 0

        assert len(_lease_finalizations(harness.service)) == 1
    finally:
        ad._reset_for_tests()


def test_owned_material_route_proof_publishes_no_bare_served_model_claim(
    tmp_path, monkeypatch
):
    """RED for R5/blocker 4 on the rail this candidate actually publishes.

    The Antigravity receipt is built from the client's own pre-flight
    "Propagating selected model override to backend" line, which proves what
    was requested/selected — never what the provider served.  The owned
    material proof therefore must carry no bare ``served_model_id`` /
    ``served_model_label`` key at all, and must declare the evidence class
    explicitly, so a downstream consumer cannot read propagation as served
    identity.  (The ordinary ``execute()`` path keeps its historical aliases;
    that difference is deliberate and documented in ``_agy_route_proof``.)
    """
    from tools import async_delegation as ad
    from hermes_cli.fleet.adapters import live_routes

    ad._reset_for_tests()
    try:
        harness = _material_harness(tmp_path, monkeypatch, lane="antigravity", pid=5159)
        proofs = []
        inner = live_routes._SubscriptionCliAdapter._finish_owned_material_run

        def _capture(self, run, *, timeout_seconds=None):
            result = inner(self, run, timeout_seconds=timeout_seconds)
            proof = (result.metadata or {}).get("route_proof")
            if proof is not None:
                proofs.append(proof)
            return result

        monkeypatch.setattr(
            live_routes._SubscriptionCliAdapter, "_finish_owned_material_run", _capture
        )

        result = launch_fleet_owned_material_review(harness.ledger, **harness.launch)
        assert result["status"] == "launched"
        assert _drain(ad) == 0

        assert proofs, "owned material run published no route proof"
        proof = proofs[0]
        assert "served_model_id" not in proof
        assert "served_model_label" not in proof
        assert proof["model_evidence_kind"] == "requested_selected_propagation"
        assert proof["served_model_proven"] is False
        assert proof["served_model_evidence"] == "NOT_PROVEN"
        assert proof["requested_selected_model_id"] == harness.route.model_id
        assert "model_qualification" not in proof
        # The proven subscription route stays enabled and unpaid.
        assert proof["fallback_enabled"] is False
        assert proof["fast_mode"] is False
    finally:
        ad._reset_for_tests()


def test_published_material_receipt_carries_the_truthful_model_evidence_class(
    tmp_path, monkeypatch
):
    """RED for R5: the public receipt republished identity with no provenance.

    Withdrawing the overclaim is only half the repair — the receipt has to
    positively state which evidence class it holds, or a consumer still cannot
    tell an unproven Antigravity client-propagation from a proven Claude
    response envelope.  So the allowlist must carry the evidence keys, and it
    must NOT carry a bare ``served_model_id`` that a propagation-only lane
    could republish as provider-returned identity.
    """
    service, _native_calls, _process_calls = _service(tmp_path, monkeypatch)
    plan = plan_material_routes(
        service, task_count=4, cwd=tmp_path, prompt_fingerprint="sha256:model-evidence",
    )
    routes = {route.lane_id: route for route in plan.assignments}

    expected = {
        "antigravity": ("requested_selected_propagation", False, "NOT_PROVEN"),
        "claude_code": ("served_response_envelope", True, "PROVEN"),
    }
    for lane, (kind, proven, evidence) in expected.items():
        result = execute_material_route(
            service,
            routes[lane],
            task_id=f"model-evidence-{lane}",
            cwd=tmp_path,
            prompt="Review candidate deadbeef",
            candidate_hash="deadbeef",
            review_lens=f"lens-{lane}",
        )
        assert result["ok"], (lane, result["reason"])
        claims = result["route_receipt"]["route_proof"]["claims"]
        assert claims["model_evidence_kind"] == kind, lane
        assert claims["served_model_proven"] is proven, lane
        assert claims["served_model_evidence"] == evidence, lane
        # A bare served identity can never be republished by a material receipt.
        assert "served_model_id" not in claims, lane
        assert "served_model_label" not in claims, lane


def test_owned_rail_publishes_a_receipt_from_the_shared_sealed_builder(
    tmp_path, monkeypatch
):
    """RED for the sequence-19 duplication finding.

    The owned rail used to hand-roll its public envelope, so the deny-by-default
    claim allowlist and the model-evidence class existed on only one of the two
    material rails.  Both rails must now construct their receipt through
    ``build_material_route_receipt`` so the two shapes cannot drift.
    """
    from tools import async_delegation as ad
    from tools import fleet_delegation as fd

    ad._reset_for_tests()
    try:
        harness = _material_harness(tmp_path, monkeypatch, lane="antigravity", pid=5181)
        seen = []
        inner = fd.build_material_route_receipt

        def _spy(*args, **kwargs):
            out = inner(*args, **kwargs)
            seen.append(out)
            return out

        monkeypatch.setattr(fd, "build_material_route_receipt", _spy)

        result = launch_fleet_owned_material_review(harness.ledger, **harness.launch)
        assert result["status"] == "launched"
        assert _drain(ad) == 0

        assert seen, "owned rail did not use the shared sealed receipt builder"
        _ok, _reason, receipt = seen[-1]
        claims = receipt["route_proof"]["claims"]
        assert claims["model_evidence_kind"] == "requested_selected_propagation"
        assert claims["served_model_proven"] is False
        assert claims["served_model_evidence"] == "NOT_PROVEN"
        assert "served_model_id" not in claims
        assert receipt["candidate_hash"] == "b" * 64
        assert receipt["review_lens"] == "runtime"
        assert receipt["receipt_hash"]
    finally:
        ad._reset_for_tests()


def test_both_material_rails_share_one_receipt_builder(tmp_path, monkeypatch):
    """A bounded AST census: exactly one construction site, two real callers."""
    import ast

    source = (_repo_root() / "tools" / "fleet_delegation.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    definitions = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "build_material_route_receipt"
    ]
    assert len(definitions) == 1, "the material receipt must have ONE construction site"

    callers = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and getattr(inner.func, "id", None) == "build_material_route_receipt"
            ):
                callers.add(node.name)
    # The superseded route-executor and the sealed owned rail, and nothing else.
    assert callers == {"execute_material_route", "_run_owned"}, callers


def test_material_child_is_not_spawned_when_containment_is_unavailable(
    tmp_path, monkeypatch
):
    """RED for B1: fail closed rather than spawn an unreapable prompt process.

    On a platform with no kernel owner-death guarantee (macOS today), spawning
    a provider CLI whose argv already carries the prompt would create a process
    that a sudden owner death could strand. The rail must refuse to spawn at
    all, release its lease, and say why — never downgrade silently.
    """
    from tools import async_delegation as ad
    from tools.process_registry import OwnerDeathContainment

    ad._reset_for_tests()
    try:
        harness = _material_harness(tmp_path, monkeypatch, pid=5191)
        monkeypatch.setattr(
            "tools.fleet_delegation.create_owner_death_containment",
            lambda: OwnerDeathContainment(
                "unavailable", False, "no kernel owner-death guarantee on platform"
            ),
        )

        result = launch_fleet_owned_material_review(harness.ledger, **harness.launch)
        assert result["status"] == "launched"
        assert _drain(ad) == 0

        # No provider child was ever created.
        assert harness.spawned == []
        # Exactly one Fleet lease was still released.
        assert len(_lease_finalizations(harness.service)) == 1
        # The durable saga must not claim a completed review.
        saga_state, _provisional, owned_handle = _saga_row(harness.ledger_path)
        assert saga_state == "TERMINAL"
        assert not owned_handle
    finally:
        ad._reset_for_tests()


def test_material_child_that_cannot_be_adopted_is_killed_and_fails_closed(
    tmp_path, monkeypatch
):
    """RED for B1: containment present but NOT in force must not run the child.

    `adopt()` returning False means the kernel guarantee was not established
    for this exact child even though the platform supports it (job assignment
    or resume failed).  On Windows the child is still suspended and has run
    nothing, so it must be killed and the spawn must fail closed rather than
    proceed with an unconstrained prompt-bearing process.
    """
    from tools import async_delegation as ad

    class _RefusingContainment(_FakeContainment):
        kind = "test_double_refusing"

        def adopt(self, process):
            self.adopted.append(process)
            return False

    ad._reset_for_tests()
    try:
        harness = _material_harness(tmp_path, monkeypatch, pid=5193)
        refusing = _RefusingContainment()
        monkeypatch.setattr(
            "tools.fleet_delegation.create_owner_death_containment", lambda: refusing
        )

        result = launch_fleet_owned_material_review(harness.ledger, **harness.launch)
        assert result["status"] == "launched"
        assert _drain(ad) == 0

        # Adoption was attempted on the exact child, and that child was killed.
        assert len(refusing.adopted) == 1
        assert harness.child.terminated + harness.child.killed >= 1
        # Exactly one Fleet lease released; the saga never claims a real review.
        assert len(_lease_finalizations(harness.service)) == 1
        saga_state, _provisional, owned_handle = _saga_row(harness.ledger_path)
        assert saga_state == "TERMINAL"
        assert not owned_handle
    finally:
        ad._reset_for_tests()


def test_receipt_allowlist_drops_any_qualification_claim_from_a_raw_proof():
    """RED for B4: the allowlist itself must refuse qualification claims.

    Even if some adapter emits `model_qualification`, the published receipt must
    not republish it: client-side propagation cannot qualify the model that
    served the request.  This drives the receipt builder directly with a raw
    proof containing the field, so the guarantee is the allowlist and not an
    accident of what adapters currently emit.
    """
    from hermes_cli.fleet.types import AdapterKind, AdapterResult
    from tools.fleet_delegation import FleetMaterialRoute, build_material_route_receipt

    route = FleetMaterialRoute(
        lane_id="antigravity", provider_id="google-antigravity",
        model_id="gemini-3.1-pro-high", adapter_kind="external_cli",
        qualification_evidence_id="qualification:antigravity",
        effective_execution_identity="identity:agy", available=True, reasons=(),
    )
    adapter_result = AdapterResult(
        ok=True, reason=ReasonCode.MET, provider_id=route.provider_id,
        model_id=route.model_id, auth_kind="cli_subscription",
        adapter_kind=AdapterKind.EXTERNAL_CLI, output="ok",
        metadata={"route_proof": {
            "model_qualification": "agy client-propagated selected model",
            "served_model_id": "gemini-3.1-pro-high",
            "model_evidence_source": "agy client model-override propagation log line",
            "model_evidence_kind": "requested_selected_propagation",
            "served_model_proven": False,
            "served_model_evidence": "NOT_PROVEN",
        }},
    )
    _ok, _reason, receipt = build_material_route_receipt(
        route, task_id="allowlist-check", candidate_hash="d" * 64,
        review_lens="runtime", prompt_fingerprint="sha256:x",
        identity_matches=True, adapter_result=adapter_result,
        result_ok=True, result_reason=ReasonCode.MET,
    )
    claims = receipt["route_proof"]["claims"]
    assert "model_qualification" not in claims
    assert "served_model_id" not in claims
    assert [key for key in claims if "qualification" in key] == []
    # The truthful evidence keys still survive the filter.
    assert claims["model_evidence_source"] == (
        "agy client model-override propagation log line"
    )
    assert claims["served_model_evidence"] == "NOT_PROVEN"


def test_every_owned_material_terminal_path_releases_exactly_one_lease(
    tmp_path, monkeypatch
):
    """Structural exactly-once: even an unenumerated escape cannot leak a lease.

    This drives the runner's own safety net rather than a named failure site, so
    a future edit that adds a new terminal path without its own finalization is
    still caught.
    """
    from tools import async_delegation as ad
    from tools import fleet_delegation as fd

    ad._reset_for_tests()
    try:
        harness = _material_harness(tmp_path, monkeypatch, pid=5158)

        def _escape(self, execution, task, delegation_id, receipt_id):
            raise KeyboardInterrupt("unenumerated escape after lease acquisition")

        monkeypatch.setattr(fd.OwnedMaterialRunner, "_run_owned", _escape)

        result = launch_fleet_owned_material_review(harness.ledger, **harness.launch)
        assert result["status"] == "launched"
        assert _drain(ad) == 0

        assert len(_lease_finalizations(harness.service)) == 1
    finally:
        ad._reset_for_tests()


# ---------------------------------------------------------------------------
# Production ingress.
#
# The material bridge is only real if a NON-TEST caller reaches it.  These tests
# drive the production dispatcher (which owns ledger resolution, route planning
# and the sealed-ingress call) rather than the module-private helper, and a
# bounded AST census proves the wiring exists in shipped code.
# ---------------------------------------------------------------------------

_CENSUS_ROOTS = ("tools", "hermes_cli", "gateway", "agent", "cron", "tui_gateway")
_CENSUS_FILES = ("cli.py", "run_agent.py", "model_tools.py", "toolsets.py")


def _repo_root():
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "AGENTS.md").exists() and (parent / "tools").is_dir():
            return parent
    raise AssertionError("repository root not found")


def _bounded_caller_census(symbol):
    """Deterministic, bounded AST census of production call sites for ``symbol``.

    Bounded by construction: a fixed root list, no test trees, no globbing of
    the whole filesystem.  Only real ``Call`` nodes count — an import or a
    string mention is not a caller.
    """
    import ast

    root = _repo_root()
    targets = []
    for name in _CENSUS_ROOTS:
        directory = root / name
        if directory.is_dir():
            targets.extend(sorted(directory.rglob("*.py")))
    targets.extend(root / name for name in _CENSUS_FILES if (root / name).exists())

    callers = {}
    for path in targets:
        parts = set(path.relative_to(root).parts)
        if "tests" in parts or path.name.startswith("test_"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        count = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute)
                else None
            )
            if name == symbol:
                count += 1
        if count:
            callers[str(path.relative_to(root)).replace("\\", "/")] = count
    return callers


def test_bounded_caller_census_proves_a_non_test_production_caller_for_the_sealed_ingress():
    """RED for F2/R3: the sealed ingress had zero non-test callers."""
    callers = _bounded_caller_census("launch_fleet_owned_material_review")
    assert callers, (
        "launch_fleet_owned_material_review has no production caller; the material "
        "bridge would be test-reachable only"
    )
    # The definition site itself must not be counted as a caller.
    assert "tools/release_review_launch.py" not in callers
    assert "tools/fleet_delegation.py" in callers


def test_production_dispatcher_reaches_sealed_material_ingress_and_binds_both_rails(
    tmp_path, monkeypatch
):
    """RED for F2/R3: E2E through the production dispatcher, not the private helper."""
    from tools import async_delegation as ad
    from tools.fleet_delegation import dispatch_material_review

    ad._reset_for_tests()
    try:
        harness = _material_harness(tmp_path, monkeypatch, pid=5160)
        result = dispatch_material_review(
            fleet_service=harness.service,
            ledger=harness.ledger,
            candidate_hash="b" * 64,
            review_lens="runtime",
            scope="release tests",
            lane="claude_code",
            prompt="review exact candidate",
            cwd=tmp_path,
            attempt_id=harness.launch["attempt_id"],
            fence_token=harness.launch["fence_token"],
            environment_fingerprint="env-b",
            evidence_fingerprint="evidence-b",
            preflight=harness.launch["preflight"],
            deadline_seconds=60,
            output_path="out.json",
            dispatch_kwargs={"goal": "review", "context": "candidate", "toolsets": None,
                             "role": "reviewer", "session_key": "test", "max_async_children": 1},
        )
        assert result["status"] == "launched"
        assert _drain(ad) == 0

        with closing(sqlite3.connect(harness.ledger_path)) as conn:
            saga_state, provisional, owned, pid = conn.execute(
                "SELECT saga_state, provisional_handle_id, external_handle_id, external_pid "
                "FROM workflow_material_route_plans"
            ).fetchone()
        assert saga_state == "TERMINAL"
        assert provisional and owned == provisional and pid == 5160
        with closing(sqlite3.connect(harness.hermes_home / "state.db")) as conn:
            outbox_handle, outbox_pid = conn.execute(
                "SELECT external_handle_id, external_pid FROM async_delegations"
            ).fetchone()
        assert (outbox_handle, outbox_pid) == (owned, 5160)
        assert len(_lease_finalizations(harness.service)) == 1
    finally:
        ad._reset_for_tests()


def test_production_dispatcher_defaults_to_the_live_fleet_composition_root(
    tmp_path, monkeypatch
):
    """The default (production) path must build the real live fleet service."""
    from hermes_cli.fleet import inspection
    from tools import fleet_delegation as fd

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    built = []

    def _fake_build(**kwargs):
        built.append(kwargs)
        raise RuntimeError("composition root reached")

    monkeypatch.setattr(inspection, "build_fleet_service", _fake_build)
    with pytest.raises(RuntimeError, match="composition root reached"):
        fd.dispatch_material_review(
            candidate_hash="c" * 64, review_lens="runtime", scope="release tests",
            lane="claude_code", prompt="review", cwd=tmp_path, attempt_id="attempt",
            fence_token=1, environment_fingerprint="env-c", evidence_fingerprint="evidence-c",
            preflight={}, deadline_seconds=60, output_path="out.json", dispatch_kwargs={},
        )
    assert built == [{}]


def test_production_dispatcher_fails_closed_without_candidate_or_lens(
    tmp_path, monkeypatch
):
    """Generic (non-material) requests must never reach the sealed ingress."""
    from tools import fleet_delegation as fd

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    reached = []
    monkeypatch.setattr(
        fd, "plan_material_routes",
        lambda *a, **k: reached.append(True),
    )
    for missing in ("candidate_hash", "review_lens", "prompt"):
        request = dict(
            candidate_hash="d" * 64, review_lens="runtime", scope="release tests",
            lane="claude_code", prompt="review", cwd=tmp_path, attempt_id="attempt",
            fence_token=1, environment_fingerprint="env-d", evidence_fingerprint="evidence-d",
            preflight={}, deadline_seconds=60, output_path="out.json", dispatch_kwargs={},
            fleet_service=object(), ledger=object(),
        )
        request[missing] = ""
        result = fd.dispatch_material_review(**request)
        assert result["status"] == "rejected"
    assert reached == []


def test_material_route_fails_closed_on_stale_or_non_comparable_capacity(
    tmp_path, monkeypatch
):
    """Distinct material-route safety under the restored ordinary capacity gate.

    Material work is the most expensive thing the fleet can start, so it gets no
    exemption from the fleet-wide rule: unknowable remaining quota means no
    route.  Transport health being UP is deliberately not sufficient.
    """
    service, _native_calls, _process_calls = _service(
        tmp_path, monkeypatch, capacity_source=_StaleCapacity()
    )
    plan = plan_material_routes(
        service, task_count=1, cwd=tmp_path,
        prompt_fingerprint="sha256:stale-capacity", preferred_lane_ids=("claude_code",),
    )
    assert not plan.assignments
    assert plan.degraded_route_capacity
    reasons = set(plan.unavailable[0].reasons)
    assert reasons & {
        ReasonCode.USAGE_STALE.value,
        ReasonCode.USAGE_NOT_COMPARABLE.value,
        ReasonCode.CAPACITY_STALE.value,
    }

    result = dispatch_material_review(
        fleet_service=service, ledger=object(), candidate_hash="e" * 64,
        review_lens="runtime", scope="release tests", lane="claude_code",
        prompt="review exact candidate", cwd=tmp_path, attempt_id="attempt",
        fence_token=1, environment_fingerprint="env-e", evidence_fingerprint="evidence-e",
        preflight={}, deadline_seconds=60, output_path="out.json", dispatch_kwargs={},
    )
    assert result["status"] == "rejected"
    assert result["error"] == "no qualified distinct fleet material route"
    assert result["degraded_route_capacity"] is True


def test_stale_pid_start_identity_cannot_cancel_a_recycled_owned_handle(
    tmp_path, monkeypatch
):
    """Owner-death race: a stale start identity must never signal a live PID."""
    from tools.process_registry import process_registry

    harness = _material_harness(tmp_path, monkeypatch, pid=5157)
    session = process_registry.register_owned_argv_process(
        harness.child, handle_id="mat_stale_identity", task_id="material-external-stale",
        cwd=str(tmp_path),
    )
    stale_start = (session.host_start_time or 0) + 4242
    assert not process_registry.cancel_owned_argv_process(
        "mat_stale_identity", pid=harness.child.pid, host_start_time=stale_start,
    )
    assert harness.child.terminated == 0 and harness.child.killed == 0
