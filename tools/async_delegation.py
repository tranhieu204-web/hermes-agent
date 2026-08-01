#!/usr/bin/env python3
"""
Async (background) delegation registry.

Backs ``delegate_task(background=true)``: the parent agent dispatches a
subagent that runs on a module-level daemon executor and returns a handle
immediately, so the user and the model can keep working while the child runs.

When the child finishes, a completion event is pushed onto the SHARED
``process_registry.completion_queue`` with ``type="async_delegation"``. The
CLI (``cli.py`` process_loop) and gateway (``_run_process_watcher`` /
``completion_queue`` drain) already poll that queue while the agent is idle
and forge a fresh user/internal turn from each event. We deliberately reuse
that rail rather than reaching into a running agent loop:

  - completions surface as a NEW turn when the agent is idle, never spliced
    between a tool result and an assistant message. That keeps strict
    message-role alternation legal and the prompt cache intact (hard
    invariant: never mutate past context).
  - we inherit the queue's de-dup, crash-recovery checkpoint, and the
    existing CLI + gateway drain wiring for free — no new drain loops in the
    two largest files in the repo.

The completion payload carries a RICH, self-contained task-source block (the
original goal, the context the parent supplied, toolsets, model, dispatch
time, status, and the full result summary). When the result re-enters the
conversation the parent may be deep in unrelated context and won't remember
why the subagent existed; the block lets it either use the result or
re-dispatch if the world has moved on.

This module owns ONLY the async lifecycle. The actual child build + run is
delegated back to ``delegate_tool._run_single_child`` via an injected
runner, so all the credential leasing, heartbeat, timeout, and result-shaping
logic stays in one place.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, List, Optional

from hermes_constants import get_hermes_home
from tools.daemon_pool import DaemonThreadPoolExecutor
from tools.thread_context import propagate_context_to_thread

logger = logging.getLogger(__name__)

# Back-compat alias — the daemon executor now lives in tools.daemon_pool so
# other subsystems (tool_executor, memory_manager, delegate_tool, skills_hub)
# can share it. Existing imports of ``_DaemonThreadPoolExecutor`` keep working.
_DaemonThreadPoolExecutor = DaemonThreadPoolExecutor


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------
# A persistent daemon executor (NOT a `with ThreadPoolExecutor()` block, which
# would join on exit and defeat the whole point of async). Workers are daemon
# threads so a hard process exit doesn't hang on an in-flight child.
_executor: Optional[ThreadPoolExecutor] = None
_executor_lock = threading.Lock()
_executor_max_workers: int = 0

_records_lock = threading.Lock()
# delegation_id -> record dict. Kept for the lifetime of the run plus a short
# tail after completion so `list_async_delegations()` can show recent results.
_records: Dict[str, Dict[str, Any]] = {}

_DEFAULT_MAX_ASYNC_CHILDREN = 3
# How many completed records to retain for status queries before pruning.
_MAX_RETAINED_COMPLETED = 50
_DURABLE_RETENTION_SECONDS = 7 * 24 * 60 * 60
_MAX_DURABLE_PENDING = 1000
# A pending completion whose delivery keeps failing is retried across claim
# cycles (and across restarts via restore_undelivered_completions). Cap the
# attempts so an unroutable row converges to a terminal 'dropped' state
# instead of replaying on every restart forever.
_MAX_DELIVERY_ATTEMPTS = 8
_DB_LOCK = threading.Lock()
_SCHEMA_INITIALIZATION_RETRY_SECONDS = 10.0
_SCHEMA_INITIALIZATION_RETRY_INTERVAL_SECONDS = 0.025

def _db_path():
    return get_hermes_home() / "state.db"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    deadline = time.monotonic() + _SCHEMA_INITIALIZATION_RETRY_SECONDS
    try:
        while True:
            try:
                _initialize_schema(conn)
                break
            except sqlite3.OperationalError as error:
                # A fresh shared Hermes home can have two processes opening
                # state.db at once.  WAL activation/DDL needs a writer lock;
                # wait only for that expected bootstrap race, never swallow a
                # malformed schema or another operational failure.
                if "locked" not in str(error).lower() or time.monotonic() >= deadline:
                    raise
                time.sleep(_SCHEMA_INITIALIZATION_RETRY_INTERVAL_SECONDS)
    except Exception:
        # A PRAGMA/DDL failure after a successful connect() must not leak the
        # just-opened connection back to the caller.
        conn.close()
        raise
    return conn


def _initialize_schema(conn: sqlite3.Connection) -> None:
    from hermes_state import apply_wal_with_fallback

    apply_wal_with_fallback(conn, db_label="state.db (async_delegation)")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS async_delegations (
            delegation_id TEXT PRIMARY KEY,
            origin_session TEXT NOT NULL,
            origin_ui_session_id TEXT NOT NULL DEFAULT '',
            parent_session_id TEXT,
            state TEXT NOT NULL,
            dispatched_at REAL NOT NULL,
            completed_at REAL,
            updated_at REAL NOT NULL,
            event_json TEXT,
            result_json TEXT,
            delivery_state TEXT NOT NULL DEFAULT 'pending',
            delivery_attempts INTEGER NOT NULL DEFAULT 0,
            delivered_at REAL,
            owner_pid INTEGER,
            owner_started_at INTEGER,
            task_json TEXT,
            delivery_claim TEXT,
            delivery_claimed_at REAL,
            origin_session_id TEXT NOT NULL DEFAULT '',
            review_receipt_id TEXT NOT NULL DEFAULT '',
            review_ledger_path TEXT NOT NULL DEFAULT '',
            event_stream_id TEXT NOT NULL DEFAULT '',
            event_sequence INTEGER NOT NULL DEFAULT 0,
            submission_state TEXT NOT NULL DEFAULT 'submit_pending',
            submission_fence INTEGER NOT NULL DEFAULT 0,
            candidate_hash TEXT NOT NULL DEFAULT '',
            effective_execution_identity TEXT NOT NULL DEFAULT '',
            recovery_attempt_id TEXT NOT NULL DEFAULT '',
            external_provisional_handle_id TEXT NOT NULL DEFAULT '',
            external_handle_id TEXT NOT NULL DEFAULT '',
            external_pid INTEGER, external_host_start_time INTEGER
        )"""
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(async_delegations)")}
    for name, sql_type in (
        ("owner_pid", "INTEGER"),
        ("owner_started_at", "INTEGER"),
        ("task_json", "TEXT"),
        ("delivery_claim", "TEXT"),
        ("delivery_claimed_at", "REAL"),
        # Raw api_server session id (X-Hermes-Session-Id) of the ORIGINATING
        # request — the wake self-post target. Without persisting it,
        # completions recovered after a process restart are unroutable on
        # api_server (the in-memory record that carried it is gone).
        ("origin_session_id", "TEXT"),
        ("review_receipt_id", "TEXT NOT NULL DEFAULT ''"),
        ("review_ledger_path", "TEXT NOT NULL DEFAULT ''"),
        ("event_stream_id", "TEXT NOT NULL DEFAULT ''"),
        ("event_sequence", "INTEGER NOT NULL DEFAULT 0"),
        ("submission_state", "TEXT NOT NULL DEFAULT 'submit_pending'"),
        ("submission_fence", "INTEGER NOT NULL DEFAULT 0"),
        ("candidate_hash", "TEXT NOT NULL DEFAULT ''"),
        ("effective_execution_identity", "TEXT NOT NULL DEFAULT ''"),
        ("recovery_attempt_id", "TEXT NOT NULL DEFAULT ''"),
        ("external_provisional_handle_id", "TEXT NOT NULL DEFAULT ''"),
        ("external_handle_id", "TEXT NOT NULL DEFAULT ''"),
        ("external_pid", "INTEGER"),
        ("external_host_start_time", "INTEGER"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE async_delegations ADD COLUMN {name} {sql_type}")
    conn.execute(
        """CREATE TRIGGER IF NOT EXISTS async_delegations_material_identity_immutable
        BEFORE UPDATE OF candidate_hash, effective_execution_identity, recovery_attempt_id, submission_fence
        ON async_delegations
        WHEN OLD.review_receipt_id <> '' AND (
            NEW.candidate_hash <> OLD.candidate_hash
            OR NEW.effective_execution_identity <> OLD.effective_execution_identity
            OR NEW.recovery_attempt_id <> OLD.recovery_attempt_id
            OR NEW.submission_fence <> OLD.submission_fence
        )
        BEGIN SELECT RAISE(ABORT, 'material outbox identity is immutable'); END"""
    )
    conn.execute(
        """CREATE TRIGGER IF NOT EXISTS async_delegations_external_handle_immutable
        BEFORE UPDATE OF external_handle_id, external_pid, external_host_start_time
        ON async_delegations
        WHEN OLD.review_receipt_id <> '' AND OLD.external_handle_id <> '' AND (
            NEW.external_handle_id <> OLD.external_handle_id
            OR NEW.external_pid IS NOT OLD.external_pid
            OR NEW.external_host_start_time IS NOT OLD.external_host_start_time
        )
        BEGIN SELECT RAISE(ABORT, 'material external handle is immutable'); END"""
    )


@contextmanager
def _transaction() -> Iterator[sqlite3.Connection]:
    """Open a connection, commit/rollback on exit, and ALWAYS close it.

    ``sqlite3.Connection.__enter__``/``__exit__`` only commit or roll back the
    transaction; they do not close the connection. Using ``with _connect()``
    alone therefore leaks a connection — and its WAL/SHM file descriptors — on
    every durable dispatch, completion, and delivery-claim, deferring the close
    to the garbage collector. On a long-running gateway that exhausts
    ``RLIMIT_NOFILE`` (the cron-ledger sibling of this bug was #69567 / PR #69594).
    """
    conn = _connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _async_event_stream_id(record: Dict[str, Any]) -> str:
    existing = str(record.get("event_stream_id") or "").strip()
    if existing:
        return existing
    delegation_id = str(record.get("delegation_id") or "unknown").strip()
    producer_scope = {
        "delegation_id": delegation_id,
        "parent_session_id": str(record.get("parent_session_id") or ""),
        "session_key": str(record.get("session_key") or ""),
        "origin_ui_session_id": str(record.get("origin_ui_session_id") or ""),
        "origin_session_id": str(record.get("origin_session_id") or ""),
    }
    digest = hashlib.sha256(
        json.dumps(producer_scope, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:24]
    return f"async-delegation:{delegation_id}:{digest}"


def _next_completion_event_identity(record: Dict[str, Any]) -> Dict[str, Any]:
    stream_id = _async_event_stream_id(record)
    prior = record.get("event_sequence", 0)
    if isinstance(prior, bool) or not isinstance(prior, int) or prior < 0:
        prior = 0
    sequence = prior + 1
    return {
        "event_id": f"{stream_id}:completion:{sequence}",
        "event_stream_id": stream_id,
        "event_sequence": sequence,
        "event_seq": sequence,
    }


def _persist_dispatch(
    record: Dict[str, Any], max_async_children: Optional[int] = None, state: str = "dispatching",
) -> bool:
    """Create one durable dispatch lease before a worker can be submitted.

    The SQLite transaction is the cross-process admission boundary.  The
    in-memory registry remains useful for local cancellation, but cannot be
    used to enforce a shared Hermes-home concurrency limit.
    """
    now = time.time()
    try:
        from gateway.status import get_process_start_time
        owner_started_at = get_process_start_time(__import__("os").getpid())
    except Exception:
        owner_started_at = None
    task_payload = {
        key: record.get(key)
        for key in ("goal", "goals", "context", "toolsets", "role", "model", "is_batch")
        if key in record
    }
    if state not in {"dispatching", "running"}:
        raise ValueError(f"unsupported durable dispatch state: {state}")
    with _DB_LOCK, _transaction() as conn:
        # SQLite's deferred transaction permits two processes to read the
        # same available count before either writes. Acquire the writer lease
        # first so count plus lease insertion is one cross-process admission.
        conn.execute("BEGIN IMMEDIATE")
        if max_async_children is not None:
            active = conn.execute(
                "SELECT COUNT(*) FROM async_delegations WHERE state IN ('dispatching', 'running', 'finalizing')"
            ).fetchone()[0]
            if active >= max_async_children:
                return False
        conn.execute(
            """INSERT INTO async_delegations
               (delegation_id, origin_session, origin_ui_session_id,
                parent_session_id, state, dispatched_at, updated_at,
                delivery_state, delivery_attempts, owner_pid,
               owner_started_at, task_json, origin_session_id, review_receipt_id, review_ledger_path,
               event_stream_id, event_sequence, submission_state, submission_fence, candidate_hash, effective_execution_identity, recovery_attempt_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?, ?, ?, ?, ?, 'submit_pending', ?, ?, ?, ?)""",
            (record["delegation_id"], record.get("session_key", ""),
             record.get("origin_ui_session_id", ""), record.get("parent_session_id"),
             state, record["dispatched_at"], now, __import__("os").getpid(),
             owner_started_at, json.dumps(task_payload),
             record.get("origin_session_id", ""), record.get("review_receipt_id", ""), record.get("review_ledger_path", ""),
             record.get("event_stream_id", ""), record.get("event_sequence", 0), int(record.get("review_fence_token", 0) or 0),
             record.get("candidate_hash", ""), record.get("effective_execution_identity", ""), record.get("recovery_attempt_id", "")),
        )
    _prune_durable_records()
    return True


def _activate_durable_dispatch(delegation_id: str) -> bool:
    """Make a pre-submission lease runnable without creating a second record."""
    with _DB_LOCK, _transaction() as conn:
        updated = conn.execute(
            "UPDATE async_delegations SET state='running', submission_state='submitted', updated_at=? WHERE delegation_id=? AND state='dispatching' AND submission_state='submit_pending'",
            (time.time(), delegation_id),
        )
        return updated.rowcount == 1


def _claim_executor_submission(delegation_id: str) -> bool:
    """Claim a durable outbox item exactly once immediately before submit.

    A retried caller sees the existing delegation identity but cannot submit a
    second worker.  If the process dies after this claim, the existing
    abandoned-owner recovery classifies the durable row as unknown rather than
    guessing that execution happened.
    """
    with _DB_LOCK, _transaction() as conn:
        updated = conn.execute(
            "UPDATE async_delegations SET submission_state='executor_claimed', updated_at=? "
            "WHERE delegation_id=? AND state='running' AND submission_state='submitted'",
            (time.time(), delegation_id),
        )
        return updated.rowcount == 1


def bind_material_provisional_handle(
    delegation_id: str,
    *,
    fence_token: int,
    handle_id: str,
) -> bool:
    """Claim outbox ownership of a material child BEFORE it is created.

    This is the async half of the pre-execution gate.  It records only the
    opaque handle — never a PID, because none exists yet — so a crash in this
    window is recoverable as an unowned submission rather than being mistaken
    for a child that ran.  ``external_handle_id`` stays empty, which keeps the
    owned-identity immutability trigger armed for the real bind.
    """
    opaque_handle = str(handle_id or "").strip()
    if not opaque_handle or any(char.isspace() for char in opaque_handle):
        raise ValueError("external handle_id must be a non-empty opaque token")
    with _DB_LOCK, _transaction() as conn:
        row = conn.execute(
            "SELECT external_provisional_handle_id, external_handle_id FROM async_delegations "
            "WHERE delegation_id=? AND state='running' AND submission_fence=?",
            (delegation_id, int(fence_token)),
        ).fetchone()
        if row is None:
            return False
        if row[0]:
            return row[0] == opaque_handle
        if row[1]:
            return False
        updated = conn.execute(
            "UPDATE async_delegations SET external_provisional_handle_id=?, updated_at=? "
            "WHERE delegation_id=? AND state='running' AND submission_fence=? "
            "AND external_provisional_handle_id='' AND external_handle_id=''",
            (opaque_handle, time.time(), delegation_id, int(fence_token)),
        )
        return updated.rowcount == 1


def bind_material_external_handle(
    delegation_id: str,
    *,
    fence_token: int,
    handle_id: str,
    pid: int,
    host_start_time: int | None,
) -> bool:
    """Persist one opaque owned external child before it may be material-running.

    This writes only non-secret process identity facts.  The same handle must
    already hold the pre-execution provisional gate, so an owned PID can never
    be the first durable trace of external work.  The caller must then bind the
    matching ledger plan; a crash between the rails is intentionally recoverable
    and never authorizes a duplicate launch.
    """
    opaque_handle = str(handle_id or "").strip()
    if not opaque_handle or any(char.isspace() for char in opaque_handle):
        raise ValueError("external handle_id must be a non-empty opaque token")
    if not isinstance(pid, int) or pid <= 0:
        raise ValueError("external PID must be positive")
    if host_start_time is not None and (not isinstance(host_start_time, int) or host_start_time < 0):
        raise ValueError("external process start time is invalid")
    with _DB_LOCK, _transaction() as conn:
        row = conn.execute(
            "SELECT external_handle_id, external_pid, external_host_start_time, external_provisional_handle_id "
            "FROM async_delegations WHERE delegation_id=? AND state='running' AND submission_fence=?",
            (delegation_id, int(fence_token)),
        ).fetchone()
        if row is None:
            return False
        if row[0]:
            return row[:3] == (opaque_handle, pid, host_start_time)
        if row[3] != opaque_handle:
            return False
        updated = conn.execute(
            "UPDATE async_delegations SET external_handle_id=?, external_pid=?, external_host_start_time=?, updated_at=? "
            "WHERE delegation_id=? AND state='running' AND submission_fence=? AND external_handle_id='' "
            "AND external_provisional_handle_id=?",
            (opaque_handle, pid, host_start_time, time.time(), delegation_id, int(fence_token), opaque_handle),
        )
        return updated.rowcount == 1


def _mark_submission_unknown(delegation_id: str, reason: str) -> None:
    """Keep an ambiguous executor submission durable for recovery evidence."""
    with _DB_LOCK, _transaction() as conn:
        binding = conn.execute(
            "SELECT review_receipt_id, review_ledger_path, recovery_attempt_id, submission_fence FROM async_delegations WHERE delegation_id=?",
            (delegation_id,),
        ).fetchone()
        conn.execute(
            "UPDATE async_delegations SET state='unknown', submission_state='submission_unknown', "
            "result_json=?, completed_at=?, updated_at=? WHERE delegation_id=?",
            (json.dumps({"status": "unknown", "error": reason}), time.time(), time.time(), delegation_id),
        )
    if binding and binding[2]:
        try:
            from tools.release_review_ledger import ReleaseReviewLedger
            canonical_path = (get_hermes_home() / "release-review-ledger.db").resolve()
            stored_path = Path(binding[1]).resolve() if binding[1] else canonical_path
            if stored_path != canonical_path:
                raise RuntimeError("stored release-review ledger path does not match the canonical Hermes home")
            ledger = ReleaseReviewLedger(canonical_path)
            receipt = ledger.has_material_route_plan(binding[0]) if binding[0] else False
            if receipt:
                _terminalize_material_or_unowned(
                    ledger, binding[0], attempt_id=binding[2], fence_token=int(binding[3]), status="unknown",
                    evidence={"delegation_id": delegation_id, "reason": "submission_unknown"},
                )
            else:
                ledger.transition_recovery_attempt(
                    binding[2], fence_token=int(binding[3]), state="INTERRUPTED", outcome="submit_unknown",
                )
        except Exception:
            logger.exception("Could not interrupt recovery attempt for unknown submit %s", delegation_id)


def _terminalize_material_or_unowned(ledger, receipt_id: str, *, attempt_id: str, fence_token: int,
                                     status: str, evidence: Dict[str, Any]) -> bool:
    """Publish a material terminal state, never treating a handle-free saga as work.

    The normal path requires a durable external handle and a RUNNING recovery
    attempt.  The fallback exists only for the crash window before that bind:
    it records an interrupted/cancelled sealed plan rather than accepting a
    completion that no owned executor could have produced.
    """
    try:
        return ledger.finalize_material_saga(
            receipt_id, attempt_id=attempt_id, fence_token=fence_token,
            status=status, evidence=evidence,
        )
    except RuntimeError:
        fallback_status = "cancelled" if str(status).lower() == "cancelled" else "unknown"
        return ledger.interrupt_unowned_material_saga(
            receipt_id, attempt_id=attempt_id, fence_token=fence_token,
            status=fallback_status, evidence=evidence,
        )


def _claim_durable_terminal(delegation_id: str, fence_token: int) -> bool:
    """Claim exactly one current-fence terminal publisher across processes."""
    with _DB_LOCK, _transaction() as conn:
        updated = conn.execute(
            "UPDATE async_delegations SET state='finalizing', updated_at=? "
            "WHERE delegation_id=? AND state='running' AND submission_fence=?",
            (time.time(), delegation_id, int(fence_token)),
        )
        return updated.rowcount == 1


def cancel_async_delegation(delegation_id: str, *, fence_token: int, reason: str = "cancelled") -> bool:
    """CAS-cancel one current async lease without permitting a late result.

    This is intentionally narrow: callers need the durable fence returned by
    admission, so a stale coordinator cannot cancel a successor generation.
    The worker may still unwind, but its later terminal publish is rejected by
    ``_claim_durable_terminal``.
    """
    with _DB_LOCK, _transaction() as conn:
        binding = conn.execute(
            "SELECT review_receipt_id, review_ledger_path, recovery_attempt_id, external_handle_id, external_pid, external_host_start_time "
            "FROM async_delegations WHERE delegation_id=?",
            (delegation_id,),
        ).fetchone()
        updated = conn.execute(
            "UPDATE async_delegations SET state='cancelled', submission_state='cancelled', "
            "result_json=?, completed_at=?, updated_at=? "
            "WHERE delegation_id=? AND state IN ('dispatching','running') AND submission_fence=?",
            (json.dumps({"status": "cancelled", "reason": reason}), time.time(), time.time(),
             delegation_id, int(fence_token)),
        )
        if updated.rowcount != 1:
            return False
    with _records_lock:
        record = _records.get(delegation_id)
        if record is not None:
            record["status"] = "cancelled"
            callback = record.get("interrupt_fn")
        else:
            callback = None
    if callable(callback):
        callback()
    # The in-memory controller is only a convenience.  After a gateway
    # restart, use the durable opaque handle/PID/start triple directly so the
    # current-fence cancellation retains exact-owner semantics.
    if binding and binding[3] and isinstance(binding[4], int):
        try:
            from tools.process_registry import process_registry
            process_registry.cancel_owned_argv_process(
                str(binding[3]), pid=int(binding[4]), host_start_time=binding[5],
                source="material-review.cancel.durable",
            )
        except Exception:
            logger.exception("Could not cancel durable material child for %s", delegation_id)
    if binding and binding[0]:
        try:
            from tools.release_review_ledger import ReleaseReviewLedger
            canonical_path = (get_hermes_home() / "release-review-ledger.db").resolve()
            stored_path = Path(binding[1]).resolve() if binding[1] else canonical_path
            if stored_path != canonical_path:
                raise RuntimeError("stored release-review ledger path does not match the canonical Hermes home")
            ledger = ReleaseReviewLedger(canonical_path)
            if binding[2] and ledger.has_material_route_plan(binding[0]):
                _terminalize_material_or_unowned(
                    ledger,
                    binding[0], attempt_id=binding[2], fence_token=fence_token, status="cancelled",
                    evidence={"delegation_id": delegation_id, "reason": reason},
                )
            else:
                ledger.finalize_async_receipt(
                    binding[0], "cancelled", {"delegation_id": delegation_id, "reason": reason, "fence_token": fence_token},
                )
                if binding[2]:
                    ledger.transition_recovery_attempt(
                        binding[2], fence_token=fence_token, state="CANCELLED", outcome=reason,
                    )
        except Exception:
            logger.exception("Could not terminalize cancelled release review receipt %s", binding[0])
    return True


def _delete_durable_delegation(delegation_id: str) -> None:
    with _DB_LOCK, _transaction() as conn:
        conn.execute("DELETE FROM async_delegations WHERE delegation_id=?", (delegation_id,))


def _prune_durable_records() -> None:
    """Bound terminal history, preferring delivered records for deletion."""
    now = time.time()
    cutoff = now - _DURABLE_RETENTION_SECONDS
    with _DB_LOCK, _transaction() as conn:
        conn.execute(
            "DELETE FROM async_delegations WHERE delivery_state='delivered' AND updated_at < ?",
            (cutoff,),
        )
        terminal_count = conn.execute(
            "SELECT COUNT(*) FROM async_delegations WHERE state NOT IN ('dispatching','running','finalizing')"
        ).fetchone()[0]
        excess = max(0, terminal_count - _MAX_RETAINED_COMPLETED)
        if excess:
            conn.execute(
                """DELETE FROM async_delegations WHERE delegation_id IN (
                     SELECT delegation_id FROM async_delegations
                     WHERE state NOT IN ('dispatching','running','finalizing')
                     ORDER BY CASE delivery_state WHEN 'delivered' THEN 0 ELSE 1 END,
                              updated_at ASC LIMIT ?
                   )""",
                (excess,),
            )
        pending_count = conn.execute(
            """SELECT COUNT(*) FROM async_delegations
               WHERE state NOT IN ('dispatching','running','finalizing') AND delivery_state='pending'"""
        ).fetchone()[0]
        overflow = max(0, pending_count - _MAX_DURABLE_PENDING)
        if overflow:
            conn.execute(
                """DELETE FROM async_delegations WHERE delegation_id IN (
                     SELECT delegation_id FROM async_delegations
                     WHERE state NOT IN ('dispatching','running','finalizing') AND delivery_state='pending'
                     ORDER BY updated_at ASC LIMIT ?
                   )""",
                (overflow,),
            )


def _persist_completion(event: Dict[str, Any], result: Dict[str, Any]) -> bool:
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        review_receipt_id = event.get("review_receipt_id") or ""
        if review_receipt_id:
            predicate = "delegation_id=? AND state='finalizing' AND submission_fence=?"
            predicate_args = (event["delegation_id"], int(event.get("review_fence_token", 0) or 0))
        else:
            predicate = "delegation_id=?"
            predicate_args = (event["delegation_id"],)
        updated = conn.execute(
            """UPDATE async_delegations SET state=?, completed_at=?, updated_at=?,
               event_json=?, result_json=?, delivery_state='pending',
               event_stream_id=?, event_sequence=?
               WHERE """ + predicate,
            (event.get("status", "completed"), event.get("completed_at", now), now,
             json.dumps(event), json.dumps(result), event.get("event_stream_id", ""),
             event.get("event_sequence", 0), *predicate_args),
        )
    if updated.rowcount != 1:
        return False
    receipt_id = event.get("review_receipt_id") or ""
    if receipt_id:
        try:
            from tools.release_review_ledger import ReleaseReviewLedger
            canonical_path = (get_hermes_home() / "release-review-ledger.db").resolve()
            stored_path = event.get("review_ledger_path") or str(canonical_path)
            if Path(stored_path).resolve() != canonical_path:
                raise RuntimeError("stored release-review ledger path does not match the canonical Hermes home")
            ledger = ReleaseReviewLedger(canonical_path)
            recovery_attempt_id = event.get("recovery_attempt_id") or ""
            if recovery_attempt_id and ledger.has_material_route_plan(receipt_id):
                _terminalize_material_or_unowned(
                    ledger,
                    receipt_id,
                    attempt_id=recovery_attempt_id,
                    fence_token=int(event.get("review_fence_token", 0) or 0),
                    status=event.get("status", "unknown"),
                    evidence={"delegation_id": event["delegation_id"], "result": result},
                )
            else:
                ledger.finalize_async_receipt(receipt_id, event.get("status", "unknown"), {
                    "delegation_id": event["delegation_id"], "result": result,
                })
                if recovery_attempt_id:
                    outcome = "COMMITTED" if event.get("status") == "completed" else "FAILED"
                    ledger.transition_recovery_attempt(
                        recovery_attempt_id, fence_token=int(event.get("review_fence_token", 0) or 0),
                        state=outcome, outcome=str(event.get("status", "unknown")),
                    )
        except Exception:
            logger.exception("Could not finalize release review receipt %s", receipt_id)
    return True


def reconcile_material_outbox() -> int:
    """Finish ledger terminalization after an async/ledger crash-window split.

    The async outbox is written first so a worker result is never lost.  If the
    process exits before the corresponding ledger transaction, this reconciler
    only replays the already-durable, current-fence terminal outcome.  It never
    restarts a child or republishes a raw worker payload.
    """
    with _DB_LOCK, _transaction() as conn:
        rows = conn.execute(
            """SELECT delegation_id, state, review_receipt_id, review_ledger_path,
                      recovery_attempt_id, submission_fence
               FROM async_delegations
               WHERE review_receipt_id <> '' AND recovery_attempt_id <> ''
                 AND state NOT IN ('dispatching', 'running', 'finalizing')"""
        ).fetchall()
    reconciled = 0
    for delegation_id, state, receipt_id, stored_path, attempt_id, fence_token in rows:
        try:
            from tools.release_review_ledger import ReleaseReviewLedger
            canonical_path = (get_hermes_home() / "release-review-ledger.db").resolve()
            if Path(stored_path).resolve() != canonical_path:
                raise RuntimeError("stored release-review ledger path does not match the canonical Hermes home")
            ledger = ReleaseReviewLedger(canonical_path)
            if not ledger.has_material_route_plan(receipt_id):
                continue
            if _terminalize_material_or_unowned(
                ledger,
                receipt_id,
                attempt_id=attempt_id,
                fence_token=int(fence_token or 0),
                status=state,
                evidence={"delegation_id": delegation_id, "reconciled_from": "durable_async_terminal"},
            ):
                reconciled += 1
        except Exception:
            logger.exception("Could not reconcile sealed material outbox %s", delegation_id)
    return reconciled


def _note_delivery_attempt(delegation_id: str) -> None:
    with _DB_LOCK, _transaction() as conn:
        conn.execute(
            "UPDATE async_delegations SET delivery_attempts=delivery_attempts+1, updated_at=? WHERE delegation_id=?",
            (time.time(), delegation_id),
        )


def recover_abandoned_delegations() -> int:
    """Classify records whose owning process disappeared as outcome unknown."""
    try:
        from gateway.status import _pid_exists, get_process_start_time
    except Exception:
        return 0
    now = time.time()
    recovered = 0
    review_terminalizations = []
    with _DB_LOCK, _transaction() as conn:
        rows = conn.execute(
            """SELECT delegation_id, origin_session, origin_ui_session_id,
                      parent_session_id, dispatched_at, owner_pid,
                      owner_started_at, task_json, origin_session_id, review_receipt_id, review_ledger_path,
                      event_stream_id, event_sequence, recovery_attempt_id, submission_fence
               FROM async_delegations WHERE state IN ('dispatching','running','finalizing')"""
        ).fetchall()
        for row in rows:
            (delegation_id, session_key, origin_ui, parent_id, dispatched_at,
             pid, started, task_json, origin_session_id, review_receipt_id, review_ledger_path,
             event_stream_id, event_sequence, recovery_attempt_id, submission_fence) = row
            live = False
            if pid and started is not None:
                live = _pid_exists(int(pid)) and get_process_start_time(int(pid)) == int(started)
            if live:
                continue
            task = json.loads(task_json or "{}")
            event = {
                "type": "async_delegation", "delegation_id": delegation_id,
                "session_key": session_key, "origin_ui_session_id": origin_ui,
                # Restore the durable wake target so completions recovered
                # after a restart remain routable to api_server sessions.
                "origin_session_id": origin_session_id or "",
                "parent_session_id": parent_id, "goal": task.get("goal", ""),
                "goals": task.get("goals"), "context": task.get("context"),
                "toolsets": task.get("toolsets"), "role": task.get("role"),
                "model": task.get("model"), "is_batch": bool(task.get("is_batch")),
                "review_receipt_id": review_receipt_id or "",
                "review_ledger_path": review_ledger_path or "",
                "recovery_attempt_id": recovery_attempt_id or "",
                "review_fence_token": int(submission_fence or 0),
                "status": "unknown", "summary": None,
                "error": "Delegation owner exited before recording a terminal result; outcome unknown.",
                "dispatched_at": dispatched_at, "completed_at": now,
            }
            event.update(_next_completion_event_identity({
                **event,
                "event_stream_id": event_stream_id or "",
                "event_sequence": event_sequence or 0,
            }))
            result = {"status": "unknown", "summary": None, "error": event["error"]}
            conn.execute(
                """UPDATE async_delegations SET state='unknown', completed_at=?,
                   updated_at=?, event_json=?, result_json=?, delivery_state='pending',
                   event_stream_id=?, event_sequence=?
                   WHERE delegation_id=?""",
                (now, now, json.dumps(event), json.dumps(result),
                 event["event_stream_id"], event["event_sequence"], delegation_id),
            )
            if review_receipt_id:
                review_terminalizations.append((review_receipt_id, review_ledger_path, event, result, recovery_attempt_id, submission_fence))
            recovered += 1
    for receipt_id, ledger_path, event, result, recovery_attempt_id, submission_fence in review_terminalizations:
        try:
            from tools.release_review_ledger import ReleaseReviewLedger
            canonical_path = (get_hermes_home() / "release-review-ledger.db").resolve()
            stored_path = Path(ledger_path).resolve() if ledger_path else canonical_path
            if stored_path != canonical_path:
                raise RuntimeError("stored release-review ledger path does not match the canonical Hermes home")
            ledger = ReleaseReviewLedger(canonical_path)
            if recovery_attempt_id and ledger.has_material_route_plan(receipt_id):
                _terminalize_material_or_unowned(
                    ledger,
                    receipt_id, attempt_id=recovery_attempt_id, fence_token=int(submission_fence or 0),
                    status="unknown", evidence={"delegation_id": event["delegation_id"], "owner": "exited"},
                )
            else:
                ledger.finalize_async_receipt(receipt_id, "unknown", {
                    "delegation_id": event["delegation_id"], "result": result,
                })
                if recovery_attempt_id:
                    ledger.transition_recovery_attempt(
                        recovery_attempt_id, fence_token=int(submission_fence or 0),
                        state="INTERRUPTED", outcome="owner_died",
                    )
        except Exception:
            logger.exception("Could not terminalize abandoned release review receipt %s", receipt_id)
    return recovered


def restore_undelivered_completions(target_queue) -> int:
    """Enqueue durable pending completions as fresh turns after process start.

    Every restored event is stamped ``restored=True`` (in-memory only — the
    stamp is added after the durable payload is deserialized and is never
    persisted). Restored events originate from a *previous* process, so no
    consumer in THIS process implicitly owns them: drain paths that run
    without an ownership filter (the legacy single-session behavior) must
    leave them queued for a consumer that can positively prove ownership,
    otherwise a brand-new session adopts a dead session's delegation
    results seconds after boot (#64484).
    """
    # Recover a terminal async write that survived a process crash before its
    # paired material-ledger transaction.  This must precede owner-death
    # classification so no already-terminal result is downgraded to unknown.
    reconcile_material_outbox()
    recover_abandoned_delegations()
    with _DB_LOCK, _transaction() as conn:
        rows = conn.execute(
            """SELECT delegation_id, event_json FROM async_delegations
               WHERE state != 'running' AND delivery_state='pending' AND event_json IS NOT NULL
               ORDER BY completed_at, delegation_id"""
        ).fetchall()
        for _delegation_id, payload in rows:
            evt = json.loads(payload)
            if isinstance(evt, dict):
                evt["restored"] = True
            target_queue.put(evt)
    return len(rows)


def mark_completion_delivered(delegation_id: str) -> bool:
    """Atomically acknowledge successful injection of a durable completion."""
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_state='delivered', delivered_at=?, updated_at=?
               WHERE delegation_id=? AND delivery_state!='delivered'""",
            (now, now, delegation_id),
        )
        return cur.rowcount == 1


def claim_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Claim one pending completion across competing consumers/processes."""
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        row = conn.execute(
            "SELECT delivery_state FROM async_delegations WHERE delegation_id=?",
            (delegation_id,),
        ).fetchone()
        if row is None:
            return True  # legacy event created before durable dispatch
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_claim=?, delivery_claimed_at=?,
                      delivery_attempts=delivery_attempts+1, updated_at=?
               WHERE delegation_id=? AND delivery_state='pending'
                 AND (delivery_claim IS NULL OR delivery_claimed_at < ?)""",
            (claim_id, now, now, delegation_id, now - 300),
        )
        return cur.rowcount == 1


def claim_event_delivery(evt: Dict[str, Any], consumer: str) -> Optional[str]:
    """Claim a durable delegation event; non-durable events need no token."""
    if evt.get("type") != "async_delegation":
        return ""
    delegation_id = str(evt.get("delegation_id") or "")
    if not delegation_id:
        return ""
    claim_id = f"{consumer}:{__import__('os').getpid()}:{uuid.uuid4().hex}"
    return claim_id if claim_completion_delivery(delegation_id, claim_id) else None


def release_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Release a failed delivery claim so another consumer may retry.

    Attempts are counted at claim time, so a row that keeps being claimed and
    released has burned real delivery attempts. Once the budget is exhausted
    the row converges to a terminal ``dropped`` state instead of returning to
    ``pending`` — otherwise an undeliverable completion replays on every
    gateway restart forever (restore_undelivered_completions only restores
    pending rows).
    """
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        capped = conn.execute(
            """UPDATE async_delegations SET delivery_state='dropped',
                      delivery_claim=NULL, delivery_claimed_at=NULL, updated_at=?
               WHERE delegation_id=? AND delivery_state='pending'
                 AND delivery_claim=? AND delivery_attempts>=?""",
            (now, delegation_id, claim_id, _MAX_DELIVERY_ATTEMPTS),
        )
        if capped.rowcount == 1:
            logger.warning(
                "Async delegation %s exhausted its %d delivery attempts; "
                "marking terminally dropped (result remains queryable).",
                delegation_id, _MAX_DELIVERY_ATTEMPTS,
            )
            return True
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_claim=NULL,
                      delivery_claimed_at=NULL, updated_at=?
               WHERE delegation_id=? AND delivery_state='pending'
                 AND delivery_claim=?""",
            (now, delegation_id, claim_id),
        )
        return cur.rowcount == 1


def drop_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Terminally drop a claimed completion that can never be delivered.

    Used when the delivery target is permanently gone — the spawning session
    ended at an explicit user boundary (/new, reset) rather than a compression
    rotation. Marking the row ``dropped`` (not ``delivered``) keeps the ack
    honest, and (not ``pending``) keeps restart recovery from replaying a
    completion that will be fail-closed dropped again every time.
    """
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_state='dropped',
                      updated_at=?, delivery_claim=NULL,
                      delivery_claimed_at=NULL
               WHERE delegation_id=? AND delivery_state='pending'
                 AND delivery_claim=?""",
            (now, delegation_id, claim_id),
        )
        return cur.rowcount == 1


def complete_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Acknowledge acceptance for the consumer holding this claim."""
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_state='delivered',
                      delivered_at=?, updated_at=?, delivery_claim=NULL,
                      delivery_claimed_at=NULL
               WHERE delegation_id=? AND delivery_state='pending'
                 AND delivery_claim=?""",
            (now, now, delegation_id, claim_id),
        )
        return cur.rowcount == 1


def complete_event_delivery(evt: Dict[str, Any], claim_id: str) -> None:
    if claim_id and evt.get("type") == "async_delegation":
        complete_completion_delivery(str(evt.get("delegation_id") or ""), claim_id)


def release_event_delivery(evt: Dict[str, Any], claim_id: str) -> None:
    if claim_id and evt.get("type") == "async_delegation":
        release_completion_delivery(str(evt.get("delegation_id") or ""), claim_id)


def get_durable_delegation(delegation_id: str) -> Optional[Dict[str, Any]]:
    with _DB_LOCK, _transaction() as conn:
        row = conn.execute(
            """SELECT origin_session, state, dispatched_at, completed_at,
                      result_json, delivery_state, delivery_attempts,
                      origin_session_id
               FROM async_delegations WHERE delegation_id=?""", (delegation_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "delegation_id": delegation_id, "origin_session": row[0], "state": row[1],
        "dispatched_at": row[2], "completed_at": row[3],
        "result": json.loads(row[4]) if row[4] else None,
        "delivery_state": row[5], "delivery_attempts": row[6],
        "origin_session_id": row[7] or "",
    }


def _get_executor(max_workers: int) -> ThreadPoolExecutor:
    """Lazily create (or grow) the shared daemon executor.

    We never shrink — ThreadPoolExecutor can't resize — but if the configured
    cap grows between calls we rebuild a larger pool. Existing in-flight
    futures keep running on the old pool until it's garbage collected.
    """
    global _executor, _executor_max_workers
    with _executor_lock:
        if _executor is None or max_workers > _executor_max_workers:
            # Daemon threads: thread_name_prefix aids debugging in stack dumps.
            _executor = _DaemonThreadPoolExecutor(
                max_workers=max_workers,
                thread_name_prefix="async-delegate",
            )
            _executor_max_workers = max_workers
        return _executor


def active_count() -> int:
    """Number of async delegations currently running."""
    with _records_lock:
        return sum(
            1 for r in _records.values()
            if r.get("status") in {"running", "finalizing"}
        )


def _new_delegation_id() -> str:
    return f"deleg_{uuid.uuid4().hex[:8]}"


def _prune_completed_locked() -> None:
    """Drop the oldest completed records beyond the retention cap.

    Caller must hold ``_records_lock``.
    """
    completed = [
        (rid, r)
        for rid, r in _records.items()
        if r.get("status") != "running"
    ]
    if len(completed) <= _MAX_RETAINED_COMPLETED:
        return
    # Oldest-first by completion time (fall back to dispatch time).
    completed.sort(key=lambda kv: kv[1].get("completed_at") or kv[1].get("dispatched_at") or 0)
    for rid, _ in completed[: len(completed) - _MAX_RETAINED_COMPLETED]:
        _records.pop(rid, None)


def _current_origin_session_id() -> str:
    """Raw session id of the ORIGINATING api_server request, or ``""``.

    The obvious source — ``HERMES_SESSION_ID`` via ``get_session_env`` — is
    NOT safe to read at dispatch time: constructing a child agent
    (``agent/agent_init.py``) calls ``set_current_session_id(child.session_id)``,
    clobbering that ContextVar *and* ``os.environ`` with the subagent's
    internal ``{timestamp}_{uuid}`` id moments before the dispatch code reads
    it, so the completion wake would self-post into the subagent's own
    (unread) session instead of the spawner's.

    The request-scoped ``HERMES_SESSION_CHAT_ID`` binding survives child
    construction: ``_bind_api_server_session`` binds ``chat_id`` to the raw
    ``X-Hermes-Session-Id``, and its only writer is ``set_session_vars`` —
    ``set_current_session_id`` never touches it. Gate on the platform: on
    push platforms ``chat_id`` is a chat, not a session, so yield ``""``
    there.
    """
    try:
        from gateway.session_context import get_session_env

        if get_session_env("HERMES_SESSION_PLATFORM", "") != "api_server":
            return ""
        return get_session_env("HERMES_SESSION_CHAT_ID", "") or ""
    except Exception:
        return ""


def dispatch_async_delegation(
    *,
    goal: str,
    context: Optional[str],
    toolsets: Optional[List[str]],
    role: str,
    model: Optional[str],
    session_key: str,
    parent_session_id: Optional[str] = None,
    runner: Callable[[], Dict[str, Any]],
    origin_ui_session_id: str = "",
    origin_session_id: str = "",
    interrupt_fn: Optional[Callable[[], None]] = None,
    max_async_children: int = _DEFAULT_MAX_ASYNC_CHILDREN,
    review_receipt_id: str = "",
    review_ledger_path: str = "",
    delegation_id: str = "",
    review_fence_token: int = 0,
    candidate_hash: str = "",
    effective_execution_identity: str = "",
    recovery_attempt_id: str = "",
) -> Dict[str, Any]:
    """Spawn ``runner`` on the daemon executor and return a handle immediately.

    Parameters
    ----------
    goal, context, toolsets, role, model
        The dispatch-time task spec, captured verbatim for the rich
        completion block.
    session_key
        The gateway session_key (from ``tools.approval.get_current_session_key``)
        captured on the parent thread BEFORE dispatch, because the daemon
        worker thread won't carry the contextvar. Used to route the
        completion back to the originating session.
    parent_session_id
        The durable ``state.db`` session id of the parent agent that spawned
        the delegation. Carried on the completion event so the gateway can
        pin routing to the spawning session instead of recovering the latest
        ``ended_at IS NULL`` row for the peer tuple (#57498).
    runner
        Zero-arg callable that builds + runs the child and returns the same
        result dict ``_run_single_child`` produces. Runs on the worker thread.
    interrupt_fn
        Optional callable to signal the child to stop (used on shutdown /
        explicit cancel).
    max_async_children
        Concurrency cap. When at capacity the dispatch is REJECTED (the caller
        should fall back to sync or tell the user) rather than queued, so a
        runaway model can't pile up unbounded background work.

    Returns
    -------
    dict
        ``{"status": "dispatched", "delegation_id": ...}`` on success, or
        ``{"status": "rejected", "error": ...}`` when at capacity.
    """
    delegation_id = delegation_id or _new_delegation_id()
    ledger = None
    sealed_material_plan = False
    if review_receipt_id:
        if not review_ledger_path:
            return {"status": "rejected", "error": "receipt-bound review is missing its ledger path"}
        expected_ledger_path = (get_hermes_home() / "release-review-ledger.db").resolve()
        if Path(review_ledger_path).resolve() != expected_ledger_path:
            return {"status": "rejected", "error": "receipt-bound review must use the canonical Hermes ledger path"}
        try:
            from tools.release_review_ledger import ReleaseReviewLedger
            ledger = ReleaseReviewLedger(Path(review_ledger_path))
            ledger.assert_async_dispatch_binding(review_receipt_id, delegation_id, __import__("os").getpid())
            sealed_material_plan = bool(recovery_attempt_id and ledger.has_material_route_plan(review_receipt_id))
        except Exception as exc:
            return {"status": "rejected", "error": f"receipt-bound review was not admitted: {exc}"}
    dispatched_at = time.time()
    record: Dict[str, Any] = {
        "delegation_id": delegation_id,
        "goal": goal,
        "context": context,
        "toolsets": list(toolsets) if toolsets else None,
        "role": role,
        "model": model,
        "session_key": session_key,
        "origin_ui_session_id": origin_ui_session_id,
        "origin_session_id": origin_session_id,
        "review_receipt_id": review_receipt_id,
        "review_ledger_path": review_ledger_path,
        "parent_session_id": parent_session_id,
        "status": "dispatching",
        "dispatched_at": dispatched_at,
        "completed_at": None,
        "interrupt_fn": interrupt_fn,
        "event_sequence": 0,
        "review_fence_token": review_fence_token,
        "candidate_hash": candidate_hash,
        "effective_execution_identity": effective_execution_identity,
        "recovery_attempt_id": recovery_attempt_id,
    }
    record["event_stream_id"] = _async_event_stream_id(record)
    # Capacity check and record insert under ONE lock hold — checking
    # active_count() separately would let two concurrent dispatches (e.g.
    # from different gateway sessions) both pass the check and exceed the cap.
    with _records_lock:
        running = sum(
            1 for r in _records.values()
            if r.get("status") in ("dispatching", "running")
        )
        if running >= max_async_children:
            return {
                "status": "rejected",
                "error": (
                    f"Async delegation capacity reached ({max_async_children} "
                    f"running). Wait for one to finish (its result will re-enter "
                    f"the chat), or run this task synchronously "
                    f"(background=false). Raise delegation.max_concurrent_children in "
                    f"config.yaml to allow more concurrent background subagents."
                ),
            }
        _records[delegation_id] = record

    if not _persist_dispatch(record, max_async_children):
        with _records_lock:
            _records.pop(delegation_id, None)
        if ledger is not None:
            try:
                ledger.mark_launch_failed(review_receipt_id, "launch_rejected")
            except RuntimeError:
                pass
        return {
            "status": "rejected",
            "error": f"Async delegation capacity reached ({max_async_children} running across Hermes processes).",
        }
    # A recovery attempt is accepted only after its receipt-bound delegation is
    # durably present in state.db.  The durable row is the recovery authority
    # for a crashed owner; transitioning sooner would strand a retry fence with
    # no outbox record to reconcile.
    if ledger is not None and recovery_attempt_id:
        try:
            ledger.transition_recovery_attempt(
                recovery_attempt_id,
                fence_token=int(review_fence_token),
                state="ACCEPTED",
                outcome="receipt-bound material review accepted",
            )
        except Exception as exc:
            with _records_lock:
                _records.pop(delegation_id, None)
            _delete_durable_delegation(delegation_id)
            try:
                ledger.mark_launch_failed(review_receipt_id, "recovery_transition_failed")
            except RuntimeError:
                pass
            return {"status": "rejected", "error": f"material recovery admission failed: {exc}"}
    try:
        if ledger is not None:
            ledger.activate_async_dispatch(review_receipt_id, delegation_id, __import__("os").getpid())
        if not _activate_durable_dispatch(delegation_id):
            raise RuntimeError("durable dispatch lease was not activatable")
    except Exception as exc:
        with _records_lock:
            _records.pop(delegation_id, None)
        # Preserve the durable row as an unknown submission rather than
        # deleting recovery authority between activation failure and a caller's
        # error handler.  This also moves ACCEPTED/RUNNING attempts to the
        # fenced INTERRUPTED terminal state.
        _mark_submission_unknown(delegation_id, f"activation failed: {type(exc).__name__}")
        if ledger is not None:
            try:
                ledger.mark_launch_failed(review_receipt_id, "launch_rejected")
            except RuntimeError:
                pass
        return {"status": "rejected", "error": f"receipt-bound review activation failed: {exc}"}
    with _records_lock:
        record["status"] = "running"
    executor = _get_executor(max_async_children)

    def _worker() -> None:
        result: Dict[str, Any] = {}
        status = "error"
        try:
            result = runner() or {}
            status = result.get("status") or "completed"
        except Exception as exc:  # noqa: BLE001 — must never crash the worker
            logger.exception("Async delegation %s crashed", delegation_id)
            result = {
                "status": "error",
                "summary": None,
                "error": f"{type(exc).__name__}: {exc}",
                "api_calls": 0,
                "duration_seconds": round(time.time() - dispatched_at, 2),
            }
            status = "error"
        finally:
            _finalize(delegation_id, result, status)

    try:
        if not _claim_executor_submission(delegation_id):
            raise RuntimeError("durable submission outbox was already claimed or is not runnable")
        # Submission was atomically claimed.  This is the first point at which
        # a material retry is executing; before it a failed preflight/capacity
        # check leaves the attempt PREPARED and does not consume the retry.
        if ledger is not None and recovery_attempt_id and not sealed_material_plan:
            ledger.transition_recovery_attempt(
                recovery_attempt_id,
                fence_token=int(review_fence_token),
                state="RUNNING",
                outcome="executor submission claimed after durable outbox binding",
            )
        # Propagate the dispatching profile so the detached child resolves
        # get_hermes_home() under the right profile.
        executor.submit(propagate_context_to_thread(_worker))
    except Exception as exc:  # pragma: no cover — pool submit failure is rare
        with _records_lock:
            _records.pop(delegation_id, None)
        _mark_submission_unknown(delegation_id, f"executor submission failed: {type(exc).__name__}")
        if ledger is not None:
            try:
                ledger.mark_launch_failed(review_receipt_id)
            except RuntimeError:
                pass
        return {
            "status": "rejected",
            "error": f"Failed to schedule async delegation: {exc}",
        }
    logger.info(
        "Dispatched async delegation %s (session_key=%s): %s",
        delegation_id, session_key or "<cli>", (goal or "")[:80],
    )
    return {"status": "dispatched", "delegation_id": delegation_id, "review_receipt_id": review_receipt_id}


def _finalize(delegation_id: str, result: Dict[str, Any], status: str) -> None:
    """Mark a record complete and push the completion event onto the queue."""
    claimed = _begin_finalization(delegation_id)
    if claimed is None:
        return
    event_record, _interrupt_fn = claimed

    _push_completion_event(event_record, result, status)
    _finish_finalization(delegation_id, status)


def _begin_finalization(
    delegation_id: str,
) -> Optional[tuple[Dict[str, Any], Optional[Callable[[], None]]]]:
    """Atomically claim terminal delivery while keeping the record active."""
    with _records_lock:
        record = _records.get(delegation_id)
        if record is None or record.get("status") != "running":
            return
        fence_token = int(record.get("review_fence_token", 0) or 0)
    if not _claim_durable_terminal(delegation_id, fence_token):
        return
    with _records_lock:
        record = _records.get(delegation_id)
        if record is None or record.get("status") != "running":
            return
        # Stay active until durable persistence and queue publication finish;
        # otherwise process shutdown can kill this daemon worker in the narrow
        # gap after status flips but before SQLite is committed.
        record["status"] = "finalizing"
        record["completed_at"] = time.time()
        interrupt_fn = record.get("interrupt_fn")
        record["interrupt_fn"] = None  # drop the closure; child is done
        event_record = dict(record)

    return event_record, interrupt_fn


def _finish_finalization(delegation_id: str, status: str) -> None:
    with _records_lock:
        record = _records.get(delegation_id)
        if record is not None:
            record["status"] = status
        _prune_completed_locked()


def _push_completion_event(
    record: Dict[str, Any], result: Dict[str, Any], status: str
) -> None:
    """Push a type='async_delegation' event onto the shared completion queue.

    Best-effort: a failure here must not crash the worker, but it WOULD mean a
    silently-lost result, so we log loudly.
    """
    try:
        from tools.process_registry import process_registry
    except Exception as exc:  # pragma: no cover
        logger.error(
            "Async delegation %s finished but process_registry import failed; "
            "result lost: %s",
            record.get("delegation_id"), exc,
        )
        return

    is_material_review = bool(record.get("review_receipt_id"))
    summary = result.get("summary")
    error = result.get("error")
    dispatched_at = record.get("dispatched_at") or time.time()
    completed_at = record.get("completed_at") or time.time()

    evt = {
        "type": "async_delegation",
        "delegation_id": record.get("delegation_id"),
        # session_key routes the completion back to the originating gateway
        # session; empty string => CLI (single-session) path.
        "session_key": record.get("session_key", ""),
        "origin_ui_session_id": record.get("origin_ui_session_id", ""),
        "origin_session_id": record.get("origin_session_id", ""),
        "parent_session_id": record.get("parent_session_id"),
        "review_receipt_id": record.get("review_receipt_id", ""),
        "review_fence_token": int(record.get("review_fence_token", 0) or 0),
        "recovery_attempt_id": record.get("recovery_attempt_id", ""),
        "status": status,
        "duration_seconds": result.get(
            "duration_seconds", round(completed_at - dispatched_at, 2)
        ),
        "dispatched_at": dispatched_at,
        "completed_at": completed_at,
    }
    if is_material_review:
        # Receipt-bound work may contain raw reviewer output, prompts, command
        # paths, or provider diagnostics.  Completion events are user-facing
        # and durable in the async DB, so build a fresh allowlisted envelope.
        try:
            finding_count = int(result.get("finding_count", 0) or 0)
        except (TypeError, ValueError):
            finding_count = 0
        evt.update({
            "material_review_public": True,
            "lane": str(record.get("effective_execution_identity") or "")[:256],
            "verdict": "completed" if status == "completed" else "failed",
            "finding_count": max(0, min(999, finding_count)),
        })
        persisted_result = {
            "status": evt["verdict"], "finding_count": evt["finding_count"],
            "duration_seconds": evt["duration_seconds"],
        }
    else:
        evt.update({
            "review_ledger_path": record.get("review_ledger_path", ""),
            "goal": record.get("goal", ""), "context": record.get("context"),
            "toolsets": record.get("toolsets"), "role": record.get("role"),
            "model": result.get("model") or record.get("model"), "summary": summary,
            "error": error, "api_calls": result.get("api_calls", 0),
            "exit_reason": result.get("exit_reason"),
        })
        persisted_result = result
    evt.update(_next_completion_event_identity(record))
    if not _persist_completion(evt, persisted_result):
        logger.info("Async delegation %s terminal result lost the current fence", record.get("delegation_id"))
        return
    try:
        process_registry.completion_queue.put(evt)
    except Exception as exc:  # pragma: no cover
        logger.error(
            "Async delegation %s: failed to enqueue completion event; "
            "result lost: %s",
            record.get("delegation_id"), exc,
        )


def dispatch_async_delegation_batch(
    *,
    goals: List[str],
    context: Optional[str],
    toolsets: Optional[List[str]],
    role: str,
    model: Optional[str],
    session_key: str,
    parent_session_id: Optional[str] = None,
    runner: Callable[[], Dict[str, Any]],
    origin_ui_session_id: str = "",
    origin_session_id: str = "",
    interrupt_fn: Optional[Callable[[], None]] = None,
    max_async_children: int = _DEFAULT_MAX_ASYNC_CHILDREN,
    delegation_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Dispatch a WHOLE fan-out batch as ONE background unit.

    Unlike ``dispatch_async_delegation`` (which backs a single subagent),
    ``runner`` here runs the entire batch — it builds and joins on every child
    in parallel and returns the combined ``{"results": [...],
    "total_duration_seconds": N}`` dict that the synchronous path would have
    returned. We occupy ONE async slot for the whole batch (the in-batch
    parallelism is bounded separately by ``max_concurrent_children``), so a
    single ``delegate_task`` fan-out never exhausts the async pool by itself.

    When the batch finishes, a SINGLE completion event is pushed onto the
    shared ``process_registry.completion_queue`` carrying the full per-task
    ``results`` list, so the consolidated summaries re-enter the conversation
    as one message once every child is done — the chat is never blocked while
    they run.

    Returns ``{"status": "dispatched", "delegation_id": ...}`` on success or
    ``{"status": "rejected", "error": ...}`` when the async pool is at
    capacity.
    """
    delegation_id = delegation_id or _new_delegation_id()
    dispatched_at = time.time()
    n = len(goals)
    # A combined goal label for status listings / the completion header.
    combined_goal = (
        goals[0] if n == 1 else f"{n} parallel subagents: " + "; ".join(g[:40] for g in goals)
    )
    record: Dict[str, Any] = {
        "delegation_id": delegation_id,
        "goal": combined_goal,
        "goals": list(goals),
        "context": context,
        "toolsets": list(toolsets) if toolsets else None,
        "role": role,
        "model": model,
        "session_key": session_key,
        "origin_ui_session_id": origin_ui_session_id,
        "origin_session_id": origin_session_id,
        "parent_session_id": parent_session_id,
        "status": "dispatching",
        "dispatched_at": dispatched_at,
        "completed_at": None,
        "interrupt_fn": interrupt_fn,
        "is_batch": True,
        "event_sequence": 0,
    }
    record["event_stream_id"] = _async_event_stream_id(record)
    with _records_lock:
        running = sum(
            1 for r in _records.values()
            if r.get("status") in ("dispatching", "running")
        )
        if running >= max_async_children:
            return {
                "status": "rejected",
                "error": (
                    f"Async delegation capacity reached ({max_async_children} "
                    f"running). Wait for one to finish (its result will re-enter "
                    f"the chat), or raise delegation.max_concurrent_children in "
                    f"config.yaml to allow more concurrent background units."
                ),
            }
        _records[delegation_id] = record

    if not _persist_dispatch(record, max_async_children):
        with _records_lock:
            _records.pop(delegation_id, None)
        return {
            "status": "rejected",
            "error": f"Async delegation capacity reached ({max_async_children} running across Hermes processes).",
        }
    if not _activate_durable_dispatch(delegation_id):
        with _records_lock:
            _records.pop(delegation_id, None)
        _delete_durable_delegation(delegation_id)
        return {"status": "rejected", "error": "durable dispatch lease was not activatable"}
    with _records_lock:
        record["status"] = "running"
    executor = _get_executor(max_async_children)

    def _worker() -> None:
        combined: Dict[str, Any] = {}
        status = "error"
        try:
            combined = runner() or {}
            # Batch status: completed unless every child errored/was interrupted.
            child_results = combined.get("results") or []
            if child_results and all(
                (r.get("status") not in ("completed", "success"))
                for r in child_results
            ):
                status = "error"
            else:
                status = "completed"
        except Exception as exc:  # noqa: BLE001 — must never crash the worker
            logger.exception("Async delegation batch %s crashed", delegation_id)
            combined = {
                "results": [],
                "error": f"{type(exc).__name__}: {exc}",
                "total_duration_seconds": round(time.time() - dispatched_at, 2),
            }
            status = "error"
        finally:
            _finalize_batch(delegation_id, combined, status)

    try:
        # Propagate the dispatching profile to the detached batch children.
        executor.submit(propagate_context_to_thread(_worker))
    except Exception as exc:  # pragma: no cover
        with _records_lock:
            _records.pop(delegation_id, None)
        _delete_durable_delegation(delegation_id)
        return {
            "status": "rejected",
            "error": f"Failed to schedule async delegation batch: {exc}",
        }
    logger.info(
        "Dispatched async delegation batch %s (%d task(s), session_key=%s)",
        delegation_id, n, session_key or "<cli>",
    )
    return {"status": "dispatched", "delegation_id": delegation_id}


def _finalize_batch(
    delegation_id: str, combined: Dict[str, Any], status: str
) -> None:
    """Mark a batch record complete and push ONE combined completion event."""
    claimed = _begin_finalization(delegation_id)
    if claimed is None:
        return
    event_record, _interrupt_fn = claimed

    _push_batch_completion_event(event_record, combined, status)
    _finish_finalization(delegation_id, status)


def _push_batch_completion_event(
    event_record: Dict[str, Any], combined: Dict[str, Any], status: str
) -> None:
    """Push a combined async-delegation batch completion event."""
    try:
        from tools.process_registry import process_registry
    except Exception as exc:  # pragma: no cover
        logger.error(
            "Async delegation batch %s finished but process_registry import "
            "failed; result lost: %s",
            event_record.get("delegation_id"), exc,
        )
        return

    dispatched_at = event_record.get("dispatched_at") or time.time()
    completed_at = event_record.get("completed_at") or time.time()
    evt = {
        "type": "async_delegation",
        "delegation_id": event_record.get("delegation_id"),
        "session_key": event_record.get("session_key", ""),
        "origin_ui_session_id": event_record.get("origin_ui_session_id", ""),
        "origin_session_id": event_record.get("origin_session_id", ""),
        "parent_session_id": event_record.get("parent_session_id"),
        "goal": event_record.get("goal", ""),
        "goals": event_record.get("goals"),
        "context": event_record.get("context"),
        "toolsets": event_record.get("toolsets"),
        "role": event_record.get("role"),
        "model": event_record.get("model"),
        "status": status,
        "is_batch": True,
        # The full per-task results list — the formatter renders a
        # consolidated multi-task block from this.
        "results": combined.get("results") or [],
        # Per-task live transcript log paths (cache/delegation/live/...).
        # They persist after completion and double as the full-fidelity
        # operational record of each child's run.
        "live_transcripts": combined.get("live_transcripts"),
        "error": combined.get("error"),
        "total_duration_seconds": combined.get("total_duration_seconds"),
        "dispatched_at": dispatched_at,
        "completed_at": completed_at,
    }
    evt.update(_next_completion_event_identity(event_record))
    _persist_completion(evt, combined)
    try:
        process_registry.completion_queue.put(evt)
    except Exception as exc:  # pragma: no cover
        logger.error(
            "Async delegation batch %s: failed to enqueue completion event; "
            "result lost: %s",
            event_record.get("delegation_id"), exc,
        )


def list_async_delegations() -> List[Dict[str, Any]]:
    """Snapshot of async delegations (running + recently completed)."""
    with _records_lock:
        return [
            {key: value for key, value in record.items() if key != "interrupt_fn"}
            for record in _records.values()
        ]


def interrupt_all(reason: str = "shutdown") -> int:
    """Signal every running async delegation to stop. Returns how many.

    Used on ``/stop`` and gateway shutdown so a dangling background subagent
    can't keep burning tokens with no one listening. The child still emits a
    completion event (status='interrupted') via the normal finalize path.
    """
    count = 0
    with _records_lock:
        targets = [
            r for r in _records.values()
            if r.get("status") == "running"
        ]
    for r in targets:
        fn = r.get("interrupt_fn")
        if callable(fn):
            try:
                fn()
                count += 1
            except Exception as exc:
                logger.debug(
                    "interrupt_all: %s interrupt failed: %s",
                    r.get("delegation_id"), exc,
                )
    if count:
        logger.info("Interrupted %d async delegation(s) (%s)", count, reason)
    return count


def interrupt_review_receipt(review_receipt_id: str, reason: str = "release review deadline") -> int:
    """Interrupt only the async reviewer bound to one durable review receipt."""
    with _records_lock:
        targets = [
            record for record in _records.values()
            if record.get("review_receipt_id") == review_receipt_id
            and record.get("status") == "running"
        ]
    count = 0
    for record in targets:
        callback = record.get("interrupt_fn")
        if callable(callback):
            callback()
            count += 1
    return count


def force_timeout_review_receipt(review_receipt_id: str, reason: str = "release review deadline") -> int:
    """Close receipt-bound async work on the normal completion rail.

    Python cannot safely kill an arbitrary worker thread.  This function first
    asks the owned child to interrupt, then atomically finalizes its durable
    delegation as timed out.  A late runner result is ignored because the
    record is no longer runnable, so it cannot revive a released receipt.
    """
    interrupt_review_receipt(review_receipt_id, reason)
    with _records_lock:
        delegation_ids = [
            record["delegation_id"] for record in _records.values()
            if record.get("review_receipt_id") == review_receipt_id
            and record.get("status") == "running"
        ]
    finalized = 0
    for delegation_id in delegation_ids:
        claimed = _begin_finalization(delegation_id)
        if claimed is None:
            continue
        record, _interrupt = claimed
        completed_at = record.get("completed_at") or time.time()
        result = {
            "status": "timebox_expired", "summary": None,
            "error": f"{reason}; terminal receipt recorded while worker unwinds.",
            "api_calls": 0,
            "duration_seconds": round(completed_at - (record.get("dispatched_at") or completed_at), 2),
            "exit_reason": "timebox_expired",
        }
        _push_completion_event(record, result, "timebox_expired")
        _finish_finalization(delegation_id, "timebox_expired")
        finalized += 1
    return finalized


def interrupt_for_session(
    session_key: str = "",
    origin_ui_session_id: str = "",
    parent_session_id: str = "",
    reason: str = "session_end",
) -> int:
    """Signal running async delegations owned by ONE session to stop.

    A delegation's lifecycle is bound to the session that spawned it: when
    that session ends, its in-flight background subagents must end with it —
    a completed orphan would otherwise sit on the shared completion queue
    with no live owner, either leaking into another chat or burning tokens
    with no one listening (#55578).

    Selectors (any matching field claims the record):
    - ``origin_ui_session_id``: the live TUI tab/window that commissioned it.
    - ``session_key``: the durable routing key captured at dispatch.
    - ``parent_session_id``: the spawning agent's durable session-db id —
      the right selector for gateway chats, whose ``session_key`` (the
      platform conversation key) SURVIVES a ``/new`` reset while the
      session id rotates.

    Returns how many were interrupted.
    """
    if not session_key and not origin_ui_session_id and not parent_session_id:
        return 0
    count = 0
    with _records_lock:
        targets = [
            r for r in _records.values()
            if r.get("status") == "running"
            and (
                (origin_ui_session_id and str(r.get("origin_ui_session_id") or "") == origin_ui_session_id)
                or (session_key and str(r.get("session_key") or "") == session_key)
                or (parent_session_id and str(r.get("parent_session_id") or "") == parent_session_id)
            )
        ]
    for r in targets:
        fn = r.get("interrupt_fn")
        if callable(fn):
            try:
                fn()
                count += 1
            except Exception as exc:
                logger.debug(
                    "interrupt_for_session: %s interrupt failed: %s",
                    r.get("delegation_id"), exc,
                )
    if count:
        logger.info(
            "Interrupted %d async delegation(s) for ending session (%s)",
            count, reason,
        )
    return count


def _reset_for_tests() -> None:
    """Test-only: clear all state and tear down the executor."""
    global _executor, _executor_max_workers
    with _executor_lock:
        if _executor is not None:
            _executor.shutdown(wait=False)
        _executor = None
        _executor_max_workers = 0
    with _records_lock:
        _records.clear()
