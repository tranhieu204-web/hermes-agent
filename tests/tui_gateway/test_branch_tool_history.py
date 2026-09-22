"""A branch carries the parent's tool calls, tool results and image attachments (real SessionDB, real gateway).

Before: desktop branches copied only user/assistant rows with visible text — ``vision_analyze`` calls and results
vanished, ``@image:`` references were lost on the seeded path, and the child model then disowned its earlier
image descriptions as fabricated."""

import json
import threading
import types
from unittest.mock import MagicMock, patch

import pytest

import agent.branch_transcript
from agent.anthropic_message_convert import convert_messages_to_anthropic
from agent.context_compressor import SUMMARY_PREFIX, ContextCompressor
from agent.transports import get_transport
from agent.turn_context import substitute_api_content
from hermes_state import SessionDB
from tui_gateway import server

PARENT = "parent-key"
IMG1 = "C:/img/cat.png"
IMG2 = "C:/img/dog.png"


def _call(call_id: str, path: str) -> dict:
    return {"id": call_id, "type": "function",
            "function": {"name": "vision_analyze", "arguments": json.dumps({"image_url": path})}}


def _turn(n: int, path: str, call_id: str) -> list:
    return [
        {"role": "user", "content": f"what is in picture {n}?\n@image:{path}",
         "api_content": f"[The user attached an image: {path}]\n[Examine it with the vision_analyze tool using "
                        f"image_url: {path}]\n\nwhat is in picture {n}?", "timestamp": 1000.0 + n * 10},
        {"role": "assistant", "content": f"Let me look at picture {n}.", "tool_calls": [_call(call_id, path)],
         "reasoning_content": f"need vision {n}", "finish_reason": "tool_calls", "timestamp": 1001.0 + n * 10},
        {"role": "tool", "tool_call_id": call_id, "tool_name": "vision_analyze",
         "content": json.dumps({"analysis": f"picture {n} shows an animal #{n}"}), "timestamp": 1002.0 + n * 10},
        {"role": "assistant", "content": f"Picture {n} shows an animal #{n}.", "finish_reason": "stop",
         "timestamp": 1003.0 + n * 10},
    ]


PARENT_ROWS = _turn(1, IMG1, "call_one") + _turn(2, IMG2, "call_two")


@pytest.fixture
def db(tmp_path, monkeypatch):
    database = SessionDB(db_path=tmp_path / "state.db")
    monkeypatch.setattr(server, "_get_db", lambda: database)
    monkeypatch.setattr(server, "_resolve_model", lambda: "claude-opus-5")
    yield database
    database.close()


def _parent(db, rows=PARENT_ROWS):
    db.create_session(PARENT, source="desktop")
    db.append_messages_batch(PARENT, [dict(row) for row in rows])


def _child_rows(db, key):
    return [{k: v for k, v in m.items() if not k.startswith("_")} for m in db.get_messages_as_conversation(key)]


def _shape(rows):
    return [(m["role"], m.get("tool_call_id") or ",".join(c["id"] for c in m.get("tool_calls") or []))
            for m in rows]


def _assert_carries_both_turns(rows):
    assert _shape(rows) == [("user", ""), ("assistant", "call_one"), ("tool", "call_one"), ("assistant", ""),
                            ("user", ""), ("assistant", "call_two"), ("tool", "call_two"), ("assistant", "")]
    assert rows[0]["content"].endswith(f"@image:{IMG1}") and rows[4]["content"].endswith(f"@image:{IMG2}")
    assert IMG1 in rows[0]["api_content"]
    assert rows[2]["tool_name"] == "vision_analyze" and "animal #1" in rows[2]["content"]
    assert rows[1]["content"] == "Let me look at picture 1." and rows[3]["content"] == "Picture 1 shows an animal #1."
    assert rows[1]["reasoning_content"] == "need vision 1"


def _wire(history):
    wire = []
    for message in history:
        copy = {k: v for k, v in message.items() if not k.startswith("_") and k not in ("display_kind", "display_metadata")}
        substitute_api_content(copy)
        wire.append(copy)
    return wire


def _assert_provider_pairs(history, call_ids):
    """Every carried call answered by its result in Anthropic, OpenAI chat-completions and Codex Responses shapes."""
    _assert_anthropic_pairs(history, call_ids)
    chat = get_transport("chat_completions").convert_messages(_wire(history), model="gpt-5.5")
    for call_id in call_ids:
        use = next(i for i, m in enumerate(chat) if m["role"] == "assistant"
                   and any(c["id"] == call_id for c in m.get("tool_calls") or ()))
        results = [m for m in chat[use + 1:] if m["role"] == "tool"][:len(chat[use]["tool_calls"])]
        assert chat[use + 1]["role"] == "tool" and call_id in [m["tool_call_id"] for m in results]
    assert not [m for m in chat if m["role"] == "tool"
                and not any(c["id"] == m["tool_call_id"] for a in chat for c in a.get("tool_calls") or ())]
    items = get_transport("codex_responses").convert_messages(_wire(history), model="gpt-5.5-codex")
    calls = {i["call_id"]: n for n, i in enumerate(items) if i.get("type") == "function_call"}
    outputs = {i["call_id"]: n for n, i in enumerate(items) if i.get("type") == "function_call_output"}
    assert set(calls) == set(outputs) and len(calls) == len(call_ids)
    assert all(calls[call_id] < outputs[call_id] for call_id in calls)


