"""Regression tests for CLI /retry history replacement semantics."""

from tests.cli.test_cli_init import _make_cli


def test_retry_last_truncates_history_before_requeueing_message():
    cli = _make_cli()
    cli.conversation_history = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "one"},
        {"role": "user", "content": "retry me"},
        {"role": "assistant", "content": "old answer"},
    ]

    retry_msg = cli.retry_last()

    assert retry_msg == "retry me"
    assert cli.conversation_history == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "one"},
    ]

    cli.conversation_history.append({"role": "user", "content": retry_msg})
    cli.conversation_history.append({"role": "assistant", "content": "new answer"})

    assert [m["content"] for m in cli.conversation_history if m["role"] == "user"] == [
        "first",
        "retry me",
    ]


def test_process_command_retry_requeues_original_message_not_retry_command():
    cli = _make_cli()
    queued = []

    class _Queue:
        def put(self, value):
            queued.append(value)

    cli._pending_input = _Queue()
    cli.conversation_history = [
        {"role": "user", "content": "retry me"},
        {"role": "assistant", "content": "old answer"},
    ]

    cli.process_command("/retry")

    assert queued == ["retry me"]
    assert cli.conversation_history == []


def test_cli_retry_converts_array_index_to_user_ordinal_and_archives_suffix_before_requeue(
    tmp_path,
):
    """CLI retry must durably rewind before mutating its in-memory history."""
    from hermes_state import SessionDB

    sid = "cli-retry-red-coordinate"
    history = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "calling tool"},
        {"role": "tool", "content": "tool result"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "retry me"},
        {"role": "assistant", "content": "old answer"},
    ]
    user_indices = [i for i, message in enumerate(history) if message.get("role") == "user"]
    last_user_array_index = user_indices[-1]
    last_user_ordinal = len(user_indices) - 1

    assert user_indices == [0, 4]
    assert last_user_array_index != last_user_ordinal
    assert last_user_array_index >= len(user_indices)

    db = SessionDB(db_path=tmp_path / "cli-retry-red-state.db")
    db.create_session(sid, source="cli")
    for message in history:
        db.append_message(sid, role=message["role"], content=message["content"])

    cli = _make_cli()
    cli._session_db = db
    cli.session_id = sid
    cli.conversation_history = list(history)

    try:
        retry_message = cli.retry_last()
        assert retry_message == "retry me", "wrong coordinate wiring refused the CLI retry"
        assert cli.conversation_history == history[:last_user_array_index]

        all_rows = db.get_messages(sid, include_inactive=True)
        rewound = [
            row
            for row in all_rows
            if row.get("active") == 0 and row.get("compacted") == 0
        ]
        assert [row["content"] for row in rewound] == ["retry me", "old answer"], (
            "base CLI retry changed only memory; its dropped suffix has no durable rewind archive"
        )
        assert [row["content"] for row in db.get_messages(sid)] == [
            message["content"] for message in history[:last_user_array_index]
        ]
    finally:
        db.close()
