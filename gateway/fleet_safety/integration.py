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
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from gateway.fleet_safety.deadloop_guard import (
    GuardThresholds,
    RunawayGuard,
    SessionObservation,
)
from gateway.fleet_safety.enforcer import GuardEnforcer, KillActions
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


def _get_or_create_guard(runner: Any, thresholds: GuardThresholds) -> RunawayGuard:
    guard = getattr(runner, "_deadloop_guard", None)
    if guard is None:
        guard = RunawayGuard(thresholds)
        try:
            runner._deadloop_guard = guard
        except Exception:
            pass
    else:
        guard.thresholds = thresholds  # pick up live config changes each tick
    return guard


def _collect_observations(
    runner: Any, now: float, assumed_context_tokens: int
) -> Tuple[List[SessionObservation], Dict[str, Tuple[str, Any]]]:
    """Sample every running agent into observations keyed by the agent's
    session_id. Returns (observations, {session_id: (session_key, agent)})."""
    observations: List[SessionObservation] = []
    mapping: Dict[str, Tuple[str, Any]] = {}

    running = getattr(runner, "_running_agents", {}) or {}
    started_ts = getattr(runner, "_running_agents_ts", {}) or {}
    # The pending sentinel marks a session whose agent object doesn't exist yet.
    try:
        from gateway.run import _AGENT_PENDING_SENTINEL
    except Exception:
        _AGENT_PENDING_SENTINEL = object()

    for session_key, agent in list(running.items()):
        if agent is _AGENT_PENDING_SENTINEL or agent is None:
            continue
        try:
            summary = agent.get_activity_summary() or {}
        except Exception:
            summary = {}
        session_id = getattr(agent, "session_id", None) or session_key
        started_at = float(started_ts.get(session_key, now) or now)
        api_calls = int(summary.get("api_call_count", 0) or 0)
        # No live per-session token meter is exposed; estimate from call count
        # and the assumed per-call context size. Deliberately conservative.
        tokens_used = api_calls * int(assumed_context_tokens)
        # State hash changes only on forward progress: new activity or a new
        # tool. Re-sending the same context with the same activity ts and tool
        # (calls climbing, nothing new happening) reads as "no progress".
        state_hash = f"{summary.get('last_activity_ts')}:{summary.get('current_tool')}"

        observations.append(
            SessionObservation(
                session_id=session_id,
                started_at=started_at,
                api_call_count=api_calls,
                tokens_used=tokens_used,
                context_tokens=int(assumed_context_tokens),
                state_hash=state_hash,
                error_code=None,  # runtime doesn't surface last error here yet
                provider=str(getattr(agent, "provider", "") or ""),
                model=str(getattr(agent, "model", "") or ""),
                effort=_agent_effort(agent),
            )
        )
        mapping[session_id] = (session_key, agent)

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

    def __init__(self, runner: Any, loop: Any, mapping: Dict[str, Tuple[str, Any]]) -> None:
        self._runner = runner
        self._loop = loop
        self._mapping = mapping

    def interrupt(self, session_id: str, reason: str) -> bool:
        entry = self._mapping.get(session_id)
        if not entry:
            return False
        _session_key, agent = entry
        try:
            agent.interrupt(reason)
            return True
        except Exception as e:
            logger.warning("dead-loop guard: interrupt failed for %s: %s", session_id, e)
            return False

    def release_lease(self, session_id: str) -> bool:
        registry = getattr(self._runner, "_turn_leases", None)
        tokens = getattr(self._runner, "_turn_lease_tokens", None)
        if registry is None or not isinstance(tokens, dict):
            return False
        released = False
        # Release any held lease token whose resolved session_id matches. The
        # registry's release() is idempotent and ownership-checked, so this is
        # safe even if the turn's own finally releases concurrently.
        for token in list(tokens.values()):
            try:
                if getattr(token, "session_id", None) == session_id:
                    if registry.release(token):
                        released = True
            except Exception as e:
                logger.debug("dead-loop guard: lease release error for %s: %s", session_id, e)
        return released

    def notify(self, text: str) -> bool:
        # 1) Always log at ERROR — guaranteed to surface in errors.log and the
        #    desktop/gateway log views. A kill is never silent even if no
        #    messaging channel is reachable.
        logger.error("FLEET-SAFETY DEAD-LOOP KILL\n%s", text)
        # 2) Best-effort fan-out to every configured home channel (Telegram +
        #    desktop) on the gateway loop.
        delivered = False
        try:
            delivered = self._broadcast_home_channels(text)
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("dead-loop guard: home-channel broadcast failed: %s", e)
        return True  # logged ⇒ reported; `delivered` is a bonus, not required

    def _broadcast_home_channels(self, text: str) -> bool:
        runner = self._runner
        loop = self._loop
        config = getattr(runner, "config", None)
        adapters = getattr(runner, "adapters", None)
        if config is None or loop is None:
            return False
        try:
            from gateway.delivery import resolve_delivery_transport
            from gateway.run import safe_schedule_threadsafe
        except Exception:
            return False

        platforms = getattr(config, "platforms", {}) or {}

        async def _send_all() -> bool:
            any_ok = False
            for platform, platform_cfg in platforms.items():
                home = getattr(platform_cfg, "home_channel", None)
                if not home or not getattr(home, "chat_id", None):
                    continue
                transport = resolve_delivery_transport(platform, config, adapters)
                if transport is None:
                    continue
                try:
                    result = await transport.adapter.send(str(home.chat_id), text)
                    if result is None or getattr(result, "success", True) is not False:
                        any_ok = True
                except Exception as e:
                    logger.debug("dead-loop guard: send to %s failed: %s", platform, e)
            return any_ok

        fut = safe_schedule_threadsafe(
            _send_all(), loop,
            logger=logger,
            log_message="dead-loop guard alert scheduling error",
        )
        if fut is None:
            return False
        try:
            return bool(fut.result(timeout=20))
        except Exception:
            return False


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

        assumed = int(guard_cfg.get("assumed_context_tokens", DEFAULT_ASSUMED_CONTEXT_TOKENS) or
                      DEFAULT_ASSUMED_CONTEXT_TOKENS)
        guard = _get_or_create_guard(runner, thresholds)

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
            logger.warning(
                "dead-loop guard tripped on %s (%s): interrupted=%s lease_released=%s "
                "notified=%s errors=%s",
                trip.session_id, trip.reason.value, result.interrupted,
                result.lease_released, result.notified, result.errors,
            )
            # Stop tracking a killed session — it's been reported once and the
            # loop is being torn down; a stale latched entry would just linger.
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
) -> SelectedLane:
    """Select the best fallback routing lane using usage-headroom rules."""
    cfg = _load_full_config()
    return select_best_lane(cfg, current_provider=current_provider, is_heavy=is_heavy, now=now)


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
