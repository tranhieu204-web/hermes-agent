"""Safe non-interactive external CLI adapter."""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from .base import safe_child_environment, validate_execution
from ..types import (
    AdapterKind,
    AdapterRequest,
    AdapterResult,
    Qualification,
    ReasonCode,
)
from ..usage_refresh import no_console_creationflags


class ExternalCliAdapter:
    def __init__(
        self,
        executable: str,
        *,
        fixed_args: Sequence[str] = (),
        base_environment: Mapping[str, str] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> None:
        self.executable = executable
        self.fixed_args = tuple(fixed_args)
        self.environment = safe_child_environment(base_environment)
        self.cancelled = cancelled or (lambda: False)

    def _resolved_executable(self) -> Path | None:
        candidate = Path(self.executable)
        if candidate.is_absolute():
            return candidate.resolve() if candidate.is_file() else None
        found = shutil.which(self.executable, path=self.environment.get("PATH"))
        return Path(found).resolve() if found else None

    @staticmethod
    def _failure(
        request: AdapterRequest,
        qualification: Qualification,
        reason: ReasonCode,
    ) -> AdapterResult:
        return AdapterResult(
            ok=False,
            reason=reason,
            provider_id=request.profile.provider_id,
            model_id=request.model,
            auth_kind=qualification.auth_kind or "unknown",
            adapter_kind=AdapterKind.EXTERNAL_CLI,
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
            or not qualification.executable
            or executable != Path(qualification.executable).resolve()
        ):
            return self._failure(
                request, qualification, ReasonCode.QUALIFICATION_FAILED
            )
        if self.cancelled():
            return self._failure(
                request, qualification, ReasonCode.EXECUTION_CANCELLED
            )
        argv = [
            str(executable),
            *self.fixed_args,
            "--model",
            request.model,
            "--effort",
            request.effort,
            "--fast-mode",
            "off",
            "--output-format",
            "json",
        ]
        try:
            completed = subprocess.run(
                argv,
                input=request.prompt,
                capture_output=True,
                text=True,
                cwd=request.cwd,
                env=self.environment,
                timeout=request.timeout_seconds,
                shell=False,
                check=False,
                creationflags=no_console_creationflags(),
            )
        except subprocess.TimeoutExpired:
            return self._failure(
                request, qualification, ReasonCode.EXECUTION_TIMEOUT
            )
        except OSError:
            return self._failure(
                request, qualification, ReasonCode.EXECUTION_FAILED
            )
        if completed.returncode != 0:
            return self._failure(
                request, qualification, ReasonCode.EXECUTION_FAILED
            )
        try:
            payload = json.loads(completed.stdout)
        except (json.JSONDecodeError, TypeError):
            return self._failure(
                request, qualification, ReasonCode.MALFORMED_OUTPUT
            )
        if not isinstance(payload, dict) or payload.get("schema_version") != "1":
            return self._failure(
                request, qualification, ReasonCode.MALFORMED_OUTPUT
            )
        if payload.get("provider_id") != request.profile.provider_id:
            return self._failure(
                request, qualification, ReasonCode.PROVIDER_MISMATCH
            )
        if payload.get("model_id") != request.model:
            return self._failure(
                request, qualification, ReasonCode.MODEL_MISMATCH
            )
        if payload.get("auth_kind") != qualification.auth_kind:
            return self._failure(
                request, qualification, ReasonCode.CREDENTIAL_MISMATCH
            )
        if payload.get("ok") is not True or not isinstance(
            payload.get("output", ""), str
        ):
            return self._failure(
                request, qualification, ReasonCode.EXECUTION_FAILED
            )
        metadata = {
            str(key): value
            for key, value in payload.items()
            if key
            not in {
                "schema_version",
                "ok",
                "provider_id",
                "model_id",
                "auth_kind",
                "output",
            }
        }
        return AdapterResult(
            ok=True,
            reason=ReasonCode.MET,
            provider_id=request.profile.provider_id,
            model_id=request.model,
            auth_kind=qualification.auth_kind or "unknown",
            adapter_kind=AdapterKind.EXTERNAL_CLI,
            output=payload["output"],
            metadata=metadata,
        )
