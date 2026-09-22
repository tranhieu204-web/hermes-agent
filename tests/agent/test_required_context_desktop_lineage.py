"""Required-context lineage for desktop rows pre-created by ``_ensure_session_db_row``.

The desktop gateway INSERT-OR-IGNOREs the session row (model/provider/reasoning/service_tier) on
prompt.submit before the agent exists, so the agent's own lineage write never lands. A pristine row
(no messages, no persisted system prompt, no lineage) must pin a fresh snapshot into that row; every
other lineage-less row keeps failing closed. All tests use a real SessionDB.
"""

import contextlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent.required_context as required_context
from agent.required_context import (
    RequiredContextError,
    append_required_context,
    initialize_required_context_lineage,
    load_required_context_snapshot,
)
from hermes_state import SessionDB

FILES = [
    "sakaan-workflow.md",
    "GRANTS.md",
    "ROUTING.md",
    "procedures/hardened-review.md",
    "Sakaan-crew.md",
]
KEY = "agent:main:desktop:dm:lineage"
DESKTOP_KEYS = {
    "model": "claude-opus-5",
    "provider": "anthropic",
    "reasoning_config": {"effort": "high"},
    "service_tier": "priority",
}


@pytest.fixture(autouse=True)
def _canonical_trust_root(tmp_path, monkeypatch):
    root = tmp_path / ".sakaan"
    root.mkdir()
    monkeypatch.setattr(required_context, "_CANONICAL_SAKAAN_ROOT", root)
    monkeypatch.setattr(required_context, "_CANONICAL_POINTER", root / "current.txt")
    monkeypatch.setattr(required_context, "_CANONICAL_GENERATIONS_ROOT", root / "generations")


@pytest.fixture()
def cfg(tmp_path):
    generation = tmp_path / ".sakaan" / "generations" / "generation-one"
    for name in FILES:
        target = generation / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"one:{name}\r\n".encode("utf-8"))
    pointer = tmp_path / ".sakaan" / "current.txt"
    pointer.write_text(str(generation), encoding="utf-8")
    return {"required_context": {"enabled": True, "pointer": str(pointer), "files": FILES}}


@pytest.fixture()
def db(tmp_path):
    store = SessionDB(db_path=tmp_path / "state.db")
    yield store
    store.close()


def _desktop_row(db, monkeypatch):
    """Create the row exactly as desktop prompt.submit does, through the real gateway helper."""
    from tui_gateway import server

    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_resolve_model", lambda: "global/default")
    assert server._ensure_session_db_row({
        "session_key": KEY,
        "source": "desktop",
        "model_override": {"model": DESKTOP_KEYS["model"], "provider": DESKTOP_KEYS["provider"]},
        "create_reasoning_override": DESKTOP_KEYS["reasoning_config"],
        "create_service_tier_override": DESKTOP_KEYS["service_tier"],
    }) is True
    row = db.get_session(KEY)
    assert row is not None and not row.get("system_prompt") and row["message_count"] == 0
    assert "_required_context_lineage" not in json.loads(row["model_config"])


def _agent(db):
    return SimpleNamespace(session_id=KEY, _session_db=db, _session_init_model_config={"max_tokens": None})


def _row_config(db):
    return json.loads(db.get_session(KEY)["model_config"])


def _init(agent, cfg):
    touched = []
    initialize_required_context_lineage(agent, config=cfg, before_provider=lambda: touched.append(1))
    return touched


def test_a_desktop_precreated_row_pins_lineage_and_keeps_desktop_keys(db, cfg, monkeypatch):
    _desktop_row(db, monkeypatch)
    agent = _agent(db)

    assert _init(agent, cfg) == [1]

    expected = load_required_context_snapshot(cfg)
    assert agent._required_context_snapshot.prompt_sha256 == expected.prompt_sha256
    persisted = _row_config(db)
    for key, value in DESKTOP_KEYS.items():
        assert persisted[key] == value
    lineage = persisted["_required_context_lineage"]
    assert lineage["prompt_sha256"] == expected.prompt_sha256
    assert agent._session_init_model_config["_required_context_lineage"] == lineage
    assert agent._session_init_model_config["max_tokens"] is None


