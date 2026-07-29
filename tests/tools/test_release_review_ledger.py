import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from tools.release_review_ledger import ReleaseReviewLedger
from tools.release_review_launch import launch_async_review, launch_shell_review


def _preflight():
    ready = {"status": "verified", "evidence": "captured"}
    return {
        "target": dict(ready),
        "install": dict(ready),
        "restart": dict(ready),
        "rollback": dict(ready),
        "health": {**ready, "authenticated": True, "method": "token probe", "endpoint": "/health"},
    }


def _request(**overrides):
    request = {
        "candidate_hash": "a" * 64,
        "scope": "runtime and tests",
        "lane": "codex",
        "model": "m",
        "prompt": "p",
        "deadline_seconds": 60,
        "output_path": "out.json",
    }
    request.update(overrides)
    return request


def test_same_review_returns_existing_receipt(tmp_path):
    ledger = ReleaseReviewLedger(tmp_path / "reviews.db")
    first = ledger.admit(**_request(scope=" runtime   and tests "))
    second = ledger.admit(**_request())
    assert first["status"] == "admitted"
    assert second["status"] == "existing"
    assert second["receipt_id"] == first["receipt_id"]


def test_variant_output_or_deadline_is_conflict_not_silent_reuse(tmp_path):
    ledger = ReleaseReviewLedger(tmp_path / "reviews.db")
    ledger.admit(**_request())
    output_variant = ledger.admit(**_request(output_path="other.json"))
    deadline_variant = ledger.admit(**_request(deadline_seconds=120))
    assert output_variant["status"] == "conflict"
    assert deadline_variant["status"] == "conflict"


def test_conflicting_requested_id_is_rejected(tmp_path):
    ledger = ReleaseReviewLedger(tmp_path / "reviews.db")
    ledger.admit(**_request(receipt_id="r"))
    conflict = ledger.admit(**_request(candidate_hash="b" * 64, receipt_id="r"))
    assert conflict["status"] == "conflict"
    assert conflict["reason"] == "receipt_id_reused"


@pytest.mark.parametrize("deadline", [0, -1, float("inf"), 3601])
def test_invalid_deadlines_are_rejected(tmp_path, deadline):
    ledger = ReleaseReviewLedger(tmp_path / "reviews.db")
    with pytest.raises(ValueError):
        ledger.admit(**_request(deadline_seconds=deadline))


def test_preflight_requires_verifiable_controls_and_authenticated_health(tmp_path):
    ledger = ReleaseReviewLedger(tmp_path / "reviews.db")
    receipt = ledger.admit(**_request())
    with pytest.raises(ValueError):
        ledger.capture_preflight(receipt["receipt_id"], {key: "verified" for key in _preflight()})
    incomplete = _preflight()
    incomplete["health"]["authenticated"] = False
    with pytest.raises(ValueError):
        ledger.capture_preflight(receipt["receipt_id"], incomplete)
    ledger.capture_preflight(receipt["receipt_id"], _preflight())


def test_claim_is_atomic_and_second_claim_cannot_launch(tmp_path):
    ledger = ReleaseReviewLedger(tmp_path / "reviews.db")
    receipt = ledger.admit(**_request())
    ledger.capture_preflight(receipt["receipt_id"], _preflight())
    assert ledger.claim_launch(receipt["receipt_id"])["status"] == "claimed"
    assert ledger.claim_launch(receipt["receipt_id"])["state"] == "launching"
    ledger.attach_processes(receipt["receipt_id"], 101, 102, "pid:101")
    with pytest.raises(RuntimeError):
        ledger.attach_processes(receipt["receipt_id"], 201, 202, "pid:201")
    with sqlite3.connect(tmp_path / "reviews.db") as conn:
        assert conn.execute("SELECT root_pid, leaf_pid, state FROM release_review_receipts").fetchone() == (101, 102, "running")


