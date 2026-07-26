"""RED-first tests for durable default-continue extension lifecycle."""

from gateway.fleet_safety.extension_lifecycle import (
    ExtensionRegistry,
    ExtensionState,
)


def _request(registry, *, now=100.0):
    return registry.request(
        session_id="private-session-id",
        checkpoint_key="turn-1:token-rate",
        now=now,
        duration_seconds=60.0,
        grant_size=50,
    )


def test_request_is_active_by_default_and_notice_is_deduplicated(tmp_path):
    registry = ExtensionRegistry(tmp_path / "extensions.json")

    first, notify_first = _request(registry)
    duplicate, notify_duplicate = _request(registry, now=110.0)

    assert first.state is ExtensionState.ACTIVE
    assert first.should_continue is True
    assert first.revision == 1
    assert first.grant_size == 50
    assert notify_first is True
    assert duplicate.event_id == first.event_id
    assert notify_duplicate is True

    registry.mark_notice_delivered(first.event_id)
    same, notify_after_delivery = _request(registry, now=120.0)
    assert same.event_id == first.event_id
    assert notify_after_delivery is False


def test_registry_reload_preserves_active_extension_and_notice_dedupe(tmp_path):
    path = tmp_path / "extensions.json"
    registry = ExtensionRegistry(path)
    record, _ = _request(registry)
    registry.mark_notice_delivered(record.event_id)

    reloaded = ExtensionRegistry(path)
    restored, should_notify = _request(reloaded, now=130.0)

    assert restored.event_id == record.event_id
    assert restored.state is ExtensionState.ACTIVE
    assert restored.should_continue is True
    assert should_notify is False


def test_approval_is_durable_acknowledgement_not_a_prerequisite(tmp_path):
    path = tmp_path / "extensions.json"
    registry = ExtensionRegistry(path)
    record, _ = _request(registry)

    approved = registry.approve(record.event_id, now=105.0)
    assert approved.state is ExtensionState.APPROVED
    assert approved.should_continue is True

    restored = ExtensionRegistry(path).get(record.event_id)
    assert restored.state is ExtensionState.APPROVED
    assert restored.decision_at == 105.0


def test_denial_is_durable_stop_and_cannot_be_silently_reopened(tmp_path):
    path = tmp_path / "extensions.json"
    registry = ExtensionRegistry(path)
    record, _ = _request(registry)

    denied = registry.deny(record.event_id, now=106.0)
    assert denied.state is ExtensionState.DENIED
    assert denied.should_continue is False

    same, should_notify = ExtensionRegistry(path).request(
        session_id="private-session-id",
        checkpoint_key="turn-1:token-rate",
        now=120.0,
        duration_seconds=60.0,
        grant_size=50,
    )
    assert same.event_id == record.event_id
    assert same.state is ExtensionState.DENIED
    assert same.should_continue is False
    assert should_notify is False


def test_timeout_continues_and_expiry_renews_with_new_event(tmp_path):
    path = tmp_path / "extensions.json"
    registry = ExtensionRegistry(path)
    first, _ = _request(registry)
    registry.mark_notice_delivered(first.event_id)

    timed_out = registry.timeout(first.event_id, now=160.0)
    assert timed_out.state is ExtensionState.TIMED_OUT_CONTINUING
    assert timed_out.should_continue is True

    renewed, should_notify = _request(registry, now=161.0)
    assert renewed.event_id != first.event_id
    assert renewed.revision == 2
    assert renewed.state is ExtensionState.ACTIVE
    assert renewed.should_continue is True
    assert should_notify is True

    reloaded = ExtensionRegistry(path)
    assert reloaded.get(first.event_id).state is ExtensionState.TIMED_OUT_CONTINUING
    assert reloaded.get(renewed.event_id).state is ExtensionState.ACTIVE


def test_explicit_expiry_continues_until_renewal(tmp_path):
    registry = ExtensionRegistry(tmp_path / "extensions.json")
    record, _ = _request(registry)

    expired = registry.expire(record.event_id, now=160.0)
    assert expired.state is ExtensionState.EXPIRED_CONTINUING
    assert expired.should_continue is True

    renewed, should_notify = _request(registry, now=161.0)
    assert renewed.revision == 2
    assert should_notify is True


def test_persisted_file_uses_opaque_scope_not_raw_session_id(tmp_path):
    path = tmp_path / "extensions.json"
    _request(ExtensionRegistry(path))

    raw = path.read_text(encoding="utf-8")
    assert "private-session-id" not in raw
    assert "turn-1:token-rate" not in raw


def test_stop_denies_all_active_extensions_for_session(tmp_path):
    path = tmp_path / "extensions.json"
    registry = ExtensionRegistry(path)
    first, _ = _request(registry)
    second, _ = registry.request(
        session_id="private-session-id",
        checkpoint_key="turn-1:call-rate",
        now=101.0,
        duration_seconds=60.0,
        grant_size=10,
    )
    other, _ = registry.request(
        session_id="other-session",
        checkpoint_key="turn-1:token-rate",
        now=101.0,
        duration_seconds=60.0,
        grant_size=10,
    )

    denied = registry.deny_active_for_session("private-session-id", now=110.0)

    assert {record.event_id for record in denied} == {first.event_id, second.event_id}
    assert all(record.state is ExtensionState.DENIED for record in denied)
    reloaded = ExtensionRegistry(path)
    assert reloaded.get(first.event_id).should_continue is False
    assert reloaded.get(second.event_id).should_continue is False
    assert reloaded.get(other.event_id).should_continue is True
