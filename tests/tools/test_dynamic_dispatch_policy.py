"""Behavior contracts for the inert Sakaan/Hermes dispatch policy."""

import json
import subprocess
import sys

import pytest

from tools.dynamic_dispatch_policy import (
    AGY_CANONICAL_MODEL_LABELS,
    DEFAULT_ROUTES,
    DecisionStatus,
    DispatchRequest,
    ProviderQuotaSnapshot,
    Role,
    RouteAvailabilitySnapshot,
    TelemetryState,
    WorkerRoute,
    dry_run,
    evaluate_dispatch,
)

NOW = "2026-08-05T12:00:00Z"


def route(role=Role.BUILDER, *, route_id="primary", profile="profile-a", provider="provider-a", model="model-high", effort="high", rank=100, fallbacks=()):
    return WorkerRoute(route_id, role, profile, provider, model, effort, rank, "workers", fallbacks)


def available(target, *, captured_at=NOW, state=TelemetryState.KNOWN, value=True):
    return RouteAvailabilitySnapshot(target.profile, target.provider, target.model, value, captured_at, "fixture", state, "high")


def quota(target, *, limit=100, used=60, remaining=40, captured_at=NOW, state=TelemetryState.KNOWN):
    return ProviderQuotaSnapshot(target.provider, target.quota_scope, limit, used, remaining, captured_at, "fixture", state, "high")


def agy_route(
    *,
    role=Role.BUILDER,
    profile="agy-builder",
    effort="high",
    selector="Gemini 3.1 Pro (High)",
    route_log=None,
    governance_status="ACTIVE",
):
    if route_log is None:
        route_log = 'Propagating selected model override to backend: label="Gemini 3.1 Pro (High)"'
    return WorkerRoute(
        "agy.gemini-3.1-pro-high.v1",
        role,
        profile,
        "agy",
        "gemini-3.1-pro-high",
        effort,
        500,
        "gemini",
        agy_selector=selector,
        route_log=route_log,
        governance_status=governance_status,
    )


def decide(request, routes, availability=(), quotas=(), **kwargs):
    return evaluate_dispatch(request, routes, availability, quotas, as_of=NOW, **kwargs)


def test_default_policy_declares_one_complete_stable_route_per_worker_role():
    assert {item.role for item in DEFAULT_ROUTES} == set(Role)
    assert len({item.route_id for item in DEFAULT_ROUTES}) == len(DEFAULT_ROUTES)
    for item in DEFAULT_ROUTES:
        assert item.route_id.startswith("sakaan.") and item.route_id.endswith(".v1")
        assert all((item.profile, item.provider, item.model, item.reasoning_effort))


def test_highest_route_is_selected_and_effort_is_exact():
    high = route(route_id="high", rank=20)
    low = route(route_id="low", model="model-low", rank=10)
    request = DispatchRequest("builder", "profile-a", "high")

    decision = decide(request, (low, high), (available(high), available(low)))
    assert decision.status is DecisionStatus.ALLOW
    assert decision.selected_route == high

    mismatch = decide(DispatchRequest("builder", "profile-a", "medium"), (high,), (available(high),))
    assert mismatch.status is DecisionStatus.HOLD
    assert mismatch.reasons[0].code == "NO_ROUTE_AT_REQUESTED_EFFORT"


def test_unavailable_highest_route_does_not_silently_downgrade():
    high = route(route_id="high", rank=20)
    low = route(route_id="low", model="model-low", rank=10)
    decision = decide(
        DispatchRequest("builder", "profile-a", "high"),
        (high, low),
        (available(high, value=False), available(low)),
    )
    assert decision.status is DecisionStatus.HOLD
    assert decision.selected_route is None
    assert decision.reasons[0].code == "ROUTE_UNAVAILABLE"
    assert decision.reasons[0].route_id == "high"
    assert decision.candidate_attempts[0].route == high


def test_only_declared_matching_fallback_can_be_used():
    fallback = route(route_id="fallback", model="model-peer", rank=10)
    primary = route(route_id="primary", rank=20, fallbacks=("fallback",))
    decision = decide(
        DispatchRequest("builder", "profile-a", "high"),
        (primary, fallback),
        (available(primary, value=False), available(fallback)),
    )
    assert decision.status is DecisionStatus.ALLOW
    assert decision.selected_route == fallback
    assert decision.used_fallback is True


