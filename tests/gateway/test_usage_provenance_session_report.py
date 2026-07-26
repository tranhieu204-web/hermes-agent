"""End-to-end usage provenance and bound-session behavior contracts."""

from types import SimpleNamespace

import pytest

from agent.progress_telemetry import ProgressTelemetry, TerminalEvent, TerminalStatus
from agent.usage_provenance import (
    UsageComponentReceipt,
    UsageProvenance,
    aggregate_usage,
)
from gateway.fleet_safety.deadloop_guard import (
    GuardEvaluationResult,
    GuardThresholds,
    RunawayGuard,
    SessionObservation,
)
from gateway.fleet_safety.integration import _collect_observations
from gateway.fleet_safety.report import build_hard_stop_report, format_kill_report


def _component(
    component_id: str,
    provenance: UsageProvenance | str,
    *,
    session_id: str = "bound-session",
    total_tokens: int | None = 10,
    reason: str | None = None,
) -> UsageComponentReceipt:
    normalized = UsageProvenance(provenance)
    return UsageComponentReceipt(
        component_id=component_id,
        source="backend_test_receipt",
        session_id=session_id,
        provenance=normalized,
        authority=(
            "backend_observed"
            if normalized is UsageProvenance.MEASURED
            else "unverified"
        ),
        authoritative=normalized is UsageProvenance.MEASURED,
        accepted_event_id=(
            f"accepted:{component_id}"
            if normalized is UsageProvenance.MEASURED
            else None
        ),
        input_tokens=total_tokens,
        output_tokens=0 if total_tokens is not None else None,
        total_tokens=total_tokens,
        reason=reason,
    )


@pytest.mark.parametrize(
    ("components", "expected", "known", "unknown", "verified"),
    [
        ([_component("parent", "measured")], UsageProvenance.MEASURED, 1, 0, True),
        ([_component("parent", "estimated")], UsageProvenance.ESTIMATED, 1, 0, False),
        (
            [_component("parent", "measured"), _component("child", "estimated")],
            UsageProvenance.MIXED,
            2,
            0,
            False,
        ),
        (
            [
                _component("parent", "measured"),
                _component("child", "unknown", total_tokens=None, reason="child_usage_missing"),
            ],
            UsageProvenance.MIXED,
            1,
            1,
            False,
        ),
        (
            [_component("child", "unknown", total_tokens=None, reason="child_usage_missing")],
            UsageProvenance.UNKNOWN,
            0,
            1,
            False,
        ),
    ],
)
def test_one_aggregation_function_preserves_provenance_truth(
    components, expected, known, unknown, verified
):
    usage = aggregate_usage("bound-session", components)

    assert usage.session_id == "bound-session"
    assert usage.provenance is expected
    assert usage.known_component_count == known
    assert usage.unknown_component_count == unknown
    assert usage.usage_verified is verified
    assert usage.headroom_verified is False


def test_session_mismatch_fails_closed_and_keeps_receipt():
    usage = aggregate_usage(
        "bound-session",
        [_component("wrong-session", "measured", session_id="other-session")],
    )

    assert usage.session_id == "bound-session"
    assert usage.provenance is UsageProvenance.UNKNOWN
    assert usage.known_component_count == 0
    assert usage.unknown_component_count == 1
    assert usage.components[0].component_id == "wrong-session"
    assert usage.components[0].session_matches is False
    assert usage.components[0].reason == "session_mismatch"
    assert usage.usage_verified is False
    assert usage.headroom_verified is False


@pytest.mark.parametrize(
    ("claimed_provenance", "expected_reason"),
    [
        (UsageProvenance.ESTIMATED, "usage_value_missing"),
        (UsageProvenance.MIXED, "component_provenance_mixed"),
    ],
)
def test_component_without_numeric_evidence_is_unknown(
    claimed_provenance, expected_reason
):
    """A provenance label alone must never manufacture known usage."""

    component = UsageComponentReceipt(
        component_id=f"missing:{claimed_provenance.value}",
        session_id="bound-session",
        provenance=claimed_provenance,
    )
    usage = aggregate_usage("bound-session", [component])

    assert component.provenance is UsageProvenance.UNKNOWN
    assert component.known is False
    assert component.reason == expected_reason
    assert usage.provenance is UsageProvenance.UNKNOWN
    assert usage.known_component_count == 0
    assert usage.unknown_component_count == 1
    assert usage.usage_verified is False


def test_explicit_estimated_zero_remains_known():
    component = UsageComponentReceipt(
        component_id="estimated-zero",
        session_id="bound-session",
        provenance=UsageProvenance.ESTIMATED,
        total_tokens=0,
    )
    usage = aggregate_usage("bound-session", [component])

    assert component.provenance is UsageProvenance.ESTIMATED
    assert component.known is True
    assert usage.provenance is UsageProvenance.ESTIMATED
    assert usage.known_component_count == 1
    assert usage.unknown_component_count == 0


