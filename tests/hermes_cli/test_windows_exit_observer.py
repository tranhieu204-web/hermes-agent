"""Windows exit observer — characterization tests, then the observer contract.

The dispatcher on native Windows has NO exit-status producer at all:
``reap_worker_zombies`` is a documented no-op, ``_classify_worker_exit`` reads
only the POSIX wait-status registry, and both production spawn routes discard
the ``Popen`` object after returning ``proc.pid``. Live evidence: 70/70
crashed runs classified as ``pid <N> not alive``.

HONEST LABELS (Hermes I-2): the ``test_characterization_*`` tests below are
NEGATIVE CHARACTERIZATION tests — they assert the documented defect on the
gate-OFF legacy path and are GREEN BY DESIGN on both the unfixed and the
fixed candidate (the legacy path is preserved behind the gate). They are NOT
the RED proof. The genuine RED/desired-behavior acceptance is
``test_desired_exit_code_reaches_run_history_through_dispatch``, which
enters only through top-level ``dispatch_once`` ticks and demonstrably FAILS
on the unfixed candidate (run against 46f3ca9a4; failing output recorded in
the remediation evidence pack).
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


def _spawn_via_production_route(conn, monkeypatch, tmp_path, exit_code,
                                sleep_s: float = 0.0, max_retries=None):
    """Dispatch one task through the REAL ``_default_spawn`` seam.

    Only the hermes argv is redirected to a deterministic python child —
    everything else (claim, workspace, env, log, Popen call, pid persistence)
    is the production path.
    """
    script = _fake_worker(tmp_path, exit_code, sleep_s=sleep_s)
    monkeypatch.setattr(
        kb, "_resolve_hermes_argv", lambda: [sys.executable, str(script)]
    )
    import hermes_cli.profiles as profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    tid = kb.create_task(conn, title=f"exit-{exit_code}", assignee="worker",
                         max_retries=max_retries)
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
# CHARACTERIZATION 1 — hermes-route spawn seam loses every exit code (gate OFF)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("exit_code", [0, 75, 7])
def test_characterization_default_spawn_exit_code_lost_gate_off(
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
# CHARACTERIZATION 2 — the Claude Plan route discards the Popen handle
# ---------------------------------------------------------------------------
def test_characterization_claude_plan_discards_popen_handle(
    kanban_home, tmp_path, monkeypatch
):
    """The plan route returns a bare int pid; the handle (and with it the
    only Windows source of the exit code) is dropped on the floor.

    The fake claude executable exits with the DETERMINISTIC contract code
    75 (Hermes I-2: the old version pointed at ``sys.executable -p …``,
    which produced an unrelated option-parser exit and left an unused
    ``_fake_worker`` script behind)."""
    fake_claude = tmp_path / "fake_claude.cmd"
    fake_claude.write_text("@echo off\r\nexit /b 75\r\n", encoding="ascii")
    # Route the task down the claude-plan branch with the fake executable.
    monkeypatch.setattr(kb, "_task_uses_claude_plan_route", lambda task: True)
    monkeypatch.setattr(kb, "claude_cli_path", lambda: str(fake_claude))
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
        spawned = kb._default_spawn(
            kb.get_task(conn, tid) or kb.claim_task(conn, tid),
            str(tmp_path),
        )
        assert isinstance(spawned, int), (
            "claude-plan spawn returns a bare pid — the Popen handle is "
            "discarded, so the exit code is unrecoverable"
        )
        _wait_pid_gone(spawned)
        # The deterministic 75 is GONE: the legacy path cannot classify it.
        assert kb._classify_worker_exit(spawned) == ("unknown", None)


# ---------------------------------------------------------------------------
# CHARACTERIZATION 3 — negative control: a parent-held handle dies with the parent
# ---------------------------------------------------------------------------
def test_characterization_in_process_waiter_loses_handle(tmp_path):
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


# ===========================================================================
# DESIRED BEHAVIOR (the genuine RED test) — deliberately restricted to APIs
# that exist on the UNFIXED candidate so it runs there unmodified.
# ===========================================================================
def test_desired_exit_code_reaches_run_history_through_dispatch(
    kanban_home, tmp_path, monkeypatch
):
    """I-2 desired-behavior acceptance, through the top-level production
    boundary ONLY: dispatch a worker that exits 7 via ``dispatch_once``,
    then keep ticking ``dispatch_once`` until the attempt closes — the run
    history must carry the exact exit code.

    RED on the unfixed candidate (46f3ca9a4): the run closes as
    ``pid <N> not alive`` with no code, and this test FAILS (output
    recorded in the remediation evidence pack). GREEN only once a working
    observer + reconciler are wired into the dispatch tick."""
    monkeypatch.setenv("HERMES_KANBAN_WINDOWS_EXIT_OBSERVER", "1")
    import hermes_cli.profiles as profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    script = tmp_path / "exit7.py"
    script.write_text("import sys\nsys.exit(7)\n", encoding="utf-8")
    monkeypatch.setattr(
        kb, "_resolve_hermes_argv", lambda: [sys.executable, str(script)])
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="desired-code", assignee="worker", max_retries=1)
        result = kb.dispatch_once(conn)
        assert [s[0] for s in result.spawned] == [tid]
        pid = conn.execute(
            "SELECT worker_pid FROM tasks WHERE id = ?",
            (tid,)).fetchone()["worker_pid"]
        assert pid
        deadline = time.time() + 30
        while time.time() < deadline and kb._pid_alive(pid):
            time.sleep(0.1)
        run = None
        deadline = time.time() + 30
        while time.time() < deadline:
            kb.dispatch_once(conn)
            run = conn.execute(
                "SELECT outcome, error, ended_at FROM task_runs "
                "WHERE task_id = ? ORDER BY id LIMIT 1", (tid,)).fetchone()
            if run and run["ended_at"]:
                break
            time.sleep(0.2)
        assert run is not None and run["ended_at"], (
            "the dispatch ticks never closed the attempt")
        assert "exited with code 7" in (run["error"] or ""), (
            "DESIRED BEHAVIOR: the exact exit code must reach run history "
            f"through dispatch_once alone; got outcome={run['outcome']!r} "
            f"error={run['error']!r}")


# ===========================================================================
# GREEN — gate ON: the same production seams now yield exact exit codes
# ===========================================================================
@pytest.fixture
def observer_on(kanban_home, monkeypatch):
    monkeypatch.setenv(kb.WINDOWS_EXIT_OBSERVER_ENV, "1")
    return kanban_home


def _read_receipt_for(conn, tid):
    task = conn.execute(
        "SELECT current_run_id FROM tasks WHERE id = ?", (tid,)
    ).fetchone()
    return task


def _latest_run(conn, tid):
    return conn.execute(
        "SELECT * FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        (tid,),
    ).fetchone()


# --------------------------------------------------------------------------
# Production Reachability Gate #2 + #3: real top-level boundary
# (dispatch_once), spy bound at the DEFINITION module, branch matrix over
# every promised branch, artifact read back through the real consumer with
# task id AND run id asserted.
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "exit_code,expected_kind",
    [(0, "clean_exit"), (75, "rate_limited"), (7, "nonzero_exit")],
)
def test_observer_branch_matrix_through_production_boundary(
    observer_on, tmp_path, monkeypatch, exit_code, expected_kind
):
    """Exit 0/75/7 through the REAL dispatch boundary now classify exactly.

    BOTH ends are the top-level production entry: the launch is a real
    ``dispatch_once`` and the CONSUMPTION is a second real ``dispatch_once``
    tick after the exit — never a direct ``detect_crashed_workers`` call.
    Definition-module spies prove the tick itself reached
    ``reconcile_windows_exit_receipts`` and the canonical writer; moving
    the writer back behind one branch fails the 0 and 75 cases, and
    removing the reconciler call from ``_dispatch_once_locked`` fails the
    reconcile-spy assertion (mutation guards #5 and #7)."""
    spy_calls = []
    reconcile_calls = []
    real_writer = kb.write_supervisor_observation
    real_reconcile = kb.reconcile_windows_exit_receipts

    def spying_writer(task_id, **kwargs):
        spy_calls.append((task_id, kwargs.get("run_id"),
                          kwargs.get("kind"), kwargs.get("code")))
        return real_writer(task_id, **kwargs)

    def spying_reconcile(conn, **kwargs):
        reconcile_calls.append(kwargs.get("board"))
        return real_reconcile(conn, **kwargs)

    monkeypatch.setattr(kb, "write_supervisor_observation", spying_writer)
    monkeypatch.setattr(kb, "reconcile_windows_exit_receipts",
                        spying_reconcile)

    with kb.connect() as conn:
        tid, task = _spawn_via_production_route(
            conn, monkeypatch, tmp_path, exit_code, max_retries=1,
        )
        run_id = task.current_run_id
        assert run_id, "claimed dispatch must carry a run id"
        assert task.worker_pid, "DB must store the REAL worker pid"
        # Identity persisted on both tables, and the pid is the CHILD, not
        # the observer (mutation guard #4: observer-pid-instead-of-child).
        trow = conn.execute(
            "SELECT worker_pid, worker_launch_id, worker_pid_start, "
            "observer_pid FROM tasks WHERE id = ?", (tid,)).fetchone()
        assert trow["worker_launch_id"]
        assert trow["observer_pid"] and trow["observer_pid"] != trow["worker_pid"]
        rrow = _latest_run(conn, tid)
        assert rrow["worker_launch_id"] == trow["worker_launch_id"]
        assert rrow["worker_pid"] == trow["worker_pid"]

        _wait_pid_gone(task.worker_pid)
        # Give the detached observer a beat to finalize the receipt.
        receipt_path = kb.exit_receipt_path(tid, run_id=run_id)
        deadline = time.time() + 20
        while time.time() < deadline:
            state, rec = kb.read_exit_receipt(receipt_path)
            if state == "exited":
                break
            time.sleep(0.1)
        assert state == "exited", f"final receipt missing: {state}"
        assert rec["exit_code"] == exit_code

        # CONSUME through the top-level production boundary: a SECOND real
        # dispatch tick, not a direct helper call.
        reconcile_calls.clear()
        res2 = kb.dispatch_once(conn)
        crashed = res2.crashed
        assert reconcile_calls, (
            "dispatch_once must call reconcile_windows_exit_receipts — "
            "the production consumer boundary is bypassed")
        assert res2.spawned == [], (
            "the closed attempt must not silently respawn inside the "
            "assertion tick")
        if expected_kind == "rate_limited":
            assert tid not in crashed
            assert tid in res2.rate_limited
            task_after = kb.get_task(conn, tid)
            assert task_after.consecutive_failures == 0, (
                "a quota wall must not consume the failure budget")
            run = _latest_run(conn, tid)
            assert run["outcome"] == "rate_limited"
        elif expected_kind == "clean_exit":
            ev = conn.execute(
                "SELECT payload FROM task_events WHERE task_id = ? AND "
                "kind = 'protocol_violation' ORDER BY id DESC LIMIT 1",
                (tid,),
            ).fetchone()
            assert ev is not None, (
                "exit 0 while still running must be a protocol violation")
            assert json.loads(ev["payload"])["exit_code"] == 0
        else:
            assert tid in crashed
            run = _latest_run(conn, tid)
            assert f"exited with code {exit_code}" in run["error"]

        # Spy: the canonical writer ran for THIS task and THIS run.
        matching = [c for c in spy_calls if c[0] == tid and c[1] == run_id]
        assert matching, "canonical writer unreached from production"

        # Artifact readback through the REAL consumers.
        state, sup = kb.read_supervisor_record(tid, run_id=run_id)
        assert state == "valid"
        assert sup["task_id"] == tid
        assert str(sup["run_id"]) == str(run_id)
        assert sup["version"] == kb.SUPERVISOR_RECORD_VERSION_V2
        assert sup["exit_kind"] == expected_kind
        assert sup["exit_code"] == exit_code
        assert sup["receipt_sha256"] == rec["_sha256"]
        assert sup["termination_initiator"] == "natural"
        if expected_kind == "clean_exit":
            # I-6: in the crash context (card still running) a clean exit
            # is a protocol violation — never a bare 'clean_exit' cause.
            assert sup["cause"] == "worker_protocol_violation"
        diag = kb.diagnose_worker_failure(tid, run_id=run_id)
        assert diag and diag.startswith("observed:")


def test_generic_claude_exit_75_stays_nonzero(observer_on, tmp_path,
                                              monkeypatch):
    """Sentinel scope: decimal 75 from a NON-hermes contract has not
    declared Hermes's rate-limit protocol and must remain nonzero_exit."""
    fake_claude = tmp_path / "fake_claude.cmd"
    fake_claude.write_text("@echo off\r\nexit /b 75\r\n", encoding="ascii")
    monkeypatch.setattr(kb, "_task_uses_claude_plan_route", lambda t: True)
    monkeypatch.setattr(kb, "claude_cli_path", lambda: str(fake_claude))
    import hermes_cli.profiles as profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="claude-75", assignee="worker")
        result = kb.dispatch_once(conn)
        assert [s[0] for s in result.spawned] == [tid]
        task = kb.get_task(conn, tid)
        run_id = task.current_run_id
        _wait_pid_gone(task.worker_pid)
        deadline = time.time() + 20
        while time.time() < deadline:
            state, rec = kb.read_exit_receipt(
                kb.exit_receipt_path(tid, run_id=run_id))
            if state == "exited":
                break
            time.sleep(0.1)
        assert state == "exited"
        assert rec["exit_contract"] == "generic_process_v1"
        assert rec["exit_code"] == 75

        crashed = kb.detect_crashed_workers(conn)
        assert tid in crashed, "generic 75 is a real failure, not a quota wall"
        assert tid not in kb.detect_crashed_workers._last_rate_limited
        _state, sup = kb.read_supervisor_record(tid, run_id=run_id)
        assert sup["exit_kind"] == "nonzero_exit"
        assert sup["exit_code"] == 75


