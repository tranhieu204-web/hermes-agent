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


def _run_primary(args, child_argv) -> int:
    if not child_argv:
        print("missing child argv after --", file=sys.stderr)
        return EXIT_USAGE

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
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        _write_error_receipt(args, "child_spawn_failed", str(exc))
        return EXIT_BOOTSTRAP_FAILED

    receipt = _base_receipt(
        args, worker_pid=child.pid, worker_pid_start=_pid_start(child.pid),
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


def _run_recovery(args) -> int:
    """Reattach to a live worker whose primary observer died.

    The recovery handle pins the process object, so the exit code read after
    the wait belongs to exactly the process whose start fingerprint the
    dispatcher validated before dispatching this recovery observer.
    """
    import ctypes
    from ctypes import wintypes

    if not args.worker_pid:
        print("--recover requires --worker-pid", file=sys.stderr)
        return EXIT_USAGE

    pid = int(args.worker_pid)
    live_start = _pid_start(pid)
    if (args.worker_pid_start is not None and live_start is not None
            and int(live_start) != int(args.worker_pid_start)):
        _write_error_receipt(
            args, "recovery_identity_mismatch",
            f"pid {pid} start {live_start} != expected {args.worker_pid_start}",
        )
        return EXIT_OBSERVER_ERROR

    SYNCHRONIZE = 0x00100000
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
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
        if (args.worker_pid_start is not None and live_start is not None
                and int(live_start) != int(args.worker_pid_start)):
            _write_error_receipt(
                args, "recovery_identity_mismatch",
                f"pid {pid} start changed to {live_start} after open",
            )
            return EXIT_OBSERVER_ERROR

        receipt = _base_receipt(
            args, worker_pid=pid,
            worker_pid_start=(
                int(args.worker_pid_start)
                if args.worker_pid_start is not None else live_start
            ),
        )
        INFINITE = 0xFFFFFFFF
        kernel32.WaitForSingleObject(handle, INFINITE)
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            final = _finalize(
                receipt, state="observer_error",
                error="recovery_exit_code_unavailable",
                detail=f"GetExitCodeProcess error {ctypes.get_last_error()}",
            )
            _try_write_final(args, final)
            return EXIT_OBSERVER_ERROR
        final = _finalize(receipt, state="exited", exit_code=int(code.value))
        final["recovered"] = True
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
