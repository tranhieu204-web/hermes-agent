"""A1/A3 refusal oracles for destructive Kanban supervisor actions."""

from __future__ import annotations

import json
import signal
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated board state for lifecycle-safety tests."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _local_claim_lock() -> str:
    host = kb._claimer_id().split(":", 1)[0]
    return f"{host}:safety-test"


def _bind_stale_worker(
    conn,
    task_id: str,
    *,
    pid: int,
    pid_start: int | None,
) -> str:
    lock = _local_claim_lock()
    claimed = kb.claim_task(conn, task_id, claimer=lock)
    assert claimed is not None
    old = int(time.time()) - (5 * 3600)
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET worker_pid = ?, worker_pid_start = ?, "
            "started_at = ?, last_heartbeat_at = NULL WHERE id = ?",
            (pid, pid_start, old, task_id),
        )
        conn.execute(
            "UPDATE task_runs SET worker_pid = ?, worker_pid_start = ?, "
            "started_at = ? WHERE id = ("
            "SELECT current_run_id FROM tasks WHERE id = ?)",
            (pid, pid_start, old, task_id),
        )
    return lock


def test_heartbeat_staleness_never_signals_live_bound_worker(
    kanban_home, monkeypatch
):
    """A1: generic elapsed-time/heartbeat evidence cannot authorize a signal."""
    pid = 424_242
    start = 111
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(kb, "_pid_alive", lambda candidate: candidate == pid)
    monkeypatch.setattr(
        kb, "_process_start_time", lambda candidate: start if candidate == pid else None
    )
    monkeypatch.setattr(kb.time, "sleep", lambda _seconds: None)

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="quiet-live", assignee="worker")
        lock = _bind_stale_worker(conn, task_id, pid=pid, pid_start=start)
        old_heartbeat = int(time.time()) - (5 * 3600)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET last_heartbeat_at = ? WHERE id = ?",
                (old_heartbeat, task_id),
            )
            conn.execute(
                "UPDATE task_runs SET last_heartbeat_at = ? WHERE task_id = ?",
                (old_heartbeat, task_id),
            )
        before_expiry = kb.get_task(conn, task_id).claim_expires

        stale = kb.detect_stale_running(
            conn,
            stale_timeout_seconds=14_400,
            signal_fn=lambda candidate, sig: signals.append((candidate, sig)),
        )

        assert signals == []
        assert stale == []
        task = kb.get_task(conn, task_id)
        assert task.status == "running"
        assert task.claim_lock == lock
        assert task.claim_expires >= before_expiry
        event = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? "
            "AND kind = 'reclaim_deferred' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        assert event is not None
        payload = json.loads(event["payload"])
        assert payload["reason"] == "heartbeat_stale_worker_alive"
        assert payload["termination_attempted"] is False

        monkeypatch.setattr(kb.time, "time", lambda: task.claim_expires + 1)
        assert kb.release_stale_claims(
            conn,
            signal_fn=lambda candidate, sig: signals.append((candidate, sig)),
        ) == 0
        assert signals == []
        assert kb.get_task(conn, task_id).status == "running"


def test_identityless_legacy_worker_is_defer_only(monkeypatch):
    """A3: a bare live PID is never an authorized signal address."""
    pid = 424_242
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(kb, "_pid_alive", lambda candidate: candidate == pid)
    monkeypatch.setattr(kb.time, "sleep", lambda _seconds: None)

    result = kb._terminate_reclaimed_worker(
        pid,
        _local_claim_lock(),
        signal_fn=lambda candidate, sig: signals.append((candidate, sig)),
        worker_pid_start=None,
    )

    assert signals == []
    assert result["termination_attempted"] is False
    assert result["defer_reclaim"] is True
    assert result["identity_state"] == "missing"
    assert kb._worker_survived_termination(result) is True


def test_recycled_pid_is_not_signalled(monkeypatch):
    """A3: a changed start fingerprint proves the numeric PID was reused."""
    pid = 424_242
    stored_start = 111
    live_start = 222
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(kb, "_pid_alive", lambda candidate: candidate == pid)
    monkeypatch.setattr(
        kb,
        "_process_start_time",
        lambda candidate: live_start if candidate == pid else None,
    )
    monkeypatch.setattr(kb.time, "sleep", lambda _seconds: None)

    result = kb._terminate_reclaimed_worker(
        pid,
        _local_claim_lock(),
        signal_fn=lambda candidate, sig: signals.append((candidate, sig)),
        worker_pid_start=stored_start,
    )

    assert signals == []
    assert result["termination_attempted"] is False
    assert result["terminated"] is True
    assert result["identity_state"] == "recycled"
    assert result["defer_reclaim"] is False


def test_pid_reuse_between_validation_and_signal_is_refused(monkeypatch):
    """A3: the identity is revalidated at the destructive-action boundary."""
    pid = 424_242
    stored_start = 111
    live_starts = iter((stored_start, 222, 222))
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(kb, "_pid_alive", lambda candidate: candidate == pid)
    monkeypatch.setattr(
        kb,
        "_process_start_time",
        lambda candidate: next(live_starts) if candidate == pid else None,
    )

    result = kb._terminate_reclaimed_worker(
        pid,
        _local_claim_lock(),
        signal_fn=lambda candidate, sig: signals.append((candidate, sig)),
        worker_pid_start=stored_start,
    )

    assert signals == []
    assert result["termination_attempted"] is False
    assert result["terminated"] is True
    assert result["identity_state"] == "recycled_before_signal"