def test_claim_does_not_consume_review_timebox_before_attachment(tmp_path):
    ledger = ReleaseReviewLedger(tmp_path / "reviews.db")
    receipt = ledger.admit(**_request())
    ledger.capture_preflight(receipt["receipt_id"], _preflight())
    assert ledger.expire_due(now=time.time() + 61) == 0
    assert ledger.claim_launch(receipt["receipt_id"])["state"] == "launching"


def test_unknown_receipt_cannot_be_updated(tmp_path):
    ledger = ReleaseReviewLedger(tmp_path / "reviews.db")
    with pytest.raises(KeyError):
        ledger.attach_processes("missing", 1, 2, "pid:1")


def test_findings_are_append_only_and_produce_incremental_scope(tmp_path):
    ledger = ReleaseReviewLedger(tmp_path / "reviews.db")
    receipt = ledger.admit(**_request())
    ledger.capture_preflight(receipt["receipt_id"], _preflight())
    ledger.claim_launch(receipt["receipt_id"])
    ledger.attach_processes(receipt["receipt_id"], 1, 1, "pid:1")
    ledger.append_findings(receipt["receipt_id"], [{"finding_id": "one", "files": ["a.py"], "tests": ["test_a"], "disposition": "open"}])
    ledger.append_findings(receipt["receipt_id"], [{"finding_id": "two", "files": ["b.py", "a.py"], "tests": ["test_b"], "disposition": "fixed"}])
    assert ledger.incremental_scope(receipt["receipt_id"]) == {"files": ["a.py", "b.py"], "tests": ["test_a", "test_b"]}
    with pytest.raises(ValueError):
        ledger.append_findings(receipt["receipt_id"], [{"finding_id": "one", "files": ["a.py"], "tests": ["test_a"]}])


def test_direct_shell_launch_reuses_receipt_without_second_process(tmp_path):
    ledger = ReleaseReviewLedger(tmp_path / "reviews.db")
    calls = []

    class Process:
        pid = 42

        def poll(self):
            return 0

    def popen(command, **kwargs):
        calls.append((command, kwargs))
        return Process()

    args = {**_request(), "preflight": _preflight(), "command": ["reviewer", "--read-only"]}
    first = launch_shell_review(ledger, popen=popen, restart_recovery_mode="current_process_only", **args)
    second = launch_shell_review(ledger, popen=popen, restart_recovery_mode="current_process_only", **args)
    assert first["status"] == "launched"
    assert first["root_pid"] == 42
    assert second["status"] == "existing"
    assert len(calls) == 1
    assert calls[0][1]["shell"] is False


def test_direct_shell_requires_explicit_non_durable_restart_mode(tmp_path):
    ledger = ReleaseReviewLedger(tmp_path / "reviews.db")
    called = False

    def popen(*_args, **_kwargs):
        nonlocal called
        called = True

    for mode in (None, "durable"):
        result = launch_shell_review(
            ledger, **_request(), preflight=_preflight(), command=["reviewer"],
            popen=popen, restart_recovery_mode=mode,
        )
        assert result["status"] == "rejected"
        assert "durable restart recovery is unsupported" in result["error"]
    assert called is False


def test_direct_attachment_restarts_deadline_from_successful_attach(tmp_path):
    ledger = ReleaseReviewLedger(tmp_path / "reviews.db")
    receipt = ledger.admit(**_request(deadline_seconds=60))
    ledger.capture_preflight(receipt["receipt_id"], _preflight())
    ledger.claim_launch(receipt["receipt_id"])
    ledger.attach_processes(receipt["receipt_id"], 91, 91, "pid:91:single-process")
    with sqlite3.connect(tmp_path / "reviews.db") as conn:
        deadline_at, updated_at, deadline_seconds, state = conn.execute(
            "SELECT deadline_at, updated_at, deadline_seconds, state FROM release_review_receipts WHERE receipt_id=?",
            (receipt["receipt_id"],),
        ).fetchone()
    assert state == "running"
    assert deadline_at == pytest.approx(updated_at + deadline_seconds, abs=0.01)


