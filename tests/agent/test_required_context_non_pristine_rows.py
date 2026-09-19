"""Required-context lineage for rows that are born NON-pristine, and the backfill for rows already stranded.

``initialize_required_context_lineage`` pins a snapshot only onto a PRISTINE row. Every creator that
writes a row together with (or immediately before) its messages therefore has to carry the lineage in
its own INSERT, or that row fails closed on every later turn with "existing session has no pinned
snapshot". A row whose history was COPIED from a parent must INHERIT the parent's lineage — pinning the
current generation onto copied history is a lineage lie, not a fix.

Every test drives real code against a real ``SessionDB``; rows use the live shapes (NULL ``system_prompt``,
real ``model_config`` JSON), not stubs.
"""

import contextlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.backfill_required_context_lineage import EXIT_REFUSED

import agent.required_context as required_context
from agent.required_context import (
    LINEAGE_METADATA_KEY,
    RequiredContextError,
    branch_lineage_metadata,
    config_for_profile_home,
    initialize_required_context_lineage,
    load_required_context_snapshot,
    new_lineage_metadata,
)
from hermes_state import SessionDB

FILES = [
    "sakaan-workflow.md",
    "GRANTS.md",
    "ROUTING.md",
    "procedures/hardened-review.md",
    "Sakaan-crew.md",
]


@pytest.fixture(autouse=True)
def _canonical_trust_root(tmp_path, monkeypatch):
    root = tmp_path / ".sakaan"
    root.mkdir()
    monkeypatch.setattr(required_context, "_CANONICAL_SAKAAN_ROOT", root)
    monkeypatch.setattr(required_context, "_CANONICAL_POINTER", root / "current.txt")
    monkeypatch.setattr(required_context, "_CANONICAL_GENERATIONS_ROOT", root / "generations")


def _generation(tmp_path, name="generation-one"):
    generation = tmp_path / ".sakaan" / "generations" / name
    for file_name in FILES:
        target = generation / file_name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"{name}:{file_name}\r\n".encode("utf-8"))
    pointer = tmp_path / ".sakaan" / "current.txt"
    pointer.write_text(str(generation), encoding="utf-8")
    return generation


@pytest.fixture()
def cfg(tmp_path):
    _generation(tmp_path)
    return {"required_context": {
        "enabled": True, "pointer": str(tmp_path / ".sakaan" / "current.txt"), "files": FILES}}


@pytest.fixture()
def off_cfg(cfg):
    return {"required_context": {**cfg["required_context"], "enabled": False}}


@pytest.fixture()
def db(tmp_path):
    store = SessionDB(db_path=tmp_path / "state.db")
    yield store
    store.close()


@pytest.fixture()
def ambient(cfg, monkeypatch):
    """Make a bare ``_config(None)`` resolve to the enabled test config, as a single-profile process does."""
    monkeypatch.setattr(required_context, "_config", lambda config: cfg if config is None else config)
    return cfg


def _agent(db, session_id, **extra):
    return SimpleNamespace(session_id=session_id, _session_db=db, _session_init_model_config={}, **extra)


def _row_config(db, session_id):
    return json.loads(db.get_session(session_id)["model_config"] or "{}")


def _lineage(db, session_id):
    return _row_config(db, session_id).get(LINEAGE_METADATA_KEY)


def _pinned_parent(db, cfg, session_id="parent", generation=None):
    """A realistic pinned parent: v1 lineage, NULL system_prompt, two turns."""
    snapshot = load_required_context_snapshot(generation or cfg)
    db.create_session(session_id, source="desktop", model="claude-opus-5",
                      model_config={"model": "claude-opus-5",
                                    LINEAGE_METADATA_KEY: required_context.snapshot_to_metadata(snapshot)})
    db.append_message(session_id, role="user", content="hello")
    db.append_message(session_id, role="assistant", content="hi")
    return snapshot


def _stranded(db, session_id="stranded"):
    """The live shape this whole task exists for: messages, real model_config, NULL prompt, no lineage."""
    db.create_session(session_id, source="desktop", model="claude-opus-5",
                      model_config={"model": "claude-opus-5", "provider": "anthropic",
                                    "reasoning_config": {"effort": "high"}})
    db.append_message(session_id, role="user", content="a turn from before the guard was on")
    db.append_message(session_id, role="assistant", content="a reply from before the guard was on")
    row = db.get_session(session_id)
    assert row["message_count"] == 2 and not row["system_prompt"]
    assert LINEAGE_METADATA_KEY not in json.loads(row["model_config"])


# ── the acceptance core: a stranded row, before and after the backfill ────────────────────────────
def test_stranded_row_fails_closed_with_an_actionable_message(db, cfg):
    _stranded(db)
    with pytest.raises(RequiredContextError, match="existing session has no pinned snapshot"):
        initialize_required_context_lineage(_agent(db, "stranded"), config=cfg)


def test_backfilled_row_then_runs_a_normal_turn(db, cfg, tmp_path, monkeypatch, capsys):
    _stranded(db)
    _run_backfill(db, cfg, tmp_path, monkeypatch, apply=True)

    agent = _agent(db, "stranded")
    touched = []
    initialize_required_context_lineage(agent, config=cfg, before_provider=lambda: touched.append(1))
    assert touched == [1]
    expected = load_required_context_snapshot(cfg)
    assert agent._required_context_snapshot.prompt_sha256 == expected.prompt_sha256
    # The row keeps everything it had; the lineage is merged in beside it, as v1.
    persisted = _row_config(db, "stranded")
    assert persisted["model"] == "claude-opus-5" and persisted["reasoning_config"] == {"effort": "high"}
    assert persisted[LINEAGE_METADATA_KEY]["version"] == 1
    assert "system_prompt_sha256" not in persisted[LINEAGE_METADATA_KEY]


# ── S1: the seeded-branch fallback must never mispin ──────────────────────────────────────────────
def _seed_branch(monkeypatch, db, parent_key, key="seeded-child", profile_home=None):
    from tui_gateway import server

    monkeypatch.setattr(server, "_session_db", lambda record: contextlib.nullcontext(db))
    monkeypatch.setattr(server, "_resolve_model", lambda: "claude-opus-5")
    monkeypatch.setattr(server, "_current_profile_name", lambda: "default")
    monkeypatch.setattr(server, "profile_name_for_home", lambda home: None)
    return server._seed_branch_row({"cwd": None, "history_lock": __import__("threading").Lock()},
                                   key, parent_key, [{"role": "user", "content": "hi"}], "desktop", profile_home)


def test_s1_seed_branch_write_failure_fails_closed_instead_of_falling_back(db, ambient, monkeypatch):
    """A generic write failure used to fall through to lazy row creation, whose first-prompt path pins the
    CURRENT generation onto history already copied from the parent. Under the guard there is no fallback."""
    from tui_gateway import server

    _pinned_parent(db, ambient)
    monkeypatch.setattr(server, "_persist_branch", _raise(RuntimeError("database is locked")))
    with pytest.raises(RequiredContextError, match="could not inherit its parent's pinned snapshot"):
        _seed_branch(monkeypatch, db, "parent")
    assert db.get_session("seeded-child") is None


def test_s1_seed_branch_write_failure_still_falls_back_while_the_guard_is_off(db, off_cfg, monkeypatch):
    """The #93959 fallback is not removed — it is only unavailable while the guard is on."""
    from tui_gateway import server

    monkeypatch.setattr(required_context, "_config", lambda config: off_cfg if config is None else config)
    db.create_session("parent", source="desktop", model="claude-opus-5")
    db.append_message("parent", role="user", content="hello")
    monkeypatch.setattr(server, "_persist_branch", _raise(RuntimeError("database is locked")))
    assert _seed_branch(monkeypatch, db, "parent") is None
    assert db.get_session("seeded-child") is None


def _raise(exc):
    def _boom(*_args, **_kwargs):
        raise exc
    return _boom


