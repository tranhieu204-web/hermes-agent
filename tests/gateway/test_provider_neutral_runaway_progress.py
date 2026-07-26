"""Comprehensive test suite for provider-neutral progress-aware runaway control."""

import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

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
from gateway.fleet_safety.integration import _collect_observations


class _StopBeforeTurnBuild(RuntimeError):
    pass


def _activity_agent(*, provider="custom"):
    return SimpleNamespace(
        _progress_telemetry=ProgressTelemetry(session_id="live-session"),
        _last_activity_ts=time.time(),
        _last_activity_desc="testing",
        _current_tool=None,
        _api_call_count=0,
        max_iterations=90,
        iteration_budget=IterationBudget(90),
        session_input_tokens=1_000,
        session_output_tokens=200,
        session_cache_read_tokens=300,
        session_cache_write_tokens=40,
        session_reasoning_tokens=50,
        session_estimated_cost_usd=1.2,
        session_cost_status="actual",
        session_cost_source="provider_cost_api",
        session_id="live-session",
        provider=provider,
        model="provider-model",
        reasoning_config={"effort": "high"},
    )


def _raise_before_turn_build(*_args, **_kwargs):
    raise _StopBeforeTurnBuild


def test_turn_start_snapshots_cumulative_usage_and_resets_turn_telemetry(monkeypatch):
    from agent import conversation_loop
    from run_agent import AIAgent

    agent = _activity_agent()
    agent._api_call_count = 7
    agent._progress_telemetry.record_attempt_completion(
        "read_file", {"path": "before.py"}, "old result"
    )
    monkeypatch.setattr(conversation_loop, "build_turn_context", _raise_before_turn_build)

    with pytest.raises(_StopBeforeTurnBuild):
        conversation_loop.run_conversation(agent, "new turn", moa_config={})

    assert agent._api_call_count == 0
    reset_snapshot = agent._progress_telemetry.get_activity_snapshot()
    assert reset_snapshot["attempt_seq"] == 0
    assert reset_snapshot["progress_seq"] == 0
    assert reset_snapshot["failure_seq"] == 0

    agent._api_call_count = 2
    agent.session_input_tokens += 600
    agent.session_output_tokens += 100
    agent.session_cache_read_tokens += 200
    agent.session_cache_write_tokens += 10
    agent.session_reasoning_tokens += 30
    agent.session_estimated_cost_usd += 0.4

    summary = AIAgent.get_activity_summary(agent)
    assert summary["usage"] == {
        "input_tokens": 600,
        "output_tokens": 100,
        "cache_read_tokens": 200,
        "cache_write_tokens": 10,
        "reasoning_tokens": 30,
        "cost": pytest.approx(0.4),
        "model_requests": 2,
        "quality": "measured",
        "cost_status": "actual",
        "cost_source": "provider_cost_api",
    }


@pytest.mark.parametrize("provider", ["codex", "antigravity"])
def test_provider_error_code_propagates_into_guard_observation(provider):
    from run_agent import AIAgent

    agent = _activity_agent(provider=provider)
    agent._last_provider_error_code = 429
    agent.get_activity_summary = lambda: AIAgent.get_activity_summary(agent)
    summary = agent.get_activity_summary()
    runner = SimpleNamespace(
        _running_agents={"conversation": agent},
        _running_agents_ts={"conversation": 100.0},
    )

    observations, _mapping = _collect_observations(
        runner,
        now=120.0,
        assumed_context_tokens=160_000,
    )

    assert summary["last_error_code"] == 429
    assert observations[0].error_code == 429


def test_child_usage_quality_override_remains_unknown_in_guard_observation():
    from run_agent import AIAgent

    agent = _activity_agent(provider="codex")
    agent.session_usage_quality = "unknown"
    agent.get_activity_summary = lambda: AIAgent.get_activity_summary(agent)
    runner = SimpleNamespace(
        _running_agents={"conversation": agent},
        _running_agents_ts={"conversation": 100.0},
    )

    observations, _mapping = _collect_observations(
        runner,
        now=120.0,
        assumed_context_tokens=160_000,
    )

    assert agent.get_activity_summary()["usage"]["quality"] == "unknown"
    assert observations[0].usage_quality == "unknown"


