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


def test_safety_stop_requests_interrupt_then_reports_without_direct_release():
    actions = _FakeActions(interrupt=True)
    result = GuardEnforcer(actions).enforce(_trip())

    assert [c[0] for c in actions.calls] == ["interrupt", "notify"]
    assert result.stop_requested is True
    assert result.interrupted is True  # compatibility: request accepted, not termination
    assert result.lease_released is False
    assert result.killed is False
    assert result.terminated is False
    assert result.notified is True
    assert "Interrupt request accepted: yes" in result.report
    assert "Lease: retained until generation-safe gateway unwind" in result.report


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

    assert result.stop_requested is False
    assert result.killed is False
    assert result.notified is True
    assert "Interrupt request accepted: no" in result.report
    assert any("interrupt failed" in error for error in result.errors)


def test_delivery_failure_is_not_reported_as_success():
    actions = _FakeActions(interrupt=True, notify=False)
    result = GuardEnforcer(actions).enforce(_trip())
    assert result.stop_requested is True
    assert result.notified is False


def test_continuation_notice_only_notifies():
    actions = _FakeActions()
    trip = _trip(
        outcome=GuardOutcome.CONTINUATION_NOTICE,
        is_hard_stop=False,
        reason=TripReason.TOKEN_RATE,
        trip_reason=TripReason.TOKEN_RATE,
        detail="usage checkpoint crossed while progress remains unknown",
        extension_grant_size=40,
        extension_expires_at=1_722_222_222.0,
        extension_revision=2,
    )
    result = GuardEnforcer(actions).enforce(trip)

    assert [c[0] for c in actions.calls] == ["notify"]
    assert result.stop_requested is False
    assert result.killed is False
    assert result.lease_released is False
    assert "Extension checkpoint" in result.report
    assert "Extension grant: 40 model calls" in result.report
    assert "Extension revision: 2" in result.report
    assert "Extension expires at:" in result.report
    assert "Continuing by default. Send STOP or /stop to cancel." in result.report


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


def test_continuation_report_distinguishes_unknown_usage():
    report = format_continuation_report(
        _trip(
            outcome=GuardOutcome.CONTINUATION_NOTICE,
            is_hard_stop=False,
            usage_quality="unknown",
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cache_write_tokens=0,
            reasoning_tokens=0,
            cost=0.0,
            cost_status="unknown",
            cost_source="none",
        )
    )
    assert "Usage provenance: unknown" in report
    assert "Cost: unknown" in report
    assert "private-session-id-must-not-leak" not in report
