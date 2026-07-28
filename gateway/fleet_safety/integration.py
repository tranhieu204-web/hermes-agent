"""Live wiring: bind the deterministic guard engine to the real gateway.

This is the only module in the package that touches gateway internals, and it
does so lazily (imports inside functions) so the engine stays importable and
unit-testable on its own and there is no import cycle with ``gateway.run``.

Two entry points:

- :func:`run_guard_tick` — called once per housekeeping tick. Samples every
  running agent, feeds the persistent :class:`RunawayGuard`, and on a trip
  hard-stops that session (interrupt + lease release) and reports it.
- :func:`verified_usage_for` / :func:`decide_routing` — the wallet-cap seam a
  router calls before sending heavy work to a provider. Reconciles the cached
  usage figure against the authoritative provider source, then decides.

Everything here is best-effort and defensive: any failure is logged and
swallowed so a guard bug can never take down the housekeeping loop or a turn.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agent.usage_provenance import UsageProvenance, usage_aggregate_from_mapping
from hermes_constants import get_hermes_home

from gateway.fleet_safety.deadloop_guard import (
    GuardOutcome,
    GuardThresholds,
    RunawayGuard,
    SessionObservation,
)
from gateway.fleet_safety.enforcer import EnforcementResult, GuardEnforcer, KillActions
from gateway.fleet_safety.extension_lifecycle import ExtensionRegistry
from gateway.fleet_safety.wallet_cap import (
    RoutingRequest,
    WalletCap,
    WalletCapConfig,
    WalletDecision,
)
from gateway.fleet_safety.usage_verify import (
    VerifiedUsage,
    extract_authoritative,
    load_cached_percent,
    verified_usage_for,
    verify_usage,
)
from gateway.fleet_safety.selector import (
    SelectedLane,
    select_best_lane,
)

logger = logging.getLogger(__name__)

# Default estimate for the context size (tokens) of a single model call when
# the live runtime doesn't expose a real token count. The 2026-07-25 incident
# re-sent ~160k tokens per call; using that as the per-call estimate makes the
# token-rate detector meaningful from call-count data alone. Configurable via
# ``fleet_safety.deadloop_guard.assumed_context_tokens``.
DEFAULT_ASSUMED_CONTEXT_TOKENS = 160_000
_EXTENSION_REGISTRIES: dict[str, ExtensionRegistry] = {}
_DESKTOP_LOOP_LOCK = threading.Lock()


def _extension_state_path(config: dict) -> str:
    configured = config.get("extension_state_path")
    if configured:
        return str(Path(configured).expanduser())
    return str(get_hermes_home() / "fleet_safety" / "extensions.json")


def _get_extension_registry(config: dict) -> ExtensionRegistry:
    path = _extension_state_path(config)
    registry = _EXTENSION_REGISTRIES.get(path)
    if registry is None:
        registry = ExtensionRegistry(path)
        _EXTENSION_REGISTRIES[path] = registry
    return registry


def _load_full_config() -> dict:
    try:
        from hermes_cli.config import load_config
        return load_config() or {}
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("config load failed: %s", e)
        return {}


def _load_fleet_safety_config() -> dict:
    return _load_full_config().get("fleet_safety") or {}


# --------------------------------------------------------------------------
# Detector / kill wiring
# --------------------------------------------------------------------------


def _get_or_create_guard(
    runner: Any,
    thresholds: GuardThresholds,
    registry: Optional[ExtensionRegistry] = None,
) -> RunawayGuard:
    if registry is None:
        path = getattr(runner, "_deadloop_extension_registry_path", None)
        registry = _get_extension_registry(
            {"extension_state_path": path} if path else {}
        )
    guard = getattr(runner, "_deadloop_guard", None)
    if guard is None:
        guard = RunawayGuard(thresholds, extension_registry=registry)
        try:
            runner._deadloop_guard = guard
        except Exception:
            pass
    else:
        guard.thresholds = thresholds  # pick up live config changes each tick
        guard.extension_registry = registry
    return guard


def deny_active_extensions(
    runner_or_session: Any,
    session_ids: Optional[List[str]] = None,
    *,
    now: Optional[float] = None,
) -> int:
    """Persist exact STOP denial for active checkpoints.

    The ``(runner, session_ids)`` form is used by gateway STOP routing. The
    one-argument session-id form remains available to small integrations.
    """

    if session_ids is None:
        ids = [str(runner_or_session)]
        registries = tuple(_EXTENSION_REGISTRIES.values())
    else:
        ids = [str(value) for value in session_ids if str(value).strip()]
        guard = getattr(runner_or_session, "_deadloop_guard", None)
        registry = getattr(guard, "extension_registry", None)
        registries = (registry,) if registry is not None else ()
    denied = 0
    decision_at = time.time() if now is None else float(now)
    for session_id in ids:
        per_session = 0
        for registry in registries:
            per_session += len(
                registry.deny_active_for_session(session_id, now=decision_at)
            )
        if per_session:
            denied += 1
    return denied


def _observation_for_agent(
    session_id: str,
    agent: Any,
    *,
    started_at: float,
    assumed_context_tokens: int,
) -> SessionObservation:
    """Project one live agent through the common gateway/desktop schema."""
    try:
        summary = agent.get_activity_summary() or {}
    except Exception as exc:
        logger.warning(
            "dead-loop guard: activity snapshot failed for %s: %s",
            session_id,
            type(exc).__name__,
        )
        summary = {}
    try:
        api_calls = int(summary.get("api_call_count", 0) or 0)
        attempt_seq = summary.get("attempt_seq")
        failure_seq = summary.get("failure_seq")
        progress_seq = summary.get("progress_seq")
        no_progress_streak = summary.get("no_progress_streak")
        turn_generation = summary.get("turn_generation")
        is_non_retryable = bool(summary.get("is_non_retryable_failure", False))
        last_error_code = summary.get("last_error_code")
        terminal_session_id = (
            summary.get("last_session_id") or summary.get("session_id") or session_id
        )
        usage = usage_aggregate_from_mapping(
            session_id,
            summary.get("usage"),
            default_component_id="activity-summary-usage",
            fallback_session_id=str(terminal_session_id or ""),
            missing_reason="missing_activity_usage",
        )
        # Prefer sanitized backend receipts. When none exist, retain the
        # conservative call-count estimate for the rate detector only.
        estimated_tokens = api_calls * int(assumed_context_tokens)
        tokens_used = (
            usage.total_tokens
            if usage.usage_verified
            else max(usage.total_tokens, estimated_tokens)
        )
        try:
            context_tokens = max(0, int(summary.get("context_tokens", 0) or 0))
        except (TypeError, ValueError, OverflowError):
            context_tokens = 0
        # No-progress is about verified state advancement, not heartbeat/API
        # activity. Timestamps and current-tool labels would change during a
        # dead loop and falsely reset the stall detector.
        state_hash = f"progress:{progress_seq}"

        return SessionObservation(
                session_id=session_id,
                started_at=started_at,
                api_call_count=api_calls,
                tokens_used=tokens_used,
                token_count_provenance=(
                    UsageProvenance.MEASURED
                    if usage.usage_verified
                    else UsageProvenance.ESTIMATED
                ),
                context_tokens=context_tokens,
                state_hash=state_hash,
                error_code=last_error_code if is_non_retryable else None,
                turn_generation=(
                    int(turn_generation) if turn_generation is not None else None
                ),
                attempt_seq=(int(attempt_seq) if attempt_seq is not None else None),
                progress_seq=(int(progress_seq) if progress_seq is not None else None),
                no_progress_streak=(
                    int(no_progress_streak)
                    if no_progress_streak is not None
                    else None
                ),
                failure_seq=(int(failure_seq) if failure_seq is not None else None),
                failure_streak=int(summary.get("failure_streak", 0) or 0),
                is_non_retryable_failure=is_non_retryable,
                last_event_id=summary.get("last_event_id"),
                last_event_sequence=summary.get("last_event_sequence"),
                last_call_id=summary.get("last_call_id"),
                last_adapter=summary.get("last_adapter"),
                last_source=summary.get("last_source"),
                last_retryability=summary.get("last_retryability"),
                last_status=summary.get("last_status"),
                usage=usage,
                terminal_session_id=terminal_session_id,
                provider=str(getattr(agent, "provider", "") or ""),
                model=str(getattr(agent, "model", "") or ""),
                effort=_agent_effort(agent),
            )
    except Exception:
        logger.warning(
            "dead-loop guard: observation projection failed for %s",
            session_id,
            exc_info=True,
        )
        return SessionObservation(session_id=session_id, started_at=started_at)


def _collect_observations(
    runner: Any, now: float, assumed_context_tokens: int
) -> Tuple[List[SessionObservation], Dict[str, Tuple[str, Any, Optional[int]]]]:
    """Sample every running gateway agent through the common projection."""
    observations: List[SessionObservation] = []
    mapping: Dict[str, Tuple[str, Any, Optional[int]]] = {}

    running = getattr(runner, "_running_agents", {}) or {}
    started_ts = getattr(runner, "_running_agents_ts", {}) or {}
    try:
        from gateway.run import _AGENT_PENDING_SENTINEL
    except Exception:
        _AGENT_PENDING_SENTINEL = object()

    for session_key, agent in list(running.items()):
        if agent is _AGENT_PENDING_SENTINEL or agent is None:
            continue
        if bool(getattr(agent, "_hermes_guard_interrupt_pending", False)):
            continue
        session_id = str(getattr(agent, "session_id", None) or session_key).strip()
        try:
            started_at = float(started_ts.get(session_key, now) or now)
        except (TypeError, ValueError, OverflowError):
            started_at = now
        observations.append(
            _observation_for_agent(
                session_id,
                agent,
                started_at=started_at,
                assumed_context_tokens=assumed_context_tokens,
            )
        )
        generation = getattr(agent, "_hermes_run_generation", None)
        mapping[session_id] = (
            session_key,
            agent,
            int(generation) if generation is not None else None,
        )

    return observations, mapping


def _collect_desktop_observations(
    server_module: Any,
    now: float,
    assumed_context_tokens: int,
) -> Tuple[List[SessionObservation], Dict[str, Tuple[Any, Any, str]]]:
    """Snapshot live desktop turns without reading telemetry under a session lock."""
    with server_module._sessions_lock:
        candidates = list((getattr(server_module, "_sessions", {}) or {}).items())

    first_seen = getattr(server_module, "_desktop_guard_first_seen", None)
    if not isinstance(first_seen, dict):
        first_seen = {}
        setattr(server_module, "_desktop_guard_first_seen", first_seen)

    observations: List[SessionObservation] = []
    mapping: Dict[str, Tuple[Any, Any, str]] = {}
    live_keys: set[tuple[str, str]] = set()
    for raw_sid, session in candidates:
        if not isinstance(session, dict):
            continue
        lock = session.get("history_lock")
        if lock is None:
            continue
        with lock:
            running = session.get("running") is True
            agent = session.get("agent")
            turn = session.get("inflight_turn")
            if not running or agent is None or not isinstance(turn, dict):
                continue
            token = str(turn.get("guard_turn_token") or "").strip()
            if not token or turn.get("streaming") is False or turn.get("status") == "error":
                continue
            started_raw = turn.get("started_at")
        sid = str(raw_sid or getattr(agent, "session_id", "") or "").strip()
        if not sid:
            continue
        key = (sid, token)
        live_keys.add(key)
        try:
            started_at = float(started_raw)
            if not math.isfinite(started_at) or started_at <= 0:
                raise ValueError("invalid turn start")
        except (TypeError, ValueError, OverflowError):
            started_at = float(first_seen.setdefault(key, now))
        observations.append(
            _observation_for_agent(
                sid,
                agent,
                started_at=started_at,
                assumed_context_tokens=assumed_context_tokens,
            )
        )
        mapping[sid] = (session, agent, token)

    for key in tuple(first_seen):
        if key not in live_keys:
            first_seen.pop(key, None)
    return observations, mapping


def _agent_effort(agent: Any) -> str:
    try:
        rc = getattr(agent, "reasoning_config", None)
        if isinstance(rc, dict):
            return str(rc.get("effort", "") or "")
    except Exception:
        pass
    return ""


class _LiveKillActions(KillActions):
    """Adapts the running gateway to the enforcer's three effects."""

    def __init__(
        self,
        runner: Any,
        loop: Any,
        mapping: Dict[str, Tuple[str, Any, Optional[int]]],
    ) -> None:
        self._runner = runner
        self._loop = loop
        self._mapping = mapping
        self._notification_target: Optional[str] = None
        self.interrupt_pending = False

    def interrupt(self, session_id: str, reason: str) -> bool:
        entry = self._mapping.get(session_id)
        if not entry:
            return False
        session_key, agent, generation = entry
        self._notification_target = session_id
        if generation is None:
            logger.warning(
                "dead-loop guard: refusing unbound interrupt for %s", session_id
            )
            return False
        is_current = getattr(self._runner, "_is_session_run_current", None)
        if not callable(is_current) or not is_current(session_key, generation):
            return False
        try:
            agent.interrupt(reason)
            self.interrupt_pending = True
            setattr(agent, "_hermes_guard_interrupt_pending", True)
            evict = getattr(self._runner, "_evict_cached_agent", None)
            if callable(evict):
                evict(session_key)
            # Acceptance is not termination. The generation-owned running slot
            # and turn lease remain until the worker's normal unwind removes
            # them; housekeeping must never publish a reusable slot early.
            return True
        except Exception as e:
            logger.warning("fleet-safety guard: interrupt failed for %s: %s", session_id, e)
            return False

    def release_lease(self, session_id: str) -> bool:
        entry = self._mapping.get(session_id)
        if not entry:
            return False
        session_key, _agent, generation = entry
        if generation is None:
            return False
        release = getattr(self._runner, "_release_turn_lease", None)
        if not callable(release):
            return False
        return bool(release(session_key, generation))

    def notify(self, text: str) -> bool:
        # 1) Always log at ERROR — guaranteed to surface in errors.log and the
        #    desktop/gateway log views. A kill is never silent even if no
        #    messaging channel is reachable.
        logger.warning("FLEET-SAFETY NOTICE\n%s", text)
        # 2) Best-effort delivery only to the originating route. Broadcasting
        #    session/provider details to unrelated home channels crosses tenant
        #    and project boundaries.
        try:
            return self._notify_origin(text)
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("dead-loop guard: origin notification failed: %s", e)
            return False

    def _notify_origin(self, text: str) -> bool:
        runner = self._runner
        loop = self._loop
        entry = self._mapping.get(self._notification_target or "")
        if not entry or loop is None:
            return False
        session_key, _agent, _generation = entry
        get_source = getattr(runner, "_get_cached_session_source", None)
        source = get_source(session_key) if callable(get_source) else None
        if source is None:
            return False
        get_adapter = getattr(runner, "_adapter_for_source", None)
        adapter = get_adapter(source) if callable(get_adapter) else None
        if adapter is None:
            return False
        try:
            from gateway.run import safe_schedule_threadsafe
        except Exception:
            return False

        async def _send_origin() -> bool:
            try:
                metadata = runner._thread_metadata_for_source(source)
            except Exception:
                metadata = None
            result = await adapter.send(source.chat_id, text, metadata=metadata)
            if result is False:
                return False
            if isinstance(result, dict):
                return result.get("success", True) is not False
            return result is None or getattr(result, "success", True) is not False

        fut = safe_schedule_threadsafe(
            _send_origin(), loop,
            logger=logger,
            log_message="dead-loop guard alert scheduling error",
        )
        if fut is None:
            return False
        try:
            return bool(fut.result(timeout=20))
        except Exception:
            return False


