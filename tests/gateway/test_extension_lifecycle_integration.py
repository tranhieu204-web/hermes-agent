"""RED-first integration contracts for durable guard extensions."""

from types import SimpleNamespace

from gateway.fleet_safety.deadloop_guard import (
    GuardOutcome,
    GuardThresholds,
    RunawayGuard,
    SessionObservation,
)
from gateway.fleet_safety.extension_lifecycle import ExtensionRegistry
from gateway.fleet_safety.integration import (
    _get_or_create_guard,
    deny_active_extensions,
)


def _thresholds():
    return GuardThresholds(
        window_seconds=900.0,
        max_tokens_per_window=100,
        max_calls_per_window=100,
        max_runtime_seconds=10_000,
        no_progress_samples=3,
    )


def _observation():
    return SessionObservation(
        session_id="session-a",
        started_at=100.0,
        api_call_count=1,
        tokens_used=200,
        input_tokens=100,
        output_tokens=20,
        cache_read_tokens=80,
        usage_quality="measured",
        progress_seq=1,
        attempt_seq=1,
        state_hash="p_seq:1:attempt:1",
    )


def test_guard_restart_deduplicates_delivered_extension_notice(tmp_path):
    path = tmp_path / "extensions.json"
    first_guard = RunawayGuard(
        _thresholds(), extension_registry=ExtensionRegistry(path)
    )
    first = first_guard.observe(_observation(), now=120.0)

    assert first is not None
    assert first.outcome is GuardOutcome.CONTINUATION_NOTICE
    assert first.extension_event_id
    first_guard.mark_extension_notice_delivered(first.extension_event_id)

    restarted = RunawayGuard(
        _thresholds(), extension_registry=ExtensionRegistry(path)
    )
    duplicate = restarted.observe(_observation(), now=125.0)
    assert duplicate is None


def test_guard_renews_expired_extension_with_new_event(tmp_path):
    path = tmp_path / "extensions.json"
    guard = RunawayGuard(_thresholds(), extension_registry=ExtensionRegistry(path))
    first = guard.observe(_observation(), now=120.0)
    guard.mark_extension_notice_delivered(first.extension_event_id)
    guard.extension_registry.expire(first.extension_event_id, now=180.0)

    restarted = RunawayGuard(
        _thresholds(), extension_registry=ExtensionRegistry(path)
    )
    renewed = restarted.observe(_observation(), now=181.0)

    assert renewed is not None
    assert renewed.outcome is GuardOutcome.CONTINUATION_NOTICE
    assert renewed.extension_event_id != first.extension_event_id


def test_guard_durable_denial_becomes_verified_stop_after_restart(tmp_path):
    path = tmp_path / "extensions.json"
    guard = RunawayGuard(_thresholds(), extension_registry=ExtensionRegistry(path))
    first = guard.observe(_observation(), now=120.0)
    guard.extension_registry.deny(first.extension_event_id, now=121.0)

    restarted = RunawayGuard(
        _thresholds(), extension_registry=ExtensionRegistry(path)
    )
    denied = restarted.observe(_observation(), now=122.0)

    assert denied is not None
    assert denied.outcome is GuardOutcome.VERIFIED_HARD_STOP
    assert denied.is_hard_stop is True
    assert "denied" in denied.detail.lower()


def test_gateway_guard_factory_uses_restart_persistent_registry_path(tmp_path):
    path = tmp_path / "extensions.json"
    first_runner = SimpleNamespace(_deadloop_extension_registry_path=path)
    first = _get_or_create_guard(first_runner, _thresholds())
    notice = first.observe(_observation(), now=120.0)
    first.mark_extension_notice_delivered(notice.extension_event_id)

    second_runner = SimpleNamespace(_deadloop_extension_registry_path=path)
    second = _get_or_create_guard(second_runner, _thresholds())

    assert second.observe(_observation(), now=125.0) is None


def test_stop_bridge_denies_active_extension_for_runner_session(tmp_path):
    path = tmp_path / "extensions.json"
    runner = SimpleNamespace(_deadloop_extension_registry_path=path)
    guard = _get_or_create_guard(runner, _thresholds())
    notice = guard.observe(_observation(), now=120.0)

    denied_count = deny_active_extensions(runner, ["session-a"], now=121.0)

    assert denied_count == 1
    assert guard.extension_registry.get(notice.extension_event_id).should_continue is False