def _assert_anthropic_pairs(history, call_ids):
    wire = _wire(history)
    _, converted = convert_messages_to_anthropic(wire, model="claude-opus-5")
    for call_id in call_ids:
        use = next(i for i, m in enumerate(converted) if m["role"] == "assistant" and isinstance(m["content"], list)
                   and any(b.get("type") == "tool_use" and b.get("id") == call_id for b in m["content"]))
        result = converted[use + 1]
        assert result["role"] == "user" and any(
            b.get("type") == "tool_result" and b.get("tool_use_id") == call_id for b in result["content"])
    return converted


def _live_parent_session(db):
    model_history, _ = db.get_resume_conversations(PARENT)
    return {"agent": types.SimpleNamespace(), "session_key": PARENT, "history": model_history,
            "history_lock": threading.Lock(), "history_version": 0, "running": False, "attached_images": [],
            "image_counter": 0, "cols": 80, "slash_worker": None, "show_reasoning": False,
            "tool_progress_mode": "all"}


@pytest.fixture
def branch_rpc(db, monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(server, "_new_session_key", lambda: "branch-key")
    monkeypatch.setattr(server, "_session_cwd", lambda session: str(tmp_path))
    monkeypatch.setattr(server, "_make_agent", lambda *a, **k: types.SimpleNamespace())
    monkeypatch.setattr(server, "_init_session", lambda sid, key, agent, history, **k: captured.update(history=history))
    server._sessions["sid"] = _live_parent_session(db)

    def call(**params):
        response = server.handle_request({"id": "1", "method": "session.branch", "params": {"session_id": "sid", **params}})
        assert response.get("result"), response.get("error")
        return response["result"], captured["history"]
    yield call
    server._sessions.pop("sid", None)


def test_session_branch_carries_tool_turns_and_image_refs(db, branch_rpc):
    _parent(db)
    result, child_history = branch_rpc()

    _assert_carries_both_turns(_child_rows(db, "branch-key"))
    _assert_carries_both_turns(child_history)
    _assert_provider_pairs(child_history, ["call_one", "call_two"])
    tools =[m for m in result["messages"] if m["role"] == "tool"]
    assert [t["name"] for t in tools] == ["vision_analyze", "vision_analyze"]
    assert f"@image:{IMG1}" in result["messages"][0]["text"]


def test_session_branch_count_keeps_the_answered_tool_turn(db, branch_rpc):
    _parent(db)
    # Desktop count = bubbles: the user bubble + ONE assistant bubble (the renderer merges text, tool call and answer).
    _, child_history = branch_rpc(count=2)

    assert _shape(child_history) == [("user", ""), ("assistant", "call_one"), ("tool", "call_one"), ("assistant", "")]
    assert child_history[-1]["content"] == "Picture 1 shows an animal #1."
    assert _shape(_child_rows(db, "branch-key")) == _shape(child_history)
    _assert_anthropic_pairs(child_history, ["call_one"])


@pytest.mark.parametrize("count,expected", [
    (1, [("user", "")]),
    (3, [("user", ""), ("assistant", "call_one"), ("tool", "call_one"), ("assistant", ""), ("user", "")]),
])
def test_branch_prefix_counts_desktop_bubbles(count, expected):
    history = [dict(row) for row in PARENT_ROWS]
    history[1]["content"] = ""  # tool-only assistant row: absorbed into the answer's bubble, not a bubble itself
    assert _shape(server._branch_history_prefix(history, count)) == expected


def test_session_branch_drops_trailing_unanswered_tool_call(db, branch_rpc):
    """An in-flight/interrupted final tool turn (call without result) is removed, never copied as an orphan."""
    rows = PARENT_ROWS + [
        {"role": "user", "content": "and picture 3?", "timestamp": 2000.0},
        {"role": "assistant", "content": "", "tool_calls": [_call("call_three", "C:/img/3.png")],
         "reasoning_details": [{"type": "thinking", "thinking": "x", "signature": "sig"}], "timestamp": 2001.0},
    ]
    _parent(db, rows)
    _, child_history = branch_rpc()

    assert _shape(child_history)[-1] == ("user", "")
    assert not any("call_three" in json.dumps(m) for m in child_history + _child_rows(db, "branch-key"))
    _assert_anthropic_pairs(child_history, ["call_one", "call_two"])


def _compact_parent(db):
    """What a first compaction (``protect_first_n`` 3) writes: copies of the head (turn 1's image, call and result),
    the summary of the middle (turn 1's answer; assistant-role after the tool head, before a user tail), the tail."""
    head = [dict(row) for row in _turn(1, IMG1, "call_one")[:3]]
    summary = {"role": "assistant", "content": SUMMARY_PREFIX + "\nEarlier: picture 1 described.",
               "_compressed_summary": True, "timestamp": 1050.0}
    tail = [dict(row) for row in _turn(2, IMG2, "call_two")]
    db.archive_and_compact(PARENT, [*head, summary, *tail], tail_count=len(tail))


def _model_view(rows):
    return [(m["role"], m.get("content"), m.get("tool_call_id"), json.dumps(m.get("tool_calls")), m.get("tool_name"),
             m.get("api_content"), bool(m.get("_compressed_summary"))) for m in rows]


def _assert_child_holds_only(db, child_key, agent_history, parent_model):
    """A compacted parent's branch: stored rows == child model == agent history == the parent's model projection at
    the cut, every row live (no archived row copied), accepted as-is by the pre-call repair."""
    from agent.agent_runtime_helpers import repair_message_sequence
    child_model, child_display = db.get_resume_conversations(child_key)
    assert _model_view(agent_history) == _model_view(child_model) == _model_view(parent_model)
    assert _model_view(child_display) == _model_view([{k: v for k, v in m.items() if k != "_compressed_summary"}
                                                      for m in parent_model])
    stored = db.get_messages(child_key, include_inactive=True)
    assert len(stored) == len(parent_model) and all(m["active"] and not m["compacted"] for m in stored)
    assert db.get_resume_message_count(child_key, tip_only=True) == len(parent_model)
    assert db.get_session(child_key)["message_count"] == len(parent_model)
    repaired = [dict(m) for m in agent_history]
    assert repair_message_sequence(None, repaired) == 0 and _model_view(repaired) == _model_view(agent_history)


def _assert_compaction_projection(db, child_key, agent_history):
    parent_model = db.get_resume_conversations(PARENT)[0]
    _assert_child_holds_only(db, child_key, agent_history, parent_model)
    assert agent_history[0]["content"].endswith(f"@image:{IMG1}") and IMG1 in agent_history[0]["api_content"]
    _assert_provider_pairs(agent_history, ["call_one", "call_two"])


def test_session_branch_of_compacted_parent_copies_the_parent_model_projection(db, branch_rpc):
    _parent(db)
    _compact_parent(db)
    server._sessions["sid"]["history"] = db.get_resume_conversations(PARENT)[0]

    result, child_history = branch_rpc()

    _assert_compaction_projection(db, "branch-key", child_history)
    assert [m["name"] for m in result["messages"] if m["role"] == "tool"] == ["vision_analyze", "vision_analyze"]
    assert result["messages"][0]["text"].endswith(f"@image:{IMG1}")
    assert "Picture 1 shows" not in json.dumps(result["messages"])  # turn 1's archived answer is not copied


@pytest.mark.parametrize("count,kept", [(None, 2), (5, 1)])  # whole chat; the live question (branch message 5)
def test_session_branch_of_compacted_parent_keeps_the_unflushed_live_tail(db, branch_rpc, count, kept):
    _parent(db)
    _compact_parent(db)
    live = [{"role": "user", "content": "and a third?", "timestamp": 1100.0},
            {"role": "assistant", "content": "No third picture yet.", "timestamp": 1101.0}]
    parent_model = db.get_resume_conversations(PARENT)[0]
    server._sessions["sid"]["history"] = parent_model + live

    _, child_history = branch_rpc(**({} if count is None else {"count": count}))

    assert _contents(child_history) == _contents(parent_model + live[:kept])
    _assert_child_holds_only(db, "branch-key", child_history, parent_model + live[:kept])


def test_session_branch_count_inside_archived_turns_copies_the_head_before_it(db, branch_rpc):
    """Branch message 2 (turn 1's answer) was summarized away; the parent's model rows displayed up to it are the
    head copies: the image turn with its call and result."""
    _parent(db)
    _compact_parent(db)
    server._sessions["sid"]["history"] = db.get_resume_conversations(PARENT)[0]

    _, child_history = branch_rpc(count=2)

    assert _shape(child_history) == [("user", ""), ("assistant", "call_one"), ("tool", "call_one")]
    _assert_child_holds_only(db, "branch-key", child_history, db.get_resume_conversations(PARENT)[0][:3])


# Caption-less image turn: the renderer lifts the @image: line into attachments, so the bubble has no text and
# toBranchMessages does not number it — the answer is branch message 1.
IMAGE_ONLY_ROWS = [
    {"role": "user", "content": f"@image:{IMG1}", "timestamp": 3000.0,
     "api_content": f"[The user attached an image: cat.png]\n[Examine it with the vision_analyze tool using image_url: {IMG1}]"},
    {"role": "assistant", "content": "", "tool_calls": [_call("call_img", IMG1)], "timestamp": 3001.0},
    {"role": "tool", "tool_call_id": "call_img", "tool_name": "vision_analyze",
     "content": json.dumps({"analysis": "a ginger cat"}), "timestamp": 3002.0},
    {"role": "assistant", "content": "It's a ginger cat.", "timestamp": 3003.0},
    {"role": "user", "content": "thanks", "timestamp": 3004.0},
    {"role": "assistant", "content": "You're welcome.", "timestamp": 3005.0},
]


def test_session_branch_count_after_caption_less_image_keeps_tool_turn_and_answer(db, branch_rpc):
    _parent(db, IMAGE_ONLY_ROWS)
    _, child_history = branch_rpc(count=1)

    assert _shape(child_history) == [("user", ""), ("assistant", "call_img"), ("tool", "call_img"), ("assistant", "")]
    assert child_history[0]["content"] == f"@image:{IMG1}" and child_history[-1]["content"] == "It's a ginger cat."
    assert _shape(_child_rows(db, "branch-key")) == _shape(child_history)
    _assert_provider_pairs(child_history, ["call_img"])


def test_session_branch_count_skips_model_switch_timeline_row(db, branch_rpc):
    rows = [
        {"role": "user", "content": "hi", "timestamp": 1.0},
        {"role": "assistant", "content": "hello", "timestamp": 2.0},
        {"role": "user", "content": "[System: The active model for this chat has changed to gpt-5.5. From this point "
                                    "forward, use this runtime metadata.]", "display_kind": "model_switch", "timestamp": 3.0},
        {"role": "user", "content": "q2", "timestamp": 4.0},
        {"role": "assistant", "content": "a2", "timestamp": 5.0},
        {"role": "user", "content": "q3", "timestamp": 6.0},
        {"role": "assistant", "content": "a3", "timestamp": 7.0},
    ]
    _parent(db, rows)
    _, child_history = branch_rpc(count=4)  # hi, hello, q2, a2 — the model-switch row renders as a system bubble

    assert [m["content"] for m in child_history][-2:] == ["q2", "a2"] and len(child_history) == 5


def test_branch_prefix_reasoning_only_assistant_row_opens_a_bubble():
    history = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "A"},
        {"role": "assistant", "content": "", "reasoning_content": "thinking about tools"},
        {"role": "assistant", "content": "", "tool_calls": [_call("call_r", IMG1)]},
        {"role": "tool", "tool_call_id": "call_r", "tool_name": "vision_analyze", "content": "{}"},
        {"role": "assistant", "content": "B"},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "C"},
    ]
    # Renderer bubbles: q1 | A | (reasoning + tool call + B) | q2 | C — so branch message 3 is "B".
    prefix = server._branch_history_prefix([dict(m) for m in history], 3)
    assert [m["content"] for m in prefix] == ["q1", "A", "", "", "{}", "B"]


