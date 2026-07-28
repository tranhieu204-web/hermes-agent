"""Runaway-loop detector for active agent sessions.

The detector watches a stream of per-session :class:`SessionObservation`
samples (one per housekeeping tick) and trips on any of four independent
runaway signals — the four failure modes of the 2026-07-25 incident:

  (a) **Rate.** Model-call count *or* estimated token spend over a threshold in
      a rolling window (default: >100 calls or >4,000,000 tokens per 15 min on
      one session). The incident's token rate (~5.5M tokens / 15 min) trips
      this; a healthy interactive session does not.
  (b) **Repeated non-retryable error.** The same non-retryable (4xx) status
      code observed on ``>=K`` consecutive samples (default 3). A 4xx does not
      get better on retry — hammering it is pure waste.
  (c) **Wall-clock.** One continuous turn running longer than a hard cap
      (default 60 min). The incident ran ~13 h.
  (d) **No-forward-progress.** A huge context (>150k tokens) re-sent across
      ``>=M`` consecutive samples (default 3) with an unchanged state hash —
      i.e. the loop keeps paying for a giant context but produces no new tool
      results / state change.

Determinism: :meth:`RunawayGuard.observe` takes ``now`` explicitly and holds
all mutable state on the guard instance. Given the same sequence of
``(observation, now)`` pairs it always trips at the same point on the same
reason, in the fixed priority order below. It never reads the clock itself.
"""

from __future__ import annotations

import enum
import math
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, Optional

from agent.usage_provenance import (
    UsageAggregate,
    UsageComponentReceipt,
    UsageProvenance,
    aggregate_usage,
    usage_aggregate_from_mapping,
)

from agent.usage_provenance import (
    UsageAggregate,
    UsageComponentReceipt,
    UsageProvenance,
    aggregate_usage,
    usage_aggregate_from_mapping,
)
from gateway.fleet_safety.extension_lifecycle import ExtensionRegistry


class TripReason(str, enum.Enum):
    """Why a session was flagged as a runaway. Ordered by evaluation priority."""

    WALL_CLOCK = "wall_clock_runtime_exceeded"
    TOKEN_RATE = "token_spend_rate_exceeded"
    CALL_RATE = "model_call_rate_exceeded"
    REPEATED_ERROR = "repeated_non_retryable_error"
    NO_PROGRESS = "huge_context_no_forward_progress"
    EXTENSION_DENIED = "extension_denied_by_user"


class GuardOutcome(str, enum.Enum):
    """Whether an evaluation is informational or requests a safety stop."""

    NO_ACTION = "no_action"
    CONTINUATION_NOTICE = "continuation_notice"
    VERIFIED_HARD_STOP = "verified_hard_stop"


@dataclass(frozen=True)
class GuardThresholds:
    """Trip thresholds. All windows/caps are in seconds; spend in tokens.

    Defaults are chosen so the 2026-07-25 incident trips well before a full
    wallet drain, while a normal long interactive/agentic session does not.
    """

    window_seconds: float = 900.0            # rolling window for rate checks (15 min)
    max_calls_per_window: int = 100          # (a) call-rate ceiling in window
    max_tokens_per_window: int = 4_000_000   # (a) token-rate ceiling in window
    max_runtime_seconds: float = 3600.0      # (c) continuous wall-clock cap (60 min)
    repeated_error_limit: int = 3            # (b) same 4xx on N consecutive samples
    huge_context_tokens: int = 150_000       # (d) "huge" context threshold
    no_progress_samples: int = 3             # (d) consecutive stalled samples to trip
    enabled: bool = True

    @classmethod
    def from_config(cls, cfg: Optional[dict]) -> "GuardThresholds":
        """Build from a ``fleet_safety.deadloop_guard`` config dict.

        Unknown keys are ignored; missing keys fall back to the class default.
        Values are coerced defensively so a malformed user config can never
        crash the housekeeping tick — a bad value falls back to the default.
        """
        cfg = cfg or {}

        def _positive_num(key: str, default, cast):
            try:
                val = cfg.get(key, default)
                parsed = cast(val) if val is not None else default
                if isinstance(parsed, float) and not math.isfinite(parsed):
                    return default
                return parsed if parsed > 0 else default
            except (TypeError, ValueError, OverflowError):
                return default

        return cls(
            window_seconds=_positive_num("window_seconds", cls.window_seconds, float),
            max_calls_per_window=_positive_num("max_calls_per_window", cls.max_calls_per_window, int),
            max_tokens_per_window=_positive_num("max_tokens_per_window", cls.max_tokens_per_window, int),
            max_runtime_seconds=_positive_num("max_runtime_seconds", cls.max_runtime_seconds, float),
            repeated_error_limit=_positive_num("repeated_error_limit", cls.repeated_error_limit, int),
            huge_context_tokens=_positive_num("huge_context_tokens", cls.huge_context_tokens, int),
            no_progress_samples=_positive_num("no_progress_samples", cls.no_progress_samples, int),
            enabled=(cfg.get("enabled") if isinstance(cfg.get("enabled"), bool) else cls.enabled),
        )


