"""Plaintext STOP must use the existing hard-stop route, exactly."""

from types import SimpleNamespace
import asyncio

import pytest

from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    Platform,
    SendResult,
    coerce_plaintext_gateway_command,
)
from gateway.run import GatewayRunner, _INTERRUPT_REASON_STOP
from gateway.session import SessionSource


class _RunningAgent:
    def get_activity_summary(self):
        return {
            "seconds_since_activity": 0,
            "api_call_count": 2,
            "max_iterations": 2,
        }


class _SessionStore:
    def __init__(self, session_key: str):
        self._session_key = session_key

    def get_or_create_session(self, _source):
        return SimpleNamespace(
            session_key=self._session_key,
            model=None,
            base_url=None,
            api_key=None,
            provider=None,
        )


class _Adapter(BasePlatformAdapter):
    """Small real adapter surface for exercising inbound command dispatch."""

    def __init__(self):
        config = SimpleNamespace(
            extra={
                "group_sessions_per_user": True,
                "thread_sessions_per_user": False,
            },
            typing_indicator=False,
        )
        super().__init__(config, Platform.TELEGRAM)
        self.sent = []

    async def connect(self, *, is_reconnect=False):
        return True

    async def disconnect(self):
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(
            {
                "chat_id": chat_id,
                "content": content,
                "reply_to": reply_to,
                "metadata": metadata,
            }
        )
        return SendResult(success=True, message_id=f"sent-{len(self.sent)}")

    async def get_chat_info(self, chat_id):
        return {"name": chat_id, "type": "dm"}


def _source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_type="dm",
        chat_id="chat-1",
        user_id="user-1",
    )


def _event(text: str) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=_source(),
    )


@pytest.mark.parametrize("raw", ["STOP", "stop", "  StOp\n"])
def test_exact_plaintext_stop_is_normalized_to_registered_stop_command(raw):
    event = _event(raw)

    assert coerce_plaintext_gateway_command(event) is True
    assert event.text == "/stop"
    assert event.get_command() == "stop"


@pytest.mark.parametrize(
    "raw",
    [
        "please STOP now",
        "the process should stop",
        "STOP please",
        "STOP!",
        "unstoppable",
        "/stop",
    ],
)
def test_plaintext_stop_does_not_match_prose_substrings_or_slash_commands(raw):
    event = _event(raw)
    original = event.text

    assert coerce_plaintext_gateway_command(event) is False
    assert event.text == original


def test_exact_plaintext_stop_follows_existing_active_run_hard_stop_route():
    async def _exercise():
        runner = object.__new__(GatewayRunner)
        session_key = "agent:main:telegram:dm:chat-1"
        runner._running_agents = {session_key: _RunningAgent()}
        runner._running_agents_ts = {session_key: 0}
        runner._session_run_generations = {}
        runner.session_store = _SessionStore(session_key)
        runner._coalescer = None
        runner._run_immediate_replay_tasks = {}
        runner._is_user_authorized = lambda _source: True
        runner._check_slash_access = lambda _source, _name: None
        runner._sibling_thread_run_keys = lambda _source, _key: []

        calls = []

        async def _interrupt_and_clear(
            key,
            source,
            *,
            interrupt_reason,
            invalidation_reason,
        ):
            calls.append(
                (key, source, interrupt_reason, invalidation_reason)
            )

        runner._interrupt_and_clear_session = _interrupt_and_clear

        adapter = _Adapter()
        adapter.set_message_handler(runner._handle_message)
        active_task = asyncio.create_task(asyncio.Event().wait())
        adapter._active_sessions[session_key] = asyncio.Event()
        adapter._session_tasks[session_key] = active_task

        event = _event("  STOP  ")
        try:
            await adapter.handle_message(event)
        finally:
            if not active_task.done():
                active_task.cancel()
            await asyncio.gather(active_task, return_exceptions=True)
        return event, adapter.sent, calls, session_key

    event, sent, calls, session_key = asyncio.run(_exercise())

    assert event.text == "/stop"
    assert len(calls) == 1
    assert calls[0][0] == session_key
    assert calls[0][2:] == (_INTERRUPT_REASON_STOP, "stop_command")
    assert len(sent) == 1
    assert "stopped" in sent[0]["content"].lower()
