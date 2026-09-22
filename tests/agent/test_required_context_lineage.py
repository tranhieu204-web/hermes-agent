import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import agent.required_context as required_context

from agent.required_context import (
    REQUIRED_CONTEXT_BEGIN,
    RequiredContextError,
    adopt_current_generation,
    append_required_context,
    initialize_required_context_lineage,
    load_required_context_snapshot,
    restore_required_context_lineage,
    snapshot_to_metadata,
)
from hermes_cli.cli_commands_mixin import CLICommandsMixin
from hermes_cli.commands import resolve_command


FILES = [
    "sakaan-workflow.md",
    "GRANTS.md",
    "ROUTING.md",
    "procedures/hardened-review.md",
    "Sakaan-crew.md",
]


def _fixture(tmp_path: Path, suffix: str):
    generation = tmp_path / ".sakaan" / "generations" / f"generation-{suffix}"
    generation.mkdir(parents=True)
    for name in FILES:
        target = generation / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"{suffix}:{name}\r\n".encode("utf-8"))
    return generation


@pytest.fixture(autouse=True)
def _canonical_trust_root(tmp_path, monkeypatch):
    root = tmp_path / ".sakaan"
    root.mkdir()
    monkeypatch.setattr(required_context, "_CANONICAL_SAKAAN_ROOT", root)
    monkeypatch.setattr(required_context, "_CANONICAL_POINTER", root / "current.txt")
    monkeypatch.setattr(required_context, "_CANONICAL_GENERATIONS_ROOT", root / "generations")


def _config(pointer: Path):
    return {
        "required_context": {
            "enabled": True,
            "pointer": str(pointer),
            "files": FILES,
        }
    }


def test_raw_bytes_are_frozen_and_pointer_changes_only_affect_new_lineage(tmp_path):
    first = _fixture(tmp_path, "one")
    second = _fixture(tmp_path, "two")
    pointer = tmp_path / ".sakaan" / "current.txt"
    pointer.write_bytes(str(first).encode("utf-8"))
    cfg = _config(pointer)

    snapshot = load_required_context_snapshot(cfg)
    assert b"\r\n" in snapshot.files[0].raw
    frozen = snapshot.prompt_block
    pointer.write_bytes(str(second).encode("utf-8"))

    restored = restore_required_context_lineage(snapshot_to_metadata(snapshot), config=cfg)
    assert restored.prompt_block == frozen
    assert "one:Sakaan-crew.md" in restored.prompt_block
    assert "two:Sakaan-crew.md" not in restored.prompt_block
    assert load_required_context_snapshot(cfg).prompt_block != frozen


def test_existing_lineage_without_snapshot_fails_before_provider_capability(tmp_path):
    generation = _fixture(tmp_path, "one")
    pointer = tmp_path / ".sakaan" / "current.txt"
    pointer.write_text(str(generation), encoding="utf-8")
    cfg = _config(pointer)
    touched = []

    class DB:
        def get_session(self, _session_id):
            return {"system_prompt": "legacy", "model_config": json.dumps({})}

    agent = SimpleNamespace(
        session_id="existing", _session_db=DB(), _session_init_model_config={},
    )
    with pytest.raises(RequiredContextError, match="new lineage"):
        initialize_required_context_lineage(
            agent, config=cfg, before_provider=lambda: touched.append("provider")
        )
    assert touched == []


def test_corrupt_persisted_snapshot_fails_closed(tmp_path):
    generation = _fixture(tmp_path, "one")
    pointer = tmp_path / ".sakaan" / "current.txt"
    pointer.write_text(str(generation), encoding="utf-8")
    cfg = _config(pointer)
    snapshot = load_required_context_snapshot(cfg)
    metadata = snapshot_to_metadata(snapshot)
    metadata["files"][0]["raw_b64"] = "AAAA"
    with pytest.raises(RequiredContextError, match="integrity"):
        restore_required_context_lineage(metadata, config=cfg)


def test_new_lineage_persists_snapshot_and_prompt_has_one_block(tmp_path):
    generation = _fixture(tmp_path, "one")
    pointer = tmp_path / ".sakaan" / "current.txt"
    pointer.write_text(str(generation), encoding="utf-8")
    cfg = _config(pointer)

    class DB:
        def get_session(self, _session_id):
            return None

    agent = SimpleNamespace(session_id="new", _session_db=DB(), _session_init_model_config={})
    initialize_required_context_lineage(agent, config=cfg)
    assert agent._required_context_snapshot.prompt_block.count(REQUIRED_CONTEXT_BEGIN) == 1
    assert "_required_context_lineage" in agent._session_init_model_config


def test_alternate_pointer_and_outside_generation_are_rejected(tmp_path):
    generation = _fixture(tmp_path, "one")
    canonical_pointer = tmp_path / ".sakaan" / "current.txt"
    canonical_pointer.write_text(str(generation), encoding="utf-8")
    alternate = tmp_path / "alternate.txt"
    alternate.write_text(str(generation), encoding="utf-8")
    with pytest.raises(RequiredContextError, match="canonical Sakaan current"):
        load_required_context_snapshot(_config(alternate))

    outside = tmp_path / "outside-generation"
    outside.mkdir()
    canonical_pointer.write_text(str(outside), encoding="utf-8")
    with pytest.raises(RequiredContextError, match="direct child"):
        load_required_context_snapshot(_config(canonical_pointer))