@dataclass(frozen=True)
class SessionObservation:
    """One sampled snapshot of an active session.

    Cumulative counters (``api_call_count``, ``tokens_used``) are lifetime
    totals for the session; the guard differences them across the rolling
    window to get a rate. ``state_hash`` is any value that changes when the
    session makes forward progress (new tool result / new activity); the guard
    only cares whether it changed, not what it is.
    """

    session_id: str
    started_at: float                       # epoch seconds the turn began
    api_call_count: int = 0                 # cumulative model calls this session
    tokens_used: int = 0                    # cumulative estimated tokens this session
    token_count_provenance: UsageProvenance | str = UsageProvenance.ESTIMATED
    context_tokens: int = 0                 # size of the context on the latest call
    state_hash: Optional[str] = None        # changes iff the session made progress
    error_code: Optional[int] = None        # non-retryable status on the latest call, else None
    attempt_seq: Optional[int] = None       # distinct terminal attempts observed
    progress_seq: Optional[int] = None
    no_progress_streak: Optional[int] = None
    failure_seq: Optional[int] = None       # distinct failed terminal attempts
    failure_streak: int = 0
    is_non_retryable_failure: bool = False
    last_event_id: Optional[str] = None
    last_event_sequence: Optional[int] = None
    last_call_id: Optional[str] = None
    last_adapter: Optional[str] = None
    last_source: Optional[str] = None
    last_retryability: Optional[str] = None
    last_status: Optional[str] = None
    usage: UsageAggregate | dict | None = None
    terminal_session_id: Optional[str] = None
    provider: str = ""
    model: str = ""
    effort: str = ""
    turn_generation: Optional[int] = None   # monotonic telemetry turn epoch
    usage_quality: str = "unknown"
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    cost: float = 0.0
    cost_status: str = "unknown"
    cost_source: str = "none"

    def __post_init__(self) -> None:
        bound_session_id = str(self.session_id or "").strip()
        if not bound_session_id:
            raise ValueError("session observation requires a nonempty session_id")
        terminal_session_id = str(self.terminal_session_id or "").strip()
        usage = usage_aggregate_from_mapping(
            bound_session_id,
            self.usage,
            default_component_id="session-observation-usage",
            fallback_session_id=terminal_session_id or bound_session_id,
            missing_reason="missing_observation_usage",
        )
        if terminal_session_id and terminal_session_id != bound_session_id:
            usage = aggregate_usage(
                bound_session_id,
                (
                    *usage.components,
                    UsageComponentReceipt(
                        component_id="session-observation-terminal-session",
                        session_id=terminal_session_id,
                        provenance=UsageProvenance.UNKNOWN,
                        reason="session_mismatch",
                    ),
                ),
            )
        object.__setattr__(self, "session_id", bound_session_id)
        object.__setattr__(
            self,
            "token_count_provenance",
            UsageProvenance.coerce(self.token_count_provenance),
        )
        object.__setattr__(self, "usage", usage)
        if self.usage_quality == "unknown" and usage.provenance is not UsageProvenance.UNKNOWN:
            object.__setattr__(self, "usage_quality", usage.provenance.value)
        for field_name in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "reasoning_tokens",
        ):
            if int(getattr(self, field_name, 0) or 0) == 0:
                object.__setattr__(
                    self,
                    field_name,
                    int(getattr(usage, field_name, 0) or 0),
                )
        # Identity is bound by the runtime, not by terminal payloads.
        object.__setattr__(self, "terminal_session_id", bound_session_id)

    def __post_init__(self) -> None:
        bound_session_id = str(self.session_id or "").strip()
        if not bound_session_id:
            raise ValueError("session observation requires a nonempty session_id")
        terminal_session_id = str(self.terminal_session_id or "").strip()
        usage = usage_aggregate_from_mapping(
            bound_session_id,
            self.usage,
            default_component_id="session-observation-usage",
            fallback_session_id=terminal_session_id or bound_session_id,
            missing_reason="missing_observation_usage",
        )
        if terminal_session_id and terminal_session_id != bound_session_id:
            usage = aggregate_usage(
                bound_session_id,
                (
                    *usage.components,
                    UsageComponentReceipt(
                        component_id="session-observation-terminal-session",
                        session_id=terminal_session_id,
                        provenance=UsageProvenance.UNKNOWN,
                        reason="session_mismatch",
                    ),
                ),
            )
        object.__setattr__(self, "session_id", bound_session_id)
        object.__setattr__(
            self,
            "token_count_provenance",
            UsageProvenance.coerce(self.token_count_provenance),
        )
        object.__setattr__(self, "usage", usage)
        # Identity is bound by the runtime, not by terminal payloads.
        object.__setattr__(self, "terminal_session_id", bound_session_id)