def test_b_second_init_after_first_turn_restores_pinned_lineage(db, cfg, monkeypatch, tmp_path):
    _desktop_row(db, monkeypatch)
    first = _agent(db)
    _init(first, cfg)
    pinned = _row_config(db)["_required_context_lineage"]

    # First turn: the agent builds and persists its prompt (system_prompt.py appends the block) and messages land;
    # the agent's own lazy create is an upsert that must not clobber the pinned row.
    prompt = append_required_context("You are Hermes.\nModel: claude-opus-5", first._required_context_snapshot)
    db.create_session(KEY, source="desktop", model="claude-opus-5", model_config={"max_tokens": None}, system_prompt=prompt)
    db.update_system_prompt(KEY, prompt)
    db.append_message(KEY, role="user", content="hi")
    db.append_message(KEY, role="assistant", content="hello")

    # The canonical pointer moves on; the existing lineage must keep its pinned bytes.
    second_generation = tmp_path / ".sakaan" / "generations" / "generation-two"
    for name in FILES:
        target = second_generation / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"two:{name}\n".encode("utf-8"))
    Path(cfg["required_context"]["pointer"]).write_text(str(second_generation), encoding="utf-8")

    second = _agent(db)
    assert _init(second, cfg) == [1]
    assert second._required_context_snapshot.prompt_block == first._required_context_snapshot.prompt_block
    assert _row_config(db)["_required_context_lineage"] == pinned
    # Restored agents carry the lineage (v1) so rows they create later — compression children — inherit it.
    assert second._session_init_model_config["_required_context_lineage"] == pinned


def test_second_init_never_overwrites_an_existing_pin(db, cfg, monkeypatch):
    _desktop_row(db, monkeypatch)
    _init(_agent(db), cfg)
    pinned = _row_config(db)["_required_context_lineage"]

    assert db.pin_pristine_session_model_config_key(KEY, "_required_context_lineage", {"version": 1}) is False
    assert _row_config(db)["_required_context_lineage"] == pinned


def test_c_existing_row_with_messages_and_no_lineage_still_fails_closed(db, cfg, monkeypatch):
    _desktop_row(db, monkeypatch)
    db.append_message(KEY, role="user", content="legacy turn")
    before = db.get_session(KEY)["model_config"]

    agent, touched = _agent(db), []
    with pytest.raises(RequiredContextError, match="existing session has no pinned snapshot"):
        initialize_required_context_lineage(agent, config=cfg, before_provider=lambda: touched.append(1))
    assert touched == []
    assert db.get_session(KEY)["model_config"] == before
    assert db.pin_pristine_session_model_config_key(KEY, "_required_context_lineage", {"version": 1}) is False


def test_c_stale_zero_counter_with_message_rows_still_fails_closed(db, cfg, monkeypatch):
    """The messages table is the authority, not the cached ``message_count`` counter."""
    _desktop_row(db, monkeypatch)
    db.append_message(KEY, role="user", content="legacy turn")
    db._execute_write(lambda conn: conn.execute("UPDATE sessions SET message_count = 0 WHERE id = ?", (KEY,)))
    assert db.get_session(KEY)["message_count"] == 0

    with pytest.raises(RequiredContextError, match="existing session has no pinned snapshot"):
        _init(_agent(db), cfg)
    assert "_required_context_lineage" not in _row_config(db)


def test_d_existing_row_with_persisted_prompt_and_no_lineage_still_fails_closed(db, cfg, monkeypatch):
    _desktop_row(db, monkeypatch)
    db.update_system_prompt(KEY, "legacy prompt without a required-context block")
    before = db.get_session(KEY)["model_config"]

    agent, touched = _agent(db), []
    with pytest.raises(RequiredContextError, match="existing session has no pinned snapshot"):
        initialize_required_context_lineage(agent, config=cfg, before_provider=lambda: touched.append(1))
    assert touched == []
    assert db.get_session(KEY)["model_config"] == before
    assert db.pin_pristine_session_model_config_key(KEY, "_required_context_lineage", {"version": 1}) is False


def test_e_enabled_snapshot_unavailable_still_fails_closed(db, cfg, monkeypatch):
    _desktop_row(db, monkeypatch)
    before = db.get_session(KEY)["model_config"]
    monkeypatch.setattr(required_context, "load_required_context_snapshot", lambda _cfg: None)

    touched = []
    with pytest.raises(RequiredContextError, match="enabled snapshot is unavailable"):
        initialize_required_context_lineage(_agent(db), config=cfg, before_provider=lambda: touched.append(1))
    assert touched == []
    assert db.get_session(KEY)["model_config"] == before


