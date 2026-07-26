"""Provider-neutral runaway-progress contracts exercised through real components."""

from types import SimpleNamespace

import pytest

from agent.progress_telemetry import (
    ProgressTelemetry,
    Retryability,
    TerminalEvent,
    TerminalStatus,
    classify_retryability,
    normalize_result,
)
from agent.tool_guardrails import ToolCallGuardrailController
from gateway.fleet_safety.deadloop_guard import (
    GuardThresholds,
    RunawayGuard,
    SessionObservation,
    TripReason,
)
from gateway.fleet_safety.integration import _collect_observations


class _FakeOpenAI:
    def __init__(self, **kwargs):
        self.api_key = kwargs.get("api_key", "test")
        self.base_url = kwargs.get("base_url", "http://test")

    def close(self):
        return None


def _event(event_id: str, *, result: object = {"value": "same"}) -> TerminalEvent:
    return TerminalEvent(
        event_id=event_id,
        call_id="call-7",
        adapter="openai-wire",
        source="tool_executor",
        status=TerminalStatus.SUCCESS,
        result=result,
        retryability=Retryability.UNKNOWN,
    )


def test_terminal_event_replay_is_noop_but_distinct_event_id_is_new_attempt():
    telemetry = ProgressTelemetry(session_id="session-real")

    first = telemetry.record_terminal_event(
        _event("evt-1"), tool_name="read_file", args={"path": "same.py"}
    )
    replay = telemetry.record_terminal_event(
        _event("evt-1"), tool_name="read_file", args={"path": "same.py"}
    )
    distinct = telemetry.record_terminal_event(
        _event("evt-2"), tool_name="read_file", args={"path": "same.py"}
    )

    assert first.replayed is False
    assert replay.replayed is True
    assert distinct.replayed is False
    assert telemetry.attempt_seq == 2
    assert telemetry.no_progress_streak == 1
    assert telemetry.last_event_id == "evt-2"


@pytest.mark.parametrize("digits", ["12345678", "123456789", "1234567890", "12345678901", "123456789012"])
@pytest.mark.parametrize("field", ["order_id", "invoice", "account", "ticket", "transaction"])
def test_numeric_business_identifiers_remain_semantic(field: str, digits: str):
    other = str(int(digits) + 1).zfill(len(digits))

    assert normalize_result({field: digits}) != normalize_result({field: other})
    assert normalize_result(digits) != normalize_result(other)


def test_only_structural_or_iso_timestamps_are_normalized():
    first = {
        "timestamp": 1721900000,
        "created_at": "2026-07-26T01:02:03Z",
        "order_id": "1721900000",
    }
    second = {
        "timestamp": 1721900999,
        "created_at": "2026-07-26T02:03:04Z",
        "order_id": "1721900000",
    }

    assert normalize_result(first) == normalize_result(second)
    assert normalize_result("2026-07-26T01:02:03Z result") == normalize_result(
        "2026-07-26T02:03:04Z result"
    )
    assert normalize_result("1721900000") != normalize_result("1721900999")


@pytest.mark.parametrize(
    ("status_code", "error", "expected"),
    [
        (429, "rate limited", Retryability.RETRYABLE),
        (500, "server error", Retryability.RETRYABLE),
        (503, "service unavailable", Retryability.RETRYABLE),
        (None, "transient connection timeout", Retryability.RETRYABLE),
        (401, "unauthorized", Retryability.NON_RETRYABLE),
        (403, "forbidden", Retryability.NON_RETRYABLE),
        (None, "authentication failed", Retryability.NON_RETRYABLE),
        (None, "provider failed", Retryability.UNKNOWN),
    ],
)
def test_retryability_is_explicit_and_provider_neutral(status_code, error, expected):
    assert classify_retryability(status_code=status_code, error=error) is expected


