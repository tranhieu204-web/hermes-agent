"""Pure eligibility and deterministic fleet selection policy."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from .types import (
    AdapterKind,
    Confidence,
    Freshness,
    LaneHealth,
    LaneEvaluation,
    LaneInputs,
    MeasurementKind,
    OverageState,
    ReasonCode,
    RoutePurpose,
    RouteDecision,
    TaskSpec,
)
from .parent_models import is_sonnet_model


SWITCH_DELTA = Decimal("20.000")
_CONFIDENCE_RANK = {
    Confidence.LOW: 0,
    Confidence.MEDIUM: 1,
    Confidence.HIGH: 2,
}


def evaluate_lane(
    inputs: LaneInputs,
    task: TaskSpec,
    *,
    now: datetime | None = None,
    minimum_confidence: Confidence = Confidence.HIGH,
    purpose: RoutePurpose = RoutePurpose.TASK_WORKER,
) -> LaneEvaluation:
    """Evaluate every gate without short-circuiting the reason matrix."""

    at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    profile = inputs.profile
    qualification = inputs.qualification
    reasons: list[ReasonCode] = []
    usage_reasons: list[ReasonCode] = []

    # 1. Basic eligibility.
    if not inputs.enabled:
        reasons.append(ReasonCode.LANE_DISABLED)
    if purpose is RoutePurpose.TASK_WORKER and not profile.supports_task_worker:
        reasons.append(ReasonCode.PURPOSE_UNSUPPORTED)
    if (
        purpose is RoutePurpose.DESKTOP_PARENT
        and not profile.supports_parent_session
    ):
        reasons.append(ReasonCode.PARENT_SESSION_UNSUPPORTED)
    if (
        purpose is RoutePurpose.DESKTOP_PARENT
        and profile.adapter_kind is AdapterKind.EXTERNAL_CLI
        and (
            qualification is None
            or not qualification.parent_session_proven
        )
    ):
        reasons.append(ReasonCode.PARENT_SESSION_UNPROVEN)
    if not profile.implemented:
        reasons.append(ReasonCode.ADAPTER_UNIMPLEMENTED)
    if not profile.platform_supported:
        reasons.append(ReasonCode.PLATFORM_UNSUPPORTED)
    if not inputs.adapter_found:
        reasons.append(ReasonCode.ADAPTER_NOT_FOUND)

    # 2. Subscription auth and billing policy.
    if qualification is None or qualification.auth_kind is None:
        reasons.append(ReasonCode.AUTH_MISSING)
    else:
        if qualification.auth_kind not in profile.allowed_auth_kinds:
            reasons.append(ReasonCode.AUTH_KIND_FORBIDDEN)
        if not qualification.auth_source:
            reasons.append(ReasonCode.AUTH_SOURCE_UNKNOWN)
        if not qualification.subscription_only_proven:
            reasons.append(ReasonCode.SUBSCRIPTION_NOT_PROVEN)
        if not qualification.paid_fallback_absent:
            reasons.append(ReasonCode.PAID_FALLBACK_PRESENT)
        if (
            qualification.overage_disabled is not True
            or qualification.overage_state is not OverageState.OFF
        ):
            reasons.append(ReasonCode.OVERAGE_STATUS_UNKNOWN_OR_ON)

    # 3. Qualification and model policy.
    selected_model: str | None = None
    selected_effort = profile.selected_effort
    if qualification is None or not qualification.qualified:
        reasons.append(ReasonCode.QUALIFICATION_FAILED)
    else:
        if qualification.expires_at.astimezone(timezone.utc) < at:
            reasons.append(ReasonCode.QUALIFICATION_STALE)
        if qualification.provider_id != profile.provider_id:
            reasons.append(ReasonCode.QUALIFICATION_FAILED)
        selected_model = next(
            (
                model
                for model in profile.ordered_models
                if model in qualification.models
            ),
            None,
        )
        if selected_model is None:
            reasons.append(ReasonCode.QUALIFICATION_FAILED)
        elif is_sonnet_model(selected_model):
            # Sonnet is catalog-only; never a fleet parent/worker admission target.
            selected_model = None
            reasons.append(ReasonCode.MODEL_NOT_ADMITTED)
        if (
            selected_effort is None
            or selected_effort not in qualification.efforts
            or len(tuple(dict.fromkeys(profile.supported_efforts))) < 2
        ):
            reasons.append(ReasonCode.EFFORT_POLICY_UNSATISFIED)
        if not profile.fast_off_verifiable or not qualification.fast_off_supported:
            reasons.append(ReasonCode.QUALIFICATION_FAILED)

    # 4. Capabilities must be proven by both profile and current evidence.
    qualified_capabilities = (
        qualification.capabilities if qualification is not None else frozenset()
    )
    if not task.required_capabilities.issubset(
        profile.capabilities & qualified_capabilities
    ):
        reasons.append(ReasonCode.CAPABILITY_MISMATCH)

    # 5. Occupancy.
    if inputs.active_leases >= inputs.max_concurrency:
        reasons.append(ReasonCode.OCCUPANCY_FULL)

    snapshot = inputs.capacity.snapshot
    health = inputs.capacity.health
    if (
        inputs.require_verified_health
        and purpose is RoutePurpose.TASK_WORKER
    ):
        if (
            health is None
            or health.freshness is not Freshness.FRESH
            or health.status is LaneHealth.UNKNOWN
        ):
            reasons.append(ReasonCode.HEALTH_UNVERIFIED)
        elif health.status is LaneHealth.DOWN:
            reasons.append(ReasonCode.HEALTH_DOWN)
    trustworthy_capacity = (
        snapshot is not None
        and snapshot.freshness is Freshness.FRESH
        and _CONFIDENCE_RANK[snapshot.confidence]
        >= _CONFIDENCE_RANK[minimum_confidence]
        and snapshot.measurement_kind is MeasurementKind.MEASURED
    )

    # 6. Protected reserve. Untrusted percentages never enter arithmetic.
    if trustworthy_capacity:
        if (
            snapshot.remaining_pct <= Decimal("0")  # type: ignore[union-attr]
            or snapshot.effective_remaining_pct <= Decimal("0")  # type: ignore[union-attr]
        ):
            reasons.append(ReasonCode.CAPACITY_EXHAUSTED)
        after_reservation = (
            snapshot.remaining_pct  # type: ignore[union-attr]
            - inputs.active_reserved_pct
            - task.reservation_pct
        )
        if after_reservation < inputs.reserve_floor_pct:
            reasons.append(ReasonCode.RESERVE_FLOOR)

    # 7. Capacity evidence is advisory unless it directly proves exhaustion or
    # a reserve-floor breach. Unknown/stale/non-comparable usage rotates.
    if snapshot is None:
        usage_reasons.append(
            inputs.capacity.reason or ReasonCode.CAPACITY_MISSING
        )
    else:
        if snapshot.freshness is not Freshness.FRESH:
            usage_reasons.append(ReasonCode.USAGE_STALE)
        if _CONFIDENCE_RANK[snapshot.confidence] < _CONFIDENCE_RANK[minimum_confidence]:
            usage_reasons.append(ReasonCode.USAGE_STALE)
        if (
            snapshot.measurement_kind is not MeasurementKind.MEASURED
            or not snapshot.comparability_group
            or not snapshot.quota_window_id
        ):
            usage_reasons.append(ReasonCode.USAGE_NOT_COMPARABLE)
    if (
        inputs.require_verified_health
        and purpose is RoutePurpose.TASK_WORKER
        and usage_reasons
    ):
        reasons.extend(usage_reasons)
        usage_reasons = []

    # 8. Cooldown.
    if inputs.cooldown_until is not None and inputs.cooldown_until > at:
        reasons.append(ReasonCode.LANE_COOLDOWN)

    # Preserve deterministic ordering while avoiding duplicate codes caused by
    # related auth/capacity evidence.
    hard_reasons = tuple(dict.fromkeys(reasons))
    advisory_reasons = tuple(dict.fromkeys(usage_reasons))
    eligible = not hard_reasons
    evaluation_reasons = (
        advisory_reasons
        if eligible and advisory_reasons
        else ((ReasonCode.MET,) if eligible else hard_reasons + advisory_reasons)
    )
    return LaneEvaluation(
        lane_id=profile.lane_id,
        profile=profile,
        capacity=snapshot,
        eligible=eligible,
        reasons=evaluation_reasons,
        selected_model=selected_model,
        selected_effort=selected_effort,
        fallback_eligible=False,
        qualification_evidence_id=(
            qualification.evidence_id if qualification is not None else ""
        ),
        qualification_detail=(
            qualification.detail if qualification is not None else ""
        ),
    )


def select_lane(
    evaluations: tuple[LaneEvaluation, ...],
    *,
    rotation_index: int = 0,
    switch_delta: Decimal = SWITCH_DELTA,
    selected_lane_id: str | None = None,
) -> RouteDecision:
    """Validate an external ranking choice or rotate without ranking.

    Live usage ranking belongs to ``gateway.fleet_safety.selector``. This
    function owns only the hard-gate boundary and deterministic rotation used
    by non-live callers.
    """

    ordered = tuple(sorted(evaluations, key=lambda item: item.profile.order))
    pool = tuple(item for item in ordered if item.eligible)
    if not pool:
        return RouteDecision(
            lane_id=None,
            reason=ReasonCode.NO_ELIGIBLE_LANE,
            evaluations=ordered,
            rotation_index=rotation_index,
        )

    chosen_pos = rotation_index % len(pool)
    cyclic = pool[chosen_pos]
    if selected_lane_id is None:
        chosen = cyclic
    else:
        chosen = next(
            (item for item in pool if item.lane_id == selected_lane_id),
            None,
        )
        if chosen is None:
            return RouteDecision(
                lane_id=None,
                reason=ReasonCode.NO_ELIGIBLE_LANE,
                evaluations=ordered,
                rotation_index=rotation_index,
            )

    return RouteDecision(
        lane_id=chosen.lane_id,
        reason=(
            ReasonCode.BALANCE_THRESHOLD
            if chosen is not cyclic
            else ReasonCode.ROTATION
        ),
        evaluations=ordered,
        rotation_index=(chosen_pos + 1) % len(pool),
        switch_applied=chosen is not cyclic,
    )