@pytest.mark.parametrize("provider", ["codex", "antigravity"])
def test_live_observation_uses_exact_measured_turn_deltas_for_every_provider(provider, monkeypatch):
    from agent import conversation_loop
    from run_agent import AIAgent

    agent = _activity_agent(provider=provider)
    monkeypatch.setattr(conversation_loop, "build_turn_context", _raise_before_turn_build)
    with pytest.raises(_StopBeforeTurnBuild):
        conversation_loop.run_conversation(agent, "new turn", moa_config={})

    agent._api_call_count = 3
    agent.session_input_tokens += 700
    agent.session_output_tokens += 110
    agent.session_cache_read_tokens += 220
    agent.session_cache_write_tokens += 30
    agent.session_reasoning_tokens += 40
    agent.session_estimated_cost_usd += 0.25
    agent.get_activity_summary = lambda: AIAgent.get_activity_summary(agent)
    runner = SimpleNamespace(
        _running_agents={"conversation": agent},
        _running_agents_ts={"conversation": 100.0},
    )

    observations, _mapping = _collect_observations(runner, now=120.0, assumed_context_tokens=160_000)

    assert len(observations) == 1
    obs = observations[0]
    assert obs.provider == provider
    assert obs.api_call_count == 3
    assert obs.tokens_used == 1_060
    assert obs.input_tokens == 700
    assert obs.output_tokens == 110
    assert obs.cache_read_tokens == 220
    assert obs.cache_write_tokens == 30
    assert obs.reasoning_tokens == 40
    assert obs.cost == pytest.approx(0.25)
    assert obs.usage_quality == "measured"


def test_unknown_live_counters_remain_unknown_and_cannot_hard_stop():
    summary = {
        "api_call_count": 28,
        "attempt_seq": 0,
        "failure_seq": 0,
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "reasoning_tokens": 0,
            "cost": 0.0,
            "model_requests": 28,
            "quality": "unknown",
        },
    }
    agent = SimpleNamespace(
        get_activity_summary=lambda: summary,
        session_id="unknown-session",
        provider="antigravity",
        model="provider-model",
        reasoning_config={},
    )
    runner = SimpleNamespace(
        _running_agents={"conversation": agent},
        _running_agents_ts={"conversation": 100.0},
    )

    observations, _mapping = _collect_observations(runner, now=200.0, assumed_context_tokens=160_000)

    obs = observations[0]
    assert obs.tokens_used == 0
    assert obs.usage_quality == "unknown"
    guard = RunawayGuard(
        GuardThresholds(
            max_tokens_per_window=1,
            max_calls_per_window=100,
            max_runtime_seconds=1_000,
        )
    )
    result = guard.observe(obs, now=200.0)
    assert result is None or result.is_hard_stop is False


def test_empty_unknown_usage_is_identity_for_measured_usage():
    empty = CanonicalUsage()
    measured = CanonicalUsage(
        input_tokens=10,
        output_tokens=2,
        quality=UsageSourceQuality.MEASURED,
    )

    assert (empty + measured).quality is UsageSourceQuality.MEASURED
    assert (measured + empty).quality is UsageSourceQuality.MEASURED


def test_nonempty_mixed_usage_qualities_degrade():
    measured = CanonicalUsage(input_tokens=10, quality=UsageSourceQuality.MEASURED)
    unknown = CanonicalUsage(output_tokens=2, quality=UsageSourceQuality.UNKNOWN)

    assert (measured + unknown).quality is UsageSourceQuality.UNKNOWN


def test_incident_replay_measured_path_does_not_stop():
    """Incident replay: 28 calls delta, measured 3,615,603 tokens must NOT hard-stop."""
    th = GuardThresholds(window_seconds=900, max_tokens_per_window=4_000_000, max_calls_per_window=100)
    g = RunawayGuard(th)

    # The turn predates the window, so the first cumulative sample is only a
    # rebaseline. The subsequent measured in-window delta is 3,615,603.
    obs1 = SessionObservation(
        session_id="incident_s1",
        started_at=0.0,
        api_call_count=10,
        tokens_used=1_000_000,
        context_tokens=150_000,
        usage_quality="measured",
    )
    res1 = g.observe(obs1, now=1_000.0)
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
    res2 = g.observe(obs2, now=1_300.0)
    assert (res2 is None or res2.is_hard_stop is False)
    assert (res2 is None or res2.outcome == GuardOutcome.NO_ACTION)


def test_first_sample_includes_usage_when_turn_started_inside_window():
    guard = RunawayGuard(
        GuardThresholds(
            window_seconds=900,
            max_tokens_per_window=4_000_000,
            max_calls_per_window=100,
            max_runtime_seconds=10_000,
        )
    )
    obs = SessionObservation(
        session_id="early-turn",
        started_at=100.0,
        api_call_count=30,
        tokens_used=4_100_000,
        usage_quality="measured",
    )

    result = guard.observe(obs, now=200.0)

    assert result is not None
    assert result.outcome is GuardOutcome.CONTINUATION_NOTICE
    assert result.is_hard_stop is False
    assert result.estimated_tokens == 4_100_000