def test_branch_bubbles_follow_renderer_merge_rules():
    history = [dict(row) for row in IMAGE_ONLY_ROWS]
    bubbles = server._branch_bubbles(history)
    assert [(b["role"], b["text"], b["rows"], b["counts"]) for b in bubbles] == [
        ("user", "", [0], False), ("assistant", "It'sagingercat.", [1, 3], True),
        ("user", "thanks", [4], True), ("assistant", "You'rewelcome.", [5], True)]


@pytest.fixture
def create_rpc(db, monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_profile_home", lambda *a: None)
    monkeypatch.setattr(server, "_enable_gateway_prompts", lambda: None)
    monkeypatch.setattr(server, "_schedule_agent_build", lambda *a: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)
    monkeypatch.setattr(server, "_register_session_cwd", lambda *a: None)
    monkeypatch.setattr(server, "_project_info_for_cwd", lambda *a: None)
    created = []

    def call(messages):
        response = server._methods["session.create"]("create", {
            "source": "desktop", "cwd": str(tmp_path), "parent_session_id": PARENT, "messages": messages})
        assert "error" not in response, response
        created.append(response["result"]["session_id"])
        return response["result"], server._sessions[created[-1]]
    yield call
    for sid in created:
        server._sessions.pop(sid, None)


# What the desktop renderer seeds (toBranchMessages(toChatMessages(...))): image lines lifted out, tool rows gone,
# the tool-linked assistant rows merged into one bubble.
RENDERER_SEED = [
    {"role": "user", "content": "what is in picture 1?"},
    {"role": "assistant", "content": "Let me look at picture 1.Picture 1 shows an animal #1."},
    {"role": "user", "content": "what is in picture 2?"},
    {"role": "assistant", "content": "Let me look at picture 2.Picture 2 shows an animal #2."},
]


def test_seeded_create_branch_carries_parent_tool_turns_and_image_refs(db, create_rpc):
    _parent(db)
    result, record = create_rpc(RENDERER_SEED)
    child = result["stored_session_id"]

    _assert_carries_both_turns(_child_rows(db, child))
    _assert_carries_both_turns(record["history"])
    _assert_provider_pairs(record["history"], ["call_one", "call_two"])
    assert [m["name"] for m in result["messages"] if m["role"] == "tool"] == ["vision_analyze", "vision_analyze"]


def test_seeded_create_branch_from_a_mid_bubble_copies_that_prefix(db, create_rpc):
    _parent(db)
    result, record = create_rpc(RENDERER_SEED[:2])

    assert _shape(record["history"]) == [("user", ""), ("assistant", "call_one"), ("tool", "call_one"), ("assistant", "")]
    assert _shape(_child_rows(db, result["stored_session_id"])) == _shape(record["history"])


def test_seeded_create_branch_persists_seed_when_matching_raises(db, create_rpc, monkeypatch):
    """A matcher failure must never skip the immediate child row (#93959 spinner): the seed is kept and stored."""
    _parent(db)

    def boom(*_a, **_k):
        raise RuntimeError("matcher exploded")
    monkeypatch.setattr(agent.branch_transcript, "branch_rows", boom)
    result, record = create_rpc(RENDERER_SEED)

    stored = _child_rows(db, result["stored_session_id"])
    assert [(m["role"], m["content"]) for m in stored] == [(m["role"], m["content"]) for m in RENDERER_SEED]
    assert record.get("_branch_seed_persisted") is True


def test_seeded_create_branch_after_caption_less_image_carries_image_turn(db, create_rpc):
    _parent(db, IMAGE_ONLY_ROWS)
    seed = [{"role": "assistant", "content": "It's a ginger cat."}, {"role": "user", "content": "thanks"},
            {"role": "assistant", "content": "You're welcome."}]
    result, record = create_rpc(seed)

    assert _shape(record["history"]) == _shape(IMAGE_ONLY_ROWS)
    assert _shape(_child_rows(db, result["stored_session_id"])) == _shape(IMAGE_ONLY_ROWS)
    _assert_provider_pairs(record["history"], ["call_img"])


def test_seeded_create_branch_of_compacted_parent_copies_the_parent_model_projection(db, create_rpc):
    _parent(db)
    _compact_parent(db)
    result, record = create_rpc(RENDERER_SEED)

    _assert_compaction_projection(db, result["stored_session_id"], record["history"])


def test_seeded_create_branch_keeps_seed_when_it_does_not_match_parent(db, create_rpc):
    _parent(db)
    seed = [{"role": "user", "content": "something else entirely"}, {"role": "assistant", "content": "ok"}]
    result, record = create_rpc(seed)

    assert [(m["role"], m["content"]) for m in _child_rows(db, result["stored_session_id"])] == [
        ("user", "something else entirely"), ("assistant", "ok")]
    assert [(m["role"], m["content"]) for m in record["history"]] == [(m["role"], m["content"]) for m in seed]


# The compressor pulls the tail cut back onto an assistant tool-call row, so a kept tail can start with an assistant
# row right after the summary carrier: archived [look, "Let me look."+call_a, result], carrier, tail [call_b, ...].
COMPACT_ROWS = [
    {"role": "user", "content": f"look at these\n@image:{IMG1}", "timestamp": 4000.0,
     "api_content": f"[The user attached an image: {IMG1}]\n\nlook at these"},
    {"role": "assistant", "content": "Let me look.", "tool_calls": [_call("call_a", IMG1)], "timestamp": 4001.0},
    {"role": "tool", "tool_call_id": "call_a", "tool_name": "vision_analyze", "content": "a cat", "timestamp": 4002.0},
    {"role": "assistant", "content": "", "tool_calls": [_call("call_b", IMG2)], "timestamp": 4003.0},
    {"role": "tool", "tool_call_id": "call_b", "tool_name": "vision_analyze", "content": "another cat",
     "timestamp": 4004.0},
    {"role": "assistant", "content": "Both are cats.", "timestamp": 4005.0},
    {"role": "user", "content": "thanks", "timestamp": 4006.0},
    {"role": "assistant", "content": "You're welcome.", "timestamp": 4007.0},
]
# toBranchMessages(toChatMessages(REST transcript)): the REST-hidden carrier closes "Let me look."'s bubble
# (hydration.ts: a non-assistant row with no parts flushes and resets activeAssistantIndex), so the tail's tool call
# and "Both are cats." open bubble 3.
COMPACT_SEED = [
    {"role": "user", "content": "look at these"}, {"role": "assistant", "content": "Let me look."},
    {"role": "assistant", "content": "Both are cats."}, {"role": "user", "content": "thanks"},
    {"role": "assistant", "content": "You're welcome."},
]


def _compact_live_tail(db, keep: int, text: str, timestamp: float):
    """Real in-place ``archive_and_compact`` keeping the parent's last ``keep`` live rows after a new carrier."""
    live = db.get_resume_conversations(PARENT)[0][-keep:]
    tail = [{k: v for k, v in m.items() if not k.startswith("_")} for m in live]
    summary = {"role": "user", "content": f"{SUMMARY_PREFIX}\n{text}", "_compressed_summary": True,
               "timestamp": timestamp}
    db.archive_and_compact(PARENT, [summary, *tail], tail_count=len(tail))


def _compacted_parent_with_assistant_led_tail(db):
    _parent(db, COMPACT_ROWS)
    _compact_live_tail(db, 5, "Earlier: looked at picture 1.", 4002.5)


def _contents(rows):
    return [(m["role"], str(m.get("content") or "")[:24]) for m in rows]


def test_rest_hidden_compaction_carrier_closes_the_open_bubble(db):
    _compacted_parent_with_assistant_led_tail(db)
    display = db.get_resume_conversations(PARENT)[1]
    counted = [(b["role"], b["text"]) for b in server._branch_bubbles(display) if b["counts"]]
    assert counted == [("user", "lookatthese"), ("assistant", "Letmelook."), ("assistant", "Botharecats."),
                       ("user", "thanks"), ("assistant", "You'rewelcome.")]


def test_seeded_create_branch_of_compaction_with_assistant_led_tail_carries_tool_turns(db, create_rpc):
    _compacted_parent_with_assistant_led_tail(db)
    result, record = create_rpc(COMPACT_SEED)
    child = result["stored_session_id"]

    parent_model = db.get_resume_conversations(PARENT)[0]
    assert parent_model[0].get("_compressed_summary")  # matched, not the seed: the parent's model projection
    _assert_child_holds_only(db, child, record["history"], parent_model)
    _assert_provider_pairs(record["history"], ["call_b"])
    # session.create reports what the child holds: the tail's tool turn, not the archived one.
    assert [m["name"] for m in result["messages"] if m["role"] == "tool"] == ["vision_analyze"]
    assert result["message_count"] == len(result["messages"]) and "look at these" not in json.dumps(result["messages"])


def test_session_branch_count_after_compaction_ends_on_the_answer(db, branch_rpc):
    _compacted_parent_with_assistant_led_tail(db)
    server._sessions["sid"]["history"] = db.get_resume_conversations(PARENT)[0]

    _, child_history = branch_rpc(count=3)  # "Both are cats." is desktop branch message 3

    assert child_history[-1]["content"] == "Both are cats." and child_history[0].get("_compressed_summary")
    assert _shape(child_history) == [("user", ""), ("assistant", "call_b"), ("tool", "call_b"), ("assistant", "")]
    _assert_child_holds_only(db, "branch-key", child_history, db.get_resume_conversations(PARENT)[0][:4])
    _assert_provider_pairs(child_history, ["call_b"])


def _twice_compacted_parent(db):
    _parent(db, COMPACT_ROWS)
    _compact_live_tail(db, 5, "first", 4002.5)
    db.append_messages_batch(PARENT, [{"role": "user", "content": "q3", "timestamp": 4008.0},
                                      {"role": "assistant", "content": "a3", "timestamp": 4009.0}])
    _compact_live_tail(db, 2, "second", 4007.5)
    server._sessions["sid"]["history"] = db.get_resume_conversations(PARENT)[0]


@pytest.mark.parametrize("count", [None, 7])  # whole chat; "a3", the last branch message
def test_session_branch_of_twice_compacted_parent_copies_its_current_model_projection(db, branch_rpc, count):
    _twice_compacted_parent(db)

    _, child_history = branch_rpc(**({} if count is None else {"count": count}))

    assert _contents(child_history) == [("user", f"{SUMMARY_PREFIX}\nsecond"[:24]), ("user", "q3"), ("assistant", "a3")]
    _assert_child_holds_only(db, "branch-key", child_history, db.get_resume_conversations(PARENT)[0])


@pytest.mark.parametrize("count", [2, 5])  # before the first summary; between the summaries
def test_session_branch_before_a_headless_summary_has_nothing_to_branch(db, branch_rpc, count):
    """Everything up to the chosen message was summarized away and the compaction kept no head: the parent's model
    projection holds nothing at that cut, so no child is created."""
    _twice_compacted_parent(db)

    response = server.handle_request({"id": "1", "method": "session.branch",
                                      "params": {"session_id": "sid", "count": count}})

    assert response["error"]["code"] == 4008 and db.get_session("branch-key") is None


def _raise_copy(*_a, **_k):
    raise RuntimeError("copy failed")


def test_session_branch_of_compacted_parent_copy_failure_leaves_no_half_built_child(db, branch_rpc, monkeypatch):
    _compacted_parent_with_assistant_led_tail(db)
    server._sessions["sid"]["history"] = db.get_resume_conversations(PARENT)[0]
    monkeypatch.setattr(SessionDB, "append_messages_batch", _raise_copy)

    response = server.handle_request({"id": "1", "method": "session.branch", "params": {"session_id": "sid"}})

    assert response["error"]["code"] == 5008
    assert db.get_session("branch-key") is None and db.get_messages("branch-key", include_inactive=True) == []


def test_seeded_create_of_compacted_parent_copy_failure_keeps_the_seed_for_the_lazy_fallback(db, create_rpc, monkeypatch):
    _compacted_parent_with_assistant_led_tail(db)
    monkeypatch.setattr(SessionDB, "append_messages_batch", _raise_copy)

    result, record = create_rpc(COMPACT_SEED)

    assert db.get_session(result["stored_session_id"]) is None and not record.get("_branch_seed_persisted")
    assert [(m["role"], m["content"]) for m in record["history"]] == [(m["role"], m["content"]) for m in COMPACT_SEED]


def test_untyped_system_marker_user_row_is_a_rest_user_bubble():
    """REST ships an untyped ``[System:`` user row as plain user text, so the desktop numbers it."""
    rows = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "[System: note from an older gateway]"}, {"role": "assistant", "content": "ok"}]
    assert [b["counts"] for b in server._branch_bubbles(rows)] == [True, True, True, True]
    assert [m["content"] for m in server._branch_history_prefix(rows, 3)][-1] == "[System: note from an older gateway]"


