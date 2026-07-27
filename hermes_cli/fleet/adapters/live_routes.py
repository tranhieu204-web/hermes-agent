"""Concrete subscription-only execution adapters for current fleet lanes."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

from .base import safe_child_environment, validate_execution
from .external_cli import ExternalCliAdapter
from .native_provider import NativeProviderAdapter
from ..types import AdapterKind, AdapterRequest, AdapterResult, Qualification, ReasonCode
from hermes_cli.fleet.usage_refresh import no_console_creationflags


_NATIVE_CHILD = Path(__file__).resolve().parents[1] / "native_child.py"
_NATIVE_SUCCESS_FIELDS = frozenset(
    {
        "ok",
        "provider_id",
        "model_id",
        "effort",
        "auth_kind",
        "auth_source",
        "fallback_enabled",
        "fast_mode",
        "output",
    }
)
_MAX_STDERR_CHARS = 4096
_AGY_MODEL_LABELS = {
    "gemini-3.1-pro-high": "Gemini 3.1 Pro (High)",
    "gemini-3.1-pro-low": "Gemini 3.1 Pro (Low)",
    "gemini-3.6-flash-high": "Gemini 3.6 Flash (High)",
    "gemini-3.6-flash-medium": "Gemini 3.6 Flash (Medium)",
    "gemini-3.6-flash-low": "Gemini 3.6 Flash (Low)",
    "gemini-3.5-flash-high": "Gemini 3.5 Flash (High)",
    "gemini-3.5-flash-medium": "Gemini 3.5 Flash (Medium)",
    "gemini-3.5-flash-low": "Gemini 3.5 Flash (Low)",
}
_AGY_MODEL_IDS_BY_LABEL = {
    label: model_id for model_id, label in _AGY_MODEL_LABELS.items()
}
if len(_AGY_MODEL_IDS_BY_LABEL) != len(_AGY_MODEL_LABELS):
    raise RuntimeError("AGY model display labels must be unique")
_AGY_RECEIPT_RE = re.compile(
    r'Propagating selected model override to backend: label="([^"\r\n]+)"\s*$'
)
_AGY_SUBSCRIPTION_ENDPOINT = (
    "daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent"
)


def _agy_log_path(run_id: str) -> Path:
    return (
        get_hermes_home()
        / "fleet"
        / "evidence"
        / "agy"
        / f"{run_id}.log"
    )


def _inspect_agy_receipt(
    log_path: Path,
    *,
    canonical_model_id: str,
    expected_display_label: str,
) -> dict[str, object]:
    check: dict[str, object] = {
        "canonical_model_id": canonical_model_id,
        "expected_display_label": expected_display_label,
    }
    try:
        raw = log_path.read_bytes()
    except FileNotFoundError:
        check["status"] = "missing_log"
        return check
    except OSError:
        check["status"] = "unreadable_log"
        return check
    check.update(
        {
            "log_bytes": len(raw),
            "log_sha256": hashlib.sha256(raw).hexdigest(),
        }
    )
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        check["status"] = "malformed_log"
        return check

    labels: list[str] = []
    marker_seen = False
    for line in text.splitlines():
        if "Propagating selected model override to backend:" not in line:
            continue
        marker_seen = True
        match = _AGY_RECEIPT_RE.search(line)
        if match is None:
            check["status"] = "malformed_receipt"
            return check
        labels.append(match.group(1))
    if not labels:
        check["status"] = "malformed_receipt" if marker_seen else "missing_receipt"
        return check

    distinct_labels = set(labels)
    if len(distinct_labels) != 1:
        check["status"] = "ambiguous_served_model"
        return check

    served_model_label = labels[0]
    served_model_id = _AGY_MODEL_IDS_BY_LABEL.get(served_model_label)
    check["receipt_count"] = len(labels)
    if served_model_id is None:
        check["status"] = "unsupported_served_model"
        return check
    check.update(
        {
            "served_model_id": served_model_id,
            "served_model_label": served_model_label,
        }
    )
    if served_model_id != canonical_model_id:
        check["status"] = "served_model_mismatch"
    elif served_model_label != expected_display_label:
        check["status"] = "expected_label_mismatch"
    else:
        check["status"] = "matched"
    return check


def _apply_agy_subscription_route_markers(
    text: str, check: dict[str, object]
) -> dict[str, object]:
    """Fail-closed consumer subscription markers shared by doctor and parents."""

    if "authMethod=consumer" not in text:
        check["status"] = "subscription_auth_missing"
        return check
    if _AGY_SUBSCRIPTION_ENDPOINT not in text:
        check["status"] = "subscription_endpoint_missing"
        return check
    if "GOOGLE_API_KEY" in text or "GEMINI_API_KEY" in text:
        check["status"] = "api_key_route_present"
        return check
    served_model_id = check.get("served_model_id")
    served_model_label = check.get("served_model_label")
    if (
        not isinstance(served_model_id, str)
        or not isinstance(served_model_label, str)
        or _AGY_MODEL_IDS_BY_LABEL.get(served_model_label) != served_model_id
        or served_model_id != check.get("canonical_model_id")
    ):
        check["status"] = "served_model_identity_invalid"
        return check
    check.update(
        {
            "auth_method": "consumer",
            "endpoint_kind": "antigravity_cloud_code",
            "fallback_enabled": False,
        }
    )
    return check


def inspect_agy_subscription_receipt(
    log_path: Path,
    *,
    canonical_model_id: str,
    expected_display_label: str,
) -> dict[str, object]:
    """Canonical label + consumer-subscription route receipt (secret-free)."""

    check = _inspect_agy_receipt(
        log_path,
        canonical_model_id=canonical_model_id,
        expected_display_label=expected_display_label,
    )
    if check.get("status") != "matched":
        return check
    try:
        text = log_path.read_text(encoding="utf-8")
    except OSError:
        check["status"] = "unreadable_log"
        return check
    return _apply_agy_subscription_route_markers(text, check)


def _finalize_agy_log(
    log_path: Path,
    receipt_check: Mapping[str, object],
    *,
    retain_evidence: bool,
) -> str:
    """Delete the raw log, optionally retaining a secret-free JSON receipt."""

    if not retain_evidence:
        try:
            log_path.unlink(missing_ok=True)
            return "removed"
        except OSError:
            return "not_persisted"

    try:
        log_path.unlink(missing_ok=True)
    except OSError:
        return "not_persisted"

    receipt_path = log_path.with_suffix(".json")
    temporary_path = receipt_path.with_name(
        f".{receipt_path.name}.{uuid.uuid4().hex}.tmp"
    )
    try:
        temporary_path.write_text(
            json.dumps(
                {
                    "schema_version": "1",
                    "receipt_check": dict(receipt_check),
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        temporary_path.replace(receipt_path)
        return f"agy:{receipt_path.name}"
    except OSError:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        return "not_persisted"


def inspect_claude_cli_payload(
    payload: object, *, canonical_model_id: str
) -> dict[str, object]:
    """Payload-fact receipt for one `claude -p --output-format json` run.

    Only facts the payload itself proves: success envelope, non-empty result,
    the requested model present in ``modelUsage``, and a session identity.
    Subscription-only proof (no API-key env, live OAuth record) is the
    caller's evidence, not the payload's, and is recorded by the caller.
    """

    check: dict[str, object] = {"canonical_model_id": canonical_model_id}
    if not isinstance(payload, dict):
        check["status"] = "malformed_output"
        return check
    if payload.get("is_error") is not False or payload.get("subtype") != "success":
        check["status"] = "cli_reported_error"
        check["cli_subtype"] = str(payload.get("subtype") or "")
        return check
    result = payload.get("result")
    if not isinstance(result, str) or not result.strip():
        check["status"] = "empty_output"
        return check
    usage = payload.get("modelUsage")
    if not isinstance(usage, dict) or not usage:
        check["status"] = "missing_receipt"
        return check
    if canonical_model_id not in usage:
        check["status"] = "served_model_mismatch"
        check["served_model_ids"] = sorted(str(key) for key in usage)
        return check
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        check["status"] = "missing_receipt"
        return check
    check.update(
        {
            "status": "matched",
            "served_model_id": canonical_model_id,
            "session_id": session_id,
            "num_turns": payload.get("num_turns"),
            "fallback_enabled": False,
        }
    )
    return check


def _stderr_receipt(stderr: str | bytes | None) -> str:
    """Return bounded diagnostics without retaining or echoing stderr text."""

    if isinstance(stderr, bytes):
        raw = stderr[:_MAX_STDERR_CHARS]
    else:
        raw = str(stderr or "")[:_MAX_STDERR_CHARS].encode(
            "utf-8", errors="replace"
        )
    return f"bytes={len(raw)} sha256={hashlib.sha256(raw).hexdigest()}"


def run_native_hermes_child(**request: Any) -> Mapping[str, Any]:
    """Run the exact native route in a killable, isolated Python child."""

    child_request = {
        "provider_id": str(request["provider_id"]),
        "model": str(request["model"]),
        "effort": str(request["effort"]),
        "prompt": str(request["prompt"]),
    }
    process = subprocess.Popen(
        [sys.executable, str(_NATIVE_CHILD)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=Path(request["cwd"]),
        env=safe_child_environment(),
        shell=False,
        creationflags=no_console_creationflags(),
    )
    try:
        stdout, stderr = process.communicate(
            json.dumps(child_request, separators=(",", ":"), sort_keys=True),
            timeout=request["timeout_seconds"],
        )
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.communicate()
        raise TimeoutError("native child execution timed out") from exc
    stderr_receipt = _stderr_receipt(stderr)
    if process.returncode != 0:
        raise RuntimeError(
            f"native child exited {process.returncode}; stderr {stderr_receipt}"
        )
    if not isinstance(stdout, str) or stdout != stdout.strip():
        raise ValueError("native child stdout was not a strict JSON envelope")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("native child stdout was not JSON") from exc
    if not isinstance(payload, dict) or set(payload) != _NATIVE_SUCCESS_FIELDS:
        raise ValueError("native child stdout envelope fields were invalid")
    return payload


class _SubscriptionCliAdapter(ExternalCliAdapter):
    def __init__(
        self,
        executable: str,
        *,
        lane: str,
        run_process: Callable[..., Any] = subprocess.run,
    ) -> None:
        super().__init__(executable)
        self.lane = lane
        self._run_process = run_process

    def _argv(self, executable: Path, request: AdapterRequest) -> list[str]:
        return [
            str(executable),
            "-p",
            "--model",
            request.model,
            "--effort",
            request.effort,
            "--output-format",
            "json",
        ]

    @staticmethod
    def _agy_argv(
        executable: Path,
        request: AdapterRequest,
        *,
        display_label: str,
        log_path: Path,
    ) -> list[str]:
        return [
            str(executable),
            "-p",
            request.prompt,
            "--model",
            display_label,
            "--log-file",
            str(log_path),
            "--print-timeout",
            f"{request.timeout_seconds}s",
        ]

    @staticmethod
    def _agy_failure(
        request: AdapterRequest,
        qualification: Qualification,
        reason: ReasonCode,
        receipt_check: Mapping[str, object],
    ) -> AdapterResult:
        return AdapterResult(
            ok=False,
            reason=reason,
            provider_id=request.profile.provider_id,
            model_id=request.model,
            auth_kind=qualification.auth_kind or "unknown",
            adapter_kind=AdapterKind.EXTERNAL_CLI,
            metadata={"receipt_check": dict(receipt_check)},
        )

    def _finish_agy_failure(
        self,
        request: AdapterRequest,
        qualification: Qualification,
        reason: ReasonCode,
        *,
        run_id: str,
        log_path: Path,
        receipt_check: dict[str, object],
    ) -> AdapterResult:
        receipt_check["evidence_id"] = _finalize_agy_log(
            log_path, receipt_check, retain_evidence=True
        )
        return self._agy_failure(
            request, qualification, reason, receipt_check
        )

    def _execute_agy(
        self,
        executable: Path,
        request: AdapterRequest,
        qualification: Qualification,
    ) -> AdapterResult:
        display_label = _AGY_MODEL_LABELS.get(request.model)
        if display_label is None:
            return self._agy_failure(
                request,
                qualification,
                ReasonCode.MODEL_MISMATCH,
                {
                    "canonical_model_id": request.model,
                    "status": "unsupported_model",
                },
            )

        run_id = uuid.uuid4().hex
        log_path = _agy_log_path(run_id)
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            return self._agy_failure(
                request,
                qualification,
                ReasonCode.EXECUTION_FAILED,
                {
                    "canonical_model_id": request.model,
                    "expected_display_label": display_label,
                    "status": "log_path_unavailable",
                },
            )

        try:
            completed = self._run_process(
                self._agy_argv(
                    executable,
                    request,
                    display_label=display_label,
                    log_path=log_path,
                ),
                capture_output=True,
                text=True,
                cwd=request.cwd,
                env=safe_child_environment(),
                timeout=request.timeout_seconds,
                shell=False,
                creationflags=no_console_creationflags(),
                check=False,
            )
        except subprocess.TimeoutExpired:
            receipt_check = _inspect_agy_receipt(
                log_path,
                canonical_model_id=request.model,
                expected_display_label=display_label,
            )
            receipt_check["receipt_status"] = receipt_check["status"]
            receipt_check["status"] = "process_timeout"
            return self._finish_agy_failure(
                request,
                qualification,
                ReasonCode.EXECUTION_TIMEOUT,
                run_id=run_id,
                log_path=log_path,
                receipt_check=receipt_check,
            )
        except OSError:
            receipt_check = _inspect_agy_receipt(
                log_path,
                canonical_model_id=request.model,
                expected_display_label=display_label,
            )
            receipt_check["receipt_status"] = receipt_check["status"]
            receipt_check["status"] = "process_launch_failed"
            return self._finish_agy_failure(
                request,
                qualification,
                ReasonCode.EXECUTION_FAILED,
                run_id=run_id,
                log_path=log_path,
                receipt_check=receipt_check,
            )

        receipt_check = inspect_agy_subscription_receipt(
            log_path,
            canonical_model_id=request.model,
            expected_display_label=display_label,
        )
        if completed.returncode != 0:
            receipt_check["receipt_status"] = receipt_check["status"]
            receipt_check["status"] = "process_exit_nonzero"
            receipt_check["process_returncode"] = completed.returncode
            return self._finish_agy_failure(
                request,
                qualification,
                ReasonCode.EXECUTION_FAILED,
                run_id=run_id,
                log_path=log_path,
                receipt_check=receipt_check,
            )
        if receipt_check["status"] != "matched":
            reason = (
                ReasonCode.MODEL_MISMATCH
                if receipt_check["status"]
                in {
                    "display_label_mismatch",
                    "expected_label_mismatch",
                    "served_model_mismatch",
                    "unsupported_served_model",
                }
                else ReasonCode.MALFORMED_OUTPUT
            )
            return self._finish_agy_failure(
                request,
                qualification,
                reason,
                run_id=run_id,
                log_path=log_path,
                receipt_check=receipt_check,
            )
        if not isinstance(completed.stdout, str) or not completed.stdout.strip():
            receipt_check["receipt_status"] = receipt_check["status"]
            receipt_check["status"] = "malformed_output"
            return self._finish_agy_failure(
                request,
                qualification,
                ReasonCode.MALFORMED_OUTPUT,
                run_id=run_id,
                log_path=log_path,
                receipt_check=receipt_check,
            )

        receipt_check["log_cleanup"] = _finalize_agy_log(
            log_path, receipt_check, retain_evidence=False
        )
        if receipt_check["log_cleanup"] == "not_persisted":
            receipt_check["receipt_status"] = receipt_check["status"]
            receipt_check["status"] = "log_cleanup_failed"
            return self._finish_agy_failure(
                request,
                qualification,
                ReasonCode.EXECUTION_FAILED,
                run_id=run_id,
                log_path=log_path,
                receipt_check=receipt_check,
            )
        return AdapterResult(
            ok=True,
            reason=ReasonCode.MET,
            provider_id=request.profile.provider_id,
            model_id=request.model,
            auth_kind=qualification.auth_kind or "unknown",
            adapter_kind=AdapterKind.EXTERNAL_CLI,
            output=completed.stdout,
            metadata={
                "receipt_check": receipt_check,
                "route_proof": {
                    "executable": str(executable),
                    "version": qualification.version,
                    "requested_model_id": request.model,
                    "served_model_id": receipt_check["served_model_id"],
                    "served_model_label": receipt_check["served_model_label"],
                    "effort": request.effort,
                    "auth_kind": qualification.auth_kind,
                    "fast_mode": False,
                    "fallback_enabled": False,
                    "model_qualification": "agy live backend receipt",
                },
            },
        )

    def execute(
        self, request: AdapterRequest, qualification: Qualification
    ) -> AdapterResult:
        failure = validate_execution(request, qualification)
        if failure is not None:
            return self._failure(request, qualification, failure)
        executable = self._resolved_executable()
        if (
            executable is None
            or qualification.executable is None
            or executable != Path(qualification.executable).resolve()
        ):
            return self._failure(request, qualification, ReasonCode.QUALIFICATION_FAILED)
        if self.lane == "antigravity":
            return self._execute_agy(executable, request, qualification)
        try:
            completed = self._run_process(
                self._argv(executable, request),
                input=request.prompt,
                capture_output=True,
                text=True,
                cwd=request.cwd,
                env=safe_child_environment(),
                timeout=request.timeout_seconds,
                shell=False,
                check=False,
                creationflags=no_console_creationflags(),
            )
        except subprocess.TimeoutExpired:
            return self._failure(request, qualification, ReasonCode.EXECUTION_TIMEOUT)
        except OSError:
            return self._failure(request, qualification, ReasonCode.EXECUTION_FAILED)
        if completed.returncode != 0:
            return self._failure(request, qualification, ReasonCode.EXECUTION_FAILED)
        output = completed.stdout
        metadata: dict[str, object] = {
            "route_proof": {
                "executable": str(executable),
                "version": qualification.version,
                "requested_model_id": request.model,
                "effort": request.effort,
                "auth_kind": qualification.auth_kind,
                "fast_mode": False,
                "fallback_enabled": False,
            }
        }
        try:
            payload = json.loads(output)
        except (TypeError, json.JSONDecodeError):
            return self._failure(request, qualification, ReasonCode.MALFORMED_OUTPUT)
        if self.lane == "claude_code":
            check = inspect_claude_cli_payload(
                payload, canonical_model_id=request.model
            )
            if check.get("status") != "matched":
                reason = (
                    ReasonCode.MODEL_MISMATCH
                    if check.get("status") == "served_model_mismatch"
                    else ReasonCode.MALFORMED_OUTPUT
                )
                metadata["cli_receipt"] = check
                return AdapterResult(
                    ok=False,
                    reason=reason,
                    provider_id=request.profile.provider_id,
                    model_id=request.model,
                    auth_kind=qualification.auth_kind or "unknown",
                    adapter_kind=AdapterKind.EXTERNAL_CLI,
                    metadata=metadata,
                )
            output = payload["result"]
            metadata["cli_receipt"] = check
            return AdapterResult(
                ok=True,
                reason=ReasonCode.MET,
                provider_id=request.profile.provider_id,
                model_id=request.model,
                auth_kind=qualification.auth_kind or "unknown",
                adapter_kind=AdapterKind.EXTERNAL_CLI,
                output=output,
                metadata=metadata,
            )
        if not isinstance(payload, dict) or not isinstance(payload.get("result"), str):
            return self._failure(request, qualification, ReasonCode.MALFORMED_OUTPUT)
        usage = payload.get("modelUsage")
        if isinstance(usage, dict) and usage and request.model not in usage:
            return self._failure(request, qualification, ReasonCode.MODEL_MISMATCH)
        output = payload["result"]
        metadata["cli_receipt"] = {
            key: payload[key]
            for key in ("session_id", "is_error", "num_turns")
            if key in payload
        }
        return AdapterResult(
            ok=True,
            reason=ReasonCode.MET,
            provider_id=request.profile.provider_id,
            model_id=request.model,
            auth_kind=qualification.auth_kind or "unknown",
            adapter_kind=AdapterKind.EXTERNAL_CLI,
            output=output,
            metadata=metadata,
        )


class ClaudeCodeAdapter(_SubscriptionCliAdapter):
    def __init__(self, executable: str = "claude", **kwargs: Any) -> None:
        super().__init__(executable, lane="claude_code", **kwargs)


class AntigravityAdapter(_SubscriptionCliAdapter):
    def __init__(self, executable: str = "agy", **kwargs: Any) -> None:
        super().__init__(executable, lane="antigravity", **kwargs)


def live_adapters(
    *,
    native_runner: Callable[..., Mapping[str, Any]] = run_native_hermes_child,
    qualifications: Mapping[str, Qualification] | None = None,
) -> dict[str, object]:
    qualifications = qualifications or {}
    antigravity = qualifications.get("antigravity")
    claude_code_qual = qualifications.get("claude_code")
    return {
        "chatgpt_codex": NativeProviderAdapter(native_runner),
        "claude_code": ClaudeCodeAdapter(
            claude_code_qual.executable
            if claude_code_qual and claude_code_qual.qualified and claude_code_qual.executable
            else "claude"
        ),
        "grok": NativeProviderAdapter(native_runner),
        "antigravity": AntigravityAdapter(
            antigravity.executable
            if antigravity and antigravity.qualified and antigravity.executable
            else "agy"
        ),
    }
