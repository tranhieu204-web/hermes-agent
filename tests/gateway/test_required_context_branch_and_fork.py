"""Required-context lineage on the two gateway branch/fork sites that create a row WITH history.

Both copy the parent's transcript into the new row, so neither can ever be pinned by the runtime's
pristine pin: the lineage has to be INHERITED in the same INSERT, or the child fails closed on its
first turn. And both must read the SESSION's profile config, not the launch process's — a multiplexed
gateway serves several profiles from one process (S2/F2).

Real ``SessionDB``/``SessionStore``, real handlers; the only stubs are the agent-free runner scaffolds
the two harnesses already use elsewhere in this directory.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import agent.required_context as required_context
from agent.required_context import (
    LINEAGE_METADATA_KEY, load_required_context_snapshot, snapshot_to_metadata,
)
from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource, SessionStore
from hermes_state import AsyncSessionDB, SessionDB

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
    (tmp_path / ".sakaan" / "current.txt").write_text(str(generation), encoding="utf-8")
    return generation


def _profile_home(tmp_path, name, *, enabled):
    home = tmp_path / "profiles" / name
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(
        "required_context:\n"
        f"  enabled: {'true' if enabled else 'false'}\n"
        f"  pointer: {tmp_path / '.sakaan' / 'current.txt'}\n"
        "  files:\n" + "".join(f"    - {item}\n" for item in FILES),
        encoding="utf-8")
    return home


def _launch_profile_without_the_guard(tmp_path, monkeypatch):
    """A launch profile with NO required_context at all: a bare ``load_config_readonly()`` therefore
    reads the guard as OFF, so anything that still refuses proves it read the session's profile."""
    launch = tmp_path / "launch"
    launch.mkdir()
    (launch / "config.yaml").write_text("model:\n  default: claude-opus-5\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(launch))
    return launch


def _cfg(tmp_path):
    return {"required_context": {
        "enabled": True, "pointer": str(tmp_path / ".sakaan" / "current.txt"), "files": FILES}}


def _row_config(db, session_id):
    return json.loads(db.get_session(session_id)["model_config"] or "{}")


# ── /branch (gateway/slash_commands_session.py) ───────────────────────────────────────────────────
def _source() -> SessionSource:
    return SessionSource(platform=Platform.TELEGRAM, user_id="42", chat_id="42", chat_type="dm")


def _branch_runner(store: SessionStore, profile_home):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner.config = {}
    runner._background_tasks = set()
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._busy_ack_ts = {}
    runner._pending_approvals = {}
    runner._update_prompt_pending = {}
    runner._agent_cache_lock = None
    runner.session_store = store
    runner._session_db = AsyncSessionDB(store._db)
    runner._pending_skills_reload_notes = {}
    runner._resolve_profile_home_for_source = lambda _source: profile_home
    return runner


@pytest.fixture()
def store(tmp_path, monkeypatch):
    import hermes_state

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    return SessionStore(sessions_dir=tmp_path, config=GatewayConfig())


@pytest.mark.asyncio
async def test_branch_child_inherits_the_parents_lineage_in_its_insert(store, tmp_path, monkeypatch):
    _generation(tmp_path)
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(required_context, "_config", lambda config: cfg if config is None else config)

    source = _source()
    parent = store.get_or_create_session(source)
    snapshot = load_required_context_snapshot(cfg)
    assert store._db.set_session_model_config_key_if_absent(
        parent.session_id, LINEAGE_METADATA_KEY, snapshot_to_metadata(snapshot)) is not None
    store._db.append_message(parent.session_id, role="user", content="hello")
    store._db.append_message(parent.session_id, role="assistant", content="world")

    runner = _branch_runner(store, None)
    reply = await runner._handle_branch_command(MessageEvent(text="/branch", source=source, message_id="m1"))
    assert "branch" in reply.lower()

    child = store.get_or_create_session(source).session_id
    assert child != parent.session_id
    persisted = _row_config(store._db, child)
    assert persisted["_branched_from"] == parent.session_id
    assert persisted[LINEAGE_METADATA_KEY]["version"] == 1  # v1: the child rebuilds its own prompt
    assert persisted[LINEAGE_METADATA_KEY]["prompt_sha256"] == snapshot.prompt_sha256
    # The copied history really is there, which is why a fresh pin would have been a lie.
    assert len(store._db.get_messages_as_conversation(child)) == 2


@pytest.mark.asyncio
async def test_branch_of_a_lineage_less_parent_is_refused_and_creates_no_row(store, tmp_path, monkeypatch):
    _generation(tmp_path)
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(required_context, "_config", lambda config: cfg if config is None else config)

    source = _source()
    parent = store.get_or_create_session(source)
    # A routing row created TODAY is pinned at creation (``_create_session_row``), so this is the
    # legacy shape: a row that predates that fix and carries no lineage of its own.
    store._db.patch_session_model_config(parent.session_id, {LINEAGE_METADATA_KEY: None})
    assert LINEAGE_METADATA_KEY not in _row_config(store._db, parent.session_id)
    store._db.append_message(parent.session_id, role="user", content="hello")
    before = {row["id"] for row in store._db.list_sessions_rich(limit=100, include_children=True)}

    runner = _branch_runner(store, None)
    reply = await runner._handle_branch_command(MessageEvent(text="/branch", source=source, message_id="m1"))
    assert "no pinned snapshot" in reply
    assert {row["id"] for row in store._db.list_sessions_rich(limit=100, include_children=True)} == before
    # The user is still on the parent — /branch did not half-switch them.
    assert store.get_or_create_session(source).session_id == parent.session_id


@pytest.mark.asyncio
async def test_branch_reads_the_sources_profile_not_the_launch_profile(store, tmp_path, monkeypatch):
    """S2 at the ``/branch`` site: the launch profile has no ``required_context`` at all, so a bare
    config read says "off" and would have branched a lineage-less parent happily. The source's profile
    has it ON, so the same branch is refused."""
    _generation(tmp_path)
    _launch_profile_without_the_guard(tmp_path, monkeypatch)

    source = _source()
    parent = store.get_or_create_session(source)
    store._db.append_message(parent.session_id, role="user", content="hello")

    off = _branch_runner(store, _profile_home(tmp_path, "off", enabled=False))
    assert "no pinned snapshot" not in await off._handle_branch_command(
        MessageEvent(text="/branch", source=source, message_id="m1"))

    source2 = SessionSource(platform=Platform.TELEGRAM, user_id="99", chat_id="99", chat_type="dm")
    parent2 = store.get_or_create_session(source2)
    store._db.append_message(parent2.session_id, role="user", content="hello")
    on = _branch_runner(store, _profile_home(tmp_path, "on", enabled=True))
    assert "no pinned snapshot" in await on._handle_branch_command(
        MessageEvent(text="/branch", source=source2, message_id="m2"))
    assert store.get_or_create_session(source2).session_id == parent2.session_id


# ── POST /api/sessions/{id}/fork (gateway/platforms/api_server.py) ────────────────────────────────
class _FakeRequest:
    """Enough of ``web.Request`` for ``_handle_fork_session``: match_info, headers and a JSON body."""

    def __init__(self, session_id: str, body=None):
        self.match_info = {"session_id": session_id}
        self.headers = {}
        self.query = {}
        self._body = body if body is not None else {}

    async def json(self):
        return self._body


class _FakeResponse:
    def __init__(self, payload, status):
        self.status, self.text = status, json.dumps(payload)


class _WebShim:
    """``aiohttp`` is not installed in every test environment (it is optional for the gateway and
    absent here), and ``api_server.web`` is then ``None``. Only ``json_response`` is needed to drive
    the real handler, so the responses are shimmed rather than the handler skipped."""

    @staticmethod
    def json_response(payload, status=200, headers=None):
        return _FakeResponse(payload, status)


@pytest.fixture(autouse=True)
def _web_responses(monkeypatch):
    from gateway.platforms import api_server

    if api_server.web is None:
        monkeypatch.setattr(api_server, "web", _WebShim)


def _api_adapter(db):
    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter

    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={}))
    adapter._session_db = db
    return adapter