def test_reparse_in_generation_ancestor_is_rejected(tmp_path, monkeypatch):
    generation = _fixture(tmp_path, "one")
    pointer = tmp_path / ".sakaan" / "current.txt"
    pointer.write_text(str(generation), encoding="utf-8")
    original = required_context._reject_reparse

    def reject_generations(path):
        if Path(path).name == "generations":
            raise RequiredContextError("WORKFLOW_SOURCE_UNAVAILABLE: synthetic reparse ancestor")
        return original(path)

    monkeypatch.setattr(required_context, "_reject_reparse", reject_generations)
    with pytest.raises(RequiredContextError, match="reparse ancestor"):
        load_required_context_snapshot(_config(pointer))


def test_explicit_migration_creates_child_and_never_rewrites_old_lineage(tmp_path):
    first = _fixture(tmp_path, "one")
    second = _fixture(tmp_path, "two")
    pointer = tmp_path / ".sakaan" / "current.txt"
    pointer.write_text(str(first), encoding="utf-8")
    cfg = _config(pointer)
    original = load_required_context_snapshot(cfg)
    original_prompt = f"STATIC\n\n{original.prompt_block}"

    class DB:
        def __init__(self):
            self.rows = {
                "old": {
                    "system_prompt": original_prompt,
                    "model_config": json.dumps({
                        "_required_context_lineage": snapshot_to_metadata(original),
                    }),
                }
            }

        def get_session(self, session_id):
            return self.rows.get(session_id)

        def create_session(self, session_id, **kwargs):
            self.rows[session_id] = {
                "system_prompt": kwargs["system_prompt"],
                "model_config": json.dumps(kwargs["model_config"]),
                "parent_session_id": kwargs["parent_session_id"],
            }

    db = DB()
    old_client = object()
    old_permit = object()
    agent = SimpleNamespace(
        session_id="old", _session_db=db, _session_init_model_config={},
        model="synthetic", session_source="test", client=old_client,
        _subscription_route_permit=old_permit,
    )
    initialize_required_context_lineage(agent, config=cfg)
    pointer.write_text(str(second), encoding="utf-8")

    migrated = adopt_current_generation(agent, new_session_id="new", config=cfg)
    assert agent.session_id == "new"
    assert agent._parent_session_id == "old"
    assert agent.client is None
    assert agent._subscription_route_permit is None
    assert db.rows["new"]["parent_session_id"] == "old"
    assert "two:Sakaan-crew.md" in migrated.prompt_block
    assert db.rows["old"]["system_prompt"] == original_prompt
    assert db.rows["new"]["system_prompt"] == f"STATIC\n\n{migrated.prompt_block}"
    assert db.rows["new"]["system_prompt"].count(REQUIRED_CONTEXT_BEGIN) == 1

    # A restarted/manual resume of either lineage restores its own immutable
    # bytes even after the pointer changes again. Compression-style rebuilding
    # appends the same frozen block exactly once.
    old_restored = restore_required_context_lineage(
        json.loads(db.rows["old"]["model_config"])["_required_context_lineage"], config=cfg,
    )
    new_restored = restore_required_context_lineage(
        json.loads(db.rows["new"]["model_config"])["_required_context_lineage"], config=cfg,
    )
    assert old_restored.prompt_block == original.prompt_block
    assert new_restored.prompt_block == migrated.prompt_block
    assert append_required_context("COMPRESSED", new_restored).count(REQUIRED_CONTEXT_BEGIN) == 1

    restarted = SimpleNamespace(
        session_id="new", _session_db=db, _session_init_model_config={},
    )
    initialize_required_context_lineage(restarted, config=cfg)
    assert restarted._required_context_snapshot.prompt_block == migrated.prompt_block

    db.rows["new"]["system_prompt"] += "tamper"
    with pytest.raises(RequiredContextError, match="full prompt integrity"):
        initialize_required_context_lineage(restarted, config=cfg)