@dataclass(frozen=True)
class GuardEvaluationResult:
    """A fired detection with immutable session and usage evidence."""

    session_id: str
    reason: TripReason
    detail: str
    estimated_tokens: int
    estimated_calls: int
    runtime_seconds: float
    provider: str
    model: str
    effort: str
    last_state: Optional[str]
    usage: UsageAggregate | dict | None = None
    token_count_provenance: UsageProvenance | str = UsageProvenance.ESTIMATED
    outcome: GuardOutcome = GuardOutcome.VERIFIED_HARD_STOP
    is_hard_stop: bool = True
    notice_text: str = ""
    extension_event_id: str = ""
    extension_grant_size: int = 0
    extension_expires_at: float = 0.0
    extension_revision: int = 0
    trip_reason: Optional[TripReason] = None
    usage_quality: str = "unknown"
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    cost: float = 0.0
    cost_status: str = "unknown"
    cost_source: str = "none"

    def __post_init__(self) -> None:
        bound_session_id = str(self.session_id or "").strip()
        if not bound_session_id:
            raise ValueError("guard evaluation requires a nonempty session_id")
        object.__setattr__(self, "session_id", bound_session_id)
        object.__setattr__(
            self,
            "usage",
            usage_aggregate_from_mapping(
                bound_session_id,
                self.usage,
                default_component_id="guard-evaluation-usage",
                fallback_session_id=bound_session_id,
                missing_reason="missing_evaluation_usage",
            ),
        )
        object.__setattr__(
            self,
            "token_count_provenance",
            UsageProvenance.coerce(self.token_count_provenance),
        )
        object.__setattr__(self, "trip_reason", self.trip_reason or self.reason)
        if self.usage_quality == "unknown" and self.usage.provenance is not UsageProvenance.UNKNOWN:
            object.__setattr__(self, "usage_quality", self.usage.provenance.value)
        for field_name in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "reasoning_tokens",
        ):
            if int(getattr(self, field_name, 0) or 0) == 0:
                object.__setattr__(
                    self,
                    field_name,
                    int(getattr(self.usage, field_name, 0) or 0),
                )

    @property
    def usage_provenance(self) -> UsageProvenance:
        return self.usage.provenance

    @property
    def known_component_count(self) -> int:
        return self.usage.known_component_count

    @property
    def unknown_component_count(self) -> int:
        return self.usage.unknown_component_count

    @property
    def usage_verified(self) -> bool:
        return self.usage.usage_verified

    @property
    def headroom_verified(self) -> bool:
        return self.usage.headroom_verified


# Backward-compatible public name for enforcer/tests while evaluation callers
# adopt the more precise result type.
Trip = GuardEvaluationResult