def test_branch_prefix_keeps_the_chosen_bubbles_trailing_tool_rows():
    rows = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "Checking.", "tool_calls": [_call("call_x", IMG1)]},
        {"role": "tool", "tool_call_id": "call_x", "tool_name": "vision_analyze", "content": "x"},
        {"role": "assistant", "content": "", "tool_calls": [_call("call_y", IMG2)]},
        {"role": "tool", "tool_call_id": "call_y", "tool_name": "vision_analyze", "content": "y"},
        {"role": "user", "content": "next"},
        {"role": "assistant", "content": "done"},
    ]
    assert _shape(server._branch_history_prefix([dict(m) for m in rows], 2)) == [
        ("user", ""), ("assistant", "call_x"), ("tool", "call_x"), ("assistant", "call_y"), ("tool", "call_y")]


# ── Real compaction output: ContextCompressor.compress() (stubbed summary LLM) committed as
# agent/conversation_compression.py commits it (archive_and_compact in place, publish_compression_child rotated) ──
# The first compaction keeps protect_first_n (3) head rows — the image turn's user row, call and result — so the
# parent's model projection is [head copies, summary, tail]. Real sizes: compress() prunes the head before copying
# it (a result over 200 chars becomes a one-line stub, call arguments over 500 chars are shortened), so the copy is
# not merged with its archived original in the display projection.
VISION_ANALYSIS = json.dumps({
    "success": True, "analysis": (
        "The image shows a ginger tabby cat lying on a blue knitted blanket beside a window. Sunlight falls across "
        "its fur; its eyes are half closed and its front paws are tucked under its chest. Behind it are a potted "
        "fern and a wooden bookshelf holding several paperback books.")})