async def _fork(adapter, source_id, fork_id):
    response = await adapter._handle_fork_session(_FakeRequest(source_id, {"id": fork_id}))
    return response.status, json.loads(response.text)


@pytest.fixture()
def db(tmp_path):
    store = SessionDB(db_path=tmp_path / "state.db")
    yield store
    store.close()


@pytest.mark.asyncio
async def test_api_fork_inherits_the_sources_lineage(db, tmp_path, monkeypatch):
    _generation(tmp_path)
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(required_context, "_config", lambda config: cfg if config is None else config)

    snapshot = load_required_context_snapshot(cfg)
    db.create_session("api-parent", source="api_server", model="claude-opus-5",
                      model_config={LINEAGE_METADATA_KEY: snapshot_to_metadata(snapshot)})
    db.append_message("api-parent", role="user", content="hello")

    status, _payload = await _fork(_api_adapter(db), "api-parent", "api-fork")
    assert status == 201
    persisted = _row_config(db, "api-fork")
    assert persisted["_branched_from"] == "api-parent"
    assert persisted[LINEAGE_METADATA_KEY]["prompt_sha256"] == snapshot.prompt_sha256


@pytest.mark.asyncio
async def test_api_fork_of_a_lineage_less_source_is_a_409_not_a_mispinned_row(db, tmp_path, monkeypatch):
    _generation(tmp_path)
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(required_context, "_config", lambda config: cfg if config is None else config)
    db.create_session("api-parent", source="api_server", model="claude-opus-5")
    db.append_message("api-parent", role="user", content="hello")

    status, payload = await _fork(_api_adapter(db), "api-parent", "api-fork")
    assert status == 409 and payload["error"]["code"] == "lineage_unavailable"
    assert db.get_session("api-fork") is None
    assert db.get_session("api-parent")["end_reason"] is None  # the source was not ended either