def test_evidence_survives_dispatcher_death(observer_on, tmp_path,
                                            monkeypatch):
    """The decisive property: the dispatching process DIES while the worker
    runs; a NEW dispatcher still recovers the exact exit code (23).

    A mini-dispatcher subprocess claims + spawns through the real
    ``dispatch_once`` and exits immediately (the 'gateway restart'). The
    detached observer must survive it, capture the worker's exit 23, and
    this process — the 'new gateway' — must classify it exactly."""
    worktree = str(Path(kb.__file__).resolve().parent.parent)
    worker = _fake_worker(tmp_path, 23, sleep_s=3.0)
    mini = tmp_path / "mini_dispatcher.py"
    mini.write_text(
        "import sys\n"
        f"sys.path.insert(0, r'{worktree}')\n"
        "import hermes_cli.profiles as profiles\n"
        "profiles.profile_exists = lambda n: True\n"
        "from hermes_cli import kanban_db as kb\n"
        f"kb._resolve_hermes_argv = lambda: [sys.executable, r'{worker}']\n"
        "conn = kb.connect()\n"
        "res = kb.dispatch_once(conn)\n"
        "assert res.spawned, f'nothing spawned: {res}'\n"
        "print('SPAWNED', res.spawned)\n",
        encoding="utf-8",
    )
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="survive-restart", assignee="worker")

    env = dict(os.environ)
    env[kb.WINDOWS_EXIT_OBSERVER_ENV] = "1"
    out = subprocess.run(
        [sys.executable, str(mini)], env=env, capture_output=True,
        text=True, timeout=60,
    )
    assert out.returncode == 0, f"mini dispatcher failed: {out.stderr}"
    # The dispatching process is now DEAD. Its worker + observer live on.
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        run_id = task.current_run_id
        assert task.worker_pid and run_id
        assert kb._pid_alive(task.worker_pid), (
            "worker must outlive the dispatcher that spawned it")
        _wait_pid_gone(task.worker_pid)
        deadline = time.time() + 20
        while time.time() < deadline:
            state, rec = kb.read_exit_receipt(
                kb.exit_receipt_path(tid, run_id=run_id))
            if state == "exited":
                break
            time.sleep(0.1)
        assert state == "exited", (
            "observer must survive dispatcher death and finalize the receipt")
        assert rec["exit_code"] == 23

        crashed = kb.detect_crashed_workers(conn)
        assert tid in crashed
        run = _latest_run(conn, tid)
        assert "exited with code 23" in run["error"]
        _s, sup = kb.read_supervisor_record(tid, run_id=run_id)
        assert sup["exit_code"] == 23
        assert sup["exit_kind"] == "nonzero_exit"


