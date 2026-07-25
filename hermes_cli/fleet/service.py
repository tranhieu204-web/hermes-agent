"""Fleet inspection, selection, execution, and lifecycle orchestration."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable, Mapping, MutableMapping, Protocol, Sequence

from .capacity import BridgeUsageAdapter
from .config import FleetConfig
from .policy import evaluate_lane, select_lane
from .state import FleetStore
from .types import (
    AdapterRequest,
    AdapterResult,
    CapacityRead,
    FleetRunResult,
    LaneInputs,
    LaneProfile,
    ParentAdmission,
    ParentPin,
    ParentTurnAcquisition,
    Qualification,
    ReasonCode,
    RoutePurpose,
    RouteDecision,
    TaskSpec,
)


class ExecutableAdapter(Protocol):
    def execute(
        self, request: AdapterRequest, qualification: Qualification
    ) -> AdapterResult: ...


class FleetService:
    def __init__(
        self,
        *,
        config: FleetConfig,
        store: FleetStore,
        profiles: Sequence[LaneProfile],
        qualifications: Mapping[str, Qualification],
        adapters: Mapping[str, ExecutableAdapter],
        capacity_source: BridgeUsageAdapter,
        now: Callable[[], datetime] | None = None,
        owner_uuid: str | None = None,
        require_verified_health: bool = False,
    ) -> None:
        self.config = config
        self.store = store
        self.profiles = tuple(sorted(profiles, key=lambda item: item.order))
        self.qualifications: MutableMapping[str, Qualification] = dict(
            qualifications
        )
        self.adapters: MutableMapping[str, ExecutableAdapter] = dict(adapters)
        self.capacity_source = capacity_source
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.owner_uuid = owner_uuid or str(uuid.uuid4())
        self.require_verified_health = require_verified_health

    def _inputs(
        self,
        at: datetime,
        *,
        purpose: RoutePurpose = RoutePurpose.TASK_WORKER,
        lane_id: str | None = None,
    ) -> tuple[LaneInputs, ...]:
        candidates: list[LaneInputs] = []
        for profile in self.profiles:
            if lane_id is not None and profile.lane_id != lane_id:
                continue
            lane_config = self.config.lanes[profile.lane_id]
            active, reserved = self.store.lane_usage(profile.lane_id, now=at)
            cooldown = self.store.cooldown(profile.lane_id, now=at)
            capacity = (
                self.capacity_source.read(
                    profile.lane_id,
                    now=at,
                    reserved_pct=reserved,
                )
                if lane_config.enabled
                else CapacityRead(
                    None,
                    ReasonCode.CAPACITY_MISSING,
                    f"{profile.lane_id}: disabled; live usage not fetched",
                )
            )
            candidates.append(
                LaneInputs(
                    profile=profile,
                    enabled=lane_config.enabled,
                    adapter_found=(
                        profile.lane_id in self.adapters
                        if purpose is RoutePurpose.TASK_WORKER
                        else profile.supports_parent_session
                    ),
                    qualification=self.qualifications.get(profile.lane_id),
                    capacity=capacity,
                    active_leases=active,
                    active_reserved_pct=reserved,
                    max_concurrency=lane_config.max_concurrency,
                    reserve_floor_pct=lane_config.reserve_floor_pct,
                    cooldown_until=cooldown[0] if cooldown else None,
                    rotation_without_fresh_capacity=(
                        self.config.rotation_without_fresh_capacity
                    ),
                    require_verified_health=self.require_verified_health,
                )
            )
        return tuple(candidates)

    def inspect(self, task: TaskSpec, *, lane_id: str | None = None) -> tuple:
        at = self._now().astimezone(timezone.utc)
        return tuple(
            evaluate_lane(
                candidate,
                task,
                now=at,
                minimum_confidence=self.config.minimum_confidence,
            )
            for candidate in self._inputs(at, lane_id=lane_id)
        )

    def inspect_parent(self, task: TaskSpec, *, lane_id: str | None = None) -> tuple:
        at = self._now().astimezone(timezone.utc)
        return tuple(
            evaluate_lane(
                candidate,
                task,
                now=at,
                minimum_confidence=self.config.minimum_confidence,
                purpose=RoutePurpose.DESKTOP_PARENT,
            )
            for candidate in self._inputs(
                at, purpose=RoutePurpose.DESKTOP_PARENT, lane_id=lane_id
            )
        )

    def plan(self, task: TaskSpec) -> RouteDecision:
        evaluations = self.inspect(task)
        return select_lane(
            evaluations,
            rotation_index=self.store.rotation_cursor(),
            switch_delta=self.config.switch_delta_pct,
        )

    def preview_parent(self, task: TaskSpec) -> RouteDecision:
        evaluations = self.inspect_parent(task)
        if not self.config.enabled or not self.config.parent_desktop_enabled:
            return RouteDecision(
                lane_id=None,
                reason=ReasonCode.FLEET_DISABLED,
                evaluations=evaluations,
                rotation_index=self.store.rotation_cursor(
                    purpose=RoutePurpose.DESKTOP_PARENT
                ),
            )
        return select_lane(
            evaluations,
            rotation_index=self.store.rotation_cursor(
                purpose=RoutePurpose.DESKTOP_PARENT
            ),
            switch_delta=self.config.switch_delta_pct,
        )

    def admit_parent(
        self,
        *,
        profile_id: str,
        lineage_root_id: str,
        session_id: str,
        task: TaskSpec,
        preferred_lane_id: str | None = None,
        preferred_model_id: str | None = None,
    ) -> ParentAdmission:
        existing = self.store.read_parent_pin(profile_id, lineage_root_id)
        if existing is not None:
            return ParentAdmission(ReasonCode.MET, existing)
        if not self.config.enabled or not self.config.parent_desktop_enabled:
            return ParentAdmission(ReasonCode.FLEET_DISABLED, None)
        at = self._now().astimezone(timezone.utc)
        return self.store.admit_parent(
            profile_id=profile_id,
            lineage_root_id=lineage_root_id,
            session_id=session_id,
            task=task,
            candidates=self._inputs(
                at, purpose=RoutePurpose.DESKTOP_PARENT
            ),
            now=at,
            preferred_lane_id=preferred_lane_id,
            preferred_model_id=preferred_model_id,
        )

    def resolve_parent_pin(
        self, *, profile_id: str, lineage_root_id: str
    ) -> ParentPin | None:
        return self.store.read_parent_pin(profile_id, lineage_root_id)

    def acquire_parent_turn(
        self,
        *,
        profile_id: str,
        lineage_root_id: str,
        task: TaskSpec,
    ) -> ParentTurnAcquisition:
        at = self._now().astimezone(timezone.utc)
        return self.store.acquire_parent_turn(
            profile_id=profile_id,
            lineage_root_id=lineage_root_id,
            task=task,
            candidates=self._inputs(
                at, purpose=RoutePurpose.DESKTOP_PARENT
            ),
            owner_uuid=self.owner_uuid,
            ttl_seconds=self.config.lease_ttl_seconds,
            now=at,
        )

    def run(self, task: TaskSpec, *, prompt: str) -> FleetRunResult:
        at = self._now().astimezone(timezone.utc)
        if not self.config.enabled:
            return FleetRunResult(
                ok=False,
                task_id=task.task_id,
                reason=ReasonCode.FLEET_DISABLED,
                pin=self.store.read_pin(task.task_id),
                lease=None,
                adapter_result=None,
            )

        acquisition = self.store.acquire(
            task,
            self._inputs(at),
            owner_uuid=self.owner_uuid,
            ttl_seconds=self.config.lease_ttl_seconds,
            now=at,
        )
        if acquisition.reason is not ReasonCode.MET or acquisition.lease is None:
            return FleetRunResult(
                ok=False,
                task_id=task.task_id,
                reason=acquisition.reason,
                pin=acquisition.pin,
                lease=acquisition.lease,
                adapter_result=None,
                evaluations=acquisition.evaluations,
            )

        pin = acquisition.pin
        lease = acquisition.lease
        assert pin is not None
        qualification = self.qualifications.get(pin.lane_id)
        adapter = self.adapters.get(pin.lane_id)
        profile = next(
            (item for item in self.profiles if item.lane_id == pin.lane_id), None
        )
        if qualification is None or adapter is None or profile is None:
            self.store.release(lease, outcome="failed", now=at)
            return FleetRunResult(
                ok=False,
                task_id=task.task_id,
                reason=ReasonCode.PINNED_LANE_UNAVAILABLE,
                pin=pin,
                lease=lease,
                adapter_result=None,
                evaluations=acquisition.evaluations,
            )

        renewed = self.store.heartbeat(
            lease,
            ttl_seconds=self.config.lease_ttl_seconds,
            now=at,
        )
        if renewed is None:
            return FleetRunResult(
                ok=False,
                task_id=task.task_id,
                reason=ReasonCode.LEASE_CONFLICT,
                pin=pin,
                lease=lease,
                adapter_result=None,
                evaluations=acquisition.evaluations,
            )
        lease = renewed
        self.store.record_event(
            at=at,
            task_id=task.task_id,
            lane_id=pin.lane_id,
            event_type="EXECUTION_STARTED",
            reason=ReasonCode.MET.value,
            decision={
                "adapter_kind": pin.adapter_kind.value,
                "provider_id": pin.provider_id,
                "model_id": pin.model_id,
                "effort": pin.effort,
                "fast_mode": pin.fast_mode,
                "auth_kind": qualification.auth_kind,
                "qualification": qualification.evidence_id,
            },
        )
        request = AdapterRequest(
            task_id=task.task_id,
            cwd=task.cwd,
            prompt=prompt,
            profile=profile,
            model=pin.model_id,
            effort=pin.effort,
            fast_mode=pin.fast_mode,
            timeout_seconds=self.config.execution_timeout_seconds,
        )
        try:
            adapter_result = adapter.execute(request, qualification)
        except Exception:
            adapter_result = AdapterResult(
                ok=False,
                reason=ReasonCode.EXECUTION_FAILED,
                provider_id=pin.provider_id,
                model_id=pin.model_id,
                auth_kind=qualification.auth_kind or "unknown",
                adapter_kind=pin.adapter_kind,
            )

        cooldown_seconds = adapter_result.metadata.get("cooldown_seconds")
        if (
            not adapter_result.ok
            and adapter_result.metadata.get("classification") == "rate_limit"
            and isinstance(cooldown_seconds, int)
            and not isinstance(cooldown_seconds, bool)
            and cooldown_seconds > 0
        ):
            self.store.set_cooldown(
                pin.lane_id,
                until=at + timedelta(seconds=cooldown_seconds),
                reason="PROVIDER_RATE_LIMIT",
                now=at,
            )
        event_type = (
            "EXECUTION_COMPLETED" if adapter_result.ok else "EXECUTION_FAILED"
        )
        self.store.record_event(
            at=at,
            task_id=task.task_id,
            lane_id=pin.lane_id,
            event_type=event_type,
            reason=adapter_result.reason.value,
            decision={
                "adapter_kind": adapter_result.adapter_kind.value,
                "provider_id": adapter_result.provider_id,
                "model_id": adapter_result.model_id,
                "auth_kind": adapter_result.auth_kind,
            },
        )
        outcome = (
            "completed"
            if adapter_result.ok
            else (
                "cancelled"
                if adapter_result.reason is ReasonCode.EXECUTION_CANCELLED
                else "failed"
            )
        )
        self.store.release(lease, outcome=outcome, now=at)
        return FleetRunResult(
            ok=adapter_result.ok,
            task_id=task.task_id,
            reason=adapter_result.reason,
            pin=pin,
            lease=lease,
            adapter_result=adapter_result,
            evaluations=acquisition.evaluations,
        )

    def release_task(self, task_id: str, *, outcome: str) -> bool:
        at = self._now().astimezone(timezone.utc)
        lease = self.store.read_live_lease(task_id, now=at)
        return (
            self.store.release(lease, outcome=outcome, now=at)
            if lease is not None
            else False
        )