def test_e_missing_pointer_on_pristine_row_fails_closed_without_pinning(db, cfg, monkeypatch):
    _desktop_row(db, monkeypatch)
    Path(cfg["required_context"]["pointer"]).unlink()

    with pytest.raises(RequiredContextError, match="WORKFLOW_SOURCE_UNAVAILABLE: path is missing"):
        _init(_agent(db), cfg)
    assert "_required_context_lineage" not in _row_config(db)


def test_f_disabled_required_context_is_unchanged(db, cfg, monkeypatch):
    _desktop_row(db, monkeypatch)
    before = db.get_session(KEY)["model_config"]
    disabled = {"required_context": dict(cfg["required_context"], enabled=False)}

    agent = _agent(db)
    assert _init(agent, disabled) == [1]
    assert agent._required_context_snapshot is None
    assert db.get_session(KEY)["model_config"] == before
    assert agent._session_init_model_config == {"max_tokens": None}


# --- A pinned lineage whose persisted system prompt is absent restores; tampering still fails closed. ---


def _persist_turn(db, snapshot, model="claude-opus-5"):
    prompt = append_required_context(f"You are Hermes.\nModel: {model}", snapshot)
    db.update_system_prompt(KEY, prompt)
    db.append_message(KEY, role="user", content="hi")
    db.append_message(KEY, role="assistant", content="hello")
    return prompt


def _pinned_with_prompt(db, cfg, monkeypatch):
    _desktop_row(db, monkeypatch)
    first = _agent(db)
    _init(first, cfg)
    _persist_turn(db, first._required_context_snapshot)
    assert _init(_agent(db), cfg) == [1]
    return first._required_context_snapshot


def test_pinned_row_whose_first_turn_failed_before_prompt_persisted_restores_on_retry(db, cfg, monkeypatch):
    _desktop_row(db, monkeypatch)
    first = _agent(db)
    _init(first, cfg)
    pinned = _row_config(db)["_required_context_lineage"]
    assert not db.get_session(KEY).get("system_prompt")

    second = _agent(db)
    assert _init(second, cfg) == [1]
    assert second._required_context_snapshot.prompt_block == first._required_context_snapshot.prompt_block
    assert _row_config(db)["_required_context_lineage"] == pinned

    _persist_turn(db, second._required_context_snapshot)
    third = _agent(db)
    assert _init(third, cfg) == [1]
    assert third._required_context_snapshot.prompt_block == first._required_context_snapshot.prompt_block
    assert _row_config(db)["_required_context_lineage"] == pinned


@pytest.mark.parametrize("null_prompt", [
    lambda db: db.update_session_model(KEY, "claude-sonnet-5", provider="anthropic"),
    lambda db: db.update_session_runtime_lock(KEY, model="claude-sonnet-5", provider="anthropic", confirmed=True),
], ids=["update_session_model", "update_session_runtime_lock"])
def test_prompt_nulled_by_runtime_writes_then_rebuild_restores(db, cfg, monkeypatch, null_prompt):
    snapshot = _pinned_with_prompt(db, cfg, monkeypatch)
    pinned = _row_config(db)["_required_context_lineage"]

    null_prompt(db)
    assert db.get_session(KEY)["system_prompt"] is None
    rebuilt = _agent(db)
    assert _init(rebuilt, cfg) == [1]
    assert rebuilt._required_context_snapshot.prompt_block == snapshot.prompt_block
    assert _row_config(db)["_required_context_lineage"] == pinned

    _persist_turn(db, rebuilt._required_context_snapshot, model="claude-sonnet-5")
    assert _init(_agent(db), cfg) == [1]


def test_stale_full_prompt_digest_is_cleared_only_while_prompt_is_absent(db, cfg, tmp_path):
    """A migrated (v2) lineage pins a full-prompt digest; once the prompt is nulled the digest is dropped."""
    snapshot = load_required_context_snapshot(cfg)
    original = append_required_context("STATIC\nModel: claude-opus-5", snapshot)
    v2 = required_context.snapshot_to_metadata(snapshot, system_prompt=original)
    db.create_session(KEY, source="desktop", model="claude-opus-5",
                      model_config={"_required_context_lineage": v2, "keep": 1}, system_prompt=original)
    assert _init(_agent(db), cfg) == [1]

    db.update_session_model(KEY, "claude-sonnet-5")
    assert _init(_agent(db), cfg) == [1]
    lineage = _row_config(db)["_required_context_lineage"]
    assert "system_prompt_sha256" not in lineage and lineage["version"] == 1
    assert {k: v for k, v in v2.items() if k not in ("system_prompt_sha256", "version")} == {
        k: v for k, v in lineage.items() if k != "version"}
    assert _row_config(db)["keep"] == 1

    _persist_turn(db, snapshot, model="claude-sonnet-5")
    assert _init(_agent(db), cfg) == [1]