def test_run_agent_activity_summary_exposes_sanitized_explicit_provenance():
    from run_agent import AIAgent

    telemetry = ProgressTelemetry(session_id="bound-session")
    telemetry.record_usage_component(
        component_id="model-call:1",
        session_id="bound-session",
        provenance="measured",
        input_tokens=7,
        output_tokens=3,
        total_tokens=10,
        provider_payload={"secret": "must-not-escape"},
    )
    telemetry.record_terminal_event(
        TerminalEvent(
            event_id="private-result-event",
            call_id="private-result-call",
            adapter="test",
            source="tool_executor",
            status=TerminalStatus.SUCCESS,
            result={"secret": "must-not-cross-activity-boundary"},
            session_id="bound-session",
        ),
        tool_name="private_tool",
    )
    agent = SimpleNamespace(
        _last_activity_ts=1.0,
        _last_activity_desc="model response",
        _current_tool=None,
        _api_call_count=1,
        max_iterations=4,
        iteration_budget=SimpleNamespace(used=1, max_total=4),
        _progress_telemetry=telemetry,
    )

    summary = AIAgent.get_activity_summary(agent)

    assert summary["session_id"] == "bound-session"
    assert summary["usage_provenance"] == "measured"
    assert summary["known_component_count"] == 1
    assert summary["unknown_component_count"] == 0
    assert summary["usage"]["fully_measured"] is True
    assert "secret" not in repr(summary["usage"])
    assert "last_result" not in summary
    assert summary["last_result_hash"]
    assert "must-not-cross-activity-boundary" not in repr(summary)


@pytest.mark.parametrize(
    "adapter", ["openai-wire", "anthropic", "codex-responses", "gateway"]
)
def test_provider_neutral_terminal_matrix_has_equivalent_usage_semantics(adapter):
    telemetry = ProgressTelemetry(session_id="bound-session")
    recorded = telemetry.record_terminal_event(
        TerminalEvent(
            event_id=f"{adapter}:response:1",
            call_id="call-1",
            adapter=adapter,
            source="provider_terminal",
            status=TerminalStatus.SUCCESS,
            result="done",
            session_id="bound-session",
            usage={
                "component_id": "backend-call:1",
                "session_id": "bound-session",
                "provenance": "measured",
                "input_tokens": 7,
                "output_tokens": 3,
                "total_tokens": 10,
            },
        ),
        tool_name="provider_response",
    )

    usage = recorded.snapshot["usage"]
    assert (
        usage["session_id"],
        usage["provenance"],
        usage["total_tokens"],
        usage["known_component_count"],
        usage["unknown_component_count"],
    ) == ("bound-session", "measured", 10, 1, 0)


def test_replayed_terminal_event_and_poll_snapshot_do_not_change_usage():
    telemetry = ProgressTelemetry(session_id="bound-session")
    event = TerminalEvent(
        event_id="stable-event",
        call_id="stable-call",
        adapter="wire",
        source="provider_terminal",
        status=TerminalStatus.SUCCESS,
        result="done",
        session_id="bound-session",
        usage={
            "component_id": "backend-call:1",
            "session_id": "bound-session",
            "provenance": "estimated",
            "total_tokens": 9,
        },
    )

    first = telemetry.record_terminal_event(event, tool_name="provider_response")
    before = telemetry.get_activity_snapshot()["usage"]
    replay = telemetry.record_terminal_event(event, tool_name="provider_response")
    poll_one = telemetry.get_activity_snapshot()["usage"]
    poll_two = telemetry.get_activity_snapshot()["usage"]

    assert first.replayed is False
    assert replay.replayed is True
    assert before == poll_one == poll_two
    assert poll_two["provenance"] == "estimated"
    assert poll_two["total_tokens"] == 9
    assert poll_two["known_component_count"] == 1


def test_mismatched_terminal_usage_never_rebinds_final_session():
    telemetry = ProgressTelemetry(session_id="bound-session")
    recorded = telemetry.record_terminal_event(
        TerminalEvent(
            event_id="mismatch",
            call_id="mismatch",
            adapter="wire",
            source="provider_terminal",
            status=TerminalStatus.SUCCESS,
            result="done",
            session_id="other-session",
            usage={
                "component_id": "backend-call:wrong",
                "session_id": "other-session",
                "provenance": "measured",
                "total_tokens": 99,
            },
        ),
        tool_name="provider_response",
    )

    snapshot = recorded.snapshot
    assert telemetry.session_id == "bound-session"
    assert snapshot["session_id"] == "bound-session"
    assert snapshot["last_session_id"] == "bound-session"
    assert snapshot["usage_provenance"] == "unknown"
    assert snapshot["unknown_component_count"] == 1
    assert snapshot["usage"]["components"][0]["reason"] == "session_mismatch"


