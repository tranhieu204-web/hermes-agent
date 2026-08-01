"""Single, fail-closed launch adapter for direct and async release reviewers."""
from __future__ import annotations

import os
import subprocess
import threading
import uuid
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

from hermes_constants import get_hermes_home
from tools.release_review_ledger import ReleaseReviewLedger


# This capability is module-private by construction.  Only the sealed fleet
# material ingress may pass it to the launcher; public/generic callers cannot
# select a runner after the route-plan receipt is admitted.
_MATERIAL_LAUNCH_CAPABILITY = object()


def _admit_capture_claim(ledger: ReleaseReviewLedger, receipt_id: Optional[str], **request: Any) -> Dict[str, Any]:
    preflight = request.pop("preflight")
    decision_id = request.pop("decision_id", "")
    if decision_id:
        ledger.require_decision(decision_id)
    receipt = ledger.admit(receipt_id=receipt_id, **request)
    if receipt["status"] != "admitted":
        return receipt
    ledger.capture_preflight(receipt["receipt_id"], preflight)
    claim = ledger.claim_launch(receipt["receipt_id"])
    return {**receipt, "claim": claim}


def launch_shell_review(
    ledger: ReleaseReviewLedger, *, command: Sequence[str], receipt_id: Optional[str] = None,
    popen: Callable[..., Any] = subprocess.Popen, restart_recovery_mode: Optional[str] = None,
    **request: Any,
) -> Dict[str, Any]:
    """Claim before starting a direct reviewer; commands never use ``shell=True``.

    Direct Popen ownership is intentionally confined to this process.  A caller
    must acknowledge that it does not require durable restart recovery.
    """
    if restart_recovery_mode != "current_process_only":
        return {
            "status": "rejected",
            "error": "direct-shell reviews require restart_recovery_mode='current_process_only'; durable restart recovery is unsupported",
        }
    receipt = _admit_capture_claim(ledger, receipt_id, **request)
    if receipt["status"] != "admitted" or receipt["claim"]["status"] != "claimed":
        return receipt
    if not command or not all(isinstance(part, str) and part for part in command):
        ledger.mark_launch_failed(receipt["receipt_id"])
        raise ValueError("shell review command must be a non-empty argument sequence")
    try:
        process = popen(list(command), shell=False)
    except Exception:
        ledger.mark_launch_failed(receipt["receipt_id"])
        raise
    try:
        ledger.attach_processes(receipt["receipt_id"], int(process.pid), int(process.pid), f"pid:{int(process.pid)}:single-process")
    except RuntimeError:
        if ledger.receipt_state(receipt["receipt_id"]) != "timebox_expired":
            if process.poll() is None:
                process.terminate()
            ledger.mark_launch_failed(receipt["receipt_id"])
            raise
        # The only process touched here is the object just returned by this
        # launch.  If an external timeout won before attachment, preserve that
        # terminal outcome and never turn it into a misleading launch error.
        evidence = {
            "reason": "receipt expired before direct attachment",
            "process_handle": f"pid:{int(process.pid)}:single-process",
            "termination": "requested" if process.poll() is None else "already_exited",
        }
        ledger.finalize_async_receipt(receipt["receipt_id"], "timebox_expired", evidence)
        if process.poll() is None:
            try:
                process.terminate()
            except Exception as exc:  # evidence is already durable; do not lose it
                evidence["termination_error"] = f"{type(exc).__name__}: {exc}"
        return {**receipt, "status": "timebox_expired", "root_pid": int(process.pid), "leaf_pid": int(process.pid)}
    except Exception:
        if process.poll() is None:
            process.terminate()
        ledger.mark_launch_failed(receipt["receipt_id"])
        raise

    def _timeout_direct_process():
        # Write the durable timeout receipt before signalling the owned process.
        ledger.finalize_async_receipt(
            receipt["receipt_id"], "timebox_expired",
            {"reason": "direct review deadline", "process_handle": f"pid:{int(process.pid)}:single-process"},
        )
        if process.poll() is None:
            process.terminate()

    ledger.supervise_deadline(receipt["receipt_id"], _timeout_direct_process)
    def _watch_exit():
        code = process.poll()
        if code is None:
            retry = threading.Timer(0.01, _watch_exit)
            retry.daemon = True
            retry.start()
            return
        ledger.finalize_direct_receipt(receipt["receipt_id"], int(code), {"process_handle": f"pid:{int(process.pid)}"})
    watcher = threading.Timer(0.01, _watch_exit)
    watcher.daemon = True
    watcher.start()
    return {**receipt, "status": "launched", "root_pid": int(process.pid), "leaf_pid": int(process.pid)}