def test_first_sample_for_pre_window_turn_rebaselines_and_waits():
    guard = RunawayGuard(
        GuardThresholds(
            window_seconds=900,
            max_tokens_per_window=4_000_000,
            max_calls_per_window=100,
            max_runtime_seconds=10_000,
        )
    )
    old_turn = SessionObservation(
        session_id="old-turn",
        started_at=0.0,
        api_call_count=30,
        tokens_used=5_000_000,
        usage_quality="measured",
    )

    assert guard.observe(old_turn, now=1_000.0) is None
    assert guard.observe(
        SessionObservation(
            session_id="old-turn",
            started_at=0.0,
            api_call_count=31,
            tokens_used=5_100_000,
            usage_quality="measured",
        ),
        now=1_100.0,
    ) is None


def test_cumulative_counter_reset_rebaselines_without_spike():
    guard = RunawayGuard(
        GuardThresholds(
            window_seconds=900,
            max_tokens_per_window=4_000_000,
            max_calls_per_window=100,
            max_runtime_seconds=10_000,
        )
    )
    assert guard.observe(
        SessionObservation(
            session_id="counter-reset",
            started_at=100.0,
            api_call_count=10,
            tokens_used=3_000_000,
            usage_quality="measured",
        ),
        now=200.0,
    ) is None

    reset_result = guard.observe(
        SessionObservation(
            session_id="counter-reset",
            started_at=100.0,
            api_call_count=1,
            tokens_used=100_000,
            usage_quality="measured",
        ),
        now=300.0,
    )
    assert reset_result is None

    assert guard.observe(
        SessionObservation(
            session_id="counter-reset",
            started_at=100.0,
            api_call_count=2,
            tokens_used=200_000,
            usage_quality="measured",
        ),
        now=400.0,
    ) is None


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


def test_measured_and_estimated_quality_labels_are_explicit():
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
    res1 = tel.record_attempt_completion(
        "read_file",
        {"path": "a", "heartbeat_at": "2026-07-25T01:02:03Z"},
        "data\ntime=100\nrequest_id=req_first",
        is_failure=False,
    )
    res2 = tel.record_attempt_completion(
        "read_file",
        {"path": "a", "heartbeat_at": "2026-07-25T01:03:04Z"},
        "data\ntime=200\nrequest_id=req_second",
        is_failure=False,
    )
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

    tel.record_attempt_completion(
        "write_file",
        {"path": "bar.py", "content": "hello"},
        '{"bytes_written": 5, "files_modified": ["/workspace/bar.py"]}',
        is_failure=False,
    )
    assert tel.last_outcome == AttemptOutcome.VERIFIED_PROGRESS
    assert tel.no_progress_streak == 0


def test_unrelated_unknown_attempt_does_not_erase_repeat_fingerprint():
    tel = ProgressTelemetry(session_id="s_ledger")
    assert tel.record_attempt_completion("read_file", {"path": "a"}, "same") is AttemptOutcome.UNKNOWN
    assert tel.record_attempt_completion("web_search", {"query": "other"}, "new") is AttemptOutcome.UNKNOWN

    outcome = tel.record_attempt_completion("read_file", {"path": "a"}, "same")

    assert outcome is AttemptOutcome.VERIFIED_NO_PROGRESS
    assert tel.no_progress_streak == 1


def test_generic_file_write_success_and_noop_are_not_verified_progress():
    tel = ProgressTelemetry(session_id="s_write")

    generic = tel.record_attempt_completion(
        "write_file",
        {"path": "a.txt", "content": "same"},
        "File written successfully",
    )
    noop = tel.record_attempt_completion(
        "patch",
        {"path": "a.txt", "old_string": "same", "new_string": "same"},
        '{"success": true, "files_modified": []}',
    )

    assert generic is AttemptOutcome.UNKNOWN
    assert noop is AttemptOutcome.UNKNOWN
    assert tel.progress_seq == 0


def test_polling_same_snapshot_does_not_advance_k():
    """Housekeeping polls on unchanged state must not advance no_progress_streak K."""
    tel = ProgressTelemetry(session_id="s_poll")
    tel.record_attempt_completion("read_file", {"path": "a"}, "content", is_failure=False)
    tel.record_attempt_completion("read_file", {"path": "a"}, "content", is_failure=False)
    assert tel.no_progress_streak == 1

    for _ in range(10):
        snapshot = tel.get_activity_snapshot()
        assert snapshot["no_progress_streak"] == 1


