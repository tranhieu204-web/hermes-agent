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
from typing import Any, Callable, Iterable, Mapping, Sequence

from hermes_cli.fleet.service import FleetService, OwnedExternalExecution
from hermes_cli.fleet.types import AdapterResult, LaneEvaluation, Qualification, ReasonCode, TaskSpec
from tools.release_review_ledger import canonical_effective_route_identity


_MATERIAL_CAPABILITIES = frozenset({"workspace_read", "shell"})


class _MaterialCancelled(Exception):
    """Internal signal: this exact runner was cancelled, not failed."""


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
        self._finalized = False
        # The opaque handle is minted BEFORE anything can execute so it can be
        # written to both durable rails as the pre-execution gate.  It carries
        # no argv, prompt, environment, or provider fact.
        self._handle_id = f"mat_{uuid.uuid4().hex}"
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

    def _terminate_owned_child(
        self, process: Any, *, pid: int | None, host_start_time: int | None, registered: bool,
    ) -> bool:
        """Terminate ONLY the exact child this runner created.

        Preference order is the registry's PID-plus-start-time verified path;
        if the child was never registered (the failure this guards), fall back
        to the process object we hold a reference to.  There is deliberately no
        PID scan, process-name match, or process-tree sweep: an unverifiable
        identity must not be signalled at all.
        """
        if registered and pid:
            try:
                from tools.process_registry import process_registry
                if process_registry.cancel_owned_argv_process(
                    self._handle_id, pid=int(pid), host_start_time=host_start_time,
                    source="material-review.binding-failure",
                ):
                    return True
            except Exception:
                pass
        for method in ("terminate", "kill"):
            call = getattr(process, method, None)
            if not callable(call):
                continue
            try:
                call()
                return True
            except Exception:
                continue
        return False

    def _finalize_once(
        self, execution: OwnedExternalExecution, adapter_result: AdapterResult,
    ) -> Any:
        """Release the Fleet lease exactly once across every terminal path."""
        with self._lock:
            if self._finalized:
                return None
            self._finalized = True
        return self._service.finalize_owned_external(execution, adapter_result)

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
            return self._run_owned(execution, task, delegation_id, receipt_id)
        finally:
            # Structural exactly-once guarantee.  Every terminal path below
            # finalizes with its own precise adapter result; this net only fires
            # when an unenumerated escape (registry bookkeeping error, or a
            # BaseException such as KeyboardInterrupt) would otherwise leak the
            # Fleet lease.  `_finalize_once` makes it a no-op after a precise
            # finalization, so the lease is released exactly once either way.
            try:
                self._finalize_once(execution, self._failure(execution, ReasonCode.EXECUTION_FAILED))
            except Exception:
                pass

    def _run_owned(
        self, execution: OwnedExternalExecution, task: TaskSpec,
        delegation_id: str, receipt_id: str,
    ) -> dict[str, object]:
        from tools.process_registry import process_registry
        from tools.release_review_ledger import ReleaseReviewLedger
        from tools import async_delegation

        # ---- Pre-execution durable gate -------------------------------------
        # Nothing below may construct a provider argv until BOTH durable rails
        # own this handle.  The prompt therefore cannot reach an executable
        # before the submission/lease/outbox ownership exists, and a crash in
        # this window leaves a PID-free saga that recovery terminalizes rather
        # than relaunching.
        handle_id = self._handle_id
        try:
            with self._lock:
                cancelled = self._cancelled
            if cancelled:
                raise _MaterialCancelled()
            ledger = ReleaseReviewLedger(self._ledger_path)
            ledger.bind_material_provisional_handle(
                receipt_id, attempt_id=self._attempt_id,
                fence_token=self._fence_token, handle_id=handle_id,
            )
            if not async_delegation.bind_material_provisional_handle(
                delegation_id, fence_token=self._fence_token, handle_id=handle_id,
            ):
                raise RuntimeError("async material provisional gate lost its current fence")
        except _MaterialCancelled:
            self._finalize_once(execution, self._failure(execution, ReasonCode.EXECUTION_CANCELLED))
            return {"status": "cancelled", "summary": "material review cancelled before the durable gate"}
        except Exception:
            self._finalize_once(execution, self._failure(execution, ReasonCode.EXECUTION_FAILED))
            return {"status": "error", "summary": "material pre-execution durable gate failed"}

        # ---- Child creation and owned-identity upgrade ----------------------
        try:
            run = execution.adapter.start_owned_material(execution.request, execution.qualification)
        except Exception:
            self._finalize_once(execution, self._failure(execution, ReasonCode.EXECUTION_FAILED))
            return {"status": "error", "summary": "external material child could not start"}

        process = run.process
        registered = False
        session = None
        try:
            session = process_registry.register_owned_argv_process(
                process, handle_id=handle_id, task_id=task.task_id, cwd=str(self._cwd),
            )
            registered = True
            with self._lock:
                self._owned = (handle_id, int(process.pid), session.host_start_time)
                cancelled = self._cancelled
            ledger.bind_material_owned_handle(
                receipt_id, attempt_id=self._attempt_id, fence_token=self._fence_token,
                handle_id=handle_id, pid=int(process.pid), host_start_time=session.host_start_time,
            )
            if not async_delegation.bind_material_external_handle(
                delegation_id, fence_token=self._fence_token, handle_id=handle_id,
                pid=int(process.pid), host_start_time=session.host_start_time,
            ):
                raise RuntimeError("async material handle binding lost its current fence")
            if cancelled:
                raise _MaterialCancelled()
        except BaseException as exc:
            # The child exists but is not fully owned: terminate exactly it and
            # release exactly one Fleet lease.  This must not re-raise, or the
            # lease would leak the way an escaped registration error used to.
            self._terminate_owned_child(
                process, pid=getattr(process, "pid", None),
                host_start_time=getattr(session, "host_start_time", None),
                registered=registered,
            )
            was_cancel = isinstance(exc, _MaterialCancelled)
            self._finalize_once(execution, self._failure(
                execution,
                ReasonCode.EXECUTION_CANCELLED if was_cancel else ReasonCode.EXECUTION_FAILED,
            ))
            return {
                "status": "cancelled" if was_cancel else "error",
                "summary": (
                    "external material child cancelled before ownership completed"
                    if was_cancel else "external material handle binding failed"
                ),
            }

        # ---- Owned execution ------------------------------------------------
        try:
            result = run.finish()
        except BaseException:
            self._terminate_owned_child(
                process, pid=int(process.pid),
                host_start_time=session.host_start_time, registered=True,
            )
            self._finalize_once(execution, self._failure(execution, ReasonCode.EXECUTION_FAILED))
            return {"status": "error", "summary": "owned external material execution failed"}
        try:
            process_registry.complete_owned_argv_process(
                handle_id, pid=int(process.pid), host_start_time=session.host_start_time,
                returncode=getattr(process, "returncode", None),
            )
        except Exception:
            # The child already exited, so there is nothing to terminate — but
            # the lease still has to be released on this path rather than
            # escaping into the caller with the lease held.
            self._finalize_once(execution, self._failure(execution, ReasonCode.EXECUTION_FAILED))
            return {"status": "error", "summary": "owned external material completion bookkeeping failed"}
        final = self._finalize_once(execution, result)
        if final is None:  # pragma: no cover - defensive; only a double-finalize path
            return {"status": "error", "summary": "owned external material lease was already finalized"}
        with self._lock:
            cancelled = self._cancelled
        # Build the public envelope through the SAME sealed receipt helper the
        # other material rail uses, rather than hand-rolling a second shape.
        # That is what keeps the deny-by-default claim allowlist — including the
        # model-evidence class — identical on both rails.
        pin = getattr(final, "pin", None) or execution.pin
        _ok, _reason, receipt = build_material_route_receipt(
            self._route,
            task_id=task.task_id,
            candidate_hash=self._candidate_hash,
            review_lens=self._review_lens,
            prompt_fingerprint=str(task.prompt_fingerprint),
            identity_matches=bool(
                pin is not None
                and pin.lane_id == self._route.lane_id
                and pin.provider_id == self._route.provider_id
                and pin.model_id == self._route.model_id
                and pin.adapter_kind.value == self._route.adapter_kind
            ),
            adapter_result=getattr(final, "adapter_result", None) or result,
            result_ok=bool(final.ok),
            result_reason=final.reason,
        )
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
                "route_receipt": receipt,
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


