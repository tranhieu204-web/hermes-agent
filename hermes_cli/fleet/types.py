"""Immutable fleet router contracts.

These objects deliberately contain normalized metadata only. Prompts, child
output, credentials, and inherited environments do not belong in fleet state
or audit payloads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, FrozenSet, Mapping, Sequence


class ReasonCode(str, Enum):
    MET = "MET"
    ROTATION = "ROTATION"
    BALANCE_THRESHOLD = "BALANCE_THRESHOLD"
    MANUAL_OVERRIDE = "MANUAL_OVERRIDE"
    LANE_DISABLED = "LANE_DISABLED"
    PURPOSE_UNSUPPORTED = "PURPOSE_UNSUPPORTED"
    PARENT_SESSION_UNSUPPORTED = "PARENT_SESSION_UNSUPPORTED"
    PARENT_SESSION_UNPROVEN = "PARENT_SESSION_UNPROVEN"
    ADAPTER_UNIMPLEMENTED = "ADAPTER_UNIMPLEMENTED"
    PLATFORM_UNSUPPORTED = "PLATFORM_UNSUPPORTED"
    ADAPTER_NOT_FOUND = "ADAPTER_NOT_FOUND"
    AUTH_MISSING = "AUTH_MISSING"
    AUTH_KIND_FORBIDDEN = "AUTH_KIND_FORBIDDEN"
    AUTH_SOURCE_UNKNOWN = "AUTH_SOURCE_UNKNOWN"
    SUBSCRIPTION_NOT_PROVEN = "SUBSCRIPTION_NOT_PROVEN"
    PAID_FALLBACK_PRESENT = "PAID_FALLBACK_PRESENT"
    OVERAGE_STATUS_UNKNOWN_OR_ON = "OVERAGE_STATUS_UNKNOWN_OR_ON"
    QUALIFICATION_FAILED = "QUALIFICATION_FAILED"
    QUALIFICATION_STALE = "QUALIFICATION_STALE"
    EFFORT_POLICY_UNSATISFIED = "EFFORT_POLICY_UNSATISFIED"
    CAPABILITY_MISMATCH = "CAPABILITY_MISMATCH"
    OCCUPANCY_FULL = "OCCUPANCY_FULL"
    RESERVE_FLOOR = "RESERVE_FLOOR"
    CAPACITY_MISSING = "CAPACITY_MISSING"
    CAPACITY_INVALID = "CAPACITY_INVALID"
    CAPACITY_STALE = "CAPACITY_STALE"
    CAPACITY_CONFIDENCE_LOW = "CAPACITY_CONFIDENCE_LOW"
    CAPACITY_EXHAUSTED = "CAPACITY_EXHAUSTED"
    USAGE_STALE = "USAGE_STALE"
    USAGE_NOT_COMPARABLE = "USAGE_NOT_COMPARABLE"
    HEALTH_DOWN = "HEALTH_DOWN"
    HEALTH_UNVERIFIED = "HEALTH_UNVERIFIED"
    ROTATION_WITHOUT_FRESH_CAPACITY = "ROTATION_WITHOUT_FRESH_CAPACITY"
    LANE_COOLDOWN = "LANE_COOLDOWN"
    NO_ELIGIBLE_LANE = "NO_ELIGIBLE_LANE"
    PINNED_LANE_UNAVAILABLE = "PINNED_LANE_UNAVAILABLE"
    FLEET_DISABLED = "FLEET_DISABLED"
    PROVIDER_MISMATCH = "PROVIDER_MISMATCH"
    MODEL_MISMATCH = "MODEL_MISMATCH"
    MODEL_NOT_ADMITTED = "MODEL_NOT_ADMITTED"
    CREDENTIAL_MISMATCH = "CREDENTIAL_MISMATCH"
    MALFORMED_OUTPUT = "MALFORMED_OUTPUT"
    EXECUTION_TIMEOUT = "EXECUTION_TIMEOUT"
    EXECUTION_CANCELLED = "EXECUTION_CANCELLED"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    TASK_NOT_FOUND = "TASK_NOT_FOUND"
    LEASE_CONFLICT = "LEASE_CONFLICT"
    RELEASED = "RELEASED"


class AdapterKind(str, Enum):
    NATIVE_PROVIDER = "native_provider"
    EXTERNAL_CLI = "external_cli"


class RoutePurpose(str, Enum):
    TASK_WORKER = "task_worker"
    DESKTOP_PARENT = "desktop_parent"


class Freshness(str, Enum):
    FRESH = "fresh"
    STALE = "stale"
    INVALID = "invalid"


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class MeasurementKind(str, Enum):
    MEASURED = "measured"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"


class OverageState(str, Enum):
    OFF = "off"
    ON = "on"
    UNKNOWN = "unknown"


class LaneHealth(str, Enum):
    UP = "UP"
    DOWN = "DOWN"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class HealthRead:
    status: LaneHealth
    captured_at: datetime
    read_at: datetime
    expires_at: datetime
    freshness: Freshness
    source_id: str
    detail: str = ""


@dataclass(frozen=True)
class CapacitySnapshot:
    lane_id: str
    used_pct: Decimal
    remaining_pct: Decimal
    reserved_pct: Decimal
    effective_remaining_pct: Decimal
    source_kind: str
    source_id: str
    captured_at: datetime
    read_at: datetime
    expires_at: datetime
    freshness: Freshness
    confidence: Confidence
    schema_version: str
    overage_disabled: bool | None
    comparability_group: str | None = None
    quota_window_id: str | None = None
    measurement_kind: MeasurementKind = MeasurementKind.UNKNOWN


@dataclass(frozen=True)
class CapacityRead:
    snapshot: CapacitySnapshot | None
    reason: ReasonCode | None
    detail: str = ""
    health: HealthRead | None = None


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    cwd: Path
    required_capabilities: FrozenSet[str] = frozenset()
    reservation_pct: Decimal = Decimal("1.000")
    prompt_fingerprint: str = ""


@dataclass(frozen=True)
class LaneProfile:
    lane_id: str
    order: int
    adapter_kind: AdapterKind
    provider_id: str
    ordered_models: tuple[str, ...]
    supported_efforts: tuple[str, ...]
    capabilities: FrozenSet[str]
    allowed_auth_kinds: FrozenSet[str]
    implemented: bool = True
    platform_supported: bool = True
    fast_mode_supported: bool = False
    fast_off_verifiable: bool = True
    executable: str | None = None
    supports_task_worker: bool = True
    supports_parent_session: bool = False

    @property
    def selected_model(self) -> str | None:
        return self.ordered_models[0] if self.ordered_models else None

    @property
    def selected_effort(self) -> str | None:
        distinct = tuple(dict.fromkeys(self.supported_efforts))
        return distinct[-2] if len(distinct) >= 2 else None


@dataclass(frozen=True)
class Qualification:
    qualified: bool
    captured_at: datetime
    expires_at: datetime
    auth_kind: str | None
    auth_source: str | None
    overage_disabled: bool | None
    provider_id: str
    models: tuple[str, ...]
    efforts: tuple[str, ...]
    fast_off_supported: bool
    capabilities: FrozenSet[str]
    executable: str | None = None
    version: str | None = None
    evidence_id: str = ""
    detail: str = ""
    subscription_only_proven: bool = False
    paid_fallback_absent: bool = False
    overage_state: OverageState = OverageState.UNKNOWN
    parent_session_proven: bool = False


@dataclass(frozen=True)
class LaneInputs:
    profile: LaneProfile
    enabled: bool
    adapter_found: bool
    qualification: Qualification | None
    capacity: CapacityRead
    active_leases: int = 0
    active_reserved_pct: Decimal = Decimal("0")
    max_concurrency: int = 1
    reserve_floor_pct: Decimal = Decimal("0")
    cooldown_until: datetime | None = None
    rotation_without_fresh_capacity: bool = False
    require_verified_health: bool = False


@dataclass(frozen=True)
class LaneEvaluation:
    lane_id: str
    profile: LaneProfile
    capacity: CapacitySnapshot | None
    eligible: bool
    reasons: tuple[ReasonCode, ...]
    selected_model: str | None
    selected_effort: str | None
    fallback_eligible: bool = False
    qualification_evidence_id: str = ""
    qualification_detail: str = ""


@dataclass(frozen=True)
class RouteDecision:
    lane_id: str | None
    reason: ReasonCode
    evaluations: tuple[LaneEvaluation, ...]
    rotation_index: int
    switch_applied: bool = False


@dataclass(frozen=True)
class TaskPin:
    task_id: str
    lane_id: str
    adapter_kind: AdapterKind
    provider_id: str
    model_id: str
    effort: str
    fast_mode: bool
    cwd_fingerprint: str
    status: str


@dataclass(frozen=True)
class ParentPin:
    profile_id: str
    lineage_root_id: str
    session_id: str
    purpose: RoutePurpose
    lane_id: str
    adapter_kind: AdapterKind
    provider_id: str
    model_id: str
    effort: str
    fast_mode: bool
    qualification_evidence_hash: str
    route_identity: str
    selection_reason: ReasonCode
    status: str


@dataclass(frozen=True)
class LeaseHandle:
    task_id: str
    lane_id: str
    owner_uuid: str
    generation: int
    reserved_pct: Decimal
    expires_at: datetime


@dataclass(frozen=True)
class ParentLeaseHandle:
    profile_id: str
    lineage_root_id: str
    lane_id: str
    owner_uuid: str
    generation: int
    reserved_pct: Decimal
    expires_at: datetime


@dataclass(frozen=True)
class ParentAdmission:
    reason: ReasonCode
    pin: ParentPin | None
    evaluations: tuple[LaneEvaluation, ...] = ()


@dataclass(frozen=True)
class ParentTurnAcquisition:
    reason: ReasonCode
    pin: ParentPin | None
    lease: ParentLeaseHandle | None
    evaluations: tuple[LaneEvaluation, ...] = ()


@dataclass(frozen=True)
class AdapterRequest:
    task_id: str
    cwd: Path
    prompt: str
    profile: LaneProfile
    model: str
    effort: str
    fast_mode: bool = False
    timeout_seconds: int = 1800


@dataclass(frozen=True)
class AdapterResult:
    ok: bool
    reason: ReasonCode
    provider_id: str
    model_id: str
    auth_kind: str
    adapter_kind: AdapterKind
    output: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FleetRunResult:
    ok: bool
    task_id: str
    reason: ReasonCode
    pin: TaskPin | None
    lease: LeaseHandle | None
    adapter_result: AdapterResult | None
    evaluations: Sequence[LaneEvaluation] = ()
