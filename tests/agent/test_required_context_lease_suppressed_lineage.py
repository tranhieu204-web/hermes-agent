"""Lineage must survive the turn lease suppressing the agent's row create.

``admit_durable_turn_lease`` sets ``agent._session_db_created = True`` once it has proven the
durable row exists ("suppress the redundant create attempt"). ``AIAgent._ensure_db_session``
returns immediately on that flag, and ``persist_agent_lineage`` used to be reachable ONLY from
inside that create. So an agent that initialized while the row did not yet exist — it holds the
lineage only in ``_session_init_model_config`` — and whose row then appeared before the turn ran
(desktop ``prompt.submit`` / ``_ensure_session_db_row``) wrote its prompt block but never its
lineage. Observed live on session ``20260918_191006_5cd313``: required-context block in the
persisted prompt, no ``_required_context_lineage`` in ``model_config``, so the next turn's
``_restore_existing_lineage`` would hard-fail.

Everything here drives the real functions (``admit_durable_turn_lease``,
``turn_context._ensure_session_row`` → ``AIAgent._ensure_db_session``) against a real
``SessionDB``.

The suppressed branch runs on every turn, so its failure paths are pinned here too: the row the
flag promised may not exist (a fail-closed durability probe, a mid-life delete) and that must not
abort the turn, while a row whose ``model_config`` cannot be decoded must; and the confirmed merge
latches per ``(session_id, lineage)`` so a steady-state turn costs no write transaction.
"""

import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest

import agent.required_context as required_context
from agent import turn_context
from agent.required_context import (
    RequiredContextError,
    append_required_context,
    initialize_required_context_lineage,
)
from agent.turn_facade_lease import admit_durable_turn_lease
from hermes_state import SessionDB

FILES = [
    "sakaan-workflow.md",
    "GRANTS.md",
    "ROUTING.md",
    "procedures/hardened-review.md",
    "Sakaan-crew.md",
]
KEY = "agent:main:desktop:dm:lease-lineage"
LINEAGE = required_context.LINEAGE_METADATA_KEY


@pytest.fixture(autouse=True)
def _canonical_trust_root(tmp_path, monkeypatch):
    root = tmp_path / ".sakaan"
    root.mkdir()
    monkeypatch.setattr(required_context, "_CANONICAL_SAKAAN_ROOT", root)
    monkeypatch.setattr(required_context, "_CANONICAL_POINTER", root / "current.txt")
    monkeypatch.setattr(required_context, "_CANONICAL_GENERATIONS_ROOT", root / "generations")


def _generation(tmp_path, name, marker):
    generation = tmp_path / ".sakaan" / "generations" / name
    for path in FILES:
        target = generation / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"{marker}:{path}\n".encode("utf-8"))
    return generation


@pytest.fixture()
def cfg(tmp_path):
    generation = _generation(tmp_path, "generation-one", "one")
    pointer = tmp_path / ".sakaan" / "current.txt"
    pointer.write_text(str(generation), encoding="utf-8")
    return {"required_context": {"enabled": True, "pointer": str(pointer), "files": FILES}}


@pytest.fixture()
def db(tmp_path):
    store = SessionDB(db_path=tmp_path / "state.db")
    yield store
    store.close()


def _desktop_row(db, monkeypatch):
    """Create the row exactly as desktop ``prompt.submit`` does, through the real gateway helper."""
    from tui_gateway import server

    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_resolve_model", lambda: "global/default")
    assert server._ensure_session_db_row({
        "session_key": KEY,
        "source": "desktop",
        "model_override": {"model": "grok-4.6", "provider": "xai"},
    }) is True
    row = db.get_session(KEY)
    assert row is not None and not row.get("system_prompt") and row["message_count"] == 0
    assert LINEAGE not in json.loads(row["model_config"])


def _agent(db):
    """An agent whose row write is the real ``AIAgent._ensure_db_session``, able to take a lease."""
    from run_agent import AIAgent

    agent = SimpleNamespace(
        session_id=KEY, _session_db=db, _session_init_model_config={"max_tokens": None},
        platform="desktop", model="grok-4.6", _cached_system_prompt=None, _parent_session_id=None,
        _session_db_created=False, _persist_disabled=False, _pending_cli_user_message=None,
        _interrupt_requested=False, _interrupt_message=None, _execution_thread_id=None,
        _session_turn_lease_refresh_interval=60.0, _session_persist_lock=None, statuses=[],
    )
    agent._emit_status = agent.statuses.append
    agent._emit_warning = agent.statuses.append
    agent._touch_activity = lambda *a, **k: None
    agent._liveness_activity_lock = lambda: threading.Lock()
    agent._session_row_model_config = lambda: AIAgent._session_row_model_config(agent)
    agent._ensure_db_session = lambda: AIAgent._ensure_db_session(agent)
    return agent