def test_direct_expiry_between_spawn_and_attach_is_terminal_not_an_error(tmp_path):
    class ExpiringLedger(ReleaseReviewLedger):
        def attach_processes(self, receipt_id, root_pid, leaf_pid, launch_handle):
            with sqlite3.connect(self.path) as conn:
                conn.execute(
                    "UPDATE release_review_receipts SET state='timebox_expired' WHERE receipt_id=?", (receipt_id,)
                )
            return super().attach_processes(receipt_id, root_pid, leaf_pid, launch_handle)

    class Process:
        pid = 92
        terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

    ledger = ExpiringLedger(tmp_path / "reviews.db")
    process = Process()
    result = launch_shell_review(
        ledger, **_request(), preflight=_preflight(), command=["reviewer"],
        popen=lambda *_args, **_kwargs: process, restart_recovery_mode="current_process_only",
    )
    assert result["status"] == "timebox_expired"
    assert process.terminated is True
    with sqlite3.connect(tmp_path / "reviews.db") as conn:
        state, terminal = conn.execute(
            "SELECT state, terminal_json FROM release_review_receipts WHERE receipt_id=?", (result["receipt_id"],)
        ).fetchone()
    assert state == "timebox_expired"
    assert "receipt expired before direct attachment" in terminal


def test_direct_launcher_records_timebox_before_terminating_its_own_process(tmp_path, monkeypatch):
    ledger = ReleaseReviewLedger(tmp_path / "reviews.db")

    class Process:
        pid = 77
        terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

    process = Process()
    callbacks = []
    monkeypatch.setattr(ledger, "supervise_deadline", lambda _receipt_id, callback: callbacks.append(callback))
    result = launch_shell_review(
        ledger,
        **_request(deadline_seconds=60),
        preflight=_preflight(),
        command=["reviewer"],
        popen=lambda *_args, **_kwargs: process,
        restart_recovery_mode="current_process_only",
    )
    assert ledger.deadline_watch_state(result["receipt_id"], now=time.time() + 61) == "expired"
    callbacks[0]()
    assert process.terminated is True
    with sqlite3.connect(tmp_path / "reviews.db") as conn:
        state, terminal = conn.execute(
            "SELECT state, terminal_json FROM release_review_receipts WHERE receipt_id=?", (result["receipt_id"],)
        ).fetchone()
    assert state == "timebox_expired"
    assert "direct review deadline" in terminal


def test_direct_shell_normal_exit_records_terminal_evidence_before_deadline(tmp_path):
    ledger = ReleaseReviewLedger(tmp_path / "reviews.db")

    class Process:
        pid = 78

        def poll(self):
            return 0

        def terminate(self):
            raise AssertionError("completed reviewer must not be terminated")

    result = launch_shell_review(
        ledger, **_request(deadline_seconds=1), preflight=_preflight(), command=["reviewer"],
        popen=lambda *_args, **_kwargs: Process(), restart_recovery_mode="current_process_only",
    )
    deadline = time.monotonic() + 1
    terminal = None
    while terminal is None and time.monotonic() < deadline:
        with sqlite3.connect(tmp_path / "reviews.db") as conn:
            terminal = conn.execute("SELECT state, terminal_json FROM release_review_receipts WHERE receipt_id=?", (result["receipt_id"],)).fetchone()
        if terminal[0] != "completed":
            terminal = None
            time.sleep(0.01)
    assert terminal[0] == "completed"
    assert "return_code" in terminal[1]
    assert ledger.deadline_watch_state(result["receipt_id"]) == "terminal"