def test_observer_loss_recovery_through_dispatch_tick(
    observer_on, tmp_path, monkeypatch
):
    """Primary observer dies under a LIVE worker: the next real production
    tick — via ``reconcile_windows_exit_receipts`` at the top of
    ``_dispatch_once_locked`` — must attach ONE recovery observer instead
    of reclaiming or duplicate-spawning, and the worker's eventual exact
    exit code is still captured end to end.

    This behaviour exists ONLY in the top-of-tick reconciler: removing
    that production call makes this test fail (I-9 mutation guard #7),
    which is what proves the consumer boundary is genuinely wired.
    """
    with kb.connect() as conn:
        tid, task = _spawn_via_production_route(
            conn, monkeypatch, tmp_path, 42, sleep_s=8.0, max_retries=1,
        )
        run_id = task.current_run_id
        trow = conn.execute(
            "SELECT observer_pid FROM tasks WHERE id = ?", (tid,)).fetchone()
        obs_pid = trow["observer_pid"]
        assert obs_pid and obs_pid != task.worker_pid
        # Kill ONLY the primary observer; the worker keeps running.
        subprocess.run(
            ["taskkill", "/PID", str(obs_pid), "/F"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        _wait_pid_gone(obs_pid)
        assert kb._pid_alive(task.worker_pid), (
            "worker must survive its observer")

        # Next REAL tick: no reclaim, no duplicate spawn — recovery attach.
        res2 = kb.dispatch_once(conn)
        assert res2.spawned == [] and tid not in res2.crashed
        rrow = conn.execute(
            "SELECT observer_pid FROM task_runs WHERE id = ?",
            (run_id,)).fetchone()
        assert rrow["observer_pid"] and rrow["observer_pid"] != obs_pid, (
            "the production tick must attach a recovery observer")
        ev = conn.execute(
            "SELECT 1 FROM task_events WHERE task_id = ? AND "
            "kind = 'exit_observer_recovery'", (tid,)).fetchone()
        assert ev is not None

        _wait_pid_gone(task.worker_pid)
        deadline = time.time() + 20
        while time.time() < deadline:
            state, rec = kb.read_exit_receipt(
                kb.exit_receipt_path(tid, run_id=run_id))
            if state == "exited":
                break
            time.sleep(0.1)
        assert state == "exited", "recovery observer must finalize the receipt"
        assert rec["exit_code"] == 42

        res3 = kb.dispatch_once(conn)
        assert tid in res3.crashed
        _s, sup = kb.read_supervisor_record(tid, run_id=run_id)
        assert _s == "valid"
        assert sup["exit_kind"] == "nonzero_exit"
        assert sup["exit_code"] == 42


def test_bootstrap_failure_is_spawn_failed_never_untracked(
    observer_on, tmp_path, monkeypatch
):
    """If the launched receipt cannot be published, the spawn FAILS —
    no silent fallback to an unobserved direct launch, and the launch
    reservation is released for the retry."""
    import hermes_cli.profiles as profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    script = _fake_worker(tmp_path, 0, sleep_s=30)
    monkeypatch.setattr(
        kb, "_resolve_hermes_argv", lambda: [sys.executable, str(script)]
    )
    monkeypatch.setattr(kb, "_EXIT_OBSERVER_BOOTSTRAP_TIMEOUT_SECONDS", 6.0)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="bootstrap-fail", assignee="worker")
        # Fresh board → this task's first run id is 1. Making the receipt
        # path a DIRECTORY breaks the observer's atomic publish.
        receipt = kb.exit_receipt_path(tid, run_id=1)
        receipt.mkdir(parents=True)

        result = kb.dispatch_once(conn)
        assert not result.spawned
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.consecutive_failures == 1
        assert task.worker_pid is None
        assert task.worker_launch_id is None if hasattr(
            task, "worker_launch_id") else True
        row = conn.execute(
            "SELECT worker_launch_id, worker_pid FROM task_runs "
            "WHERE task_id = ? ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
        assert row["worker_launch_id"] is None, "reservation must be released"
        assert row["worker_pid"] is None
        ev = conn.execute(
            "SELECT 1 FROM task_events WHERE task_id = ? AND "
            "kind = 'exit_observer_bootstrap_failed'", (tid,)).fetchone()
        assert ev is not None


def test_timeout_cause_survives_late_exit_receipt(observer_on, tmp_path,
                                                  monkeypatch):
    """Precedence tier 1: a dispatcher-initiated max-runtime kill owns the
    semantic cause; the observer's receipt (arriving after the kill) adds
    the mechanical code without rewriting timed_out into nonzero_exit.

    Timeout AND late-receipt reconciliation both run through the top-level
    production boundary (real ``dispatch_once`` ticks), never by calling
    ``enforce_max_runtime`` / ``reconcile_windows_exit_receipts`` directly.
    """
    import hermes_cli.profiles as profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    script = _fake_worker(tmp_path, 0, sleep_s=120)
    monkeypatch.setattr(
        kb, "_resolve_hermes_argv", lambda: [sys.executable, str(script)]
    )
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="timeout-precedence", assignee="worker",
            max_runtime_seconds=1, max_retries=1,
        )
        result = kb.dispatch_once(conn)
        assert [s[0] for s in result.spawned] == [tid]
        task = kb.get_task(conn, tid)
        run_id = task.current_run_id
        worker_pid = task.worker_pid
        time.sleep(1.2)
        res2 = kb.dispatch_once(conn)
        assert tid in res2.timed_out, (
            "the production tick itself must enforce max runtime")
        assert res2.spawned == []
        _wait_pid_gone(worker_pid)

        # The initiated record exists and owns the cause.
        _s, sup = kb.read_supervisor_record(tid, run_id=run_id)
        assert _s == "valid"
        assert sup["termination_initiator"] == "max_runtime"
        assert sup["cause"] == "timed_out"

        # Wait for the observer's final receipt, then run the NEXT real
        # dispatch tick — the top-of-tick reconciler is what must pick the
        # late receipt up (removing that production call fails here).
        deadline = time.time() + 20
        while time.time() < deadline:
            state, rec = kb.read_exit_receipt(
                kb.exit_receipt_path(tid, run_id=run_id))
            if state == "exited":
                break
            time.sleep(0.1)
        assert state == "exited"
        kb.dispatch_once(conn)

        _s, sup = kb.read_supervisor_record(tid, run_id=run_id)
        assert sup["termination_initiator"] == "max_runtime", (
            "a later exit code must not erase who initiated the kill")
        assert sup["cause"] == "timed_out"
        assert sup["exit_code"] == rec["exit_code"], (
            "the mechanical code is retained as enrichment")
        ev = conn.execute(
            "SELECT 1 FROM task_events WHERE task_id = ? AND "
            "kind = 'late_exit_observation'", (tid,)).fetchone()
        assert ev is not None, "late receipt enriches the closed run"
        run = _latest_run(conn, tid)
        assert run["outcome"] == "timed_out", "run outcome must not reopen"


# ===========================================================================
# Receipt contract — exact schema, identity binding, honest failure states
# ===========================================================================
@pytest.fixture()
def logdir(tmp_path, monkeypatch):
    d = tmp_path / "logs"
    d.mkdir()
    monkeypatch.setattr(kb, "worker_logs_dir", lambda board=None: d)
    monkeypatch.setattr(kb, "_launched_receipt_first_seen", {})
    return d


def _mk_receipt(**over):
    """A schema-valid launched receipt for the current host/boot."""
    host = kb._claimer_id().split(":", 1)[0]
    base = {
        "schema": kb.EXIT_RECEIPT_SCHEMA,
        "version": 1,
        "state": "launched",
        "final": False,
        "sequence": 1,
        "source": "windows_popen_observer",
        "task_id": "t_x",
        "run_id": "3",
        "board": "default",
        "launch_id": "a" * 32,
        "claim_lock_sha256": kb._claim_lock_sha256("lock"),
        "command_kind": "hermes",
        "exit_contract": "hermes_kanban_v1",
        "host_id": host,
        "boot_id": kb._boot_id(),
        "observer_pid": os.getpid(),
        # Non-null by default: a receipt factory that omits launch
        # fingerprints shares the implementation's fail-open premise and
        # cannot catch missing-fingerprint acceptance bugs (Hermes I-10).
        "observer_pid_start": 555,
        "worker_pid": 4242,
        "worker_pid_start": 777,
        "launched_at": "2026-07-27T00:00:00.000000Z",
        "observed_at": None,
        "exit_semantics": "windows_process_exit_code",
        "exit_code": None,
        "observer_error": None,
        "observer_error_detail": None,
    }
    base.update(over)
    return base


def _write_receipt(logdir, rec, task_id="t_x", run_id="3"):
    path = logdir / f"{task_id}.run{run_id}{kb.EXIT_RECEIPT_SUFFIX}"
    path.write_text(json.dumps(rec), encoding="utf-8")
    return path


def _final(rec, code=7, **over):
    rec = dict(rec)
    rec.update(state="exited", final=True, sequence=2,
               observed_at="2026-07-27T00:00:05.000000Z", exit_code=code)
    rec.update(over)
    return rec


def test_receipt_round_trip_valid(logdir):
    path = _write_receipt(logdir, _final(_mk_receipt(), code=75))
    state, rec = kb.read_exit_receipt(path)
    assert state == "exited"
    assert rec["exit_code"] == 75
    assert rec["_sha256"]


@pytest.mark.parametrize("mutation,reason", [
    ({"schema": "wrong"}, "schema_mismatch"),
    ({"version": 2}, "version_mismatch"),
    ({"state": "done"}, "bad_state"),
    ({"final": 1}, "final_not_bool"),           # bool-as-int rejected
    ({"final": True}, "final_state_incoherent"),  # launched must not be final
    ({"sequence": 2}, "bad_sequence"),
    ({"source": "cmd_wrapper"}, "bad_source"),
    ({"run_id": "abc"}, "bad_run_id"),
    ({"command_kind": "codex"}, "bad_command_kind"),
    ({"exit_contract": "posix"}, "bad_exit_contract"),
    ({"observer_pid": True}, "bad_observer_pid"),  # bool-as-int rejected
    ({"observer_pid": -1}, "bad_observer_pid"),
    ({"worker_pid": 0}, "bad_worker_pid"),
    ({"exit_semantics": "posix_wait_status"}, "bad_exit_semantics"),
    ({"exit_code": 7}, "unexpected_exit_code"),  # launched with a code
    ({"recovered": True}, "unknown_keys"),       # closed key set
    ({"observed_at": "2026-07-27T00:00:01Z"}, "unexpected_observed_at"),
    ({"observer_error": "oops"}, "unexpected_observer_error"),
    ({"observer_error_detail": "ctx"}, "unexpected_observer_error_detail"),
])
def test_receipt_exact_schema_rejections(logdir, mutation, reason):
    path = _write_receipt(logdir, _mk_receipt(**mutation))
    state, rec = kb.read_exit_receipt(path)
    assert state == "invalid"
    assert rec["reason"] == reason
    assert rec["_sha256"], "invalid receipts keep their hash as evidence"


def test_receipt_missing_key_rejected(logdir):
    rec = _mk_receipt()
    del rec["boot_id"]
    path = _write_receipt(logdir, rec)
    state, out = kb.read_exit_receipt(path)
    assert state == "invalid"
    assert out["reason"] == "missing_keys"


def test_error_receipt_detail_bounded(logdir):
    rec = _mk_receipt(worker_pid=None)
    rec.update(state="observer_error", final=True, sequence=2,
               observed_at="2026-07-27T00:00:05.000000Z",
               observer_error="wait_failed",
               observer_error_detail="x" * 501)
    path = _write_receipt(logdir, rec)
    state, out = kb.read_exit_receipt(path)
    assert state == "invalid"
    assert out["reason"] == "bad_observer_error_detail"


def test_final_receipt_requires_observed_at(logdir):
    path = _write_receipt(logdir, _final(_mk_receipt(), observed_at=None))
    state, out = kb.read_exit_receipt(path)
    assert state == "invalid"
    assert out["reason"] == "missing_observed_at"


def test_exited_receipt_requires_int_code_not_bool(logdir):
    path = _write_receipt(logdir, _final(_mk_receipt(), code=True))
    state, rec = kb.read_exit_receipt(path)
    assert state == "invalid"
    assert rec["reason"] == "missing_exit_code"


@pytest.mark.parametrize("field,value,expected", [
    ("task_id", "t_OTHER", "task_id"),
    ("run_id", "4", "run_id"),
    ("board", "otherboard", "board"),
    ("launch_id", "b" * 32, "launch_id"),
    ("claim_lock_sha256", kb._claim_lock_sha256("stolen"), "claim_lock"),
    ("host_id", "otherhost", "host_id"),
    # Mismatched values reject…
    ("worker_pid", 999, "worker_pid"),
    ("worker_pid_start", 888, "worker_pid_start"),
    ("observer_pid", 999983, "observer_pid"),
    ("observer_pid_start", 888888, "observer_pid_start"),
    # …and MISSING identity is unknown/conflict, never a match (I-5/I-10):
    # when the DB carries an expected value, a receipt without one must
    # fail CLOSED.
    ("worker_pid", None, "worker_pid_missing"),
    ("worker_pid_start", None, "worker_pid_start_missing"),
    ("observer_pid_start", None, "observer_pid_start_missing"),
])
def test_receipt_identity_mismatches_each_rejected(logdir, field, value,
                                                   expected):
    """FULL expected identity at the validation call — every field the DB
    can carry participates, and each one is proven to reject both a
    mismatched and a missing receipt value."""
    rec = _final(_mk_receipt(**{field: value}))
    mismatch = kb._receipt_identity_mismatch(
        rec, task_id="t_x", run_id="3", board="default",
        launch_id="a" * 32, claim_lock="lock",
        worker_pid=4242, worker_pid_start=777,
        observer_pid=os.getpid(), observer_pid_start=555,
    )
    assert mismatch == expected


def test_receipt_identity_full_binding_accepts_exact_match(logdir):
    """Control for the rejection matrix: the unmutated receipt binds
    cleanly against the same full expected identity."""
    mismatch = kb._receipt_identity_mismatch(
        _final(_mk_receipt()), task_id="t_x", run_id="3", board="default",
        launch_id="a" * 32, claim_lock="lock",
        worker_pid=4242, worker_pid_start=777,
        observer_pid=os.getpid(), observer_pid_start=555,
    )
    assert mismatch is None


def test_pid_identity_helpers_fail_closed_on_unknown(monkeypatch):
    """I-5: unknown identity must HOLD — never a kill-eligible match,
    never a release-eligible 'gone'. ``_pid_alive_with_start`` stays
    hold-biased (deferral decisions only); the confirmed variants used for
    kill/trust/release fail closed."""
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(kb, "_process_start_time", lambda pid: None)
    assert kb._pid_alive_with_start(1234, 777) is True
    assert kb._pid_confirmed_ours(1234, 777) is False
    assert kb._pid_confirmed_gone(1234, 777) is False

    monkeypatch.setattr(kb, "_process_start_time", lambda pid: 999)
    assert kb._pid_confirmed_ours(1234, 777) is False
    assert kb._pid_confirmed_gone(1234, 777) is True  # provably recycled

    monkeypatch.setattr(kb, "_process_start_time", lambda pid: 777)
    assert kb._pid_confirmed_ours(1234, 777) is True
    assert kb._pid_confirmed_gone(1234, 777) is False

    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    assert kb._pid_confirmed_ours(1234, 777) is False
    assert kb._pid_confirmed_gone(1234, 777) is True

    monkeypatch.setattr(kb, "_pid_alive", lambda pid: True)
    assert kb._pid_confirmed_ours(1234, None) is False, (
        "no spawn fingerprint -> never kill-eligible")
    assert kb._pid_confirmed_gone(1234, None) is False, (
        "a live pid without a fingerprint can never be proven gone")


def test_windows_status_values_are_never_posix_signals(logdir):
    """0xC0000005 (access violation) is a Windows process exit code and
    must surface as nonzero_exit with the exact integer — not 'signaled'."""
    kind, code = kb._map_receipt_exit(
        _final(_mk_receipt(), code=3221225477))
    assert kind == "nonzero_exit"
    assert code == 3221225477


def test_exit_75_scoped_to_hermes_contract():
    hermes = _final(_mk_receipt(), code=75)
    generic = _final(_mk_receipt(exit_contract="generic_process_v1"), code=75)
    assert kb._map_receipt_exit(hermes) == ("rate_limited", 75)
    assert kb._map_receipt_exit(generic) == ("nonzero_exit", 75)


def test_invalid_receipt_suppresses_legacy_inference(logdir):
    """An attempted-but-unusable structured receipt must not reactivate the
    known-false-positive log heuristic."""
    (logdir / "t_x.log").write_text("Error: APIConnectionError",
                                    encoding="utf-8")
    _write_receipt(logdir, _mk_receipt(schema="wrong"))
    kind, code, obs = kb._observed_worker_exit(
        "t_x", "3", 4242, launch_id="a" * 32, claim_lock="lock",
        worker_pid_start=777,
    )
    assert (kind, code) == ("unknown", None)
    assert obs["observation_state"] == "observer_invalid"
    assert obs["cause"] == "exit_receipt_invalid"
    # And the canonical record it produces carries the honest cause, which
    # diagnose then prefers over inference.
    kb.write_supervisor_observation("t_x", kind=kind, code=code, pid=4242,
                                    run_id="3", observation=obs)
    out = kb.diagnose_worker_failure("t_x", run_id="3")
    assert out.startswith("observed:exit_receipt_invalid")
    assert "legacy-inference" not in out


def test_host_reboot_is_deterministic_with_null_code(logdir):
    _write_receipt(logdir, _mk_receipt(boot_id="samehost:but-other-boot"))
    kind, code, obs = kb._observed_worker_exit(
        "t_x", "3", 4242, launch_id="a" * 32, claim_lock="lock",
        worker_pid_start=777,
    )
    assert (kind, code) == ("unknown", None)
    assert obs["observation_state"] == "host_reboot"
    assert obs["cause"] == "host_reboot"
    assert obs["quality"] == "deterministic_event_no_exit_code"
    kb.write_supervisor_observation("t_x", kind=kind, code=code, pid=4242,
                                    run_id="3", observation=obs)
    _s, sup = kb.read_supervisor_record("t_x", run_id="3")
    assert sup["cause"] == "host_reboot"
    assert sup["exit_code"] is None, "a reboot must never invent a code"


def test_observer_loss_is_honest_after_grace(logdir, monkeypatch):
    # A real-but-dead observer pid: spawn a child that exits immediately.
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=30)
    _write_receipt(logdir, _mk_receipt(observer_pid=dead.pid))
    monkeypatch.setattr(kb, "_LAUNCHED_RECEIPT_GRACE_SECONDS", 0.0)
    kind, code, obs = kb._observed_worker_exit(
        "t_x", "3", 4242, launch_id="a" * 32, claim_lock="lock",
        worker_pid_start=777,
    )
    assert (kind, code) == ("unknown", None)
    assert obs["observation_state"] == "observer_lost"
    assert obs["cause"] == "exit_observer_lost"
    assert obs["quality"] == "incomplete"


