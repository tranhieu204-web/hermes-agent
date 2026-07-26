"""RED-first behavioral contracts for exact standalone STOP normalization."""

import os
import tempfile
from pathlib import Path

import pytest

_TEST_HOME = Path(tempfile.gettempdir()) / "hermes-plaintext-stop-tests"
_TEST_HOME.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("HERMES_HOME", str(_TEST_HOME / "hermes"))
os.environ.setdefault("HOME", str(_TEST_HOME))
os.environ.setdefault("USERPROFILE", str(_TEST_HOME))
os.environ.setdefault("LOCALAPPDATA", str(_TEST_HOME / "localappdata"))

from gateway.config import PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    Platform,
    coerce_plaintext_gateway_command,
)
from gateway.session import SessionSource, build_session_key
from hermes_cli.commands import should_bypass_active_session


def _event(text, *, chat_type="dm", message_type=MessageType.TEXT):
    return MessageEvent(
        text=text,
        message_type=message_type,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_type=chat_type,
            chat_id="chat",
            user_id="user",
        ),
    )


@pytest.mark.parametrize("text", ["STOP", "stop", " Stop ", "\tSTOP\n"])
def test_exact_stop_normalizes_to_existing_slash_stop_path(text):
    event = _event(text)
    coerce_plaintext_gateway_command(event)

    assert event.text == "/stop"
    assert event.get_command() == "stop"
    assert should_bypass_active_session(event.get_command()) is True


@pytest.mark.parametrize(
    "text",
    [
        "do not stop",
        "please stop doing that",
        "STOP now",
        "the word STOP is embedded",
        "stopping",
    ],
)
def test_nonexact_stop_text_remains_ordinary_user_text(text):
    event = _event(text)
    coerce_plaintext_gateway_command(event)
    assert event.text == text
    assert event.get_command() is None


def test_exact_stop_in_group_uses_slash_access_control_instead_of_bypassing_it():
    event = _event("STOP", chat_type="group")
    coerce_plaintext_gateway_command(event)

    assert event.text == "/stop"
    assert event.get_command() == "stop"


def test_stop_normalization_preserves_non_text_media_events():
    event = _event("STOP", message_type=MessageType.PHOTO)
    coerce_plaintext_gateway_command(event)
    assert event.text == "STOP"


class _Adapter(BasePlatformAdapter):
    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        raise AssertionError("active STOP dispatch must not use ordinary send")

    async def get_chat_info(self, chat_id):
        return {"name": "test", "type": "dm"}


def test_exact_stop_reaches_active_session_control_path(monkeypatch):
    import asyncio
    import contextlib

    async def scenario():
        adapter = _Adapter(PlatformConfig(enabled=True), Platform.TELEGRAM)

        async def handler(event):
            raise AssertionError("active STOP must not enter the ordinary handler")

        adapter.set_message_handler(handler)
        event = _event("STOP")
        session_key = build_session_key(event.source)
        owner = asyncio.create_task(asyncio.sleep(60))
        adapter._active_sessions[session_key] = asyncio.Event()
        adapter._session_tasks[session_key] = owner

        called = []

        async def dispatch(evt, key, command):
            called.append((evt.text, key, command))

        monkeypatch.setattr(adapter, "_dispatch_active_session_command", dispatch)
        try:
            await adapter.handle_message(event)
        finally:
            owner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await owner

        assert called == [("/stop", session_key, "stop")]

    asyncio.run(scenario())
