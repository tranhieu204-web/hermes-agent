"""Run-scoped termination authority.

An operator terminating run N from the dashboard must never affect run N+1.

``POST /runs/{run_id}/terminate`` validated only that the *requested* run was
still open and then called ``reclaim_task(task_id)``, which re-reads whichever
run is *current*.  Between those two steps the requested run can end and a
successor run can claim the task, so the operator's run-scoped action lands on
a run they never asked about — a wrong-run destructive action that does not
depend on PID reuse.

``reclaim_task`` already had sibling functions carrying ``expected_run_id`` and
binding it into their CAS (``AND current_run_id = ?``); it was simply missed
when that guard was rolled out.  These tests pin the guarantee at both levels:
the storage CAS, and the HTTP surface the operator actually touches.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB (matches the sibling suites)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _host() -> str:
    return kb._claimer_id().split(":", 1)[0]


def _start_run(conn, task_id: str, pid: int) -> int:
    """Claim ``task_id`` as a fresh run and publish ``pid``; return the run id."""
    kb.claim_task(conn, task_id, claimer=f"{_host()}:worker")
    kb._set_worker_pid(conn, task_id, pid)
    run_id = kb._current_run_id(conn, task_id)
    assert run_id is not None
    return run_id


def _supersede(conn, task_id: str, monkeypatch, old_pid: int, new_pid: int):
    """End the current run and start a successor; return (old_run, new_run)."""
    old_run = kb._current_run_id(conn, task_id)
    # The old worker is genuinely gone, so the reclaim that ends run N is
    # legitimate — this is the ordinary end-of-run path, not the defect.
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    assert kb.reclaim_task(conn, task_id) is True
    new_run = _start_run(conn, task_id, new_pid)
    assert new_run != old_run
    return old_run, new_run


def test_terminate_run_endpoint_refuses_superseded_run(kanban_home, monkeypatch):
    """The operator asks to kill run N; run N+1 must survive untouched.

    This is the user-facing proof, and it reproduces the ACTUAL race rather
    than a pre-ended run (which the endpoint's existing ``ended_at`` check
    already rejects).  A deterministic barrier supersedes the run *inside* the
    endpoint's own window: the endpoint reads run N while it is still open,
    and by the time it acts, run N+1 owns the task.  Unfixed, the endpoint
    reclaims whatever run is current and ends run N+1.
    """
    plugin_api = pytest.importorskip(
        "plugins.kanban.dashboard.plugin_api",
        reason="kanban dashboard plugin not importable in this environment",
    )
    from fastapi import HTTPException

    with kb.connect() as conn:
        t = kb.create_task(conn, title="run-scope", assignee="a")
        old_run = _start_run(conn, t, 11111)

        state: dict[str, int] = {}
        real_get_run = plugin_api.kanban_db.get_run

        # Isolate the AUTHORITY question from the termination primitive: this
        # test asks "which run may this request act on?", not "does the kill
        # syscall work?".  A terminator that always reports clean success
        # removes the identity/liveness guards from the picture, so a wrong-run
        # action cannot be masked by them, and records exactly which PID the
        # code chose to target.
        targeted: list[int] = []

        def _fake_terminate(pid, lock, *, signal_fn=None, worker_pid_start=None):
            targeted.append(pid)
            return {
                "pid": pid,
                "host_local": True,
                "termination_attempted": True,
                "terminated": True,
            }

        monkeypatch.setattr(kb, "_terminate_reclaimed_worker", _fake_terminate)

        def _get_run_then_supersede(c, run_id):
            """Return run N as still-open, then let run N+1 take the task."""
            snapshot = real_get_run(c, run_id)
            if not state:
                _, new_run = _supersede(conn, t, monkeypatch, 11111, 22222)
                state["new_run"] = new_run
                # The successor's worker is alive and healthy.
                monkeypatch.setattr(kb, "_pid_alive", lambda _pid: True)
                monkeypatch.setattr(kb, "_terminate_reclaimed_worker", _fake_terminate)
            return snapshot

        monkeypatch.setattr(plugin_api.kanban_db, "get_run", _get_run_then_supersede)

        with pytest.raises(HTTPException) as excinfo:
            plugin_api.terminate_run_endpoint(
                run_id=old_run,
                payload=plugin_api.TerminateRunBody(reason="operator abort"),
                board=None,
            )
        assert excinfo.value.status_code == 409

        # The successor must be entirely unaffected.  (11111 legitimately
        # appears here: ending run N is the ordinary path taken by the
        # supersede helper.  22222 is the successor and must never be hit.)
        new_run = state["new_run"]
        assert 22222 not in targeted, f"successor worker was targeted: {targeted}"
        assert kb._current_run_id(conn, t) == new_run
        assert kb.get_task(conn, t).status == "running"
        assert real_get_run(conn, new_run).ended_at is None


def test_reclaim_task_refuses_when_run_superseded(kanban_home, monkeypatch):
    """Storage-level CAS: a stale ``expected_run_id`` reclaims nothing."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="cas", assignee="a")
        _start_run(conn, t, 11111)
        old_run, new_run = _supersede(conn, t, monkeypatch, 11111, 22222)

        monkeypatch.setattr(kb, "_pid_alive", lambda _pid: True)
        signalled: list[tuple[int, int]] = []

        assert kb.reclaim_task(
            conn, t, expected_run_id=old_run,
            signal_fn=lambda pid, sig: signalled.append((pid, sig)),
        ) is False

        # Refusing must be silent: no signal may be delivered to the successor.
        assert signalled == []
        assert kb._current_run_id(conn, t) == new_run
        assert kb.get_task(conn, t).status == "running"
        assert kb.get_run(conn, new_run).ended_at is None


def test_reclaim_task_allows_the_current_run(kanban_home, monkeypatch):
    """Positive control: the guard must not break the legitimate reclaim."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="current", assignee="a")
        run_id = _start_run(conn, t, 11111)

        monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
        assert kb.reclaim_task(conn, t, expected_run_id=run_id) is True
        assert kb.get_task(conn, t).status == "ready"
        assert kb.get_run(conn, run_id).ended_at is not None


def test_reclaim_task_without_expected_run_id_is_unchanged(kanban_home, monkeypatch):
    """Back-compat: existing callers that pass no run id keep task-level scope."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="compat", assignee="a")
        run_id = _start_run(conn, t, 11111)

        monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
        assert kb.reclaim_task(conn, t) is True
        assert kb.get_task(conn, t).status == "ready"
        assert kb.get_run(conn, run_id).ended_at is not None
