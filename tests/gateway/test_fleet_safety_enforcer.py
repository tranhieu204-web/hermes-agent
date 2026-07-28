"""RED-first contracts for honest safety-stop enforcement and reporting."""

from gateway.fleet_safety.deadloop_guard import GuardOutcome, Trip, TripReason
from gateway.fleet_safety.enforcer import GuardEnforcer
from gateway.fleet_safety.report import format_continuation_report, format_kill_report


def _trip(**kw):
    base = dict(
        session_id="private-session-id-must-not-leak",
        reason=TripReason.NO_PROGRESS,
        trip_reason=TripReason.NO_PROGRESS,
        outcome=GuardOutcome.VERIFIED_HARD_STOP,
        is_hard_stop=True,
        detail="producer verified no progress across 3 distinct attempts",
        estimated_tokens=1_000,
        estimated_calls=7,
        runtime_seconds=90,
        provider="openai-codex",
        model="gpt-test",
        effort="xhigh",
        last_state="internal-state-must-not-leak",
        usage_quality="measured",
        input_tokens=100,
        output_tokens=20,
        cache_read_tokens=800,
        cache_write_tokens=50,
        reasoning_tokens=30,
        cost=0.25,
        cost_status="estimated",
        cost_source="test-pricing",
    )
    base.update(kw)
    return Trip(**base)


class _FakeActions:
    def __init__(self, interrupt=True, notify=True, raise_on=None):
        self._interrupt = interrupt
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
        raise AssertionError("guard enforcer must not release generation-owned leases")

    def notify(self, text):
        self.calls.append(("notify", text))
        if "notify" in self._raise_on:
            raise RuntimeError("boom")
        return self._notify


def test_enforce_requests_stop_then_notifies_without_releasing_lease():
    actions = _FakeActions()
    result = GuardEnforcer(actions).enforce(_trip())
    kinds = [c[0] for c in actions.calls]
    assert kinds == ["interrupt", "notify"]
    assert result.stop_requested is True
    assert result.interrupted is True
    assert result.lease_released is False
    assert result.notified is True
    assert result.killed is False
    assert "Interrupt request accepted: yes" in result.report
    assert "Lease: retained until generation-safe gateway unwind" in result.report
    assert not result.errors


def test_failed_interrupt_is_reported_honestly_and_still_notifies():
    actions = _FakeActions(interrupt=False)
    result = GuardEnforcer(actions).enforce(_trip())

    assert [c[0] for c in actions.calls] == ["interrupt", "notify"]
    assert result.stop_requested is False
    assert result.interrupted is False
    assert result.killed is False
    assert "Interrupt request accepted: no" in result.report
    assert result.notified is True


def test_interrupt_exception_does_not_claim_success_or_skip_notification():
    actions = _FakeActions(raise_on={"interrupt"})
    result = GuardEnforcer(actions).enforce(_trip())
    # Interrupt raised, so the lease stays held; notification still runs.
    assert any(c[0] == "notify" for c in actions.calls)
    assert not any(c[0] == "release_lease" for c in actions.calls)
    assert result.notified is True
    assert "Interrupt request accepted: no" in result.report
    assert any("interrupt failed" in error for error in result.errors)


def test_delivery_failure_is_not_reported_as_success():
    actions = _FakeActions(interrupt=True, notify=False)
    result = GuardEnforcer(actions).enforce(_trip())
    assert result.stop_requested is True
    assert result.notified is False


def test_unconfirmed_interrupt_cannot_release_lease_or_claim_kill():
    actions = _FakeActions(interrupt=False, lease=True)
    result = GuardEnforcer(actions).enforce(_trip())
    assert result.interrupted is False
    assert result.lease_released is False
    assert result.killed is False
    assert not any(c[0] == "release_lease" for c in actions.calls)


def test_notify_receives_the_formatted_report():
    actions = _FakeActions()
    result = GuardEnforcer(actions).enforce(_trip())
    notify_text = [c[1] for c in actions.calls if c[0] == "notify"][0]
    assert notify_text == result.report
    assert "20260725_001655_7eff2a" not in notify_text
    assert "Safety stop requested" in notify_text


def test_safety_report_is_plain_truthful_and_has_separate_usage_dimensions():
    report = format_kill_report(_trip(), interrupt_request_accepted=True)

    assert "Safety stop requested" in report
    assert "Usage provenance: measured" in report
    assert "Input tokens: 100" in report
    assert "Output tokens: 20" in report
    assert "Cache read tokens: 800" in report
    assert "Cache write tokens: 50" in report
    assert "Reasoning tokens: 30" in report
    assert "Cost: 0.250000 (estimated; test-pricing)" in report
    assert "Model calls: 7" in report
    assert "private-session-id-must-not-leak" not in report
    assert "internal-state-must-not-leak" not in report
    assert "spend" not in report.lower()
    assert "killed" not in report.lower()
    assert "hard-stopped" not in report.lower()
    assert "dead-loop" not in report.lower()
    assert "<br>" not in report
    assert "&nbsp;" not in report


def test_report_contains_truthful_request_receipt_without_raw_state():
    r = format_kill_report(_trip())
    assert "Safety stop requested" in r
    assert "20260725_001655_7eff2a" not in r
    assert "wall_clock_runtime_exceeded" in r
    assert "Model calls: 1800" in r
    assert "Runtime seconds: 46800.0" in r
    assert "xai" in r and "grok-4.5" in r and "effort=max" in r
    assert "Usage provenance: unknown" in r
    assert "frozen:terminal" not in r
    assert "lease released" not in r.lower()


def test_report_keeps_usage_dimensions_and_cost_status_separate():
    report = format_kill_report(
        _trip(
            usage_quality="measured",
            input_tokens=1000,
            output_tokens=200,
            cache_read_tokens=300,
            cache_write_tokens=40,
            reasoning_tokens=50,
            cost=1.25,
            cost_status="estimated",
            cost_source="test",
        )
    )
    assert "Input tokens: 1000" in report
    assert "Output tokens: 200" in report
    assert "Cache read tokens: 300" in report
    assert "Cache write tokens: 40" in report
    assert "Reasoning tokens: 50" in report
    assert "Cost: 1.250000 (estimated; test)" in report


def test_report_omits_last_state_when_absent():
    r = format_kill_report(_trip(last_state=None))
    assert "last state" not in r


def test_continuation_notice_only_notifies():
    actions = _FakeActions()
    trip = _trip(
        outcome=GuardOutcome.CONTINUATION_NOTICE,
        is_hard_stop=False,
        notice_text="Extension checkpoint",
        extension_grant_size=100,
        extension_revision=1,
    )

    result = GuardEnforcer(actions).enforce(trip)

    assert [call[0] for call in actions.calls] == ["notify"]
    assert result.stop_requested is False
    assert result.interrupted is False
    assert result.lease_released is False
    assert result.notified is True
    assert "Extension checkpoint" in result.report
    assert "Continuing by default" in result.report
