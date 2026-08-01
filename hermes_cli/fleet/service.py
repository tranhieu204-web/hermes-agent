"""Fleet inspection, selection, execution, and lifecycle orchestration."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, MutableMapping, Protocol, Sequence

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


@dataclass(frozen=True)
class OwnedExternalExecution:
    """An acquired fleet lease reserved for one externally-owned material child.

    The service owns admission, lane lease and final audit/release.  The
    material adapter owns only argv-only child I/O, while the dispatcher binds
    the child identity to its separate recovery fence before it may run.
    """

    task: TaskSpec
    pin: object
    lease: object
    request: AdapterRequest
    qualification: Qualification
    adapter: ExecutableAdapter
    evaluations: tuple


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
        lane_selector: Callable[..., object] | None = None,
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
        self.lane_selector = lane_selector

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

    def _select_route(
        self, evaluations: tuple, *, rotation_index: int
    ) -> RouteDecision:
        if self.lane_selector is None:
            return select_lane(evaluations, rotation_index=rotation_index)

        eligible = tuple(item for item in evaluations if item.eligible)
        if not eligible:
            return select_lane(evaluations, rotation_index=rotation_index)

        cyclic = eligible[rotation_index % len(eligible)]
        selector_config: dict[str, Any] = {
            "fleet": {
                "switch_delta_pct": float(self.config.switch_delta_pct),
                "lanes": {
                    item.lane_id: {
                        "enabled": item.eligible,
                        "provider": item.profile.provider_id,
                        "model": item.selected_model
                        or next(iter(item.profile.ordered_models), ""),
                        "reserve_floor_pct": float(
                            self.config.lanes[item.lane_id].reserve_floor_pct
                        ),
                    }
                    for item in evaluations
                },
            }
        }
        selected = self.lane_selector(
            config=selector_config,
            current_provider=cyclic.profile.provider_id,
            is_heavy=True,
            now=self._now().astimezone(timezone.utc).timestamp(),
            usage_by_lane={
                item.lane_id: 100.0
                - float(item.capacity.effective_remaining_pct)
                for item in eligible
                if item.capacity is not None
            },
        )
        return select_lane(
            evaluations,
            rotation_index=rotation_index,
            selected_lane_id=str(getattr(selected, "lane", "") or ""),
        )

    def plan(self, task: TaskSpec) -> RouteDecision:
        return self._select_route(
            self.inspect(task),
            rotation_index=self.store.rotation_cursor(),
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
        preferred_provider_id: str | None = None,
        preferred_model_id: str | None = None,
    ) -> ParentAdmission:
        existing = self.store.read_parent_pin(profile_id, lineage_root_id)
        if existing is not None:
            return ParentAdmission(ReasonCode.MET, existing)
        if not self.config.enabled:
            return ParentAdmission(ReasonCode.FLEET_DISABLED, None)
        # parent_desktop_enabled governs AUTO-commissioning only (default
        # sessions the fleet routes by itself). An explicit operator pick of
        # lane+provider+model from the picker is its own consent and admits
        # while the auto toggle is off — every downstream gate (lane enabled,
        # qualification, parent_session_proven, capacity, occupancy) still
        # applies unchanged. Operator decision 2026-07-27: default chat stays
        # on the configured model with auto-parents off, while explicit
        # "Claude · Fleet" (or any lane) picks keep working.
        explicit_pick = bool(
            str(preferred_lane_id or "").strip()
            and str(preferred_provider_id or "").strip()
            and str(preferred_model_id or "").strip()
        )
        if not explicit_pick and not self.config.parent_desktop_enabled:
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
            preferred_provider_id=preferred_provider_id,
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

    def run(
        self,
        task: TaskSpec,
        *,
        prompt: str,
        preferred_lane_id: str | None = None,
    ) -> FleetRunResult:
        """Execute one task through the canonical fleet route.

        ``preferred_lane_id`` is a dispatcher-owned admission constraint, not
        a model-controlled provider override.  It exists so material review
        fan-out can reserve distinct, already-qualified lanes while still
        using the same lease, qualification, adapter, audit, and release
        machinery as ordinary fleet execution.
        """
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

        candidates = self._inputs(at)
        if preferred_lane_id is not None:
            preferred_lane_id = str(preferred_lane_id).strip()
            known_lanes = {candidate.profile.lane_id for candidate in candidates}
            if not preferred_lane_id or preferred_lane_id not in known_lanes:
                evaluations = tuple(
                    evaluate_lane(
                        candidate,
                        task,
                        now=at,
                        minimum_confidence=self.config.minimum_confidence,
                    )
                    for candidate in candidates
                )
                self.store.record_event(
                    at=at,
                    task_id=task.task_id,
                    lane_id=preferred_lane_id or None,
                    event_type="ROUTE_DENIED",
                    reason=ReasonCode.NO_ELIGIBLE_LANE.value,
                    decision={"preferred_lane_id": preferred_lane_id},
                )
                return FleetRunResult(
                    ok=False,
                    task_id=task.task_id,
                    reason=ReasonCode.NO_ELIGIBLE_LANE,
                    pin=self.store.read_pin(task.task_id),
                    lease=None,
                    adapter_result=None,
                    evaluations=evaluations,
                )
            existing_pin = self.store.read_pin(task.task_id)
            if (
                existing_pin is not None
                and (
                    existing_pin.lane_id != preferred_lane_id
                    or not any(
                        item.eligible
                        and item.profile.lane_id == preferred_lane_id
                        and item.profile.provider_id == existing_pin.provider_id
                        and item.profile.adapter_kind == existing_pin.adapter_kind
                        and item.selected_model == existing_pin.model_id
                        and item.selected_effort == existing_pin.effort
                        for item in (
                            evaluate_lane(
                                candidate,
                                task,
                                now=at,
                                minimum_confidence=self.config.minimum_confidence,
                            )
                            for candidate in candidates
                        )
                    )
                )
            ):
                evaluations = tuple(
                    evaluate_lane(
                        candidate,
                        task,
                        now=at,
                        minimum_confidence=self.config.minimum_confidence,
                    )
                    for candidate in candidates
                )
                self.store.record_event(
                    at=at,
                    task_id=task.task_id,
                    lane_id=existing_pin.lane_id,
                    event_type="ROUTE_DENIED",
                    reason=ReasonCode.PINNED_LANE_UNAVAILABLE.value,
                    decision={
                        "pinned_lane_id": existing_pin.lane_id,
                        "preferred_lane_id": preferred_lane_id,
                        "identity_check": "mismatch",
                    },
                )
                return FleetRunResult(
                    ok=False,
                    task_id=task.task_id,
                    reason=ReasonCode.PINNED_LANE_UNAVAILABLE,
                    pin=existing_pin,
                    lease=None,
                    adapter_result=None,
                    evaluations=evaluations,
                )
        selected_lane_id = None
        if self.store.read_pin(task.task_id) is None:
            if preferred_lane_id is not None:
                selected_lane_id = preferred_lane_id
            else:
                evaluations = tuple(
                    evaluate_lane(
                        candidate,
                        task,
                        now=at,
                        minimum_confidence=self.config.minimum_confidence,
                    )
                    for candidate in candidates
                )
                selected_lane_id = self._select_route(
                    evaluations,
                    rotation_index=self.store.rotation_cursor(),
                ).lane_id

        acquisition = self.store.acquire(
            task,
            candidates,
            owner_uuid=self.owner_uuid,
            ttl_seconds=self.config.lease_ttl_seconds,
            now=at,
            selected_lane_id=selected_lane_id,
            enforce_selected_lane=(
                preferred_lane_id is not None or self.lane_selector is not None
            ),
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

    def acquire_owned_external(
        self, task: TaskSpec, *, prompt: str, preferred_lane_id: str,
    ) -> OwnedExternalExecution | FleetRunResult:
        """Reserve one qualified argv-only external lane without running it yet."""
        at = self._now().astimezone(timezone.utc)
        preferred_lane_id = str(preferred_lane_id or "").strip()
        candidates = self._inputs(at)
        known_lanes = {candidate.profile.lane_id for candidate in candidates}
        if not self.config.enabled:
            return FleetRunResult(False, task.task_id, ReasonCode.FLEET_DISABLED, self.store.read_pin(task.task_id), None, None)
        if not preferred_lane_id or preferred_lane_id not in known_lanes:
            evaluations = tuple(evaluate_lane(candidate, task, now=at, minimum_confidence=self.config.minimum_confidence) for candidate in candidates)
            return FleetRunResult(False, task.task_id, ReasonCode.NO_ELIGIBLE_LANE, self.store.read_pin(task.task_id), None, None, evaluations)
        existing_pin = self.store.read_pin(task.task_id)
        if existing_pin is not None and existing_pin.lane_id != preferred_lane_id:
            evaluations = tuple(evaluate_lane(candidate, task, now=at, minimum_confidence=self.config.minimum_confidence) for candidate in candidates)
            return FleetRunResult(False, task.task_id, ReasonCode.PINNED_LANE_UNAVAILABLE, existing_pin, None, None, evaluations)
        acquisition = self.store.acquire(
            task, candidates, owner_uuid=self.owner_uuid, ttl_seconds=self.config.lease_ttl_seconds,
            now=at, selected_lane_id=preferred_lane_id, enforce_selected_lane=True,
        )
        if acquisition.reason is not ReasonCode.MET or acquisition.lease is None:
            return FleetRunResult(False, task.task_id, acquisition.reason, acquisition.pin, acquisition.lease, None, acquisition.evaluations)
        pin, lease = acquisition.pin, acquisition.lease
        assert pin is not None
        qualification = self.qualifications.get(pin.lane_id)
        adapter = self.adapters.get(pin.lane_id)
        profile = next((item for item in self.profiles if item.lane_id == pin.lane_id), None)
        if qualification is None or adapter is None or profile is None or not callable(getattr(adapter, "start_owned_material", None)):
            self.store.release(lease, outcome="failed", now=at)
            return FleetRunResult(False, task.task_id, ReasonCode.PINNED_LANE_UNAVAILABLE, pin, lease, None, acquisition.evaluations)
        renewed = self.store.heartbeat(lease, ttl_seconds=self.config.lease_ttl_seconds, now=at)
        if renewed is None:
            return FleetRunResult(False, task.task_id, ReasonCode.LEASE_CONFLICT, pin, lease, None, acquisition.evaluations)
        self.store.record_event(
            at=at, task_id=task.task_id, lane_id=pin.lane_id, event_type="EXECUTION_STARTED", reason=ReasonCode.MET.value,
            decision={"adapter_kind": pin.adapter_kind.value, "provider_id": pin.provider_id, "model_id": pin.model_id,
                      "effort": pin.effort, "fast_mode": pin.fast_mode, "auth_kind": qualification.auth_kind,
                      "qualification": qualification.evidence_id, "owned_external": True},
        )
        return OwnedExternalExecution(
            task=task, pin=pin, lease=renewed,
            request=AdapterRequest(task_id=task.task_id, cwd=task.cwd, prompt=prompt, profile=profile,
                                   model=pin.model_id, effort=pin.effort, fast_mode=pin.fast_mode,
                                   timeout_seconds=self.config.execution_timeout_seconds),
            qualification=qualification, adapter=adapter, evaluations=acquisition.evaluations,
        )

    def finalize_owned_external(self, execution: OwnedExternalExecution, adapter_result: AdapterResult) -> FleetRunResult:
        """Record and release one exact owned external fleet lease once."""
        at = self._now().astimezone(timezone.utc)
        self.store.record_event(
            at=at, task_id=execution.task.task_id, lane_id=execution.pin.lane_id,
            event_type="EXECUTION_COMPLETED" if adapter_result.ok else "EXECUTION_FAILED",
            reason=adapter_result.reason.value,
            decision={"adapter_kind": adapter_result.adapter_kind.value, "provider_id": adapter_result.provider_id,
                      "model_id": adapter_result.model_id, "auth_kind": adapter_result.auth_kind, "owned_external": True},
        )
        outcome = "completed" if adapter_result.ok else ("cancelled" if adapter_result.reason is ReasonCode.EXECUTION_CANCELLED else "failed")
        self.store.release(execution.lease, outcome=outcome, now=at)
        return FleetRunResult(adapter_result.ok, execution.task.task_id, adapter_result.reason, execution.pin,
                              execution.lease, adapter_result, execution.evaluations)

    def release_task(self, task_id: str, *, outcome: str) -> bool:
        at = self._now().astimezone(timezone.utc)
        lease = self.store.read_live_lease(task_id, now=at)
        return (
            self.store.release(lease, outcome=outcome, now=at)
            if lease is not None
            else False
        )