def test_unavailable_receipt_pending_then_honest_close(logdir, monkeypatch):
    """I-6: a transiently unreadable receipt (sharing/AV race) is PENDING —
    held and retried — never lumped in with structurally invalid bytes;
    only a PERSISTENTLY unavailable receipt closes, and honestly as
    unavailable, never as a guessed cause."""
    monkeypatch.setattr(kb, "read_exit_receipt",
                        lambda path: ("unavailable", None))
    kind, code, obs = kb._observed_worker_exit(
        "t_x", "31", 4242, launch_id="a" * 32, claim_lock="lock",
        worker_pid_start=777,
    )
    assert obs["observation_state"] == "pending_receipt", (
        "first unavailable sighting must hold, not close")
    # Grace expired -> honest close as unavailable.
    monkeypatch.setattr(kb, "_UNAVAILABLE_RECEIPT_GRACE_SECONDS", 0.0)
    kind, code, obs = kb._observed_worker_exit(
        "t_x", "31", 4242, launch_id="a" * 32, claim_lock="lock",
        worker_pid_start=777,
    )
    assert (kind, code) == ("unknown", None)
    assert obs["cause"] == "exit_receipt_unavailable"
    assert obs["quality"] == "incomplete"


def test_initiator_only_write_is_monotonic(logdir):
    """I-6: enrichment must be MONOTONIC — an initiator-only write must not
    drop the receipt path/hash/boot/observation-state/mechanics an earlier
    reconciliation already recorded."""
    path = _write_receipt(logdir, _final(_mk_receipt(run_id="7"), code=9))
    _state, rec = kb.read_exit_receipt(path)
    assert _state == "exited"
    st = kb.write_supervisor_observation(
        "t_x", kind="nonzero_exit", code=9, pid=4242, run_id="7",
        observation={"receipt": rec, "observation_state": "exited"})
    assert st == "written"
    _s, before = kb.read_supervisor_record("t_x", run_id="7")
    assert before["receipt_sha256"] == rec["_sha256"]
    # Now an initiator-only write (no receipt in hand).
    st = kb.write_supervisor_observation(
        "t_x", run_id="7", pid=4242, initiator="manual_reclaim")
    assert st == "enriched"
    _s, after = kb.read_supervisor_record("t_x", run_id="7")
    assert after["termination_initiator"] == "manual_reclaim"
    for field in ("receipt_path", "receipt_sha256", "boot_id",
                  "observation_state", "exit_kind", "exit_code",
                  "worker_pid_start", "observer_pid", "observer_pid_start"):
        assert after[field] == before[field], (
            f"initiator-only enrichment dropped {field}")


