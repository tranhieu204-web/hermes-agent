"""Read-only, fail-closed qualification of live subscription lanes."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hermes_constants import get_hermes_home

from .adapters.base import safe_child_environment
from .adapters.live_routes import (
    _AGY_MODEL_LABELS,
    _agy_log_path,
    inspect_agy_subscription_receipt,
)
from .types import AdapterKind, LaneProfile, OverageState, Qualification


_FORBIDDEN_ENV = {
    "chatgpt_codex": ("OPENAI_API_KEY",),
    "claude_code": ("ANTHROPIC_API_KEY",),
    "grok": ("XAI_API_KEY",),
    "antigravity": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
}

# Bounded doctor live-probe lifecycle:
# - one short no-write print-mode probe per cache miss
# - short-TTL sanitized proof cache under HERMES_HOME (default 5 minutes)
# - raw logs always deleted on success and failure
# - ordinary status/doctor rendering reuses the cache and must not re-fire
#   unlimited inference while the proof is fresh
_PROOF_CACHE_SCHEMA = "2"
_PROOF_CACHE_TTL = timedelta(minutes=5)
_PROBE_TIMEOUT_SECONDS = 60
_PROBE_PROMPT = "Reply with exactly: pong"
_PROOF_CACHE_NAME = "doctor-live-proof.json"

_RECEIPT_STATUS_DETAIL = {
    "display_label_mismatch": "live served-model receipt mismatch",
    "served_model_mismatch": "live served-model receipt mismatch",
    "expected_label_mismatch": "live served-model receipt mismatch",
    "ambiguous_served_model": "live served-model receipt mismatch (ambiguous)",
    "unsupported_served_model": "live served-model receipt unsupported model",
    "served_model_identity_invalid": "live served-model receipt identity invalid",
    "missing_receipt": "live served-model receipt missing",
    "missing_log": "live served-model receipt missing",
    "malformed_receipt": "live served-model receipt malformed",
    "malformed_log": "live served-model receipt malformed",
    "unreadable_log": "live served-model receipt unreadable",
    "subscription_auth_missing": "live served-model receipt auth method missing",
    "subscription_endpoint_missing": "live served-model receipt endpoint missing",
    "api_key_route_present": "live served-model receipt api-key route present",
    "process_timeout": "live served-model receipt timeout",
    "process_launch_failed": "live served-model receipt launch failed",
    "process_exit_nonzero": "live served-model receipt nonzero exit",
    "empty_output": "live served-model receipt empty output",
    "unsupported_model": "live served-model receipt unsupported model",
}


def _command(argv: Sequence[str]) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=10,
            shell=False,
            check=False,
        )
        return completed.returncode, completed.stdout, completed.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", type(exc).__name__


def _claude_code_oauth_status() -> Mapping[str, object]:
    """Return secret-free evidence for the live Claude Code OAuth record."""

    from agent.anthropic_adapter import (
        _is_oauth_token,
        is_claude_code_token_valid,
        read_claude_code_credentials,
    )

    credentials = read_claude_code_credentials()
    if not isinstance(credentials, dict):
        return {"logged_in": False}
    token = str(credentials.get("accessToken") or "").strip()
    source = str(credentials.get("source") or "").strip()
    return {
        "logged_in": bool(
            source
            and token
            and _is_oauth_token(token)
            and is_claude_code_token_valid(credentials)
        ),
        "auth_mode": "claude_code_oauth",
        "source": source,
    }


def _sanitize_receipt_detail(status: str) -> str:
    return _RECEIPT_STATUS_DETAIL.get(
        status, f"live served-model receipt {status.replace('_', ' ')}"
    )


class FleetQualificationDoctor:
    """Inspect route identity without returning or persisting credentials."""

    def __init__(
        self,
        *,
        auth_status: Callable[[str], Mapping[str, object]] | None = None,
        claude_oauth_status: Callable[[], Mapping[str, object]] | None = None,
        which: Callable[[str], str | None] = shutil.which,
        command: Callable[[Sequence[str]], tuple[int, str, str]] = _command,
        environment: Mapping[str, str] | None = None,
        billing_status: Callable[[str], Mapping[str, object]] | None = None,
        now: Callable[[], datetime] | None = None,
        platform_name: str | None = None,
        run_process: Callable[..., object] | None = None,
        proof_cache_dir: str | Path | None = None,
        probe_timeout_seconds: int = _PROBE_TIMEOUT_SECONDS,
    ) -> None:
        if auth_status is None:
            from hermes_cli.auth import get_auth_status

            auth_status = get_auth_status
        self.auth_status = auth_status
        self.claude_oauth_status = (
            claude_oauth_status or _claude_code_oauth_status
        )
        self.which = which
        self.command = command
        self.environment = dict(os.environ if environment is None else environment)
        self.billing_status = billing_status
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.platform_name = os.name if platform_name is None else platform_name
        self.run_process = run_process or subprocess.run
        self.proof_cache_dir = (
            Path(proof_cache_dir)
            if proof_cache_dir is not None
            else get_hermes_home() / "fleet" / "evidence" / "agy"
        )
        self.probe_timeout_seconds = max(1, int(probe_timeout_seconds))

    def _failed(
        self, profile: LaneProfile, detail: str, *, executable: str | None = None
    ) -> Qualification:
        at = self.now().astimezone(timezone.utc)
        return Qualification(
            qualified=False,
            captured_at=at,
            expires_at=at + timedelta(minutes=5),
            auth_kind=None,
            auth_source=None,
            overage_disabled=None,
            provider_id=profile.provider_id,
            models=(),
            efforts=(),
            fast_off_supported=False,
            capabilities=frozenset(),
            executable=executable,
            evidence_id=f"live-doctor:{profile.lane_id}:not-qualified",
            detail=detail,
        )

    def _proof_cache_path(self) -> Path:
        return Path(self.proof_cache_dir) / _PROOF_CACHE_NAME

    def _read_proof_cache(
        self,
        *,
        executable: str,
        version: str,
        model_id: str,
    ) -> Mapping[str, object] | None:
        path = self._proof_cache_path()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("schema_version") != _PROOF_CACHE_SCHEMA:
            return None
        if payload.get("lane_id") != "antigravity":
            return None
        if payload.get("executable") != executable:
            return None
        if payload.get("version") != version:
            return None
        if payload.get("canonical_model_id") != model_id:
            return None
        expires_raw = payload.get("expires_at")
        if not isinstance(expires_raw, str):
            return None
        try:
            expires_at = datetime.fromisoformat(expires_raw)
        except ValueError:
            return None
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if self.now().astimezone(timezone.utc) >= expires_at.astimezone(timezone.utc):
            return None
        status = payload.get("status")
        if not isinstance(status, str) or not status:
            return None
        if status == "matched" and (
            payload.get("served_model_id") != model_id
            or payload.get("served_model_label") != _AGY_MODEL_LABELS.get(model_id)
            or payload.get("auth_method") != "consumer"
            or payload.get("endpoint_kind") != "antigravity_cloud_code"
        ):
            return None
        return payload

    def _write_proof_cache(self, payload: Mapping[str, object]) -> None:
        path = self._proof_cache_path()
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(dict(payload), separators=(",", ":"), sort_keys=True),
                encoding="utf-8",
            )
            temporary.replace(path)
        except OSError:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _delete_raw_log(self, log_path: Path) -> None:
        try:
            log_path.unlink(missing_ok=True)
        except OSError:
            pass

    def _probe_agy_live_receipt(
        self,
        *,
        profile: LaneProfile,
        executable: str,
        version: str,
        model_id: str,
        display_label: str,
    ) -> tuple[Mapping[str, object] | None, str | None]:
        """Run one bounded print-mode probe; return sanitized proof or error."""

        cached = self._read_proof_cache(
            executable=executable, version=version, model_id=model_id
        )
        if cached is not None:
            if cached.get("status") == "matched":
                return cached, None
            status = str(cached.get("status") or "failed")
            return None, _sanitize_receipt_detail(status)

        run_id = f"doctor-{uuid.uuid4().hex}"
        log_path = _agy_log_path(run_id)
        # Keep doctor probe logs under the injectable proof cache dir when tests
        # pin HERMES_HOME-unrelated paths, while production uses get_hermes_home().
        if self.proof_cache_dir is not None:
            log_path = Path(self.proof_cache_dir) / f"{run_id}.log"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None, "live served-model receipt unreadable"

        argv = [
            executable,
            "-p",
            _PROBE_PROMPT,
            "--model",
            display_label,
            "--log-file",
            str(log_path),
            "--print-timeout",
            f"{self.probe_timeout_seconds}s",
        ]
        at = self.now().astimezone(timezone.utc)
        try:
            completed = self.run_process(
                argv,
                capture_output=True,
                text=True,
                cwd=str(Path.cwd()),
                env=safe_child_environment(self.environment),
                timeout=self.probe_timeout_seconds,
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired:
            self._delete_raw_log(log_path)
            proof = {
                "schema_version": _PROOF_CACHE_SCHEMA,
                "lane_id": profile.lane_id,
                "executable": executable,
                "version": version,
                "canonical_model_id": model_id,
                "status": "process_timeout",
                "captured_at": at.isoformat(),
                "expires_at": (at + _PROOF_CACHE_TTL).isoformat(),
            }
            self._write_proof_cache(proof)
            return None, _sanitize_receipt_detail("process_timeout")
        except OSError:
            self._delete_raw_log(log_path)
            proof = {
                "schema_version": _PROOF_CACHE_SCHEMA,
                "lane_id": profile.lane_id,
                "executable": executable,
                "version": version,
                "canonical_model_id": model_id,
                "status": "process_launch_failed",
                "captured_at": at.isoformat(),
                "expires_at": (at + _PROOF_CACHE_TTL).isoformat(),
            }
            self._write_proof_cache(proof)
            return None, _sanitize_receipt_detail("process_launch_failed")

        receipt = inspect_agy_subscription_receipt(
            log_path,
            canonical_model_id=model_id,
            expected_display_label=display_label,
        )
        self._delete_raw_log(log_path)

        status = str(receipt.get("status") or "missing_receipt")
        stdout = getattr(completed, "stdout", "")
        returncode = getattr(completed, "returncode", 1)
        if returncode != 0:
            status = "process_exit_nonzero"
        elif status == "matched" and (
            not isinstance(stdout, str) or not stdout.strip()
        ):
            status = "empty_output"

        proof: dict[str, object] = {
            "schema_version": _PROOF_CACHE_SCHEMA,
            "lane_id": profile.lane_id,
            "executable": executable,
            "version": version,
            "canonical_model_id": model_id,
            "status": status,
            "captured_at": at.isoformat(),
            "expires_at": (at + _PROOF_CACHE_TTL).isoformat(),
        }
        if status == "matched":
            proof.update(
                {
                    "served_model_id": receipt.get("served_model_id"),
                    "served_model_label": receipt.get("served_model_label"),
                    "auth_method": receipt.get("auth_method"),
                    "endpoint_kind": receipt.get("endpoint_kind"),
                    "log_sha256": receipt.get("log_sha256"),
                    "log_bytes": receipt.get("log_bytes"),
                    "receipt_count": receipt.get("receipt_count"),
                }
            )
        self._write_proof_cache(proof)
        if status != "matched":
            return None, _sanitize_receipt_detail(status)
        return proof, None

    def _external_receipt(
        self, profile: LaneProfile, executable: str
    ) -> tuple[str | None, tuple[str, ...], str | None]:
        command_name = executable
        version_code, version_out, _ = self.command((command_name, "--version"))
        if version_code != 0 or not version_out.strip():
            return None, (), "version command failed"
        version = version_out.strip().splitlines()[0]
        code, stdout, _ = self.command((command_name, "models"))
        if code != 0:
            return None, (), "agy models command failed"
        listed_tokens = frozenset(
            re.findall(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", stdout)
        )
        qualified_models = tuple(
            model for model in profile.ordered_models if model in listed_tokens
        )
        if not qualified_models:
            return None, (), "required exact model absent from agy models"

        if profile.lane_id != "antigravity":
            return version, qualified_models, None

        # Static catalog listing is never sufficient for Antigravity admission.
        model_id = qualified_models[0]
        display_label = _AGY_MODEL_LABELS.get(model_id)
        if display_label is None:
            return None, (), _sanitize_receipt_detail("unsupported_model")
        _proof, error = self._probe_agy_live_receipt(
            profile=profile,
            executable=executable,
            version=version,
            model_id=model_id,
            display_label=display_label,
        )
        if error:
            return None, (), error
        return version, (model_id,), None

    def _external_executable(self, profile: LaneProfile) -> str | None:
        executable = self.which(profile.executable or "")
        if executable:
            return str(Path(executable).resolve())
        if (
            self.platform_name == "nt"
            and profile.lane_id == "antigravity"
            and self.environment.get("LOCALAPPDATA")
        ):
            candidate = (
                Path(self.environment["LOCALAPPDATA"])
                / "agy"
                / "bin"
                / "agy.exe"
            ).resolve()
            if candidate.is_file():
                return str(candidate)
        return None

    def qualify(self, profiles: Iterable[LaneProfile]) -> dict[str, Qualification]:
        result: dict[str, Qualification] = {}
        for profile in profiles:
            if not profile.implemented:
                result[profile.lane_id] = self._failed(
                    profile, "adapter is not implemented"
                )
                continue
            forbidden = next(
                (name for name in _FORBIDDEN_ENV.get(profile.lane_id, ()) if self.environment.get(name)),
                None,
            )
            if forbidden:
                result[profile.lane_id] = self._failed(
                    profile, f"forbidden API-key environment variable present: {forbidden}"
                )
                continue
            at = self.now().astimezone(timezone.utc)
            executable = None
            version = None
            models = profile.ordered_models
            auth_source = None
            auth_kind = "oauth_subscription"
            parent_session_proven = False
            if profile.adapter_kind is AdapterKind.NATIVE_PROVIDER:
                if profile.provider_id == "anthropic":
                    status = self.claude_oauth_status()
                    expected_mode = "claude_code_oauth"
                else:
                    status = self.auth_status(profile.provider_id)
                    expected_mode = (
                        "chatgpt"
                        if profile.provider_id == "openai-codex"
                        else "oauth_device_code"
                    )
                if status.get("logged_in") is not True or status.get("auth_mode") != expected_mode:
                    result[profile.lane_id] = self._failed(
                        profile, f"{profile.provider_id} subscription OAuth is not proven"
                    )
                    continue
                source = status.get("source")
                if not isinstance(source, str) or not source:
                    result[profile.lane_id] = self._failed(
                        profile, f"{profile.provider_id} auth source is not attributable"
                    )
                    continue
                # Credential-pool aliases (for example ``pool:...``) and the
                # runtime resolver's source label (for example
                # ``manual:device_code``) describe the same OAuth credential
                # through different layers.  Bind execution to the stable,
                # provider-scoped subscription identity after both layers have
                # independently proven their attributable source.
                auth_source = (
                    "anthropic:claude_code_oauth"
                    if profile.provider_id == "anthropic"
                    else f"{profile.provider_id}:oauth_subscription"
                )
            else:
                executable = self._external_executable(profile)
                if not executable:
                    result[profile.lane_id] = self._failed(
                        profile, f"executable not found: {profile.executable}"
                    )
                    continue
                version, models, error = self._external_receipt(
                    profile, executable
                )
                if error:
                    result[profile.lane_id] = self._failed(
                        profile, error, executable=executable
                    )
                    continue
                auth_kind = "cli_subscription"
                auth_source = "antigravity:agy-live-receipt"
                parent_session_proven = profile.lane_id == "antigravity"
            if profile.lane_id == "claude_code":
                policy_detail = (
                    "observed evidence: live Claude Code OAuth credential, exact "
                    "native Anthropic route, and forbidden billable API-key env absent; "
                    "provider overage state requires separate billing telemetry"
                )
            elif profile.lane_id == "antigravity":
                policy_detail = (
                    "policy evidence: agy executable/version plus exact model "
                    "catalog gate, bounded live consumer-subscription "
                    "served-model receipt (label/auth/endpoint/no API-key), "
                    "and forbidden billable API-key env absent; the persistent "
                    "external parent driver binds Hermes lineage to agy "
                    "--conversation continuity and requires the same exact "
                    "consumer-subscription served-model receipt on every turn; "
                    "the sanitized route has no paid/API-key fallback and any "
                    "explicit bridge overage-on evidence still blocks admission; "
                    "doctor proof cache TTL is short and raw probe logs are deleted"
                )
            else:
                policy_detail = (
                    f"policy evidence: {profile.provider_id} subscription OAuth "
                    "route and forbidden billable API-key env absent; "
                    "provider overage state requires separate billing telemetry"
                )
            billing = (
                dict(self.billing_status(profile.lane_id))
                if self.billing_status is not None
                else {}
            )
            if (
                profile.lane_id == "antigravity"
                and "overage_state" not in billing
            ):
                # Live consumer/no-key/no-fallback receipt proves the only
                # admitted Antigravity route has no paid fallback path. This
                # narrow default does not invent provider overage telemetry
                # for other lanes. An explicit bridge "on" value still wins
                # and blocks admission below.
                billing["overage_state"] = OverageState.OFF.value
            try:
                overage_state = OverageState(
                    str(billing.get("overage_state", OverageState.UNKNOWN.value))
                )
            except ValueError:
                overage_state = OverageState.UNKNOWN
            result[profile.lane_id] = Qualification(
                qualified=True,
                captured_at=at,
                expires_at=at + timedelta(minutes=5),
                auth_kind=auth_kind,
                auth_source=auth_source,
                overage_disabled=overage_state is OverageState.OFF,
                provider_id=profile.provider_id,
                models=models,
                efforts=profile.supported_efforts,
                fast_off_supported=True,
                capabilities=profile.capabilities,
                executable=str(Path(executable).resolve()) if executable else None,
                version=version,
                evidence_id=f"live-doctor:{profile.lane_id}:{at.isoformat()}",
                detail=policy_detail,
                subscription_only_proven=True,
                paid_fallback_absent=True,
                overage_state=overage_state,
                parent_session_proven=parent_session_proven,
            )
        return result
