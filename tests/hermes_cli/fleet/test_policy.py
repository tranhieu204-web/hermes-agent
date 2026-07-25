from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from hermes_cli.fleet.policy import evaluate_lane, select_lane
from hermes_cli.fleet.types import (
    AdapterKind,
    CapacityRead,
    CapacitySnapshot,
    Confidence,
    Freshness,
    LaneInputs,
    LaneProfile,
    MeasurementKind,
    OverageState,
    Qualification,
    ReasonCode,
    RoutePurpose,
    TaskSpec,
)


NOW = datetime(2026, 7, 24, tzinfo=timezone.utc)
TASK = TaskSpec(
    task_id="task-1",
    cwd=Path("."),
    required_capabilities=frozenset({"workspace_write", "shell"}),
    reservation_pct=Decimal("5.000"),
)


def _profile(lane_id: str = "chatgpt_codex", order: int = 0) -> LaneProfile:
    return LaneProfile(
        lane_id=lane_id,
        order=order,
        adapter_kind=AdapterKind.NATIVE_PROVIDER,
        provider_id=f"{lane_id}-provider",
        ordered_models=("m1", "m2", "m3"),
        supported_efforts=("low", "medium", "high", "max"),
        capabilities=frozenset({"workspace_write", "shell", "vision"}),
        allowed_auth_kinds=frozenset({"oauth_subscription"}),
    )


def _qualification(profile: LaneProfile) -> Qualification:
    return Qualification(
        qualified=True,
        captured_at=NOW - timedelta(minutes=5),
        expires_at=NOW + timedelta(hours=1),
        auth_kind="oauth_subscription",
        auth_source=f"{profile.lane_id}:subscription",
        overage_disabled=True,
        provider_id=profile.provider_id,
        models=profile.ordered_models,
        efforts=profile.supported_efforts,
        fast_off_supported=True,
        capabilities=profile.capabilities,
        evidence_id=f"qualification:{profile.lane_id}",
        subscription_only_proven=True,
        paid_fallback_absent=True,
        overage_state=OverageState.OFF,
    )


def _capacity(lane_id: str, remaining: str = "60.000") -> CapacityRead:
    rem = Decimal(remaining)
    return CapacityRead(
        CapacitySnapshot(
            lane_id=lane_id,
            used_pct=(Decimal("100.000") - rem),
            remaining_pct=rem,
            reserved_pct=Decimal("0"),
            effective_remaining_pct=rem,
            source_kind="bridge_file",
            source_id=f"bridge:{lane_id}#hash",
            captured_at=NOW - timedelta(minutes=5),
            read_at=NOW,
            expires_at=NOW + timedelta(hours=1),
            freshness=Freshness.FRESH,
            confidence=Confidence.HIGH,
            schema_version="1",
            overage_disabled=True,
            comparability_group="subscription-weekly",
            quota_window_id="2026-W30",
            measurement_kind=MeasurementKind.MEASURED,
        ),
        None,
    )


