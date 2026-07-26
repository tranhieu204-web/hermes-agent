import json
from types import SimpleNamespace

from agent.progress_telemetry import ProgressTelemetry
from agent.usage_provenance import (
    UsageProvenance,
    capture_delegated_usage_receipt_sink,
)
from tools.delegate_tool import _record_delegated_child_usage


def _parent():
    telemetry = ProgressTelemetry(session_id="parent-session")
    return SimpleNamespace(
        session_id="parent-session",
        _progress_telemetry=telemetry,
    )


def _measured_child_usage():
    return {
        "session_id": "child-session",
        "provenance": "measured",
        "known_total": 34,
        "unknown_components": 0,
        "components": [
            {
                "component_id": "child-response:1",
                "source": "model_response",
                "session_id": "child-session",
                "provenance": "measured",
                "authority": "backend_observed",
                "authoritative": True,
                "accepted_event_id": "child-event:1",
                "total_tokens": 34,
                "details": {"total_tokens": 34},
            }
        ],
    }


def test_terminal_child_without_usage_records_unknown_receipt():
    parent = _parent()

    receipt = _record_delegated_child_usage(
        parent,
        task_index=0,
        child_session_id="terminal-child-session",
        child_subagent_id="terminal-child-1",
        child_usage=None,
    )

    assert receipt.provenance is UsageProvenance.UNKNOWN
    assert receipt.reason == "child_session_or_usage_missing"
    assert receipt.authoritative is False
    assert parent._progress_telemetry.usage_aggregate.provenance is UsageProvenance.UNKNOWN


def test_session_matched_backend_measured_child_is_authoritative():
    parent = _parent()

    receipt = _record_delegated_child_usage(
        parent,
        task_index=1,
        child_session_id="child-session",
        child_subagent_id="measured-child-1",
        child_usage=_measured_child_usage(),
    )

    assert receipt.provenance is UsageProvenance.MEASURED
    assert receipt.authority == "delegated_child_backend"
    assert receipt.authoritative is True
    assert receipt.total_tokens == 34


def test_measured_parent_plus_unknown_child_is_mixed_and_replay_is_noop():
    parent = _parent()
    parent._progress_telemetry.record_usage_component(
        component_id="parent-response:1",
        source="model_response",
        session_id="parent-session",
        provenance="measured",
        authority="provider_response",
        authoritative=True,
        accepted_event_id="parent-event:1",
        total_tokens=21,
    )

    kwargs = {
        "task_index": 2,
        "child_session_id": "terminal-child-session",
        "child_subagent_id": "terminal-child-2",
        "child_usage": None,
    }
    _record_delegated_child_usage(parent, **kwargs)
    _record_delegated_child_usage(parent, **kwargs)

    aggregate = parent._progress_telemetry.usage_aggregate
    assert aggregate.provenance is UsageProvenance.MIXED
    assert aggregate.known_total == 21
    assert aggregate.unknown_component_count == 1
    assert len(aggregate.components) == 2


def test_completion_receipt_uses_dispatch_captured_session_and_ledger():
    parent = _parent()
    dispatch_session_id = parent.session_id
    dispatch_telemetry = parent._progress_telemetry

    # A parent can rotate sessions while a background child is still running.
    # Completion must never be credited to whichever session happens to be
    # current at callback time.
    parent.session_id = "rotated-session"
    parent._progress_telemetry = ProgressTelemetry(session_id="rotated-session")

    receipt = _record_delegated_child_usage(
        parent,
        task_index=4,
        child_session_id="child-session",
        child_subagent_id="dispatch-bound-child",
        child_usage=_measured_child_usage(),
        parent_session_id=dispatch_session_id,
        telemetry=dispatch_telemetry,
    )

    assert receipt.session_id == dispatch_session_id
    assert dispatch_telemetry.usage_aggregate.known_total == 34
    assert parent._progress_telemetry.usage_aggregate.known_total == 0