def test_only_distinct_non_retryable_failures_build_the_streak():
    telemetry = ProgressTelemetry(session_id="retry-session")

    def failure(event_id: str, retryability: Retryability) -> TerminalEvent:
        return TerminalEvent(
            event_id=event_id,
            call_id=event_id,
            adapter="anthropic",
            source="tool_executor",
            status=TerminalStatus.FAILURE,
            result={"error": "denied"},
            retryability=retryability,
            error_code=403,
        )

    telemetry.record_terminal_event(
        failure("retryable", Retryability.RETRYABLE), tool_name="web_search"
    )
    telemetry.record_terminal_event(
        failure("unknown", Retryability.UNKNOWN), tool_name="web_search"
    )
    assert telemetry.failure_streak == 0

    telemetry.record_terminal_event(
        failure("nonretry-1", Retryability.NON_RETRYABLE), tool_name="web_search"
    )
    telemetry.record_terminal_event(
        failure("nonretry-1", Retryability.NON_RETRYABLE), tool_name="web_search"
    )
    assert telemetry.failure_streak == 1

    telemetry.record_terminal_event(
        failure("nonretry-2", Retryability.NON_RETRYABLE), tool_name="web_search"
    )
    assert telemetry.failure_streak == 2

    telemetry.record_terminal_event(
        _event("success-reset", result={"value": "changed"}), tool_name="web_search"
    )
    assert telemetry.failure_streak == 0


def test_agent_binds_telemetry_to_final_real_session_before_events(monkeypatch, tmp_path):
    from run_agent import AIAgent

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setattr("run_agent.get_tool_definitions", lambda **_kwargs: [])
    monkeypatch.setattr("run_agent.check_toolset_requirements", lambda: {})
    monkeypatch.setattr("run_agent.OpenAI", _FakeOpenAI)

    agent = AIAgent(
        api_key="test-key",
        base_url="http://test",
        provider="openrouter",
        api_mode="chat_completions",
        session_id="final-session-id",
        max_iterations=1,
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )

    assert agent.session_id == "final-session-id"
    assert agent._progress_telemetry.session_id == agent.session_id
    recorded = agent._progress_telemetry.record_terminal_event(
        _event("first-live-event"), tool_name="read_file"
    )
    assert recorded.replayed is False


@pytest.mark.parametrize(
    "adapter", ["openai-wire", "anthropic", "codex-responses", "gateway"]
)
def test_conversation_guardrail_records_equivalent_provider_terminal_events(adapter):
    from agent.tool_guardrails import ToolCallGuardrailController
    from run_agent import AIAgent

    telemetry = ProgressTelemetry(session_id="provider-session")
    agent = type("Agent", (), {})()
    agent._progress_telemetry = telemetry
    agent._tool_guardrails = ToolCallGuardrailController()
    agent._tool_guardrail_halt_decision = None
    agent.provider = adapter
    agent.api_mode = adapter

    output = AIAgent._append_guardrail_observation(
        agent,
        "read_file",
        {"path": "same.py"},
        "same result",
        failed=False,
        event_id=f"{adapter}:evt-1",
        call_id="call-1",
        adapter=adapter,
        source="conversation_guardrail",
    )

    assert output == "same result"
    assert telemetry.attempt_seq == 1
    assert telemetry.last_call_id == "call-1"
    assert telemetry.last_adapter == adapter
    assert telemetry.last_source == "conversation_guardrail"
    assert telemetry.last_retryability is Retryability.UNKNOWN


def test_retryable_or_unknown_failure_breaks_non_retryable_streak():
    telemetry = ProgressTelemetry(session_id="bound")
    telemetry.record_attempt_completion(
        "web_search",
        {},
        "auth",
        is_failure=True,
        failure_sig="auth",
        retryability=Retryability.NON_RETRYABLE,
    )
    assert telemetry.failure_streak == 1

    telemetry.record_attempt_completion(
        "web_search",
        {},
        "busy",
        is_failure=True,
        failure_sig="busy",
        retryability=Retryability.RETRYABLE,
    )
    assert telemetry.failure_streak == 0

    telemetry.record_attempt_completion(
        "web_search",
        {},
        "opaque",
        is_failure=True,
        failure_sig="opaque",
        retryability=Retryability.UNKNOWN,
    )
    assert telemetry.failure_streak == 0


