"""CLI /branch shares the branch transcript copy: tool turns paired, attachments and reasoning state carried."""

import json
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from agent.anthropic_message_convert import convert_messages_to_anthropic
from agent.turn_context import substitute_api_content
from hermes_state import SessionDB

IMG = "C:/img/cat.png"
IMAGE_PARTS = [{"type": "text", "text": "what is in this picture?"},
               {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}}]


def _call(call_id):
    return {"id": call_id, "type": "function",
            "function": {"name": "vision_analyze", "arguments": json.dumps({"image_url": IMG})}}


def _tool(call_id, text):
    return {"role": "tool", "name": "vision_analyze", "content": text, "tool_call_id": call_id}


@pytest.fixture
def cli(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    cli = MagicMock()
    cli._session_db = db
    cli.session_id = "20260917_120000_parent"
    cli.model, cli.max_turns, cli.reasoning_config = "claude-opus-5", 90, {"enabled": True}
    cli.session_start, cli._pending_title, cli._resumed, cli.agent = datetime.now(), None, False, None
    cli._agent_running = False  # MagicMock auto-attrs are truthy; /branch's busy-guard would else always fire
    cli.conversation_history = [
        {"role": "user", "content": IMAGE_PARTS},
        {"role": "assistant", "content": "", "tool_calls": [_call("call_one")], "reasoning_content": "look first"},
        _tool("call_one", "a grey cat"),
        {"role": "assistant", "content": "It is a grey cat."},
        {"role": "user", "content": "[System: personality changed]", "display_kind": "personality_switch"},
        {"role": "user", "content": f"compare with this\n@image:{IMG}",
         "api_content": f"[Examine it with the vision_analyze tool using image_url: {IMG}]\n\ncompare with this"},
        {"role": "assistant", "content": "", "tool_calls": [_call("call_two")]},
        _tool("call_two", "the same cat"),
        {"role": "assistant", "content": "Same cat."},
        {"role": "user", "content": "one more?"},
        {"role": "assistant", "content": "Checking.", "tool_calls": [_call("call_three")]},  # interrupted, no result
    ]
    db.create_session(session_id=cli.session_id, source="cli", model=cli.model)
    yield cli
    db.close()


def test_cli_branch_carries_paired_tool_turns_and_attachments(cli):
    from cli import HermesCLI

    HermesCLI._handle_branch_command(cli, "/branch")

    rows = cli._session_db.get_messages_as_conversation(cli.session_id)
    shape = [(m["role"], m.get("tool_call_id") or ",".join(c["id"] for c in m.get("tool_calls") or [])) for m in rows]
    assert shape == [("user", ""), ("assistant", "call_one"), ("tool", "call_one"), ("assistant", ""),
                     ("user", ""), ("user", ""), ("assistant", "call_two"), ("tool", "call_two"), ("assistant", ""),
                     ("user", ""), ("assistant", "")]
    assert rows[0]["content"] == IMAGE_PARTS
    assert rows[1]["reasoning_content"] == "look first"
    assert rows[2]["tool_name"] == "vision_analyze"
    assert rows[4]["display_kind"] == "personality_switch"
    assert rows[5]["content"].endswith(f"@image:{IMG}") and IMG in rows[5]["api_content"]
    # The interrupted final call is removed from its row (the visible text stays) in storage AND model history.
    assert rows[-1]["content"] == "Checking." and "tool_calls" not in rows[-1]
    assert not any("call_three" in json.dumps(m) for m in cli.conversation_history)

    wire = []
    for message in cli.conversation_history:
        copy = {k: v for k, v in message.items() if k not in ("display_kind", "display_metadata")}
        substitute_api_content(copy)
        wire.append(copy)
    _, converted = convert_messages_to_anthropic(wire, model="claude-opus-5")
    for call_id in ("call_one", "call_two"):
        use = next(i for i, m in enumerate(converted) if isinstance(m["content"], list)
                   and any(b.get("type") == "tool_use" and b.get("id") == call_id for b in m["content"]))
        assert any(b.get("type") == "tool_result" and b.get("tool_use_id") == call_id
                   for b in converted[use + 1]["content"])


def test_cli_branch_of_really_compacted_parent_copies_its_model_projection(cli):
    """The CLI's live history IS the model projection: a compacted parent's branch holds its head copies (the pruned
    vision result stub stays paired with its call), summary and tail, all live; no archived row is copied."""
    from unittest.mock import patch

    from agent.agent_runtime_helpers import repair_message_sequence
    from agent.context_compressor import ContextCompressor
    from cli import HermesCLI

    db, parent = cli._session_db, cli.session_id
    analysis = json.dumps({"success": True, "analysis": "A grey cat sitting on a red armchair by a lamp. " * 6})
    db.append_messages_batch(parent, [
        {"role": "user", "content": f"what is this?\n@image:{IMG}", "timestamp": 1.0},
        {"role": "assistant", "content": "Let me look.", "tool_calls": [_call("call_head")], "timestamp": 2.0},
        {**_tool("call_head", analysis), "tool_name": "vision_analyze", "timestamp": 3.0},
        {"role": "assistant", "content": "A grey cat.", "timestamp": 4.0},
        *({"role": role, "content": f"{role[0]}{n} " + "x" * 4000, "timestamp": 10.0 + n * 2 + (role == "assistant")}
          for n in range(12) for role in ("user", "assistant"))])
    with patch("agent.context_compressor.get_model_context_length", return_value=8000):
        compressor = ContextCompressor(model="test-model", quiet_mode=True, config_context_length=8000)
    watermark, response = db.get_active_message_watermark(parent), MagicMock()
    response.choices[0].message.content = "## Active Task\nlooked at a picture"
    with patch("agent.context_compressor.call_llm", return_value=response):
        compressed = compressor.compress(db.get_resume_conversations(parent)[0], current_tokens=100_000, force=True)
    tail = {id(m) for m in compressed if m.pop("_compaction_tail", None)}
    db.archive_and_compact(parent, compressed, watermark=watermark, tail_count=sum(id(m) in tail for m in compressed))
    parent_model = db.get_resume_conversations(parent)[0]
    assert parent_model[3].get("_compressed_summary") and "grey cat sitting" not in parent_model[2]["content"]
    cli.conversation_history = parent_model

    HermesCLI._handle_branch_command(cli, "/branch")

    def view(rows):
        return [(m["role"], m.get("content"), m.get("tool_call_id"), m.get("tool_calls"), m.get("tool_name"),
                 bool(m.get("_compressed_summary"))) for m in rows]
    child_model, child_display = db.get_resume_conversations(cli.session_id)
    assert cli.session_id != parent and view(child_model) == view(parent_model) == view(cli.conversation_history)
    assert [m["role"] for m in child_display] == [m["role"] for m in parent_model]
    stored = db.get_messages(cli.session_id, include_inactive=True)
    assert len(stored) == len(parent_model) and all(m["active"] and not m["compacted"] for m in stored)
    assert repair_message_sequence(None, [dict(m) for m in cli.conversation_history]) == 0