def test_frozen_dispatch_ledger_persists_unknown_without_mutating_history():
    parent = _parent()
    dispatch_telemetry = parent._progress_telemetry
    sink = capture_delegated_usage_receipt_sink(parent)

    dispatch_telemetry.freeze()
    parent.session_id = "rotated-session"
    parent._progress_telemetry = ProgressTelemetry(session_id="rotated-session")

    receipt = _record_delegated_child_usage(
        parent,
        task_index=5,
        child_session_id="child-session",
        child_subagent_id="late-child",
        child_usage=_measured_child_usage(),
        parent_session_id="parent-session",
        telemetry=dispatch_telemetry,
        receipt_sink=sink,
    )

    assert receipt.session_id == "parent-session"
    assert receipt.provenance is UsageProvenance.UNKNOWN
    assert receipt.reason == "dispatch_parent_ledger_frozen"
    assert dispatch_telemetry.usage_aggregate.component_count == 0
    assert sink.receipts == (receipt,)
    assert parent._progress_telemetry.usage_aggregate.component_count == 0


def test_foreground_aggregation_keeps_dispatch_usage_owner_when_parent_rotates(monkeypatch):
    import tools.delegate_tool as delegate_tool

    durable_receipts = []

    class ReceiptStore:
        def record_delegated_usage_receipt(self, *, parent_session_id, receipt):
            durable_receipts.append((parent_session_id, receipt))

    dispatch_telemetry = ProgressTelemetry(session_id="parent-session")
    parent = SimpleNamespace(
        session_id="parent-session",
        _progress_telemetry=dispatch_telemetry,
        _session_db=ReceiptStore(),
        _delegate_depth=0,
        _active_children=[],
        _active_children_lock=None,
        _interrupt_requested=False,
        model="test-model",
        session_estimated_cost_usd=0.0,
        session_cost_source="none",
        session_cost_status="unknown",
    )
    child = SimpleNamespace(
        session_id="child-session",
        _subagent_id="foreground-child",
        _delegate_role="leaf",
        tool_progress_callback=None,
        model="test-model",
    )
    creds = {
        "model": "test-model",
        "provider": None,
        "base_url": None,
        "api_key": None,
        "api_mode": None,
        "request_overrides": None,
        "max_output_tokens": None,
        "command": None,
        "args": None,
    }

    monkeypatch.setattr(delegate_tool, "_load_config", lambda: {"max_iterations": 1})
    monkeypatch.setattr(
        delegate_tool, "_resolve_delegation_credentials", lambda *_args: creds
    )
    monkeypatch.setattr(delegate_tool, "_build_child_agent", lambda **_kwargs: child)

    def finish_after_rotation(*_args, **_kwargs):
        dispatch_telemetry.freeze()
        parent.session_id = "rotated-session"
        parent._progress_telemetry = ProgressTelemetry(session_id="rotated-session")
        return {
            "task_index": 0,
            "status": "completed",
            "summary": "done",
            "api_calls": 1,
            "duration_seconds": 0.1,
            "_child_session_id": "child-session",
            "_child_subagent_id": "foreground-child",
            "_child_usage": _measured_child_usage(),
        }

    monkeypatch.setattr(delegate_tool, "_run_single_child", finish_after_rotation)

    result = json.loads(
        delegate_tool.delegate_task(
            goal="finish after rotation", background=False, parent_agent=parent
        )
    )

    assert result["results"][0]["status"] == "completed"
    assert dispatch_telemetry.usage_aggregate.component_count == 0
    assert parent._progress_telemetry.usage_aggregate.component_count == 0
    assert len(durable_receipts) == 1
    parent_session_id, receipt = durable_receipts[0]
    assert parent_session_id == "parent-session"
    assert receipt["provenance"] == "unknown"
    assert receipt["reason"] == "dispatch_parent_ledger_frozen"