# ── S2: the decision follows the SESSION's profile, not the launch profile ────────────────────────
def _profile_home(tmp_path, name, *, enabled):
    home = tmp_path / "profiles" / name
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(
        "required_context:\n"
        f"  enabled: {'true' if enabled else 'false'}\n"
        f"  pointer: {tmp_path / '.sakaan' / 'current.txt'}\n"
        "  files:\n" + "".join(f"    - {name}\n" for name in FILES),
        encoding="utf-8")
    return home


def test_s2_profile_config_decides_the_guard_not_the_launch_profile(tmp_path, monkeypatch):
    """``config_for_profile_home`` reads THAT profile's config.yaml. The launch profile here has no
    required_context at all, so a bare read says 'off' for a profile that has it on."""
    _generation(tmp_path)
    launch = tmp_path / "launch"
    launch.mkdir()
    (launch / "config.yaml").write_text("model:\n  default: claude-opus-5\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(launch))

    on = _profile_home(tmp_path, "on", enabled=True)
    off = _profile_home(tmp_path, "off", enabled=False)
    assert required_context.required_context_enabled(config_for_profile_home(on)) is True
    assert required_context.required_context_enabled(config_for_profile_home(off)) is False
    assert required_context.required_context_enabled(config_for_profile_home(None)) is False
    # The override is context-local and must not leak into the next read.
    assert required_context.required_context_enabled() is False


def test_s2_persist_branch_uses_the_passed_profile_config(db, tmp_path, monkeypatch):
    """``_persist_branch`` without ``config=`` judged every branch by the launch profile. With the
    session's profile threaded through, a lineage-less parent on an ENABLED profile is refused even
    though the launch profile has the guard off."""
    from tui_gateway import server

    _generation(tmp_path)
    launch = tmp_path / "launch"
    launch.mkdir()
    (launch / "config.yaml").write_text("model:\n  default: claude-opus-5\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(launch))
    monkeypatch.setattr(server, "_resolve_model", lambda: "claude-opus-5")
    db.create_session("parent", source="desktop", model="claude-opus-5")
    db.append_message("parent", role="user", content="hello")

    # Launch profile (guard off): the legacy behaviour, a branch with no lineage.
    server._persist_branch(db, "child-launch", "parent", "b", [], source="desktop", cwd=None,
                           profile_name=None, config=config_for_profile_home(None))
    assert _lineage(db, "child-launch") is None

    # The session's own profile has it on, so the same lineage-less parent is refused.
    on = _profile_home(tmp_path, "on", enabled=True)
    with pytest.raises(RequiredContextError, match="branch parent session has no pinned snapshot"):
        server._persist_branch(db, "child-profile", "parent", "b", [], source="desktop", cwd=None,
                               profile_name=None, config=config_for_profile_home(on))
    assert db.get_session("child-profile") is None


# ── S3: rows born with history ────────────────────────────────────────────────────────────────────
def test_s3_seeded_branch_row_created_lazily_inherits_the_parent(db, ambient, monkeypatch):
    """``_ensure_session_db_row`` for a SEEDED child with a parent: inherit, never pin fresh."""
    from tui_gateway import server

    snapshot = _pinned_parent(db, ambient)
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_resolve_model", lambda: "claude-opus-5")
    assert server._ensure_session_db_row({
        "session_key": "lazy-child", "source": "desktop", "seeded": True, "parent_session_id": "parent"}) is True

    lineage = _lineage(db, "lazy-child")
    assert lineage is not None and lineage["version"] == 1
    assert lineage["prompt_sha256"] == snapshot.prompt_sha256
    # And it resolves on the child's first turn rather than failing closed.
    child = _agent(db, "lazy-child")
    initialize_required_context_lineage(child, config=ambient)
    assert child._required_context_snapshot.prompt_block == snapshot.prompt_block


def test_s3_seeded_parentless_row_pins_the_current_generation(db, ambient, monkeypatch):
    """A client opening a chat with its own opening turns: nothing to inherit, so it starts a lineage."""
    from tui_gateway import server

    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_resolve_model", lambda: "claude-opus-5")
    assert server._ensure_session_db_row({
        "session_key": "seeded-solo", "source": "desktop", "seeded": True}) is True
    assert _lineage(db, "seeded-solo")["prompt_sha256"] == load_required_context_snapshot(ambient).prompt_sha256


def test_s3_unseeded_row_stays_pristine_for_the_runtime_pin(db, ambient, monkeypatch):
    """The R15 pristine pin must NOT be pre-empted: an ordinary desktop row is still born bare."""
    from tui_gateway import server

    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_resolve_model", lambda: "claude-opus-5")
    assert server._ensure_session_db_row({"session_key": "plain", "source": "desktop"}) is True
    assert _lineage(db, "plain") is None
    initialize_required_context_lineage(_agent(db, "plain"), config=ambient)
    assert _lineage(db, "plain") is not None  # pinned by the runtime, in its own transaction


def test_s3_seeded_branch_of_a_lineage_less_parent_is_refused_not_mispinned(db, ambient, monkeypatch):
    from tui_gateway import server

    db.create_session("bare-parent", source="desktop", model="claude-opus-5")
    db.append_message("bare-parent", role="user", content="hello")
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_resolve_model", lambda: "claude-opus-5")
    with pytest.raises(RequiredContextError, match="branch parent session has no pinned snapshot"):
        server._ensure_session_db_row({
            "session_key": "child", "source": "desktop", "seeded": True, "parent_session_id": "bare-parent"})
    assert db.get_session("child") is None


def test_s3_gateway_route_row_is_pinned_at_creation(db, ambient):
    """Bot Mode: several writers append message rows between this INSERT and the first agent, so the
    lineage cannot wait for the runtime's pristine pin."""
    from gateway.session_recovery import SessionRecoveryMixin

    store = SessionRecoveryMixin()
    store._db_for_key = lambda key: db
    store._record_gateway_session_peer = lambda *a, **k: None
    store._resolve_profile_for_key = lambda origin: None
    store._create_session_row(
        "agent:main:telegram:dm:1",
        {"session_id": "bot-1", "source": "telegram", "model_config": {"_reset_from": "older"}},
        None, None, log=_raise(AssertionError("create must not fail")))

    persisted = _row_config(db, "bot-1")
    assert persisted["_reset_from"] == "older"  # the existing kwargs survive
    assert persisted[LINEAGE_METADATA_KEY]["prompt_sha256"] == load_required_context_snapshot(
        ambient).prompt_sha256
    # A user row appended before any agent exists no longer strands the session.
    db.append_message("bot-1", role="user", content="[MCP servers have been reloaded...]")
    initialize_required_context_lineage(_agent(db, "bot-1"), config=ambient)


def test_s3_foreign_import_pins_the_current_generation(db, ambient, tmp_path, monkeypatch):
    """Imported Claude/Codex turns were produced under no Sakaan generation, so they start one here."""
    from hermes_cli import foreign_sessions

    def item(role, kind, text):
        return {"timestamp": "2026-08-15T21:35:28Z", "type": "response_item",
                "payload": {"type": "message", "role": role, "content": [{"type": kind, "text": text}]}}

    transcript = tmp_path / "rollout-2026-08-15T21-35-28-0000-1111.jsonl"
    transcript.write_text("\n".join(json.dumps(line) for line in [
        {"type": "session_meta", "payload": {"session_id": "0000-1111", "cwd": "/home/user/repo"}},
        item("user", "input_text", "Summarize the transcripts please."),
        item("assistant", "output_text", "Reading them one at a time."),
    ]) + "\n", encoding="utf-8")
    session_id = foreign_sessions.import_foreign_session("codex", transcript, db=db)

    row = db.get_session(session_id)
    assert row["message_count"] == 2
    assert _lineage(db, session_id)["prompt_sha256"] == load_required_context_snapshot(ambient).prompt_sha256
    initialize_required_context_lineage(_agent(db, session_id), config=ambient)


def test_s3_acp_save_no_longer_wipes_the_pinned_lineage(db, ambient):
    """``_persist``'s update branch replaced model_config wholesale, deleting the lineage on every save."""
    from acp_adapter.session import SessionManager, SessionState

    _pinned_parent(db, ambient, session_id="acp-1")
    manager = SessionManager(db=db)
    state = SessionState(session_id="acp-1", agent=SimpleNamespace(provider="anthropic"), cwd="/w",
                         model="claude-opus-5", history=[{"role": "user", "content": "hello"}])
    manager._persist(state)

    assert _lineage(db, "acp-1") is not None
    assert json.loads(db.get_session("acp-1")["model_config"])["cwd"] == "/w"  # the meta it meant to write
    initialize_required_context_lineage(_agent(db, "acp-1"), config=ambient)


def test_s3_acp_fork_row_inherits_before_its_agent_is_built(db, ambient):
    from acp_adapter.session import SessionManager

    snapshot = _pinned_parent(db, ambient, session_id="acp-parent")
    manager = SessionManager(agent_factory=lambda **kw: SimpleNamespace(model="claude-opus-5"), db=db)
    manager._precreate_fork_row("acp-fork", SimpleNamespace(session_id="acp-parent", model="claude-opus-5"), "/w")

    lineage = _lineage(db, "acp-fork")
    assert lineage["version"] == 1 and lineage["prompt_sha256"] == snapshot.prompt_sha256


def test_s3_gateway_turn_metadata_sync_keeps_a_lineage_pinned_after_its_read(db, ambient):
    """R15's runtime-save race in its Bot-Mode form: ``_sync_session_model_from_agent`` SELECTs
    model_config, mutates it in Python and writes the whole column back on EVERY turn. A lineage
    pinned between that read and that write used to be erased — and the next turn then failed closed."""
    from agent.required_context import snapshot_to_metadata

    db.create_session("bot-2", source="telegram", model="claude-opus-5", model_config={"model": "claude-opus-5"})
    stale = json.loads(db.get_session("bot-2")["model_config"])  # the turn's read

    metadata = snapshot_to_metadata(load_required_context_snapshot(ambient))
    assert db.set_session_model_config_key_if_absent("bot-2", LINEAGE_METADATA_KEY, metadata) == metadata

    stale["gateway_runtime"] = {"provider": "anthropic"}  # ...and now the turn writes its stale copy back
    db.update_session_meta_preserving_keys("bot-2", stale, "claude-opus-5",
                                           preserve_keys=(LINEAGE_METADATA_KEY,))

    persisted = _row_config(db, "bot-2")
    assert persisted["gateway_runtime"] == {"provider": "anthropic"}  # the sync still does its job
    assert persisted[LINEAGE_METADATA_KEY] == metadata  # without eating the lineage
    initialize_required_context_lineage(_agent(db, "bot-2"), config=ambient)


# The CLI ``/new`` case needs the real HermesCLI harness, so it lives with the other /new tests:
# tests/hermes_cli/test_cli_new_session.py::test_new_session_row_records_the_agents_pinned_lineage


# ── S4: the backfill script ───────────────────────────────────────────────────────────────────────
def _run_backfill(db, cfg, tmp_path, monkeypatch, *, apply=False, backup=None, extra=(), quiescent=True):
    """Invoke the script's ``main`` against this test DB, with its config read stubbed to ``cfg``.

    ``quiescent`` stubs the live-holder precondition out. It has to be stubbed for the rows-and-writes
    tests: the ``db`` fixture holds this very state.db open IN THIS PROCESS, and the Windows
    exclusive-share probe cannot tell our own handle from another process's — which is the right answer
    for the real tool and the wrong one for a test that needs the store. The probe itself is covered
    separately by ``test_s4_apply_refuses_while_the_database_is_still_open`` and
    ``test_s4_live_holder_probe_passes_on_a_closed_database``, which do NOT stub it.
    """
    import scripts.backfill_required_context_lineage as backfill

    monkeypatch.setattr(required_context, "_config", lambda config: cfg if config is None else config)
    if quiescent:
        # ``raising=False``: the precondition does not exist on the pre-repair commit, and a
        # fail-on-base run must exercise each test's real behaviour, not trip over a missing symbol.
        monkeypatch.setattr(backfill, "_require_no_live_holder", lambda _db_path: None, raising=False)
    argv = ["--db", str(tmp_path / "state.db"), *extra]
    if apply:
        argv.append("--apply")
        argv += ["--backup", str(backup or tmp_path / "backup" / "state.db.bak")]
    return backfill.main(argv)


def test_s4_dry_run_writes_nothing_and_reports_the_plan(db, cfg, tmp_path, monkeypatch, capsys):
    _stranded(db)
    assert _run_backfill(db, cfg, tmp_path, monkeypatch) == 0
    assert "PLAN" in capsys.readouterr().out
    assert _lineage(db, "stranded") is None  # a dry run is a dry run


def test_s4_apply_refuses_without_a_backup(db, cfg, tmp_path, monkeypatch, capsys):
    import scripts.backfill_required_context_lineage as backfill

    _stranded(db)
    monkeypatch.setattr(required_context, "_config", lambda config: cfg if config is None else config)
    assert backfill.main(["--db", str(tmp_path / "state.db"), "--apply"]) == backfill.EXIT_REFUSED
    assert "requires --backup" in capsys.readouterr().err
    assert _lineage(db, "stranded") is None


def test_s4_apply_writes_a_verified_backup_first(db, cfg, tmp_path, monkeypatch):
    _stranded(db)
    backup = tmp_path / "backup" / "state.db.bak"
    assert _run_backfill(db, cfg, tmp_path, monkeypatch, apply=True, backup=backup) == 0
    assert backup.stat().st_size > 0
    # The backup is a real, openable snapshot taken BEFORE the write: it still has no lineage.
    with SessionDB(db_path=backup) as restored:
        assert LINEAGE_METADATA_KEY not in json.loads(restored.get_session("stranded")["model_config"])
    assert _lineage(db, "stranded") is not None


def test_s4_is_idempotent(db, cfg, tmp_path, monkeypatch, capsys):
    _stranded(db)
    assert _run_backfill(db, cfg, tmp_path, monkeypatch, apply=True) == 0
    first = _lineage(db, "stranded")
    capsys.readouterr()
    assert _run_backfill(db, cfg, tmp_path, monkeypatch, apply=True,
                         backup=tmp_path / "backup2" / "state.db.bak") == 0
    out = capsys.readouterr().out
    assert "already_pinned" in out and "0 to pin" in out
    assert _lineage(db, "stranded") == first  # untouched, not re-pinned


def test_s4_refuses_to_overwrite_an_existing_backup(db, cfg, tmp_path, monkeypatch, capsys):
    _stranded(db)
    backup = tmp_path / "taken.bak"
    backup.write_bytes(b"do not clobber me")
    assert _run_backfill(db, cfg, tmp_path, monkeypatch, apply=True, backup=backup) != 0
    assert "refusing to overwrite" in capsys.readouterr().err
    assert backup.read_bytes() == b"do not clobber me"
    assert _lineage(db, "stranded") is None


def test_s4_session_id_limits_scope(db, cfg, tmp_path, monkeypatch):
    _stranded(db, "one")
    _stranded(db, "two")
    assert _run_backfill(db, cfg, tmp_path, monkeypatch, apply=True, extra=["--session-id", "one"]) == 0
    assert _lineage(db, "one") is not None
    assert _lineage(db, "two") is None


def test_s4_unknown_session_id_is_a_refusal(db, cfg, tmp_path, monkeypatch, capsys):
    _stranded(db)
    assert _run_backfill(db, cfg, tmp_path, monkeypatch, extra=["--session-id", "nope"]) != 0
    assert "not found in this database" in capsys.readouterr().err


def _prompted_row(db, session_id, prompt):
    db.create_session(session_id, source="cli", model="claude-opus-5", system_prompt=prompt)
    db.append_message(session_id, role="user", content="hello")
    # The live shape B3 turned on: sessions.system_prompt is write-NULL-only, the prompt is interned.
    assert db._read_one("SELECT system_prompt FROM sessions WHERE id = ?", (session_id,))[0] is None
    assert db.get_session(session_id)["system_prompt"] == prompt


def _block_for(tmp_path, cfg, name):
    """A prompt carrying generation ``name``'s block, with the pointer left back on generation-one."""
    from agent.required_context import append_required_context

    _generation(tmp_path, name)
    other = load_required_context_snapshot({"required_context": {**cfg["required_context"], "enabled": True}})
    _generation(tmp_path)  # point back at generation-one, which is NOT what this row must be pinned to
    return append_required_context("You are Hermes.", other), other


# ── B3 class 2: a row whose prompt already declares a generation is pinned to THAT generation ────
def test_runtime_class2_self_heals_from_its_own_canonical_block(db, cfg, tmp_path):
    prompt, other = _block_for(tmp_path, cfg, "generation-two")
    _prompted_row(db, "prompted", prompt)

    agent = _agent(db, "prompted")
    initialize_required_context_lineage(agent, config=cfg)

    lineage = _lineage(db, "prompted")
    assert lineage["version"] == 1 and "system_prompt_sha256" not in lineage
    assert lineage["generation"] == other.generation != load_required_context_snapshot(cfg).generation
    assert agent._required_context_snapshot.prompt_block == other.prompt_block


def test_runtime_class2_post_pin_reversed_prompt_race_is_typed_fail_closed(
    db, cfg, tmp_path, monkeypatch,
):
    from agent.required_context import REQUIRED_CONTEXT_BEGIN, REQUIRED_CONTEXT_END

    prompt, _other = _block_for(tmp_path, cfg, "generation-raced")
    _prompted_row(db, "prompted", prompt)
    original_get_session = db.get_session
    reads = 0

    def racing_get_session(session_id):
        nonlocal reads
        row = original_get_session(session_id)
        reads += 1
        if reads == 2:
            return {
                **row,
                "system_prompt": f"{REQUIRED_CONTEXT_END}\nraced content\n{REQUIRED_CONTEXT_BEGIN}",
            }
        return row

    monkeypatch.setattr(db, "get_session", racing_get_session)
    provider_calls = []

    with pytest.raises(RequiredContextError, match="WORKFLOW_SOURCE_UNAVAILABLE"):
        initialize_required_context_lineage(
            _agent(db, "prompted"), config=cfg, before_provider=lambda: provider_calls.append(1),
        )

    assert provider_calls == []


@pytest.mark.parametrize("shape", ["generation_bytes_changed", "legacy_header", "end_before_begin"])
def test_runtime_class2_invalid_proof_stays_typed_fail_closed(db, cfg, tmp_path, shape):
    if shape == "generation_bytes_changed":
        prompt, other = _block_for(tmp_path, cfg, "generation-edited")
        (Path(other.generation) / "GRANTS.md").write_bytes(b"edited after the prompt was persisted\r\n")
    elif shape == "legacy_header":
        prompt, _other = _legacy_block_prompt(tmp_path, cfg)
    else:
        from agent.required_context import REQUIRED_CONTEXT_BEGIN, REQUIRED_CONTEXT_END

        prompt = f"{REQUIRED_CONTEXT_END}\nlegacy content\n{REQUIRED_CONTEXT_BEGIN}"
    _prompted_row(db, "prompted", prompt)
    before = db.get_session("prompted")["model_config"]

    with pytest.raises(RequiredContextError, match="WORKFLOW_SOURCE_UNAVAILABLE"):
        initialize_required_context_lineage(_agent(db, "prompted"), config=cfg)

    assert db.get_session("prompted")["model_config"] == before
    assert _lineage(db, "prompted") is None


def test_s4_class2_pins_the_generation_its_own_block_declares(db, cfg, tmp_path, monkeypatch, capsys):
    """Not today's pointer: the conversation really saw generation-two, so that is the truthful pin —
    and pinning generation-one instead would leave prompt and metadata disagreeing, which is exactly
    the ``persisted prompt integrity is invalid`` hard error after the flip."""
    prompt, other = _block_for(tmp_path, cfg, "generation-two")
    _prompted_row(db, "prompted", prompt)

    assert _run_backfill(db, cfg, tmp_path, monkeypatch, apply=True) == 0
    assert "class2_prompt_block_pins_its_own_generation" in capsys.readouterr().out
    lineage = _lineage(db, "prompted")
    assert lineage["version"] == 1 and "system_prompt_sha256" not in lineage
    assert lineage["generation"] == other.generation != load_required_context_snapshot(cfg).generation
    assert lineage["prompt_sha256"] == other.prompt_sha256
    # And the row now takes a normal turn instead of failing closed.
    agent = _agent(db, "prompted")
    initialize_required_context_lineage(agent, config=cfg)
    assert agent._required_context_snapshot.prompt_block == other.prompt_block


def test_s4_class2_refuses_a_block_whose_generation_is_no_longer_on_disk(db, cfg, tmp_path, monkeypatch, capsys):
    prompt, other = _block_for(tmp_path, cfg, "generation-gone")
    _prompted_row(db, "prompted", prompt)
    import shutil
    shutil.rmtree(other.generation)

    assert _run_backfill(db, cfg, tmp_path, monkeypatch, apply=True) == EXIT_REFUSED
    assert "class2_declared_generation_could_not_be_loaded" in capsys.readouterr().out
    assert _lineage(db, "prompted") is None


def test_s4_pins_a_row_whose_prompt_already_carries_the_current_block(db, cfg, tmp_path, monkeypatch):
    from agent.required_context import append_required_context

    _prompted_row(db, "matching", append_required_context("You are Hermes.", load_required_context_snapshot(cfg)))
    assert _run_backfill(db, cfg, tmp_path, monkeypatch, apply=True) == 0
    assert _lineage(db, "matching")["version"] == 1
    initialize_required_context_lineage(_agent(db, "matching"), config=cfg)


def test_s4_refuses_a_prompt_with_duplicated_block_markers(db, cfg, tmp_path, monkeypatch, capsys):
    from agent.required_context import REQUIRED_CONTEXT_BEGIN, REQUIRED_CONTEXT_END, append_required_context

    prompt = append_required_context("You are Hermes.", load_required_context_snapshot(cfg))
    _prompted_row(db, "doubled", f"{prompt}\n{REQUIRED_CONTEXT_BEGIN}\n{REQUIRED_CONTEXT_END}")
    assert _run_backfill(db, cfg, tmp_path, monkeypatch, apply=True) == EXIT_REFUSED
    assert "prompt_has_duplicate_or_unbalanced_block_markers" in capsys.readouterr().out
    assert _lineage(db, "doubled") is None


# ── R3 class 2b: a block THIS code cannot reproduce is cleared, never pinned ──────────────────────
def _legacy_block_prompt(tmp_path, cfg, name="generation-legacy"):
    """A prompt carrying the REAL legacy block the three live 2026-09-08 rows persisted.

    Non-normative test data, copied from the coordinator's read of a COPY of the live database:
    ``# Required Context Snapshot`` where today's ``_build_snapshot`` writes ``# Required Sakaan
    Context``, plus a ``generation_sha256:`` line the current emitter does not write at all. The
    generation it names is left on disk and unedited, so the ONLY thing making this row unpinnable is
    the block format — which is what makes it class 2b instead of a load failure or a mismatch.
    """
    from agent.required_context import REQUIRED_CONTEXT_BEGIN

    prompt, other = _block_for(tmp_path, cfg, name)
    lines = other.prompt_block.splitlines()
    assert lines[:2] == [REQUIRED_CONTEXT_BEGIN, "# Required Sakaan Context"]
    assert lines[2] == f"generation: {other.generation}"
    legacy = "\n".join([lines[0], "# Required Context Snapshot", lines[2],
                        f"generation_sha256: {'be3ae5ff' * 8}", *lines[3:]])
    assert prompt.count(other.prompt_block) == 1
    return prompt.replace(other.prompt_block, legacy), other


def test_s4_class2b_legacy_header_is_refused_without_the_flag(db, cfg, tmp_path, monkeypatch, capsys):
    """The bug this round exists for: the live rows' block cannot be reproduced by this code, so no
    metadata can ever satisfy it, and pinning it is not an option — but neither is silently clearing
    an operator's prompt, so without the flag it is a named refusal and a non-zero exit."""
    prompt, _other = _legacy_block_prompt(tmp_path, cfg)
    _prompted_row(db, "legacy", prompt)

    assert _run_backfill(db, cfg, tmp_path, monkeypatch, apply=True) == EXIT_REFUSED
    out = capsys.readouterr().out
    assert "class2b_block_this_code_cannot_reproduce_needs_--clear-stale-prompt" in out
    assert "legacy_or_unrecognized_block_header" in out
    assert _lineage(db, "legacy") is None
    assert db.get_session("legacy")["system_prompt"] == prompt  # untouched


def test_s4_class2b_legacy_header_with_the_flag_clears_and_pins(db, cfg, tmp_path, monkeypatch, capsys):
    """Operator decision 2026-09-18: the same act already authorized for class 3. The stale legacy
    block goes, the current generation is pinned, and the row takes a normal turn again."""
    prompt, other = _legacy_block_prompt(tmp_path, cfg)
    _prompted_row(db, "legacy", prompt)

    assert _run_backfill(db, cfg, tmp_path, monkeypatch, apply=True, extra=["--clear-stale-prompt"]) == 0
    out = capsys.readouterr().out
    assert "class2b_legacy_block_dropped_then_pinned" in out
    # The legacy header is unparseable to this code, but it DOES name a generation, and the operator
    # is owed it — read back verbatim and labelled unverified, never silently turned into "none".
    assert f"dropping declared generation: {other.generation} (unverified:" in out
    assert not db.get_session("legacy")["system_prompt"]
    assert _lineage(db, "legacy")["prompt_sha256"] == load_required_context_snapshot(cfg).prompt_sha256
    initialize_required_context_lineage(_agent(db, "legacy"), config=cfg)


def test_s4_class2b_mismatching_block_is_refused_without_the_flag(db, cfg, tmp_path, monkeypatch, capsys):
    """The other 2b shape: the header parses and the generation still loads, but that generation no
    longer rebuilds to these bytes, so the restore path's byte comparison could never accept it."""
    prompt, other = _block_for(tmp_path, cfg, "generation-edited")
    _prompted_row(db, "prompted", prompt)
    (Path(other.generation) / "GRANTS.md").write_bytes(b"edited after the fact\r\n")

    assert _run_backfill(db, cfg, tmp_path, monkeypatch, apply=True) == EXIT_REFUSED
    out = capsys.readouterr().out
    assert "class2b_block_this_code_cannot_reproduce_needs_--clear-stale-prompt" in out
    assert f"block_is_not_what_that_generation_rebuilds_to; declared generation: {other.generation}" in out
    assert _lineage(db, "prompted") is None
    assert db.get_session("prompted")["system_prompt"] == prompt


def test_s4_class2b_mismatching_block_with_the_flag_names_the_generation_it_drops(
        db, cfg, tmp_path, monkeypatch, capsys):
    """...and when it IS cleared, the output names the superseded generation, so the operator can see
    what was dropped rather than only that something was."""
    prompt, other = _block_for(tmp_path, cfg, "generation-edited")
    _prompted_row(db, "prompted", prompt)
    (Path(other.generation) / "GRANTS.md").write_bytes(b"edited after the fact\r\n")

    assert _run_backfill(db, cfg, tmp_path, monkeypatch, apply=True, extra=["--clear-stale-prompt"]) == 0
    out = capsys.readouterr().out
    assert "class2b_legacy_block_dropped_then_pinned" in out
    assert f"dropping declared generation: {other.generation}" in out
    assert not db.get_session("prompted")["system_prompt"]
    current = load_required_context_snapshot(cfg)
    assert _lineage(db, "prompted")["generation"] == current.generation != str(other.generation)
    initialize_required_context_lineage(_agent(db, "prompted"), config=cfg)


def test_s4_class2b_is_idempotent_on_a_rerun(db, cfg, tmp_path, monkeypatch, capsys):
    """A second pass over a cleared 2b row is ``already_pinned`` and a clean exit — it has no block
    left to re-classify, and the re-run re-verifies the pin exactly as the runtime would."""
    prompt, _other = _legacy_block_prompt(tmp_path, cfg)
    _prompted_row(db, "legacy", prompt)
    assert _run_backfill(db, cfg, tmp_path, monkeypatch, apply=True, extra=["--clear-stale-prompt"]) == 0
    first = _lineage(db, "legacy")
    capsys.readouterr()

    assert _run_backfill(db, cfg, tmp_path, monkeypatch, apply=True, extra=["--clear-stale-prompt"],
                         backup=tmp_path / "backup2" / "state.db.bak") == 0
    out = capsys.readouterr().out
    assert "already_pinned" in out and "0 to pin" in out
    assert _lineage(db, "legacy") == first


def test_s4_class2b_does_not_swallow_a_generation_that_merely_could_not_be_read(
        db, cfg, tmp_path, monkeypatch, capsys):
    """An offline or transiently unreadable generation is NOT 2b: clearing the prompt over it would
    destroy the only surviving record of what the conversation saw, so it stays a hard refusal even
    with the clearing flag on."""
    import shutil

    prompt, other = _block_for(tmp_path, cfg, "generation-gone")
    _prompted_row(db, "prompted", prompt)
    shutil.rmtree(other.generation)

    assert _run_backfill(db, cfg, tmp_path, monkeypatch, apply=True, extra=["--clear-stale-prompt"]) == EXIT_REFUSED
    out = capsys.readouterr().out
    assert "class2_declared_generation_could_not_be_loaded" in out
    assert "class2b_block_this_code_cannot_reproduce" not in out
    assert "class2b_legacy_block_dropped_then_pinned" not in out
    assert _lineage(db, "prompted") is None
    assert db.get_session("prompted")["system_prompt"] == prompt


# ── B3 class 3: a stale blockless prompt needs its own, default-off authorization ─────────────────
def test_s4_class3_is_refused_without_the_separate_flag(db, cfg, tmp_path, monkeypatch, capsys):
    _prompted_row(db, "bare-prompt", "You are Hermes.")
    assert _run_backfill(db, cfg, tmp_path, monkeypatch, apply=True) == EXIT_REFUSED
    out = capsys.readouterr().out
    assert "class3_stale_prompt_without_block_needs_--clear-stale-prompt" in out
    assert "class 2b/3 stale-prompt clearing: off (refuses)" in out
    assert _lineage(db, "bare-prompt") is None
    assert db.get_session("bare-prompt")["system_prompt"] == "You are Hermes."  # untouched


def test_s4_class3_with_the_flag_clears_the_stale_prompt_and_pins(db, cfg, tmp_path, monkeypatch, capsys):
    """The capability, built but not used: with the operator's separate authorization the stale prompt
    is cleared so the next build re-emits it around the block — exactly what ``/model`` already does."""
    _prompted_row(db, "bare-prompt", "You are Hermes.")
    assert _run_backfill(db, cfg, tmp_path, monkeypatch, apply=True, extra=["--clear-stale-prompt"]) == 0
    assert "class3_stale_prompt_cleared_then_pinned" in capsys.readouterr().out
    assert not db.get_session("bare-prompt")["system_prompt"]
    assert _lineage(db, "bare-prompt")["prompt_sha256"] == load_required_context_snapshot(cfg).prompt_sha256
    initialize_required_context_lineage(_agent(db, "bare-prompt"), config=cfg)


def test_s4_clear_stale_prompt_is_not_implied_by_apply_for_the_other_classes(db, cfg, tmp_path, monkeypatch):
    """The flag must not widen what happens to a class-1/class-2 row: only class 3 consults it."""
    prompt, other = _block_for(tmp_path, cfg, "generation-two")
    _prompted_row(db, "prompted", prompt)
    _stranded(db, "no-prompt")

    assert _run_backfill(db, cfg, tmp_path, monkeypatch, apply=True, extra=["--clear-stale-prompt"]) == 0
    assert db.get_session("prompted")["system_prompt"] == prompt  # class 2 keeps its prompt
    assert _lineage(db, "prompted")["generation"] == other.generation
    assert _lineage(db, "no-prompt")["generation"] == load_required_context_snapshot(cfg).generation


# ── B2: the backfill→flip window ─────────────────────────────────────────────────────────────────
def test_s4_apply_refuses_while_the_database_is_still_open(db, cfg, tmp_path, monkeypatch, capsys):
    """"Stop Hermes first" is CHECKED, not documented: the ``db`` fixture is a live holder of this very
    state.db, and the real precondition (not stubbed here) refuses rather than racing it."""
    _stranded(db)
    assert _run_backfill(db, cfg, tmp_path, monkeypatch, apply=True, quiescent=False) == EXIT_REFUSED
    assert "the database is in use" in capsys.readouterr().err
    assert _lineage(db, "stranded") is None


def test_s4_live_holder_probe_passes_on_a_closed_database(db, cfg, tmp_path, monkeypatch):
    """...and it is not a blanket refusal: with the store closed, the same probe passes."""
    import scripts.backfill_required_context_lineage as backfill

    _stranded(db)
    db.close()
    backfill._require_no_live_holder(tmp_path / "state.db")  # no Refused


def test_s4_write_refuses_a_row_that_took_a_turn_after_the_plan(db, cfg, tmp_path, monkeypatch, capsys):
    """B2's exact window. The plan says "no persisted prompt → pin"; a turn lands in between and
    persists a prompt with NO block. Pinning anyway would leave the row bricked and the tool would
    have reported it clean, so the write re-reads the prompt in its own transaction and refuses."""
    import scripts.backfill_required_context_lineage as backfill

    _stranded(db)
    real_plan = backfill._plan_row

    def _plan_then_a_turn_lands(*args, **kwargs):
        plan = real_plan(*args, **kwargs)
        db.update_system_prompt("stranded", "You are Hermes.")  # the live desktop, mid-run
        return plan

    monkeypatch.setattr(backfill, "_plan_row", _plan_then_a_turn_lands)
    assert _run_backfill(db, cfg, tmp_path, monkeypatch, apply=True) == backfill.EXIT_ROW_FAILED
    assert "row_took_a_turn_between_the_plan_and_the_write" in capsys.readouterr().out
    assert _lineage(db, "stranded") is None


def test_s4_a_rerun_reports_a_bricked_pinned_row_instead_of_already_pinned(db, cfg, tmp_path, monkeypatch, capsys):
    """The other half of B2: re-running over a row pinned earlier that has since persisted a blockless
    prompt must NOT say ``already_pinned`` and exit 0 — that row fails closed on every turn."""
    _stranded(db)
    assert _run_backfill(db, cfg, tmp_path, monkeypatch, apply=True) == 0
    db.update_system_prompt("stranded", "You are Hermes.")  # the window this tool cannot reach into
    capsys.readouterr()

    assert _run_backfill(db, cfg, tmp_path, monkeypatch, apply=True,
                         backup=tmp_path / "backup2" / "state.db.bak") == EXIT_REFUSED
    assert "pinned_lineage_does_not_match_the_persisted_prompt" in capsys.readouterr().out
    # ...and the runtime really does reject it, which is what the report is about.
    with pytest.raises(RequiredContextError, match="persisted prompt integrity is invalid"):
        initialize_required_context_lineage(_agent(db, "stranded"), config=cfg)


def test_s4_a_healthy_pinned_row_is_still_a_clean_exit(db, cfg, tmp_path, monkeypatch, capsys):
    from agent.required_context import append_required_context

    snapshot = _pinned_parent(db, cfg, session_id="pinned")
    db.update_system_prompt("pinned", append_required_context("You are Hermes.", snapshot))
    before = _lineage(db, "pinned")
    assert _run_backfill(db, cfg, tmp_path, monkeypatch, apply=True) == 0
    assert "already_pinned" in capsys.readouterr().out
    assert _lineage(db, "pinned") == before


def test_s4_leaves_an_already_pinned_row_exactly_as_it_was(db, cfg, tmp_path, monkeypatch):
    snapshot = _pinned_parent(db, cfg, session_id="pinned")
    before = _lineage(db, "pinned")
    assert _run_backfill(db, cfg, tmp_path, monkeypatch, apply=True) == 0
    assert _lineage(db, "pinned") == before
    assert before["prompt_sha256"] == snapshot.prompt_sha256


# ── S4: exit-code and backup hygiene ─────────────────────────────────────────────────────────────
def test_s4_a_config_that_cannot_be_loaded_is_a_refusal_not_a_traceback(db, tmp_path, monkeypatch, capsys):
    """``RequiredContextError`` used to escape ``main`` as an exit-1 traceback, breaking the documented
    ``2 = refusal`` contract. A ``files`` list that drifted from the canonical five is the live shape."""
    import scripts.backfill_required_context_lineage as backfill

    _generation(tmp_path)
    drifted = {"required_context": {"enabled": False, "pointer": str(tmp_path / ".sakaan" / "current.txt"),
                                    "files": FILES[:-1]}}
    monkeypatch.setattr(required_context, "_config", lambda config: drifted if config is None else config)
    assert backfill.main(["--db", str(tmp_path / "state.db")]) == backfill.EXIT_REFUSED
    assert "canonical five-file order" in capsys.readouterr().err


def test_s4_an_unwritable_backup_path_is_a_refusal_not_a_traceback(db, cfg, tmp_path, monkeypatch, capsys):
    """``OSError`` from the backup destination is a refusal too, and nothing is written."""
    _stranded(db)
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"not a directory")
    assert _run_backfill(db, cfg, tmp_path, monkeypatch, apply=True,
                         backup=blocker / "sub" / "state.db.bak") == EXIT_REFUSED
    assert _lineage(db, "stranded") is None


def test_s4_a_failed_backup_leaves_no_debris_to_block_a_retry(db, cfg, tmp_path, monkeypatch, capsys):
    """A backup that failed after creating its file used to leave that file at ``dest``, and every later
    run then refused the path forever ("backup path already exists"). The failing run cleans up after
    itself. Driven through the row-count verification, which runs once the copy has already landed."""
    import scripts.backfill_required_context_lineage as backfill

    _stranded(db)
    backup = tmp_path / "backup" / "state.db.bak"

    with monkeypatch.context() as patch:
        patch.setattr(backfill, "_session_count", lambda _path: 999)
        assert _run_backfill(db, cfg, tmp_path, patch, apply=True, backup=backup) == EXIT_REFUSED
    assert "source had at least 999" in capsys.readouterr().err
    assert not backup.exists()
    assert _lineage(db, "stranded") is None

    # The same path now works, which is the whole point.
    assert _run_backfill(db, cfg, tmp_path, monkeypatch, apply=True, backup=backup) == 0
    assert backup.stat().st_size > 0 and _lineage(db, "stranded") is not None


def test_s4_the_expected_row_count_is_the_callers_pre_copy_reading(db, cfg, tmp_path):
    """Contract test (not a race reproducer): ``_backup`` verifies the copy against a count the CALLER
    read before it, so a session that appears while the copy runs cannot turn into a spurious refusal.
    Reading it inside ``_backup``, after the copy, is what made that window a refusal."""
    import scripts.backfill_required_context_lineage as backfill

    _stranded(db)
    path = tmp_path / "state.db"
    expected = backfill._session_count(path)
    db.create_session("arrived-mid-run", source="desktop", model="claude-opus-5")  # the concurrent insert
    backfill._backup(path, tmp_path / "backup" / "state.db.bak", expected)  # no Refused
    assert backfill._session_count(path) == expected + 1


# ── helpers shared with the guard's own contract ──────────────────────────────────────────────────
def test_new_lineage_metadata_is_v1_and_off_when_disabled(cfg, off_cfg):
    assert new_lineage_metadata(off_cfg) is None
    metadata = new_lineage_metadata(cfg)
    assert metadata["version"] == 1 and "system_prompt_sha256" not in metadata


def test_branch_lineage_metadata_still_refuses_a_lineage_less_parent(db, cfg):
    db.create_session("bare", source="cli", model="claude-opus-5")
    with pytest.raises(RequiredContextError, match="branch parent session has no pinned snapshot"):
        branch_lineage_metadata(db, "bare", config=cfg)


# ── B1: the lineage rides the INSERT and is decided ONLY when there is an INSERT ──────────────────
def _desktop_session(db, monkeypatch, **fields):
    from tui_gateway import server

    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_resolve_model", lambda: "claude-opus-5")
    return {"source": "desktop", **fields}


def test_b1_a_pinned_branch_child_keeps_working_after_its_parent_row_is_deleted(db, ambient, monkeypatch):
    """``_ensure_session_db_row`` runs on EVERY prompt.submit and every dispatch, and ``seeded`` is never
    cleared. Re-deriving a seeded row's lineage there re-read the PARENT row every turn — so deleting the
    original chat turned every later message in a correctly pinned child into a hard 5073, forever. The
    child's own row already carries the inherited lineage; there is nothing left to decide."""
    from tui_gateway import server

    snapshot = _pinned_parent(db, ambient)
    session = _desktop_session(db, monkeypatch, session_key="lazy-child", seeded=True, parent_session_id="parent")
    assert server._ensure_session_db_row(session) is True
    assert _lineage(db, "lazy-child")["prompt_sha256"] == snapshot.prompt_sha256

    db.delete_session("parent")  # the user deletes the conversation they branched from
    for _turn in range(3):
        assert server._ensure_session_db_row(session) is True
    assert _lineage(db, "lazy-child")["prompt_sha256"] == snapshot.prompt_sha256
    initialize_required_context_lineage(_agent(db, "lazy-child"), config=ambient)


def test_b1_an_existing_seeded_row_does_not_reread_the_sakaan_generation(db, ambient, monkeypatch):
    """The parentless half: two full generation reads (5 files, sha256 + base64 of ~49 KB) per live turn,
    every one of which could fail transiently — a pointer repoint mid-install, a file-changed-while-loading
    race — and turn a working chat into 5073. An existing row reads nothing."""
    from tui_gateway import server

    session = _desktop_session(db, monkeypatch, session_key="seeded-solo", seeded=True)
    assert server._ensure_session_db_row(session) is True
    pinned = _lineage(db, "seeded-solo")
    assert pinned is not None

    reads = []
    real = required_context.load_required_context_snapshot
    monkeypatch.setattr(required_context, "load_required_context_snapshot",
                        lambda config=None: (reads.append(1), real(config))[1])
    for _turn in range(3):
        assert server._ensure_session_db_row(session) is True
    assert reads == []  # not two per turn, not one
    assert _lineage(db, "seeded-solo") == pinned


def test_b1_a_seeded_row_that_does_not_exist_yet_is_still_pinned_at_birth(db, ambient, monkeypatch):
    """...and the narrowing must not cost the fix it guards: the FIRST call still pins."""
    from tui_gateway import server

    session = _desktop_session(db, monkeypatch, session_key="fresh-seed", seeded=True)
    assert server._ensure_session_db_row(session) is True
    assert _lineage(db, "fresh-seed")["prompt_sha256"] == load_required_context_snapshot(ambient).prompt_sha256


# ── S1: an ACP fork must stay visible in the listings — guard on OR off ───────────────────────────
def _acp_manager(db, model="claude-opus-5"):
    from acp_adapter.session import SessionManager

    return SessionManager(agent_factory=lambda **_kw: SimpleNamespace(model=model, provider="anthropic"), db=db)


def test_s1_acp_fork_row_carries_the_durable_branch_marker_with_the_guard_on(db, ambient):
    from acp_adapter.session import SessionState

    _pinned_parent(db, ambient, session_id="acp-parent")
    manager = _acp_manager(db)
    manager._precreate_fork_row("acp-fork", SimpleNamespace(session_id="acp-parent", model="claude-opus-5"), "/w")
    assert _row_config(db, "acp-fork")["_branched_from"] == "acp-parent"

    # ...and the save that follows the fork does not drop it again (the update path rebuilds model_config).
    manager._persist(SessionState(session_id="acp-fork", agent=SimpleNamespace(provider="anthropic"), cwd="/w",
                                  model="claude-opus-5", history=[{"role": "user", "content": "hello"}],
                                  parent_session_id="acp-parent"))
    persisted = _row_config(db, "acp-fork")
    assert persisted["_branched_from"] == "acp-parent" and LINEAGE_METADATA_KEY in persisted


def test_s1_acp_fork_stays_listable_after_a_restart_with_the_guard_off(db, off_cfg, monkeypatch):
    """The path R1 claimed was byte-identical while the guard is off. It is not: ``fork_session`` sets
    ``parent_session_id`` unconditionally, so the row is created WITH a parent and no ``_branched_from``
    marker — and ACP never ends the parent as 'branched', so ``_LISTABLE_CHILD_SQL`` drops it and
    ``_ephemeral_child_sql`` re-reads it as a subagent run. The fork looks DELETED after a restart."""
    monkeypatch.setattr(required_context, "_config", lambda config: off_cfg if config is None else config)
    db.create_session("acp-parent", source="acp", model="claude-opus-5", model_config={"cwd": "/w"})
    db.append_message("acp-parent", role="user", content="hello")

    manager = _acp_manager(db)
    fork = manager.fork_session("acp-parent", cwd="/w")
    assert fork is not None and LINEAGE_METADATA_KEY not in _row_config(db, fork.session_id)  # guard off
    assert _row_config(db, fork.session_id)["_branched_from"] == "acp-parent"

    listed = {row["id"] for row in db.list_sessions_rich(source="acp", limit=100)}
    assert fork.session_id in listed
    # Now the restart: memory is gone, the row is restored from the DB and saved again.
    manager._sessions.clear()
    restored = manager.get_session(fork.session_id)
    assert restored is not None and restored.parent_session_id == "acp-parent"
    manager.save_session(fork.session_id)
    assert _row_config(db, fork.session_id)["_branched_from"] == "acp-parent"
    assert fork.session_id in {row["id"] for row in db.list_sessions_rich(source="acp", limit=100)}


def test_s1_a_restored_compression_child_is_not_stamped_as_a_branch(db, off_cfg, monkeypatch):
    """The narrow edge of the restore above: ``parent_session_id`` is also set on compression
    continuations and subagent runs, and stamping one ``_branched_from`` would reclassify it in
    ``_BRANCH_CHILD_SQL``. The marker is read from model_config, never from the column."""
    monkeypatch.setattr(required_context, "_config", lambda config: off_cfg if config is None else config)
    db.create_session("acp-root", source="acp", model="claude-opus-5", model_config={"cwd": "/w"})
    db.append_message("acp-root", role="user", content="hello")
    db.end_session("acp-root", "compression")
    db.create_session("acp-cont", source="acp", model="claude-opus-5", model_config={"cwd": "/w"},
                      parent_session_id="acp-root")
    db.append_message("acp-cont", role="user", content="continued")

    manager = _acp_manager(db)
    restored = manager.get_session("acp-cont")
    assert restored is not None and restored.parent_session_id is None
    manager.save_session("acp-cont")
    assert "_branched_from" not in _row_config(db, "acp-cont")


# ── S3: the changed-but-untested sites ───────────────────────────────────────────────────────────
def test_s3_prompt_submit_reports_a_lineage_refusal_as_5073_not_a_disk_error(db, ambient, monkeypatch):
    """``_persist_session_row_for_submit`` mapped every exception through ``describe_storage_failure``,
    which would have told the user to go and free disk space for a Sakaan lineage problem."""
    from tui_gateway import server

    db.create_session("bare-parent", source="desktop", model="claude-opus-5")
    db.append_message("bare-parent", role="user", content="hello")
    session = _desktop_session(db, monkeypatch, session_key="child", seeded=True,
                               parent_session_id="bare-parent", history=[{"role": "user", "content": "hi"}],
                               history_lock=__import__("threading").Lock())

    error = server._persist_session_row_for_submit("rid-1", session)
    assert error is not None and error["error"]["code"] == 5073
    message = error["error"]["message"]
    assert "Sakaan instruction snapshot" in message and "no pinned snapshot" in message
    assert db.get_session("child") is None


def test_s3_seed_row_rolls_its_row_back_and_fails_closed(db, ambient, monkeypatch):
    """``_seed_row`` (the parentless seeded create) must not leave a committed row that already holds the
    seed and no lineage — the first prompt would then resume a row that can never be pinned."""
    from tui_gateway import server

    record = _desktop_session(db, monkeypatch, session_key="seed-fail", seeded=True,
                              history=[{"role": "user", "content": "hi"}],
                              history_lock=__import__("threading").Lock())
    monkeypatch.setattr(server, "_session_db", lambda _record: contextlib.nullcontext(db))
    monkeypatch.setattr(required_context, "new_lineage_metadata",
                        _raise(RequiredContextError("WORKFLOW_SOURCE_UNAVAILABLE: pointer is gone")))
    with pytest.raises(RequiredContextError, match="pointer is gone"):
        server._seed_row(record)
    assert db.get_session("seed-fail") is None


def test_s3_session_create_maps_a_parentless_seed_refusal_to_5008(db, ambient, monkeypatch):
    """The wire contract: ``session.create`` answers 5008 and drops the in-memory session, rather than
    handing the first prompt a live session whose seeded row can never carry a lineage."""
    from tui_gateway import server

    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_schedule_agent_build", lambda _sid: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)
    monkeypatch.setattr(server, "_register_session_cwd", lambda _session: None)
    monkeypatch.setattr(required_context, "new_lineage_metadata",
                        _raise(RequiredContextError("WORKFLOW_SOURCE_UNAVAILABLE: pointer is gone")))
    before = set(server._sessions)
    response = server.handle_request({"id": "c1", "method": "session.create", "params": {
        "cols": 96, "source": "desktop", "messages": [{"role": "user", "content": "hi"}]}})
    assert response["error"]["code"] == 5008
    assert "session failed" in response["error"]["message"]
    assert set(server._sessions) == before  # no live session left behind


def test_s3_legacy_meta_update_fallback_still_carries_the_pinned_lineage(db, ambient, monkeypatch):
    """``_persist_live_session_runtime``'s pre-``preserve_keys`` fallback replaces model_config wholesale;
    without carrying the row's lineage forward, one runtime persist strands the session."""
    from tui_gateway import server

    snapshot = _pinned_parent(db, ambient, session_id="legacy")
    calls = []

    class _LegacyStore:
        """A store too old for update_session_meta_preserving_keys."""

        def get_session(self, session_id):
            return db.get_session(session_id)

        def update_session_meta(self, session_id, model_config_json, model=None):
            calls.append(json.loads(model_config_json))
            db.update_session_meta(session_id, model_config_json, model)

    server._persist_live_session_runtime({
        "session_key": "legacy", "model": "claude-opus-5",
        "agent": SimpleNamespace(model="claude-opus-5", provider="anthropic", _session_db=_LegacyStore())})

    assert calls and calls[0][LINEAGE_METADATA_KEY]["prompt_sha256"] == snapshot.prompt_sha256
    assert _lineage(db, "legacy")["prompt_sha256"] == snapshot.prompt_sha256
    initialize_required_context_lineage(_agent(db, "legacy"), config=ambient)


def test_s3_gateway_turn_sync_keeps_the_lineage_through_the_real_helper(db, ambient):
    """R15's runtime-save race in its Bot-Mode form, driven through ``_sync_session_model_from_agent``
    itself rather than the store call it makes: the agent pins its lineage between that helper's read
    and its write (``persist_agent_lineage`` on the first turn), and the write must not erase it."""
    from gateway.run_turn import GatewayTurnMixin

    db.create_session("bot-3", source="telegram", model="claude-opus-5", model_config={"model": "claude-opus-5"})
    runner = SimpleNamespace(_session_db=SimpleNamespace(_db=db))
    agent = SimpleNamespace(model="claude-opus-5", provider="anthropic", _fallback_activated=False)

    real_get_session, metadata = db.get_session, {}

    def _pin_between_the_read_and_the_write(session_id, *args, **kwargs):
        row = real_get_session(session_id, *args, **kwargs)
        if session_id == "bot-3" and not metadata:
            metadata.update(required_context.snapshot_to_metadata(load_required_context_snapshot(ambient)))
            db.set_session_model_config_key_if_absent("bot-3", LINEAGE_METADATA_KEY, dict(metadata))
        return row

    db.get_session = _pin_between_the_read_and_the_write
    try:
        GatewayTurnMixin._sync_session_model_from_agent(runner, "bot-3", agent)
    finally:
        db.get_session = real_get_session

    persisted = _row_config(db, "bot-3")
    # The sync still does its job...
    assert persisted["gateway_runtime"] == {"provider": "anthropic", "fallback_active": False}
    assert persisted[LINEAGE_METADATA_KEY] == metadata  # without eating the lineage
    initialize_required_context_lineage(_agent(db, "bot-3"), config=ambient)


# ── N1: a secondary profile whose config.yaml is torn reads as REFUSE, never as "guard off" ──────
def test_n1_torn_profile_yaml_refuses_instead_of_silently_disabling_the_guard(tmp_path, monkeypatch):
    """The R1 UNKNOWN the reviewer resolved, nailed down. ``get_active_config_parse_failure`` is keyed on
    ``get_config_path()``, which honours the context-local ``HERMES_HOME`` override, so the failure record
    written while loading the SECONDARY profile is the one ``_config`` reads back — a fail-CLOSED refusal.
    A path-insensitive or process-global check would have answered "no failure" and read the guard as off,
    which is the fail-open this test exists to exclude."""
    _generation(tmp_path)
    launch = _profile_home(tmp_path, "launch", enabled=True)
    monkeypatch.setenv("HERMES_HOME", str(launch))
    torn = tmp_path / "profiles" / "torn"
    torn.mkdir(parents=True)
    (torn / "config.yaml").write_text("required_context:\n  enabled: true\n  files: [\n", encoding="utf-8")

    # The launch profile is readable and has the guard ON, so a leaked/global answer would be "enabled".
    assert required_context.required_context_enabled(config_for_profile_home(launch)) is True
    with pytest.raises(RequiredContextError, match="required_context configuration is unavailable"):
        config_for_profile_home(torn)
    # ...and the override did not leak: the launch profile still answers for itself afterwards.
    assert required_context.required_context_enabled(config_for_profile_home(None)) is True