LONG_WRITE_CALL = {"id": "call_head", "type": "function", "function": {
    "name": "write_file", "arguments": json.dumps({"path": "C:/notes/cat.md", "content": "# Cat notes\n" + "z" * 900})}}


def _head_rows(kind: str) -> list:
    call = _call("call_head", IMG1) if kind == "vision" else LONG_WRITE_CALL
    result = VISION_ANALYSIS if kind == "vision" else "Wrote 911 bytes to C:/notes/cat.md"
    return [
        {"role": "user", "content": f"what is this?\n@image:{IMG1}", "timestamp": 5000.0,
         "api_content": f"[The user attached an image: {IMG1}]\n[Examine it with the vision_analyze tool using "
                        f"image_url: {IMG1}]\n\nwhat is this?"},
        {"role": "assistant", "content": "Let me look.", "tool_calls": [call], "timestamp": 5001.0},
        {"role": "tool", "tool_call_id": "call_head", "tool_name": call["function"]["name"], "content": result,
         "timestamp": 5002.0},
        {"role": "assistant", "content": "It's a ginger cat.", "timestamp": 5003.0},
    ]


def _filler(first: int, turns: int, timestamp: float, size: int = 800) -> list:
    return [row for n in range(first, first + turns) for row in (
        {"role": "user", "content": f"q{n} " + "x" * size, "timestamp": timestamp + n * 2},
        {"role": "assistant", "content": f"a{n} " + "y" * size, "timestamp": timestamp + n * 2 + 1})]


