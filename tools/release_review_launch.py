"""Single, fail-closed launch adapter for direct and async release reviewers."""
from __future__ import annotations

import os
import subprocess
import threading
import uuid
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

from hermes_constants import get_hermes_home
from tools.release_review_ledger import ReleaseReviewLedger


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
    dispatch_kwargs: Mapping[str, Any], receipt_id: Optional[str] = None, **request: Any,
) -> Dict[str, Any]:
    """Claim before forwarding to the existing async-delegation dispatcher."""
    if ledger.path.resolve() != (get_hermes_home() / "release-review-ledger.db").resolve():
        return {"status": "rejected", "error": "async reviews require the canonical Hermes ledger path"}
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
    if dispatch_kwargs.get("recovery_attempt_id"):
        return {"status": "rejected", "error": "material reviews require the receipt-bound material adapter"}
    return _launch_async_review(
        ledger, dispatch=dispatch, dispatch_kwargs=dispatch_kwargs, receipt_id=receipt_id, **request,
    )


def launch_material_async_review(
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
    ledger.assert_current_recovery_attempt(
        attempt_id,
        fence_token=int(fence_token),
        candidate_hash=request["candidate_hash"],
        environment_fingerprint=request["environment_fingerprint"],
        normalized_scope=request["scope"],
    )
    bound = dict(dispatch_kwargs)
    bound["review_fence_token"] = int(fence_token)
    bound["recovery_attempt_id"] = attempt_id
    try:
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
