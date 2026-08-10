import json
import sys

import pytest

from agent.memory_manager import sanitize_context
from hermes_state import SessionDB


def _seed_rewound_session(db_path, *, session_id="rewind-export-session"):
    raw_sentinel = "\n  P3-RAW-SENTINEL-8f6a  \t"
    compacted_only = "P3-COMPACTED-ROW-MUST-STAY-EXCLUDED"
    db = SessionDB(db_path)
    db.create_session(session_id, "cli")
    db.append_message(session_id, "user", compacted_only)
    db.archive_and_compact(
        session_id, [{"role": "user", "content": "retained prefix"}]
    )
    db.append_message(session_id, "assistant", "retained reply")
    db.append_message(session_id, "user", raw_sentinel)
    db.append_message(session_id, "assistant", "rewound tail")
    expected_history = db.get_messages(session_id)
    db.rewind_active_history(
        session_id,
        expected_history=expected_history,
        truncate_before_user_ordinal=1,
    )
    db.close()
    return session_id, raw_sentinel, compacted_only


def _run_sessions_export(monkeypatch, argv):
    import hermes_cli.main as main_mod

    monkeypatch.setattr(sys, "argv", ["hermes", "sessions", "export", *argv])
    main_mod.main()


def test_include_rewound_jsonl_exports_raw_bytes_without_conversation_projection(
    monkeypatch, tmp_path, capsys
):
    import hermes_state

    db_path = tmp_path / "state.db"
    output_path = tmp_path / "recovery.jsonl"
    session_id, raw_sentinel, compacted_only = _seed_rewound_session(db_path)
    assert sanitize_context(raw_sentinel).strip() != raw_sentinel

    real_session_db = hermes_state.SessionDB

    def open_isolated_db():
        db = real_session_db(db_path)

        def forbid_projection(*args, **kwargs):
            raise AssertionError("recovery must not use conversation projection")

        db.get_messages_as_conversation = forbid_projection
        return db

    monkeypatch.setattr(hermes_state, "SessionDB", open_isolated_db)
    _run_sessions_export(
        monkeypatch,
        [
            "--include-rewound",
            "--session-id",
            session_id,
            "--format",
            "jsonl",
            str(output_path),
        ],
    )

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    messages = payload["messages"]
    sentinel_rows = [row for row in messages if row["content"] == raw_sentinel]
    assert len(sentinel_rows) == 1
    assert sentinel_rows[0]["content"].encode("utf-8") == raw_sentinel.encode("utf-8")
    assert (sentinel_rows[0]["active"], sentinel_rows[0]["compacted"]) == (0, 0)
    assert [row["content"] for row in messages].count("retained prefix") == 1
    assert compacted_only not in [row["content"] for row in messages]
    assert "Exported 1 session" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("extra_args", "expected_error"),
    [
        ([], "--include-rewound requires --session-id"),
        (["--session-id", "s1", "--format", "md"], "--include-rewound requires --format jsonl"),
        (
            ["--session-id", "s1", "--only", "user-prompts"],
            "--include-rewound is incompatible with --only",
        ),
        (
            ["--session-id", "s1", "--lineage", "logical"],
            "--include-rewound is incompatible with --lineage logical",
        ),
        (
            ["--session-id", "s1", "--delete-after-verified", "--yes"],
            "--include-rewound is incompatible with --delete-after-verified",
        ),
        (
            ["--session-id", "s1", "--redact"],
            "--include-rewound is incompatible with --redact",
        ),
    ],
)
def test_include_rewound_scope_fence_rejects_broad_or_destructive_combinations(
    monkeypatch, tmp_path, capsys, extra_args, expected_error
):
    import hermes_state

    class RefusalDB:
        def __getattr__(self, name):
            if name == "close":
                return lambda: None
            raise AssertionError(f"scope refusal reached database operation: {name}")

    monkeypatch.setattr(hermes_state, "SessionDB", RefusalDB)
    _run_sessions_export(
        monkeypatch,
        ["--include-rewound", *extra_args, str(tmp_path / "refused.jsonl")],
    )

    output = capsys.readouterr().out
    assert expected_error in output
    assert not (tmp_path / "refused.jsonl").exists()