def test_unknown_child_remains_visible_in_terminal_receipt():
    telemetry = ProgressTelemetry(session_id="bound-session")
    recorded = telemetry.record_terminal_event(
        TerminalEvent(
            event_id="parent-with-child",
            call_id="parent-with-child",
            adapter="wire",
            source="subagent_terminal",
            status=TerminalStatus.SUCCESS,
            result="done",
            session_id="bound-session",
            usage={
                "session_id": "bound-session",
                "components": [
                    {
                        "component_id": "parent",
                        "session_id": "bound-session",
                        "provenance": "measured",
                        "total_tokens": 10,
                    },
                    {
                        "component_id": "child",
                        "session_id": "bound-session",
                        "provenance": "unknown",
                        "reason": "child_usage_missing",
                    },
                ],
            },
        ),
        tool_name="delegate_task",
    )

    usage = recorded.snapshot["usage"]
    assert usage["provenance"] == "mixed"
    assert usage["known_component_count"] == 1
    assert usage["unknown_component_count"] == 1
    assert {row["component_id"] for row in usage["components"]} == {"parent", "child"}
    assert any(row["reason"] == "child_usage_missing" for row in usage["components"])


def _runner_with_summary(summary, *, agent_session_id="bound-session"):
    agent = SimpleNamespace(
        get_activity_summary=lambda: summary,
        session_id=agent_session_id,
        provider="neutral-provider",
        model="neutral-model",
        reasoning_config={},
    )
    return SimpleNamespace(
        _running_agents={"conversation-key": agent},
        _running_agents_ts={"conversation-key": 1.0},
    )


def test_integration_preserves_bound_session_and_usage_receipt():
    telemetry = ProgressTelemetry(session_id="bound-session")
    telemetry.record_usage_component(
        component_id="model-call:1",
        session_id="bound-session",
        provenance="measured",
        total_tokens=12,
    )
    summary = {
        "api_call_count": 1,
        **telemetry.get_activity_snapshot(),
    }

    observations, mapping = _collect_observations(
        _runner_with_summary(summary), now=5.0, assumed_context_tokens=160_000
    )

    observation = observations[0]
    assert observation.session_id == "bound-session"
    assert observation.terminal_session_id == "bound-session"
    assert observation.usage.session_id == "bound-session"
    assert observation.usage.provenance is UsageProvenance.MEASURED
    assert mapping["bound-session"][0] == "conversation-key"


def test_missing_activity_usage_is_unknown_and_cannot_verify_usage_or_headroom():
    observations, _ = _collect_observations(
        _runner_with_summary({"api_call_count": 2}),
        now=5.0,
        assumed_context_tokens=160_000,
    )

    usage = observations[0].usage
    assert usage.provenance is UsageProvenance.UNKNOWN
    assert usage.unknown_component_count == 1
    assert usage.components[0].reason == "missing_activity_usage"
    assert usage.usage_verified is False
    assert usage.headroom_verified is False


def test_guard_evaluation_and_structured_report_keep_same_usage_identity():
    usage = aggregate_usage(
        "bound-session",
        [
            _component("parent", "measured"),
            _component("child", "unknown", total_tokens=None, reason="child_usage_missing"),
        ],
    )
    observation = SessionObservation(
        session_id="bound-session",
        started_at=0.0,
        api_call_count=2,
        tokens_used=20,
        token_count_provenance="estimated",
        usage=usage,
    )
    guard = RunawayGuard(
        GuardThresholds(
            max_runtime_seconds=1,
            max_calls_per_window=10_000,
            max_tokens_per_window=10_000_000,
        )
    )

    evaluation = guard.observe(observation, now=2.0)
    assert isinstance(evaluation, GuardEvaluationResult)
    assert evaluation.session_id == "bound-session"
    assert evaluation.usage == usage
    assert evaluation.usage_provenance is UsageProvenance.MIXED
    assert evaluation.known_component_count == 1
    assert evaluation.unknown_component_count == 1
    assert evaluation.usage_verified is False
    assert evaluation.headroom_verified is False

    report = build_hard_stop_report(evaluation)
    payload = report.to_dict()
    assert payload["session_id"] == "bound-session"
    assert payload["usage_provenance"] == "mixed"
    assert payload["known_component_count"] == 1
    assert payload["unknown_component_count"] == 1
    assert payload["fully_measured"] is False
    assert payload["has_unknown_components"] is True
    assert payload["measured_vs_unknown"] == "known_and_unknown"
    assert payload["usage_verified"] is False
    assert payload["headroom_verified"] is False
    assert payload["decision"] == "hard_stop_required"
    assert payload["enforcement_outcome"] == "not_asserted"

    rendered = format_kill_report(evaluation)
    assert "bound-session" in rendered
    assert "provenance: mixed" in rendered
    assert "known components: 1" in rendered
    assert "unknown components: 1" in rendered
    assert "enforcement outcome: not asserted" in rendered
    assert "turn aborted" not in rendered
    assert "lease released" not in rendered
    assert "No human action required" not in rendered