@pytest.mark.asyncio
async def test_api_fork_reads_the_requests_profile_not_the_launch_profile(db, tmp_path, monkeypatch):
    """S2 at the api_server site. The launch profile has no ``required_context``, so a bare read says
    "off"; the request's ``/p/<profile>/`` scope has it ON, and the fork is refused accordingly."""
    from gateway.platforms import api_server

    _generation(tmp_path)
    _launch_profile_without_the_guard(tmp_path, monkeypatch)
    db.create_session("api-parent", source="api_server", model="claude-opus-5")
    db.append_message("api-parent", role="user", content="hello")
    adapter = _api_adapter(db)

    # No scope: the launch profile decides, the guard is off, the fork goes through unpinned.
    status, _payload = await _fork(adapter, "api-parent", "fork-launch")
    assert status == 201 and LINEAGE_METADATA_KEY not in _row_config(db, "fork-launch")

    on = _profile_home(tmp_path, "on", enabled=True)
    monkeypatch.setattr(api_server.APIServerAdapter, "_request_profile_home", staticmethod(lambda: on))
    db.create_session("api-parent-2", source="api_server", model="claude-opus-5")
    db.append_message("api-parent-2", role="user", content="hello")
    status, payload = await _fork(adapter, "api-parent-2", "fork-profile")
    assert status == 409 and payload["error"]["code"] == "lineage_unavailable"
    assert db.get_session("fork-profile") is None


def test_api_request_profile_home_is_none_outside_a_profile_scope():
    from gateway.platforms.api_server import APIServerAdapter

    assert APIServerAdapter._request_profile_home() is None


def test_api_request_profile_home_resolves_the_scoped_profile(monkeypatch, tmp_path):
    from gateway.platforms import api_server

    token = api_server._api_request_profile.set("worker")
    try:
        monkeypatch.setattr("hermes_cli.profiles.get_profile_dir", lambda name: tmp_path / "profiles" / name)
        assert api_server.APIServerAdapter._request_profile_home() == Path(tmp_path / "profiles" / "worker")
    finally:
        api_server._api_request_profile.reset(token)