def _real_compress(db, key: str) -> tuple:
    with patch("agent.context_compressor.get_model_context_length", return_value=8000):
        compressor = ContextCompressor(model="test-model", quiet_mode=True, config_context_length=8000)
    watermark = db.get_active_message_watermark(key)
    response = MagicMock()
    response.choices[0].message.content = "## Active Task\nlooked at a picture"
    with patch("agent.context_compressor.call_llm", return_value=response):
        compressed = compressor.compress(db.get_resume_conversations(key)[0], current_tokens=100_000, force=True)
    tail = {id(m) for m in compressed if isinstance(m, dict) and m.pop("_compaction_tail", None)}
    return compressed, watermark, sum(id(m) in tail for m in compressed)


def _real_compact(db, key: str = PARENT):
    """One in-place compaction (conversation_compression.py ``archive_and_compact``)."""
    compressed, watermark, tail_count = _real_compress(db, key)
    db.archive_and_compact(key, compressed, watermark=watermark, tail_count=tail_count)


def _counted_bubbles(db, key: str = PARENT):
    return [b for b in server._branch_bubbles(db.get_resume_conversations(key)[1]) if b["counts"]]


def _renderer_seed(db, key: str = PARENT):
    """toBranchMessages(toChatMessages(REST transcript)) of the parent: one message per counted bubble."""
    display = db.get_resume_conversations(key)[1]
    seed = []
    for bubble in _counted_bubbles(db, key):
        texts = [server._branch_display_text(display[i]) or "" for i in bubble["rows"]]
        seed.append({"role": bubble["role"], "content": "".join(t.split("\n@image:")[0] for t in texts)})
    return seed


