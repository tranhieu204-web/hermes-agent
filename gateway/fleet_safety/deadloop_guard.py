"""Runaway-loop detector for active agent sessions — provider-neutral & progress-aware.

The detector watches a stream of per-session :class:`SessionObservation`
samples (one per housekeeping tick) and evaluates runaway signals:

  (a) Rate / Runtime / Call thresholds: Emit CONTINUATION_NOTICE and continue execution by default.
  (b) Repeated non-recoverable error streak (K consecutive failures): Hard stop.
  (c) Verified No-Progress streak (M consecutive verified no-progress attempts): Hard stop.

Only verified no-progress or repeated non-recoverable failures hard stop work.
Threshold crossings emit a continuation notice and continue by default.
"""

from __future__ import annotations

import enum
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, Optional


class TripReason(str, enum.Enum):
    """Why a session was flagged or tripped."""

    WALL_CLOCK = "wall_clock_runtime_exceeded"
    TOKEN_RATE = "token_spend_rate_exceeded"
    CALL_RATE = "model_call_rate_exceeded"
    REPEATED_ERROR = "repeated_non_retryable_error"
    NO_PROGRESS = "huge_context_no_forward_progress"


class GuardOutcome(str, enum.Enum):
    """Outcome of a guard observation."""

    NO_ACTION = "no_action"
    CONTINUATION_NOTICE = "continuation_notice"
    VERIFIED_HARD_STOP = "verified_hard_stop"


@dataclass(frozen=True)
class GuardThresholds:
    """Trip thresholds. All windows/caps are in seconds; spend in tokens."""

    window_seconds: float = 900.0            # rolling window for rate checks (15 min)
    max_calls_per_window: int = 100          # call-rate ceiling in window
    max_tokens_per_window: int = 4_000_000   # token-rate ceiling in window
    max_runtime_seconds: float = 3600.0      # continuous wall-clock cap (60 min)
    repeated_error_limit: int = 3            # same 4xx / non-retryable failure streak
    huge_context_tokens: int = 150_000       # "huge" context threshold
    no_progress_samples: int = 3             # consecutive verified no-progress attempts
    enabled: bool = True

    @classmethod
    def from_config(cls, cfg: Optional[dict]) -> "GuardThresholds":
        cfg = cfg or {}

        def _num(key: str, default, cast):
            try:
                val = cfg.get(key, default)
                return cast(val) if val is not None else default
            except (TypeError, ValueError):
                return default

        return cls(
            window_seconds=_num("window_seconds", cls.window_seconds, float),
            max_calls_per_window=_num("max_calls_per_window", cls.max_calls_per_window, int),
            max_tokens_per_window=_num("max_tokens_per_window", cls.max_tokens_per_window, int),
            max_runtime_seconds=_num("max_runtime_seconds", cls.max_runtime_seconds, float),
            repeated_error_limit=_num("repeated_error_limit", cls.repeated_error_limit, int),
            huge_context_tokens=_num("huge_context_tokens", cls.huge_context_tokens, int),
            no_progress_samples=_num("no_progress_samples", cls.no_progress_samples, int),
            enabled=bool(cfg.get("enabled", cls.enabled)),
        )


@dataclass(frozen=True)
class SessionObservation:
    """One sampled snapshot of an active session."""

    session_id: str
    started_at: float                       # epoch seconds the turn began
    api_call_count: int = 0                 # cumulative model calls this session
    tokens_used: int = 0                    # cumulative tokens (measured or estimated)
    context_tokens: int = 0                 # size of the context on the latest call
    state_hash: Optional[str] = None        # changes iff progress made
    error_code: Optional[int] = None        # non-retryable status
    provider: str = ""
    model: str = ""
    effort: str = ""
    usage_quality: str = "unknown"         # "measured", "estimated", "unknown"
    no_progress_streak: int = 0
    failure_streak: int = 0
    is_non_retryable_failure: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    cost: float = 0.0


