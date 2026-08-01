"""Concrete subscription-only execution adapters for current fleet lanes."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
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


@dataclass
class OwnedExternalMaterialRun:
    """In-memory handle for a material child whose identity is persisted elsewhere.

    The object deliberately contains the executable command, prompt and raw
    stdio only in process memory.  Callers persist just their separately
    minted opaque handle, PID and host start identity before calling
    :meth:`finish`.
    """

    adapter: "_SubscriptionCliAdapter"
    process: Any
    request: AdapterRequest
    qualification: Qualification
    executable: Path
    stdin_payload: str | None
    agy_run_id: str | None = None
    agy_log_path: Path | None = None
    agy_display_label: str | None = None

    def finish(self, *, timeout_seconds: int | float | None = None) -> AdapterResult:
        return self.adapter._finish_owned_material_run(self, timeout_seconds=timeout_seconds)


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
        check["status"] = "ambiguous_propagated_model"
        return check

    # NOTE ON PROVENANCE.  `Propagating selected model override to backend` is
    # written by the Antigravity CLIENT before the request leaves the host.  It
    # proves which model was REQUESTED and SELECTED and propagated; it is NOT a
    # provider-returned served identity and proves nothing about which model
    # actually served the request.  Nothing here may therefore be NAMED as a
    # served identity: the only permitted uses of "served" are the two explicit
    # declarations below that state the served identity is NOT proven.
    propagated_model_label = labels[0]
    propagated_model_id = _AGY_MODEL_IDS_BY_LABEL.get(propagated_model_label)
    check["receipt_count"] = len(labels)
    if propagated_model_id is None:
        check["status"] = "unsupported_propagated_model"
        return check
    check.update(
        {
            "requested_selected_model_id": propagated_model_id,
            "requested_selected_model_label": propagated_model_label,
            "model_evidence_kind": "requested_selected_propagation",
            "served_model_proven": False,
            "served_model_evidence": "NOT_PROVEN",
        }
    )
    if propagated_model_id != canonical_model_id:
        check["status"] = "propagated_model_mismatch"
    elif propagated_model_label != expected_display_label:
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
    propagated_model_id = check.get("requested_selected_model_id")
    propagated_model_label = check.get("requested_selected_model_label")
    if (
        not isinstance(propagated_model_id, str)
        or not isinstance(propagated_model_label, str)
        or _AGY_MODEL_IDS_BY_LABEL.get(propagated_model_label) != propagated_model_id
        or propagated_model_id != check.get("canonical_model_id")
    ):
        check["status"] = "propagated_model_identity_invalid"
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


def _agy_route_proof(
    *,
    executable: Path | str,
    request: AdapterRequest,
    qualification: Qualification,
    receipt_check: Mapping[str, object],
) -> dict[str, object]:
    """Published Antigravity route proof with truthful model-evidence class.

    The AGY receipt is built from the client's own
    ``Propagating selected model override to backend`` line, which is written
    before the request leaves the host.  It proves the model that was
    *requested and selected*, never the model the provider actually served.
    The proof therefore publishes ``requested_selected_model_*`` plus an
    explicit ``NOT_PROVEN`` served-evidence class.  The subscription route
    itself stays proven and unpaid — only the served-identity claim is
    withdrawn.

    No compatibility alias is emitted.  A key named ``served_model_id`` would
    assert a provider-returned identity that this lane cannot observe, so it is
    removed outright on every rail rather than retained beside the disclaimer:
    a consumer reading only the key name would still be misled.
    """

    proof: dict[str, object] = {
        "executable": str(executable),
        "version": qualification.version,
        "requested_model_id": request.model,
        "requested_selected_model_id": receipt_check.get("requested_selected_model_id"),
        "requested_selected_model_label": receipt_check.get(
            "requested_selected_model_label"
        ),
        "model_evidence_kind": "requested_selected_propagation",
        "served_model_proven": False,
        "served_model_evidence": "NOT_PROVEN",
        "effort": request.effort,
        "auth_kind": qualification.auth_kind,
        "fast_mode": False,
        "fallback_enabled": False,
        # NOT `model_qualification`.  A client-side propagation line cannot
        # qualify the model that actually served the request, so this names the
        # evidence ARTIFACT rather than asserting any qualification or proof.
        "model_evidence_source": "agy client model-override propagation log line",
    }
    return proof


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
            # `modelUsage` is part of the provider's own RESPONSE envelope, so
            # unlike the Antigravity propagation line this genuinely binds the
            # served model identity.
            "served_model_id": canonical_model_id,
            "model_evidence_kind": "served_response_envelope",
            "served_model_proven": True,
            "served_model_evidence": "PROVEN",
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
        popen: Callable[..., Any] = subprocess.Popen,
    ) -> None:
        super().__init__(executable)
        self.lane = lane
        self._run_process = run_process
        self._popen = popen

    def start_owned_material(
        self, request: AdapterRequest, qualification: Qualification,
        *, containment: Any = None,
    ) -> OwnedExternalMaterialRun:
        """Start an argv-only external material child without executing it yet.

        This restricted seam is intentionally unavailable to generic fleet
        work.  The material dispatcher binds the returned PID/start identity
        on both durable rails before it invokes ``finish``.

        ``containment`` carries the caller's kernel-enforced owner-death
        guarantee.  When supplied it arms the guarantee at creation time and
        the child is only released to run once the kernel is holding it, so a
        sudden owner death cannot leave a prompt-bearing process alive.  The
        caller is responsible for refusing to spawn when no containment is
        available; this seam does not silently downgrade.
        """
        failure = validate_execution(request, qualification)
        if failure is not None:
            raise RuntimeError(f"external material route is not qualified: {failure.value}")
        executable = self._resolved_executable()
        if (
            executable is None
            or qualification.executable is None
            or executable != Path(qualification.executable).resolve()
        ):
            raise RuntimeError("external material executable no longer matches qualification")
        if self.lane == "antigravity":
            display_label = _AGY_MODEL_LABELS.get(request.model)
            if display_label is None:
                raise RuntimeError("external material model is unsupported by Antigravity")
            run_id = uuid.uuid4().hex
            log_path = _agy_log_path(run_id)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            argv = self._agy_argv(
                executable, request, display_label=display_label, log_path=log_path,
            )
            stdin_payload: str | None = None
        elif self.lane == "claude_code":
            run_id = None
            log_path = None
            display_label = None
            argv = self._argv(executable, request)
            stdin_payload = request.prompt
        else:
            raise RuntimeError("only owned Claude Code and Antigravity material routes are supported")
        spawn_kwargs: dict[str, Any] = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "cwd": request.cwd,
            "env": safe_child_environment(),
            "shell": False,
            "creationflags": no_console_creationflags(),
        }
        if containment is not None:
            extra = containment.popen_kwargs()
            # Creation flags must be OR-ed, never replaced: dropping the
            # no-console flag would surface a window, and dropping the
            # containment flag would let the child run unconstrained.
            if "creationflags" in extra:
                spawn_kwargs["creationflags"] |= int(extra.pop("creationflags"))
            spawn_kwargs.update(extra)
        process = self._popen(argv, **spawn_kwargs)
        if containment is not None and not containment.adopt(process):
            # The child exists but the kernel guarantee is NOT in force.  On
            # Windows it is still suspended and has executed nothing.  Kill
            # exactly it and fail closed rather than run a prompt-bearing
            # process that could outlive a sudden owner death.
            for method in ("kill", "terminate"):
                call = getattr(process, method, None)
                if callable(call):
                    try:
                        call()
                        break
                    except Exception:
                        continue
            raise RuntimeError(
                "owner-death containment could not be established for the "
                "external material child"
            )
        return OwnedExternalMaterialRun(
            adapter=self,
            process=process,
            request=request,
            qualification=qualification,
            executable=executable,
            stdin_payload=stdin_payload,
            agy_run_id=run_id,
            agy_log_path=log_path,
            agy_display_label=display_label,
        )

    def _finish_owned_material_run(
        self, run: OwnedExternalMaterialRun, *, timeout_seconds: int | float | None,
    ) -> AdapterResult:
        timeout = run.request.timeout_seconds if timeout_seconds is None else timeout_seconds
        try:
            stdout, stderr = run.process.communicate(run.stdin_payload, timeout=timeout)
        except subprocess.TimeoutExpired:
            run.process.kill()
            run.process.communicate()
            if self.lane == "antigravity" and run.agy_log_path is not None and run.agy_display_label is not None and run.agy_run_id is not None:
                receipt_check = _inspect_agy_receipt(
                    run.agy_log_path, canonical_model_id=run.request.model,
                    expected_display_label=run.agy_display_label,
                )
                receipt_check["receipt_status"] = receipt_check["status"]
                receipt_check["status"] = "process_timeout"
                return self._finish_agy_failure(
                    run.request, run.qualification, ReasonCode.EXECUTION_TIMEOUT,
                    run_id=run.agy_run_id, log_path=run.agy_log_path, receipt_check=receipt_check,
                )
            return self._failure(run.request, run.qualification, ReasonCode.EXECUTION_TIMEOUT)
        except OSError:
            return self._failure(run.request, run.qualification, ReasonCode.EXECUTION_FAILED)

        if self.lane == "antigravity":
            return self._finish_owned_agy(run, stdout=stdout, stderr=stderr)
        return self._finish_owned_claude(run, stdout=stdout)

    def _finish_owned_claude(self, run: OwnedExternalMaterialRun, *, stdout: Any) -> AdapterResult:
        if run.process.returncode != 0:
            return self._failure(run.request, run.qualification, ReasonCode.EXECUTION_FAILED)
        try:
            payload = json.loads(stdout)
        except (TypeError, json.JSONDecodeError):
            return self._failure(run.request, run.qualification, ReasonCode.MALFORMED_OUTPUT)
        check = inspect_claude_cli_payload(payload, canonical_model_id=run.request.model)
        if check.get("status") != "matched":
            reason = ReasonCode.MODEL_MISMATCH if check.get("status") == "served_model_mismatch" else ReasonCode.MALFORMED_OUTPUT
            return AdapterResult(
                ok=False, reason=reason, provider_id=run.request.profile.provider_id,
                model_id=run.request.model, auth_kind=run.qualification.auth_kind or "unknown",
                adapter_kind=AdapterKind.EXTERNAL_CLI, metadata={"cli_receipt": check},
            )
        return AdapterResult(
            ok=True, reason=ReasonCode.MET, provider_id=run.request.profile.provider_id,
            model_id=run.request.model, auth_kind=run.qualification.auth_kind or "unknown",
            adapter_kind=AdapterKind.EXTERNAL_CLI, output=payload["result"],
            metadata={
                "cli_receipt": check,
                "route_proof": {
                    "executable": str(run.executable), "version": run.qualification.version,
                    "requested_model_id": run.request.model, "effort": run.request.effort,
                    "auth_kind": run.qualification.auth_kind, "fast_mode": False,
                    "fallback_enabled": False,
                    # Unlike Antigravity's client-side propagation line, the
                    # Claude `--output-format json` envelope is a provider
                    # response artifact, so served identity really is proven.
                    "model_evidence_kind": "served_response_envelope",
                    "served_model_proven": True,
                    "served_model_evidence": "PROVEN",
                },
            },
        )

    def _finish_owned_agy(self, run: OwnedExternalMaterialRun, *, stdout: Any, stderr: Any) -> AdapterResult:
        if run.agy_log_path is None or run.agy_display_label is None or run.agy_run_id is None:
            return self._failure(run.request, run.qualification, ReasonCode.EXECUTION_FAILED)
        receipt_check = inspect_agy_subscription_receipt(
            run.agy_log_path, canonical_model_id=run.request.model,
            expected_display_label=run.agy_display_label,
        )
        if run.process.returncode != 0:
            receipt_check["receipt_status"] = receipt_check["status"]
            receipt_check["status"] = "process_exit_nonzero"
            return self._finish_agy_failure(
                run.request, run.qualification, ReasonCode.EXECUTION_FAILED,
                run_id=run.agy_run_id, log_path=run.agy_log_path, receipt_check=receipt_check,
            )
        if receipt_check["status"] != "matched" or not isinstance(stdout, str) or not stdout.strip():
            reason = ReasonCode.MODEL_MISMATCH if receipt_check["status"] in {
                "display_label_mismatch", "expected_label_mismatch",
                "propagated_model_mismatch", "unsupported_propagated_model",
            } else ReasonCode.MALFORMED_OUTPUT
            receipt_check["receipt_status"] = receipt_check["status"]
            receipt_check["status"] = "malformed_output" if not stdout else receipt_check["status"]
            return self._finish_agy_failure(
                run.request, run.qualification, reason,
                run_id=run.agy_run_id, log_path=run.agy_log_path, receipt_check=receipt_check,
            )
        receipt_check["log_cleanup"] = _finalize_agy_log(run.agy_log_path, receipt_check, retain_evidence=False)
        if receipt_check["log_cleanup"] == "not_persisted":
            return self._finish_agy_failure(
                run.request, run.qualification, ReasonCode.EXECUTION_FAILED,
                run_id=run.agy_run_id, log_path=run.agy_log_path, receipt_check=receipt_check,
            )
        return AdapterResult(
            ok=True, reason=ReasonCode.MET, provider_id=run.request.profile.provider_id,
            model_id=run.request.model, auth_kind=run.qualification.auth_kind or "unknown",
            adapter_kind=AdapterKind.EXTERNAL_CLI, output=stdout,
            metadata={"receipt_check": receipt_check, "route_proof": _agy_route_proof(
                executable=run.executable, request=run.request,
                qualification=run.qualification, receipt_check=receipt_check,
            )},
        )

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
                    "propagated_model_mismatch",
                    "unsupported_propagated_model",
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
                "route_proof": _agy_route_proof(
                    executable=executable,
                    request=request,
                    qualification=qualification,
                    receipt_check=receipt_check,
                ),
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
        # The evidence class is filled in per lane below, once the response
        # envelope has actually been validated.  It is never asserted up front.
        route_proof: dict[str, object] = {
            "executable": str(executable),
            "version": qualification.version,
            "requested_model_id": request.model,
            "effort": request.effort,
            "auth_kind": qualification.auth_kind,
            "fast_mode": False,
            "fallback_enabled": False,
        }
        metadata: dict[str, object] = {"route_proof": route_proof}
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
            # `modelUsage` is part of the provider's own response envelope, so
            # this lane genuinely proves the served identity.
            route_proof.update({
                "model_evidence_kind": "served_response_envelope",
                "served_model_proven": True,
                "served_model_evidence": "PROVEN",
            })
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
        # Served identity is proven here only when the response envelope
        # actually carried it; an absent `modelUsage` proves nothing and must
        # be published as NOT_PROVEN rather than assumed.
        served_proven = isinstance(usage, dict) and bool(usage) and request.model in usage
        route_proof.update({
            "model_evidence_kind": (
                "served_response_envelope" if served_proven else "no_served_evidence"
            ),
            "served_model_proven": served_proven,
            "served_model_evidence": "PROVEN" if served_proven else "NOT_PROVEN",
        })
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