def dispatch_material_review(
    *,
    candidate_hash: str,
    review_lens: str,
    scope: str,
    lane: str,
    prompt: str,
    cwd: Path | str,
    attempt_id: str,
    fence_token: int,
    environment_fingerprint: str,
    evidence_fingerprint: str,
    preflight: Mapping[str, object],
    deadline_seconds: float,
    output_path: str,
    dispatch_kwargs: Mapping[str, object],
    ledger: Any = None,
    fleet_service: FleetService | None = None,
    receipt_id: str | None = None,
) -> dict[str, object]:
    """Production dispatcher for one candidate-bound owned material review.

    This is the shipped composition root for the material bridge: it resolves
    the canonical Hermes review ledger, builds the live fleet service through
    ``hermes_cli.fleet.inspection.build_fleet_service``, plans the single
    canonical route, and hands the result to the sealed receipt-bound ingress.

    It is deliberately the ONLY production entry point.  Generic
    ``delegate_task`` and the public async launcher stay fail-closed for
    material work, and a request that does not carry a candidate, lens, and
    prompt is rejected here before any route is planned or any child exists.
    """
    from hermes_cli.fleet.inspection import build_fleet_service
    from hermes_constants import get_hermes_home
    from tools.release_review_ledger import ReleaseReviewLedger
    from tools.release_review_launch import launch_fleet_owned_material_review

    candidate_hash = str(candidate_hash or "").strip().lower()
    review_lens = str(review_lens or "").strip().lower()
    prompt = str(prompt or "").strip()
    lane = str(lane or "").strip()
    if not candidate_hash or not review_lens or not prompt or not lane:
        return {
            "status": "rejected",
            "error": "material reviews require a candidate_hash, review_lens, lane, and prompt",
        }

    service = build_fleet_service() if fleet_service is None else fleet_service
    if ledger is None:
        ledger = ReleaseReviewLedger(get_hermes_home() / "release-review-ledger.db")

    resolved_cwd = Path(cwd).resolve()
    plan = plan_material_routes(
        service,
        task_count=1,
        cwd=resolved_cwd,
        prompt_fingerprint=_sha256_json(
            {
                "candidate_hash": candidate_hash,
                "review_lens": review_lens,
                "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            }
        ),
        preferred_lane_ids=(lane,),
    )
    if not plan.assignments:
        return {
            "status": "rejected",
            "error": "no qualified distinct fleet material route",
            "degraded_route_capacity": plan.degraded_route_capacity,
            "unavailable": [item.public_receipt() for item in plan.unavailable],
        }
    route = plan.assignments[0]

    bound_dispatch = dict(dispatch_kwargs)
    bound_dispatch["model"] = route.model_id
    return launch_fleet_owned_material_review(
        ledger,
        fleet_service=service,
        cwd=str(resolved_cwd),
        attempt_id=attempt_id,
        fence_token=int(fence_token),
        dispatch_kwargs=bound_dispatch,
        receipt_id=receipt_id,
        candidate_hash=candidate_hash,
        scope=scope,
        lane=lane,
        model=route.model_id,
        prompt=prompt,
        deadline_seconds=deadline_seconds,
        output_path=output_path,
        environment_fingerprint=environment_fingerprint,
        evidence_fingerprint=evidence_fingerprint,
        effective_route_identity=route.effective_execution_identity,
        review_lens=review_lens,
        preflight=preflight,
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
    ok, reason, receipt = build_material_route_receipt(
        route,
        task_id=task_id,
        candidate_hash=candidate_hash,
        review_lens=review_lens,
        prompt_fingerprint=prompt_fingerprint,
        identity_matches=identity_matches,
        adapter_result=result.adapter_result,
        result_ok=bool(result.ok),
        result_reason=result.reason,
    )
    adapter_result = result.adapter_result
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


def build_material_route_receipt(
    route: FleetMaterialRoute,
    *,
    task_id: str,
    candidate_hash: str,
    review_lens: str,
    prompt_fingerprint: str,
    identity_matches: bool,
    adapter_result: AdapterResult | None,
    result_ok: bool,
    result_reason: ReasonCode,
) -> tuple[bool, str, dict[str, object]]:
    """Build the single canonical public material route receipt.

    This is the one place a material receipt is constructed.  Both material
    rails call it — the superseded ``execute_material_route`` path and the
    sealed owned-external rail — so the deny-by-default claim allowlist, the
    identity/adapter cross-checks and the receipt hash cannot drift apart
    between them.  It performs no I/O and records no event; the caller owns
    persistence, because the two rails persist through different stores.
    """
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
    if result_ok and identity_matches and not adapter_provider_matches:
        reason = ReasonCode.PROVIDER_MISMATCH.value
    elif result_ok and identity_matches and not adapter_model_matches:
        reason = ReasonCode.MODEL_MISMATCH.value
    elif result_ok and identity_matches and not adapter_kind_matches:
        reason = ReasonCode.QUALIFICATION_FAILED.value
    else:
        reason = (
            result_reason.value
            if identity_matches or not result_ok
            else ReasonCode.PROVIDER_MISMATCH.value
        )
    ok = bool(
        result_ok
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
    #
    # The model-evidence keys are allowlisted as a set so a published claim is
    # always self-describing: `model_evidence_kind` / `served_model_proven` /
    # `served_model_evidence` travel with the identity they qualify.  A bare
    # `served_model_id` is deliberately NOT allowlisted — a lane that only has
    # client-side propagation evidence (Antigravity) must not be able to
    # republish it as a provider-returned served identity.
    allowed_proof = {
        key: raw_route_proof[key]
        for key in (
            "version", "requested_model_id", "requested_selected_model_id",
            "requested_selected_model_label", "model_evidence_kind",
            "served_model_proven", "served_model_evidence",
            "effort", "auth_kind", "fast_mode",
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
    return ok, reason, receipt