def test_invalid_prior_supervisor_bytes_survive_supersession(logdir):
    """I-6: invalid structured evidence must SURVIVE being superseded —
    the exact prior bytes are parked in a sidecar and linked by hash."""
    sup_path = kb.supervisor_record_path("t_x", run_id="9")
    sup_path.parent.mkdir(parents=True, exist_ok=True)
    bad = b'{"version": "not-even-close"'
    sup_path.write_bytes(bad)
    prior_sha = kb.hashlib.sha256(bad).hexdigest()
    st = kb.write_supervisor_observation(
        "t_x", kind="nonzero_exit", code=3, pid=4242, run_id="9")
    assert st == "written"
    sidecar = sup_path.with_name(f"{sup_path.name}.invalid.{prior_sha[:12]}")
    assert sidecar.exists(), "invalid prior evidence must be preserved"
    assert sidecar.read_bytes() == bad
    _s, rec = kb.read_supervisor_record("t_x", run_id="9")
    assert rec["supersedes_sha256"] == prior_sha


def test_live_observer_defers_classification(logdir):
    """Worker dead + observer alive = the final receipt is imminent; the
    run must NOT be closed on weaker evidence this tick."""
    _write_receipt(
        logdir,
        _mk_receipt(observer_pid=os.getpid(),
                    observer_pid_start=kb._process_start_time(os.getpid())),
    )
    kind, code, obs = kb._observed_worker_exit(
        "t_x", "3", 4242, launch_id="a" * 32, claim_lock="lock",
        worker_pid_start=777,
    )
    assert obs["observation_state"] == "pending_receipt"


def test_pid_reuse_fingerprint_is_a_conflict(logdir):
    """Same pid, different start fingerprint = a different process. The
    receipt must be rejected as conflict, never trusted."""
    _write_receipt(logdir, _final(_mk_receipt(worker_pid_start=111), code=7))
    kind, code, obs = kb._observed_worker_exit(
        "t_x", "3", 4242, launch_id="a" * 32, claim_lock="lock",
        worker_pid_start=777,
    )
    assert (kind, code) == ("unknown", None)
    assert obs["observation_state"] == "conflict"
    assert obs["mismatch"] == "worker_pid_start"


def test_unknown_v1_record_upgraded_by_exact_receipt(logdir):
    """A pre-existing unknown/process_vanished projection is replaced by the
    stronger v2 record when a valid receipt for the exact launch arrives —
    with the prior bytes preserved by hash."""
    kb.write_supervisor_record("t_x", kind="unknown", pid=4242, run_id="3")
    prior_sha = kb.hashlib.sha256(
        kb.supervisor_record_path("t_x", run_id="3").read_bytes()
    ).hexdigest()
    path = _write_receipt(logdir, _final(_mk_receipt(), code=7))
    _state, rec = kb.read_exit_receipt(path)
    status = kb.write_supervisor_observation(
        "t_x", pid=4242, run_id="3",
        observation={"observation_state": "exited", "receipt": rec,
                     "quality": "deterministic"},
    )
    assert status == "enriched"
    _s, sup = kb.read_supervisor_record("t_x", run_id="3")
    assert sup["version"] == kb.SUPERVISOR_RECORD_VERSION_V2
    assert sup["exit_kind"] == "nonzero_exit"
    assert sup["exit_code"] == 7
    assert sup["supersedes_sha256"] == prior_sha


def test_conflicting_launch_ids_never_overwrite(logdir):
    """Two valid records claiming different launch ids for one run: emit
    conflict, keep the existing record untouched."""
    path = _write_receipt(logdir, _final(_mk_receipt(), code=7))
    _state, rec = kb.read_exit_receipt(path)
    kb.write_supervisor_observation(
        "t_x", pid=4242, run_id="3",
        observation={"observation_state": "exited", "receipt": rec,
                     "quality": "deterministic"},
    )
    before = kb.supervisor_record_path("t_x", run_id="3").read_bytes()
    other = _final(_mk_receipt(launch_id="b" * 32), code=9)
    other["_sha256"] = "deadbeef"
    status = kb.write_supervisor_observation(
        "t_x", pid=4242, run_id="3",
        observation={"observation_state": "exited", "receipt": other,
                     "quality": "deterministic"},
    )
    assert status == "conflict"
    assert kb.supervisor_record_path(
        "t_x", run_id="3").read_bytes() == before


def test_exit_receipt_path_requires_run_id():
    with pytest.raises(ValueError):
        kb.exit_receipt_path("t_x", run_id=None)


def test_atomic_write_stale_temp_cannot_collide(tmp_path):
    """A stale temp file from a killed writer must not break (or be
    consumed by) the next writer — unique per-writer temp names.

    BOTH stale shapes are planted: a unique-style leftover AND the fixed
    ``<final>.tmp`` name. A writer that regresses to the fixed temp name
    collides with the second one under ``O_EXCL`` and fails here
    (mutation guard M3 — the original version of this test never failed
    under that mutation because it only planted the unique shape)."""
    from hermes_cli import kanban_exit_observer as obs

    final = tmp_path / "r.json"
    stale_unique = tmp_path / f"r.json.{'c' * 32}.9999.2.tmp"
    stale_unique.write_text("{half json", encoding="utf-8")
    stale_fixed = tmp_path / "r.json.tmp"
    stale_fixed.write_text("{stale fixed-name temp", encoding="utf-8")
    obs._atomic_write_receipt(
        str(final), {"ok": True}, launch_id="a" * 32, sequence=2)
    assert json.loads(final.read_text(encoding="utf-8")) == {"ok": True}
    assert stale_unique.read_text(encoding="utf-8") == "{half json"
    assert stale_fixed.read_text(encoding="utf-8") == (
        "{stale fixed-name temp"), "another writer's temp must be untouched"
    # A second, overlapping writer (different launch id / same final path)
    # must also succeed without consuming anyone else's temp.
    obs._atomic_write_receipt(
        str(final), {"ok": 2}, launch_id="b" * 32, sequence=2)
    assert json.loads(final.read_text(encoding="utf-8")) == {"ok": 2}


def test_duplicate_launch_reservation_rejected(observer_on, monkeypatch):
    """CAS 1: a second launch for the SAME run is rejected before any
    process is spawned."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="dup", assignee="worker")
        task = kb.claim_task(conn, tid)
        conn.execute(
            "UPDATE task_runs SET worker_launch_id = 'occupied' WHERE id = ?",
            (task.current_run_id,),
        )
        conn.commit()
        spec = kb.WorkerLaunchSpec(
            argv=(sys.executable, "-c", "pass"), cwd=None,
            env=dict(os.environ), log_path=Path("unused.log"),
            command_kind="hermes", exit_contract="hermes_kanban_v1",
        )
        popen_calls = []
        real_popen = subprocess.Popen
        monkeypatch.setattr(
            kb.subprocess, "Popen",
            lambda *a, **k: popen_calls.append(a) or real_popen(*a, **k),
        )
        with pytest.raises(RuntimeError, match="duplicate_launch_rejected"):
            kb._spawn_windows_exit_observer(task, spec)
        assert popen_calls == [], "the losing launch must not spawn anything"


# ===========================================================================
# Hermes L0b findings — recovery contract, board binding, fail-closed conflict
# ===========================================================================
def _bind_launch(conn, tid, rid, worker, wstart, obs_pid, launch_id):
    conn.execute(
        "UPDATE tasks SET worker_pid=?, worker_pid_start=?, observer_pid=?, "
        "observer_pid_start=?, worker_launch_id=? WHERE id=?",
        (worker, wstart, obs_pid, None, launch_id, tid))
    conn.execute(
        "UPDATE task_runs SET worker_pid=?, worker_pid_start=?, "
        "observer_pid=?, observer_pid_start=?, worker_launch_id=? "
        "WHERE id=?",
        (worker, wstart, obs_pid, None, launch_id, rid))
    conn.commit()


def _launched_receipt_for(tid, rid, claim_lock, *, worker_pid,
                          worker_pid_start, observer_pid,
                          command_kind="hermes",
                          exit_contract="hermes_kanban_v1",
                          launch_id="c" * 32, board="default", **over):
    rec = _mk_receipt(
        task_id=tid, run_id=str(rid), board=board, launch_id=launch_id,
        claim_lock_sha256=kb._claim_lock_sha256(claim_lock),
        command_kind=command_kind, exit_contract=exit_contract,
        observer_pid=observer_pid, worker_pid=worker_pid,
        worker_pid_start=worker_pid_start,
    )
    rec.update(over)
    return rec


def test_recovery_observer_inherits_original_contract(observer_on, tmp_path):
    """Hermes L0b blocking #1: recovery re-observes, it never re-labels.

    A Claude-route (generic_process_v1) worker whose primary observer died
    is recovered; it exits 75. The recovered receipt must keep the generic
    contract so 75 stays nonzero_exit — not Hermes rate_limited."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="recover", assignee="worker")
        task = kb.claim_task(conn, tid)
        rid = task.current_run_id
        worker = subprocess.Popen(
            [sys.executable, "-c",
             "import time,sys; time.sleep(4); sys.exit(75)"])
        try:
            wstart = kb._process_start_time(worker.pid)
            dead = subprocess.Popen([sys.executable, "-c", "pass"])
            dead.wait(timeout=30)
            _bind_launch(conn, tid, rid, worker.pid, wstart, dead.pid,
                         "c" * 32)
            receipt_path = kb.exit_receipt_path(tid, run_id=rid)
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            receipt_path.write_text(json.dumps(_launched_receipt_for(
                tid, rid, task.claim_lock, worker_pid=worker.pid,
                worker_pid_start=wstart, observer_pid=dead.pid,
                command_kind="claude_plan",
                exit_contract="generic_process_v1")), encoding="utf-8")

            kb.reconcile_windows_exit_receipts(conn)
            row = conn.execute(
                "SELECT observer_pid FROM task_runs WHERE id=?",
                (rid,)).fetchone()
            assert row["observer_pid"] != dead.pid, (
                "recovery observer must CAS its identity into the run")
            ev = conn.execute(
                "SELECT 1 FROM task_events WHERE task_id=? AND "
                "kind='exit_observer_recovery'", (tid,)).fetchone()
            assert ev is not None

            worker.wait(timeout=30)
            deadline = time.time() + 20
            while time.time() < deadline:
                state, rec = kb.read_exit_receipt(receipt_path)
                if state == "exited":
                    break
                time.sleep(0.1)
            assert state == "exited", "recovery observer must finalize"
            assert rec["exit_contract"] == "generic_process_v1", (
                "recovery must inherit the ORIGINAL launch contract")
            assert rec["command_kind"] == "claude_plan"
            assert rec["exit_code"] == 75
            assert kb._map_receipt_exit(rec) == ("nonzero_exit", 75)
        finally:
            if worker.poll() is None:
                worker.kill()


