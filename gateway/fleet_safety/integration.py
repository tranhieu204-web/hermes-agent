"""Live wiring: bind the deterministic guard engine to the real gateway.

This is the only module in the package that touches gateway internals, and it
does so lazily (imports inside functions) so the engine stays importable and
unit-testable on its own and there is no import cycle with ``gateway.run``.

Two entry points:

- :func:`run_guard_tick` — called once per housekeeping tick. Samples every
  running agent, feeds the persistent :class:`RunawayGuard`, and on a trip
  evaluates continuation notice or hard-stops that session (interrupt + lease release).
- :func:`verified_usage_for` / :func:`decide_routing` — the wallet-cap seam a
  router calls before sending heavy work to a provider. Reconciles the cached
  usage figure against the authoritative provider source, then decides.

Everything here is best-effort and defensive: any failure is logged and
swallowed so a guard bug can never take down the housekeeping loop or a turn.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from gateway.fleet_safety.deadloop_guard import (
    GuardThresholds,
    RunawayGuard,
    SessionObservation,
    GuardOutcome,
)
from gateway.fleet_safety.enforcer import GuardEnforcer, KillActions
from gateway.fleet_safety.extension_lifecycle import ExtensionRegistry
from gateway.fleet_safety.wallet_cap import (
    RoutingRequest,
    WalletCap,
    WalletCapConfig,
    WalletDecision,
    WalletAction,
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

DEFAULT_ASSUMED_CONTEXT_TOKENS = 160_000


def _load_fleet_safety_config() -> dict:
    try:
        from hermes_cli.config import load_config
        return (load_config().get("fleet_safety") or {})
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("fleet_safety config load failed: %s", e)
        return {}


def _get_or_create_guard(runner: Any, thresholds: GuardThresholds) -> RunawayGuard:
    guard = getattr(runner, "_deadloop_guard", None)
    if guard is None:
        state_path = getattr(runner, "_deadloop_extension_registry_path", None)
        if state_path is None:
            try:
                from hermes_constants import get_hermes_home

                state_path = get_hermes_home() / "fleet_safety" / "extensions.json"
            except Exception:
                state_path = None
        guard = RunawayGuard(
            thresholds,
            extension_registry=ExtensionRegistry(state_path),
        )
        try:
            runner._deadloop_guard = guard
        except Exception:
            pass
    else:
        guard.thresholds = thresholds  # pick up live config changes each tick
    return guard


def deny_active_extensions(
    runner: Any,
    session_ids: List[str],
    *,
    now: Optional[float] = None,
) -> int:
    """Persist STOP denial for all matching active extension records."""
    guard = getattr(runner, "_deadloop_guard", None)
    registry = getattr(guard, "extension_registry", None)
    if registry is None:
        return 0
    when = time.time() if now is None else float(now)
    denied_ids = set()
    for session_id in dict.fromkeys(str(value) for value in session_ids if value):
        try:
            for record in registry.deny_active_for_session(session_id, now=when):
                denied_ids.add(record.event_id)
        except Exception as exc:  # pragma: no cover - defensive command path
            logger.debug("fleet-safety extension denial failed: %s", exc)
    return len(denied_ids)


def _collect_observations(
    runner: Any, now: float, assumed_context_tokens: int
) -> Tuple[List[SessionObservation], Dict[str, Tuple[str, Any]]]:
    """Sample every running agent into observations keyed by distinct execution context.

    Returns (observations, {session_id: (session_key, agent)}).
    """
    observations: List[SessionObservation] = []
    mapping: Dict[str, Tuple[str, Any]] = {}

    running = getattr(runner, "_running_agents", {}) or {}
    started_ts = getattr(runner, "_running_agents_ts", {}) or {}

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

        # Context isolation to prevent agents sharing session_id from overwriting each other
        agent_id = getattr(agent, "agent_id", None) or getattr(agent, "_subagent_id", None)
        base_session_id = getattr(agent, "session_id", None) or session_key
        if agent_id and agent_id != base_session_id:
            effective_session_id = f"{base_session_id}:{agent_id}"
        else:
            effective_session_id = base_session_id

        started_value = started_ts.get(session_key, now)
        started_at = float(now if started_value is None else started_value)
        api_calls = int(summary.get("api_call_count", 0) or 0)

        # Explicit producer usage is authoritative even when every counter is
        # zero and quality is unknown. Only agents with no usage key at all
        # enter the clearly-labelled legacy estimate path.
        has_explicit_usage = "usage" in summary or "turn_usage" in summary
        u = summary.get("usage") if "usage" in summary else summary.get("turn_usage")
        if has_explicit_usage and isinstance(u, Mapping):
            inp = int(u.get("input_tokens") or 0)
            outp = int(u.get("output_tokens") or 0)
            cread = int(u.get("cache_read_tokens") or 0)
            cwrite = int(u.get("cache_write_tokens") or 0)
            reasoning = int(u.get("reasoning_tokens") or 0)
            cost_val = float(u.get("cost") or 0.0)
            cost_status = str(u.get("cost_status") or "unknown")
            cost_source = str(u.get("cost_source") or "none")
            quality = str(u.get("quality") or "unknown").lower()
            if quality not in {"measured", "estimated", "unknown"}:
                quality = "unknown"
            tokens_used = inp + outp + cread + cwrite
        else:
            tokens_used = api_calls * int(assumed_context_tokens)
            inp = tokens_used
            outp = 0
            cread = 0
            cwrite = 0
            reasoning = 0
            cost_val = 0.0
            cost_status = "unknown"
            cost_source = "none"
            quality = "estimated" if api_calls > 0 else "unknown"

        # State hash from progress telemetry
        p_seq = summary.get("progress_seq")
        a_seq = summary.get("attempt_seq")
        f_seq = summary.get("failure_seq")
        no_prog_streak = int(summary.get("no_progress_streak", 0) or 0)
        fail_streak = int(summary.get("failure_streak", 0) or 0)

        if p_seq is not None:
            state_hash = f"p_seq:{p_seq}:attempt:{a_seq}"
        else:
            state_hash = summary.get("last_attempt_key")

        observations.append(
            SessionObservation(
                session_id=effective_session_id,
                started_at=started_at,
                api_call_count=api_calls,
                tokens_used=tokens_used,
                context_tokens=int(assumed_context_tokens),
                state_hash=state_hash,
                error_code=summary.get("last_error_code"),
                provider=str(getattr(agent, "provider", "") or ""),
                model=str(getattr(agent, "model", "") or ""),
                effort=_agent_effort(agent),
                usage_quality=quality,
                attempt_seq=int(a_seq or 0),
                progress_seq=int(p_seq or 0),
                no_progress_streak=no_prog_streak,
                failure_seq=int(f_seq or 0),
                failure_streak=fail_streak,
                is_non_retryable_failure=bool(summary.get("is_non_retryable_failure", False)),
                input_tokens=inp,
                output_tokens=outp,
                cache_read_tokens=cread,
                cache_write_tokens=cwrite,
                reasoning_tokens=reasoning,
                cost=cost_val,
                cost_status=cost_status,
                cost_source=cost_source,
            )
        )
        mapping[effective_session_id] = (session_key, agent)

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
            logger.warning("fleet-safety guard: interrupt failed for %s: %s", session_id, e)
            return False

    def release_lease(self, session_id: str) -> bool:
        registry = getattr(self._runner, "_turn_leases", None)
        tokens = getattr(self._runner, "_turn_lease_tokens", None)
        if registry is None or not isinstance(tokens, dict):
            return False
        released = False
        base_sid = session_id.split(":")[0]
        for token in list(tokens.values()):
            try:
                tok_sid = getattr(token, "session_id", None)
                if tok_sid in (session_id, base_sid):
                    if registry.release(token):
                        released = True
            except Exception as e:
                logger.debug("fleet-safety guard: lease release error for %s: %s", session_id, e)
        return released

    def notify(self, text: str) -> bool:
        logger.info("FLEET-SAFETY NOTICE\n%s", text)
        delivered = False
        try:
            delivered = self._broadcast_home_channels(text)
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("fleet-safety guard: home-channel broadcast failed: %s", e)
        return bool(delivered)

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
                    logger.debug("fleet-safety guard: send to %s failed: %s", platform, e)
            return any_ok

        fut = safe_schedule_threadsafe(
            _send_all(), loop,
            logger=logger,
            log_message="fleet-safety guard alert scheduling error",
        )
        if fut is None:
            return False
        try:
            return bool(fut.result(timeout=20))
        except Exception:
            return False


def run_guard_tick(runner: Any, loop: Any = None, now: Optional[float] = None) -> None:
    """Sample active agents and enforce checkpoint or safety-stop decisions.

    Safe to call from the housekeeping thread every tick. Never raises. A
    tripped session remains latched until it disappears from the live registry,
    preventing repeated interrupt/notification effects while gateway unwind is
    still in progress.
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

        live_ids = {o.session_id for o in observations}
        for sid in guard.active_session_ids():
            if sid not in live_ids:
                guard.forget(sid)

        if not observations:
            return

        actions = _LiveKillActions(runner, loop, mapping)
        enforcer = GuardEnforcer(actions)

        for obs in observations:
            eval_res = guard.observe(obs, now)
            if eval_res is None:
                continue
            result = enforcer.enforce(eval_res)
            if (
                result.notified
                and getattr(eval_res, "outcome", None) == GuardOutcome.CONTINUATION_NOTICE
                and getattr(eval_res, "extension_event_id", "")
            ):
                guard.mark_extension_notice_delivered(eval_res.extension_event_id)
            logger.info(
                "fleet-safety guard evaluated %s (%s): outcome=%s "
                "stop_requested=%s notified=%s errors=%s",
                eval_res.session_id,
                getattr(eval_res, "reason", None),
                getattr(eval_res, "outcome", None),
                result.stop_requested,
                result.notified,
                result.errors,
            )
    except Exception as e:  # pragma: no cover - defensive top-level guard
        logger.debug("fleet-safety guard tick error: %s", e)


def select_best_lane_for(
    current_provider: str = "",
    *,
    is_heavy: bool = True,
    now: Optional[float] = None,
) -> SelectedLane:
    cfg = _load_fleet_safety_config()
    return select_best_lane(cfg, current_provider=current_provider, is_heavy=is_heavy, now=now)


def decide_routing(
    provider: str,
    effort: str = "medium",
    *,
    is_heavy: bool = False,
    now: Optional[float] = None,
) -> WalletDecision:
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
    decision = cap.decide(
        RoutingRequest(provider=provider, effort=effort, is_heavy=is_heavy),
        verified.used_percent,
    )
    if decision.action == WalletAction.FALLBACK_PROVIDER:
        from dataclasses import replace
        best = select_best_lane(cfg, current_provider=provider, is_heavy=is_heavy, now=now)
        decision = replace(
            decision,
            fallback_provider=best.provider,
            fallback_model=best.model,
            reason=f"{decision.reason} -> fallback to {best.lane} ({best.provider}/{best.model})",
        )
    return decision