def _inputs(profile: LaneProfile | None = None, remaining: str = "60.000"):
    profile = profile or _profile()
    return LaneInputs(
        profile=profile,
        enabled=True,
        adapter_found=True,
        qualification=_qualification(profile),
        capacity=_capacity(profile.lane_id, remaining),
        max_concurrency=1,
        reserve_floor_pct=Decimal("10.000"),
    )


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda i: replace(i, enabled=False), ReasonCode.LANE_DISABLED),
        (
            lambda i: replace(i, profile=replace(i.profile, implemented=False)),
            ReasonCode.ADAPTER_UNIMPLEMENTED,
        ),
        (
            lambda i: replace(
                i, profile=replace(i.profile, platform_supported=False)
            ),
            ReasonCode.PLATFORM_UNSUPPORTED,
        ),
        (lambda i: replace(i, adapter_found=False), ReasonCode.ADAPTER_NOT_FOUND),
        (lambda i: replace(i, qualification=None), ReasonCode.AUTH_MISSING),
        (
            lambda i: replace(
                i,
                qualification=replace(
                    i.qualification, auth_kind="api_key"  # type: ignore[arg-type]
                ),
            ),
            ReasonCode.AUTH_KIND_FORBIDDEN,
        ),
        (
            lambda i: replace(
                i,
                qualification=replace(
                    i.qualification, auth_source=None  # type: ignore[arg-type]
                ),
            ),
            ReasonCode.AUTH_SOURCE_UNKNOWN,
        ),
        (
            lambda i: replace(
                i,
                qualification=replace(
                    i.qualification, overage_disabled=None  # type: ignore[arg-type]
                ),
            ),
            ReasonCode.OVERAGE_STATUS_UNKNOWN_OR_ON,
        ),
        (
            lambda i: replace(
                i,
                qualification=replace(
                    i.qualification, qualified=False  # type: ignore[arg-type]
                ),
            ),
            ReasonCode.QUALIFICATION_FAILED,
        ),
        (
            lambda i: replace(
                i,
                qualification=replace(
                    i.qualification,
                    expires_at=NOW - timedelta(seconds=1),  # type: ignore[arg-type]
                ),
            ),
            ReasonCode.QUALIFICATION_STALE,
        ),
        (
            lambda i: replace(
                i, profile=replace(i.profile, supported_efforts=("max",))
            ),
            ReasonCode.EFFORT_POLICY_UNSATISFIED,
        ),
        (
            lambda i: replace(
                i,
                qualification=replace(
                    i.qualification,
                    capabilities=frozenset({"workspace_write"}),  # type: ignore[arg-type]
                ),
            ),
            ReasonCode.CAPABILITY_MISMATCH,
        ),
        (lambda i: replace(i, active_leases=1), ReasonCode.OCCUPANCY_FULL),
        (
            lambda i: replace(
                i,
                active_reserved_pct=Decimal("46"),
            ),
            ReasonCode.RESERVE_FLOOR,
        ),
        (
            lambda i: replace(i, cooldown_until=NOW + timedelta(minutes=1)),
            ReasonCode.LANE_COOLDOWN,
        ),
    ],
)
def test_each_gate_fails_closed_and_preserves_a_reason_matrix(mutate, reason):
    evaluation = evaluate_lane(mutate(_inputs()), TASK, now=NOW)

    assert not evaluation.eligible
    assert reason in evaluation.reasons


def test_model_policy_uses_strongest_model_second_highest_effort_and_fast_off():
    evaluation = evaluate_lane(_inputs(), TASK, now=NOW)

    assert evaluation.eligible
    assert evaluation.reasons == (ReasonCode.MET,)
    assert evaluation.selected_model == "m1"
    assert evaluation.selected_effort == "high"


def test_desktop_parent_excludes_external_worker_without_parent_session_support():
    profile = replace(
        _profile("antigravity"),
        adapter_kind=AdapterKind.EXTERNAL_CLI,
        supports_parent_session=False,
    )

    evaluation = evaluate_lane(
        _inputs(profile),
        TASK,
        now=NOW,
        purpose=RoutePurpose.DESKTOP_PARENT,
    )

    assert not evaluation.eligible
    assert ReasonCode.PARENT_SESSION_UNSUPPORTED in evaluation.reasons


def test_external_parent_requires_separate_persistent_session_proof():
    profile = replace(
        _profile("antigravity"),
        adapter_kind=AdapterKind.EXTERNAL_CLI,
        supports_parent_session=True,
    )
    inputs = _inputs(profile)

    evaluation = evaluate_lane(
        inputs,
        TASK,
        now=NOW,
        purpose=RoutePurpose.DESKTOP_PARENT,
    )

    assert not evaluation.eligible
    assert ReasonCode.PARENT_SESSION_UNPROVEN in evaluation.reasons