def test_public_cli_adopt_generation_command_uses_shared_migration_boundary(tmp_path, monkeypatch):
    first = _fixture(tmp_path, "one")
    second = _fixture(tmp_path, "two")
    pointer = tmp_path / ".sakaan" / "current.txt"
    pointer.write_text(str(first), encoding="utf-8")
    cfg = _config(pointer)
    original = load_required_context_snapshot(cfg)
    original_prompt = f"STABLE\n\nCONTEXT\n\nVOLATILE\n\n{original.prompt_block}"

    class DB:
        def __init__(self):
            self.rows = {"old": {"system_prompt": original_prompt, "model_config": json.dumps({
                "_required_context_lineage": snapshot_to_metadata(original),
            })}}
            self.ended = []

        def get_session(self, session_id):
            return self.rows.get(session_id)

        def create_session(self, session_id, **kwargs):
            self.rows[session_id] = {
                "system_prompt": kwargs["system_prompt"],
                "model_config": json.dumps(kwargs["model_config"]),
                "parent_session_id": kwargs["parent_session_id"],
            }

        def get_messages_as_conversation(self, _session_id):
            return []

        def append_messages_batch(self, _session_id, _rows):
            raise AssertionError("history copy was not requested")

        def end_session(self, session_id, reason):
            self.ended.append((session_id, reason))

    db = DB()
    agent = SimpleNamespace(
        session_id="old", _session_db=db, _session_init_model_config={}, model="synthetic",
        session_source="cli", client=object(), _subscription_route_permit=object(),
        _flush_messages_to_session_db=lambda *_args, **_kwargs: None,
    )
    initialize_required_context_lineage(agent, config=cfg)
    pointer.write_text(str(second), encoding="utf-8")
    monkeypatch.setattr(required_context, "_config", lambda config=None: cfg)
    output = []
    import cli as cli_module
    monkeypatch.setattr(cli_module, "_cprint", output.append)
    monkeypatch.setattr(cli_module, "_sync_process_session_id", lambda _sid: None)
    cli = SimpleNamespace(
        agent=agent, _session_db=db, session_id="old", conversation_history=[],
        _pending_title=None, _resumed=False,
    )

    assert resolve_command("adopt-generation").name == "adopt-generation"
    CLICommandsMixin._handle_adopt_generation_command(cli, "/adopt-generation")

    assert cli.session_id != "old"
    assert agent.session_id == cli.session_id
    assert db.ended == [("old", "generation_adopted")]
    assert db.rows[cli.session_id]["parent_session_id"] == "old"
    assert db.rows[cli.session_id]["system_prompt"].startswith("STABLE\n\nCONTEXT\n\nVOLATILE")
    assert "two:Sakaan-crew.md" in db.rows[cli.session_id]["system_prompt"]
    assert agent.client is None
    assert output and "Adopted Sakaan generation" in output[0]


def test_public_cli_copy_history_uses_same_sanitized_rows_in_memory_and_database(tmp_path, monkeypatch):
    first = _fixture(tmp_path, "one")
    second = _fixture(tmp_path, "two")
    pointer = tmp_path / ".sakaan" / "current.txt"
    pointer.write_text(str(first), encoding="utf-8")
    cfg = _config(pointer)
    original = load_required_context_snapshot(cfg)
    original_prompt = f"STATIC\n\n{original.prompt_block}"
    old_history = [
        {"role": "system", "content": "internal", "sidecar": "secret"},
        {"role": "user", "content": "question", "sidecar": "drop"},
        {"role": "tool", "content": "tool-output", "tool_call_id": "drop"},
        {"role": "assistant", "content": "answer", "provider_state": "drop"},
        {"role": "developer", "content": "internal"},
    ]

    class DB:
        def __init__(self):
            self.rows = {"old": {"system_prompt": original_prompt, "model_config": json.dumps({
                "_required_context_lineage": snapshot_to_metadata(original),
            })}}
            self.messages = {}

        def get_session(self, session_id):
            return self.rows.get(session_id)

        def create_session(self, session_id, **kwargs):
            self.rows[session_id] = {
                "system_prompt": kwargs["system_prompt"],
                "model_config": json.dumps(kwargs["model_config"]),
                "parent_session_id": kwargs["parent_session_id"],
            }

        def get_messages_as_conversation(self, _session_id):
            raise AssertionError("CLI must pass its single sanitized in-memory list")

        def append_messages_batch(self, session_id, rows):
            self.messages[session_id] = list(rows)

        def end_session(self, _session_id, _reason):
            return None

    db = DB()
    agent = SimpleNamespace(
        session_id="old", _session_db=db, _session_init_model_config={}, model="synthetic",
        session_source="cli", client=object(), _subscription_route_permit=object(),
        _flush_messages_to_session_db=lambda *_args, **_kwargs: None,
    )
    initialize_required_context_lineage(agent, config=cfg)
    pointer.write_text(str(second), encoding="utf-8")
    monkeypatch.setattr(required_context, "_config", lambda config=None: cfg)
    import cli as cli_module
    monkeypatch.setattr(cli_module, "_cprint", lambda *_args: None)
    monkeypatch.setattr(cli_module, "_sync_process_session_id", lambda _sid: None)
    cli = SimpleNamespace(
        agent=agent, _session_db=db, session_id="old", conversation_history=old_history,
        _pending_title=None, _resumed=False,
    )

    CLICommandsMixin._handle_adopt_generation_command(cli, "/adopt-generation --copy-history")

    expected = [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
    ]
    assert cli.conversation_history == expected
    assert cli._resume_display_history == expected
    assert db.messages[cli.session_id] == expected
    assert all(set(row) == {"role", "content"} for row in cli.conversation_history)
    assert agent.client is None
    assert agent._subscription_route_permit is None
