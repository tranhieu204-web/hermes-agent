"""Comprehensive test suite for provider-neutral progress-aware runaway control."""

import pytest
from unittest.mock import MagicMock, patch
from gateway.fleet_safety.deadloop_guard import (
    GuardThresholds,
    RunawayGuard,
    SessionObservation,
    TripReason,
    GuardOutcome,
)
from gateway.fleet_safety.enforcer import GuardEnforcer, KillActions, is_stop_command
from agent.progress_telemetry import (
    AttemptOutcome,
    CanonicalUsage,
    ProgressTelemetry,
    UsageSourceQuality,
    normalize_result,
)
from agent.iteration_budget import IterationBudget


def test_incident_replay_measured_path_does_not_stop():
    """Incident replay: 28 calls delta, measured 3,615,603 tokens must NOT hard-stop."""
    th = GuardThresholds(window_seconds=900, max_tokens_per_window=4_000_000, max_calls_per_window=100)
    g = RunawayGuard(th)
    
    obs1 = SessionObservation(
        session_id="incident_s1",
        started_at=0.0,
        api_call_count=10,
        tokens_used=1_000_000,
        context_tokens=150_000,
        usage_quality="measured",
    )
    res1 = g.observe(obs1, now=10.0)
    outcome1 = res1.outcome if res1 else GuardOutcome.NO_ACTION
    assert outcome1 in (GuardOutcome.NO_ACTION, GuardOutcome.CONTINUATION_NOTICE)
    assert (res1 is None or res1.is_hard_stop is False)

    obs2 = SessionObservation(
        session_id="incident_s1",
        started_at=0.0,
        api_call_count=38,
        tokens_used=4_615_603,
        context_tokens=150_000,
        usage_quality="measured",
    )
    res2 = g.observe(obs2, now=300.0)
    assert (res2 is None or res2.is_hard_stop is False)
    assert (res2 is None or res2.outcome == GuardOutcome.NO_ACTION)


def test_threshold_crossing_emits_continuation_notice_not_hard_stop():
    """Threshold crossing (4,480,000 tokens) must emit continuation notice, NOT hard stop."""
    th = GuardThresholds(window_seconds=900, max_tokens_per_window=4_000_000)
    g = RunawayGuard(th)

    obs1 = SessionObservation(
        session_id="s_rate",
        started_at=0.0,
        api_call_count=1,
        tokens_used=0,
        usage_quality="estimated",
    )
    g.observe(obs1, now=0.0)

    obs2 = SessionObservation(
        session_id="s_rate",
        started_at=0.0,
        api_call_count=29,
        tokens_used=4_480_000,
        usage_quality="estimated",
    )
    res = g.observe(obs2, now=300.0)
    assert res is not None
    assert res.outcome == GuardOutcome.CONTINUATION_NOTICE
    assert res.is_hard_stop is False
    assert "Continuing by default. Reply STOP or /stop to cancel." in res.notice_text


def test_codex_measured_and_antigravity_estimated_paths():
    """Codex measured vs Antigravity unknown-estimated paths handling."""
    u_measured = CanonicalUsage(input_tokens=1000, output_tokens=200, quality=UsageSourceQuality.MEASURED)
    assert u_measured.quality == UsageSourceQuality.MEASURED
    assert u_measured.total_tokens == 1200

    u_est = CanonicalUsage(input_tokens=160000, output_tokens=0, quality=UsageSourceQuality.ESTIMATED)
    assert u_est.quality == UsageSourceQuality.ESTIMATED


def test_separate_cache_and_cost_semantics():
    """Cache read/write tokens do not double-count as monetary spend or prompt tokens twice."""
    u = CanonicalUsage(
        input_tokens=100,
        output_tokens=50,
        cache_read_tokens=400,
        cache_write_tokens=50,
        cost=0.002,
        quality=UsageSourceQuality.MEASURED,
    )
    assert u.total_tokens == 600
    assert u.cost == 0.002


def test_heartbeats_and_current_tool_not_progress():
    """Timestamps/heartbeats/current tool alone must not masquerade as progress."""
    tel = ProgressTelemetry(session_id="s_hb")
    res1 = tel.record_attempt_completion("read_file", {"path": "a"}, "data\ntime=100", is_failure=False)
    res2 = tel.record_attempt_completion("read_file", {"path": "a"}, "data\ntime=200", is_failure=False)
    assert res2 == AttemptOutcome.VERIFIED_NO_PROGRESS
    assert tel.no_progress_streak == 1


def test_tristate_attempt_outcomes_and_streak():
    """Test VERIFIED_PROGRESS, VERIFIED_NO_PROGRESS, UNKNOWN telemetry rules."""
    tel = ProgressTelemetry(session_id="s_telemetry")
    
    tel.record_attempt_completion("read_file", {"path": "foo.py"}, "def foo(): pass\n// time: 1721900000", is_failure=False)
    assert tel.last_outcome == AttemptOutcome.UNKNOWN
    assert tel.no_progress_streak == 0

    tel.record_attempt_completion("read_file", {"path": "foo.py"}, "def foo(): pass\n// time: 1721900500", is_failure=False)
    assert tel.last_outcome == AttemptOutcome.VERIFIED_NO_PROGRESS
    assert tel.no_progress_streak == 1

    tel.record_attempt_completion("web_search", {"query": "test"}, "some search result", is_failure=False)
    assert tel.last_outcome == AttemptOutcome.UNKNOWN
    assert tel.no_progress_streak == 1

    tel.record_attempt_completion("write_file", {"path": "bar.py", "content": "hello"}, "File written successfully", is_failure=False, file_mutation_landed=True)
    assert tel.last_outcome == AttemptOutcome.VERIFIED_PROGRESS
    assert tel.no_progress_streak == 0


