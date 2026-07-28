"""Enforcement half of the progress contract: terminate and requeue a stalled worker.

Detection alone leaves a hung worker holding its slot forever — the 2026-07-26
delegation loop sat at ~0 CPU for 45 minutes while the dispatcher reported it
healthy. But enforcement KILLS PROCESSES, so nearly every test here asserts a
case where it must REFUSE to act. A false stall destroys live work; a missed
stall only delays recovery.
"""

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture()
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    logs = tmp_path / "wlogs"
    logs.mkdir()
    monkeypatch.setattr(kb, "worker_logs_dir", lambda board=None: logs)
    kb.init_db()
    with kb.connect() as c:
        yield c, logs


class _Killer:
    """Records termination attempts instead of killing anything."""

    def __init__(self):
        self.calls = []

    def __call__(self, pid, claim_lock, **kw):
        self.calls.append((pid, claim_lock))
        return {"terminated": True}


# Synthetic clock. `started_at` MUST be expressed on the same timeline as the
# `now` passed to enforcement: mixing a real epoch with a synthetic `now` makes
# (now - started_at) negative, which the launch-grace guard correctly reads as
# "just started" and skips. That was a test bug, not a production one.
T0 = 1_000_000


def _running_task(conn, logs, *, pid=999999, log_text="working", started_at=T0 - 10_000):
    tid = kb.create_task(conn, title="t", assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    # MUST claim with the real claimer id: enforcement only touches claims
    # owned by THIS host, so a synthetic "worker" lock is correctly ignored.
    assert kb.claim_task(conn, tid, claimer=kb._claimer_id()) is not None
    (logs / f"{tid}.log").write_text(log_text, encoding="utf-8")
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET worker_pid=?, started_at=? WHERE id=?",
            (pid, int(started_at), tid),
        )
    return tid


def _run_id(conn, tid):
    return conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (tid,)).fetchone()[0]


def _prime(conn, tid, fp, when):
    rid = _run_id(conn, tid)
    if rid is None:
        return None
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE task_runs SET progress_fingerprint=?, last_progress_at=? WHERE id=?",
            (fp, when, rid),
        )
    return rid


# ------------------------------------------------------------- MUST NOT KILL
def test_progressing_worker_is_never_terminated(board, monkeypatch):
    conn, logs = board
    tid = _running_task(conn, logs)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: True)
    _prime(conn, tid, "stale-fp", T0)
    (logs / f"{tid}.log").write_text("working, now with MORE output", encoding="utf-8")
    killer = _Killer()
    out = kb.observe_and_enforce_progress(conn, now=T0 + 100_000, stall_seconds=60,
                                          terminate_fn=killer)
    assert out == [] and killer.calls == []


def test_unobservable_never_terminates(board, monkeypatch):
    """No log = no evidence. Absence of evidence must not kill a worker."""
    conn, logs = board
    tid = _running_task(conn, logs)
    (logs / f"{tid}.log").unlink()
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: True)
    _prime(conn, tid, "some-fp", T0)
    killer = _Killer()
    assert kb.observe_and_enforce_progress(conn, now=T0 + 500_000, stall_seconds=1,
                                           terminate_fn=killer) == []
    assert killer.calls == []


def test_within_the_window_is_not_terminated(board, monkeypatch):
    conn, logs = board
    tid = _running_task(conn, logs)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: True)
    fp = kb.worker_progress_fingerprint(tid)
    _prime(conn, tid, fp, T0)
    killer = _Killer()
    assert kb.observe_and_enforce_progress(conn, now=T0 + 30, stall_seconds=900,
                                           terminate_fn=killer) == []
    assert killer.calls == []


def test_dead_pid_is_left_to_the_crash_path(board, monkeypatch):
    """A dead worker is detect_crashed_workers' business — don't double-handle."""
    conn, logs = board
    tid = _running_task(conn, logs)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    fp = kb.worker_progress_fingerprint(tid)
    _prime(conn, tid, fp, T0)
    killer = _Killer()
    assert kb.observe_and_enforce_progress(conn, now=T0 + 500_000, stall_seconds=60,
                                           terminate_fn=killer) == []
    assert killer.calls == []