@dataclass
class _SessionState:
    """Per-session accumulator held on the guard across ticks."""

    samples: Deque[tuple] = field(default_factory=deque)  # (now, api_call_count, tokens_used)
    turn_generation: Optional[int] = None
    last_error_code: Optional[int] = None
    last_failure_signature: Optional[str] = None
    repeated_error_count: int = 0
    last_attempt_seq: Optional[int] = None
    last_failure_seq: Optional[int] = None
    last_state_hash: Optional[str] = None
    state_initialized: bool = False
    stalled_samples: int = 0
    tripped: bool = False   # latched — a killed session is not re-reported every tick
    notice_latched: bool = False


class RunawayGuard:
    """Stateful, deterministic runaway detector across many sessions.

    Feed it one :class:`SessionObservation` per active session per tick via
    :meth:`observe`; it returns a :class:`Trip` the first time a session
    crosses any threshold and ``None`` otherwise. Once a session trips it is
    latched (subsequent observations return ``None``) so a killed loop is
    reported exactly once. Call :meth:`forget` when a session ends to release
    its state.
    """

    def __init__(
        self,
        thresholds: Optional[GuardThresholds] = None,
        *,
        extension_registry: Optional[ExtensionRegistry] = None,
    ) -> None:
        self.thresholds = thresholds or GuardThresholds()
        self.extension_registry = extension_registry
        self._sessions: Dict[str, _SessionState] = {}

    def mark_extension_notice_delivered(self, event_id: str) -> None:
        if self.extension_registry is not None and event_id:
            self.extension_registry.mark_notice_delivered(event_id)

    # -- lifecycle -------------------------------------------------------

    def forget(self, session_id: str) -> None:
        """Drop tracking for a session that has ended (or after a kill)."""
        self._sessions.pop(session_id, None)

    def active_session_ids(self) -> list:
        return list(self._sessions.keys())

    # -- core ------------------------------------------------------------

    def observe(self, obs: SessionObservation, now: float) -> Optional[GuardEvaluationResult]:
        """Record a sample and return a :class:`Trip` if this session crosses
        a threshold for the first time. Deterministic in ``(obs, now)``."""
        if not self.thresholds.enabled or not obs.session_id:
            return None

        st = self._sessions.get(obs.session_id)
        if st is None:
            st = _SessionState()
            self._sessions[obs.session_id] = st

        if obs.turn_generation is not None:
            generation = int(obs.turn_generation)
            if st.turn_generation is None:
                st.turn_generation = generation
            elif generation < st.turn_generation:
                # A delayed housekeeping sample from an older turn cannot
                # rewind the current generation's high-water marks or latch.
                return None
            elif generation > st.turn_generation:
                self._reset_turn_state(st, generation)

        if st.tripped:
            return None

        self._update_samples(st, obs, now)
        self._update_error_streak(st, obs)
        self._update_progress(st, obs)

        trip = self._evaluate(st, obs, now)
        if trip is not None and trip.is_hard_stop:
            st.tripped = True
        return trip

    # -- accumulators ----------------------------------------------------

    @staticmethod
    def _reset_turn_state(st: _SessionState, generation: int) -> None:
        st.samples.clear()
        st.turn_generation = generation
        st.last_error_code = None
        st.repeated_error_count = 0
        st.last_attempt_seq = None
        st.last_failure_seq = None
        st.last_state_hash = None
        st.state_initialized = False
        st.stalled_samples = 0
        st.tripped = False
        st.notice_latched = False

    def _update_samples(self, st: _SessionState, obs: SessionObservation, now: float) -> None:
        if st.samples:
            last_now, last_calls, last_tokens = st.samples[-1]
            if (
                now < last_now
                or int(obs.api_call_count) < last_calls
                or int(obs.tokens_used) < last_tokens
            ):
                # A new turn/session epoch or a non-monotonic sample must not be
                # differenced against the previous counter epoch.
                st.samples.clear()
        st.samples.append((now, int(obs.api_call_count), int(obs.tokens_used)))
        cutoff = now - self.thresholds.window_seconds
        # Keep one sample just before the cutoff so the window delta spans the
        # full window rather than only the samples strictly inside it.
        while len(st.samples) > 1 and st.samples[1][0] < cutoff:
            st.samples.popleft()

    def _update_error_streak(self, st: _SessionState, obs: SessionObservation) -> None:
        sequence = obs.attempt_seq
        if sequence is None or sequence <= 0:
            sequence = obs.failure_seq
        if sequence is not None:
            sequence = int(sequence)
            if st.last_attempt_seq is not None and sequence <= st.last_attempt_seq:
                # Housekeeping polls and transport replays do not create new
                # failures. Only a distinct terminal-attempt sequence advances.
                return
            st.last_attempt_seq = sequence

            prior_failure_seq = st.last_failure_seq
            if obs.failure_seq is not None:
                st.last_failure_seq = int(obs.failure_seq)
            code = obs.error_code if obs.is_non_retryable_failure else None
            if code is None:
                st.last_error_code = None
                st.repeated_error_count = 0
                return
            if code == st.last_error_code:
                delta = 1
                if prior_failure_seq is not None and obs.failure_seq is not None:
                    delta = max(1, int(obs.failure_seq) - prior_failure_seq)
                st.repeated_error_count += delta
            else:
                st.last_error_code = code
                # Telemetry's failure_streak counts consecutive
                # non-retryable failures regardless of status code. The guard
                # contract is stricter: only repetitions of this same error
                # advance the detector.
                st.repeated_error_count = 1
            return

        # Legacy observation producers have no terminal sequence. Preserve the
        # original per-sample behavior until they adopt the additive fields.
        code = obs.error_code
        if code is None:
            # A clean (non-erroring) sample breaks the streak — the session is
            # making calls that don't hit the same wall.
            st.last_error_code = None
            st.repeated_error_count = 0
            return
        if code == st.last_error_code:
            st.repeated_error_count += 1
        else:
            st.last_error_code = code
            st.repeated_error_count = 1

    def _update_progress(self, st: _SessionState, obs: SessionObservation) -> None:
        if obs.no_progress_streak is not None:
            # Producer terminal-event telemetry is authoritative. Repeated
            # housekeeping polls carry the same streak and therefore cannot
            # manufacture additional no-progress evidence.
            st.stalled_samples = max(0, int(obs.no_progress_streak))
            return
        huge = obs.context_tokens >= self.thresholds.huge_context_tokens
        made_progress = not st.state_initialized or obs.state_hash != st.last_state_hash
        st.last_state_hash = obs.state_hash
        st.state_initialized = True
        if huge and not made_progress:
            st.stalled_samples += 1
        else:
            st.stalled_samples = 0

    # -- decision --------------------------------------------------------

    def _window_deltas(self, st: _SessionState) -> tuple:
        """(calls, tokens) accumulated across the retained window."""
        if len(st.samples) < 2:
            if not st.samples:
                return 0, 0
            # Counters are turn-scoped. On the first housekeeping sample they
            # are the only conservative evidence available, and the entire
            # observed turn is inside the current detector epoch.
            return max(0, st.samples[-1][1]), max(0, st.samples[-1][2])
        first = st.samples[0]
        last = st.samples[-1]
        calls = max(0, last[1] - first[1])
        tokens = max(0, last[2] - first[2])
        return calls, tokens

    def _evaluate(
        self, st: _SessionState, obs: SessionObservation, now: float
    ) -> Optional[GuardEvaluationResult]:
        t = self.thresholds
        runtime = max(0.0, now - obs.started_at)
        calls_in_window, tokens_in_window = self._window_deltas(st)

        def _mk(
            reason: TripReason,
            detail: str,
            *,
            outcome: GuardOutcome = GuardOutcome.VERIFIED_HARD_STOP,
            is_hard_stop: bool = True,
            notice_text: str = "",
            extension_event_id: str = "",
            extension_grant_size: int = 0,
            extension_expires_at: float = 0.0,
            extension_revision: int = 0,
        ) -> GuardEvaluationResult:
            return GuardEvaluationResult(
                session_id=obs.session_id,
                reason=reason,
                trip_reason=reason,
                detail=detail,
                estimated_tokens=int(obs.tokens_used),
                estimated_calls=int(obs.api_call_count),
                runtime_seconds=runtime,
                provider=obs.provider,
                model=obs.model,
                effort=obs.effort,
                last_state=obs.state_hash,
                usage=obs.usage,
                token_count_provenance=obs.token_count_provenance,
                outcome=outcome,
                is_hard_stop=is_hard_stop,
                notice_text=notice_text,
                extension_event_id=extension_event_id,
                extension_grant_size=extension_grant_size,
                extension_expires_at=extension_expires_at,
                extension_revision=extension_revision,
                usage_quality=obs.usage_quality,
                input_tokens=obs.input_tokens,
                output_tokens=obs.output_tokens,
                cache_read_tokens=obs.cache_read_tokens,
                cache_write_tokens=obs.cache_write_tokens,
                reasoning_tokens=obs.reasoning_tokens,
                cost=obs.cost,
                cost_status=obs.cost_status,
                cost_source=obs.cost_source,
            )

        # Only verified no-progress or repeated non-retryable failures request a
        # safety stop. Resource volume and elapsed time open a renewable,
        # default-continue checkpoint instead.
        if st.repeated_error_count >= t.repeated_error_limit:
            failure_label = (
                f"HTTP {st.last_error_code}"
                if st.last_error_code is not None
                else (st.last_failure_signature or "code-free non-retryable failure")
            )
            return _mk(
                TripReason.REPEATED_ERROR,
                f"{failure_label} repeated {st.repeated_error_count}x "
                f"(limit {t.repeated_error_limit})",
            )
        if st.stalled_samples >= t.no_progress_samples:
            return _mk(
                TripReason.NO_PROGRESS,
                f"{obs.context_tokens:,}-token context re-sent {st.stalled_samples}x "
                "with producer-verified no forward progress",
            )

        reasons: list[str] = []
        primary_reason: Optional[TripReason] = None
        if runtime > t.max_runtime_seconds:
            primary_reason = TripReason.WALL_CLOCK
            reasons.append(
                f"runtime {runtime / 60:.1f}m > {t.max_runtime_seconds / 60:.0f}m"
            )
        if tokens_in_window > t.max_tokens_per_window:
            primary_reason = primary_reason or TripReason.TOKEN_RATE
            reasons.append(
                f"tokens {tokens_in_window:,} > {t.max_tokens_per_window:,}/"
                f"{t.window_seconds / 60:.0f}m ({obs.usage_quality})"
            )
        if calls_in_window > t.max_calls_per_window:
            primary_reason = primary_reason or TripReason.CALL_RATE
            reasons.append(
                f"calls {calls_in_window} > {t.max_calls_per_window}/"
                f"{t.window_seconds / 60:.0f}m"
            )

        if primary_reason is None:
            st.notice_latched = False
            return None

        detail = ", ".join(reasons)
        event_id = ""
        grant_size = 0
        expires_at = 0.0
        revision = 0
        if self.extension_registry is not None:
            record, should_notify = self.extension_registry.request(
                session_id=obs.session_id,
                checkpoint_key=f"{obs.started_at:.6f}:{primary_reason.value}",
                now=now,
                duration_seconds=t.window_seconds,
                grant_size=t.max_calls_per_window,
            )
            event_id = record.event_id
            grant_size = record.grant_size
            expires_at = record.expires_at
            revision = record.revision
            if not record.should_continue:
                return _mk(
                    TripReason.EXTENSION_DENIED,
                    "extension denied by user; cooperative interruption requested",
                    extension_event_id=event_id,
                    extension_grant_size=grant_size,
                    extension_expires_at=expires_at,
                    extension_revision=revision,
                )
            if not should_notify:
                return None
        else:
            if st.notice_latched:
                return None
            st.notice_latched = True

        notice = (
            f"Extension checkpoint: {detail}.\n"
            "Continuing by default. Send STOP or /stop to cancel."
        )
        return _mk(
            primary_reason,
            detail,
            outcome=GuardOutcome.CONTINUATION_NOTICE,
            is_hard_stop=False,
            notice_text=notice,
            extension_event_id=event_id,
            extension_grant_size=grant_size,
            extension_expires_at=expires_at,
            extension_revision=revision,
        )