def test_stale_digest_is_not_cleared_when_a_prompt_lands_first(db, cfg):
    snapshot = load_required_context_snapshot(cfg)
    original = append_required_context("STATIC", snapshot)
    v2 = required_context.snapshot_to_metadata(snapshot, system_prompt=original)
    db.create_session(KEY, source="desktop", model_config={"_required_context_lineage": v2})
    db.update_system_prompt(KEY, append_required_context("OTHER", snapshot))

    cleared = dict(v2, version=1)
    cleared.pop("system_prompt_sha256")
    assert db.replace_session_model_config_key_if_prompt_absent(KEY, "_required_context_lineage", v2, cleared) is False
    with pytest.raises(RequiredContextError, match="full prompt integrity"):
        _init(_agent(db), cfg)
    assert _row_config(db)["_required_context_lineage"] == v2


@pytest.mark.parametrize("tamper", [
    lambda prompt, block: prompt.replace("one:GRANTS.md", "evil:GRANTS.md"),
    lambda prompt, block: f"{prompt}\n\n{block}",
    lambda prompt, block: prompt.replace(block, "no block"),
], ids=["altered-block", "duplicated-block", "missing-block"])
def test_pinned_session_with_tampered_nonempty_prompt_still_fails_closed(db, cfg, monkeypatch, tamper):
    snapshot = _pinned_with_prompt(db, cfg, monkeypatch)
    db.update_session_model(KEY, "claude-sonnet-5")
    prompt = append_required_context("You are Hermes.\nModel: claude-sonnet-5", snapshot)
    db.update_system_prompt(KEY, tamper(prompt, snapshot.prompt_block))

    touched = []
    with pytest.raises(RequiredContextError, match="persisted prompt integrity is invalid"):
        initialize_required_context_lineage(_agent(db), config=cfg, before_provider=lambda: touched.append(1))
    assert touched == []


@pytest.mark.parametrize("field", ["file-digest", "prompt_sha256", "generation"])
def test_tampered_snapshot_metadata_with_empty_prompt_still_fails_closed(db, cfg, monkeypatch, field):
    _desktop_row(db, monkeypatch)
    _init(_agent(db), cfg)
    config = _row_config(db)
    lineage = config["_required_context_lineage"]
    if field == "file-digest":
        lineage["files"][0]["sha256"] = "0" * 64
    elif field == "prompt_sha256":
        lineage["prompt_sha256"] = "0" * 64
    else:
        lineage["generation"] = lineage["generation"] + "-other"
    db.update_session_meta(KEY, json.dumps(config))
    before = db.get_session(KEY)["model_config"]
    assert not db.get_session(KEY).get("system_prompt")

    touched = []
    with pytest.raises(RequiredContextError, match="persisted snapshot integrity is invalid"):
        initialize_required_context_lineage(_agent(db), config=cfg, before_provider=lambda: touched.append(1))
    assert touched == []
    assert db.get_session(KEY)["model_config"] == before


# --- R4: lineage persists across pre-submit agents, compression children, branches and runtime saves. ---


def _generation_two(tmp_path, cfg):
    generation = tmp_path / ".sakaan" / "generations" / "generation-two"
    for name in FILES:
        target = generation / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"two:{name}\n".encode("utf-8"))
    Path(cfg["required_context"]["pointer"]).write_text(str(generation), encoding="utf-8")


def _row_writing_agent(db):
    """An agent whose row write is the real ``AIAgent._ensure_db_session``."""
    from run_agent import AIAgent

    agent = SimpleNamespace(
        session_id=KEY, _session_db=db, _session_init_model_config={"max_tokens": None}, platform="desktop",
        model="claude-opus-5", _cached_system_prompt=None, _parent_session_id=None, _session_db_created=False,
        _persist_disabled=False, _pending_cli_user_message=None,
    )
    agent._session_row_model_config = lambda: AIAgent._session_row_model_config(agent)
    agent._ensure_db_session = lambda: AIAgent._ensure_db_session(agent)
    return agent


class _Proxy:
    """A real SessionDB with selected methods intercepted to interleave a concurrent writer."""

    def __init__(self, db, **overrides):
        self._db, self._overrides = db, overrides

    def __getattr__(self, name):
        return self._overrides.get(name) or getattr(self._db, name)


