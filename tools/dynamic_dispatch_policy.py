#!/usr/bin/env python3
"""Pure, fail-closed policy for a future Sakaan/Hermes worker dispatcher.

This module deliberately has no Hermes runtime imports. Policy evaluation and
``dry_run`` are pure: they cannot call a provider, modify configuration, or
spawn a worker. The CLI reads its input and emits its receipt to stdout without
mutating the filesystem by default; ``--output`` is the explicit opt-in write
path.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


ROUTINE_CODER_MIN_HEADROOM = 0.30
DEFAULT_MAX_TELEMETRY_AGE_SECONDS = 300

# AGY accepts human-facing CLI selectors, not Hermes/provider model slugs. Keep
# this table explicit: adding a model requires reviewing the exact identity AGY
# reports back in its backend propagation log.
AGY_CANONICAL_MODEL_LABELS: Mapping[str, str] = {
    "gemini-3.1-pro-high": "Gemini 3.1 Pro (High)",
}
AGY_BACKEND_PROPAGATION_MARKER = "Propagating selected model override to backend:"
_AGY_BACKEND_PROPAGATION_RE = re.compile(
    r'Propagating selected model override to backend:\s*label="([^"\r\n]+)"\s*$'
)


class Role(str, Enum):
    BUILDER = "builder"
    REVIEWER = "reviewer"
    INSPECTOR = "inspector"
    ROUTINE_CODER = "routine_coder"


class DecisionStatus(str, Enum):
    ALLOW = "ALLOW"
    HOLD = "HOLD"


class TelemetryState(str, Enum):
    KNOWN = "known"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class WorkerRoute:
    """One data-only model route.  A name here is not evidence of availability."""

    route_id: str
    role: Role
    profile: str
    provider: str
    model: str
    reasoning_effort: str
    rank: int
    quota_scope: str
    fallback_route_ids: tuple[str, ...] = ()
    agy_selector: str | None = None
    route_log: str | None = None
    governance_status: str | None = None


@dataclass(frozen=True)
class DispatchRequest:
    role: str
    profile: str
    reasoning_effort: str
    route_id: str | None = None


@dataclass(frozen=True)
class RouteAvailabilitySnapshot:
    profile: str
    provider: str
    model: str
    available: bool | None
    captured_at: str
    source: str
    state: TelemetryState
    confidence: str


@dataclass(frozen=True)
class ProviderQuotaSnapshot:
    provider: str
    scope: str
    limit: float | None
    used: float | None
    remaining: float | None
    captured_at: str
    source: str
    state: TelemetryState
    confidence: str


@dataclass(frozen=True)
class DecisionReason:
    code: str
    message: str
    route_id: str | None = None


@dataclass(frozen=True)
class RouteIdentityReceipt:
    """AGY identity evidence, deliberately independent from provider quota."""

    requested_id: str
    configured_selector: str | None
    expected_label: str | None
    observed_label: str | None
    reason: str


@dataclass(frozen=True)
class CandidateAttempt:
    """Evidence produced by one specific route candidate."""

    route: WorkerRoute
    outcome: DecisionStatus
    quota_headroom: float | None
    reasons: tuple[DecisionReason, ...]
    route_identity: RouteIdentityReceipt | None = None


@dataclass(frozen=True)
class DispatchDecision:
    status: DecisionStatus
    requested_role: str
    requested_profile: str
    requested_reasoning_effort: str
    requested_route_id: str | None
    selected_route: WorkerRoute | None
    used_fallback: bool
    quota_headroom: float | None
    reasons: tuple[DecisionReason, ...]
    candidate_attempts: tuple[CandidateAttempt, ...]
    route_identity: RouteIdentityReceipt | None = None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["status"] = self.status.value

        def clean_route(route_data: dict[str, Any]) -> None:
            # Raw AGY logs are evaluation input, not receipt material. Identity
            # evidence is emitted in the dedicated route_identity object.
            route_data.pop("agy_selector")
            route_data.pop("route_log")
            route_data.pop("governance_status")

        if result["selected_route"] is not None:
            result["selected_route"]["role"] = self.selected_route.role.value
            clean_route(result["selected_route"])
        for index, attempt in enumerate(self.candidate_attempts):
            result["candidate_attempts"][index]["outcome"] = attempt.outcome.value
            result["candidate_attempts"][index]["route"]["role"] = attempt.route.role.value
            clean_route(result["candidate_attempts"][index]["route"])
            if attempt.route_identity is None:
                result["candidate_attempts"][index].pop("route_identity")
        if self.route_identity is None:
            result.pop("route_identity")
        return result


# Deliberately inert example identities. Availability must always be proven by a
# supplied snapshot; merely appearing in this policy never makes a route usable.
DEFAULT_ROUTES: tuple[WorkerRoute, ...] = (
    WorkerRoute("sakaan.builder.primary.v1", Role.BUILDER, "builder-primary", "provider-a", "builder-model", "high", 400, "workers"),
    WorkerRoute("sakaan.reviewer.primary.v1", Role.REVIEWER, "reviewer-primary", "provider-b", "reviewer-model", "high", 300, "workers"),
    WorkerRoute("sakaan.inspector.primary.v1", Role.INSPECTOR, "inspector-primary", "provider-c", "inspector-model", "medium", 200, "workers"),
    WorkerRoute("sakaan.routine-coder.primary.v1", Role.ROUTINE_CODER, "routine-coder-primary", "provider-d", "routine-model", "medium", 100, "workers"),
)


def _parse_timestamp(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _freshness_error(captured_at: str, as_of: datetime, max_age: timedelta) -> str | None:
    try:
        captured = _parse_timestamp(captured_at)
    except (TypeError, ValueError):
        return "invalid"
    age = as_of - captured
    if age < timedelta(0):
        return "future"
    if age > max_age:
        return "stale"
    return None


def _finite_nonnegative(value: float | None) -> bool:
    return value is not None and isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def _is_agy_route(route: WorkerRoute) -> bool:
    return route.provider.strip().casefold() == "agy"


def _agy_identity_result(route: WorkerRoute) -> tuple[RouteIdentityReceipt, DecisionReason | None]:
    """Validate one AGY selector and its supplied backend propagation log."""
    expected = AGY_CANONICAL_MODEL_LABELS.get(route.model)

    def result(code: str, message: str, observed: str | None = None) -> tuple[RouteIdentityReceipt, DecisionReason]:
        return (
            RouteIdentityReceipt(route.model, route.agy_selector, expected, observed, code),
            DecisionReason(code, message, route.route_id),
        )

    if expected is None:
        return result("AGY_REQUESTED_MODEL_NOT_ALLOWLISTED", "requested AGY model ID is not in the canonical identity allowlist")
    if route.agy_selector == route.model:
        return result("AGY_SELECTOR_NOT_CANONICAL", "AGY selector must be the exact CLI display label, not a canonical internal slug")
    if route.agy_selector != expected:
        return result("AGY_SELECTOR_NOT_CANONICAL", "AGY selector does not exactly match the allowlisted CLI display label")

    propagation_lines = [
        line for line in (route.route_log or "").splitlines()
        if AGY_BACKEND_PROPAGATION_MARKER in line
    ]
    if not propagation_lines:
        return result("AGY_BACKEND_LABEL_MISSING", "AGY route log has no backend model propagation label")

    observed_labels: list[str] = []
    for line in propagation_lines:
        match = _AGY_BACKEND_PROPAGATION_RE.search(line)
        if match is None:
            return result("AGY_BACKEND_LABEL_MALFORMED", "AGY backend model propagation line is malformed")
        observed_labels.append(match.group(1))

    unique_labels = set(observed_labels)
    if len(unique_labels) != 1:
        return result("AGY_BACKEND_LABEL_CONFLICT", "AGY route log contains conflicting backend model propagation labels")
    observed = observed_labels[0]
    if observed != expected:
        return result("AGY_SERVED_MODEL_MISMATCH", "AGY served model label does not match the requested canonical identity", observed)
    identity = RouteIdentityReceipt(route.model, route.agy_selector, expected, observed, "AGY_ROUTE_IDENTITY_VERIFIED")
    return identity, None


def _hold(request: DispatchRequest, code: str, message: str, route: WorkerRoute | None = None) -> DispatchDecision:
    return DispatchDecision(
        status=DecisionStatus.HOLD,
        requested_role=request.role,
        requested_profile=request.profile,
        requested_reasoning_effort=request.reasoning_effort,
        requested_route_id=request.route_id,
        selected_route=route,
        used_fallback=False,
        quota_headroom=None,
        reasons=(DecisionReason(code, message, route.route_id if route else None),),
        candidate_attempts=(),
    )


def _availability_reason(
    route: WorkerRoute,
    snapshots: Sequence[RouteAvailabilitySnapshot],
    as_of: datetime,
    max_age: timedelta,
) -> DecisionReason | None:
    matching = [s for s in snapshots if (s.profile, s.provider, s.model) == (route.profile, route.provider, route.model)]
    if not matching:
        return DecisionReason("ROUTE_TELEMETRY_MISSING", "no availability snapshot was supplied for the route target", route.route_id)

    # Undateable matching observations are negative evidence, not old
    # observations that may be silently sorted behind a parseable one. Their
    # ordering cannot be established, so fail closed regardless of siblings.
    try:
        for candidate in matching:
            _parse_timestamp(candidate.captured_at)
    except (TypeError, ValueError):
        return DecisionReason(
            "ROUTE_TELEMETRY_INVALID",
            "matching route availability telemetry contains an invalid or undateable captured_at",
            route.route_id,
        )

    def sort_key(snapshot: RouteAvailabilitySnapshot) -> datetime:
        return _parse_timestamp(snapshot.captured_at)

    snapshot = max(matching, key=sort_key)
    freshness = _freshness_error(snapshot.captured_at, as_of, max_age)
    if freshness:
        return DecisionReason(f"ROUTE_TELEMETRY_{freshness.upper()}", f"route availability telemetry is {freshness}", route.route_id)
    if snapshot.state is not TelemetryState.KNOWN or snapshot.available is None:
        return DecisionReason("ROUTE_AVAILABILITY_UNKNOWN", "route availability telemetry is unknown", route.route_id)
    if snapshot.available is not True:
        return DecisionReason("ROUTE_UNAVAILABLE", "the requested route target is reported unavailable", route.route_id)
    return None


def _quota_result(
    route: WorkerRoute,
    snapshots: Sequence[ProviderQuotaSnapshot],
    as_of: datetime,
    max_age: timedelta,
) -> tuple[float | None, DecisionReason | None]:
    matching = [s for s in snapshots if (s.provider, s.scope) == (route.provider, route.quota_scope)]
    if not matching:
        return None, DecisionReason("QUOTA_TELEMETRY_MISSING", "no matching provider quota snapshot was supplied", route.route_id)

    # Undateable matching observations are negative evidence, not old
    # observations that may be silently sorted behind a parseable one. Their
    # ordering cannot be established, so fail closed regardless of siblings.
    try:
        for candidate in matching:
            _parse_timestamp(candidate.captured_at)
    except (TypeError, ValueError):
        return None, DecisionReason(
            "QUOTA_TELEMETRY_INVALID_TIMESTAMP",
            "matching quota telemetry contains an invalid or undateable captured_at",
            route.route_id,
        )

    def sort_key(snapshot: ProviderQuotaSnapshot) -> datetime:
        return _parse_timestamp(snapshot.captured_at)

    snapshot = max(matching, key=sort_key)
    freshness = _freshness_error(snapshot.captured_at, as_of, max_age)
    if freshness:
        return None, DecisionReason(f"QUOTA_TELEMETRY_{freshness.upper()}", f"quota telemetry is {freshness}", route.route_id)
    if snapshot.state is not TelemetryState.KNOWN:
        return None, DecisionReason("QUOTA_TELEMETRY_UNKNOWN", "quota telemetry is explicitly unknown", route.route_id)
    if not all(_finite_nonnegative(value) for value in (snapshot.limit, snapshot.used, snapshot.remaining)):
        return None, DecisionReason("QUOTA_TELEMETRY_INVALID", "known quota telemetry requires finite, non-negative limit, used, and remaining values", route.route_id)
    assert snapshot.limit is not None and snapshot.used is not None and snapshot.remaining is not None
    tolerance = max(1e-9, snapshot.limit * 1e-9)
    if snapshot.limit <= 0 or snapshot.used > snapshot.limit + tolerance or snapshot.remaining > snapshot.limit + tolerance or abs((snapshot.used + snapshot.remaining) - snapshot.limit) > tolerance:
        return None, DecisionReason("QUOTA_TELEMETRY_INVALID", "quota values must satisfy limit > 0 and used + remaining = limit", route.route_id)
    headroom = snapshot.remaining / snapshot.limit
    if headroom < ROUTINE_CODER_MIN_HEADROOM:
        return headroom, DecisionReason("ROUTINE_CODER_HEADROOM_BELOW_30_PERCENT", "routine_coder requires provider quota headroom of at least 30%", route.route_id)
    return headroom, None


def evaluate_dispatch(
    request: DispatchRequest,
    routes: Sequence[WorkerRoute],
    availability: Sequence[RouteAvailabilitySnapshot],
    quotas: Sequence[ProviderQuotaSnapshot],
    *,
    as_of: str,
    max_telemetry_age_seconds: int = DEFAULT_MAX_TELEMETRY_AGE_SECONDS,
) -> DispatchDecision:
    """Evaluate one dispatch request without side effects.

    ``as_of`` is mandatory rather than read from the clock, making dry runs
    reproducible. Selection is fail-closed: only an explicitly declared fallback
    may be considered after the highest-ranked candidate fails.
    """
    try:
        requested_role = Role(request.role)
    except ValueError:
        return _hold(request, "INVALID_ROLE", "role must be one of builder, reviewer, inspector, routine_coder")
    if not request.profile:
        return _hold(request, "INVALID_PROFILE", "an explicit profile is required")
    if not request.reasoning_effort:
        return _hold(request, "INVALID_REASONING_EFFORT", "an explicit reasoning effort is required")
    try:
        decision_time = _parse_timestamp(as_of)
    except (TypeError, ValueError):
        return _hold(request, "INVALID_AS_OF", "as_of must be an ISO-8601 timestamp with a UTC offset")
    if not isinstance(max_telemetry_age_seconds, int) or isinstance(max_telemetry_age_seconds, bool) or max_telemetry_age_seconds < 0:
        return _hold(request, "INVALID_MAX_TELEMETRY_AGE", "max telemetry age must be a non-negative integer")
    max_age = timedelta(seconds=max_telemetry_age_seconds)

    ids: dict[str, WorkerRoute] = {}
    for route in routes:
        if not route.route_id or route.route_id in ids:
            return _hold(request, "INVALID_POLICY_DUPLICATE_ROUTE_ID", "policy route IDs must be non-empty and unique")
        ids[route.route_id] = route

    if request.route_id is not None:
        primary = ids.get(request.route_id)
        if primary is None:
            return _hold(request, "ROUTE_NOT_FOUND", "the requested route ID is not declared by policy")
        candidates = [primary]
    else:
        matches = [r for r in routes if r.role is requested_role and r.profile == request.profile and r.reasoning_effort == request.reasoning_effort]
        if not matches:
            role_matches = [r for r in routes if r.role is requested_role]
            if role_matches and all(r.profile != request.profile for r in role_matches):
                return _hold(request, "ROLE_PROFILE_MISMATCH", "requested role and profile are not paired by policy")
            return _hold(request, "NO_ROUTE_AT_REQUESTED_EFFORT", "no policy route exactly matches the requested reasoning effort")
        primary = max(matches, key=lambda route: (route.rank, route.route_id))
        candidates = [primary]

    if primary.role is not requested_role:
        return _hold(request, "ROUTE_ROLE_MISMATCH", "requested route does not implement the requested role", primary)
    if primary.profile != request.profile:
        return _hold(request, "ROLE_PROFILE_MISMATCH", "requested route does not use the explicitly requested profile", primary)
    if primary.reasoning_effort != request.reasoning_effort:
        return _hold(request, "REASONING_EFFORT_MISMATCH", "requested route does not provide the exact requested reasoning effort", primary)

    for fallback_id in primary.fallback_route_ids:
        fallback = ids.get(fallback_id)
        if fallback is None:
            return _hold(request, "INVALID_POLICY_FALLBACK_NOT_FOUND", f"declared fallback route {fallback_id!r} does not exist", primary)
        if fallback.role is not requested_role or fallback.profile != request.profile or fallback.reasoning_effort != request.reasoning_effort:
            return _hold(request, "INVALID_POLICY_FALLBACK_MISMATCH", "fallback must preserve requested role, profile, and reasoning effort", primary)
        candidates.append(fallback)

    attempts: list[CandidateAttempt] = []
    for index, route in enumerate(candidates):
        route_identity = None
        if _is_agy_route(route):
            route_identity, identity_failure = _agy_identity_result(route)
            if identity_failure is not None:
                attempts.append(CandidateAttempt(route, DecisionStatus.HOLD, None, (identity_failure,), route_identity))
                continue
            if (route.governance_status or "").strip().upper() == "DROPPED":
                governance_failure = DecisionReason(
                    "ROUTE_DROPPED_BY_GOVERNANCE",
                    "AGY route is dropped by governance and cannot be requalified",
                    route.route_id,
                )
                route_identity = RouteIdentityReceipt(
                    route_identity.requested_id,
                    route_identity.configured_selector,
                    route_identity.expected_label,
                    route_identity.observed_label,
                    governance_failure.code,
                )
                attempts.append(CandidateAttempt(route, DecisionStatus.HOLD, None, (governance_failure,), route_identity))
                continue
        else:
            unavailable = _availability_reason(route, availability, decision_time, max_age)
            if unavailable is not None:
                attempts.append(CandidateAttempt(route, DecisionStatus.HOLD, None, (unavailable,)))
                continue
        headroom = None
        if requested_role is Role.ROUTINE_CODER:
            headroom, quota_failure = _quota_result(route, quotas, decision_time, max_age)
            if quota_failure is not None:
                attempts.append(CandidateAttempt(route, DecisionStatus.HOLD, headroom, (quota_failure,)))
                continue
        allowed_reason = DecisionReason("ROUTE_ALLOWED", "exact requested route and effort passed all policy gates", route.route_id)
        attempts.append(CandidateAttempt(route, DecisionStatus.ALLOW, headroom, (allowed_reason,), route_identity))
        return DispatchDecision(
            status=DecisionStatus.ALLOW,
            requested_role=request.role,
            requested_profile=request.profile,
            requested_reasoning_effort=request.reasoning_effort,
            requested_route_id=request.route_id,
            selected_route=route,
            used_fallback=index > 0,
            quota_headroom=headroom,
            reasons=(allowed_reason,),
            candidate_attempts=tuple(attempts),
            route_identity=route_identity,
        )

    assert attempts
    return DispatchDecision(
        status=DecisionStatus.HOLD,
        requested_role=request.role,
        requested_profile=request.profile,
        requested_reasoning_effort=request.reasoning_effort,
        requested_route_id=request.route_id,
        selected_route=None,
        used_fallback=False,
        quota_headroom=None,
        reasons=tuple(reason for attempt in attempts for reason in attempt.reasons),
        candidate_attempts=tuple(attempts),
        route_identity=attempts[-1].route_identity,
    )


def _enum(enum_type: type[Enum], value: Any) -> Any:
    return enum_type(value)


def _required_string(item: Mapping[str, Any], field: str) -> str:
    value = item[field]
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    return value


def _optional_string(item: Mapping[str, Any], field: str) -> str | None:
    value = item.get(field)
    if value is not None and not isinstance(value, str):
        raise TypeError(f"{field} must be a string or null")
    return value


def _routes_from_json(items: Iterable[Mapping[str, Any]]) -> tuple[WorkerRoute, ...]:
    return tuple(WorkerRoute(
        route_id=item["route_id"], role=_enum(Role, item["role"]), profile=item["profile"],
        provider=_required_string(item, "provider"), model=item["model"], reasoning_effort=item["reasoning_effort"],
        rank=item["rank"], quota_scope=item["quota_scope"], fallback_route_ids=tuple(item.get("fallback_route_ids", ())),
        agy_selector=item.get("agy_selector"), route_log=_optional_string(item, "route_log"),
        governance_status=_optional_string(item, "governance_status"),
    ) for item in items)


def _receipt_base() -> dict[str, Any]:
    return {
        "artifact_type": "isolated_dispatch_policy_dry_run",
        "runtime_integration": False,
        "activation_status": "not_activated",
        "dry_run": True,
    }


def _operator_error(code: str, message: str) -> dict[str, Any]:
    return {
        **_receipt_base(),
        "status": "OPERATOR_ERROR",
        "error": {"code": code, "message": message},
        "receipts": [],
    }


def dry_run(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Purely convert supplied JSON-like data into deterministic receipts.

    Malformed operator input is represented as data and fails closed with no
    partial dispatch receipts. This function performs no filesystem writes;
    only the CLI's explicit ``--output`` option writes a receipt.
    """
    if not isinstance(payload, Mapping):
        return _operator_error("INVALID_DRY_RUN_INPUT", "dry-run input must be a JSON object")
    if "as_of" not in payload:
        return _operator_error("MISSING_AS_OF", "dry-run input requires as_of")
    try:
        _parse_timestamp(payload["as_of"])
    except (TypeError, ValueError):
        return _operator_error("INVALID_AS_OF", "as_of must be an ISO-8601 timestamp with a UTC offset")

    try:
        routes = _routes_from_json(payload.get("routes", ()))
        availability = tuple(RouteAvailabilitySnapshot(**{**item, "state": _enum(TelemetryState, item["state"])}) for item in payload.get("availability", ()))
        quotas = tuple(ProviderQuotaSnapshot(**{**item, "state": _enum(TelemetryState, item["state"])}) for item in payload.get("quotas", ()))
        requests = payload.get("requests")
        if requests is None:
            requests = (payload["request"],)
        receipts = [evaluate_dispatch(
            DispatchRequest(**item), routes, availability, quotas,
            as_of=payload["as_of"], max_telemetry_age_seconds=payload.get("max_telemetry_age_seconds", DEFAULT_MAX_TELEMETRY_AGE_SECONDS),
        ).to_dict() for item in requests]
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        return _operator_error("INVALID_DRY_RUN_INPUT", "dry-run policy, telemetry, or request data is malformed")

    return {**_receipt_base(), "status": "OK", "as_of": payload["as_of"], "receipts": receipts}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Isolated dynamic dispatch policy dry run; stdout mode performs no filesystem writes")
    parser.add_argument("input", type=Path, help="read JSON policy/request/telemetry input")
    parser.add_argument("--output", type=Path, help="opt in to writing receipt JSON here instead of pure stdout output")
    args = parser.parse_args(argv)
    try:
        with args.input.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except json.JSONDecodeError:
        result = _operator_error("INVALID_JSON", "input is not valid JSON")
    except OSError:
        result = _operator_error("INPUT_READ_FAILED", "input JSON could not be read")
    else:
        result = dry_run(payload)
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        try:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
        except OSError:
            print(json.dumps(_operator_error("OUTPUT_WRITE_FAILED", "opt-in output path could not be written"), indent=2, sort_keys=True))
            return 2
    else:
        print(rendered, end="")
    return 2 if result.get("status") == "OPERATOR_ERROR" else 0


if __name__ == "__main__":
    raise SystemExit(main())