def test_simultaneous_timeboxes_terminate_each_reviewers_own_process(tmp_path):
    ledger = ReleaseReviewLedger(tmp_path / "reviews.db")

    class Process:
        def __init__(self, pid):
            self.pid = pid
            self.terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

    first, second = Process(81), Process(82)
    processes = iter((first, second))
    for candidate in ("a" * 64, "b" * 64):
        launch_shell_review(
            ledger,
            **_request(candidate_hash=candidate, deadline_seconds=0.1),
            preflight=_preflight(),
            command=["reviewer"],
            popen=lambda *_args, **_kwargs: next(processes),
            restart_recovery_mode="current_process_only",
        )
    deadline = time.monotonic() + 1
    while not (first.terminated and second.terminated) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert first.terminated is True
    assert second.terminated is True


def test_async_launch_uses_one_receipt_and_preserves_rejected_state(tmp_path):
    ledger = ReleaseReviewLedger(tmp_path / "reviews.db")
    calls = []

    def dispatch(**kwargs):
        calls.append(kwargs)
        return {"status": "rejected", "error": "capacity"}

    args = {**_request(), "preflight": _preflight(), "dispatch": dispatch, "dispatch_kwargs": {"goal": "review"}}
    first = launch_async_review(ledger, **args)
    second = launch_async_review(ledger, **args)
    assert first["status"] == "rejected"
    assert second["status"] == "existing"
    assert second["state"] == "launch_rejected"
    assert len(calls) == 0


def test_async_launcher_uses_the_real_async_delegation_rail(tmp_path, monkeypatch):
    """The adapter must protect the same dispatcher Hermes uses in production."""
    from tools import async_delegation as ad

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    ad._reset_for_tests()
    try:
        ledger = ReleaseReviewLedger(tmp_path / "reviews.db")
        result = launch_async_review(
            ledger,
            **_request(),
            preflight=_preflight(),
            dispatch=ad.dispatch_async_delegation,
            dispatch_kwargs={
                "goal": "read-only review",
                "context": "frozen candidate",
                "toolsets": None,
                "role": "reviewer",
                "model": "m",
                "session_key": "test",
                "runner": lambda: {"status": "completed", "summary": "done"},
                "interrupt_fn": lambda: None,
                "max_async_children": 1,
            },
        )
        assert result["status"] == "launched"
        assert result["dispatch"]["status"] == "dispatched"
        assert result["dispatch"]["review_receipt_id"] == result["receipt_id"]
        assert result["root_pid"] > 0
        assert result["leaf_pid"] is None
        deadline = time.monotonic() + 2
        while ad.active_count() and time.monotonic() < deadline:
            time.sleep(0.01)
        with sqlite3.connect(tmp_path / "reviews.db") as conn:
            state, terminal = conn.execute(
                "SELECT state, terminal_json FROM release_review_receipts WHERE receipt_id=?",
                (result["receipt_id"],),
            ).fetchone()
        assert state == "completed"
        assert "delegation_id" in terminal
    finally:
        deadline = time.monotonic() + 2
        while ad.active_count() and time.monotonic() < deadline:
            time.sleep(0.01)
        ad._reset_for_tests()