def test_recovery_refuses_to_overwrite_final_receipt(observer_on, tmp_path):
    """A late recovery observer must treat a final receipt as settled
    evidence — exit 0, bytes untouched."""
    receipt = tmp_path / "t_r.run1.exit-receipt.json"
    final_rec = _final(_mk_receipt(), code=7)
    receipt.write_text(json.dumps(final_rec), encoding="utf-8")
    before = receipt.read_bytes()
    sleeper = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        out = subprocess.run(
            [sys.executable, "-m", "hermes_cli.kanban_exit_observer",
             "--recover", "--receipt", str(receipt), "--task", "t_r",
             "--run", "1", "--launch", "a" * 32, "--board", "default",
             "--kind", "hermes", "--exit-contract", "hermes_kanban_v1",
             "--claim-lock-sha256", kb._claim_lock_sha256("lock"),
             "--log", str(tmp_path / "t_r.log"),
             "--worker-pid", str(sleeper.pid),
             "--worker-pid-start",
             str(kb._process_start_time(sleeper.pid))],
            capture_output=True, text=True, timeout=60,
            cwd=str(Path(kb.__file__).resolve().parent.parent),
        )
        assert out.returncode == 0, out.stderr
        assert receipt.read_bytes() == before, (
            "final receipt bytes must never be replaced by recovery")
    finally:
        sleeper.kill()


@pytest.mark.parametrize("blob", [
    "{not json at all",                       # unparseable
    '{"state": "weird", "final": false}',     # parseable, not launched
    '{"final": 1, "state": "launched"}',      # incoherent final flag
    # Launched-LOOK-ALIKE missing the entire identity/schema key set: only
    # an exact-schema-valid launched receipt may ever be replaced.
    '{"state": "launched", "final": false}',
    # Exact-schema-valid launched receipt bound to a DIFFERENT launch id.
    json.dumps(dict(_mk_receipt(task_id="t_r2", run_id="1",
                                launch_id="f" * 32))),
])
def test_recovery_preserves_non_launched_receipts(observer_on, tmp_path,
                                                  blob):
    """A recovery observer may only replace a well-formed LAUNCHED receipt.
    Invalid or incoherent bytes are first-class observer_invalid evidence —
    recovery must refuse (nonzero) and leave them untouched."""
    receipt = tmp_path / "t_r2.run1.exit-receipt.json"
    receipt.write_text(blob, encoding="utf-8")
    before = receipt.read_bytes()
    quick = subprocess.Popen([sys.executable, "-c", "pass"])
    quick.wait(timeout=30)
    out = subprocess.run(
        [sys.executable, "-m", "hermes_cli.kanban_exit_observer",
         "--recover", "--receipt", str(receipt), "--task", "t_r2",
         "--run", "1", "--launch", "a" * 32, "--board", "default",
         "--kind", "hermes", "--exit-contract", "hermes_kanban_v1",
         "--claim-lock-sha256", kb._claim_lock_sha256("lock"),
         "--log", str(tmp_path / "t_r2.log"),
         "--worker-pid", str(quick.pid),
         # A real (already-exited) pid with a nominal fingerprint: the
         # refusal under test must come from the RECEIPT check, not from
         # the separate missing-fingerprint usage guard.
         "--worker-pid-start", "1"],
        capture_output=True, text=True, timeout=60,
        cwd=str(Path(kb.__file__).resolve().parent.parent),
    )
    assert out.returncode != 0, "recovery must refuse non-launched evidence"
    assert receipt.read_bytes() == before, (
        "invalid receipt bytes are evidence and must be preserved")


def test_named_board_evidence_stays_on_its_board(observer_on, tmp_path,
                                                 monkeypatch):
    """Hermes L0b blocking #2: a named-board dispatch writes AND reads its
    exit evidence under that board, end to end."""
    import hermes_cli.profiles as profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    script = _fake_worker(tmp_path, 9)
    monkeypatch.setattr(
        kb, "_resolve_hermes_argv", lambda: [sys.executable, str(script)])
    with kb.connect(board="alpha") as conn:
        tid = kb.create_task(conn, title="board-bound", assignee="worker",
                             board="alpha")
        result = kb.dispatch_once(conn, board="alpha")
        assert [s[0] for s in result.spawned] == [tid]
        task = kb.get_task(conn, tid)
        rid = task.current_run_id
        receipt_path = kb.exit_receipt_path(tid, board="alpha", run_id=rid)
        assert "alpha" in str(receipt_path)
        _wait_pid_gone(task.worker_pid)
        deadline = time.time() + 20
        while time.time() < deadline:
            state, rec = kb.read_exit_receipt(receipt_path)
            if state == "exited":
                break
            time.sleep(0.1)
        assert state == "exited", "receipt must land on the named board"
        assert rec["board"] == "alpha"

        crashed = kb.detect_crashed_workers(conn, board="alpha")
        assert tid in crashed
        _s, sup = kb.read_supervisor_record(tid, board="alpha", run_id=rid)
        assert _s == "valid"
        assert sup["exit_code"] == 9
        assert sup["board"] == "alpha"


def test_conflicting_evidence_fails_closed_to_triage(observer_on, tmp_path):
    """Hermes L0b blocking #3: a conflicting receipt (PID-reuse fingerprint)
    for a dead worker must NOT close/requeue normally — the task parks in
    triage without consuming the failure budget."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="conflict", assignee="worker")
        task = kb.claim_task(conn, tid)
        rid = task.current_run_id
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait(timeout=30)
        _bind_launch(conn, tid, rid, dead.pid, 777, os.getpid(), "c" * 32)
        receipt_path = kb.exit_receipt_path(tid, run_id=rid)
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(json.dumps(_final(_launched_receipt_for(
            tid, rid, task.claim_lock, worker_pid=dead.pid,
            worker_pid_start=111,  # != 777: a different process entirely
            observer_pid=os.getpid()), code=7)), encoding="utf-8")

        crashed = kb.detect_crashed_workers(conn)
        assert tid not in crashed, "conflict must not be billed as a crash"
        after = kb.get_task(conn, tid)
        assert after.status == "triage"
        assert after.consecutive_failures == 0, (
            "conflicting evidence must not consume the failure budget")
        run = _latest_run(conn, tid)
        assert run["outcome"] == "conflict"
        ev = conn.execute(
            "SELECT 1 FROM task_events WHERE task_id=? AND "
            "kind='exit_evidence_conflict'", (tid,)).fetchone()
        assert ev is not None


@pytest.mark.parametrize("receipt_worker_pid,receipt_start,steal_lock", [
    (4242, 111, False),   # historical fingerprint mismatch
    (5555, 777, False),   # historical worker PID mismatch (I-15)
    (4242, 777, True),    # historical claim-lock hash mismatch (I-15)
])
def test_late_receipt_with_bad_identity_never_enriches(
    observer_on, tmp_path, receipt_worker_pid, receipt_start, steal_lock
):
    """Hermes L0b blocking #4 + I-15: pass-B late reconciliation must bind
    EVERY piece of historical identity the closed run row still carries —
    launch id, worker PID, PID-start fingerprint, AND the claim-lock hash.
    Any single mismatch yields a conflict event, never a coded
    late_exit_observation."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="late-bad", assignee="worker")
        task = kb.claim_task(conn, tid)
        rid = task.current_run_id
        with kb.write_txn(conn):
            kb._end_run(conn, tid, outcome="timed_out", status="timed_out")
        # NOTE on the I-15 premise: ``_end_run`` has ALWAYS nulled
        # ``task_runs.claim_lock`` / ``worker_pid`` on close (pre-existing
        # at base f3c133285) — Hermes's "no close-path SQL nulls them" was
        # contradicted by the base source. The binding rule is therefore
        # "any historical identity value that DID survive must bind"; this
        # test plants surviving values to prove each one binds.
        conn.execute(
            "UPDATE task_runs SET worker_launch_id=?, worker_pid=?, "
            "worker_pid_start=?, claim_lock=? WHERE id=?",
            ("c" * 32, 4242, 777, task.claim_lock, rid))
        conn.commit()
        receipt_lock = "stolen-lock" if steal_lock else task.claim_lock
        receipt_path = kb.exit_receipt_path(tid, run_id=rid)
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(json.dumps(_final(_launched_receipt_for(
            tid, rid, receipt_lock, worker_pid=receipt_worker_pid,
            worker_pid_start=receipt_start,
            observer_pid=os.getpid()), code=5)), encoding="utf-8")

        kb.reconcile_windows_exit_receipts(conn)
        late = conn.execute(
            "SELECT 1 FROM task_events WHERE task_id=? AND "
            "kind='late_exit_observation'", (tid,)).fetchone()
        assert late is None, "mismatched identity must not enrich history"
        conflict = conn.execute(
            "SELECT 1 FROM task_events WHERE task_id=? AND "
            "kind='exit_evidence_conflict'", (tid,)).fetchone()
        assert conflict is not None
        run = _latest_run(conn, tid)
        meta = json.loads(run["metadata"] or "{}")
        assert "late_exit_code" not in meta