def _launch_async_review(
    ledger: ReleaseReviewLedger, *, dispatch: Callable[..., Dict[str, Any]],
    dispatch_kwargs: Mapping[str, Any], receipt_id: Optional[str] = None,
    already_claimed: bool = False, **request: Any,
) -> Dict[str, Any]:
    """Claim before forwarding to the existing async-delegation dispatcher."""
    if ledger.path.resolve() != (get_hermes_home() / "release-review-ledger.db").resolve():
        return {"status": "rejected", "error": "async reviews require the canonical Hermes ledger path"}
    if already_claimed:
        if not receipt_id or ledger.receipt_state(receipt_id) != "launching":
            return {"status": "rejected", "error": "material route receipt is not an active launch claim"}
        receipt = {"status": "admitted", "receipt_id": receipt_id, "claim": {"status": "claimed", "receipt_id": receipt_id}}
    else:
        receipt = _admit_capture_claim(ledger, receipt_id, **request)
        if receipt["status"] != "admitted" or receipt["claim"]["status"] != "claimed":
            return receipt
    if not callable(dispatch_kwargs.get("interrupt_fn")):
        ledger.mark_launch_failed(receipt["receipt_id"], "launch_rejected")
        return {**receipt, "status": "rejected", "dispatch": {"error": "receipt-bound async reviews require interrupt_fn"}}
    try:
        # Bind the delegation identity before the dispatcher can submit its
        # runner.  A very fast runner used to be able to finalize before the
        # post-dispatch attach below, leaving a completed delegation without a
        # durable receipt-to-owner link.
        delegation_id = f"deleg_{uuid.uuid4().hex[:8]}"
        ledger.bind_async_dispatch(receipt["receipt_id"], delegation_id, os.getpid())
        dispatch_input = dict(dispatch_kwargs)
        runner_factory = dispatch_input.pop("_sealed_material_runner_factory", None)
        if runner_factory is not None:
            if not callable(runner_factory):
                raise ValueError("sealed material runner factory must be callable")
            # The runner is constructed only after the durable receipt and
            # delegation id exist, but before executor submission.  This
            # avoids the old closure race where a child could start before it
            # knew which receipt/fence it was required to bind.
            dispatch_input["runner"] = runner_factory(delegation_id, receipt["receipt_id"])
        dispatch_input["delegation_id"] = delegation_id
        dispatch_input["review_receipt_id"] = receipt["receipt_id"]
        dispatch_input["review_ledger_path"] = str(ledger.path)
        dispatch_input["candidate_hash"] = request["candidate_hash"]
        dispatch_input["effective_execution_identity"] = request.get("effective_route_identity") or request["model"]
        dispatch_input["review_fence_token"] = int(dispatch_input.get("review_fence_token", 0) or 0)
        result = dispatch(**dispatch_input)
    except Exception:
        ledger.mark_launch_failed(receipt["receipt_id"])
        raise
    if result.get("status") != "dispatched":
        # The async dispatcher can already have terminalized a receipt while
        # retaining an UNKNOWN durable outbox for crash recovery.  Do not turn
        # that idempotent rejection into a second state-transition error.
        if ledger.receipt_state(receipt["receipt_id"]) in {"launching", "dispatching", "running"}:
            ledger.mark_launch_failed(receipt["receipt_id"], "launch_rejected")
        return {**receipt, "status": "rejected", "dispatch": result}
    returned_id = result.get("delegation_id")
    if returned_id != delegation_id:
        ledger.mark_launch_failed(receipt["receipt_id"])
        raise RuntimeError("async reviewer returned an unexpected delegation identity")
    if ledger.receipt_state(receipt["receipt_id"]) not in {"running", "completed", "failed", "unknown", "timebox_expired"}:
        ledger.mark_launch_failed(receipt["receipt_id"])
        return {**receipt, "status": "rejected", "dispatch": {"error": "async dispatcher did not activate the durable receipt"}}
    # Async dispatcher exposes the supplied interrupt function in its record;
    # a deadline only signals that dedicated review, never unrelated work.
    def _interrupt():
        from tools.async_delegation import force_timeout_review_receipt
        force_timeout_review_receipt(receipt["receipt_id"], "release review deadline")
    ledger.supervise_deadline(receipt["receipt_id"], _interrupt)
    return {**receipt, "status": "launched", "dispatch": result, "root_pid": os.getpid(), "leaf_pid": None}


