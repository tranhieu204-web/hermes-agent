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

    The spy on ``write_supervisor_observation`` (definition module) proves
    the canonical writer is reached in the COMMON path for every branch —
    moving it back behind one branch fails the 0 and 75 cases (mutation
    guard #5 of the design)."""
    spy_calls = []
    real_writer = kb.write_supervisor_observation

    def spying_writer(task_id, **kwargs):
        spy_calls.append((task_id, kwargs.get("run_id"),
                          kwargs.get("kind"), kwargs.get("code")))
        return real_writer(task_id, **kwargs)

    monkeypatch.setattr(kb, "write_supervisor_observation", spying_writer)

    with kb.connect() as conn:
        tid, task = _spawn_via_production_route(
            conn, monkeypatch, tmp_path, exit_code
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

        crashed = kb.detect_crashed_workers(conn)
        if expected_kind == "rate_limited":
            assert tid not in crashed
            assert tid in kb.detect_crashed_workers._last_rate_limited
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
    the mechanical code without rewriting timed_out into nonzero_exit."""
    import hermes_cli.profiles as profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    script = _fake_worker(tmp_path, 0, sleep_s=120)
    monkeypatch.setattr(
        kb, "_resolve_hermes_argv", lambda: [sys.executable, str(script)]
    )
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="timeout-precedence", assignee="worker",
            max_runtime_seconds=1,
        )
        result = kb.dispatch_once(conn)
        assert [s[0] for s in result.spawned] == [tid]
        task = kb.get_task(conn, tid)
        run_id = task.current_run_id
        worker_pid = task.worker_pid
        time.sleep(1.2)
        timed_out = kb.enforce_max_runtime(conn)
        assert tid in timed_out
        _wait_pid_gone(worker_pid)

        # The initiated record exists and owns the cause.
        _s, sup = kb.read_supervisor_record(tid, run_id=run_id)
        assert _s == "valid"
        assert sup["termination_initiator"] == "max_runtime"
        assert sup["cause"] == "timed_out"

        # Wait for the observer's final receipt, then reconcile (as the
        # next dispatch tick would).
        deadline = time.time() + 20
        while time.time() < deadline:
            state, rec = kb.read_exit_receipt(
                kb.exit_receipt_path(tid, run_id=run_id))
            if state == "exited":
                break
            time.sleep(0.1)
        assert state == "exited"
        kb.reconcile_windows_exit_receipts(conn)

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
        "observer_pid_start": None,
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
])
def test_receipt_exact_schema_rejections(logdir, mutation, reason):
    path = _write_receipt(logdir, _mk_receipt(**mutation))
    state, rec = kb.read_exit_receipt(path)
    assert state == "invalid"
    assert rec["reason"] == reason
    assert rec["_sha256"], "invalid receipts keep their hash as evidence"


def test_exited_receipt_requires_int_code_not_bool(logdir):
    path = _write_receipt(logdir, _final(_mk_receipt(), code=True))
    state, rec = kb.read_exit_receipt(path)
    assert state == "invalid"
    assert rec["reason"] == "missing_exit_code"


@pytest.mark.parametrize("field,value,expected", [
    ("task_id", "t_OTHER", "task_id"),
    ("run_id", "4", "run_id"),
    ("launch_id", "b" * 32, "launch_id"),
    ("claim_lock_sha256", kb._claim_lock_sha256("stolen"), "claim_lock"),
    ("host_id", "otherhost", "host_id"),
    ("worker_pid", 999, "worker_pid"),
    ("worker_pid_start", 888, "worker_pid_start"),
])
def test_receipt_identity_mismatches_each_rejected(logdir, field, value,
                                                   expected):
    rec = _final(_mk_receipt(**{field: value}))
    mismatch = kb._receipt_identity_mismatch(
        rec, task_id="t_x", run_id="3", launch_id="a" * 32,
        claim_lock="lock", worker_pid=4242, worker_pid_start=777,
    )
    assert mismatch == expected


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
    consumed by) the next writer — unique per-writer temp names."""
    from hermes_cli import kanban_exit_observer as obs

    final = tmp_path / "r.json"
    stale = tmp_path / f"r.json.{'c' * 32}.9999.2.tmp"
    stale.write_text("{half json", encoding="utf-8")
    obs._atomic_write_receipt(
        str(final), {"ok": True}, launch_id="a" * 32, sequence=2)
    assert json.loads(final.read_text(encoding="utf-8")) == {"ok": True}
    assert stale.read_text(encoding="utf-8") == "{half json"


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


def test_dispatch_tick_reconciles_before_crash_detection():
    """Source-order guard: the reconciler must run before
    detect_crashed_workers in the dispatch tick."""
    import inspect

    src = inspect.getsource(kb._dispatch_once_locked)
    assert "reconcile_windows_exit_receipts(conn)" in src
    assert src.index("reconcile_windows_exit_receipts(conn)") < src.index(
        "result.crashed = detect_crashed_workers(conn)")


def test_every_new_seam_has_nontest_production_callers():
    """Production Reachability Gate #1 (static): each new seam must keep at
    least the enumerated NON-TEST callers inside the production module.
    Zero non-test callers = automatic HOLD — six features this session were
    built correctly and never reached production; this guard fails the
    seventh at commit time.

    Enumeration (non-test call sites in hermes_cli/kanban_db.py):
      _spawn_worker_spec            <- _default_spawn, _spawn_claude_plan_worker
      _spawn_windows_exit_observer  <- _spawn_worker_spec
      _set_worker_process_identity  <- ready + review dispatch loops
      _observed_worker_exit         <- detect_crashed_workers
      write_supervisor_observation  <- detect_crashed_workers,
                                       enforce_max_runtime,
                                       release_stale_claims,
                                       detect_stale_running, reclaim_task,
                                       archive_task (+ reconciler)
      reconcile_windows_exit_receipts <- _dispatch_once_locked
      kanban_exit_observer module   <- observer argv in
                                       _spawn_windows_exit_observer and
                                       _spawn_recovery_observer
    """
    import inspect

    src = inspect.getsource(kb)
    minimum_callers = {
        "_spawn_worker_spec(": 3,             # def + 2 production routes
        "_spawn_windows_exit_observer(": 2,   # def + launch primitive
        "_set_worker_process_identity(": 3,   # def + ready + review loops
        "_observed_worker_exit(": 2,          # def + detect_crashed_workers
        "write_supervisor_observation(": 8,   # def + 7 production writers
        "reconcile_windows_exit_receipts(": 2,  # def + dispatch tick
        "hermes_cli.kanban_exit_observer": 2,   # primary + recovery argv
    }
    for needle, expected in minimum_callers.items():
        found = src.count(needle)
        assert found >= expected, (
            f"{needle} appears {found}x, expected >= {expected} — a "
            "production call site was removed (reachability HOLD)")
