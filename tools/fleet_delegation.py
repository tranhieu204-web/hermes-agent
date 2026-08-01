"""Dispatcher-owned bridge from material delegation to the canonical fleet.

This module contains no provider credentials and no alternate execution
stack. It plans against ``FleetService`` qualifications, reserves one distinct
effective route per material task, executes through the service's native or
external adapter, and records a non-secret candidate-bound result receipt.

The generic ``delegate_task`` and async outbox wire this bridge in separately;
keeping the bridge isolated makes the provider contract testable without
changing the current parent-agent child constructor.
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from hermes_cli.fleet.service import FleetService, OwnedExternalExecution
from hermes_cli.fleet.types import AdapterResult, LaneEvaluation, Qualification, ReasonCode, TaskSpec
from tools.release_review_ledger import canonical_effective_route_identity


_MATERIAL_CAPABILITIES = frozenset({"workspace_read", "shell"})


def _sha256_json(value: object) -> str:
    raw = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class FleetMaterialRoute:
    lane_id: str
    provider_id: str
    model_id: str
    adapter_kind: str
    qualification_evidence_id: str
    effective_execution_identity: str
    available: bool
    reasons: tuple[str, ...]

    def public_receipt(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class FleetMaterialPlan:
    assignments: tuple[FleetMaterialRoute, ...]
    unavailable: tuple[FleetMaterialRoute, ...]
    requested: int

    @property
    def degraded_route_capacity(self) -> bool:
        return len(self.assignments) < self.requested


class OwnedMaterialRunner:
    """Private sealed runner for one already-planned external material lane.

    It never delegates through the generic batch constructor.  The process is
    argv-only and its raw protocol output is consumed in memory; only the
    opaque handle/PID/start identity cross the durable async and ledger rails.
    """

    def __init__(
        self, *, service: FleetService, route: FleetMaterialRoute, cwd: Path,
        prompt: str, candidate_hash: str, review_lens: str, ledger_path: Path,
        attempt_id: str, fence_token: int,
    ) -> None:
        self._service = service
        self._route = route
        self._cwd = Path(cwd).resolve()
        self._prompt = str(prompt)
        self._candidate_hash = str(candidate_hash).lower()
        self._review_lens = str(review_lens).lower()
        self._ledger_path = Path(ledger_path).resolve()
        self._attempt_id = str(attempt_id)
        self._fence_token = int(fence_token)
        self._lock = threading.Lock()
        self._cancelled = False
        self._owned: tuple[str, int, int | None] | None = None

    def interrupt(self) -> None:
        """Signal only the exact durable child that this runner owns."""
        with self._lock:
            self._cancelled = True
            owned = self._owned
        if owned is not None:
            from tools.process_registry import process_registry
            process_registry.cancel_owned_argv_process(
                owned[0], pid=owned[1], host_start_time=owned[2],
                source="material-review.cancel",
            )

    def factory(self, delegation_id: str, receipt_id: str) -> Callable[[], dict[str, object]]:
        return lambda: self._run(delegation_id, receipt_id)

    def _failure(self, execution: OwnedExternalExecution, reason: ReasonCode) -> AdapterResult:
        return AdapterResult(
            ok=False, reason=reason, provider_id=execution.pin.provider_id,
            model_id=execution.pin.model_id, auth_kind=execution.qualification.auth_kind or "unknown",
            adapter_kind=execution.pin.adapter_kind,
        )

    def _run(self, delegation_id: str, receipt_id: str) -> dict[str, object]:
        task = TaskSpec(
            task_id=f"material-external-{delegation_id}", cwd=self._cwd,
            required_capabilities=_MATERIAL_CAPABILITIES,
            reservation_pct=self._service.config.default_reservation_pct,
            prompt_fingerprint=_sha256_json({
                "candidate_hash": self._candidate_hash, "review_lens": self._review_lens,
                "prompt_sha256": hashlib.sha256(self._prompt.encode("utf-8")).hexdigest(),
            }),
        )
        with self._lock:
            cancelled = self._cancelled
        if cancelled:
            return {"status": "cancelled", "summary": "material review cancelled before child start"}
        acquired = self._service.acquire_owned_external(
            task, prompt=self._prompt, preferred_lane_id=self._route.lane_id,
        )
        if not isinstance(acquired, OwnedExternalExecution):
            return {"status": "error", "summary": "qualified external material lane was not acquirable", "reason": acquired.reason.value}
        execution = acquired
        try:
            run = execution.adapter.start_owned_material(execution.request, execution.qualification)
        except Exception:
            self._service.finalize_owned_external(execution, self._failure(execution, ReasonCode.EXECUTION_FAILED))
            return {"status": "error", "summary": "external material child could not start"}

        handle_id = f"mat_{uuid.uuid4().hex}"
        from tools.process_registry import process_registry
        from tools.release_review_ledger import ReleaseReviewLedger
        from tools import async_delegation
        session = process_registry.register_owned_argv_process(
            run.process, handle_id=handle_id, task_id=task.task_id, cwd=str(self._cwd),
        )
        with self._lock:
            self._owned = (handle_id, int(run.process.pid), session.host_start_time)
            cancelled = self._cancelled
        if cancelled:
            self.interrupt()
        try:
            ledger = ReleaseReviewLedger(self._ledger_path)
            ledger.bind_material_owned_handle(
                receipt_id, attempt_id=self._attempt_id, fence_token=self._fence_token,
                handle_id=handle_id, pid=int(run.process.pid), host_start_time=session.host_start_time,
            )
            if not async_delegation.bind_material_external_handle(
                delegation_id, fence_token=self._fence_token, handle_id=handle_id,
                pid=int(run.process.pid), host_start_time=session.host_start_time,
            ):
                raise RuntimeError("async material handle binding lost its current fence")
        except Exception:
            self.interrupt()
            self._service.finalize_owned_external(execution, self._failure(execution, ReasonCode.EXECUTION_FAILED))
            return {"status": "error", "summary": "external material handle binding failed"}

        result = run.finish()
        process_registry.complete_owned_argv_process(
            handle_id, pid=int(run.process.pid), host_start_time=session.host_start_time,
            returncode=getattr(run.process, "returncode", None),
        )
        final = self._service.finalize_owned_external(execution, result)
        with self._lock:
            cancelled = self._cancelled
        return {
            "status": "cancelled" if cancelled else ("completed" if final.ok else "error"),
            "summary": "owned external material review finished",
            "material_review_public": {
                "candidate_hash": self._candidate_hash,
                "review_lens": self._review_lens,
                "effective_execution_identity": self._route.effective_execution_identity,
                "lane_id": self._route.lane_id,
                "provider_id": self._route.provider_id,
                "model_id": self._route.model_id,
                "adapter_kind": self._route.adapter_kind,
                "outcome": final.reason.value,
            },
        }


def _route_from_evaluation(
    item: LaneEvaluation,
    qualification: Qualification | None,
) -> FleetMaterialRoute:
    model_id = str(item.selected_model or "")
    provider_id = str(item.profile.provider_id)
    adapter_kind = item.profile.adapter_kind.value
    qualification_evidence_id = str(item.qualification_evidence_id or "")
    # The release-review ledger is the single authority for backing-route
    # identity.  A lane id and qualification receipt are presentation data;
    # they cannot make aliases independent.
    effective_identity = canonical_effective_route_identity(
        provider=provider_id,
        base_url=None,
        account_secret=None,
        model=model_id,
        adapter_kind=adapter_kind,
        auth_kind=qualification.auth_kind if qualification is not None else None,
        auth_source=qualification.auth_source if qualification is not None else None,
        executable=qualification.executable if qualification is not None else None,
    )
    return FleetMaterialRoute(
        lane_id=item.lane_id,
        provider_id=provider_id,
        model_id=model_id,
        adapter_kind=adapter_kind,
        qualification_evidence_id=qualification_evidence_id,
        effective_execution_identity=effective_identity,
        available=bool(item.eligible and model_id and qualification_evidence_id),
        reasons=tuple(reason.value for reason in item.reasons),
    )


def plan_material_routes(
    service: FleetService,
    *,
    task_count: int,
    cwd: Path,
    prompt_fingerprint: str,
    preferred_lane_ids: Sequence[str] | None = None,
    exclude_effective_identities: Iterable[str] = (),
) -> FleetMaterialPlan:
    """Reserve a deterministic set of distinct qualified route identities.

    This is a read-only plan. The actual lane lease remains atomic inside
    ``FleetService.run(..., preferred_lane_id=...)``.
    """

    if isinstance(task_count, bool) or not isinstance(task_count, int) or task_count < 1:
        raise ValueError("task_count must be a positive integer")
    resolved_cwd = Path(cwd).resolve()
    if not resolved_cwd.is_dir():
        raise ValueError("cwd must be an existing directory")
    fingerprint = str(prompt_fingerprint or "").strip()
    if not fingerprint:
        raise ValueError("prompt_fingerprint is required")

    task = TaskSpec(
        task_id=f"read-only-material-plan-{hashlib.sha256(fingerprint.encode()).hexdigest()[:16]}",
        cwd=resolved_cwd,
        required_capabilities=_MATERIAL_CAPABILITIES,
        reservation_pct=Decimal("0"),
        prompt_fingerprint=fingerprint,
    )
    preferred = (
        tuple(str(lane).strip() for lane in preferred_lane_ids)
        if preferred_lane_ids is not None
        else tuple(profile.lane_id for profile in service.profiles)
    )
    if any(not lane for lane in preferred) or len(set(preferred)) != len(preferred):
        raise ValueError("preferred_lane_ids must be unique non-empty lane ids")

    by_lane: dict[str, FleetMaterialRoute] = {}
    for lane_id in preferred:
        evaluations = service.inspect(task, lane_id=lane_id)
        if not evaluations:
            by_lane[lane_id] = FleetMaterialRoute(
                lane_id=lane_id,
                provider_id="",
                model_id="",
                adapter_kind="",
                qualification_evidence_id="",
                effective_execution_identity=_sha256_json(
                    {"schema_version": 1, "lane_id": lane_id, "status": "unknown"}
                ),
                available=False,
                reasons=(ReasonCode.NO_ELIGIBLE_LANE.value,),
            )
            continue
        by_lane[lane_id] = _route_from_evaluation(
            evaluations[0],
            service.qualifications.get(lane_id),
        )

    excluded = {str(value).strip() for value in exclude_effective_identities if str(value).strip()}
    assignments: list[FleetMaterialRoute] = []
    unavailable: list[FleetMaterialRoute] = []
    used = set(excluded)
    for lane_id in preferred:
        route = by_lane[lane_id]
        if (
            not route.available
            or route.effective_execution_identity in used
            or len(assignments) >= task_count
        ):
            if len(assignments) < task_count:
                unavailable.append(route)
            continue
        assignments.append(route)
        used.add(route.effective_execution_identity)

    return FleetMaterialPlan(
        assignments=tuple(assignments),
        unavailable=tuple(unavailable),
        requested=task_count,
    )


def execute_material_route(
    service: FleetService,
    route: FleetMaterialRoute,
    *,
    task_id: str,
    cwd: Path,
    prompt: str,
    candidate_hash: str,
    review_lens: str,
) -> dict[str, object]:
    """Execute one previously planned route and commit a public route receipt."""

    if not route.available:
        raise ValueError(f"fleet route {route.lane_id!r} is unavailable")
    task_id = str(task_id or "").strip()
    prompt = str(prompt or "").strip()
    candidate_hash = str(candidate_hash or "").strip().lower()
    review_lens = str(review_lens or "").strip().lower()
    if not task_id or not prompt or not candidate_hash or not review_lens:
        raise ValueError(
            "task_id, prompt, candidate_hash, and review_lens are required"
        )

    prompt_fingerprint = _sha256_json(
        {
            "candidate_hash": candidate_hash,
            "review_lens": review_lens,
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        }
    )
    result = service.run(
        TaskSpec(
            task_id=task_id,
            cwd=Path(cwd).resolve(),
            required_capabilities=_MATERIAL_CAPABILITIES,
            reservation_pct=service.config.default_reservation_pct,
            prompt_fingerprint=prompt_fingerprint,
        ),
        prompt=prompt,
        preferred_lane_id=route.lane_id,
    )
    actual_pin = result.pin
    identity_matches = bool(
        actual_pin is not None
        and actual_pin.lane_id == route.lane_id
        and actual_pin.provider_id == route.provider_id
        and actual_pin.model_id == route.model_id
        and actual_pin.adapter_kind.value == route.adapter_kind
    )
    adapter_result = result.adapter_result
    adapter_provider_matches = bool(
        adapter_result is not None
        and adapter_result.provider_id == route.provider_id
    )
    adapter_model_matches = bool(
        adapter_result is not None
        and adapter_result.model_id == route.model_id
    )
    adapter_kind_matches = bool(
        adapter_result is not None
        and adapter_result.adapter_kind.value == route.adapter_kind
    )
    if result.ok and identity_matches and not adapter_provider_matches:
        reason = ReasonCode.PROVIDER_MISMATCH.value
    elif result.ok and identity_matches and not adapter_model_matches:
        reason = ReasonCode.MODEL_MISMATCH.value
    elif result.ok and identity_matches and not adapter_kind_matches:
        reason = ReasonCode.QUALIFICATION_FAILED.value
    else:
        reason = (
            result.reason.value
            if identity_matches or not result.ok
            else ReasonCode.PROVIDER_MISMATCH.value
        )
    ok = bool(
        result.ok
        and identity_matches
        and adapter_provider_matches
        and adapter_model_matches
        and adapter_kind_matches
    )
    raw_route_proof = (
        dict(adapter_result.metadata.get("route_proof") or {})
        if adapter_result is not None
        else {}
    )
    # Provider adapters may use executable paths or endpoint details for local
    # qualification.  A material receipt must not republish those values.
    allowed_proof = {
        key: raw_route_proof[key]
        for key in (
            "version", "requested_model_id", "served_model_id",
            "served_model_label", "effort", "auth_kind", "fast_mode",
            "fallback_enabled", "model_qualification",
        )
        if key in raw_route_proof
        and isinstance(raw_route_proof[key], (str, bool, int, float))
    }
    route_proof = {
        "schema_version": 1,
        "identity_hash": _sha256_json({"effective_execution_identity": route.effective_execution_identity}),
        "proof_hash": _sha256_json(raw_route_proof),
        "claims": allowed_proof,
    }
    receipt: dict[str, object] = {
        "schema_version": 1,
        "task_id": task_id,
        "candidate_hash": candidate_hash,
        "review_lens": review_lens,
        "lane_id": route.lane_id,
        "provider_id": route.provider_id,
        "model_id": route.model_id,
        "adapter_kind": route.adapter_kind,
        "qualification_evidence_id": route.qualification_evidence_id,
        "effective_execution_identity": route.effective_execution_identity,
        "prompt_fingerprint": prompt_fingerprint,
        "status": "COMMITTED" if ok else "FAILED",
        "reason": reason,
        "route_proof": route_proof,
    }
    receipt["receipt_hash"] = _sha256_json(receipt)
    service.store.record_event(
        at=service._now().astimezone(),  # noqa: SLF001 - same service clock
        task_id=task_id,
        lane_id=route.lane_id,
        event_type="MATERIAL_RESULT_COMMITTED" if ok else "MATERIAL_RESULT_FAILED",
        reason=reason,
        decision=receipt,
    )
    return {
        "ok": ok,
        "reason": reason,
        "output": (
            adapter_result.output
            if adapter_result is not None and isinstance(adapter_result.output, str)
            else ""
        ),
        "route_receipt": receipt,
    }