class _DesktopKillActions(KillActions):
    """Token-fenced cooperative interruption for desktop/TUI turns."""

    def __init__(self, server: Any, mapping: Dict[str, Tuple[Any, Any, str]]):
        self.server, self.mapping = server, mapping
        self._notification_target = next(iter(mapping), None)

    def interrupt(self, session_id: str, reason: str) -> bool:
        target = self.mapping.get(session_id)
        if target is None:
            return False
        session, agent, token = target
        with self.server._sessions_lock:
            if self.server._sessions.get(session_id) is not session:
                return False
            with session["history_lock"]:
                turn = session.get("inflight_turn")
                if not isinstance(turn, dict) or turn.get("guard_turn_token") != token or turn.get("guard_interrupt_state") is not None:
                    return False
                turn["guard_interrupt_state"] = "claimed"
                session["_guard_interrupt_barrier"] = token
                session["_turn_cancel_requested"] = True
        try:
            supervisor = session.get("compute_host_supervisor")
            if supervisor is not None and hasattr(supervisor, "interrupt"):
                supervisor.interrupt(reason)
            else:
                agent.interrupt(reason)
        except Exception:
            logger.warning("desktop guard interrupt failed for %s", session_id, exc_info=True)
            with session["history_lock"]:
                if session.get("_guard_interrupt_barrier") == token:
                    session.pop("_guard_interrupt_barrier", None)
            return False
        drain_queued = False
        with session["history_lock"]:
            turn = session.get("inflight_turn")
            if isinstance(turn, dict) and turn.get("guard_turn_token") == token:
                turn["guard_interrupt_state"] = "requested"
            else:
                try:
                    agent.clear_interrupt()
                except Exception:
                    logger.warning("desktop guard interrupt reset failed for %s", session_id, exc_info=True)
                if session.get("_guard_interrupt_barrier") == token:
                    session.pop("_guard_interrupt_barrier", None)
                drain_queued = bool(session.get("queued_prompt"))
        if drain_queued:
            try:
                self.server._drain_queued_prompt(None, session_id, session)
            except Exception:
                logger.warning("desktop guard queued-turn drain failed for %s", session_id, exc_info=True)
        return True

    def release_lease(self, session_id: str) -> bool:
        return False

    def notify(self, report: str) -> bool:
        logger.warning("FLEET-SAFETY NOTICE\n%s", report)
        sid = self._notification_target
        if not sid:
            return False
        try:
            self.server._emit("notification.show", sid, {"message": report, "level": "warning"})
            return True
        except Exception:
            logger.warning("desktop guard notification failed for %s", sid, exc_info=True)
            return False


