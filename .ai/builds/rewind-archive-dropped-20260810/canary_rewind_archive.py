#!/usr/bin/env python3
"""Frozen P3 rewind/recovery canary. Disposable roots only."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import types


def _require_fresh_disposable_root(raw_root: str) -> tuple[Path, Path]:
    root = Path(raw_root).expanduser().resolve()
    marker = "p3-canary"
    if marker not in root.name.lower():
        raise RuntimeError(f"refusing non-canary root: {root}")
    if root.exists():
        if any(root.iterdir()):
            raise RuntimeError(f"refusing non-empty canary root: {root}")
        root.rmdir()
    root.mkdir(parents=True)
    home = root / "hermes-home"
    home.mkdir()
    return root, home


def _rewind_id(server, history, user_ordinal: int) -> str:
    index = server._model_user_indices(history)[user_ordinal]
    text = server._coerce_message_text(history[index].get("content"))
    return server._rewind_message_id(
        user_ordinal, server._rewind_prefix_hash(history, index, text)
    )


def _session(agent, history, session_id: str):
    return {
        "agent": agent,
        "session_key": session_id,
        "history": list(history),
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "attached_images": [],
        "image_counter": 0,
        "cols": 80,
        "slash_worker": None,
        "show_reasoning": False,
        "tool_progress_mode": "all",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    args = parser.parse_args()
    root, hermes_home = _require_fresh_disposable_root(args.root)
    os.environ["HERMES_HOME"] = str(hermes_home)

    from hermes_state import SessionDB
    from tui_gateway import server

    state_path = hermes_home / "state.db"
    session_id = "p3-canary-conversation"
    sentinel = "P3-CANARY-SENTINEL-4f9d8c2a"
    inactive_row = "P3-PREEXISTING-INACTIVE-ROW"
    retained_prefix = "P3-RETAINED-PREFIX"

    # Seed a three-turn conversation plus one compaction-inactive row.
    db = SessionDB(state_path)
    db.create_session(session_id, source="tui")
    db.append_message(session_id, "user", inactive_row)
    db.archive_and_compact(
        session_id, [{"role": "user", "content": retained_prefix}]
    )
    db.append_message(session_id, "assistant", "prefix reply")
    db.append_message(session_id, "user", "edit this turn")
    db.append_message(session_id, "assistant", "old edited reply")
    db.append_message(session_id, "user", sentinel)
    db.append_message(session_id, "assistant", "sentinel reply")
    db.close()

    # Frozen step 2: restart/reopen before Restore/Edit/Re-run.
    db = SessionDB(state_path)
    history = db.get_messages_as_conversation(session_id)
    assert len([m for m in history if m["role"] == "user"]) == 3

    class Agent:
        def run_conversation(
            self, prompt, conversation_history=None, stream_callback=None, **_kwargs
        ):
            return {
                "final_response": "edited response",
                "messages": [
                    *(conversation_history or []),
                    {"role": "user", "content": prompt},
                ],
            }

    class ImmediateThread:
        def __init__(self, target=None, daemon=None):
            self._target = target

        def start(self):
            self._target()

    original_thread = server.threading.Thread
    original_get_usage = server._get_usage
    original_render = server.render_message
    original_emit = server._emit
    original_get_db = server._get_db
    server._sessions[session_id] = _session(Agent(), history, session_id)
    server.threading.Thread = ImmediateThread
    server._get_usage = lambda _agent: {}
    server.render_message = lambda _title, _content: ""
    server._emit = lambda *args: None
    server._get_db = lambda: db
    try:
        response = server.handle_request(
            {
                "id": "p3-canary-prompt-submit",
                "method": "prompt.submit",
                "params": {
                    "session_id": session_id,
                    "text": "edited second turn",
                    "truncate_before_message_id": _rewind_id(server, history, 1),
                },
            }
        )
        assert response.get("result"), response
        live_history = server._sessions[session_id]["history"]
        assert sentinel not in [message.get("content") for message in live_history]
    finally:
        server._sessions.pop(session_id, None)
        server.threading.Thread = original_thread
        server._get_usage = original_get_usage
        server.render_message = original_render
        server._emit = original_emit
        server._get_db = original_get_db
        db.close()

    # Restart/read back the backend after the rewind.
    db = SessionDB(state_path)
    replay = db.get_messages_as_conversation(session_id)
    default_export = db.export_session(session_id)
    default_hits = db.search_messages(sentinel)
    all_rows = db.get_messages(session_id, include_inactive=True)
    assert sentinel not in [message["content"] for message in replay]
    assert sentinel not in [message["content"] for message in default_export["messages"]]
    assert session_id not in {hit["session_id"] for hit in default_hits}
    preexisting = [row for row in all_rows if row["content"] == inactive_row]
    assert len(preexisting) == 1
    assert (preexisting[0]["active"], preexisting[0]["compacted"]) == (0, 1)
    db.close()

    # Invoke the supported read-only recovery surface, not a private DB read.
    export_path = root / "recovery.jsonl"
    env = os.environ.copy()
    command = [
        sys.executable,
        "-c",
        "from hermes_cli.main import main; main()",
        "sessions",
        "export",
        "--include-rewound",
        "--session-id",
        session_id,
        "--format",
        "jsonl",
        str(export_path),
    ]
    completed = subprocess.run(
        command,
        cwd=Path(__file__).resolve().parents[3],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    recovery = json.loads(export_path.read_text(encoding="utf-8"))
    recovered_messages = recovery["messages"]
    recovered_sentinel = [m for m in recovered_messages if m["content"] == sentinel]
    assert len(recovered_sentinel) == 1
    assert recovered_sentinel[0]["content"].encode("utf-8") == sentinel.encode("utf-8")
    assert (recovered_sentinel[0]["active"], recovered_sentinel[0]["compacted"]) == (0, 0)
    assert [m["content"] for m in recovered_messages].count(retained_prefix) == 1

    # Non-rewind hard replacement remains destructive, including inactive rows.
    db = SessionDB(state_path)
    hard_session = "p3-canary-hard-replace"
    db.create_session(hard_session, source="cli")
    db.append_message(hard_session, "user", "hard-replace inactive")
    db.archive_and_compact(
        hard_session, [{"role": "user", "content": "hard-replace active"}]
    )
    db.replace_messages(
        hard_session, [{"role": "user", "content": "hard replacement"}]
    )
    hard_rows = db.get_messages(hard_session, include_inactive=True)
    assert [(row["content"], row["active"]) for row in hard_rows] == [
        ("hard replacement", 1)
    ]
    db.close()

    result = {
        "status": "PASS",
        "root": str(root),
        "hermes_home": str(hermes_home),
        "state_db": str(state_path),
        "session_id": session_id,
        "sentinel_utf8_hex": sentinel.encode("utf-8").hex(),
        "recovered_sentinel_count": len(recovered_sentinel),
        "retained_prefix_count": [
            m["content"] for m in recovered_messages
        ].count(retained_prefix),
        "preexisting_inactive_state": [
            preexisting[0]["active"],
            preexisting[0]["compacted"],
        ],
        "default_replay_excludes_sentinel": True,
        "default_export_excludes_sentinel": True,
        "default_search_excludes_sentinel": True,
        "hard_replace_remains_destructive": True,
        "recovery_command": command[2:],
        "recovery_stdout": completed.stdout.strip(),
    }
    result_path = root / "canary-result.json"
    result_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