def _bubble_index_of(db, row_id: int, key: str = PARENT) -> int:
    """The desktop branch message number (``count``) of the bubble holding the parent row ``row_id``."""
    display = db.get_resume_conversations(key)[1]
    return next(n for n, b in enumerate(_counted_bubbles(db, key), 1)
                if any(display[i]["_row_id"] == row_id for i in b["rows"]))


def _assert_leading_user_turn(history):
    """Provider conversion accepts the child history and opens on the user turn."""
    _, anthropic = convert_messages_to_anthropic(_wire(history), model="claude-opus-5")
    assert anthropic[0]["role"] == "user"
    chat = get_transport("chat_completions").convert_messages(_wire(history), model="gpt-5.5")
    assert next(m for m in chat if m["role"] != "system")["role"] == "user"
    items = get_transport("codex_responses").convert_messages(_wire(history), model="gpt-5.5-codex")
    assert items[0].get("role") == "user"


def _assert_pruned_head(db, kind: str, key: str = PARENT) -> list:
    """``key``'s first real compaction kept a PRUNED head; returns its model projection."""
    rows = _head_rows(kind)
    model, display = db.get_resume_conversations(key)
    assert _shape(model[:3]) == [("user", ""), ("assistant", "call_head"), ("tool", "call_head")]
    assert model[3].get("_compressed_summary") and model[0]["content"].endswith(f"@image:{IMG1}")
    if kind == "vision":  # the result copy is a stub; the full analysis is only in the archived original
        assert "ginger tabby" in rows[2]["content"] and "ginger tabby" not in model[2]["content"]
    else:  # the call copy's arguments were shortened
        assert model[1]["tool_calls"][0]["function"]["arguments"] != LONG_WRITE_CALL["function"]["arguments"]
    # Not merged with its original: the display shows the original and the copy.
    pruned_role = "tool" if kind == "vision" else "assistant"
    assert sum(m["role"] == pruned_role and "call_head" in json.dumps(m) for m in display) == 2
    return model


def _pruned_head_parent(db, kind: str) -> list:
    _parent(db, _head_rows(kind) + _filler(0, 12, 5100.0))
    _real_compact(db)
    return _assert_pruned_head(db, kind)


def _assert_projection_child(db, child_key, agent_history, parent_model):
    _assert_child_holds_only(db, child_key, agent_history, parent_model)
    _assert_leading_user_turn(agent_history)
    _assert_provider_pairs(agent_history, [c["id"] for m in parent_model for c in m.get("tool_calls") or ()])
    assert "ginger tabby" not in json.dumps(agent_history)  # no archived row came along


KINDS = pytest.mark.parametrize("kind", ["vision", "long_args"])


@KINDS
def test_real_pruned_head_whole_chat_branch_copies_the_parent_model_projection(db, branch_rpc, kind):
    parent_model = _pruned_head_parent(db, kind)
    server._sessions["sid"]["history"] = parent_model

    _, child_history = branch_rpc()

    _assert_projection_child(db, "branch-key", child_history, parent_model)
    assert child_history[0]["content"].endswith(f"@image:{IMG1}") and IMG1 in child_history[0]["api_content"]


@KINDS
def test_real_pruned_head_seeded_create_copies_the_parent_model_projection(db, create_rpc, kind):
    parent_model = _pruned_head_parent(db, kind)

    result, record = create_rpc(_renderer_seed(db))

    _assert_projection_child(db, result["stored_session_id"], record["history"], parent_model)
    assert result["message_count"] == len(result["messages"])
    assert "It's a ginger cat." not in json.dumps(result["messages"])  # the archived answer is not copied