def run_desktop_guard_tick(server_module: Any = None, now: Optional[float] = None) -> List[EnforcementResult]:
    if server_module is None:
        from tui_gateway import server as server_module
    now = time.time() if now is None else now
    cfg = _load_fleet_safety_config()
    guard_cfg = cfg.get("deadloop_guard") or {}
    thresholds = GuardThresholds.from_config(guard_cfg)
    if not thresholds.enabled:
        return []
    try:
        assumed = int(guard_cfg.get("assumed_context_tokens", DEFAULT_ASSUMED_CONTEXT_TOKENS))
        if assumed <= 0:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        assumed = DEFAULT_ASSUMED_CONTEXT_TOKENS
    guard = getattr(server_module, "_desktop_deadloop_guard", None)
    if not isinstance(guard, RunawayGuard) or guard.thresholds != thresholds:
        guard = RunawayGuard(thresholds)
        server_module._desktop_deadloop_guard = guard
    observations, mapping = _collect_desktop_observations(server_module, now, assumed)
    tokens = getattr(server_module, "_desktop_guard_tokens", {})
    server_module._desktop_guard_tokens = tokens
    live = set(mapping)
    results: List[EnforcementResult] = []
    for obs in observations:
        token = mapping[obs.session_id][2]
        if tokens.get(obs.session_id) not in (None, token):
            guard.forget(obs.session_id)
        tokens[obs.session_id] = token
        try:
            trip = guard.observe(obs, now)
            if trip is not None:
                results.append(GuardEnforcer(_DesktopKillActions(server_module, {obs.session_id: mapping[obs.session_id]})).enforce(trip))
        except Exception:
            logger.warning("desktop dead-loop guard tick failed for %s", obs.session_id, exc_info=True)
    for sid in tuple(tokens):
        if sid not in live:
            tokens.pop(sid, None)
            guard.forget(sid)
    return results


