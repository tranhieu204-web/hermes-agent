"""Regression test: /retry must return the agent response, not None.

Before the fix in PR #441, _handle_retry_command() called
_handle_message(retry_event) but discarded its return value with `return None`,
so users never received the final response.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock
from gateway.run import GatewayRunner
from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import AsyncSessionStore, SessionSource, SessionStore


@pytest.fixture
def gateway(tmp_path):
    config = MagicMock()
    config.sessions_dir = tmp_path
    config.max_context_messages = 20
    gw = GatewayRunner.__new__(GatewayRunner)
    gw.config = config
    gw.session_store = MagicMock()
    return gw


@pytest.mark.asyncio
async def test_retry_returns_response_not_none(gateway):
    """_handle_retry_command must return the inner handler response, not None."""
    gateway.session_store.get_or_create_session.return_value = MagicMock(
        session_id="test-session"
    )
    gateway.session_store.load_transcript.return_value = [
        {"role": "user", "content": "Hello Hermes"},
        {"role": "assistant", "content": "Hi there!"},
    ]
    gateway.session_store.rewrite_transcript = MagicMock()
    expected_response = "Hi there! (retried)"
    gateway._handle_message = AsyncMock(return_value=expected_response)
    event = MessageEvent(
        text="/retry",
        message_type=MessageType.TEXT,
        source=MagicMock(),
    )
    result = await gateway._handle_retry_command(event)
    assert result is not None, "/retry must not return None"
    assert result == expected_response


@pytest.mark.asyncio
async def test_retry_no_previous_message(gateway):
    """If there is no previous user message, return early with a message."""
    gateway.session_store.get_or_create_session.return_value = MagicMock(
        session_id="test-session"
    )
    gateway.session_store.load_transcript.return_value = []
    event = MessageEvent(
        text="/retry",
        message_type=MessageType.TEXT,
        source=MagicMock(),
    )
    result = await gateway._handle_retry_command(event)
    assert result == "No previous message to retry."


@pytest.mark.asyncio
async def test_retry_archives_latest_suffix_and_resends_without_platform_id(
    tmp_path, monkeypatch
):
    """Gateway /retry must archive, not delete, the retried active suffix."""
    import hermes_state

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    config = GatewayConfig()
    store = SessionStore(tmp_path / "sessions", config)
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="p2-retry-chat",
        user_id="p2-retry-user",
    )
    entry = store.get_or_create_session(source)
    history = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "first reply"},
        {"role": "user", "content": "retry me"},
        {"role": "assistant", "content": "replace me"},
    ]
    for message in history:
        store.append_to_transcript(entry.session_id, message)

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = config
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)
    runner._handle_message = AsyncMock(return_value="retried response")
    event = MessageEvent(
        text="/retry",
        message_type=MessageType.TEXT,
        source=source,
    )

    result = await runner._handle_retry_command(event)
    recovery = store._db.export_session(entry.session_id, include_rewound=True)

    assert result == "retried response"
    assert [message["content"] for message in recovery["messages"]] == [
        "first",
        "first reply",
        "retry me",
        "replace me",
    ]
    assert [
        (message["active"], message["compacted"])
        for message in recovery["messages"]
    ] == [(1, 0), (1, 0), (0, 0), (0, 0)]
    retry_event = runner._handle_message.await_args.args[0]
    assert retry_event.text == "retry me"
    assert retry_event.message_id is None


@pytest.mark.asyncio
async def test_retry_failed_persistence_preserves_tokens_and_prevents_resend():
    """A failed canonical rewind must not reset tokens or reach resend."""
    source = MagicMock()
    entry = MagicMock(session_id="p2-failed-retry", last_prompt_tokens=731)
    async_store = MagicMock()
    async_store.get_or_create_session = AsyncMock(return_value=entry)
    async_store.load_transcript = AsyncMock(
        return_value=[
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "first reply"},
            {"role": "user", "content": "retry me"},
            {"role": "assistant", "content": "replace me"},
        ]
    )
    async_store.rewrite_transcript = AsyncMock(return_value=False)
    async_store.rewind_transcript = AsyncMock(return_value=False)
    backing_store = MagicMock()
    async_store._store = backing_store
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.session_store = backing_store
    runner._async_session_store = async_store
    runner._handle_message = AsyncMock(return_value="must not resend")
    event = MessageEvent(
        text="/retry",
        message_type=MessageType.TEXT,
        source=source,
    )

    result = await runner._handle_retry_command(event)

    assert entry.last_prompt_tokens == 731
    runner._handle_message.assert_not_awaited()
    async_store.rewind_transcript.assert_awaited_once_with(
        entry.session_id,
        async_store.load_transcript.return_value,
        1,
    )
    async_store.rewrite_transcript.assert_not_awaited()
    assert result != "must not resend"
