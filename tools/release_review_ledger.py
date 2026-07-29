"""Durable admission receipts for bounded release-review launchers.

Direct-shell and async launchers share this one SQLite ledger.  A receipt is
claimed *before* a reviewer starts, so two processes cannot spend the same
review lane merely because they observed the same candidate at once.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional


_REQUIRED_PREFLIGHT = ("target", "install", "restart", "health", "rollback")
_MAX_DEADLINE_SECONDS = 60 * 60


def _normalized(value: str) -> str:
    return " ".join((value or "").split())


def _fingerprint(identity: Mapping[str, Any]) -> str:
    payload = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def review_identity(
    candidate_hash: str,
    scope: str,
    lane: str,
    model: str,
    prompt: str,
    output_path: str = "",
    deadline_seconds: Optional[float] = None,
) -> Dict[str, Any]:
    """Immutable request identity, including output target and timebox."""
    identity: Dict[str, Any] = {
        "candidate_hash": _normalized(candidate_hash),
        "normalized_scope": _normalized(scope),
        "lane": _normalized(lane),
        "model": _normalized(model),
        "prompt_hash": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "normalized_output_path": _normalized(output_path),
    }
    if deadline_seconds is not None:
        identity["deadline_seconds"] = float(deadline_seconds)
    return identity


def _logical_identity(identity: Mapping[str, Any]) -> Dict[str, Any]:
    """Identity used to reject a dangerous variant of an already-admitted review."""
    return {
        key: identity[key]
        for key in ("candidate_hash", "normalized_scope", "lane", "model", "prompt_hash")
    }


class ReleaseReviewLedger:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS release_review_receipts (
                        receipt_id TEXT PRIMARY KEY, fingerprint TEXT UNIQUE NOT NULL,
                        logical_fingerprint TEXT NOT NULL,
                        candidate_hash TEXT NOT NULL, normalized_scope TEXT NOT NULL,
                        lane TEXT NOT NULL, model TEXT NOT NULL, prompt_hash TEXT NOT NULL,
                        output_path TEXT NOT NULL, deadline_seconds REAL NOT NULL,
                        deadline_at REAL NOT NULL, state TEXT NOT NULL,
                        root_pid INTEGER, leaf_pid INTEGER, launch_handle TEXT,
                        finding_map_json TEXT NOT NULL DEFAULT '[]', preflight_json TEXT NOT NULL DEFAULT '{}',
                        terminal_json TEXT NOT NULL DEFAULT '{}',
                        created_at REAL NOT NULL, updated_at REAL NOT NULL
                    )"""
                )
                columns = {row[1] for row in conn.execute("PRAGMA table_info(release_review_receipts)")}
                for name, sql_type, default in (
                    ("logical_fingerprint", "TEXT", "''"),
                    ("output_path", "TEXT", "''"),
                    ("deadline_seconds", "REAL", "0"),
                    ("launch_handle", "TEXT", "NULL"),
                    ("terminal_json", "TEXT", "'{}'"),
                ):
                    if name not in columns:
                        nullability = "" if name == "launch_handle" else " NOT NULL"
                        conn.execute(
                            f"ALTER TABLE release_review_receipts ADD COLUMN {name} {sql_type}{nullability} DEFAULT {default}"
                        )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS release_review_receipts_logical ON release_review_receipts(logical_fingerprint)"
                )
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=0.1)

    @staticmethod
    def _validate_deadline(deadline_seconds: float) -> float:
        value = float(deadline_seconds)
        if not math.isfinite(value) or not 0 < value <= _MAX_DEADLINE_SECONDS:
            raise ValueError(f"deadline_seconds must be finite and between 0 and {_MAX_DEADLINE_SECONDS}")
        return value

    @staticmethod
    def _require_receipt(conn: sqlite3.Connection, receipt_id: str) -> sqlite3.Row | tuple:
        row = conn.execute(
            "SELECT receipt_id, state, deadline_at, root_pid, leaf_pid, preflight_json, finding_map_json "
            "FROM release_review_receipts WHERE receipt_id=?",
            (receipt_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown release review receipt: {receipt_id}")
        return row

    @staticmethod
    def _expire_if_due(conn: sqlite3.Connection, receipt_id: str, state: str, deadline_at: float, now: float) -> str:
        if state in {"admitted", "preflight_ready", "launching", "running"} and deadline_at <= now:
            conn.execute(
                "UPDATE release_review_receipts SET state='timebox_expired', updated_at=? WHERE receipt_id=?",
                (now, receipt_id),
            )
            return "timebox_expired"
        return state

    def admit(
        self,
        *,
        candidate_hash: str,
        scope: str,
        lane: str,
        model: str,
        prompt: str,
        deadline_seconds: float,
        output_path: str,
        receipt_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        deadline_seconds = self._validate_deadline(deadline_seconds)
        identity = review_identity(candidate_hash, scope, lane, model, prompt, output_path, deadline_seconds)
        if not all(identity[key] for key in ("candidate_hash", "normalized_scope", "lane", "model", "normalized_output_path")):
            raise ValueError("candidate, scope, lane, model, and output_path must be non-empty")
        fingerprint = _fingerprint(identity)
        logical_fingerprint = _fingerprint(_logical_identity(identity))
        now = time.time()
        requested_id = receipt_id or f"review_{uuid.uuid4().hex[:12]}"
        conn = self._connect()
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                exact = conn.execute(
                    "SELECT receipt_id, state, deadline_at, output_path FROM release_review_receipts WHERE fingerprint=?",
                    (fingerprint,),
                ).fetchone()
                if exact:
                    state = self._expire_if_due(conn, exact[0], exact[1], exact[2], now)
                    return {
                        "status": "existing", "receipt_id": exact[0], "state": state,
                        "deadline_at": exact[2], "output_path": exact[3], "identity": identity,
                    }
                variant = conn.execute(
                    "SELECT receipt_id FROM release_review_receipts WHERE logical_fingerprint=?",
                    (logical_fingerprint,),
                ).fetchone()
                if variant:
                    return {
                        "status": "conflict", "receipt_id": variant[0], "identity": identity,
                        "reason": "output_path_or_deadline_differs",
                    }
                conflicting = conn.execute(
                    "SELECT fingerprint FROM release_review_receipts WHERE receipt_id=?", (requested_id,)
                ).fetchone()
                if conflicting:
                    return {"status": "conflict", "receipt_id": requested_id, "identity": identity, "reason": "receipt_id_reused"}
                conn.execute(
                    """INSERT INTO release_review_receipts
                       (receipt_id, fingerprint, logical_fingerprint, candidate_hash, normalized_scope, lane, model,
                        prompt_hash, output_path, deadline_seconds, deadline_at, state, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'admitted', ?, ?)""",
                    (
                        requested_id, fingerprint, logical_fingerprint, identity["candidate_hash"],
                        identity["normalized_scope"], identity["lane"], identity["model"], identity["prompt_hash"],
                        identity["normalized_output_path"], deadline_seconds, now + deadline_seconds, now, now,
                    ),
                )
        finally:
            conn.close()
        return {"status": "admitted", "receipt_id": requested_id, "identity": identity}

    @staticmethod
    def _validate_preflight(preflight: Mapping[str, Any]) -> Dict[str, Any]:
        if not isinstance(preflight, Mapping):
            raise ValueError("preflight must be a mapping")
        missing = set(_REQUIRED_PREFLIGHT).difference(preflight)
        if missing:
            raise ValueError(f"missing preflight controls: {sorted(missing)}")
        normalized: Dict[str, Any] = {}
        for name in _REQUIRED_PREFLIGHT:
            value = preflight[name]
            if not isinstance(value, Mapping) or not value.get("evidence") or value.get("status") not in {"verified", "ready"}:
                raise ValueError(f"{name} preflight requires a verified status and non-empty evidence")
            normalized[name] = dict(value)
        health = normalized["health"]
        if health.get("authenticated") is not True or not health.get("method") or not health.get("endpoint"):
            raise ValueError("health preflight requires authenticated endpoint and method evidence")
        return normalized

    def capture_preflight(self, receipt_id: str, preflight: Mapping[str, Any]) -> None:
        verified = self._validate_preflight(preflight)
        now = time.time()
        conn = self._connect()
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                row = self._require_receipt(conn, receipt_id)
                state = self._expire_if_due(conn, receipt_id, row[1], row[2], now)
                if state != "admitted":
                    raise RuntimeError(f"receipt {receipt_id} cannot capture preflight from state {state}")
                conn.execute(
                    "UPDATE release_review_receipts SET preflight_json=?, state='preflight_ready', updated_at=? WHERE receipt_id=?",
                    (json.dumps(verified, sort_keys=True), now, receipt_id),
                )
        finally:
            conn.close()

    def claim_launch(self, receipt_id: str) -> Dict[str, Any]:
        """Atomically reserve a preflighted receipt before a launcher starts work."""
        now = time.time()
        conn = self._connect()
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                row = self._require_receipt(conn, receipt_id)
                state = self._expire_if_due(conn, receipt_id, row[1], row[2], now)
                if state != "preflight_ready":
                    return {"status": "not_claimed", "receipt_id": receipt_id, "state": state}
                conn.execute(
                    "UPDATE release_review_receipts SET state='launching', updated_at=? WHERE receipt_id=? AND state='preflight_ready'",
                    (now, receipt_id),
                )
                return {"status": "claimed", "receipt_id": receipt_id, "state": "launching"}
        finally:
            conn.close()

    def assert_launching(self, receipt_id: str) -> None:
        """Confirm an async dispatcher was reached only through an active claim."""
        conn = self._connect()
        try:
            row = self._require_receipt(conn, receipt_id)
            if row[1] != "launching":
                raise RuntimeError(f"receipt {receipt_id} is not launchable from state {row[1]}")
        finally:
            conn.close()

    def attach_processes(self, receipt_id: str, root_pid: int, leaf_pid: Optional[int], launch_handle: str) -> None:
        if not isinstance(root_pid, int) or root_pid <= 0 or (leaf_pid is not None and (not isinstance(leaf_pid, int) or leaf_pid <= 0)):
            raise ValueError("process identifiers must be positive integers")
        if not _normalized(launch_handle):
            raise ValueError("launch_handle is required")
        conn = self._connect()
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                row = self._require_receipt(conn, receipt_id)
                state = self._expire_if_due(conn, receipt_id, row[1], row[2], time.time())
                if state != "launching":
                    raise RuntimeError(f"receipt {receipt_id} cannot attach processes from state {row[1]}")
                conn.execute(
                    "UPDATE release_review_receipts SET root_pid=?, leaf_pid=?, launch_handle=?, state='running', updated_at=? WHERE receipt_id=? AND state='launching'",
                    (root_pid, leaf_pid, _normalized(launch_handle), time.time(), receipt_id),
                )
        finally:
            conn.close()

    def mark_launch_failed(self, receipt_id: str, state: str = "launch_failed") -> None:
        if state not in {"launch_failed", "launch_rejected"}:
            raise ValueError(f"unsupported launch-failure state: {state}")
        conn = self._connect()
        try:
            with conn:
                row = self._require_receipt(conn, receipt_id)
                if row[1] != "launching":
                    raise RuntimeError(f"receipt {receipt_id} cannot fail launch from state {row[1]}")
                conn.execute(
                    "UPDATE release_review_receipts SET state=?, updated_at=? WHERE receipt_id=?",
                    (state, time.time(), receipt_id),
                )
        finally:
            conn.close()

    def append_findings(self, receipt_id: str, findings: Iterable[Mapping[str, Any]]) -> None:
        """Append validated finding-to-file/test bindings without losing prior evidence."""
        incoming = []
        for item in findings:
            if not isinstance(item, Mapping):
                raise ValueError("finding must be a mapping")
            finding_id = _normalized(str(item.get("finding_id", "")))
            files = item.get("files")
            tests = item.get("tests")
            if not finding_id or not isinstance(files, list) or not files or not isinstance(tests, list) or not tests:
                raise ValueError("each finding needs id plus non-empty files and tests")
            normalized_files = sorted({_normalized(str(value)) for value in files if _normalized(str(value))})
            normalized_tests = sorted({_normalized(str(value)) for value in tests if _normalized(str(value))})
            if not normalized_files or not normalized_tests:
                raise ValueError("each finding needs non-empty normalized files and tests")
            incoming.append({
                "finding_id": finding_id,
                "files": normalized_files,
                "tests": normalized_tests,
                "disposition": _normalized(str(item.get("disposition", "open"))),
            })
        if not incoming:
            raise ValueError("at least one finding is required")
        now = time.time()
        conn = self._connect()
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                row = self._require_receipt(conn, receipt_id)
                if row[1] not in {"running", "timebox_expired"}:
                    raise RuntimeError(f"receipt {receipt_id} cannot append findings from state {row[1]}")
                existing = json.loads(row[6])
                existing_ids = {entry["finding_id"] for entry in existing}
                duplicate_ids = existing_ids.intersection(entry["finding_id"] for entry in incoming)
                if duplicate_ids:
                    raise ValueError(f"finding ids are immutable: {sorted(duplicate_ids)}")
                conn.execute(
                    "UPDATE release_review_receipts SET finding_map_json=?, updated_at=? WHERE receipt_id=?",
                    (json.dumps(existing + incoming, sort_keys=True), now, receipt_id),
                )
        finally:
            conn.close()

    def finalize_async_receipt(self, receipt_id: str, status: str, evidence: Mapping[str, Any]) -> bool:
        """Idempotently preserve one async terminal outcome and its output evidence."""
        terminal = {"status": _normalized(status), "evidence": dict(evidence)}
        conn = self._connect()
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                row = self._require_receipt(conn, receipt_id)
                if row[1] in {"completed", "failed", "unknown", "timebox_expired"}:
                    return False
                state = "completed" if status == "completed" else "failed" if status in {"error", "failed", "stalled"} else "unknown"
                conn.execute(
                    "UPDATE release_review_receipts SET state=?, terminal_json=?, updated_at=? WHERE receipt_id=?",
                    (state, json.dumps(terminal, sort_keys=True), time.time(), receipt_id),
                )
                return True
        finally:
            conn.close()

    def incremental_scope(self, receipt_id: str) -> Dict[str, list[str]]:
        conn = self._connect()
        try:
            row = self._require_receipt(conn, receipt_id)
            findings = json.loads(row[6])
            return {
                "files": sorted({file for finding in findings for file in finding["files"]}),
                "tests": sorted({test for finding in findings for test in finding["tests"]}),
            }
        finally:
            conn.close()

    def expire_due(self, now: Optional[float] = None) -> int:
        now = time.time() if now is None else now
        conn = self._connect()
        try:
            with conn:
                result = conn.execute(
                    "UPDATE release_review_receipts SET state='timebox_expired', updated_at=? "
                    "WHERE state IN ('admitted','preflight_ready','launching','running') AND deadline_at<=?",
                    (now, now),
                )
                return result.rowcount
        finally:
            conn.close()

    def expire_receipt_if_due(self, receipt_id: str, now: Optional[float] = None) -> bool:
        """Transition one due receipt, without coupling its timeout to other rows.

        ``True`` also means the receipt was already terminally expired.  A
        watchdog that wakes after an external sweep must still signal its own
        reviewer; otherwise simultaneous expiry can leave one child running.
        """
        now = time.time() if now is None else now
        conn = self._connect()
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                row = self._require_receipt(conn, receipt_id)
                state, deadline_at = row[1], row[2]
                if deadline_at > now:
                    return False
                if state in {"admitted", "preflight_ready", "launching", "running"}:
                    conn.execute(
                        "UPDATE release_review_receipts SET state='timebox_expired', updated_at=? WHERE receipt_id=?",
                        (now, receipt_id),
                    )
                    return True
                return state == "timebox_expired"
        finally:
            conn.close()

    def supervise_deadline(self, receipt_id: str, on_timeout) -> None:
        """Schedule one daemon timeout callback for a running review receipt."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT state, deadline_at FROM release_review_receipts WHERE receipt_id=?", (receipt_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown release review receipt: {receipt_id}")
            delay = max(0.0, float(row[1]) - time.time())
        finally:
            conn.close()

        def _timeout():
            try:
                if self.expire_receipt_if_due(receipt_id) and callable(on_timeout):
                    on_timeout()
                else:
                    retry = threading.Timer(0.01, _timeout)
                    retry.daemon = True
                    retry.start()
            except sqlite3.OperationalError:
                # Simultaneous watchdogs contend briefly for the same SQLite
                # write lock. Retry this receipt only; a global sweep would
                # recreate the cross-receipt timeout bug this guard prevents.
                retry = threading.Timer(0.01, _timeout)
                retry.daemon = True
                retry.start()

        timer = threading.Timer(delay, _timeout)
        timer.daemon = True
        timer.start()