def start_desktop_guard_loop(server_module: Any = None, *, stop_event: Optional[threading.Event] = None, interval_seconds: float = 60.0) -> Tuple[threading.Thread, threading.Event]:
    if server_module is None:
        from tui_gateway import server as server_module
    try:
        interval = float(interval_seconds)
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        interval = 60.0
    event = stop_event or threading.Event()
    with _DESKTOP_LOOP_LOCK:
        existing = getattr(server_module, "_desktop_guard_thread", None)
        if isinstance(existing, threading.Thread) and existing.is_alive():
            return existing, getattr(server_module, "_desktop_guard_stop_event", event)
        def _loop() -> None:
            while not event.is_set():
                try:
                    run_desktop_guard_tick(server_module)
                except Exception:
                    logger.warning("desktop dead-loop guard loop tick failed", exc_info=True)
                if event.wait(interval):
                    break
        thread = threading.Thread(target=_loop, name="hermes-desktop-deadloop-guard", daemon=True)
        server_module._desktop_guard_stop_event = event
        server_module._desktop_guard_thread = thread
        thread.start()
        return thread, event


def run_guard_tick(runner: Any, loop: Any = None, now: Optional[float] = None) -> None:
    """Sample running agents, evaluate the guard, and kill+report any trips.

    Safe to call from the housekeeping thread every tick. Never raises.
    """
    if now is None:
        now = time.time()
    try:
        cfg = _load_fleet_safety_config()
        guard_cfg = cfg.get("deadloop_guard") or {}
        thresholds = GuardThresholds.from_config(guard_cfg)
        if not thresholds.enabled:
            return

        try:
            assumed = int(
                guard_cfg.get("assumed_context_tokens", DEFAULT_ASSUMED_CONTEXT_TOKENS)
                or DEFAULT_ASSUMED_CONTEXT_TOKENS
            )
            if assumed <= 0:
                raise ValueError("assumed context must be positive")
        except (TypeError, ValueError, OverflowError):
            assumed = DEFAULT_ASSUMED_CONTEXT_TOKENS
        registry = _get_extension_registry(guard_cfg)
        guard = _get_or_create_guard(runner, thresholds, registry)

        observations, mapping = _collect_observations(runner, now, assumed)

        # Prune sessions the guard is tracking that are no longer running, so a
        # finished session's latched state is released and its id can be reused.
        live_ids = {o.session_id for o in observations}
        for sid in guard.active_session_ids():
            if sid not in live_ids:
                guard.forget(sid)

        if not observations:
            return

        actions = _LiveKillActions(runner, loop, mapping)
        enforcer = GuardEnforcer(actions)

        for obs in observations:
            trip = guard.observe(obs, now)
            if trip is None:
                continue
            result = enforcer.enforce(trip)
            if trip.extension_event_id and result.notified:
                guard.mark_extension_notice_delivered(trip.extension_event_id)
            logger.warning(
                "dead-loop guard tripped on %s (%s): interrupted=%s lease_released=%s "
                "notified=%s errors=%s",
                trip.session_id, trip.reason.value, result.interrupted,
                result.lease_released, result.notified, result.errors,
            )
            # Continuation notices keep accumulating progress and resource
            # deltas. Only a verified hard stop tears down the guard state.
            if trip.outcome is GuardOutcome.VERIFIED_HARD_STOP:
                guard.forget(trip.session_id)
    except Exception as e:  # pragma: no cover - defensive top-level guard
        logger.debug("dead-loop guard tick error: %s", e)