def test_task_inside_launch_grace_is_untouched(board, monkeypatch):
    conn, logs = board
    tid = _running_task(conn, logs, started_at=T0)  # just started
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: True)
    fp = kb.worker_progress_fingerprint(tid)
    _prime(conn, tid, fp, T0)
    killer = _Killer()
    kb.observe_and_enforce_progress(conn, now=T0 + 5, stall_seconds=1,
                                    terminate_fn=killer)
    assert killer.calls == [], "inside the launch grace window nothing may be killed"


# ----------------------------------------------------------------- MUST KILL
def test_stalled_worker_is_terminated_and_requeued(board, monkeypatch):
    """The real 2026-07-26 incident: alive, zero new output, far past the window."""
    conn, logs = board
    tid = _running_task(conn, logs, pid=424242,
                        log_text="HTTP 403 personal-team-blocked:spending-limit")
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: True)
    fp = kb.worker_progress_fingerprint(tid)
    _prime(conn, tid, fp, T0)
    killer = _Killer()

    out = kb.observe_and_enforce_progress(conn, now=T0 + 2700,
                                          stall_seconds=900, terminate_fn=killer)

    assert [t for t, _p, _s in out] == [tid]
    assert len(killer.calls) == 1 and killer.calls[0][0] == 424242
    row = conn.execute("SELECT status, worker_pid FROM tasks WHERE id=?", (tid,)).fetchone()
    assert row["status"] == "ready", "slot must be freed for another attempt"
    assert row["worker_pid"] is None


def test_stall_error_names_the_cause_not_just_the_stall(board, monkeypatch):
    conn, logs = board
    tid = _running_task(conn, logs, log_text="run out of credits")
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: True)
    fp = kb.worker_progress_fingerprint(tid)
    _prime(conn, tid, fp, T0)
    kb.observe_and_enforce_progress(conn, now=T0 + 5000, stall_seconds=900,
                                    terminate_fn=_Killer())
    err = conn.execute(
        "SELECT error FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1", (tid,)
    ).fetchone()[0]
    assert "stalled" in err and "no observed progress" in err
    assert "quota exhausted" in err


def test_stall_counts_a_failure_so_retries_are_bounded(board, monkeypatch):
    """An unbounded stall-retry loop would just re-hang forever."""
    conn, logs = board
    tid = _running_task(conn, logs)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: True)
    fp = kb.worker_progress_fingerprint(tid)
    _prime(conn, tid, fp, T0)
    kb.observe_and_enforce_progress(conn, now=T0 + 5000, stall_seconds=900,
                                    terminate_fn=_Killer())
    cf = conn.execute("SELECT consecutive_failures FROM tasks WHERE id=?", (tid,)).fetchone()[0]
    assert cf >= 1


def test_another_hosts_claim_is_never_touched(board, monkeypatch):
    """Killing a PID owned by another host would murder an unrelated process.

    PIDs are only meaningful on the host that spawned them; 424242 here may be
    something else entirely on this machine.
    """
    conn, logs = board
    tid = _running_task(conn, logs, pid=424242)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET claim_lock=? WHERE id=?", ("OtherBox:777", tid))
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: True)
    fp = kb.worker_progress_fingerprint(tid)
    _prime(conn, tid, fp, T0)
    killer = _Killer()
    out = kb.observe_and_enforce_progress(conn, now=T0 + 5000, stall_seconds=900,
                                          terminate_fn=killer)
    assert out == [] and killer.calls == []
    assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0] == "running"


def test_requeue_race_does_not_record_a_phantom_stall(board, monkeypatch):
    """If another reclaimer wins the row first, don't also mark it stalled.

    Simulates the race by having the terminator flip the task out of ``running``
    before our UPDATE lands, so the rowcount guard must decline to proceed.
    """
    conn, logs = board
    tid = _running_task(conn, logs, pid=424242)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: True)
    fp = kb.worker_progress_fingerprint(tid)
    _prime(conn, tid, fp, T0)

    def _racing_killer(pid, claim_lock, **kw):
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
        return {"terminated": True}

    out = kb.observe_and_enforce_progress(conn, now=T0 + 5000, stall_seconds=900,
                                          terminate_fn=_racing_killer)
    assert out == [], "lost the race -> must not claim the stall"
    cf = conn.execute("SELECT consecutive_failures FROM tasks WHERE id=?", (tid,)).fetchone()[0]
    assert cf == 0, "must not count a failure for a row another reclaimer owns"