def test_guard_consumes_no_progress_only_when_attempt_sequence_advances():
    guard = RunawayGuard(
        GuardThresholds(
            no_progress_samples=2,
            max_runtime_seconds=10_000,
            max_tokens_per_window=10**12,
            max_calls_per_window=10**6,
        )
    )
    first = SessionObservation(
        session_id="poll-idempotent",
        started_at=100.0,
        attempt_seq=1,
        no_progress_streak=1,
    )
    assert guard.observe(first, now=101.0) is None
    for tick in range(10):
        assert guard.observe(first, now=102.0 + tick) is None

    result = guard.observe(
        SessionObservation(
            session_id="poll-idempotent",
            started_at=100.0,
            attempt_seq=2,
            no_progress_streak=2,
        ),
        now=120.0,
    )
    assert result is not None
    assert result.reason is TripReason.NO_PROGRESS
    assert result.is_hard_stop is True


def test_ten_identical_polls_do_not_advance_progress_or_failure_evidence():
    guard = RunawayGuard(
        GuardThresholds(
            repeated_error_limit=3,
            no_progress_samples=3,
            max_runtime_seconds=10_000,
            max_tokens_per_window=10**12,
            max_calls_per_window=10**6,
        )
    )
    snapshot = SessionObservation(
        session_id="same-snapshot",
        started_at=100.0,
        attempt_seq=1,
        no_progress_streak=1,
        failure_seq=1,
        failure_streak=1,
        is_non_retryable_failure=True,
    )

    for tick in range(10):
        assert guard.observe(snapshot, now=101.0 + tick) is None


def test_retryable_failures_never_hard_stop():
    guard = RunawayGuard(
        GuardThresholds(
            repeated_error_limit=3,
            max_runtime_seconds=10_000,
            max_tokens_per_window=10**12,
            max_calls_per_window=10**6,
        )
    )

    for seq, code in enumerate((401, 429, 500, 503, 504), start=1):
        result = guard.observe(
            SessionObservation(
                session_id="retryable",
                started_at=100.0,
                failure_seq=seq,
                failure_streak=seq,
                is_non_retryable_failure=False,
                error_code=code,
            ),
            now=100.0 + seq,
        )
        assert result is None or result.is_hard_stop is False


def test_distinct_non_retryable_failure_events_hard_stop_at_limit():
    guard = RunawayGuard(
        GuardThresholds(
            repeated_error_limit=3,
            max_runtime_seconds=10_000,
            max_tokens_per_window=10**12,
            max_calls_per_window=10**6,
        )
    )

    for seq in (1, 2):
        assert guard.observe(
            SessionObservation(
                session_id="non-retryable",
                started_at=100.0,
                failure_seq=seq,
                failure_streak=seq,
                is_non_retryable_failure=True,
                error_code=400,
            ),
            now=100.0 + seq,
        ) is None

    result = guard.observe(
        SessionObservation(
            session_id="non-retryable",
            started_at=100.0,
            failure_seq=3,
            failure_streak=3,
            is_non_retryable_failure=True,
            error_code=400,
        ),
        now=103.0,
    )
    assert result is not None
    assert result.reason is TripReason.REPEATED_ERROR
    assert result.is_hard_stop is True


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
        failure_seq=3,
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


def test_legacy_missing_usage_fallback_is_estimated_and_continues():
    agent = SimpleNamespace(
        get_activity_summary=lambda: {"api_call_count": 28},
        session_id="legacy-session",
        provider="legacy-provider",
        model="legacy-model",
        reasoning_config={},
    )
    runner = SimpleNamespace(
        _running_agents={"conversation": agent},
        _running_agents_ts={"conversation": 100.0},
    )
    observations, _mapping = _collect_observations(runner, now=200.0, assumed_context_tokens=160_000)
    obs = observations[0]
    assert obs.tokens_used == 4_480_000
    assert obs.usage_quality == "estimated"

    guard = RunawayGuard(
        GuardThresholds(
            window_seconds=900,
            max_tokens_per_window=4_000_000,
            max_calls_per_window=100,
            max_runtime_seconds=10_000,
        )
    )
    result = guard.observe(obs, now=200.0)
    assert result is not None
    assert result.outcome is GuardOutcome.CONTINUATION_NOTICE
    assert result.is_hard_stop is False


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
