"""Detached Windows exit observer for kanban workers.

The dispatcher on native Windows cannot classify worker exits: it discards the
``Popen`` handle at spawn (`` _default_spawn`` / ``_spawn_claude_plan_worker``
return only ``proc.pid``), ``reap_worker_zombies`` is a documented no-op, and
``_classify_worker_exit`` reads a POSIX-only wait-status registry. Live
consequence: 70/70 crashed runs recorded as ``pid <N> not alive``.

This module is that missing producer. The dispatcher launches it detached
(``CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW | CREATE_BREAKAWAY_FROM_JOB``)
so it survives the routine gateway restart that kills any in-gateway waiter
thread — the failure mode that rules out the smaller in-process design. It:

1. spawns the real worker with the exact argv/cwd/env/log the dispatcher
   built (no shell, no quoting layer),
2. atomically publishes a ``launched`` receipt carrying both PIDs and both
   PID-start fingerprints (the dispatcher's bootstrap handshake),
3. retains the ``Popen`` handle, ``wait()``s, and atomically replaces the
   receipt with the final ``exited`` state carrying the exact return code.

The receipt is RAW process evidence only. This process never writes
``<task>.run<N>.supervisor.json`` — semantic cause (natural death vs
max-runtime vs reclaim) belongs to the dispatcher's reconciler, which runs
under the board's single-writer lock. One canonical writer; see
``kanban_db.reconcile_windows_exit_receipts``.

``--recover`` mode reattaches to an already-running worker after the primary
observer died: it opens the live process with
``SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION``, waits, and publishes the
same launch-bound final receipt. If the handle is denied it reports
``observer_error`` honestly instead of inventing an exit code.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time

EXIT_RECEIPT_SCHEMA = "hermes.kanban.exit-receipt"
EXIT_RECEIPT_VERSION = 1
RECEIPT_SOURCE = "windows_popen_observer"

# Exit codes of the OBSERVER process itself (diagnostic only; the dispatcher
# never trusts them — it trusts the receipt).
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_BOOTSTRAP_FAILED = 3
EXIT_OBSERVER_ERROR = 4


def _utc_now() -> str:
    """UTC RFC3339 with fractional seconds and Z."""
    t = time.time()
    base = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t))
    return f"{base}.{int((t % 1) * 1_000_000):06d}Z"


def _host_id() -> str:
    try:
        return socket.gethostname() or "unknown"
    except Exception:
        return "unknown"


def _boot_id() -> str:
    """Host identity + boot time — deterministic reboot evidence.

    A ``launched`` receipt whose boot_id differs from the reading
    dispatcher's current boot_id proves the run crossed a reboot; the exit
    code is then honestly unknowable (no user-space process survives to
    write it).
    """
    try:
        import psutil

        return f"{_host_id()}:{int(psutil.boot_time())}"
    except Exception:
        return f"{_host_id()}:unknown"


def _pid_start(pid: int):
    """Process-creation fingerprint, matching the dispatcher's helper.

    Must agree with ``gateway.status.get_process_start_time`` (psutil
    ``create_time`` quantized to centiseconds on Windows) or the dispatcher
    will reject the receipt as a PID-reuse suspect.
    """
    try:
        from gateway.status import get_process_start_time

        return get_process_start_time(pid)
    except Exception:
        try:
            import psutil

            return int(round(psutil.Process(pid).create_time() * 100))
        except Exception:
            return None


def _atomic_write_receipt(path: str, payload: dict, *, launch_id: str,
                          sequence: int) -> None:
    """Serialize + fsync + ``os.replace`` with a per-writer unique temp name.

    A fixed ``path + '.tmp'`` collides under overlap (stale temp from a
    killed observer, or a duplicate launch losing the CAS while mid-write).
    The unique name keeps every writer's temp file private; ``os.replace``
    makes the final receipt visible only as a complete JSON document.
    Windows offers no POSIX-style directory fsync; the receipt is atomic
    against concurrent readers, not against power loss — the dispatcher's
    reboot handling covers that honestly via ``boot_id``.
    """
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    tmp = f"{path}.{launch_id}.{os.getpid()}.{sequence}.tmp"
    fd = os.open(
        tmp,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0),
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(blob.encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass


def _base_receipt(args, *, worker_pid, worker_pid_start) -> dict:
    return {
        "schema": EXIT_RECEIPT_SCHEMA,
        "version": EXIT_RECEIPT_VERSION,
        "state": "launched",
        "final": False,
        "sequence": 1,
        "source": RECEIPT_SOURCE,
        "task_id": args.task,
        "run_id": str(args.run),
        "board": args.board,
        "launch_id": args.launch,
        "claim_lock_sha256": args.claim_lock_sha256,
        "command_kind": args.kind,
        "exit_contract": args.exit_contract,
        "host_id": _host_id(),
        "boot_id": _boot_id(),
        "observer_pid": os.getpid(),
        "observer_pid_start": _pid_start(os.getpid()),
        "worker_pid": worker_pid,
        "worker_pid_start": worker_pid_start,
        "launched_at": _utc_now(),
        "observed_at": None,
        "exit_semantics": "windows_process_exit_code",
        "exit_code": None,
        "observer_error": None,
        "observer_error_detail": None,
    }


def _finalize(receipt: dict, *, state: str, exit_code=None,
              error: str = None, detail: str = None) -> dict:
    final = dict(receipt)
    final.update(
        state=state,
        final=True,
        sequence=2,
        observed_at=_utc_now(),
        exit_code=exit_code,
        observer_error=error,
        observer_error_detail=(detail or None) and str(detail)[:500],
    )
    return final


def _terminate_tree(pid: int) -> None:
    """Force-kill a worker AND its descendants.

    ``os.kill`` on Windows reaches only the root; a bootstrap failure that
    killed the root while leaving grandchildren would create exactly the
    untracked-orphan class this wrapper exists to prevent.
    """
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, timeout=15, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="hermes_cli.kanban_exit_observer", add_help=True,
        description="Detached kanban worker exit observer (Windows).",
    )
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--launch", required=True)
    parser.add_argument("--board", required=True)
    parser.add_argument("--kind", required=True,
                        choices=("hermes", "claude_plan"))
    parser.add_argument("--exit-contract", required=True,
                        choices=("hermes_kanban_v1", "generic_process_v1"))
    parser.add_argument("--claim-lock-sha256", required=True)
    parser.add_argument("--log", required=True)
    parser.add_argument("--cwd", default=None)
    parser.add_argument("--child-env-file", default=None)
    parser.add_argument("--recover", action="store_true")
    parser.add_argument("--worker-pid", type=int, default=None)
    parser.add_argument("--worker-pid-start", type=int, default=None)
    if "--" in argv:
        split = argv.index("--")
        args = parser.parse_args(argv[:split])
        child_argv = argv[split + 1:]
    else:
        args = parser.parse_args(argv)
        child_argv = []
    return args, child_argv


def _load_child_env(args):
    """Restore the EXACT env the dispatcher built for the worker.

    The dispatcher must prepend its own package root to the OBSERVER's
    PYTHONPATH so this module is importable, but the worker's module
    resolution must not inherit that mutation: the original
    ``WorkerLaunchSpec.env`` rides along in a sidecar file and is restored
    verbatim before the child ``Popen``. Missing/unreadable sidecar fails
    the bootstrap CLOSED — launching the worker under the observer's
    polluted env would silently change which code the worker imports.
    """
    if not args.child_env_file:
        return None, "child_env_file_missing"
    try:
        with open(args.child_env_file, "r", encoding="utf-8") as fh:
            env = json.load(fh)
    except Exception as exc:
        return None, f"child_env_file_unreadable: {exc}"
    if (not isinstance(env, dict)
            or not all(isinstance(k, str) and isinstance(v, str)
                       for k, v in env.items())):
        return None, "child_env_file_invalid"
    try:
        os.unlink(args.child_env_file)
    except OSError:
        pass
    return env, None


def _run_primary(args, child_argv) -> int:
    if not child_argv:
        print("missing child argv after --", file=sys.stderr)
        return EXIT_USAGE

    child_env, env_error = _load_child_env(args)
    if env_error:
        _write_error_receipt(args, "child_env_unavailable", env_error)
        return EXIT_BOOTSTRAP_FAILED

    log_handle = None
    try:
        log_handle = open(args.log, "ab", buffering=0)
    except OSError as exc:
        # No log sink → the worker's output contract is broken; refuse to
        # launch an unobservable worker rather than silently darken it.
        _write_error_receipt(args, "log_open_failed", str(exc))
        return EXIT_BOOTSTRAP_FAILED

    try:
        child = subprocess.Popen(
            child_argv,
            cwd=args.cwd or None,
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        _write_error_receipt(args, "child_spawn_failed", str(exc))
        return EXIT_BOOTSTRAP_FAILED

    worker_start = _pid_start(child.pid)
    if worker_start is None and child.poll() is None:
        # FAIL CLOSED: a LIVE child without a birth fingerprint can never be
        # identity-bound again (PID reuse becomes unprovable), which would
        # poison every downstream recovery/kill/trust decision. Terminate
        # the child while we still hold the only handle. A child that has
        # ALREADY exited is exempt: its fingerprint is legitimately
        # unreadable post-mortem, and the retained handle (not the PID)
        # is what ``wait()`` collects, so reuse cannot misbind the code.
        _terminate_tree(child.pid)
        try:
            child.wait(timeout=30)
        except Exception:
            pass
        _write_error_receipt(args, "child_fingerprint_unavailable",
                             f"no start fingerprint for live pid {child.pid}")
        return EXIT_BOOTSTRAP_FAILED

    receipt = _base_receipt(
        args, worker_pid=child.pid, worker_pid_start=worker_start,
    )
    try:
        _atomic_write_receipt(args.receipt, receipt,
                              launch_id=args.launch, sequence=1)
    except Exception:
        # Bootstrap-failure rule: if the launched receipt cannot be durably
        # published, the child must NOT keep running untracked. Terminate the
        # tree and let the dispatcher record spawn_failed. Never fall back to
        # an unobserved launch.
        _terminate_tree(child.pid)
        try:
            child.wait(timeout=30)
        except Exception:
            pass
        return EXIT_BOOTSTRAP_FAILED

    try:
        code = child.wait()
    except Exception as exc:
        final = _finalize(receipt, state="observer_error",
                          error="wait_failed", detail=str(exc))
        _try_write_final(args, final)
        return EXIT_OBSERVER_ERROR

    final = _finalize(receipt, state="exited", exit_code=int(code))
    if not _try_write_final(args, final):
        return EXIT_OBSERVER_ERROR
    return EXIT_OK


def _try_write_final(args, final: dict) -> bool:
    """Publish the final receipt; the required order is fsync → replace →
    exit, so a surviving ``launched``-only receipt plus a gone observer is
    deterministically observer loss, never a half-written final state."""
    for attempt in range(5):
        try:
            _atomic_write_receipt(args.receipt, final,
                                  launch_id=args.launch, sequence=2)
            return True
        except Exception:
            time.sleep(0.2 * (attempt + 1))
    return False


def _write_error_receipt(args, error: str, detail: str) -> None:
    receipt = _base_receipt(args, worker_pid=None, worker_pid_start=None)
    final = _finalize(receipt, state="observer_error",
                      error=error, detail=detail)
    try:
        _atomic_write_receipt(args.receipt, final,
                              launch_id=args.launch, sequence=2)
    except Exception:
        pass


def _existing_receipt_blocks_recovery(path: str, args=None) -> str:
    """Return a reason when the on-disk receipt must not be replaced.

    Recovery may write ONLY over an EXACT-SCHEMA-VALID launched-state
    receipt whose identity matches the launch this recovery was dispatched
    for (or over no receipt at all). Everything else is evidence that must
    be preserved: a final receipt is settled; unreadable, unparseable,
    schema-invalid, launched-look-alike, or wrong-identity documents are
    first-class ``observer_invalid``/``conflict`` material for the
    dispatcher — papering over them would erase exactly the bytes those
    handlers exist to examine. Validation is delegated to the dispatcher's
    own reader (``kanban_db.read_exit_receipt``) so the two sides can never
    disagree about what "valid" means; if that validator cannot be loaded,
    recovery fails closed. Returns "" when recovery may proceed.
    """
    try:
        from hermes_cli.kanban_db import read_exit_receipt
    except Exception:
        return "receipt_validator_unavailable"
    state, rec = read_exit_receipt(path)
    if state == "absent":
        # No receipt at all: the identity we were dispatched with is the
        # only binding left; proceeding writes the launch-bound evidence.
        return ""
    if state in ("exited", "observer_error"):
        return "receipt_already_final"
    if state != "launched":
        return f"receipt_{state}_preserved"
    if args is not None:
        for field, expected in (("task_id", args.task),
                                ("run_id", str(args.run)),
                                ("launch_id", args.launch)):
            if rec.get(field) != expected:
                return "receipt_identity_mismatch"
    return ""


def _run_recovery(args) -> int:
    """Reattach to a live worker whose primary observer died.

    The recovery handle pins the process object, so the exit code read after
    the wait belongs to exactly the process whose start fingerprint the
    dispatcher validated before dispatching this recovery observer. The
    dispatcher passes through the ORIGINAL launch's ``--kind`` and
    ``--exit-contract`` (read from the launched receipt), so a recovered
    Claude-route exit 75 stays a generic nonzero exit — recovery re-observes
    a launch, it never re-labels it.
    """
    import ctypes
    from ctypes import wintypes

    if not args.worker_pid:
        print("--recover requires --worker-pid", file=sys.stderr)
        return EXIT_USAGE
    if args.worker_pid_start is None:
        # FAIL CLOSED: without the spawn-time start fingerprint there is no
        # way to prove the PID was not recycled — recovery must never wait
        # on (and publish evidence for) an arbitrary process.
        print("--recover requires --worker-pid-start (identity fail-closed)",
              file=sys.stderr)
        return EXIT_USAGE

    blocked = _existing_receipt_blocks_recovery(args.receipt, args)
    if blocked == "receipt_already_final":
        return EXIT_OK  # settled evidence; nothing to recover
    if blocked:
        print(f"recovery refused: {blocked}", file=sys.stderr)
        return EXIT_OBSERVER_ERROR

    pid = int(args.worker_pid)
    live_start = _pid_start(pid)
    if live_start is None or int(live_start) != int(args.worker_pid_start):
        # Unreadable fingerprint is UNKNOWN identity, not a match.
        _write_error_receipt(
            args, "recovery_identity_mismatch",
            f"pid {pid} start {live_start} != expected {args.worker_pid_start}",
        )
        return EXIT_OBSERVER_ERROR

    SYNCHRONIZE = 0x00100000
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    # 64-bit safety: without explicit restype/argtypes ctypes truncates the
    # returned HANDLE to a 32-bit c_int, which can corrupt every later call
    # that receives it (Wait/GetExitCode/CloseHandle).
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL,
                                     wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.GetExitCodeProcess.argtypes = (
        wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel32.OpenProcess(
        SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid,
    )
    if not handle:
        _write_error_receipt(
            args, "recovery_open_denied",
            f"OpenProcess({pid}) error {ctypes.get_last_error()}",
        )
        return EXIT_OBSERVER_ERROR

    try:
        # Fingerprint again AFTER the handle pins the process object — a PID
        # recycled between our first probe and OpenProcess would otherwise be
        # waited on as if it were ours.
        live_start = _pid_start(pid)
        if live_start is None or int(live_start) != int(args.worker_pid_start):
            _write_error_receipt(
                args, "recovery_identity_mismatch",
                f"pid {pid} start changed to {live_start} after open",
            )
            return EXIT_OBSERVER_ERROR

        receipt = _base_receipt(
            args, worker_pid=pid,
            worker_pid_start=int(args.worker_pid_start),
        )
        INFINITE = 0xFFFFFFFF
        WAIT_OBJECT_0 = 0x00000000
        STILL_ACTIVE = 259
        wait_rc = kernel32.WaitForSingleObject(handle, INFINITE)
        if wait_rc != WAIT_OBJECT_0:
            # WAIT_FAILED / WAIT_ABANDONED / anything unexpected: the wait
            # proves nothing — publishing a code now could bill a live or
            # unrelated process's state to this run.
            final = _finalize(
                receipt, state="observer_error",
                error="recovery_wait_failed",
                detail=(f"WaitForSingleObject returned {wait_rc} "
                        f"(error {ctypes.get_last_error()})"),
            )
            _try_write_final(args, final)
            return EXIT_OBSERVER_ERROR
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            final = _finalize(
                receipt, state="observer_error",
                error="recovery_exit_code_unavailable",
                detail=f"GetExitCodeProcess error {ctypes.get_last_error()}",
            )
            _try_write_final(args, final)
            return EXIT_OBSERVER_ERROR
        if int(code.value) == STILL_ACTIVE:
            # 259 after a signaled wait is either a process that really
            # exited with STILL_ACTIVE (indistinguishable) or a probe bug —
            # never publish it as a real exit code.
            final = _finalize(
                receipt, state="observer_error",
                error="recovery_still_active_code",
                detail="GetExitCodeProcess returned STILL_ACTIVE after wait",
            )
            _try_write_final(args, final)
            return EXIT_OBSERVER_ERROR
        # Re-check right before publishing. Only a still-launched receipt
        # (or none) may be replaced: a finalized one is settled evidence
        # (exit 0, someone else won), and anything invalid/unreadable that
        # appeared mid-wait must be preserved for the dispatcher's
        # observer_invalid handling, not overwritten.
        blocked = _existing_receipt_blocks_recovery(args.receipt, args)
        if blocked == "receipt_already_final":
            return EXIT_OK
        if blocked:
            print(f"recovery result withheld: {blocked}", file=sys.stderr)
            return EXIT_OBSERVER_ERROR
        final = _finalize(receipt, state="exited", exit_code=int(code.value))
        if not _try_write_final(args, final):
            return EXIT_OBSERVER_ERROR
        return EXIT_OK
    finally:
        kernel32.CloseHandle(handle)


def main(argv=None) -> int:
    if os.name != "nt":
        print("kanban_exit_observer is Windows-only", file=sys.stderr)
        return EXIT_USAGE
    args, child_argv = _parse_args(
        list(sys.argv[1:]) if argv is None else list(argv)
    )
    if args.recover:
        return _run_recovery(args)
    return _run_primary(args, child_argv)


if __name__ == "__main__":
    sys.exit(main())