def test_worker_env_preserved_byte_for_byte_through_observer(
    observer_on, tmp_path, monkeypatch
):
    """I-13 canary: the worker's environment must be EXACTLY the
    ``WorkerLaunchSpec.env`` the dispatcher built — the observer's own
    PYTHONPATH mutation (needed so the observer can import itself) must
    never leak into the worker, or module resolution silently changes."""
    dump = tmp_path / "env_dump.json"
    script = tmp_path / "dump_env.py"
    script.write_text(
        "import json, os, pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text(\n"
        "    json.dumps(dict(os.environ)), encoding='utf-8')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        kb, "_resolve_hermes_argv",
        lambda: [sys.executable, str(script), str(dump)])
    import hermes_cli.profiles as profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    captured = {}
    real_spawn_spec = kb._spawn_worker_spec

    def spy(task, spec, board=None):
        captured["spec"] = spec
        return real_spawn_spec(task, spec, board=board)

    monkeypatch.setattr(kb, "_spawn_worker_spec", spy)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="env-canary", assignee="worker")
        result = kb.dispatch_once(conn)
        assert [s[0] for s in result.spawned] == [tid]
        deadline = time.time() + 20
        while time.time() < deadline and not dump.exists():
            time.sleep(0.1)
        assert dump.exists(), "worker never ran / never dumped its env"
        child_env = json.loads(dump.read_text(encoding="utf-8"))
        spec_env = dict(captured["spec"].env)
        # Windows env keys are case-insensitive; compare canonicalized keys,
        # values byte-for-byte.
        child_norm = {k.upper(): v for k, v in child_env.items()}
        spec_norm = {k.upper(): v for k, v in spec_env.items()}
        assert child_norm == spec_norm, (
            "worker env differs from the dispatcher-built spec env: "
            f"only-in-child={sorted(set(child_norm) - set(spec_norm))} "
            f"only-in-spec={sorted(set(spec_norm) - set(child_norm))} "
            f"changed={[k for k in child_norm if k in spec_norm and child_norm[k] != spec_norm[k]]}")


def test_timeout_kills_descendants_before_requeue(
    observer_on, tmp_path, monkeypatch
):
    """I-7: max-runtime termination is a TREE kill — a grandchild spawned
    by the worker must be dead before the task is requeued. Root-only
    ``os.kill`` leaves the grandchild running and fails this test."""
    import hermes_cli.profiles as profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    grand_pid_file = tmp_path / "grand.pid"
    script = tmp_path / "worker_with_child.py"
    script.write_text(
        "import subprocess, sys, time, pathlib\n"
        "g = subprocess.Popen([sys.executable, '-c',\n"
        "                      'import time; time.sleep(120)'])\n"
        f"pathlib.Path(r'{grand_pid_file}').write_text(str(g.pid))\n"
        "time.sleep(120)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        kb, "_resolve_hermes_argv", lambda: [sys.executable, str(script)])
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="descendants", assignee="worker",
            max_runtime_seconds=1, max_retries=1,
        )
        result = kb.dispatch_once(conn)
        assert [s[0] for s in result.spawned] == [tid]
        task = kb.get_task(conn, tid)
        deadline = time.time() + 20
        while time.time() < deadline and not grand_pid_file.exists():
            time.sleep(0.1)
        assert grand_pid_file.exists(), "worker never spawned its grandchild"
        grand_pid = int(grand_pid_file.read_text())
        assert kb._pid_alive(grand_pid)
        time.sleep(1.2)
        res2 = kb.dispatch_once(conn)
        assert tid in res2.timed_out
        _wait_pid_gone(task.worker_pid, timeout=15)
        _wait_pid_gone(grand_pid, timeout=15)


def test_timeout_holds_claim_while_worker_may_be_alive(
    observer_on, tmp_path, monkeypatch
):
    """I-7: when termination cannot be CONFIRMED, max-runtime must hold —
    no claim clear, no identity clear, no requeue, no run close. The
    survivor is simulated at the definition-module terminator binding."""
    import hermes_cli.profiles as profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    script = _fake_worker(tmp_path, 0, sleep_s=30)
    monkeypatch.setattr(
        kb, "_resolve_hermes_argv", lambda: [sys.executable, str(script)])
    monkeypatch.setattr(
        kb, "_terminate_reclaimed_worker",
        lambda pid, lock, signal_fn=None, worker_pid_start=None: {
            "prev_pid": pid, "host_local": True,
            "termination_attempted": True, "terminated": False,
            "sigkill": True,
        })
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="unkillable", assignee="worker",
            max_runtime_seconds=1,
        )
        result = kb.dispatch_once(conn)
        assert [s[0] for s in result.spawned] == [tid]
        task = kb.get_task(conn, tid)
        try:
            time.sleep(1.2)
            res2 = kb.dispatch_once(conn)
            assert tid not in res2.timed_out, (
                "unconfirmed termination must never count as timed out")
            after = kb.get_task(conn, tid)
            assert after.status == "running", "claim must be HELD"
            assert after.worker_pid == task.worker_pid, (
                "launch identity must not be cleared")
            run = _latest_run(conn, tid)
            assert run["ended_at"] is None, "run must not be closed"
            ev = conn.execute(
                "SELECT payload FROM task_events WHERE task_id = ? AND "
                "kind = 'reclaim_deferred' ORDER BY id DESC LIMIT 1",
                (tid,)).fetchone()
            assert ev is not None
            assert json.loads(ev["payload"])["reason"] == (
                "max_runtime_worker_alive")
            # The initiated semantic record is kept for the eventual kill.
            _s, sup = kb.read_supervisor_record(
                tid, run_id=task.current_run_id)
            assert _s == "valid"
            assert sup["termination_initiator"] == "max_runtime"
        finally:
            from gateway.status import terminate_pid

            try:
                terminate_pid(int(task.worker_pid), force=True)
            except OSError:
                pass


def test_manual_reclaim_defers_when_worker_survives(
    observer_on, tmp_path, monkeypatch
):
    """I-7: operator reclaim must also refuse to requeue beside a possibly
    live worker — defer, keep the claim, report False."""
    import hermes_cli.profiles as profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    script = _fake_worker(tmp_path, 0, sleep_s=30)
    monkeypatch.setattr(
        kb, "_resolve_hermes_argv", lambda: [sys.executable, str(script)])
    monkeypatch.setattr(
        kb, "_terminate_reclaimed_worker",
        lambda pid, lock, signal_fn=None, worker_pid_start=None: {
            "prev_pid": pid, "host_local": True,
            "termination_attempted": True, "terminated": False,
            "sigkill": True,
        })
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="manual-hold", assignee="worker")
        result = kb.dispatch_once(conn)
        assert [s[0] for s in result.spawned] == [tid]
        task = kb.get_task(conn, tid)
        try:
            assert kb.reclaim_task(conn, tid, reason="operator") is False, (
                "reclaim must refuse while the worker may be alive")
            after = kb.get_task(conn, tid)
            assert after.status == "running"
            assert after.worker_pid == task.worker_pid
            run = _latest_run(conn, tid)
            assert run["ended_at"] is None
            ev = conn.execute(
                "SELECT payload FROM task_events WHERE task_id = ? AND "
                "kind = 'reclaim_deferred' ORDER BY id DESC LIMIT 1",
                (tid,)).fetchone()
            assert ev is not None
            assert json.loads(ev["payload"])["reason"] == (
                "manual_reclaim_worker_alive")
        finally:
            from gateway.status import terminate_pid

            try:
                terminate_pid(int(task.worker_pid), force=True)
            except OSError:
                pass


def test_stale_reclaim_initiator_matches_authorizing_branch(
    observer_on, tmp_path, monkeypatch
):
    """I-7: ``release_stale_claims`` must record the cause of the branch
    that AUTHORIZED the action — heartbeat staleness vs claim-TTL expiry
    are different causes and must not collapse into ``claim_ttl``."""
    import hermes_cli.profiles as profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)

    with kb.connect() as conn:
        # Case A: pid ALIVE but heartbeat stale — heartbeat_stale initiator.
        script = _fake_worker(tmp_path, 0, sleep_s=30)
        monkeypatch.setattr(
            kb, "_resolve_hermes_argv", lambda: [sys.executable, str(script)])
        tid_a = kb.create_task(conn, title="hb-stale", assignee="worker")
        result = kb.dispatch_once(conn)
        assert [s[0] for s in result.spawned] == [tid_a]
        task_a = kb.get_task(conn, tid_a)
        run_a = task_a.current_run_id
        now = int(time.time())
        conn.execute(
            "UPDATE tasks SET claim_expires = ?, last_heartbeat_at = ? "
            "WHERE id = ?", (now - 10, now - 7200, tid_a))
        conn.commit()
        kb.release_stale_claims(conn)
        _s, sup = kb.read_supervisor_record(tid_a, run_id=run_a)
        assert _s == "valid"
        assert sup["termination_initiator"] == "heartbeat_stale"

        # Case B: pid DEAD, no heartbeat, TTL expired — claim_ttl initiator.
        script_b = _fake_worker(tmp_path, 3)
        monkeypatch.setattr(
            kb, "_resolve_hermes_argv",
            lambda: [sys.executable, str(script_b)])
        tid_b = kb.create_task(conn, title="ttl", assignee="worker")
        result = kb.dispatch_once(conn)
        assert tid_b in [s[0] for s in result.spawned]
        task_b = kb.get_task(conn, tid_b)
        run_b = task_b.current_run_id
        _wait_pid_gone(task_b.worker_pid)
        conn.execute(
            "UPDATE tasks SET claim_expires = ?, last_heartbeat_at = NULL "
            "WHERE id = ?", (int(time.time()) - 10, tid_b))
        conn.commit()
        kb.release_stale_claims(conn)
        _s, sup = kb.read_supervisor_record(tid_b, run_id=run_b)
        assert _s == "valid"
        assert sup["termination_initiator"] == "claim_ttl"