def launch_async_review(
    ledger: ReleaseReviewLedger, *, dispatch: Callable[..., Dict[str, Any]],
    dispatch_kwargs: Mapping[str, Any], receipt_id: Optional[str] = None, **request: Any,
) -> Dict[str, Any]:
    """Public generic launcher; recovery-bound reviews have no public bypass."""
    if dispatch_kwargs.get("recovery_attempt_id") or "_sealed_material_runner_factory" in dispatch_kwargs:
        return {"status": "rejected", "error": "material reviews require the receipt-bound material adapter"}
    return _launch_async_review(
        ledger, dispatch=dispatch, dispatch_kwargs=dispatch_kwargs, receipt_id=receipt_id, **request,
    )


def _launch_material_async_review(
    ledger: ReleaseReviewLedger, *, attempt_id: str, fence_token: int,
    dispatch_kwargs: Mapping[str, Any], receipt_id: Optional[str] = None, **request: Any,
) -> Dict[str, Any]:
    """Launch one candidate-bound material review through the durable outbox.

    Material reviews intentionally have no generic-batch escape hatch.  Their
    retry fence, candidate, environment, and normalized scope are checked
    before the async dispatcher may accept or submit a child.
    """
    from tools.async_delegation import dispatch_async_delegation

    forbidden = {"goals", "is_batch", "dispatch_async_delegation_batch"}
    if forbidden.intersection(dispatch_kwargs):
        return {"status": "rejected", "error": "material reviews cannot use generic batch dispatch"}
    review_lens = str(request.get("review_lens") or "").strip().lower()
    effective_route_identity = str(request.get("effective_route_identity") or "").strip()
    if not review_lens or not effective_route_identity:
        return {"status": "rejected", "error": "material reviews require a review_lens and canonical effective_route_identity"}
    route_plan = request.pop("fleet_route_plan", None)
    if route_plan is not None and "runner" in dispatch_kwargs:
        # A material route receipt is meaningless if an arbitrary closure can
        # be substituted after admission.  The future private fleet ingress
        # owns construction of this runner from its persisted assignment.
        return {"status": "rejected", "error": "fleet material reviews construct their runner internally"}
    if route_plan is not None and not callable(dispatch_kwargs.get("_sealed_material_runner_factory")):
        return {"status": "rejected", "error": "fleet material reviews require the sealed owned-runner factory"}
    if route_plan is None:
        ledger.assert_current_recovery_attempt(
            attempt_id,
            fence_token=int(fence_token),
            candidate_hash=request["candidate_hash"],
            environment_fingerprint=request["environment_fingerprint"],
            normalized_scope=request["scope"],
            effective_route_identity=effective_route_identity,
            review_lens=review_lens,
        )
    bound = dict(dispatch_kwargs)
    bound["review_fence_token"] = int(fence_token)
    bound["recovery_attempt_id"] = attempt_id
    try:
        if route_plan is not None:
            receipt = ledger.admit_fleet_material_launch(
                attempt_id=attempt_id, fence_token=int(fence_token), route_plan=route_plan,
                receipt_id=receipt_id, **request,
            )
            if receipt["status"] != "admitted":
                return receipt
            result = _launch_async_review(
                ledger, dispatch=dispatch_async_delegation, dispatch_kwargs=bound,
                receipt_id=receipt["receipt_id"], already_claimed=True, **request,
            )
        else:
            result = _launch_async_review(
                ledger, dispatch=dispatch_async_delegation, dispatch_kwargs=bound,
                receipt_id=receipt_id, **request,
            )
    except Exception:
        # Before executor submission the retry stays PREPARED and is not
        # consumed; after durable acceptance async_delegation owns the fenced
        # INTERRUPTED/FAILED terminal transition.  Never overwrite either
        # state from this wrapper.
        raise
    return result


