"""Never restart the gateway out from under running work (operator rule 2026-07-27).

Stopping the gateway takes the dispatcher with it and orphans in-flight workers:
the task stays claimed, the worker dies with the process tree, the work is lost.
Several gateway restarts were performed during 2026-07-27 without this check.
"""

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kb.connect() as c:
        yield c


def _make_running(conn, title="build the thing"):
    tid = kb.create_task(conn, title=title, assignee="w")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    kb.claim_task(conn, tid, claimer=kb._claimer_id())
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (4242, tid))
    return tid


def test_idle_board_is_safe_to_restart(conn):
    assert kb.running_tasks_blocking_restart(conn) == []


def test_running_task_blocks_restart(conn):
    tid = _make_running(conn)
    blocking = kb.running_tasks_blocking_restart(conn)
    assert [b[0] for b in blocking] == [tid]


def test_finished_task_does_not_block(conn):
    tid = _make_running(conn)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
    assert kb.running_tasks_blocking_restart(conn) == []


def test_another_hosts_task_does_not_block(conn):
    """Not ours to wait for — that host owns its own restart safety."""
    tid = _make_running(conn)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET claim_lock=? WHERE id=?", ("OtherBox:1", tid))
    assert kb.running_tasks_blocking_restart(conn) == []


def test_unreadable_board_blocks_rather_than_assuming_idle(conn, monkeypatch):
    """FAIL-SAFE: if we cannot tell, refuse. 'Assume it's fine' is the bug."""
    class _Broken:
        # sqlite3.Connection.execute is read-only, so stand in a whole object.
        def execute(self, *a, **k):
            raise RuntimeError("board unreadable")

    blocking = kb.running_tasks_blocking_restart(_Broken())
    assert blocking, "an unreadable board must BLOCK, never read as idle"
    assert blocking[0][0] == "<unknown>"


def test_block_message_names_the_tasks(conn):
    tid = _make_running(conn, title="a very important build")
    msg = kb.format_restart_block(kb.running_tasks_blocking_restart(conn))
    assert tid in msg and "RUNNING" in msg and "--force" in msg


def test_force_bypasses_the_guard(monkeypatch):
    import hermes_cli.gateway_windows as gw
    assert gw.running_task_restart_block(force=True) is None


def test_guard_never_raises_and_never_blocks_on_internal_error(monkeypatch):
    """A guard that explodes must not make the gateway unstoppable."""
    import hermes_cli.gateway_windows as gw

    monkeypatch.setattr(kb, "connect", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    assert gw.running_task_restart_block() is None