def _admit(agent, monkeypatch):
    """The real turn-lease admission, exactly as ``turn_facade`` calls it."""
    monkeypatch.setattr("agent.turn_liveness.resolve_turn_liveness_settings", lambda cfg: (None, 1.0))
    admission = admit_durable_turn_lease(
        agent, session_id=KEY, relay_turn_id=f"{KEY}:t:abcd",
        task_context={"session_id": KEY, "task_id": "t", "platform": "desktop"},
        conversation_history=None,
    )
    assert admission.early_result is None and admission.lease is not None
    return admission.lease


def _turn_start_row_write(agent):
    """The real turn-start row write the first provider call waits on."""
    turn_context._ensure_session_row(agent, None)


def _row_config(db):
    return json.loads(db.get_session(KEY)["model_config"])


def _init_before_the_row_exists(db, cfg):
    """Agent initialized while the row is absent: lineage lives only in memory."""
    agent = _agent(db)
    assert db.get_session(KEY) is None
    initialize_required_context_lineage(agent, config=cfg)
    pinned = agent._session_init_model_config[LINEAGE]
    agent._cached_system_prompt = append_required_context(
        "You are Hermes.\nModel: grok-4.6", agent._required_context_snapshot)
    return agent, pinned


# --- The defect: BASE leaves the row without the lineage the agent is actually using. ---


def test_lease_suppressed_create_still_persists_the_pinned_lineage(db, cfg, monkeypatch):
    """FAILS ON BASE: the lease suppressed the create, so the row never got the lineage."""
    agent, pinned = _init_before_the_row_exists(db, cfg)
    _desktop_row(db, monkeypatch)  # the row appears between agent init and the turn

    lease = _admit(agent, monkeypatch)
    try:
        assert agent._session_db_created is True  # the suppression under test
        _turn_start_row_write(agent)
    finally:
        lease.release()

    persisted = _row_config(db)
    assert persisted[LINEAGE] == pinned  # byte-identical to what the agent pinned
    assert json.dumps(persisted[LINEAGE], sort_keys=True) == json.dumps(pinned, sort_keys=True)
    assert persisted["model"] == "grok-4.6"  # the desktop row's own keys survive


def test_next_turn_restores_the_lineage_the_lease_turn_persisted(db, cfg, monkeypatch):
    """FAILS ON BASE: without the row write the next agent hits the no-pinned-snapshot guard.

    This is the live symptom: a row with messages and no metadata hard-fails its next turn.
    """
    agent, _pinned = _init_before_the_row_exists(db, cfg)
    _desktop_row(db, monkeypatch)
    lease = _admit(agent, monkeypatch)
    try:
        _turn_start_row_write(agent)
    finally:
        lease.release()
    db.append_message(KEY, role="user", content="hello")
    db.append_message(KEY, role="assistant", content="hi")

    successor = _agent(db)
    initialize_required_context_lineage(successor, config=cfg)
    assert (successor._required_context_snapshot.prompt_block
            == agent._required_context_snapshot.prompt_block)


# --- The guards the fix must not weaken. ---


def test_foreign_lineage_on_the_row_still_aborts_the_turn(db, cfg, monkeypatch, tmp_path):
    """A row already holding a DIFFERENT lineage must raise before any provider call."""
    agent, pinned = _init_before_the_row_exists(db, cfg)
    _desktop_row(db, monkeypatch)
    # Another agent pins generation two onto the row first.
    generation_two = _generation(tmp_path, "generation-two", "two")
    (tmp_path / ".sakaan" / "current.txt").write_text(str(generation_two), encoding="utf-8")
    initialize_required_context_lineage(_agent(db), config=cfg)
    foreign = _row_config(db)[LINEAGE]
    assert foreign["generation"] != pinned["generation"]

    lease = _admit(agent, monkeypatch)
    try:
        with pytest.raises(RequiredContextError, match="different pinned lineage"):
            _turn_start_row_write(agent)
        assert _row_config(db)[LINEAGE] == foreign  # never overwritten
        with pytest.raises(RequiredContextError, match="different pinned lineage"):
            _turn_start_row_write(agent)  # every retry keeps refusing
    finally:
        lease.release()