@KINDS
@pytest.mark.parametrize("where", ["first_tail_answer", "inside_the_tail"])
def test_real_pruned_head_count_after_the_summary_copies_the_model_prefix(db, branch_rpc, kind, where):
    parent_model = _pruned_head_parent(db, kind)
    server._sessions["sid"]["history"] = parent_model
    answers = [i for i, m in enumerate(parent_model) if i > 3 and m["role"] == "assistant"]
    cut = answers[0] if where == "first_tail_answer" else answers[-2]

    _, child_history = branch_rpc(count=_bubble_index_of(db, parent_model[cut]["_row_id"]))

    _assert_projection_child(db, "branch-key", child_history, parent_model[:cut + 1])


@KINDS
def test_real_pruned_head_count_inside_the_archived_middle_keeps_the_head_pair(db, branch_rpc, kind):
    """The chosen message was summarized away; the model rows displayed up to it are the head copies — including the
    unmerged pruned copy displayed at its own id, so the call keeps its result as in the parent."""
    parent_model = _pruned_head_parent(db, kind)
    server._sessions["sid"]["history"] = parent_model
    archived = next(m for m in db.get_resume_conversations(PARENT)[1] if str(m.get("content")).startswith("a3 "))

    _, child_history = branch_rpc(count=_bubble_index_of(db, archived["_row_id"]))

    _assert_projection_child(db, "branch-key", child_history, parent_model[:3])


def _twice_compacted_real_parent(db) -> tuple:
    first = _pruned_head_parent(db, "vision")
    db.append_messages_batch(PARENT, _filler(12, 12, 5100.0, size=800))  # past the tail budget: summarizes tail 1
    _real_compact(db)
    parent_model, display = db.get_resume_conversations(PARENT)
    assert sum(SUMMARY_PREFIX in str(m.get("content")) for m in display) == 2
    assert parent_model[0].get("_compressed_summary")  # the second compaction kept no head (protection decayed)
    return first, parent_model


@pytest.mark.parametrize("cut_at", [None, "inside_the_tail"])
def test_real_twice_compacted_branch_copies_the_current_model_projection(db, branch_rpc, cut_at):
    _, parent_model = _twice_compacted_real_parent(db)
    server._sessions["sid"]["history"] = parent_model
    cut = [i for i, m in enumerate(parent_model) if m["role"] == "assistant"][2] if cut_at else len(parent_model) - 1

    _, child_history = branch_rpc(**({"count": _bubble_index_of(db, parent_model[cut]["_row_id"])} if cut_at else {}))

    _assert_projection_child(db, "branch-key", child_history, parent_model[:cut + 1])


def test_real_twice_compacted_cut_between_the_summaries_has_nothing_to_branch(db, branch_rpc):
    first, parent_model = _twice_compacted_real_parent(db)
    server._sessions["sid"]["history"] = parent_model
    live = {row["_row_id"] for row in parent_model}
    between = next(m for m in first[4:] if m["role"] == "assistant" and m["_row_id"] not in live)

    response = server.handle_request({"id": "1", "method": "session.branch", "params": {
        "session_id": "sid", "count": _bubble_index_of(db, between["_row_id"])}})

    assert response["error"]["code"] == 4008 and db.get_session("branch-key") is None


ROTATED = "rotated-key"


def _rotated_parent(db) -> list:
    """Legacy rotated compaction: the parent ends and a child session holds [head copies, summary, tail]."""
    _parent(db, _head_rows("vision") + _filler(0, 12, 5100.0))
    compressed, watermark, _ = _real_compress(db, PARENT)
    db.publish_compression_child(parent_session_id=PARENT, child_session_id=ROTATED, source="desktop",
                                 messages=compressed, require_compression_lease=False, watermark=watermark)
    assert db.get_session(ROTATED)["parent_session_id"] == PARENT
    model = _assert_pruned_head(db, "vision", key=ROTATED)
    positions = [m["_row_id"] for m in db.get_resume_conversations(ROTATED)[1]]
    # The tail copies display at their parent originals' positions, so the summary is displayed AFTER the tail.
    assert positions.index(model[3]["_row_id"]) > positions.index(model[4]["_row_id"])
    return model


@pytest.mark.parametrize("cut_at", [None, "inside_the_tail"])
def test_real_rotated_compaction_branch_copies_the_rotated_model_projection(db, branch_rpc, cut_at):
    parent_model = _rotated_parent(db)
    server._sessions["sid"].update(session_key=ROTATED, history=parent_model)
    cut = [i for i, m in enumerate(parent_model) if i > 3 and m["role"] == "assistant"][1] if cut_at else None

    _, child_history = branch_rpc(**(
        {"count": _bubble_index_of(db, parent_model[cut]["_row_id"], key=ROTATED)} if cut_at else {}))

    _assert_projection_child(db, "branch-key", child_history, parent_model if cut is None else parent_model[:cut + 1])


def test_real_rotated_compaction_seeded_create_copies_the_rotated_model_projection(db, create_rpc, tmp_path):
    parent_model = _rotated_parent(db)

    response = server._methods["session.create"]("create", {
        "source": "desktop", "cwd": str(tmp_path), "parent_session_id": ROTATED, "messages": _renderer_seed(db, ROTATED)})
    result = response["result"]
    try:
        _assert_projection_child(db, result["stored_session_id"], server._sessions[result["session_id"]]["history"],
                                 parent_model)
    finally:
        server._sessions.pop(result["session_id"], None)


def test_uncompacted_parent_has_no_compaction_history(db):
    """Guard: the store reports no compaction state for a parent that was never compacted (full-transcript copy)."""
    _parent(db)
    assert not db.has_compaction_history(PARENT)
    _compact_parent(db)
    assert db.has_compaction_history(PARENT)