def test_failed_primary_quota_and_failed_fallback_availability_keep_attribution():
    fallback = route(Role.ROUTINE_CODER, route_id="fallback", provider="provider-b", model="model-peer", effort="medium")
    primary = route(Role.ROUTINE_CODER, route_id="primary", effort="medium", fallbacks=("fallback",))
    decision = decide(
        DispatchRequest("routine_coder", "profile-a", "medium"),
        (primary, fallback),
        (available(primary), available(fallback, value=False)),
        (quota(primary, used=80, remaining=20),),
    )

    assert decision.status is DecisionStatus.HOLD
    assert decision.selected_route is None
    assert [(attempt.route.route_id, attempt.reasons[0].code) for attempt in decision.candidate_attempts] == [
        ("primary", "ROUTINE_CODER_HEADROOM_BELOW_30_PERCENT"),
        ("fallback", "ROUTE_UNAVAILABLE"),
    ]
    assert [(reason.route_id, reason.code) for reason in decision.reasons] == [
        ("primary", "ROUTINE_CODER_HEADROOM_BELOW_30_PERCENT"),
        ("fallback", "ROUTE_UNAVAILABLE"),
    ]


def test_role_and_profile_are_separate_and_preserved_in_hold_receipt():
    target = route(role=Role.REVIEWER, profile="review-profile")
    request = DispatchRequest("reviewer", "builder-profile", "high", "primary")
    decision = decide(request, (target,), (available(target),))
    receipt = decision.to_dict()
    assert decision.status is DecisionStatus.HOLD
    assert decision.reasons[0].code == "ROLE_PROFILE_MISMATCH"
    assert receipt["requested_role"] == "reviewer"
    assert receipt["requested_profile"] == "builder-profile"
    assert receipt["selected_route"]["profile"] == "review-profile"


def test_unknown_role_cannot_smuggle_a_provider_override():
    target = route()
    decision = decide(DispatchRequest("builder:provider-x/model-x", "profile-a", "high"), (target,), (available(target),))
    assert decision.status is DecisionStatus.HOLD
    assert decision.reasons[0].code == "INVALID_ROLE"


def test_route_requires_fresh_known_positive_availability():
    target = route()
    request = DispatchRequest("builder", "profile-a", "high")
    cases = [
        ((), "ROUTE_TELEMETRY_MISSING"),
        ((available(target, state=TelemetryState.UNKNOWN, value=None),), "ROUTE_AVAILABILITY_UNKNOWN"),
        ((available(target, captured_at="2026-08-05T11:00:00Z"),), "ROUTE_TELEMETRY_STALE"),
        ((available(target, captured_at="not-a-time"),), "ROUTE_TELEMETRY_INVALID"),
        ((available(target, captured_at="2026-08-05T13:00:00Z"),), "ROUTE_TELEMETRY_FUTURE"),
    ]
    for snapshots, code in cases:
        decision = decide(request, (target,), snapshots)
        assert decision.status is DecisionStatus.HOLD
        assert decision.reasons[0].code == code


def test_undateable_negative_availability_cannot_be_hidden_by_valid_positive_snapshot():
    target = route()
    request = DispatchRequest("builder", "profile-a", "high")
    corrupt_negative = available(target, captured_at="not-a-time", value=False)
    valid_positive = available(target, captured_at="2026-08-05T11:59:00Z")

    for snapshots in ((valid_positive, corrupt_negative), (corrupt_negative, valid_positive)):
        decision = decide(request, (target,), snapshots)
        assert decision.status is DecisionStatus.HOLD
        assert decision.reasons[0].code == "ROUTE_TELEMETRY_INVALID"
        assert "captured_at" in decision.reasons[0].message
        assert decision.reasons[0].route_id == target.route_id


def test_routine_coder_requires_fresh_known_30_percent_headroom():
    target = route(Role.ROUTINE_CODER, effort="medium")
    request = DispatchRequest("routine_coder", "profile-a", "medium")

    allowed = decide(request, (target,), (available(target),), (quota(target, used=70, remaining=30),))
    assert allowed.status is DecisionStatus.ALLOW
    assert allowed.quota_headroom == 0.30

    low = decide(request, (target,), (available(target),), (quota(target, used=71, remaining=29),))
    assert low.status is DecisionStatus.HOLD
    assert low.quota_headroom is None
    assert low.candidate_attempts[0].quota_headroom == 0.29
    assert low.reasons[0].code == "ROUTINE_CODER_HEADROOM_BELOW_30_PERCENT"


def test_routine_quota_missing_unknown_stale_and_invalid_are_loud_holds():
    target = route(Role.ROUTINE_CODER, effort="medium")
    request = DispatchRequest("routine_coder", "profile-a", "medium")
    cases = [
        ((), "QUOTA_TELEMETRY_MISSING"),
        ((quota(target, limit=None, used=None, remaining=None, state=TelemetryState.UNKNOWN),), "QUOTA_TELEMETRY_UNKNOWN"),
        ((quota(target, captured_at="2026-08-05T11:00:00Z"),), "QUOTA_TELEMETRY_STALE"),
        ((quota(target, used=20, remaining=20),), "QUOTA_TELEMETRY_INVALID"),
        ((quota(target, limit=0, used=0, remaining=0),), "QUOTA_TELEMETRY_INVALID"),
    ]
    for snapshots, code in cases:
        decision = decide(request, (target,), (available(target),), snapshots)
        assert decision.status is DecisionStatus.HOLD
        assert decision.reasons[0].code == code