@pytest.mark.parametrize(
    ("lane_id", "provider_id"),
    [
        ("chatgpt_codex", "openai-codex"),
        ("claude_code", "anthropic"),
        ("grok", "xai-oauth"),
    ],
)
def test_native_parent_requires_exact_subscription_qualification(
    lane_id, provider_id
):
    profile = replace(
        _profile(lane_id),
        provider_id=provider_id,
        supports_parent_session=True,
    )
    inputs = _inputs(profile)

    qualified = evaluate_lane(
        inputs,
        TASK,
        now=NOW,
        purpose=RoutePurpose.DESKTOP_PARENT,
    )
    unproven = evaluate_lane(
        replace(
            inputs,
            qualification=replace(
                inputs.qualification,  # type: ignore[arg-type]
                subscription_only_proven=False,
            ),
        ),
        TASK,
        now=NOW,
        purpose=RoutePurpose.DESKTOP_PARENT,
    )

    assert qualified.eligible
    assert not unproven.eligible
    assert ReasonCode.SUBSCRIPTION_NOT_PROVEN in unproven.reasons


def test_builtin_claude_lane_is_a_native_parent_not_a_cli_worker():
    from hermes_cli.fleet.profiles import profile_map

    profile = profile_map()["claude_code"]

    assert profile.adapter_kind is AdapterKind.NATIVE_PROVIDER
    assert profile.provider_id == "anthropic"
    assert profile.allowed_auth_kinds == frozenset({"oauth_subscription"})
    assert profile.supports_parent_session is True


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"subscription_only_proven": False}, ReasonCode.SUBSCRIPTION_NOT_PROVEN),
        ({"paid_fallback_absent": False}, ReasonCode.PAID_FALLBACK_PRESENT),
        ({"overage_state": OverageState.UNKNOWN}, ReasonCode.OVERAGE_STATUS_UNKNOWN_OR_ON),
        ({"overage_state": OverageState.ON}, ReasonCode.OVERAGE_STATUS_UNKNOWN_OR_ON),
    ],
)
def test_billing_safety_requires_observed_evidence_not_policy_text(changes, reason):
    inputs = _inputs()
    qualification = replace(inputs.qualification, **changes)  # type: ignore[arg-type]

    evaluation = evaluate_lane(
        replace(inputs, qualification=qualification),
        TASK,
        now=NOW,
    )

    assert not evaluation.eligible
    assert reason in evaluation.reasons


def test_external_ranking_choice_is_validated_without_policy_reranking():
    priority = _inputs(_profile("chatgpt_codex", 0), "60.000")
    alternative = _inputs(_profile("claude_code", 1), "80.000")
    evaluations = tuple(
        evaluate_lane(item, TASK, now=NOW) for item in (priority, alternative)
    )

    decision = select_lane(
        evaluations,
        rotation_index=0,
        selected_lane_id="claude_code",
    )

    assert decision.lane_id == "claude_code"
    assert decision.switch_applied
    assert decision.reason is ReasonCode.BALANCE_THRESHOLD


def test_non_comparable_usage_never_overrides_deterministic_rotation():
    priority = _inputs(_profile("chatgpt_codex", 0), "60.000")
    alternative = _inputs(_profile("grok", 1), "100.000")
    alternative = replace(
        alternative,
        capacity=replace(
            alternative.capacity,
            snapshot=replace(
                alternative.capacity.snapshot,  # type: ignore[arg-type]
                comparability_group="different-plan",
            ),
        ),
    )
    evaluations = tuple(
        evaluate_lane(item, TASK, now=NOW) for item in (priority, alternative)
    )

    decision = select_lane(evaluations, rotation_index=0)

    assert decision.lane_id == "chatgpt_codex"
    assert decision.reason is ReasonCode.ROTATION
    assert decision.evaluations == evaluations


def test_exact_top_capacity_ties_rotate_in_fixed_order_without_hidden_mutation():
    evaluations = tuple(
        evaluate_lane(
            _inputs(_profile(lane_id, order), "90.000"), TASK, now=NOW
        )
        for order, lane_id in enumerate(
            ("chatgpt_codex", "claude_code", "grok")
        )
    )

    first = select_lane(evaluations, rotation_index=0)
    same_dry_run = select_lane(evaluations, rotation_index=0)
    second = select_lane(evaluations, rotation_index=first.rotation_index)

    assert first.lane_id == "chatgpt_codex"
    assert same_dry_run == first
    assert second.lane_id == "claude_code"


