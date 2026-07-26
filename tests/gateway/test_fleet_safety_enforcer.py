"""Unit tests for the kill-and-report enforcer and the report formatter."""

from gateway.fleet_safety.deadloop_guard import Trip, TripReason
from gateway.fleet_safety.enforcer import GuardEnforcer
from gateway.fleet_safety.report import format_kill_report


def _trip(**kw):
    base = dict(
        session_id="20260725_001655_7eff2a",
        reason=TripReason.WALL_CLOCK,
        detail="turn ran 780.0 min (cap 60 min)",
        estimated_tokens=288_000_000,
        estimated_calls=1800,
        runtime_seconds=780 * 60,
        provider="xai",
        model="grok-4.5",
        effort="max",
        last_state="frozen:terminal",
    )
    base.update(kw)
    return Trip(**base)


class _FakeActions:
    def __init__(self, interrupt=True, lease=True, notify=True, raise_on=None):
        self._interrupt = interrupt
        self._lease = lease
        self._notify = notify
        self._raise_on = raise_on or set()
        self.calls = []

    def interrupt(self, session_id, reason):
        self.calls.append(("interrupt", session_id, reason))
        if "interrupt" in self._raise_on:
            raise RuntimeError("boom")
        return self._interrupt

    def release_lease(self, session_id):
        self.calls.append(("release_lease", session_id))
        if "release_lease" in self._raise_on:
            raise RuntimeError("boom")
        return self._lease

    def notify(self, text):
        self.calls.append(("notify", text))
        if "notify" in self._raise_on:
            raise RuntimeError("boom")
        return self._notify


def test_enforce_runs_all_three_effects_in_order():
    actions = _FakeActions()
    result = GuardEnforcer(actions).enforce(_trip())
    kinds = [c[0] for c in actions.calls]
    assert kinds == ["interrupt", "release_lease", "notify"]
    assert result.interrupted and result.lease_released and result.notified
    assert result.killed
    assert not result.errors


def test_enforce_reports_even_when_interrupt_fails():
    actions = _FakeActions(raise_on={"interrupt"})
    result = GuardEnforcer(actions).enforce(_trip())
    # interrupt raised, but lease + notify still ran
    assert any(c[0] == "notify" for c in actions.calls)
    assert result.notified is True
    assert any("interrupt failed" in e for e in result.errors)


def test_enforce_never_raises_on_notify_failure():
    actions = _FakeActions(raise_on={"notify"})
    result = GuardEnforcer(actions).enforce(_trip())  # must not raise
    assert result.interrupted is True
    assert result.notified is False
    assert any("notify failed" in e for e in result.errors)


def test_killed_false_if_only_lease_released():
    actions = _FakeActions(interrupt=False, lease=True)
    result = GuardEnforcer(actions).enforce(_trip())
    assert result.interrupted is False
    assert result.lease_released is True
    assert result.killed is False


def test_notify_receives_the_formatted_report():
    actions = _FakeActions()
    result = GuardEnforcer(actions).enforce(_trip())
    notify_text = [c[1] for c in actions.calls if c[0] == "notify"][0]
    assert notify_text == result.report
    assert "20260725_001655_7eff2a" in notify_text


# -- report formatter ---------------------------------------------------------


def test_report_contains_all_key_facts_without_claiming_enforcement_side_effects():
    r = format_kill_report(_trip())
    assert "HARD-STOP REQUIRED" in r
    assert "20260725_001655_7eff2a" in r
    assert "wall_clock_runtime_exceeded" in r
    assert "288.0M tokens" in r          # humanized detector counter
    assert "1800 model calls" in r
    assert "xai" in r and "grok-4.5" in r and "effort=max" in r
    assert "provenance: unknown" in r
    assert "enforcement outcome: not asserted" in r
    assert "turn aborted" not in r
    assert "lease released" not in r
    assert "No human action required" not in r


def test_report_humanizes_token_scales():
    assert "1.0K tokens" in format_kill_report(_trip(estimated_tokens=1000))
    assert "2.0M tokens" in format_kill_report(_trip(estimated_tokens=2_000_000))
    assert "3.0B tokens" in format_kill_report(_trip(estimated_tokens=3_000_000_000))
    assert "500 tokens" in format_kill_report(_trip(estimated_tokens=500))


def test_report_omits_last_state_when_absent():
    r = format_kill_report(_trip(last_state=None))
    assert "last state" not in r