def test_gate_off_launch_is_byte_identical_legacy_contract(
    kanban_home, tmp_path, monkeypatch
):
    """I-12 equivalence proof: with the gate OFF, both production routes
    take ``_direct_popen_spec`` — the SAME path POSIX always takes — and
    must reproduce the exact pre-observer ``Popen`` contract (argv, cwd,
    env identity, DEVNULL stdin, log handle mode/buffering, STDOUT merge,
    session flag, creationflags). Pinned against the bodies removed from
    ``_default_spawn`` / ``_spawn_claude_plan_worker`` at f3c133285."""
    import io

    calls = []

    class _Stub:
        pid = 4242

    def fake_popen(argv, **kw):
        calls.append((list(argv), kw))
        return _Stub()

    import hermes_cli.profiles as profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    monkeypatch.delenv(kb.WINDOWS_EXIT_OBSERVER_ENV, raising=False)
    script = _fake_worker(tmp_path, 0)
    monkeypatch.setattr(
        kb, "_resolve_hermes_argv", lambda: [sys.executable, str(script)])

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="legacy-hermes", assignee="worker")
        task = kb.claim_task(conn, tid)
        monkeypatch.setattr(kb.subprocess, "Popen", fake_popen)
        pid = kb._default_spawn(task, str(tmp_path))
        assert pid == 4242, "gate-off spawn must return the bare pid"
        argv, kw = calls[-1]
        assert argv[0] == sys.executable
        assert kw["stdin"] is subprocess.DEVNULL
        assert kw["stderr"] is subprocess.STDOUT
        assert kw["start_new_session"] is True
        assert kw["creationflags"] == (
            subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        log = kw["stdout"]
        try:
            assert isinstance(log, io.BufferedWriter), (
                "hermes route log: open(path, 'ab') default buffering")
            assert log.mode == "ab"
            assert Path(log.name).name == f"{tid}.log"
        finally:
            log.close()

        tid2 = kb.create_task(conn, title="legacy-claude", assignee="worker")
        task2 = kb.claim_task(conn, tid2)
        monkeypatch.setattr(kb, "claude_cli_path", lambda: sys.executable)
        env2 = {"LEGACY_KEY": "legacy-value"}
        pid2 = kb._spawn_claude_plan_worker(task2, str(tmp_path), env2, "p")
        assert pid2 == 4242
        argv2, kw2 = calls[-1]
        assert argv2[0] == sys.executable and argv2[1] == "-p"
        assert kw2["cwd"] == str(tmp_path)
        assert kw2["env"] == env2, "claude route env passed verbatim"
        assert kw2["stdin"] is subprocess.DEVNULL
        assert kw2["stderr"] is subprocess.STDOUT
        assert kw2.get("start_new_session", False) is False, (
            "claude route never used start_new_session")
        log2 = kw2["stdout"]
        try:
            assert isinstance(log2, io.FileIO), (
                "claude route log: open(path, 'ab', buffering=0) unbuffered")
            assert Path(log2.name).name == f"{tid2}.log"
        finally:
            log2.close()


# ===========================================================================
# Mutation guards — source-level invariants
# ===========================================================================
def test_production_routes_cannot_bypass_the_launch_primitive():
    """AST guard: neither production route may grow a direct
    ``subprocess.Popen`` again; both must call ``_spawn_worker_spec``."""
    import ast
    import inspect

    src = inspect.getsource(kb)
    tree = ast.parse(src)
    routes = {"_default_spawn": False, "_spawn_claude_plan_worker": False}
    for node in ast.walk(tree):
        if (isinstance(node, ast.FunctionDef)
                and node.name in routes):
            calls_primitive = False
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                fn = call.func
                if (isinstance(fn, ast.Attribute) and fn.attr == "Popen"):
                    raise AssertionError(
                        f"{node.name} grew a direct subprocess.Popen — all "
                        "worker launches must go through _spawn_worker_spec")
                if (isinstance(fn, ast.Name)
                        and fn.id == "_spawn_worker_spec"):
                    calls_primitive = True
            routes[node.name] = calls_primitive
    assert all(routes.values()), (
        f"production routes missing _spawn_worker_spec call: {routes}")


def test_dispatch_tick_reconciles_before_crash_detection(observer_on,
                                                         monkeypatch):
    """EXECUTABLE-order guard: through a REAL ``dispatch_once``,
    definition-module spies prove the reconciler actually runs, and runs
    before crash detection. (Replaces the old source-string index check,
    which proved text order, not reachability.)"""
    order = []
    real_rec = kb.reconcile_windows_exit_receipts
    real_det = kb.detect_crashed_workers

    def rec_spy(conn, **kw):
        order.append("reconcile")
        return real_rec(conn, **kw)

    def det_spy(conn, **kw):
        order.append("detect")
        return real_det(conn, **kw)

    monkeypatch.setattr(kb, "reconcile_windows_exit_receipts", rec_spy)
    monkeypatch.setattr(kb, "detect_crashed_workers", det_spy)
    with kb.connect() as conn:
        kb.dispatch_once(conn)
    assert "reconcile" in order, "tick never executed the reconciler"
    assert "detect" in order, "tick never executed crash detection"
    assert order.index("reconcile") < order.index("detect"), (
        "reconciler must run BEFORE crash detection so crashes consume "
        "normalized observations")


def test_every_new_seam_has_exact_production_callers():
    """Production Reachability Gate #1: AST CALL GRAPH, not text counts.

    Each new seam binds an EXACT approved caller map — enclosing production
    function -> number of call expressions. Definitions, comments,
    docstrings, and dead strings cannot count; a removed production call, a
    moved call, and an unreviewed extra caller ALL fail. (Replaces the
    ``source.count(needle) >=`` guard, which counted text, not callers —
    Hermes I-11.)
    """
    import ast
    import inspect
    from collections import Counter

    src = inspect.getsource(kb)
    tree = ast.parse(src)
    approved = {
        "_spawn_worker_spec": {
            "_default_spawn": 1, "_spawn_claude_plan_worker": 1},
        "_direct_popen_spec": {"_spawn_worker_spec": 1},
        "_spawn_windows_exit_observer": {"_spawn_worker_spec": 1},
        # ready loop + review loop, both inside _dispatch_once_locked
        "_set_worker_process_identity": {"_dispatch_once_locked": 2},
        "_observed_worker_exit": {"detect_crashed_workers": 1},
        "reconcile_windows_exit_receipts": {"_dispatch_once_locked": 1},
        "_spawn_recovery_observer": {"reconcile_windows_exit_receipts": 1},
        # canonical writer: crash common path + late-receipt reconciler +
        # the platform-gated initiated-termination helper
        "write_supervisor_observation": {
            "_record_initiated_termination": 1,
            "detect_crashed_workers": 1,
            "reconcile_windows_exit_receipts": 1,
        },
        # tier-1 initiated causes: every reclaim/timeout/archive seam
        "_record_initiated_termination": {
            "archive_task": 1, "detect_stale_running": 1,
            "enforce_max_runtime": 1, "reclaim_task": 1,
            "release_stale_claims": 1,
        },
        # shared terminator + survival hold at every reclaim seam
        "_terminate_reclaimed_worker": {
            "detect_stale_running": 1, "enforce_max_runtime": 1,
            "reclaim_task": 1, "release_stale_claims": 1,
        },
        "_worker_survived_termination": {
            "detect_stale_running": 1, "enforce_max_runtime": 1,
            "reclaim_task": 1, "release_stale_claims": 1,
        },
    }
    calls = {name: Counter() for name in approved}
    observer_argv_strings = Counter()

    class _Visitor(ast.NodeVisitor):
        def __init__(self):
            self.stack = ["<module>"]

        def visit_FunctionDef(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node):
            fn = node.func
            name = (fn.id if isinstance(fn, ast.Name)
                    else fn.attr if isinstance(fn, ast.Attribute) else None)
            if name in calls:
                calls[name][self.stack[-1]] += 1
            self.generic_visit(node)

        def visit_Constant(self, node):
            if node.value == "hermes_cli.kanban_exit_observer":
                observer_argv_strings[self.stack[-1]] += 1
            self.generic_visit(node)

    _Visitor().visit(tree)

    for target, want in approved.items():
        got = dict(calls[target])
        assert got == want, (
            f"{target}: caller map {got} != approved {want} — a production "
            "call site was removed, moved, or added without review "
            "(reachability HOLD)")
    assert dict(observer_argv_strings) == {
        "_spawn_windows_exit_observer": 1,
        "_spawn_recovery_observer": 1,
    }, "observer module argv must be built by exactly the two launchers"