def test_stale_capacity_rotates_without_preempting_or_blocking():
    candidates = (
        _inputs(_profile("chatgpt_codex", 0), "84.000"),
        _inputs(_profile("claude_code", 1), "11.000"),
        replace(
            _inputs(_profile("grok", 2), "99.000"),
            capacity=replace(
                _capacity("grok", "99.000"),
                snapshot=replace(
                    _capacity("grok", "99.000").snapshot,
                    freshness=Freshness.STALE,
                    confidence=Confidence.LOW,
                ),
                reason=ReasonCode.CAPACITY_STALE,
            ),
        ),
        replace(
            _inputs(_profile("antigravity", 3), "100.000"),
            capacity=replace(
                _capacity("antigravity", "100.000"),
                snapshot=replace(
                    _capacity("antigravity", "100.000").snapshot,
                    freshness=Freshness.STALE,
                    confidence=Confidence.LOW,
                ),
                reason=ReasonCode.CAPACITY_STALE,
            ),
        ),
    )
    evaluations = tuple(
        evaluate_lane(candidate, TASK, now=NOW) for candidate in candidates
    )

    claude, grok, agy = evaluations[1:]
    assert not claude.eligible
    assert ReasonCode.RESERVE_FLOOR in claude.reasons
    assert grok.eligible and not grok.fallback_eligible
    assert agy.eligible and not agy.fallback_eligible
    assert ReasonCode.USAGE_STALE in grok.reasons
    assert ReasonCode.MET not in grok.reasons

    first = select_lane(evaluations, rotation_index=0)
    grok_turn = select_lane(evaluations, rotation_index=1)
    agy_turn = select_lane(evaluations, rotation_index=2)

    assert (first.lane_id, first.rotation_index) == ("chatgpt_codex", 1)
    assert (grok_turn.lane_id, grok_turn.rotation_index) == ("grok", 2)
    assert grok_turn.reason is ReasonCode.ROTATION
    assert (agy_turn.lane_id, agy_turn.rotation_index) == ("antigravity", 0)
    assert agy_turn.reason is ReasonCode.ROTATION


def test_stale_capacity_never_participates_in_reserve_arithmetic():
    stale = replace(
        _inputs(_profile("grok", 0), "1.000"),
        capacity=replace(
            _capacity("grok", "1.000"),
            snapshot=replace(
                _capacity("grok", "1.000").snapshot,
                freshness=Freshness.STALE,
                confidence=Confidence.LOW,
            ),
            reason=ReasonCode.CAPACITY_STALE,
        ),
    )

    evaluation = evaluate_lane(stale, TASK, now=NOW)

    assert evaluation.eligible
    assert ReasonCode.RESERVE_FLOOR not in evaluation.reasons
    assert ReasonCode.USAGE_STALE in evaluation.reasons


def test_missing_capacity_uses_rotation_instead_of_no_route():
    missing = replace(
        _inputs(_profile("chatgpt_codex", 0)),
        capacity=CapacityRead(None, ReasonCode.CAPACITY_MISSING),
    )
    evaluation = evaluate_lane(missing, TASK, now=NOW)

    decision = select_lane((evaluation,), rotation_index=0)

    assert evaluation.eligible
    assert ReasonCode.CAPACITY_MISSING in evaluation.reasons
    assert decision.lane_id == "chatgpt_codex"
    assert decision.reason is ReasonCode.ROTATION


def test_explicit_exhaustion_still_blocks_the_lane():
    exhausted = _inputs(_profile("chatgpt_codex", 0), "0.000")

    evaluation = evaluate_lane(exhausted, TASK, now=NOW)

    assert not evaluation.eligible
    assert ReasonCode.CAPACITY_EXHAUSTED in evaluation.reasons


def test_no_eligible_lane_returns_complete_evaluations():
    items = (
        replace(_inputs(_profile("chatgpt_codex", 0)), enabled=False),
        replace(_inputs(_profile("claude_code", 1)), adapter_found=False),
    )
    evaluations = tuple(evaluate_lane(item, TASK, now=NOW) for item in items)

    decision = select_lane(evaluations)

    assert decision.lane_id is None
    assert decision.reason is ReasonCode.NO_ELIGIBLE_LANE
    assert {item.lane_id for item in decision.evaluations} == {
        "chatgpt_codex",
        "claude_code",
    }