def launch_material_async_review(
    ledger: ReleaseReviewLedger, *, attempt_id: str, fence_token: int,
    dispatch_kwargs: Mapping[str, Any], receipt_id: Optional[str] = None,
    _capability: object | None = None, **request: Any,
) -> Dict[str, Any]:
    """Reject public material-launch calls that lack the sealed ingress token."""
    if _capability is not _MATERIAL_LAUNCH_CAPABILITY:
        return {"status": "rejected", "error": "material reviews require the sealed fleet material ingress"}
    return _launch_material_async_review(
        ledger, attempt_id=attempt_id, fence_token=fence_token,
        dispatch_kwargs=dispatch_kwargs, receipt_id=receipt_id, **request,
    )


def launch_fleet_owned_material_review(
    ledger: ReleaseReviewLedger,
    *,
    fleet_service: Any,
    cwd: str,
    attempt_id: str,
    fence_token: int,
    dispatch_kwargs: Mapping[str, Any],
    receipt_id: Optional[str] = None,
    **request: Any,
) -> Dict[str, Any]:
    """Private ingress for one receipt-bound owned Claude/AGY material run.

    This is intentionally not a generic ``delegate_task`` option.  It plans a
    single canonical fleet route and installs the runner factory that binds
    the route-plan receipt, async outbox, and owned PID before provider I/O.
    """
    if _MATERIAL_LAUNCH_CAPABILITY is None:  # pragma: no cover - keeps capability private to this module
        raise RuntimeError("sealed material launch capability is unavailable")
    if "runner" in dispatch_kwargs or "_sealed_material_runner_factory" in dispatch_kwargs:
        return {"status": "rejected", "error": "sealed fleet material ingress owns its runner"}
    from pathlib import Path
    from tools.fleet_delegation import OwnedMaterialRunner, plan_material_routes

    resolved_cwd = Path(cwd).resolve()
    prompt = str(request.get("prompt") or "")
    review_lens = str(request.get("review_lens") or "").strip().lower()
    candidate_hash = str(request.get("candidate_hash") or "").strip().lower()
    if not prompt or not review_lens or not candidate_hash or not resolved_cwd.is_dir():
        return {"status": "rejected", "error": "sealed fleet material ingress is missing candidate, lens, prompt, or cwd"}
    plan = plan_material_routes(
        fleet_service, task_count=1, cwd=resolved_cwd,
        prompt_fingerprint=f"sha256:{__import__('hashlib').sha256(prompt.encode('utf-8')).hexdigest()}",
        preferred_lane_ids=(str(request.get("lane") or "").strip(),),
        exclude_effective_identities=(),
    )
    if not plan.assignments:
        return {"status": "rejected", "error": "no qualified distinct fleet material route", "unavailable": [item.public_receipt() for item in plan.unavailable]}
    route = plan.assignments[0]
    if route.effective_execution_identity != str(request.get("effective_route_identity") or ""):
        return {"status": "rejected", "error": "planned fleet route identity does not match sealed receipt identity"}
    fleet_route_plan = {
        "requested": 1,
        "degraded_route_capacity": plan.degraded_route_capacity,
        "selected": {**route.public_receipt(), "review_lens": review_lens},
        "unavailable": [item.public_receipt() for item in plan.unavailable],
    }
    controller = OwnedMaterialRunner(
        service=fleet_service, route=route, cwd=resolved_cwd, prompt=prompt,
        candidate_hash=candidate_hash, review_lens=review_lens, ledger_path=ledger.path,
        attempt_id=attempt_id, fence_token=int(fence_token),
    )
    bound = dict(dispatch_kwargs)
    bound["interrupt_fn"] = controller.interrupt
    bound["_sealed_material_runner_factory"] = controller.factory
    request = dict(request)
    request["fleet_route_plan"] = fleet_route_plan
    return launch_material_async_review(
        ledger, attempt_id=attempt_id, fence_token=fence_token, dispatch_kwargs=bound,
        receipt_id=receipt_id, _capability=_MATERIAL_LAUNCH_CAPABILITY, **request,
    )
