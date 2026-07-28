"""Producer-side completion envelope identity for replay/restart ordering."""

from __future__ import annotations

import queue
from types import SimpleNamespace
from unittest.mock import patch

from tools.async_delegation import _push_completion_event
from tools.process_registry import ProcessRegistry, ProcessSession


def _completed_process_event(registry: ProcessRegistry, process_id: str) -> dict:
    session = ProcessSession(
        id=process_id,
        command="bounded-test-command",
        session_key="gateway:owner",
        started_at=1234.5,
        exited=True,
        exit_code=0,
        notify_on_complete=True,
    )
    registry._running[process_id] = session
    with patch.object(registry, "_write_checkpoint"):
        registry._move_to_finished(session)
    return registry.completion_queue.get_nowait()


def test_process_completion_identity_survives_registry_restart_and_replay():
    first = _completed_process_event(ProcessRegistry(), "proc_envelope_1")
    recovered = _completed_process_event(ProcessRegistry(), "proc_envelope_1")

    assert first["event_stream_id"]
    assert first["event_sequence"] == 1
    assert recovered["event_stream_id"] == first["event_stream_id"]
    assert recovered["event_sequence"] == first["event_sequence"]


def test_async_completion_persists_producer_stream_and_sequence_before_enqueue():
    target = SimpleNamespace(completion_queue=queue.Queue())
    persisted = []
    record = {
        "delegation_id": "deleg_envelope_1",
        "session_key": "gateway:owner",
        "origin_session_id": "parent-session",
        "parent_session_id": "parent-session",
        "goal": "bounded test",
        "context": None,
        "toolsets": None,
        "role": "worker",
        "model": "test/model",
        "dispatched_at": 100.0,
        "completed_at": 101.0,
    }
    result = {"status": "completed", "summary": "done", "api_calls": 1}

    with (
        patch("tools.process_registry.process_registry", target),
        patch(
            "tools.async_delegation._persist_completion",
            side_effect=lambda event, _result: persisted.append(dict(event)),
        ),
    ):
        _push_completion_event(record, result, "completed")
        # Replaying the producer record after restart must retain the exact
        # envelope identity rather than manufacturing a consumer-local key.
        _push_completion_event(dict(record), result, "completed")

    emitted = [target.completion_queue.get_nowait() for _ in range(2)]
    assert emitted[0]["event_stream_id"]
    assert emitted[0]["event_sequence"] == 1
    assert emitted[1]["event_stream_id"] == emitted[0]["event_stream_id"]
    assert emitted[1]["event_sequence"] == emitted[0]["event_sequence"]
    assert persisted == emitted
