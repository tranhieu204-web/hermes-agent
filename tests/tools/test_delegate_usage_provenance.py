from types import SimpleNamespace

from agent.progress_telemetry import ProgressTelemetry
from agent.usage_provenance import UsageProvenance
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