def test_agy_routine_coder_missing_and_one_percent_quota_are_loud_holds():
    target = agy_route(role=Role.ROUTINE_CODER, profile="agy-routine", effort="medium")
    request = DispatchRequest("routine_coder", "agy-routine", "medium")

    cases = [
        ((), "QUOTA_TELEMETRY_MISSING"),
        ((quota(target, used=99, remaining=1),), "ROUTINE_CODER_HEADROOM_BELOW_30_PERCENT"),
    ]
    for snapshots, code in cases:
        decision = decide(request, (target,), quotas=snapshots)
        assert decision.status is DecisionStatus.HOLD
        assert decision.reasons[0].code == code


def test_agy_routine_coder_exactly_30_percent_quota_passes_with_valid_telemetry():
    target = agy_route(role=Role.ROUTINE_CODER, profile="agy-routine", effort="medium")
    request = DispatchRequest("routine_coder", "agy-routine", "medium")

    decision = decide(request, (target,), quotas=(quota(target, used=70, remaining=30),))

    assert decision.status is DecisionStatus.ALLOW
    assert decision.quota_headroom == 0.30
    assert decision.route_identity.reason == "AGY_ROUTE_IDENTITY_VERIFIED"


def test_undateable_quota_is_explicit_and_cannot_be_hidden_by_valid_snapshot():
    target = route(Role.ROUTINE_CODER, effort="medium")
    request = DispatchRequest("routine_coder", "profile-a", "medium")
    corrupt_negative = quota(target, used=90, remaining=10, captured_at="not-a-time")
    valid_positive = quota(target, used=60, remaining=40, captured_at="2026-08-05T11:59:00Z")

    for snapshots in ((corrupt_negative,), (valid_positive, corrupt_negative), (corrupt_negative, valid_positive)):
        decision = decide(request, (target,), (available(target),), snapshots)
        assert decision.status is DecisionStatus.HOLD
        assert decision.reasons[0].code == "QUOTA_TELEMETRY_INVALID_TIMESTAMP"
        assert decision.reasons[0].route_id == target.route_id


def test_non_routine_role_does_not_use_routine_quota_headroom():
    builder = route(Role.BUILDER)
    decision = decide(
        DispatchRequest("builder", "profile-a", "high"),
        (builder,),
        (available(builder, value=False),),
        (quota(builder, used=0, remaining=100),),
    )
    assert decision.status is DecisionStatus.HOLD
    assert decision.reasons[0].code == "ROUTE_UNAVAILABLE"
    assert decision.quota_headroom is None


def test_agy_slug_selector_is_rejected_before_log_can_prove_identity():
    target = agy_route(selector="gemini-3.1-pro-high")
    decision = decide(DispatchRequest("builder", "agy-builder", "high"), (target,))

    assert AGY_CANONICAL_MODEL_LABELS == {"gemini-3.1-pro-high": "Gemini 3.1 Pro (High)"}
    assert decision.status is DecisionStatus.HOLD
    assert decision.reasons[0].code == "AGY_SELECTOR_NOT_CANONICAL"
    assert decision.route_identity.observed_label is None


def test_agy_missing_and_malformed_backend_labels_hold_structurally():
    request = DispatchRequest("builder", "agy-builder", "high")
    cases = [
        (agy_route(route_log="AGY completed successfully"), "AGY_BACKEND_LABEL_MISSING"),
        (
            agy_route(route_log="Propagating selected model override to backend: label=Gemini 3.1 Pro (High)"),
            "AGY_BACKEND_LABEL_MALFORMED",
        ),
    ]
    for target, expected_code in cases:
        decision = decide(request, (target,))
        assert decision.status is DecisionStatus.HOLD
        assert decision.reasons[0].code == expected_code
        assert decision.route_identity.reason == expected_code


def test_agy_conflicting_backend_labels_hold_even_if_one_matches():
    target = agy_route(route_log="\n".join((
        'Propagating selected model override to backend: label="Gemini 3.1 Pro (High)"',
        'Propagating selected model override to backend: label="Gemini 3.6 Flash (High)"',
    )))
    decision = decide(DispatchRequest("builder", "agy-builder", "high"), (target,))

    assert decision.status is DecisionStatus.HOLD
    assert decision.reasons[0].code == "AGY_BACKEND_LABEL_CONFLICT"