def test_same_lineage_writes_once_then_latches_until_the_session_id_changes(db, cfg, monkeypatch):
    """One confirmed merge per (session, lineage): later turns of the same session cost no write
    transaction, and a rotation/adoption/resume-id switch re-arms the check."""
    agent, pinned = _init_before_the_row_exists(db, cfg)
    _desktop_row(db, monkeypatch)
    lease = _admit(agent, monkeypatch)
    try:
        _turn_start_row_write(agent)
        after_first = db.get_session(KEY)["model_config"]
        writes = []
        original = type(db).set_session_model_config_key_if_absent
        monkeypatch.setattr(
            type(db), "set_session_model_config_key_if_absent",
            lambda self, sid, key, value: (writes.append(sid), original(self, sid, key, value))[1],
        )
        _turn_start_row_write(agent)
        _turn_start_row_write(agent)
        assert writes == []  # latched: no store call at all on the steady-state turns
        assert db.get_session(KEY)["model_config"] == after_first  # byte-identical row

        # The lease's post-wait resume-id switch / compression rotation repoints agent.session_id.
        rotated = f"{KEY}:rotated"
        db.create_session(session_id=rotated, source="desktop", model="grok-4.6")
        agent.session_id = rotated
        _turn_start_row_write(agent)
    finally:
        lease.release()

    assert writes == [rotated]  # the new session row is checked and pinned, exactly once
    assert json.loads(db.get_session(rotated)["model_config"])[LINEAGE] == pinned
    assert _row_config(db)[LINEAGE] == pinned


# --- The failure paths the suppressed branch newly makes reachable. ---


def test_absent_row_does_not_abort_the_turn_and_the_next_turn_recovers(db, cfg):
    """``_session_db_created`` True with the row ABSENT — what a failed durability probe or a
    mid-life row delete leaves behind. It must not be reported as a lineage conflict."""
    agent, pinned = _init_before_the_row_exists(db, cfg)
    agent._session_db_created = True  # the row was promised, but it is not there
    assert db.get_session(KEY) is None

    _turn_start_row_write(agent)  # must NOT raise: absent is not a conflict
    assert db.get_session(KEY) is None  # and nothing was resurrected

    # The row then appears (desktop prompt.submit, or the create path retrying) — the next turn pins.
    db.create_session(session_id=KEY, source="desktop", model="grok-4.6")
    _turn_start_row_write(agent)
    assert _row_config(db)[LINEAGE] == pinned


