"""Unit tests for the runaway-loop detector.

Deterministic: every test drives :meth:`RunawayGuard.observe` with an explicit
``now`` and asserts exactly which sample trips, on which reason. One test per
trip condition (rate/error/wall-clock/no-progress) plus latching, priority,
disable, and pruning.
"""

import pytest

from gateway.fleet_safety.deadloop_guard import (
    GuardOutcome,
    GuardThresholds,
    RunawayGuard,
    SessionObservation,
    TripReason,
)


def _obs(session_id="s1", *, started_at=0.0, calls=0, tokens=0, context=0,
         state="x", error=None, attempt_seq=0, no_progress_streak=0,
         failure_seq=0, failure_streak=0, is_non_retryable_failure=False,
         provider="grok", model="grok-4.5", effort="max"):
    return SessionObservation(
        session_id=session_id,
        started_at=started_at,
        api_call_count=calls,
        tokens_used=tokens,
        context_tokens=context,
        state_hash=state,
        error_code=error,
        attempt_seq=attempt_seq,
        no_progress_streak=no_progress_streak,
        failure_seq=failure_seq,
        failure_streak=failure_streak,
        is_non_retryable_failure=is_non_retryable_failure,
        provider=provider,
        model=model,
        effort=effort,
    )


# -- (a) token-rate -----------------------------------------------------------


def test_token_rate_emits_continuation_within_window():
    th = GuardThresholds(window_seconds=900, max_tokens_per_window=4_000_000,
                         max_calls_per_window=10_000, max_runtime_seconds=1e9)
    g = RunawayGuard(th)
    # First sample seeds the window baseline; no delta yet.
    assert g.observe(_obs(tokens=0, calls=0), now=0.0) is None
    # +5M tokens over 300s (< window) → over the 4M ceiling.
    trip = g.observe(_obs(tokens=5_000_000, calls=30), now=300.0)
    assert trip is not None
    assert trip.reason is TripReason.TOKEN_RATE
    assert trip.outcome is GuardOutcome.CONTINUATION_NOTICE
    assert trip.is_hard_stop is False
    assert "tokens" in trip.detail
    assert trip.estimated_tokens == 5_000_000


def test_token_rate_stays_quiet_below_ceiling():
    th = GuardThresholds(window_seconds=900, max_tokens_per_window=4_000_000,
                         max_calls_per_window=10_000, max_runtime_seconds=1e9)
    g = RunawayGuard(th)
    g.observe(_obs(tokens=0), now=0.0)
    assert g.observe(_obs(tokens=1_000_000), now=300.0) is None
    assert g.observe(_obs(tokens=3_500_000), now=600.0) is None


def test_rate_window_prunes_old_samples():
    # A slow burn that never exceeds the ceiling *within any 900s window* must
    # not trip, even though the lifetime total is huge.
    th = GuardThresholds(window_seconds=900, max_tokens_per_window=4_000_000,
                         max_calls_per_window=10_000, max_runtime_seconds=1e9)
    g = RunawayGuard(th)
    tokens = 0
    tripped = False
    for i in range(20):
        tokens += 1_000_000  # 1M per 600s step = well under 4M/900s
        t = g.observe(_obs(tokens=tokens), now=i * 600.0)
        tripped = tripped or (t is not None)
    assert not tripped


def test_counter_reset_starts_a_new_rate_epoch():
    th = GuardThresholds(
        max_calls_per_window=100,
        max_tokens_per_window=4_000_000,
        max_runtime_seconds=1e9,
    )
    guard = RunawayGuard(th)
    assert guard.observe(_obs(calls=90, tokens=3_000_000), now=0.0) is None
    assert guard.observe(_obs(calls=2, tokens=20_000), now=1.0) is None
    assert guard.observe(_obs(calls=20, tokens=500_000), now=2.0) is None


# -- (a) call-rate ------------------------------------------------------------


def test_call_rate_emits_continuation():
    th = GuardThresholds(window_seconds=900, max_calls_per_window=100,
                         max_tokens_per_window=10**15, max_runtime_seconds=1e9)
    g = RunawayGuard(th)
    g.observe(_obs(calls=0), now=0.0)
    trip = g.observe(_obs(calls=101), now=120.0)
    assert trip is not None
    assert trip.reason is TripReason.CALL_RATE
    assert trip.outcome is GuardOutcome.CONTINUATION_NOTICE


# -- (b) repeated non-retryable error ----------------------------------------


def test_repeated_non_retryable_error_trips_on_kth():
    th = GuardThresholds(repeated_error_limit=3, max_runtime_seconds=1e9,
                         max_tokens_per_window=10**15, max_calls_per_window=10**9)
    g = RunawayGuard(th)
    assert g.observe(_obs(
        error=400, failure_seq=1, failure_streak=1,
        is_non_retryable_failure=True,
    ), now=0.0) is None
    assert g.observe(_obs(
        error=400, failure_seq=2, failure_streak=2,
        is_non_retryable_failure=True,
    ), now=1.0) is None
    trip = g.observe(_obs(
        error=400, failure_seq=3, failure_streak=3,
        is_non_retryable_failure=True,
    ), now=2.0)
    assert trip is not None
    assert trip.reason is TripReason.REPEATED_ERROR
    assert trip.outcome is GuardOutcome.VERIFIED_HARD_STOP
    assert trip.is_hard_stop is True
    assert "400" in trip.detail