# --------------------------------------------------------------------------
# Wallet-cap / usage-verification seam
# --------------------------------------------------------------------------


def select_best_lane_for(
    current_provider: str = "",
    *,
    is_heavy: bool = True,
    now: Optional[float] = None,
    importance: str = "normal",
) -> SelectedLane:
    """Select the best fallback routing lane using usage-headroom rules.

    Args:
        importance: Task importance level. One of "ultra", "critically_important",
                   "semi_critical", "normal" (default). Applies importance-based grading
                   to Claude/Codex/Kimi; Grok and Antigravity always pin to "high".
    """
    cfg = _load_full_config()
    return select_best_lane(cfg, current_provider=current_provider, is_heavy=is_heavy, now=now, importance=importance)


def decide_routing(
    provider: str,
    effort: str = "medium",
    *,
    is_heavy: bool = False,
    now: Optional[float] = None,
) -> WalletDecision:
    """Wallet-cap decision for routing heavy work to ``provider``.

    Reads config (``fleet_safety.wallet_cap``), verifies usage against the
    authoritative source, and returns an ALLOW / DOWNGRADE_EFFORT /
    FALLBACK_PROVIDER decision. The router applies it; this never mutates any
    wallet or places any call.
    """
    cfg = _load_fleet_safety_config()
    wallet_cfg = WalletCapConfig.from_config(cfg.get("wallet_cap") or {})
    verify_cfg = cfg.get("usage_verify") or {}
    verified = verified_usage_for(
        provider,
        now=now,
        max_age_seconds=float(verify_cfg.get("max_age_seconds", 900.0) or 900.0),
        divergence_points=float(verify_cfg.get("divergence_points", 15.0) or 15.0),
    )
    cap = WalletCap(wallet_cfg)
    return cap.decide(
        RoutingRequest(provider=provider, effort=effort, is_heavy=is_heavy),
        verified.used_percent,
    )
