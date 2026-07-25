"""Persistent external-CLI parent sessions for Desktop Fleet Auto."""

from __future__ import annotations

import hashlib
import re
import subprocess
import threading
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from hermes_cli.fleet.adapters.base import safe_child_environment
from hermes_cli.fleet.adapters.live_routes import (
    _AGY_MODEL_LABELS,
    _agy_log_path,
    _finalize_agy_log,
    inspect_agy_subscription_receipt,
)
from hermes_cli.fleet.inspection import build_fleet_service
from hermes_cli.fleet.state import FleetStore


_MODEL_ID = "gemini-3.1-pro-high"
_MODEL_LABEL = "Gemini 3.1 Pro (High)"

def _route_model_id(route: Mapping[str, Any] | None = None) -> str:
    model = str((route or {}).get("model") or _MODEL_ID).strip()
    if model not in _AGY_MODEL_LABELS:
        raise ValueError(f"unsupported antigravity model: {model}")
    return model


def _route_model_label(model_id: str) -> str:
    return _AGY_MODEL_LABELS[model_id]


_PROVIDER_ID = "antigravity-subscription"
_DISPLAY_LABEL = "Antigravity · Gemini 3.1 Pro High · external CLI"
_MAX_OUTPUT_CHARS = 1_000_000
_MAX_LOG_BYTES = 2_000_000
_CONVERSATION_RE = re.compile(
    r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12})\b",
    re.IGNORECASE,
)


def _stderr_receipt(stderr: object) -> str:
    raw = str(stderr or "")[:4096].encode("utf-8", errors="replace")
    return f"bytes={len(raw)} sha256={hashlib.sha256(raw).hexdigest()}"


def _parent_receipt(
    log_path: Path,
    *,
    expected_conversation_id: str | None,
    canonical_model_id: str,
    expected_display_label: str,
) -> dict[str, object]:
    check = inspect_agy_subscription_receipt(
        log_path,
        canonical_model_id=canonical_model_id,
        expected_display_label=expected_display_label,
    )
    if check.get("status") != "matched":
        return check
    try:
        raw = log_path.read_bytes()
    except OSError:
        check["status"] = "unreadable_log"
        return check
    if len(raw) > _MAX_LOG_BYTES:
        check["status"] = "log_too_large"
        return check
    text = raw.decode("utf-8", errors="strict")
    # Subscription markers already validated by the shared inspector; keep the
    # conversation continuity checks below as parent-session-specific proof.
    if expected_conversation_id:
        quoted = re.escape(expected_conversation_id)
        if not re.search(
            rf'conversationID="{quoted}"',
            text,
        ) or not re.search(
            rf"Print mode: resuming conversation {quoted}\b",
            text,
        ):
            check["status"] = "conversation_resume_mismatch"
            return check
        if "Created conversation " in text:
            check["status"] = "unexpected_new_conversation"
            return check
        conversation_id = expected_conversation_id
        continued = True
    else:
        created_ids = []
        for line in text.splitlines():
            if "Created conversation " not in line:
                continue
            match = _CONVERSATION_RE.search(line)
            if match:
                created_ids.append(match.group(1))
        if len(created_ids) != 1 or "Starting new conversation" not in text:
            check["status"] = "new_conversation_receipt_missing"
            return check
        conversation_id = created_ids[0]
        continued = False

    if f"Print mode: conversation={conversation_id}," not in text:
        check["status"] = "conversation_send_mismatch"
        return check
    check.update(
        {
            "status": "matched",
            "auth_kind": "cli_subscription",
            "auth_method": "consumer",
            "endpoint_kind": "antigravity_cloud_code",
            "conversation_id": conversation_id,
            "continued": continued,
            "fallback_enabled": False,
        }
    )
    return check