@dataclass
class GuardEvaluationResult:
    """Evaluation result for an observation. Backward compatible with Trip."""

    session_id: str
    reason: Optional[TripReason] = None
    detail: str = ""
    estimated_tokens: int = 0
    estimated_calls: int = 0
    runtime_seconds: float = 0.0
    provider: str = ""
    model: str = ""
    effort: str = ""
    last_state: Optional[str] = None
    outcome: GuardOutcome = GuardOutcome.VERIFIED_HARD_STOP
    is_hard_stop: bool = True
    notice_text: str = ""
    trip_reason: Optional[TripReason] = None

    def __post_init__(self) -> None:
        if self.reason is not None and self.trip_reason is None:
            self.trip_reason = self.reason
        elif self.trip_reason is not None and self.reason is None:
            self.reason = self.trip_reason


# Backward compatibility alias
Trip = GuardEvaluationResult


@dataclass
class _SessionState:
    """Per-session accumulator held on the guard across ticks."""

    samples: Deque[tuple] = field(default_factory=deque)  # (now, api_call_count, tokens_used)
    last_error_code: Optional[int] = None
    repeated_error_count: int = 0
    last_state_hash: Optional[str] = None
    stalled_samples: int = 0
    tripped: bool = False
    notice_latched: bool = False


class RunawayGuard:
    """Stateful, deterministic runaway detector across sessions."""

    def __init__(self, thresholds: Optional[GuardThresholds] = None) -> None:
        self.thresholds = thresholds or GuardThresholds()
        self._sessions: Dict[str, _SessionState] = {}

    def forget(self, session_id: str) -> None:
        """Drop tracking for a session that has ended (or after a kill)."""
        self._sessions.pop(session_id, None)

    def active_session_ids(self) -> list:
        return list(self._sessions.keys())

    def observe(self, obs: SessionObservation, now: float) -> Optional[GuardEvaluationResult]:
        """Record a sample and return an evaluation result if an action/notice is needed."""
        if not self.thresholds.enabled or not obs.session_id:
            return None

        st = self._sessions.get(obs.session_id)
        if st is None:
            st = _SessionState()
            self._sessions[obs.session_id] = st

        if st.tripped:
            return None

        self._update_samples(st, obs, now)
        self._update_error_streak(st, obs)
        self._update_progress(st, obs)

        return self._evaluate(st, obs, now)

    def _update_samples(self, st: _SessionState, obs: SessionObservation, now: float) -> None:
        st.samples.append((now, int(obs.api_call_count), int(obs.tokens_used)))
        cutoff = now - self.thresholds.window_seconds
        while len(st.samples) > 1 and st.samples[1][0] < cutoff:
            st.samples.popleft()

    def _update_error_streak(self, st: _SessionState, obs: SessionObservation) -> None:
        code = obs.error_code
        if obs.failure_streak > 0:
            st.repeated_error_count = obs.failure_streak
            return
        if code is None:
            st.last_error_code = None
            st.repeated_error_count = 0
            return
        if code == st.last_error_code:
            st.repeated_error_count += 1
        else:
            st.last_error_code = code
            st.repeated_error_count = 1

    def _update_progress(self, st: _SessionState, obs: SessionObservation) -> None:
        if obs.no_progress_streak > 0:
            st.stalled_samples = obs.no_progress_streak
            return
        huge = obs.context_tokens >= self.thresholds.huge_context_tokens
        if obs.state_hash is not None and st.last_state_hash is not None:
            made_progress = obs.state_hash != st.last_state_hash
        else:
            made_progress = True
        st.last_state_hash = obs.state_hash
        if huge and not made_progress:
            st.stalled_samples += 1
        elif made_progress:
            st.stalled_samples = 0

    def _window_deltas(self, st: _SessionState) -> tuple:
        if len(st.samples) < 2:
            return 0, 0
        first = st.samples[0]
        last = st.samples[-1]
        calls = max(0, last[1] - first[1])
        tokens = max(0, last[2] - first[2])
        return calls, tokens

    def _evaluate(self, st: _SessionState, obs: SessionObservation, now: float) -> Optional[GuardEvaluationResult]:
        t = self.thresholds
        runtime = max(0.0, now - obs.started_at)
        calls_in_window, tokens_in_window = self._window_deltas(st)

        # 1. HARD STOP CONDITIONS — ONLY from verified no-progress or repeated non-retryable error
        if st.repeated_error_count >= t.repeated_error_limit:
            st.tripped = True
            return GuardEvaluationResult(
                session_id=obs.session_id,
                reason=TripReason.REPEATED_ERROR,
                trip_reason=TripReason.REPEATED_ERROR,
                outcome=GuardOutcome.VERIFIED_HARD_STOP,
                is_hard_stop=True,
                detail=f"HTTP {st.last_error_code or 'error'} repeated {st.repeated_error_count}x (limit {t.repeated_error_limit})",
                estimated_tokens=int(obs.tokens_used),
                estimated_calls=int(obs.api_call_count),
                runtime_seconds=runtime,
                provider=obs.provider,
                model=obs.model,
                effort=obs.effort,
                last_state=obs.state_hash,
            )

        if st.stalled_samples >= t.no_progress_samples:
            st.tripped = True
            return GuardEvaluationResult(
                session_id=obs.session_id,
                reason=TripReason.NO_PROGRESS,
                trip_reason=TripReason.NO_PROGRESS,
                outcome=GuardOutcome.VERIFIED_HARD_STOP,
                is_hard_stop=True,
                detail=f"{obs.context_tokens:,}-token context re-sent {st.stalled_samples}x with no forward progress",
                estimated_tokens=int(obs.tokens_used),
                estimated_calls=int(obs.api_call_count),
                runtime_seconds=runtime,
                provider=obs.provider,
                model=obs.model,
                effort=obs.effort,
                last_state=obs.state_hash,
            )

        # 2. CONTINUATION NOTICES — for rate, runtime, call threshold crossings (latched once per episode)
        threshold_crossed = False
        reasons = []
        if runtime > t.max_runtime_seconds:
            threshold_crossed = True
            reasons.append(f"runtime {runtime / 60:.1f}m > {t.max_runtime_seconds / 60:.0f}m")
        if tokens_in_window > t.max_tokens_per_window:
            threshold_crossed = True
            reasons.append(f"tokens {tokens_in_window:,} > {t.max_tokens_per_window:,}/15m ({obs.usage_quality})")
        if calls_in_window > t.max_calls_per_window:
            threshold_crossed = True
            reasons.append(f"calls {calls_in_window} > {t.max_calls_per_window}/15m")

        if threshold_crossed:
            if not st.notice_latched:
                st.notice_latched = True
                reason_str = ", ".join(reasons)
                notice = (
                    f"⚠️ Session threshold notice: {reason_str}.\n"
                    f"Usage: input={obs.input_tokens:,}, output={obs.output_tokens:,}, "
                    f"cache_read={obs.cache_read_tokens:,}, cache_write={obs.cache_write_tokens:,}, "
                    f"reasoning={obs.reasoning_tokens:,}, quality={obs.usage_quality}.\n"
                    "Continuing by default. Reply STOP or /stop to cancel."
                )
                primary_reason = TripReason.WALL_CLOCK if runtime > t.max_runtime_seconds else (
                    TripReason.TOKEN_RATE if tokens_in_window > t.max_tokens_per_window else TripReason.CALL_RATE
                )
                return GuardEvaluationResult(
                    session_id=obs.session_id,
                    reason=primary_reason,
                    trip_reason=primary_reason,
                    outcome=GuardOutcome.CONTINUATION_NOTICE,
                    is_hard_stop=False,
                    notice_text=notice,
                    detail=reason_str,
                    estimated_tokens=int(obs.tokens_used),
                    estimated_calls=int(obs.api_call_count),
                    runtime_seconds=runtime,
                    provider=obs.provider,
                    model=obs.model,
                    effort=obs.effort,
                    last_state=obs.state_hash,
                )

        return None