def test_include_rewound_resolves_exactly_one_session_and_requests_i4(
    monkeypatch, tmp_path
):
    import hermes_state

    captured = []
    output_path = tmp_path / "one.jsonl"

    class RecordingDB:
        def resolve_session_id(self, session_id):
            captured.append(("resolve", session_id))
            return "resolved-session"

        def export_session(self, session_id, **kwargs):
            captured.append(("export", session_id, kwargs))
            return {"id": session_id, "messages": []}

        def export_all(self, *args, **kwargs):
            raise AssertionError("recovery export must not aggregate sessions")

        def list_prune_candidates(self, *args, **kwargs):
            raise AssertionError("recovery export must not enumerate sessions")

        def close(self):
            captured.append(("close",))

    monkeypatch.setattr(hermes_state, "SessionDB", RecordingDB)
    _run_sessions_export(
        monkeypatch,
        [
            "--include-rewound",
            "--session-id",
            "resolved",
            "--format",
            "jsonl",
            str(output_path),
        ],
    )

    assert captured == [
        ("resolve", "resolved"),
        ("export", "resolved-session", {"include_rewound": True}),
    ]
    assert json.loads(output_path.read_text(encoding="utf-8"))["id"] == "resolved-session"


def test_default_jsonl_export_keeps_existing_active_only_call_contract(
    monkeypatch, tmp_path
):
    import hermes_state

    captured = []
    output_path = tmp_path / "default.jsonl"

    class DefaultPathDB:
        def resolve_session_id(self, session_id):
            captured.append(("resolve", session_id))
            return "s1"

        def export_session(self, session_id):
            captured.append(("export", session_id))
            return {
                "id": session_id,
                "messages": [{"role": "user", "content": "active only"}],
            }

        def close(self):
            captured.append(("close",))

    monkeypatch.setattr(hermes_state, "SessionDB", DefaultPathDB)
    _run_sessions_export(
        monkeypatch,
        ["--session-id", "s1", "--format", "jsonl", str(output_path)],
    )

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert [message["content"] for message in payload["messages"]] == ["active only"]
    assert captured == [("resolve", "s1"), ("export", "s1")]


def test_default_replay_export_and_search_exclude_rewound_sentinel(tmp_path):
    db_path = tmp_path / "state.db"
    session_id, raw_sentinel, _compacted_only = _seed_rewound_session(db_path)
    db = SessionDB(db_path)

    replay = db.get_messages_as_conversation(session_id)
    default_export = db.export_session(session_id)
    search_hits = db.search_messages("P3-RAW-SENTINEL-8f6a")
    recovery = db.export_session(session_id, include_rewound=True)

    assert raw_sentinel not in [message["content"] for message in replay]
    assert raw_sentinel not in [message["content"] for message in default_export["messages"]]
    assert session_id not in {hit["session_id"] for hit in search_hits}
    assert raw_sentinel in [message["content"] for message in recovery["messages"]]
    db.close()


@pytest.mark.parametrize(
    ("extra_args", "expected_error"),
    [
        (["--session-id", "s1", "--source", "cli"], "session filters"),
        (["--session-id", "s1", "--dry-run"], "--dry-run"),
        (["--session-id", "s1", "--upload"], "trace upload options"),
    ],
)
def test_include_rewound_rejects_other_broad_or_mode_specific_options(
    monkeypatch, tmp_path, capsys, extra_args, expected_error
):
    import hermes_state

    class RefusalDB:
        def __getattr__(self, name):
            if name == "close":
                return lambda: None
            raise AssertionError(f"scope refusal reached database operation: {name}")

    monkeypatch.setattr(hermes_state, "SessionDB", RefusalDB)
    output_path = tmp_path / "refused-extra.jsonl"
    _run_sessions_export(
        monkeypatch, ["--include-rewound", *extra_args, str(output_path)]
    )

    assert expected_error in capsys.readouterr().out
    assert not output_path.exists()