class ExternalParentSessionDriver:
    """A non-AIAgent driver that binds agy conversation identity to lineage."""

    external_subscription = True

    def __init__(
        self,
        *,
        executable: str,
        route: Mapping[str, Any],
        cwd: Path,
        session_id: str,
        session_db,
        store: FleetStore,
        timeout_seconds: int,
        process_factory: Callable[..., Any] = subprocess.Popen,
    ) -> None:
        if (
            route.get("model_source") != "fleet_auto"
            or route.get("fleet_adapter_kind") != "external_cli"
            or route.get("fleet_lane_id") != "antigravity"
            or route.get("fleet_route_purpose") != "desktop_parent"
            or route.get("provider") != _PROVIDER_ID
            or str(route.get("model") or "") not in _AGY_MODEL_LABELS
            or any(
                not str(route.get(key) or "").strip()
                for key in (
                    "fleet_profile_id",
                    "fleet_lineage_root_id",
                    "fleet_route_identity",
                )
            )
        ):
            raise ValueError("unsupported external parent route")
        self.executable = str(Path(executable).resolve())
        self.route = dict(route)
        self.cwd = Path(cwd)
        self.session_id = session_id
        self._session_db = session_db
        self.store = store
        self.timeout_seconds = int(timeout_seconds)
        self._process_factory = process_factory
        self._process_lock = threading.Lock()
        self._active_process = None
        self._interrupt_requested = threading.Event()
        self.model = str(route.get("model") or _MODEL_ID)
        self.model_label = _route_model_label(self.model)
        self.provider = _PROVIDER_ID
        self.requested_provider = _PROVIDER_ID
        self.base_url = ""
        self.api_key = ""
        self.api_mode = "external_cli"
        self.reasoning_config = {
            "enabled": True,
            "effort": str(route.get("reasoning_effort") or "medium"),
        }
        self.service_tier = None
        self.tools: list = []
        self.context_compressor = None
        self.interim_assistant_callback = None
        self.background_review_callback = None
        self.session_input_tokens = 0
        self.session_output_tokens = 0
        self.session_reasoning_tokens = 0
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_total_tokens = 0
        self.session_api_calls = 0

    @property
    def profile_id(self) -> str:
        return str(self.route["fleet_profile_id"])

    @property
    def lineage_root_id(self) -> str:
        return str(self.route["fleet_lineage_root_id"])

    def clear_interrupt(self) -> None:
        self._interrupt_requested.clear()

    def interrupt(self) -> None:
        self._interrupt_requested.set()
        with self._process_lock:
            process = self._active_process
        if process is not None:
            try:
                process.kill()
            except OSError:
                pass

    def close(self) -> None:
        self.interrupt()

    def _argv(
        self,
        prompt: str,
        *,
        conversation_id: str | None,
        log_path: Path,
    ) -> list[str]:
        argv = [
            self.executable,
            "--model",
            self.model_label,
        ]
        if conversation_id:
            argv.extend(("--conversation", conversation_id))
        argv.extend(
            (
                "--print",
                prompt,
                "--print-timeout",
                f"{self.timeout_seconds}s",
                "--log-file",
                str(log_path),
            )
        )
        return argv

    def _finalize_receipt(
        self,
        log_path: Path,
        check: dict[str, object],
    ) -> dict[str, object]:
        check["evidence_id"] = _finalize_agy_log(
            log_path,
            check,
            retain_evidence=True,
        )
        return check

    def run_conversation(
        self,
        user_message: str,
        *,
        conversation_history: list[dict] | None = None,
        stream_callback: Callable[[str], None] | None = None,
        task_id: str | None = None,
        **_kwargs,
    ) -> dict[str, Any]:
        if not isinstance(user_message, str) or not user_message.strip():
            raise ValueError("external parent prompt must be non-empty text")
        history = list(conversation_history or [])
        roles = [str(message.get("role") or "") for message in history]
        if any(role not in {"user", "assistant"} for role in roles):
            raise ValueError("external parent history must contain only user/assistant")
        if any(
            role != ("user" if index % 2 == 0 else "assistant")
            for index, role in enumerate(roles)
        ) or (roles and roles[-1] != "assistant"):
            raise ValueError("external parent history must alternate user/assistant")

        conversation_id = self.store.read_external_parent_conversation(
            self.profile_id,
            self.lineage_root_id,
        )
        log_path = _agy_log_path(f"parent-{uuid.uuid4().hex}")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        process = self._process_factory(
            self._argv(
                user_message,
                conversation_id=conversation_id,
                log_path=log_path,
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=self.cwd,
            env=safe_child_environment(),
            shell=False,
        )
        with self._process_lock:
            self._active_process = process
        try:
            try:
                stdout, stderr = process.communicate(
                    timeout=self.timeout_seconds
                )
            except subprocess.TimeoutExpired as exc:
                process.kill()
                process.wait()
                self._finalize_receipt(
                    log_path,
                    {"status": "process_timeout"},
                )
                raise TimeoutError(
                    "Antigravity external parent turn timed out"
                ) from exc
        finally:
            with self._process_lock:
                self._active_process = None

        if self._interrupt_requested.is_set():
            self._finalize_receipt(log_path, {"status": "cancelled"})
            return {
                "final_response": "",
                "messages": history,
                "interrupted": True,
            }

        check = _parent_receipt(
            log_path,
            canonical_model_id=self.model,
            expected_display_label=self.model_label,
            expected_conversation_id=conversation_id,
        )
        if process.returncode != 0:
            check.update(
                {
                    "status": "process_exit_nonzero",
                    "process_returncode": process.returncode,
                    "stderr_receipt": _stderr_receipt(stderr),
                }
            )
            self._finalize_receipt(log_path, check)
            raise RuntimeError("Antigravity external parent process failed")
        if check.get("status") != "matched":
            status = str(check.get("status") or "missing")
            self._finalize_receipt(log_path, check)
            raise RuntimeError(
                f"Antigravity served-model receipt rejected: {status}"
            )

        output = str(stdout or "").strip()
        if not output or len(output) > _MAX_OUTPUT_CHARS:
            check["status"] = (
                "empty_output" if not output else "output_too_large"
            )
            self._finalize_receipt(log_path, check)
            raise RuntimeError("Antigravity external parent output was invalid")
        served_conversation_id = str(check["conversation_id"])
        check = self._finalize_receipt(log_path, check)
        if check.get("evidence_id") == "not_persisted":
            raise RuntimeError(
                "Antigravity served-model receipt evidence could not be persisted"
            )
        self.store.bind_external_parent_conversation(
            profile_id=self.profile_id,
            lineage_root_id=self.lineage_root_id,
            lane_id="antigravity",
            conversation_id=served_conversation_id,
        )

        messages = [
            *history,
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": output},
        ]
        if self._session_db is not None:
            self._session_db.append_message(
                self.session_id,
                role="user",
                content=user_message,
            )
            self._session_db.append_message(
                self.session_id,
                role="assistant",
                content=output,
            )
        if stream_callback is not None:
            stream_callback(output)
        self.session_api_calls += 1
        return {
            "final_response": output,
            "messages": messages,
            "external_parent_receipt": check,
        }


def build_external_parent_driver(
    *,
    route: Mapping[str, Any],
    cwd: Path,
    session_id: str,
    session_db,
) -> ExternalParentSessionDriver:
    """Resolve the live agy subscription route without any API-key fallback."""

    service = build_fleet_service()
    qualification = service.qualifications.get("antigravity")
    if (
        qualification is None
        or not qualification.qualified
        or not qualification.parent_session_proven
        or not qualification.subscription_only_proven
        or not qualification.paid_fallback_absent
        or qualification.auth_kind != "cli_subscription"
        or qualification.provider_id != _PROVIDER_ID
        or str(route.get("model") or _MODEL_ID) not in qualification.models
        or not qualification.executable
    ):
        raise RuntimeError(
            "live Antigravity subscription parent qualification unavailable"
        )
    executable = Path(qualification.executable).resolve()
    if not executable.is_file():
        raise RuntimeError("qualified Antigravity executable is unavailable")
    return ExternalParentSessionDriver(
        executable=str(executable),
        route=route,
        cwd=cwd,
        session_id=session_id,
        session_db=session_db,
        store=service.store,
        timeout_seconds=service.config.execution_timeout_seconds,
    )