def test_unreadable_live_identity_is_defer_only(monkeypatch):
    """A3: probe failure is UNKNOWN, never permission to signal or requeue."""
    pid = 424_242
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(kb, "_pid_alive", lambda candidate: candidate == pid)
    monkeypatch.setattr(kb, "_process_start_time", lambda _candidate: None)

    result = kb._terminate_reclaimed_worker(
        pid,
        _local_claim_lock(),
        signal_fn=lambda candidate, sig: signals.append((candidate, sig)),
        worker_pid_start=111,
    )

    assert signals == []
    assert result["termination_attempted"] is False
    assert result["terminated"] is False
    assert result["defer_reclaim"] is True
    assert result["identity_state"] == "unknown"


def test_windows_tree_kill_revalidates_identity_without_a_test_hook(monkeypatch):
    """A3: the production Windows tree-kill seam has the same refusal guard."""
    import gateway.status as status

    pid = 424_242
    stored_start = 111
    claim_lock = _local_claim_lock()
    live_starts = iter((stored_start, 222, 222))
    terminated: list[tuple[int, bool]] = []
    monkeypatch.setattr(kb.os, "name", "nt")
    monkeypatch.setattr(kb, "_pid_alive", lambda candidate: candidate == pid)
    monkeypatch.setattr(
        kb,
        "_process_start_time",
        lambda candidate: next(live_starts) if candidate == pid else None,
    )
    monkeypatch.setattr(
        status,
        "terminate_pid",
        lambda candidate, force=False: terminated.append((candidate, force)),
    )

    result = kb._terminate_reclaimed_worker(
        pid, claim_lock, worker_pid_start=stored_start
    )

    assert terminated == []
    assert result["termination_attempted"] is False
    assert result["terminated"] is True
    assert result["identity_state"] == "recycled_before_signal"


def test_exact_worker_identity_remains_a_signalable_positive_control(monkeypatch):
    """A3 control: identity refusal must not turn explicit reclaim into a no-op."""
    pid = 424_242
    start = 111
    alive = True
    signals: list[tuple[int, int]] = []

    def pid_alive(candidate):
        return candidate == pid and alive

    def send_signal(candidate, sig):
        nonlocal alive
        signals.append((candidate, sig))
        alive = False

    monkeypatch.setattr(kb, "_pid_alive", pid_alive)
    monkeypatch.setattr(
        kb, "_process_start_time", lambda candidate: start if pid_alive(candidate) else None
    )
    monkeypatch.setattr(kb.time, "sleep", lambda _seconds: None)

    result = kb._terminate_reclaimed_worker(
        pid,
        _local_claim_lock(),
        signal_fn=send_signal,
        worker_pid_start=start,
    )

    assert signals == [(pid, signal.SIGTERM)]
    assert result["termination_attempted"] is True
    assert result["terminated"] is True
    assert result["identity_state"] == "matched"
    assert result["defer_reclaim"] is False


@pytest.mark.parametrize("queue", ["ready", "review"])
def test_every_dispatch_spawn_persists_worker_start_identity(
    kanban_home, all_assignees_spawnable, monkeypatch, queue
):
    """A3: both ready and review spawn loops persist PID start identity."""
    pid = 424_242
    start = 987_654
    monkeypatch.setattr(
        kb, "_process_start_time", lambda candidate: start if candidate == pid else None
    )

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title=f"identity-{queue}", assignee="worker")
        if queue == "review":
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET status = 'review' WHERE id = ?", (task_id,)
                )

        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda _task, _workspace: pid,
            max_spawn=1,
        )

        assert [entry[0] for entry in result.spawned] == [task_id]
        task_row = conn.execute(
            "SELECT worker_pid, worker_pid_start, current_run_id "
            "FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        run_row = conn.execute(
            "SELECT worker_pid, worker_pid_start FROM task_runs WHERE id = ?",
            (task_row["current_run_id"],),
        ).fetchone()
        assert (task_row["worker_pid"], task_row["worker_pid_start"]) == (
            pid,
            start,
        )
        assert (run_row["worker_pid"], run_row["worker_pid_start"]) == (
            pid,
            start,
        )


@pytest.mark.parametrize("queue", ["ready", "review"])
def test_spawn_identity_cas_does_not_pollute_a_synchronously_completed_run(
    kanban_home, all_assignees_spawnable, monkeypatch, queue
):
    """A3: late parent publication cannot write a PID onto a completed run."""
    pid = 424_242
    start = 987_654
    monkeypatch.setattr(
        kb, "_process_start_time", lambda candidate: start if candidate == pid else None
    )

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title=f"fast-{queue}", assignee="worker")
        if queue == "review":
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET status = 'review' WHERE id = ?", (task_id,)
                )

        def _complete_before_return(task, _workspace):
            assert kb.complete_task(conn, task.id, result="finished") is True
            return pid

        result = kb.dispatch_once(conn, spawn_fn=_complete_before_return, max_spawn=1)

        assert [entry[0] for entry in result.spawned] == [task_id]
        task_row = conn.execute(
            "SELECT status, worker_pid, worker_pid_start FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        run_row = conn.execute(
            "SELECT worker_pid, worker_pid_start FROM task_runs "
            "WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        assert task_row["status"] == "done"
        assert task_row["worker_pid"] is None
        assert task_row["worker_pid_start"] is None
        assert run_row["worker_pid"] is None
        assert run_row["worker_pid_start"] is None