def test_b1_agent_built_before_desktop_row_persists_lineage_on_its_row_write(db, cfg, monkeypatch):
    agent = _row_writing_agent(db)
    assert db.get_session(KEY) is None
    assert _init(agent, cfg) == [1]  # image.attach / model pick: the agent exists before prompt.submit's row

    _desktop_row(db, monkeypatch)
    agent._cached_system_prompt = append_required_context(
        "You are Hermes.\nModel: claude-opus-5", agent._required_context_snapshot)
    agent._ensure_db_session()
    assert agent._session_db_created is True
    db.append_message(KEY, role="user", content="look at this image")
    db.append_message(KEY, role="assistant", content="done")

    persisted = _row_config(db)
    assert persisted["_required_context_lineage"] == agent._session_init_model_config["_required_context_lineage"]
    for key, value in DESKTOP_KEYS.items():
        assert persisted[key] == value
    rebuilt = _agent(db)
    assert _init(rebuilt, cfg) == [1]
    assert rebuilt._required_context_snapshot.prompt_block == agent._required_context_snapshot.prompt_block


def test_b1_row_holding_a_different_lineage_raises_before_provider(db, cfg, monkeypatch, tmp_path):
    from agent import turn_context

    agent = _row_writing_agent(db)
    _init(agent, cfg)
    _generation_two(tmp_path, cfg)
    _desktop_row(db, monkeypatch)
    _init(_agent(db), cfg)  # another agent pins generation two onto the row first
    foreign = _row_config(db)["_required_context_lineage"]
    assert foreign["generation"] != agent._session_init_model_config["_required_context_lineage"]["generation"]

    with pytest.raises(RequiredContextError, match="different pinned lineage"):
        turn_context._ensure_session_row(agent, None)  # the turn-start row write the provider call waits on
    assert agent._session_db_created is False
    assert _row_config(db)["_required_context_lineage"] == foreign
    with pytest.raises(RequiredContextError, match="different pinned lineage"):
        agent._ensure_db_session()  # every retry keeps refusing


@pytest.mark.parametrize("origin", ["restored", "pinned"])
def test_s1_compression_child_of_restored_or_pinned_agent_restores(db, cfg, monkeypatch, origin):
    if origin == "restored":
        _pinned_with_prompt(db, cfg, monkeypatch)
    else:
        _desktop_row(db, monkeypatch)
    parent = _agent(db)
    _init(parent, cfg)

    db.publish_compression_child(
        parent_session_id=KEY, child_session_id="compressed-child", source="desktop", model="claude-opus-5",
        messages=[{"role": "user", "content": "[summary]"}], model_config=parent._session_init_model_config,
        system_prompt=append_required_context("You are Hermes.\nModel: claude-opus-5",
                                              parent._required_context_snapshot),
        require_compression_lease=False,
    )
    child = SimpleNamespace(session_id="compressed-child", _session_db=db, _session_init_model_config={})
    assert _init(child, cfg) == [1]
    assert child._required_context_snapshot.prompt_block == parent._required_context_snapshot.prompt_block


def _branch(db, monkeypatch, parent_key, new_key="branch-child"):
    from tui_gateway import server

    monkeypatch.setattr(server, "_resolve_model", lambda: "claude-opus-5")
    server._persist_branch(db, new_key, parent_key, "branch", [
        {"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"},
    ], source="desktop", cwd=None, profile_name=None)


def test_n4_desktop_branch_inherits_parent_lineage_and_restores(db, cfg, monkeypatch):
    snapshot = _pinned_with_prompt(db, cfg, monkeypatch)
    _branch(db, monkeypatch, KEY)

    lineage = json.loads(db.get_session("branch-child")["model_config"])["_required_context_lineage"]
    assert lineage["version"] == 1 and "system_prompt_sha256" not in lineage
    child = SimpleNamespace(session_id="branch-child", _session_db=db, _session_init_model_config={})
    assert _init(child, cfg) == [1]
    assert child._required_context_snapshot.prompt_block == snapshot.prompt_block


