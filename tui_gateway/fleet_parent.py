"""Narrow Desktop-parent bridge into the shared fleet policy/state engine."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from hermes_cli.fleet.inspection import build_fleet_service
from hermes_cli.fleet.state import FleetStore
from hermes_cli.fleet.adapters.live_routes import _AGY_MODEL_LABELS
from hermes_cli.fleet.types import (
    AdapterKind,
    ParentAdmission,
    ParentLeaseHandle,
    ParentPin,
    ReasonCode,
    RoutePurpose,
    TaskSpec,
)


PARENT_CAPABILITIES = frozenset({"workspace_write", "shell"})
_EXACT_BASE_URLS = {
    "openai-codex": "https://chatgpt.com/backend-api/codex",
    "anthropic": "https://api.anthropic.com",
    "xai-oauth": "https://api.x.ai/v1",
}
_EXACT_API_MODES = {
    "openai-codex": "codex_responses",
    "anthropic": "anthropic_messages",
    "xai-oauth": "codex_responses",
}
_FORBIDDEN_RUNTIME_SOURCE_FRAGMENTS = (
    "api-key",
    "api_key",
    "custom",
    "direct-alias",
    "env",
    "explicit",
)
PERSISTED_PARENT_ROUTE_KEYS = (
    "model_source",
    "fleet_profile_id",
    "fleet_lineage_root_id",
    "fleet_lane_id",
    "fleet_adapter_kind",
    "fleet_route_purpose",
    "fleet_selection_reason",
    "fleet_pin_status",
    "fleet_route_identity",
    "display_label",
)


def admit_parent_session(
    *,
    profile_id: str,
    lineage_root_id: str,
    session_id: str,
    cwd: str,
    preferred_lane_id: str | None = None,
    preferred_provider_id: str | None = None,
    preferred_model_id: str | None = None,
) -> ParentAdmission:
    """Commit one parent route before the gateway builds an agent."""

    service = build_fleet_service()
    task = TaskSpec(
        task_id=session_id,
        cwd=Path(cwd),
        required_capabilities=PARENT_CAPABILITIES,
        reservation_pct=service.config.default_reservation_pct,
    )
    return service.admit_parent(
        profile_id=profile_id,
        lineage_root_id=lineage_root_id,
        session_id=session_id,
        task=task,
        preferred_lane_id=preferred_lane_id,
        preferred_provider_id=preferred_provider_id,
        preferred_model_id=preferred_model_id,
    )


def parent_route_metadata(pin: ParentPin) -> dict[str, Any]:
    """Return only safe route identity needed by the live gateway and UI."""

    if pin.purpose is not RoutePurpose.DESKTOP_PARENT:
        raise ValueError("fleet pin is not a Desktop parent route")
    if pin.fast_mode:
        raise ValueError("fleet parent priority mode must be disabled")
    if pin.adapter_kind is AdapterKind.NATIVE_PROVIDER:
        if pin.provider_id not in _EXACT_BASE_URLS:
            raise ValueError("unsupported native fleet parent provider")
        display_label = {
            "anthropic": "Claude · Fleet",
            "openai-codex": "Codex · Fleet",
            "xai-oauth": "Grok · Fleet",
        }[pin.provider_id]
    elif (
        pin.adapter_kind is AdapterKind.EXTERNAL_CLI
        and pin.lane_id == "antigravity"
        and pin.provider_id == "antigravity-subscription"
        and pin.model_id in _AGY_MODEL_LABELS
    ):
        user_model_label = (
            _AGY_MODEL_LABELS[pin.model_id]
            .replace(" (High)", " High")
            .replace(" (Medium)", " Medium")
            .replace(" (Low)", " Low")
        )
        display_label = (
            f"Antigravity · {user_model_label} · external CLI"
        )
    else:
        raise ValueError("unsupported external fleet parent")
    return {
        "model_source": "fleet_auto",
        "fleet_profile_id": pin.profile_id,
        "fleet_lineage_root_id": pin.lineage_root_id,
        "fleet_lane_id": pin.lane_id,
        "fleet_adapter_kind": pin.adapter_kind.value,
        "fleet_route_purpose": pin.purpose.value,
        "fleet_selection_reason": pin.selection_reason.value,
        "fleet_pin_status": pin.status,
        "fleet_route_identity": pin.route_identity,
        "model": pin.model_id,
        "provider": pin.provider_id,
        "reasoning_effort": pin.effort,
        "fast": False,
        "display_label": display_label,
    }


def persisted_parent_route(route: object) -> dict[str, Any]:
    """Filter live route metadata down to the safe durable subset."""

    if not isinstance(route, dict):
        return {}
    return {
        key: route[key]
        for key in PERSISTED_PARENT_ROUTE_KEYS
        if isinstance(route.get(key), (str, bool, int, float))
    }


def restore_parent_route(
    session_row: object,
    *,
    profile_id: str,
    store: FleetStore | None = None,
) -> dict[str, Any] | None:
    """Resolve persisted safe lineage metadata back to its immutable pin."""

    if not isinstance(session_row, dict):
        return None
    raw_config = session_row.get("model_config")
    if isinstance(raw_config, str):
        try:
            raw_config = json.loads(raw_config)
        except (TypeError, json.JSONDecodeError):
            return None
    if not isinstance(raw_config, dict):
        return None
    if raw_config.get("model_source") != "fleet_auto":
        return None
    lineage_root_id = str(
        raw_config.get("fleet_lineage_root_id") or ""
    ).strip()
    expected_identity = str(
        raw_config.get("fleet_route_identity") or ""
    ).strip()
    if not lineage_root_id or not expected_identity:
        raise RuntimeError("fleet parent session metadata is incomplete")
    if store is None:
        from hermes_constants import get_hermes_home

        store = FleetStore(get_hermes_home() / "fleet" / "state.db")
    pin = store.read_parent_pin(profile_id, lineage_root_id)
    if pin is None:
        raise RuntimeError("fleet parent lineage pin is unavailable")
    if pin.route_identity != expected_identity:
        raise RuntimeError("fleet parent lineage route identity mismatch")
    return parent_route_metadata(pin)


def parent_runtime_overrides(route: dict[str, Any]) -> dict[str, Any]:
    """Build the only runtime overrides permitted for an authoritative pin."""

    from hermes_constants import parse_reasoning_effort

    return {
        "model_override": {
            "model": str(route["model"]),
            "provider": str(route["provider"]),
        },
        "provider_override": str(route["provider"]),
        "reasoning_config_override": parse_reasoning_effort(
            route.get("reasoning_effort")
        ),
        "service_tier_override": "",
    }


class ParentTurnUnavailable(RuntimeError):
    def __init__(self, reason: ReasonCode):
        super().__init__(f"fleet parent turn unavailable: {reason.value}")
        self.reason = reason


class ParentTurnGuard:
    """Own one parent occupancy lease for exactly one complete turn."""

    def __init__(
        self,
        *,
        service,
        lease: ParentLeaseHandle,
        ttl_seconds: int,
        start_heartbeat: bool,
    ) -> None:
        self.service = service
        self.lease = lease
        self.ttl_seconds = ttl_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._released = False
        if start_heartbeat:
            self._thread = threading.Thread(
                target=self._heartbeat_loop,
                name=f"fleet-parent-heartbeat-{lease.lineage_root_id}",
                daemon=True,
            )
            self._thread.start()

    def _heartbeat_loop(self) -> None:
        interval = max(1.0, self.ttl_seconds / 3)
        while not self._stop.wait(interval):
            renewed = self.service.store.heartbeat_parent(
                self.lease,
                ttl_seconds=self.ttl_seconds,
                now=datetime.now(timezone.utc),
            )
            if renewed is None:
                return
            self.lease = renewed

    def release(
        self,
        *,
        outcome: str,
        provider_failure: bool = False,
    ) -> bool:
        if self._released:
            return False
        self._released = True
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
        now = datetime.now(timezone.utc)
        released = self.service.store.release_parent_turn(
            self.lease,
            outcome=outcome,
            now=now,
        )
        if provider_failure:
            self.service.store.set_cooldown(
                self.lease.lane_id,
                until=now + timedelta(seconds=60),
                reason="PARENT_PROVIDER_FAILURE",
                now=now,
            )
        return released


def acquire_parent_turn_guard(
    route: dict[str, Any],
    *,
    cwd: Path,
    service=None,
    start_heartbeat: bool = True,
) -> ParentTurnGuard:
    """Fail closed unless the immutable pinned route can lease this turn."""

    if service is None:
        service = build_fleet_service()
    profile_id = str(route.get("fleet_profile_id") or "").strip()
    lineage_root_id = str(route.get("fleet_lineage_root_id") or "").strip()
    if not profile_id or not lineage_root_id:
        raise ParentTurnUnavailable(ReasonCode.PINNED_LANE_UNAVAILABLE)
    task = TaskSpec(
        task_id=lineage_root_id,
        cwd=cwd,
        required_capabilities=PARENT_CAPABILITIES,
        reservation_pct=service.config.default_reservation_pct,
    )
    acquisition = service.acquire_parent_turn(
        profile_id=profile_id,
        lineage_root_id=lineage_root_id,
        task=task,
    )
    if (
        acquisition.reason is not ReasonCode.MET
        or acquisition.pin is None
        or acquisition.lease is None
        or acquisition.pin.route_identity
        != str(route.get("fleet_route_identity") or "")
    ):
        reason = (
            acquisition.reason
            if isinstance(acquisition.reason, ReasonCode)
            else ReasonCode.PINNED_LANE_UNAVAILABLE
        )
        raise ParentTurnUnavailable(reason)
    return ParentTurnGuard(
        service=service,
        lease=acquisition.lease,
        ttl_seconds=service.config.lease_ttl_seconds,
        start_heartbeat=start_heartbeat,
    )


def admission_error(admission: ParentAdmission) -> str:
    reason = (
        admission.reason
        if isinstance(admission.reason, ReasonCode)
        else ReasonCode.NO_ELIGIBLE_LANE
    )
    return f"Fleet Auto parent admission failed: {reason.value}"


def resolve_exact_native_runtime(route: dict[str, Any]) -> dict[str, Any]:
    """Resolve only the pinned subscription runtime and reject substitution."""

    provider = str(route.get("provider") or "").strip()
    model = str(route.get("model") or "").strip()
    if provider not in _EXACT_BASE_URLS or not model:
        raise RuntimeError("invalid exact fleet parent route")

    if provider == "anthropic":
        from agent.anthropic_adapter import (
            _is_oauth_token,
            is_claude_code_token_valid,
            read_claude_code_credentials,
        )

        credentials = read_claude_code_credentials()
        token = (
            str(credentials.get("accessToken") or "").strip()
            if isinstance(credentials, dict)
            else ""
        )
        source = (
            str(credentials.get("source") or "").strip()
            if isinstance(credentials, dict)
            else ""
        )
        if (
            not isinstance(credentials, dict)
            or not source
            or not token
            or not _is_oauth_token(token)
            or not is_claude_code_token_valid(credentials)
        ):
            raise RuntimeError("live Claude Code OAuth credential unavailable")
        runtime = {
            "provider": "anthropic",
            "requested_provider": "anthropic",
            "base_url": _EXACT_BASE_URLS["anthropic"],
            "api_key": token,
            "api_mode": _EXACT_API_MODES["anthropic"],
            "source": source,
        }
    else:
        from hermes_cli.runtime_provider import resolve_runtime_provider

        runtime = resolve_runtime_provider(
            requested=provider,
            target_model=model,
        )
    resolved_provider = str(runtime.get("provider") or "").strip()
    requested_provider = str(runtime.get("requested_provider") or provider).strip()
    if resolved_provider != provider or requested_provider != provider:
        raise RuntimeError("fleet parent runtime provider substitution rejected")
    if str(runtime.get("api_mode") or "") != _EXACT_API_MODES[provider]:
        raise RuntimeError("fleet parent runtime API mode substitution rejected")
    expected_base = _EXACT_BASE_URLS[provider].rstrip("/")
    if str(runtime.get("base_url") or "").strip().rstrip("/") != expected_base:
        raise RuntimeError("fleet parent custom endpoint rejected")
    source = str(runtime.get("source") or "").strip().lower()
    if not source or any(
        fragment in source for fragment in _FORBIDDEN_RUNTIME_SOURCE_FRAGMENTS
    ):
        raise RuntimeError("fleet parent runtime auth-source substitution rejected")
    if not str(runtime.get("api_key") or "").strip():
        raise RuntimeError("fleet parent OAuth credential unavailable")
    return runtime