def test_tool_guardrail_non_retryable_counter_ignores_retryable_and_unknown_failures():
    guard = ToolCallGuardrailController()
    args = {"query": "same"}
    guard.after_call(
        "web_search",
        args,
        "auth",
        failed=True,
        retryability=Retryability.NON_RETRYABLE,
    )
    guard.after_call(
        "web_search",
        args,
        "busy",
        failed=True,
        retryability=Retryability.RETRYABLE,
    )
    guard.after_call(
        "web_search",
        args,
        "opaque",
        failed=True,
        retryability=Retryability.UNKNOWN,
    )

    decision = guard.after_call(
        "web_search",
        args,
        "auth",
        failed=True,
        retryability=Retryability.NON_RETRYABLE,
    )
    assert decision.count == 1


def test_live_observation_layers_terminal_fields_without_router_semantic_changes():
    summary = {
        "api_call_count": 2,
        "attempt_seq": 3,
        "progress_seq": 1,
        "failure_seq": 2,
        "failure_streak": 1,
        "is_non_retryable_failure": True,
        "last_error_code": 401,
        "last_event_id": "evt-auth",
        "last_call_id": "call-auth",
        "last_adapter": "openai-wire",
        "last_source": "tool_executor",
        "last_retryability": "non_retryable",
        "usage": {"input_tokens": 5, "output_tokens": 2, "quality": "measured"},
    }
    agent = SimpleNamespace(
        get_activity_summary=lambda: summary,
        session_id="session-observation",
        provider="custom",
        model="model",
        reasoning_config={},
    )
    runner = SimpleNamespace(
        _running_agents={"conversation": agent},
        _running_agents_ts={"conversation": 10.0},
    )

    observations, _ = _collect_observations(
        runner, now=20.0, assumed_context_tokens=160_000
    )

    observation = observations[0]
    assert observation.failure_seq == 2
    assert observation.failure_streak == 1
    assert observation.is_non_retryable_failure is True
    assert observation.last_event_id == "evt-auth"
    assert observation.last_call_id == "call-auth"
    assert observation.last_adapter == "openai-wire"
    assert observation.last_source == "tool_executor"
    assert observation.last_retryability == "non_retryable"


def test_poll_only_snapshot_repeats_cannot_increment_guard_failure_streak():
    guard = RunawayGuard(
        GuardThresholds(
            repeated_error_limit=2,
            max_calls_per_window=100,
            max_tokens_per_window=10_000,
            max_runtime_seconds=10_000,
        )
    )
    observation = SessionObservation(
        session_id="poll-dedupe",
        started_at=0.0,
        failure_seq=1,
        failure_streak=1,
        is_non_retryable_failure=True,
        error_code=401,
    )

    assert guard.observe(observation, now=1.0) is None
    assert guard.observe(observation, now=2.0) is None
    assert guard.observe(observation, now=3.0) is None


def test_repeated_error_detector_resets_when_status_code_changes():
    guard = RunawayGuard(
        GuardThresholds(
            repeated_error_limit=2,
            max_calls_per_window=100,
            max_tokens_per_window=10_000,
            max_runtime_seconds=10_000,
        )
    )

    def failure(sequence: int, code: int) -> SessionObservation:
        return SessionObservation(
            session_id="same-error-only",
            started_at=0.0,
            attempt_seq=sequence,
            failure_seq=sequence,
            failure_streak=sequence,
            is_non_retryable_failure=True,
            error_code=code,
        )

    assert guard.observe(failure(1, 401), now=1.0) is None
    # A different non-retryable status starts a new same-error streak even
    # though the producer's all-error failure_streak is now two.
    assert guard.observe(failure(2, 403), now=2.0) is None
    trip = guard.observe(failure(3, 403), now=3.0)
    assert trip is not None
    assert trip.reason is TripReason.REPEATED_ERROR
    assert "HTTP 403 repeated 2x" in trip.detail
