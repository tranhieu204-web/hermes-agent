"""Single, fail-closed launch adapter for direct and async release reviewers."""
from __future__ import annotations

import os
import subprocess
import threading
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

from tools.release_review_ledger import ReleaseReviewLedger


def _admit_capture_claim(ledger: ReleaseReviewLedger, receipt_id: Optional[str], **request: Any) -> Dict[str, Any]:
    preflight = request.pop("preflight")
    receipt = ledger.admit(receipt_id=receipt_id, **request)
    if receipt["status"] != "admitted":
        return receipt
    ledger.capture_preflight(receipt["receipt_id"], preflight)
    claim = ledger.claim_launch(receipt["receipt_id"])
    return {**receipt, "claim": claim}


def launch_shell_review(
    ledger: ReleaseReviewLedger, *, command: Sequence[str], receipt_id: Optional[str] = None,
    popen: Callable[..., Any] = subprocess.Popen, **request: Any,
) -> Dict[str, Any]:
    """Claim before starting a direct reviewer; commands never use ``shell=True``."""
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
    except Exception:
        if process.poll() is None:
            process.terminate()
        try:
            ledger.mark_launch_failed(receipt["receipt_id"])
        except RuntimeError:
            pass  # expiry is already the durable terminal state
        raise
    ledger.supervise_deadline(receipt["receipt_id"], lambda: process.terminate() if process.poll() is None else None)
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


def launch_async_review(
    ledger: ReleaseReviewLedger, *, dispatch: Callable[..., Dict[str, Any]],
    dispatch_kwargs: Mapping[str, Any], receipt_id: Optional[str] = None, **request: Any,
) -> Dict[str, Any]:
    """Claim before forwarding to the existing async-delegation dispatcher."""
    receipt = _admit_capture_claim(ledger, receipt_id, **request)
    if receipt["status"] != "admitted" or receipt["claim"]["status"] != "claimed":
        return receipt
    if not callable(dispatch_kwargs.get("interrupt_fn")):
        ledger.mark_launch_failed(receipt["receipt_id"], "launch_rejected")
        return {**receipt, "status": "rejected", "dispatch": {"error": "receipt-bound async reviews require interrupt_fn"}}
    try:
        dispatch_input = dict(dispatch_kwargs)
        dispatch_input["review_receipt_id"] = receipt["receipt_id"]
        dispatch_input["review_ledger_path"] = str(ledger.path)
        result = dispatch(**dispatch_input)
    except Exception:
        ledger.mark_launch_failed(receipt["receipt_id"])
        raise
    if result.get("status") != "dispatched":
        ledger.mark_launch_failed(receipt["receipt_id"], "launch_rejected")
        return {**receipt, "status": "rejected", "dispatch": result}
    # The real async rail is thread-backed in the existing Hermes process. It
    # has a root process but no invented leaf PID; a concrete wrapper can add
    # one later only by creating a new receipt, never by mutating this record.
    delegation_id = result.get("delegation_id")
    if not delegation_id:
        ledger.mark_launch_failed(receipt["receipt_id"])
        raise RuntimeError("async reviewer did not return a delegation identity")
    try:
        ledger.attach_processes(receipt["receipt_id"], os.getpid(), None, f"delegation:{delegation_id}")
    except Exception:
        from tools.async_delegation import interrupt_review_receipt
        interrupt_review_receipt(receipt["receipt_id"], "receipt expired before async attachment")
        try:
            ledger.mark_launch_failed(receipt["receipt_id"])
        except RuntimeError:
            pass
        raise
    # Async dispatcher exposes the supplied interrupt function in its record;
    # a deadline only signals that dedicated review, never unrelated work.
    def _interrupt():
        from tools.async_delegation import force_timeout_review_receipt
        force_timeout_review_receipt(receipt["receipt_id"], "release review deadline")
    ledger.supervise_deadline(receipt["receipt_id"], _interrupt)
    return {**receipt, "status": "launched", "dispatch": result, "root_pid": os.getpid(), "leaf_pid": None}