def test_n4_branch_of_lineage_less_parent_fails_closed_with_message(db, cfg, monkeypatch):
    from tui_gateway import server

    monkeypatch.setattr(required_context, "_config", lambda config: cfg if config is None else config)
    _desktop_row(db, monkeypatch)
    db.append_message(KEY, role="user", content="legacy turn")

    with pytest.raises(RequiredContextError, match="branch parent session has no pinned snapshot"):
        _branch(db, monkeypatch, KEY)
    assert db.get_session("branch-child") is None
    monkeypatch.setattr(server, "_session_db", lambda record: contextlib.nullcontext(db))
    with pytest.raises(RequiredContextError, match="branch parent session has no pinned snapshot"):
        server._seed_branch_row({"cwd": None}, "seeded-child", KEY, [{"role": "user", "content": "hi"}],
                                "desktop", None)
    assert db.get_session("seeded-child") is None


@pytest.mark.parametrize("race", [False, True], ids=["pin-before-read", "pin-between-read-and-write"])
def test_n3_live_runtime_persist_never_drops_lineage(db, cfg, monkeypatch, race):
    from tui_gateway import server

    _desktop_row(db, monkeypatch)
    if not race:
        _init(_agent(db), cfg)
        store = db
    else:
        pending = [lambda: _init(_agent(db), cfg)]

        def get_session(session_id):
            row = db.get_session(session_id)
            while pending:
                pending.pop()()  # the pin commits after the runtime persist read the row
            return row
        store = _Proxy(db, get_session=get_session)
    live = SimpleNamespace(model="claude-sonnet-5", provider="anthropic", base_url="", api_mode="",
                           reasoning_config=None, service_tier=None, _session_db=store)

    server._persist_live_session_runtime({"agent": live, "session_key": KEY})

    persisted = _row_config(db)
    assert persisted["model"] == "claude-sonnet-5"
    assert persisted["_required_context_lineage"]["prompt_sha256"] == load_required_context_snapshot(cfg).prompt_sha256
    assert _init(_agent(db), cfg) == [1]


def test_n5_pin_losing_a_race_restores_the_winners_snapshot(db, cfg, monkeypatch, tmp_path):
    _desktop_row(db, monkeypatch)
    winner = {}

    def racing_pin(session_id, key, value):
        _generation_two(tmp_path, cfg)
        winner["agent"] = _agent(db)
        _init(winner["agent"], cfg)
        return db.pin_pristine_session_model_config_key(session_id, key, value)

    loser = SimpleNamespace(session_id=KEY, _session_init_model_config={},
                            _session_db=_Proxy(db, pin_pristine_session_model_config_key=racing_pin))
    assert _init(loser, cfg) == [1]
    assert "two:GRANTS.md" in loser._required_context_snapshot.prompt_block
    assert loser._required_context_snapshot.prompt_block == winner["agent"]._required_context_snapshot.prompt_block
    assert _row_config(db)["_required_context_lineage"]["generation"].endswith("generation-two")


def _v2_row_with_empty_prompt(db, cfg):
    snapshot = load_required_context_snapshot(cfg)
    v2 = required_context.snapshot_to_metadata(snapshot, system_prompt=append_required_context("STATIC", snapshot))
    db.create_session(KEY, source="desktop", model_config={"_required_context_lineage": v2})
    return v2


def test_n5_digest_clear_lost_to_a_concurrent_clear_retries_and_restores(db, cfg):
    _v2_row_with_empty_prompt(db, cfg)
    calls = []

    def racing_clear(*args):
        calls.append(args)
        assert db.replace_session_model_config_key_if_prompt_absent(*args) is True  # the other agent wins
        return db.replace_session_model_config_key_if_prompt_absent(*args)

    agent = SimpleNamespace(session_id=KEY, _session_init_model_config={},
                            _session_db=_Proxy(db, replace_session_model_config_key_if_prompt_absent=racing_clear))
    assert _init(agent, cfg) == [1]
    assert len(calls) == 1  # the re-read saw v1 metadata and returned without clearing again
    assert _row_config(db)["_required_context_lineage"]["version"] == 1


def test_n5_digest_clear_that_keeps_losing_gives_up_fail_closed(db, cfg):
    v2 = _v2_row_with_empty_prompt(db, cfg)
    agent = SimpleNamespace(session_id=KEY, _session_init_model_config={},
                            _session_db=_Proxy(db, replace_session_model_config_key_if_prompt_absent=lambda *a: False))
    touched = []
    with pytest.raises(RequiredContextError, match="persisted lineage changed during restore"):
        initialize_required_context_lineage(agent, config=cfg, before_provider=lambda: touched.append(1))
    assert touched == []
    assert _row_config(db)["_required_context_lineage"] == v2