def test_async_timeout_records_terminal_outcome_when_interrupt_is_noop(tmp_path, monkeypatch):
    """A non-cooperative thread cannot revive a receipt after its timebox."""
    from tools import async_delegation as ad

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    ad._reset_for_tests()
    gate = threading.Event()
    try:
        ledger = ReleaseReviewLedger(tmp_path / "reviews.db")
        result = launch_async_review(
            ledger,
            **_request(),
            preflight=_preflight(),
            dispatch=ad.dispatch_async_delegation,
            dispatch_kwargs={
                "goal": "read-only review",
                "context": "frozen candidate",
                "toolsets": None,
                "role": "reviewer",
                "model": "m",
                "session_key": "test",
                "runner": lambda: (gate.wait(2), {"status": "completed", "summary": "late"})[1],
                "interrupt_fn": lambda: None,
                "max_async_children": 1,
            },
        )
        assert result["status"] == "launched"
        assert ad.force_timeout_review_receipt(result["receipt_id"]) == 1
        with sqlite3.connect(tmp_path / "reviews.db") as conn:
            state, terminal = conn.execute(
                "SELECT state, terminal_json FROM release_review_receipts WHERE receipt_id=?",
                (result["receipt_id"],),
            ).fetchone()
        assert state == "timebox_expired"
        assert "timebox_expired" in terminal
        gate.set()
        deadline = time.monotonic() + 2
        while ad.active_count() and time.monotonic() < deadline:
            time.sleep(0.01)
        with sqlite3.connect(tmp_path / "reviews.db") as conn:
            assert conn.execute(
                "SELECT state FROM release_review_receipts WHERE receipt_id=?", (result["receipt_id"],)
            ).fetchone()[0] == "timebox_expired"
    finally:
        gate.set()
        deadline = time.monotonic() + 2
        while ad.active_count() and time.monotonic() < deadline:
            time.sleep(0.01)
        ad._reset_for_tests()


def test_direct_async_dispatch_refuses_unclaimed_review_receipt(tmp_path):
    from tools import async_delegation as ad

    rejected = ad.dispatch_async_delegation(
        goal="review", context=None, toolsets=None, role="reviewer", model="m", session_key="test",
        runner=lambda: {"status": "completed"}, interrupt_fn=lambda: None,
        review_receipt_id="not-claimed", review_ledger_path=str(tmp_path / "reviews.db"),
    )
    assert rejected["status"] == "rejected"
    assert "not admitted" in rejected["error"]


def test_async_adapter_never_dispatches_existing_or_conflicting_receipt(tmp_path):
    ledger = ReleaseReviewLedger(tmp_path / "reviews.db")
    calls = []

    def dispatch(**kwargs):
        calls.append(kwargs)
        return {"status": "dispatched", "delegation_id": "deleg_test"}

    args = {**_request(), "preflight": _preflight(), "dispatch": dispatch,
            "dispatch_kwargs": {"goal": "review", "interrupt_fn": lambda: None}}
    first = launch_async_review(ledger, **args)
    existing = launch_async_review(ledger, **args)
    conflict = launch_async_review(ledger, **{**args, "output_path": "other-output"})
    assert first["status"] == "launched"
    assert existing["status"] == "existing"
    assert conflict["status"] == "conflict"
    assert len(calls) == 1


def test_separate_process_same_identity_allows_one_claim(tmp_path):
    db = tmp_path / "reviews.db"
    source = (
        "from pathlib import Path\n"
        "from tools.release_review_ledger import ReleaseReviewLedger\n"
        f"l=ReleaseReviewLedger(Path(r'{db}'))\n"
        "r=l.admit(candidate_hash='a'*64,scope='runtime',lane='codex',model='m',prompt='p',deadline_seconds=60,output_path='out')\n"
        "if r['status']=='admitted':\n l.capture_preflight(r['receipt_id'], {'target':{'status':'verified','evidence':'x'},'install':{'status':'verified','evidence':'x'},'restart':{'status':'verified','evidence':'x'},'rollback':{'status':'verified','evidence':'x'},'health':{'status':'verified','evidence':'x','authenticated':True,'method':'probe','endpoint':'/health'}})\n print(l.claim_launch(r['receipt_id'])['status'])\n"
        "else:\n print(r['status'])\n"
    )
    proc = [sys.executable, "-c", source]
    first = subprocess.Popen(proc, cwd=Path(__file__).resolve().parents[2], stdout=subprocess.PIPE, text=True)
    second = subprocess.Popen(proc, cwd=Path(__file__).resolve().parents[2], stdout=subprocess.PIPE, text=True)
    outputs = {first.communicate(timeout=10)[0].strip(), second.communicate(timeout=10)[0].strip()}
    assert outputs == {"claimed", "existing"}