def test_error_streak_resets_on_clean_sample():
    th = GuardThresholds(repeated_error_limit=3, max_runtime_seconds=1e9,
                         max_tokens_per_window=10**15, max_calls_per_window=10**9)
    g = RunawayGuard(th)
    g.observe(_obs(
        error=400, failure_seq=1, failure_streak=1,
        is_non_retryable_failure=True,
    ), now=0.0)
    g.observe(_obs(
        error=400, failure_seq=2, failure_streak=2,
        is_non_retryable_failure=True,
    ), now=1.0)
    g.observe(_obs(error=None, failure_seq=3), now=2.0)
    assert g.observe(_obs(
        error=400, failure_seq=4, failure_streak=1,
        is_non_retryable_failure=True,
    ), now=3.0) is None
    assert g.observe(_obs(
        error=400, failure_seq=5, failure_streak=2,
        is_non_retryable_failure=True,
    ), now=4.0) is None


def test_error_streak_resets_on_different_code():
    th = GuardThresholds(repeated_error_limit=3, max_runtime_seconds=1e9,
                         max_tokens_per_window=10**15, max_calls_per_window=10**9)
    g = RunawayGuard(th)
    g.observe(_obs(
        error=400, failure_seq=1, failure_streak=1,
        is_non_retryable_failure=True,
    ), now=0.0)
    g.observe(_obs(
        error=400, failure_seq=2, failure_streak=2,
        is_non_retryable_failure=True,
    ), now=1.0)
    assert g.observe(_obs(
        error=429, failure_seq=3, failure_streak=1,
        is_non_retryable_failure=False,
    ), now=2.0) is None
    assert g.observe(_obs(
        error=429, failure_seq=4, failure_streak=2,
        is_non_retryable_failure=False,
    ), now=3.0) is None


# -- (c) wall-clock -----------------------------------------------------------


def test_wall_clock_emits_continuation_past_cap():
    th = GuardThresholds(max_runtime_seconds=3600, max_tokens_per_window=10**15,
                         max_calls_per_window=10**9)
    g = RunawayGuard(th)
    assert g.observe(_obs(started_at=0.0), now=1800.0) is None
    trip = g.observe(_obs(started_at=0.0), now=3600.1)
    assert trip is not None
    assert trip.reason is TripReason.WALL_CLOCK
    assert trip.outcome is GuardOutcome.CONTINUATION_NOTICE
    assert trip.is_hard_stop is False
    assert trip.runtime_seconds == pytest.approx(3600.1)


# -- (d) huge context, no forward progress -----------------------------------


def test_producer_confirmed_no_progress_trips_on_new_attempts():
    th = GuardThresholds(huge_context_tokens=150_000, no_progress_samples=3,
                         max_runtime_seconds=1e9, max_tokens_per_window=10**15,
                         max_calls_per_window=10**9)
    g = RunawayGuard(th)
    assert g.observe(_obs(
        context=160_000, attempt_seq=1, no_progress_streak=1,
    ), now=0.0) is None
    assert g.observe(_obs(
        context=160_000, attempt_seq=2, no_progress_streak=2,
    ), now=60.0) is None
    trip = g.observe(_obs(
        context=160_000, attempt_seq=3, no_progress_streak=3,
    ), now=120.0)
    assert trip is not None
    assert trip.reason is TripReason.NO_PROGRESS
    assert trip.outcome is GuardOutcome.VERIFIED_HARD_STOP
    assert trip.is_hard_stop is True


def test_no_progress_resets_when_producer_streak_resets():
    th = GuardThresholds(huge_context_tokens=150_000, no_progress_samples=3,
                         max_runtime_seconds=1e9, max_tokens_per_window=10**15,
                         max_calls_per_window=10**9)
    g = RunawayGuard(th)
    g.observe(_obs(attempt_seq=1, no_progress_streak=1), now=0.0)
    g.observe(_obs(attempt_seq=2, no_progress_streak=2), now=1.0)
    g.observe(_obs(attempt_seq=3, no_progress_streak=0), now=2.0)
    g.observe(_obs(attempt_seq=4, no_progress_streak=1), now=3.0)
    assert g.observe(_obs(attempt_seq=5, no_progress_streak=2), now=4.0) is None


def test_unchanged_state_hash_never_manufactures_no_progress():
    th = GuardThresholds(huge_context_tokens=150_000, no_progress_samples=2,
                         max_runtime_seconds=1e9, max_tokens_per_window=10**15,
                         max_calls_per_window=10**9)
    g = RunawayGuard(th)
    for i in range(5):
        assert g.observe(_obs(
            context=200_000,
            state="frozen",
            calls=i + 1,
        ), now=float(i)) is None


