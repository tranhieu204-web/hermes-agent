"""Desktop/TUI dead-loop guard integration contracts.

These tests exercise the shared production collector/action seam.  They patch
bindings in the modules that define the behavior, never package re-exports.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from gateway.fleet_safety.deadloop_guard import (
    GuardEvaluationResult,
    GuardOutcome,
    TripReason,
)
from gateway.fleet_safety.enforcer import GuardEnforcer
from gateway.fleet_safety import integration
from tui_gateway import server


class _Agent:
    def __init__(self) -> None:
        self.session_id = "desktop-session"
        self.provider = "openai-codex"
        self.model = "gpt-test"
        self.reasoning_config = {"effort": "high"}
        self.interrupt_reasons: list[str] = []
        self.clear_calls = 0
        self.on_interrupt = None

    def get_activity_summary(self):
        return {
            "api_call_count": 2,
            "context_tokens": 123,
            "progress_seq": 4,
            "turn_generation": 9,
        }

    def interrupt(self, reason: str) -> None:
        self.interrupt_reasons.append(reason)
        if self.on_interrupt is not None:
            self.on_interrupt()

    def clear_interrupt(self) -> None:
        self.clear_calls += 1


def _session(agent: _Agent) -> dict:
    session = {
        "history_lock": threading.RLock(),
        "running": True,
        "agent": agent,
        "inflight_turn": None,
        "_turn_cancel_requested": False,
    }
    with session["history_lock"]:
        assert server._start_inflight_turn(session, "hello") is True
    return session


def _trip(outcome: GuardOutcome, reason: TripReason) -> GuardEvaluationResult:
    return GuardEvaluationResult(
        session_id="desktop-session",
        reason=reason,
        detail="fixture evidence",
        estimated_tokens=10,
        estimated_calls=2,
        runtime_seconds=100.0,
        provider="openai-codex",
        model="gpt-test",
        effort="high",
        last_state="progress:4",
        outcome=outcome,
        is_hard_stop=outcome is GuardOutcome.VERIFIED_HARD_STOP,
    )


def test_start_inflight_turn_uses_unique_guard_token_and_respects_barrier():
    agent = _Agent()
    session = _session(agent)
    first = session["inflight_turn"]["guard_turn_token"]

    with session["history_lock"]:
        server._clear_inflight_turn(session)
        assert server._start_inflight_turn(session, "second") is True
        second = session["inflight_turn"]["guard_turn_token"]
        session["_guard_interrupt_barrier"] = second
        assert server._start_inflight_turn(session, "must wait") is False

    assert first != second
    assert session["inflight_turn"]["guard_turn_token"] == second


def test_desktop_collector_projects_live_turn_and_binds_token_identity():
    agent = _Agent()
    session = _session(agent)
    owner = SimpleNamespace(
        _sessions={"desktop-session": session},
        _sessions_lock=threading.RLock(),
    )

    observations, mapping = integration._collect_desktop_observations(
        owner, now=200.0, assumed_context_tokens=160_000
    )

    assert len(observations) == 1
    observation = observations[0]
    assert observation.session_id == "desktop-session"
    assert observation.api_call_count == 2
    assert observation.context_tokens == 123
    bound_session, bound_agent, bound_token = mapping["desktop-session"]
    assert bound_session is session
    assert bound_agent is agent
    assert bound_token == session["inflight_turn"]["guard_turn_token"]


def test_continuation_notice_reports_without_interrupt_or_lease_release(monkeypatch):
    agent = _Agent()
    session = _session(agent)
    emitted = []
    owner = SimpleNamespace(
        _sessions={"desktop-session": session},
        _sessions_lock=threading.RLock(),
        _emit=lambda event, sid, payload=None: emitted.append((event, sid, payload)),
    )
    token = session["inflight_turn"]["guard_turn_token"]
    actions = integration._DesktopKillActions(
        owner, {"desktop-session": (session, agent, token)}
    )

    result = GuardEnforcer(actions).enforce(
        _trip(GuardOutcome.CONTINUATION_NOTICE, TripReason.WALL_CLOCK)
    )

    assert result.interrupted is False
    assert result.lease_released is False
    assert agent.interrupt_reasons == []
    assert emitted and emitted[0][0] == "notification.show"


def test_hard_stop_is_token_fenced_and_duplicate_claim_is_rejected():
    agent = _Agent()
    session = _session(agent)
    owner = SimpleNamespace(
        _sessions={"desktop-session": session},
        _sessions_lock=threading.RLock(),
        _emit=lambda *_args, **_kwargs: None,
    )
    token = session["inflight_turn"]["guard_turn_token"]
    actions = integration._DesktopKillActions(
        owner, {"desktop-session": (session, agent, token)}
    )

    result = GuardEnforcer(actions).enforce(
        _trip(GuardOutcome.VERIFIED_HARD_STOP, TripReason.REPEATED_ERROR)
    )
    duplicate = actions.interrupt("desktop-session", "duplicate")

    assert result.interrupted is True
    assert duplicate is False
    assert len(agent.interrupt_reasons) == 1
    assert session["_turn_cancel_requested"] is True
    assert session["inflight_turn"]["guard_interrupt_state"] == "requested"
    assert session["_guard_interrupt_barrier"] == token


def test_stale_completion_barrier_clears_interrupt_before_later_turn():
    agent = _Agent()
    session = _session(agent)
    owner = SimpleNamespace(
        _sessions={"desktop-session": session},
        _sessions_lock=threading.RLock(),
        _emit=lambda *_args, **_kwargs: None,
    )
    token = session["inflight_turn"]["guard_turn_token"]
    attempted_start = []

    def complete_during_interrupt():
        with session["history_lock"]:
            server._clear_inflight_turn(session)
            session["running"] = False
            attempted_start.append(server._start_inflight_turn(session, "later"))

    agent.on_interrupt = complete_during_interrupt
    actions = integration._DesktopKillActions(
        owner, {"desktop-session": (session, agent, token)}
    )

    assert actions.interrupt("desktop-session", "verified stop") is True
    assert attempted_start == [False]
    assert agent.clear_calls == 1
    assert session.get("_guard_interrupt_barrier") is None

    with session["history_lock"]:
        assert server._start_inflight_turn(session, "later") is True
        assert session["inflight_turn"]["guard_turn_token"] != token


def test_desktop_loop_is_immediate_idempotent_and_survives_tick_failure(monkeypatch):
    owner = SimpleNamespace()
    calls = []
    observed_second = threading.Event()

    def tick(_owner):
        calls.append(time.monotonic())
        if len(calls) == 1:
            raise RuntimeError("injected tick failure")
        observed_second.set()

    monkeypatch.setattr(integration, "run_desktop_guard_tick", tick)
    thread, stop = integration.start_desktop_guard_loop(owner, interval_seconds=0.01)
    same_thread, same_stop = integration.start_desktop_guard_loop(owner, interval_seconds=0.01)
    assert observed_second.wait(1.0)
    assert (same_thread, same_stop) == (thread, stop)
    stop.set()
    thread.join(timeout=1.0)
    assert not thread.is_alive()