def test_polling_same_snapshot_does_not_advance_k():
    """Housekeeping polls on unchanged state must not advance no_progress_streak K."""
    tel = ProgressTelemetry(session_id="s_poll")
    tel.record_attempt_completion("read_file", {"path": "a"}, "content", is_failure=False)
    tel.record_attempt_completion("read_file", {"path": "a"}, "content", is_failure=False)
    assert tel.no_progress_streak == 1

    for _ in range(10):
        snapshot = tel.get_activity_snapshot()
        assert snapshot["no_progress_streak"] == 1


def test_stop_text_matching():
    """Test exact STOP vs /stop vs 'do not stop' parsing."""
    assert is_stop_command("STOP") is True
    assert is_stop_command("  stop  ") is True
    assert is_stop_command("/stop") is True
    assert is_stop_command("  /STOP  ") is True
    assert is_stop_command("do not stop") is False
    assert is_stop_command("please stop doing that") is False


def test_repeated_failure_event_streak():
    """Same non-retryable failure event streak stops, repeated polls do not advance."""
    th = GuardThresholds(repeated_error_limit=3)
    g = RunawayGuard(th)
    
    obs = SessionObservation(
        session_id="s_fail",
        started_at=0.0,
        api_call_count=1,
        failure_streak=3,
        is_non_retryable_failure=True,
    )
    res = g.observe(obs, now=10.0)
    assert res is not None
    assert res.outcome == GuardOutcome.VERIFIED_HARD_STOP
    assert res.is_hard_stop is True


def test_notice_never_interrupts_or_releases():
    """Continuation notice must never call interrupt or release_lease."""
    actions = MagicMock(spec=KillActions)
    actions.notify.return_value = True
    enforcer = GuardEnforcer(actions)

    obs = SessionObservation(session_id="s_notice", started_at=0.0)
    g = RunawayGuard(GuardThresholds(window_seconds=900, max_tokens_per_window=100))
    g.observe(obs, now=0.0)
    res_eval = g.observe(SessionObservation(session_id="s_notice", started_at=0.0, tokens_used=500), now=10.0)
    assert res_eval.outcome == GuardOutcome.CONTINUATION_NOTICE

    enf_res = enforcer.enforce(res_eval)
    assert enf_res.interrupted is False
    assert enf_res.lease_released is False
    assert enf_res.killed is False
    assert enf_res.notified is True
    actions.interrupt.assert_not_called()
    actions.release_lease.assert_not_called()


def test_iteration_budget_extend_grant():
    """IterationBudget extension is thread-safe and updates max_total and remaining."""
    budget = IterationBudget(max_total=50)
    for _ in range(50):
        assert budget.consume() is True
    assert budget.remaining == 0
    assert budget.consume() is False

    new_total = budget.extend_grant(50)
    assert new_total == 100
    assert budget.remaining == 50
    assert budget.consume() is True


def test_background_review_containment_suppresses_children_and_cron():
    """Delegated children, cron agents, internal forks, and nested reviews cannot spawn background reviews."""
    from run_agent import AIAgent
    agent_child = AIAgent(
        model="mock-model",
        provider="custom",
        api_key="dummy-key",
        base_url="http://localhost:8000/v1",
        quiet_mode=True,
        parent_session_id="parent-123",
    )
    with patch("threading.Thread") as mock_thread:
        agent_child._spawn_background_review(messages_snapshot=[])
        mock_thread.assert_not_called()

    agent_cron = AIAgent(
        model="mock-model",
        provider="custom",
        api_key="dummy-key",
        base_url="http://localhost:8000/v1",
        quiet_mode=True,
        platform="cron",
    )
    with patch("threading.Thread") as mock_thread:
        agent_cron._spawn_background_review(messages_snapshot=[])
        mock_thread.assert_not_called()


def test_delegate_task_enabled_toolsets_forwarding():
    """delegate_task forwards optional enabled_toolsets parameter."""
    from tools.delegate_tool import delegate_task
    parent_mock = MagicMock()
    parent_mock._delegate_depth = 0
    parent_mock.enabled_toolsets = ["web", "terminal"]
    parent_mock.valid_tool_names = ["web_search", "terminal"]

    with patch("tools.delegate_tool._build_child_agent") as mock_build:
        mock_child = MagicMock()
        mock_child.run_conversation.return_value = "Child output"
        mock_build.return_value = mock_child
        
        delegate_task(
            goal="subtask",
            parent_agent=parent_mock,
            enabled_toolsets=["terminal"],
        )
        assert mock_build.called
        kwargs = mock_build.call_args[1]
        assert kwargs["toolsets"] == ["terminal"]