def test_agy_flash_remap_is_a_served_model_mismatch_not_success_evidence():
    target = agy_route(
        route_log='request succeeded\nPropagating selected model override to backend: label="Gemini 3.6 Flash (High)"'
    )
    decision = decide(DispatchRequest("builder", "agy-builder", "high"), (target,))
    receipt = decision.to_dict()

    assert decision.status is DecisionStatus.HOLD
    assert decision.reasons[0].code == "AGY_SERVED_MODEL_MISMATCH"
    assert receipt["route_identity"] == {
        "requested_id": "gemini-3.1-pro-high",
        "configured_selector": "Gemini 3.1 Pro (High)",
        "expected_label": "Gemini 3.1 Pro (High)",
        "observed_label": "Gemini 3.6 Flash (High)",
        "reason": "AGY_SERVED_MODEL_MISMATCH",
    }
    assert "route_log" not in receipt["candidate_attempts"][0]["route"]


def test_agy_exact_identity_still_holds_when_dropped_by_governance():
    target = agy_route(governance_status="DROPPED")
    decision = decide(DispatchRequest("builder", "agy-builder", "high"), (target,))

    assert decision.status is DecisionStatus.HOLD
    assert decision.reasons[0].code == "ROUTE_DROPPED_BY_GOVERNANCE"
    assert decision.route_identity.observed_label == "Gemini 3.1 Pro (High)"
    assert decision.route_identity.reason == "ROUTE_DROPPED_BY_GOVERNANCE"
    assert decision.quota_headroom is None


def test_agy_exact_identity_is_allowed_when_not_dropped_without_provider_telemetry():
    target = agy_route()
    decision = decide(DispatchRequest("builder", "agy-builder", "high"), (target,))

    assert decision.status is DecisionStatus.ALLOW
    assert decision.route_identity.requested_id == "gemini-3.1-pro-high"
    assert decision.route_identity.reason == "AGY_ROUTE_IDENTITY_VERIFIED"
    assert decision.quota_headroom is None


def test_non_agy_routes_are_unaffected_by_agy_identity_fields():
    target = route()
    target = WorkerRoute(**{**target.__dict__, "agy_selector": "gemini-3.1-pro-high", "route_log": "malformed"})
    decision = decide(
        DispatchRequest("builder", "profile-a", "high"),
        (target,),
        (available(target),),
    )

    assert decision.status is DecisionStatus.ALLOW
    assert decision.route_identity is None


def test_dry_run_is_deterministic_json_data():
    target = route()
    payload = {
        "as_of": NOW,
        "routes": [{**target.__dict__, "role": target.role.value, "fallback_route_ids": []}],
        "availability": [{**available(target).__dict__, "state": "known"}],
        "quotas": [],
        "request": {"role": "builder", "profile": "profile-a", "reasoning_effort": "high"},
    }
    assert dry_run(payload) == dry_run(payload)
    assert dry_run(payload)["runtime_integration"] is False
    assert dry_run(payload)["activation_status"] == "not_activated"
    assert dry_run(payload)["receipts"][0]["status"] == "ALLOW"


def test_dry_run_missing_as_of_is_a_structured_fail_closed_operator_error():
    result = dry_run({"routes": [], "requests": []})
    assert result["status"] == "OPERATOR_ERROR"
    assert result["error"] == {"code": "MISSING_AS_OF", "message": "dry-run input requires as_of"}
    assert result["receipts"] == []


def test_cli_malformed_input_has_structured_output_no_traceback_and_nonzero_exit(tmp_path):
    malformed = tmp_path / "malformed.json"
    malformed.write_text('{"routes": []}', encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, "tools/dynamic_dispatch_policy.py", str(malformed)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert completed.stderr == ""
    assert "Traceback" not in completed.stdout
    result = json.loads(completed.stdout)
    assert result["status"] == "OPERATOR_ERROR"
    assert result["error"]["code"] == "MISSING_AS_OF"


@pytest.mark.parametrize(
    ("field", "malformed_value"),
    (
        ("provider", None),
        ("provider", 7),
        ("provider", ["agy"]),
        ("route_log", 7),
        ("route_log", ["backend label"]),
        ("governance_status", 7),
    ),
)
def test_cli_malformed_route_string_types_are_structured_operator_errors(
    tmp_path, field, malformed_value
):
    target = agy_route()
    route_data = {
        **target.__dict__,
        "role": target.role.value,
        "fallback_route_ids": [],
        field: malformed_value,
    }
    payload = {
        "as_of": NOW,
        "routes": [route_data],
        "availability": [],
        "quotas": [],
        "request": {
            "route_id": target.route_id,
            "role": "builder",
            "profile": "agy-builder",
            "reasoning_effort": "high",
        },
    }
    malformed = tmp_path / f"malformed-{field}.json"
    malformed.write_text(json.dumps(payload), encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, "tools/dynamic_dispatch_policy.py", str(malformed)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert completed.stderr == ""
    result = json.loads(completed.stdout)
    assert result["status"] == "OPERATOR_ERROR"
    assert result["receipts"] == []