def test_failed_durability_probe_leaves_a_turn_that_still_runs(db, cfg, monkeypatch):
    """The reachable route into the state above: ``_durable_session_exists`` fails closed on a
    raised probe and suppresses the create for a row that does not exist."""
    agent, pinned = _init_before_the_row_exists(db, cfg)
    original = type(db).get_session

    def _ioerr(self, *args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(type(db), "get_session", _ioerr)
    admission = admit_durable_turn_lease(
        agent, session_id=KEY, relay_turn_id=f"{KEY}:t:abcd",
        task_context={"session_id": KEY, "task_id": "t", "platform": "desktop"},
        conversation_history=None,
    )
    try:
        assert agent._session_db_created is True  # fail-closed probe suppressed the create
        _turn_start_row_write(agent)  # must NOT raise, with the store still failing
    finally:
        if admission.lease is not None:
            admission.lease.release()
        monkeypatch.setattr(type(db), "get_session", original)

    db.create_session(session_id=KEY, source="desktop", model="grok-4.6")
    _turn_start_row_write(agent)
    assert _row_config(db)[LINEAGE] == pinned


def test_unparseable_model_config_still_fails_closed(db, cfg, monkeypatch):
    """The other state the store reports as None: a row whose model_config cannot be decoded is
    never written over, and must still abort the turn."""
    agent, _pinned = _init_before_the_row_exists(db, cfg)
    _desktop_row(db, monkeypatch)
    db._execute_write(lambda conn: conn.execute(
        "UPDATE sessions SET model_config = ? WHERE id = ?", ("{not json", KEY)))

    lease = _admit(agent, monkeypatch)
    try:
        with pytest.raises(RequiredContextError, match="model_config is unreadable"):
            _turn_start_row_write(agent)
        assert db.get_session(KEY)["model_config"] == "{not json"  # never replaced
        with pytest.raises(RequiredContextError, match="model_config is unreadable"):
            _turn_start_row_write(agent)  # no latch on a failed merge
    finally:
        lease.release()


@pytest.mark.parametrize("bad_pin", [None, ["generation-one"], "generation-one"], ids=["null", "list", "string"])
def test_unreadable_pinned_lineage_value_still_fails_closed(db, cfg, monkeypatch, bad_pin):
    """The column decodes but the PIN under the key does not, so the store hands back a non-Mapping
    for a row that is present. That is corruption, not a race: the create path has always refused it
    and the merge path must too, rather than run the turn against a pin it cannot read.

    ``null`` is the case a truthiness check cannot see: it decodes to Python ``None``, which is also
    what an ABSENT key reads as, so only presence tells them apart. Set-if-absent never overwrites a
    present key, so a null pin would otherwise stay fail-open for the life of the row."""
    agent, _pinned = _init_before_the_row_exists(db, cfg)
    _desktop_row(db, monkeypatch)
    corrupt = _row_config(db)
    corrupt[LINEAGE] = bad_pin  # something other than the lineage mapping
    db._execute_write(lambda conn: conn.execute(
        "UPDATE sessions SET model_config = ? WHERE id = ?", (json.dumps(corrupt), KEY)))

    lease = _admit(agent, monkeypatch)
    try:
        with pytest.raises(RequiredContextError, match="unreadable pinned lineage"):
            _turn_start_row_write(agent)
        assert _row_config(db)[LINEAGE] == bad_pin  # never replaced
        with pytest.raises(RequiredContextError, match="unreadable pinned lineage"):
            _turn_start_row_write(agent)  # no latch on a failed merge
    finally:
        lease.release()


def test_row_deleted_then_recreated_lineage_less_is_repinned_not_latched(db, cfg, monkeypatch):
    """FAILS ON BASE (and on the latch without its invalidator): a mid-life delete followed by a
    lineage-less recreate under the same id must not be covered by the confirmed-merge latch.

    The desktop sidebar can delete a session while another window still holds the agent; the next
    ``prompt.submit`` recreates the id through INSERT-OR-IGNORE with no metadata. A still-armed latch
    would persist this agent's block-carrying prompt onto that row with no pin — the incident shape.
    """
    agent, pinned = _init_before_the_row_exists(db, cfg)
    _desktop_row(db, monkeypatch)
    lease = _admit(agent, monkeypatch)
    try:
        _turn_start_row_write(agent)
    finally:
        lease.release()
    assert _row_config(db)[LINEAGE] == pinned  # turn 1 merged and latched

    assert db.delete_session(KEY) is True
    assert db.get_session(KEY) is None
    _desktop_row(db, monkeypatch)  # recreated under the same id, lineage-less
    assert LINEAGE not in _row_config(db)

    lease = _admit(agent, monkeypatch)  # the lease's own row read clears the stale latch
    try:
        _turn_start_row_write(agent)
    finally:
        lease.release()
    assert _row_config(db)[LINEAGE] == pinned  # re-pinned, so the next agent can restore


def test_raised_probe_after_a_delete_and_recreate_does_not_honour_the_latch(db, cfg, monkeypatch):
    """FAILS ON BASE: a probe that RAISED observed nothing, so it cannot leave the latch armed.

    Same delete-then-recreate as above, but the lease's row read hits the IOERR/lock the host already
    treats as first-class (``hermes_state.py`` #84234). The read pool failing is not proof the pin
    survived, and the merge runs in its own WRITE transaction which can still succeed — so honouring
    the latch here skips the re-pin while ``_persist_turn_start`` still writes the block-carrying
    prompt onto a lineage-less row. That is the incident shape, inside a single turn."""
    agent, pinned = _init_before_the_row_exists(db, cfg)
    _desktop_row(db, monkeypatch)
    lease = _admit(agent, monkeypatch)
    try:
        _turn_start_row_write(agent)
    finally:
        lease.release()
    assert _row_config(db)[LINEAGE] == pinned  # turn 1 merged and latched

    assert db.delete_session(KEY) is True
    _desktop_row(db, monkeypatch)  # recreated under the same id, lineage-less
    assert LINEAGE not in _row_config(db)

    original = type(db).get_session

    def _ioerr(self, *args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(type(db), "get_session", _ioerr)  # reads fail; writes still work
    try:
        lease = _admit(agent, monkeypatch)
        try:
            assert agent._session_db_created is True  # fail-closed probe suppressed the create
            _turn_start_row_write(agent)
        finally:
            lease.release()
    finally:
        monkeypatch.setattr(type(db), "get_session", original)

    assert _row_config(db)[LINEAGE] == pinned  # re-pinned despite the unreadable probe


def test_no_store_call_when_the_agent_pinned_nothing(db, monkeypatch):
    """required_context disabled: the suppressed path must stay free of DB work."""
    disabled = {"required_context": {"enabled": False}}
    agent = _agent(db)
    initialize_required_context_lineage(agent, config=disabled)
    assert agent._required_context_snapshot is None
    assert LINEAGE not in agent._session_init_model_config
    _desktop_row(db, monkeypatch)

    lease = _admit(agent, monkeypatch)
    calls = []
    monkeypatch.setattr(
        type(db), "set_session_model_config_key_if_absent",
        lambda self, sid, key, value: calls.append(key),
    )
    try:
        _turn_start_row_write(agent)
    finally:
        lease.release()
    assert calls == []
    assert LINEAGE not in _row_config(db)