def test_producer_no_progress_streak_is_poll_idempotent_and_hard_stops_at_limit():
    th = GuardThresholds(
        no_progress_samples=3,
        max_runtime_seconds=1e9,
        max_tokens_per_window=10**15,
        max_calls_per_window=10**9,
    )
    guard = RunawayGuard(th)
    base = dict(
        session_id="producer-streak",
        started_at=0.0,
        api_call_count=2,
        tokens_used=100,
        attempt_seq=2,
        progress_seq=0,
        no_progress_streak=2,
    )
    for now in range(10):
        assert guard.observe(SessionObservation(**base), now=float(now)) is None

    trip = guard.observe(
        SessionObservation(
            **{
                **base,
                "api_call_count": 3,
                "attempt_seq": 3,
                "no_progress_streak": 3,
            }
        ),
        now=10.0,
    )
    assert trip is not None
    assert trip.reason is TripReason.NO_PROGRESS
    assert trip.outcome is GuardOutcome.VERIFIED_HARD_STOP


# -- latching / priority / disable / pruning ---------------------------------


def test_continuation_notice_deduplicates_for_same_threshold_episode():
    th = GuardThresholds(max_runtime_seconds=100, max_tokens_per_window=10**15,
                         max_calls_per_window=10**9)
    g = RunawayGuard(th)
    first = g.observe(_obs(started_at=0.0), now=200.0)
    assert first.reason is TripReason.WALL_CLOCK
    assert first.is_hard_stop is False
    assert g.observe(_obs(started_at=0.0), now=260.0) is None


def test_wall_clock_notice_takes_priority_over_rate():
    th = GuardThresholds(max_runtime_seconds=100, window_seconds=900,
                         max_tokens_per_window=1, max_calls_per_window=1)
    g = RunawayGuard(th)
    g.observe(_obs(started_at=0.0, tokens=0, calls=0), now=0.0)
    # both wall-clock and rate are exceeded; wall-clock wins by priority
    trip = g.observe(_obs(started_at=0.0, tokens=10**9, calls=10**6), now=200.0)
    assert trip.reason is TripReason.WALL_CLOCK
    assert trip.is_hard_stop is False


def test_disabled_guard_never_trips():
    th = GuardThresholds(enabled=False, max_runtime_seconds=1)
    g = RunawayGuard(th)
    assert g.observe(_obs(started_at=0.0), now=10_000.0) is None


def test_independent_sessions_tracked_separately():
    th = GuardThresholds(max_runtime_seconds=100, max_tokens_per_window=10**15,
                         max_calls_per_window=10**9)
    g = RunawayGuard(th)
    assert g.observe(_obs("a", started_at=0.0), now=50.0) is None
    assert g.observe(_obs("b", started_at=0.0), now=50.0) is None
    notice = g.observe(_obs("a", started_at=0.0), now=200.0)
    assert notice.session_id == "a"
    assert notice.is_hard_stop is False
    assert g.observe(_obs("b", started_at=0.0), now=90.0) is None


def test_forget_releases_notice_latched_state():
    th = GuardThresholds(max_runtime_seconds=100, max_tokens_per_window=10**15,
                         max_calls_per_window=10**9)
    g = RunawayGuard(th)
    g.observe(_obs("a", started_at=0.0), now=200.0)
    assert "a" in g.active_session_ids()
    g.forget("a")
    assert "a" not in g.active_session_ids()
    # A fresh session under the same id starts clean and can notify again.
    g.observe(_obs("a", started_at=1000.0), now=1000.0)
    assert g.observe(_obs("a", started_at=1000.0), now=1200.0).reason is TripReason.WALL_CLOCK


def test_thresholds_from_config_coerces_and_defaults():
    th = GuardThresholds.from_config({
        "window_seconds": "900",           # str coerces to float
        "max_calls_per_window": 50,
        "max_tokens_per_window": None,     # None → default
        "bogus": "ignored",
    })
    assert th.window_seconds == 900.0
    assert th.max_calls_per_window == 50
    assert th.max_tokens_per_window == GuardThresholds().max_tokens_per_window


def test_thresholds_from_config_survives_garbage():
    th = GuardThresholds.from_config({"max_runtime_seconds": "not-a-number"})
    assert th.max_runtime_seconds == GuardThresholds().max_runtime_seconds


def test_thresholds_reject_nonpositive_nonfinite_and_string_enabled_values():
    defaults = GuardThresholds()
    th = GuardThresholds.from_config(
        {
            "window_seconds": 0,
            "max_calls_per_window": -1,
            "max_runtime_seconds": "nan",
            "enabled": "false",
        }
    )
    assert th.window_seconds == defaults.window_seconds
    assert th.max_calls_per_window == defaults.max_calls_per_window
    assert th.max_runtime_seconds == defaults.max_runtime_seconds
    assert th.enabled is defaults.enabled