def test_durable_receipt_store_is_replay_idempotent_and_keeps_distinct_completions(
    tmp_path,
):
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session(session_id="parent-session", source="test")
        parent = _parent()
        parent._session_db = db
        sink = capture_delegated_usage_receipt_sink(parent)
        parent._progress_telemetry.freeze()

        common = {
            "task_index": 0,
            "child_session_id": "child-session",
            "child_usage": _measured_child_usage(),
            "parent_session_id": "parent-session",
            "telemetry": parent._progress_telemetry,
            "receipt_sink": sink,
        }
        first = _record_delegated_child_usage(
            parent, child_subagent_id="completion-one", **common
        )
        replay = _record_delegated_child_usage(
            parent, child_subagent_id="completion-one", **common
        )
        second = _record_delegated_child_usage(
            parent, child_subagent_id="completion-two", **common
        )

        assert replay is first
        assert second.component_id != first.component_id
        rows = db.get_delegated_usage_receipts("parent-session")
        assert [row["component_id"] for row in rows] == [
            "delegated-child:completion-one",
            "delegated-child:completion-two",
        ]
        assert all(row["provenance"] == "unknown" for row in rows)
        assert all(row["reason"] == "dispatch_parent_ledger_frozen" for row in rows)
    finally:
        db.close()


def test_background_delayed_completion_uses_dispatch_receipt_owner(monkeypatch):
    import gateway.session_context as session_context
    import tools.async_delegation as async_delegation
    import tools.delegate_tool as delegate_tool

    durable_receipts = []
    captured = {}

    class ReceiptStore:
        def record_delegated_usage_receipt(self, *, parent_session_id, receipt):
            durable_receipts.append((parent_session_id, receipt))

    dispatch_telemetry = ProgressTelemetry(session_id="parent-session")
    parent = SimpleNamespace(
        session_id="parent-session",
        _progress_telemetry=dispatch_telemetry,
        _session_db=ReceiptStore(),
        _delegate_depth=0,
        _active_children=[],
        _active_children_lock=None,
        _interrupt_requested=False,
        model="test-model",
        session_estimated_cost_usd=0.0,
        session_cost_source="none",
        session_cost_status="unknown",
    )
    child = SimpleNamespace(
        session_id="child-session",
        _subagent_id="background-child",
        _delegate_role="leaf",
        tool_progress_callback=None,
        model="test-model",
    )
    creds = {
        "model": "test-model",
        "provider": None,
        "base_url": None,
        "api_key": None,
        "api_mode": None,
        "request_overrides": None,
        "max_output_tokens": None,
        "command": None,
        "args": None,
    }

    monkeypatch.setattr(delegate_tool, "_load_config", lambda: {"max_iterations": 1})
    monkeypatch.setattr(
        delegate_tool, "_resolve_delegation_credentials", lambda *_args: creds
    )
    monkeypatch.setattr(delegate_tool, "_build_child_agent", lambda **_kwargs: child)
    monkeypatch.setattr(
        delegate_tool,
        "_run_single_child",
        lambda *_args, **_kwargs: {
            "task_index": 0,
            "status": "completed",
            "summary": "done",
            "api_calls": 1,
            "duration_seconds": 0.1,
            "_child_session_id": "child-session",
            "_child_subagent_id": "background-child",
            "_child_usage": _measured_child_usage(),
        },
    )
    monkeypatch.setattr(session_context, "async_delivery_supported", lambda: True)

    def fake_dispatch(**kwargs):
        captured.update(kwargs)
        return {"status": "dispatched", "delegation_id": "delegation-id"}

    monkeypatch.setattr(
        async_delegation, "dispatch_async_delegation_batch", fake_dispatch
    )

    dispatched = json.loads(
        delegate_tool.delegate_task(
            goal="finish later", background=True, parent_agent=parent
        )
    )
    assert dispatched["status"] == "dispatched"
    assert captured["parent_session_id"] == "parent-session"

    dispatch_telemetry.freeze()
    parent.session_id = "rotated-session"
    parent._progress_telemetry = ProgressTelemetry(session_id="rotated-session")
    combined = captured["runner"]()

    assert combined["results"][0]["status"] == "completed"
    assert dispatch_telemetry.usage_aggregate.component_count == 0
    assert parent._progress_telemetry.usage_aggregate.component_count == 0
    assert len(durable_receipts) == 1
    parent_session_id, receipt = durable_receipts[0]
    assert parent_session_id == "parent-session"
    assert receipt["reason"] == "dispatch_parent_ledger_frozen"
