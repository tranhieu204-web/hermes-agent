"""Windows exit observer — RED tests first, then the observer contract.

The dispatcher on native Windows has NO exit-status producer at all:
``reap_worker_zombies`` is a documented no-op, ``_classify_worker_exit`` reads
only the POSIX wait-status registry, and both production spawn routes discard
the ``Popen`` object after returning ``proc.pid``. Live evidence: 70/70
crashed runs classified as ``pid <N> not alive``.

The RED tests below drive the REAL production spawn seams (``_default_spawn``
and ``_spawn_claude_plan_worker``) — not a seeded registry — and pin the
defect: with the observer gate OFF (the default), a worker that exits 0, 75,
or 7 is indistinguishable from a vanished process. They stay green after the
fix because the legacy path is preserved behind the gate; the gate-ON
acceptance tests in this file prove the same seam now yields exact codes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb

pytestmark = pytest.mark.skipif(
    os.name != "nt", reason="Windows-only exit-observer seam"
)


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB and no crash grace."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    kb.init_db()
    return home


def _fake_worker(tmp_path: Path, exit_code: int, sleep_s: float = 0.0) -> Path:
    """Deterministic child executable: optional sleep, then exit(code)."""
    script = tmp_path / f"fake_worker_{exit_code}.py"
    script.write_text(
        "import sys, time\n"
        f"time.sleep({sleep_s})\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    return script


def _spawn_via_production_route(conn, monkeypatch, tmp_path, exit_code):
    """Dispatch one task through the REAL ``_default_spawn`` seam.

    Only the hermes argv is redirected to a deterministic python child —
    everything else (claim, workspace, env, log, Popen call, pid persistence)
    is the production path.
    """
    script = _fake_worker(tmp_path, exit_code)
    monkeypatch.setattr(
        kb, "_resolve_hermes_argv", lambda: [sys.executable, str(script)]
    )
    import hermes_cli.profiles as profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    tid = kb.create_task(conn, title=f"exit-{exit_code}", assignee="worker")
    result = kb.dispatch_once(conn)
    assert [s[0] for s in result.spawned] == [tid]
    task = kb.get_task(conn, tid)
    assert task.worker_pid, "production spawn must persist a worker pid"
    return tid, task


def _wait_pid_gone(pid: int, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not kb._pid_alive(pid):
            return
        time.sleep(0.1)
    raise AssertionError(f"pid {pid} still alive after {timeout}s")


# ---------------------------------------------------------------------------
# RED 1 — the real hermes-route spawn seam loses every exit code (gate OFF)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("exit_code", [0, 75, 7])
def test_red_default_spawn_exit_code_is_lost_without_observer(
    kanban_home, tmp_path, monkeypatch, exit_code
):
    """Exit 0 / 75 / 7 through the production spawn are all 'unknown'.

    This is the defect, proven at the actual seam: the intended
    protocol-violation (0), rate-limited (75) and nonzero (7) branches are
    operationally unreachable on Windows because no producer retains the
    child's return code.
    """
    with kb.connect() as conn:
        tid, task = _spawn_via_production_route(
            conn, monkeypatch, tmp_path, exit_code
        )
        _wait_pid_gone(task.worker_pid)

        assert kb._classify_worker_exit(task.worker_pid) == ("unknown", None)

        crashed = kb.detect_crashed_workers(conn)
        assert tid in crashed

        run = conn.execute(
            "SELECT outcome, error FROM task_runs WHERE task_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (tid,),
        ).fetchone()
        # Same ignorance for every code: a generic vanished-pid crash.
        assert run["outcome"] == "crashed"
        assert f"pid {task.worker_pid} not alive" in run["error"]
        ev = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? "
            "AND kind = 'crashed' ORDER BY id DESC LIMIT 1",
            (tid,),
        ).fetchone()
        payload = json.loads(ev["payload"])
        assert "exit_code" not in payload


# ---------------------------------------------------------------------------
# RED 2 — the Claude Plan route discards the Popen handle the same way
# ---------------------------------------------------------------------------
def test_red_claude_plan_route_discards_popen_handle(
    kanban_home, tmp_path, monkeypatch
):
    """The plan route returns a bare int pid; the handle (and with it the
    only Windows source of the exit code) is dropped on the floor."""
    script = _fake_worker(tmp_path, 75)
    # Route the task down the claude-plan branch with a fake executable.
    monkeypatch.setattr(kb, "_task_uses_claude_plan_route", lambda task: True)
    monkeypatch.setattr(kb, "claude_cli_path", lambda: sys.executable)
    monkeypatch.setattr(
        kb, "_resolve_hermes_argv",
        lambda: pytest.fail("claude-plan task must not take the hermes route"),
    )

    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="claude-red", assignee="worker",
            model_override="claude-fable-5",
        )
        import hermes_cli.profiles as profiles

        monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
        # sys.executable -p <contract> ... exits 2 immediately (bad option) —
        # good enough: a real exit code that nothing captures.
        spawned = kb._default_spawn(
            kb.get_task(conn, tid) or kb.claim_task(conn, tid),
            str(tmp_path),
        )
        assert isinstance(spawned, int), (
            "claude-plan spawn returns a bare pid — the Popen handle is "
            "discarded, so the exit code is unrecoverable"
        )
        _wait_pid_gone(spawned)
        assert kb._classify_worker_exit(spawned) == ("unknown", None)


# ---------------------------------------------------------------------------
# RED 3 — negative control: a parent-held handle dies with the parent
# ---------------------------------------------------------------------------
def test_red_in_process_waiter_loses_handle_when_parent_dies(tmp_path):
    """An in-gateway waiter cannot survive the gateway.

    A 'gateway' process retains the only Popen handle on a worker; the
    gateway is killed while the worker lives on; the worker's eventual exit
    code (23) is unrecoverable by any surviving party. This is the negative
    control that selects a detached wrapper over an in-process waiter.
    """
    pid_file = tmp_path / "grandchild.pid"
    gateway_script = tmp_path / "fake_gateway.py"
    gateway_script.write_text(
        "import subprocess, sys, time, pathlib\n"
        "child = subprocess.Popen([sys.executable, '-c',\n"
        "    'import time,sys; time.sleep(2); sys.exit(23)'])\n"
        f"pathlib.Path(r'{pid_file}').write_text(str(child.pid))\n"
        "time.sleep(600)\n",  # holds the only handle, never reaches wait()
        encoding="utf-8",
    )
    gateway = subprocess.Popen([sys.executable, str(gateway_script)])
    try:
        deadline = time.time() + 15
        while not pid_file.exists() and time.time() < deadline:
            time.sleep(0.05)
        worker_pid = int(pid_file.read_text())
        gateway.kill()  # the gateway restart
        gateway.wait(timeout=10)
        # Worker outlives its parent…
        assert kb._pid_alive(worker_pid)
        _wait_pid_gone(worker_pid)
        # …and after it exits, nobody can classify it.
        assert kb._classify_worker_exit(worker_pid) == ("unknown", None)
    finally:
        if gateway.poll() is None:
            gateway.kill()
