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
    environment_fingerprint: str = "",
    evidence_fingerprint: str = "",
) -> Dict[str, Any]:
    """Immutable request identity, including output target and timebox."""
    identity: Dict[str, Any] = {
        "candidate_hash": _normalized(candidate_hash),
        "normalized_scope": _normalized(scope),
        "lane": _normalized(lane),
        "model": _normalized(model),
        "prompt_hash": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "normalized_output_path": _normalized(output_path),
        "environment_fingerprint": _normalized(environment_fingerprint),
        "evidence_fingerprint": _normalized(evidence_fingerprint),
    }
    if deadline_seconds is not None:
        identity["deadline_seconds"] = float(deadline_seconds)
    return identity


def _logical_identity(identity: Mapping[str, Any]) -> Dict[str, Any]:
    """Identity used to reject a dangerous variant of an already-admitted review."""
    return {
        key: identity[key]
        for key in (
            "candidate_hash", "normalized_scope", "lane", "model", "prompt_hash",
            "environment_fingerprint", "evidence_fingerprint",
        )
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
                        environment_fingerprint TEXT NOT NULL DEFAULT '', evidence_fingerprint TEXT NOT NULL DEFAULT '',
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
                    ("environment_fingerprint", "TEXT", "''"),
                    ("evidence_fingerprint", "TEXT", "''"),
                ):
                    if name not in columns:
                        nullability = "" if name == "launch_handle" else " NOT NULL"
                        conn.execute(
                            f"ALTER TABLE release_review_receipts ADD COLUMN {name} {sql_type}{nullability} DEFAULT {default}"
                        )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS release_review_receipts_logical ON release_review_receipts(logical_fingerprint)"
                )
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS workflow_validation_receipts (
                        validation_id TEXT PRIMARY KEY, fingerprint TEXT UNIQUE NOT NULL,
                        candidate_hash TEXT NOT NULL, environment_fingerprint TEXT NOT NULL,
                        evidence_fingerprint TEXT NOT NULL, command_hash TEXT NOT NULL,
                        state TEXT NOT NULL, result_json TEXT NOT NULL DEFAULT '{}',
                        created_at REAL NOT NULL, updated_at REAL NOT NULL
                    )"""
                )
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS workflow_timing_events (
                        event_id INTEGER PRIMARY KEY AUTOINCREMENT, receipt_id TEXT NOT NULL,
                        phase TEXT NOT NULL, category TEXT NOT NULL, started_at REAL NOT NULL,
                        ended_at REAL NOT NULL, evidence TEXT NOT NULL
                    )"""
                )
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS workflow_alerts (
                        fingerprint TEXT PRIMARY KEY, owner TEXT NOT NULL, evidence TEXT NOT NULL,
                        expires_at REAL NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL
                    )"""
                )
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS workflow_decisions (
                        decision_id TEXT PRIMARY KEY, fingerprint TEXT UNIQUE NOT NULL,
                        scope TEXT NOT NULL, rationale TEXT NOT NULL, owner TEXT NOT NULL,
                        safety_boundary TEXT NOT NULL, acceptance_criteria TEXT NOT NULL,
                        classification TEXT NOT NULL, created_at REAL NOT NULL
                    )"""
                )
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS workflow_operation_preflights (
                        fingerprint TEXT PRIMARY KEY, candidate_hash TEXT NOT NULL, operation TEXT NOT NULL,
                        controls_json TEXT NOT NULL, created_at REAL NOT NULL
                    )"""
                )
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS workflow_cleanup_receipts (
                        receipt_id TEXT PRIMARY KEY, candidate_hash TEXT NOT NULL, eligible INTEGER NOT NULL,
                        reason TEXT NOT NULL, evidence TEXT NOT NULL, created_at REAL NOT NULL
                    )"""
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
        # The review timebox begins only after a process/delegation is attached.
        # Admission and spawn work cannot consume it or race an attachment.
        if state == "running" and deadline_at <= now:
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
        environment_fingerprint: str = "",
        evidence_fingerprint: str = "",
    ) -> Dict[str, Any]:
        deadline_seconds = self._validate_deadline(deadline_seconds)
        identity = review_identity(
            candidate_hash, scope, lane, model, prompt, output_path, deadline_seconds,
            environment_fingerprint, evidence_fingerprint,
        )
        if not all(identity[key] for key in (
            "candidate_hash", "normalized_scope", "lane", "model", "normalized_output_path",
            "environment_fingerprint", "evidence_fingerprint",
        )):
            raise ValueError("candidate, scope, lane, model, output_path, environment_fingerprint, and evidence_fingerprint must be non-empty")
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
                        prompt_hash, environment_fingerprint, evidence_fingerprint, output_path, deadline_seconds,
                        deadline_at, state, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'admitted', ?, ?)""",
                    (
                        requested_id, fingerprint, logical_fingerprint, identity["candidate_hash"],
                        identity["normalized_scope"], identity["lane"], identity["model"], identity["prompt_hash"],
                        identity["environment_fingerprint"], identity["evidence_fingerprint"],
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

    def bind_async_dispatch(self, receipt_id: str, delegation_id: str, root_pid: int) -> None:
        """Durably bind one delegation before its worker is submitted.

        ``dispatching`` is deliberately distinct from ``running``: the
        dispatcher must activate this exact binding before executor submission,
        which prevents a fast worker from finalizing before the receipt has an
        owner/handle.
        """
        if not _normalized(delegation_id) or not isinstance(root_pid, int) or root_pid <= 0:
            raise ValueError("delegation identity and root process are required")
        conn = self._connect()
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                row = self._require_receipt(conn, receipt_id)
                if row[1] != "launching":
                    raise RuntimeError(f"receipt {receipt_id} cannot bind async dispatch from state {row[1]}")
                conn.execute(
                    "UPDATE release_review_receipts SET root_pid=?, leaf_pid=NULL, launch_handle=?, state='dispatching', updated_at=? "
                    "WHERE receipt_id=? AND state='launching'",
                    (root_pid, f"delegation:{delegation_id}", time.time(), receipt_id),
                )
        finally:
            conn.close()

    def activate_async_dispatch(self, receipt_id: str, delegation_id: str, root_pid: int) -> None:
        """Activate an already-bound delegation immediately before submission."""
        conn = self._connect()
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                row = self._require_receipt(conn, receipt_id)
                expected_handle = f"delegation:{delegation_id}"
                actual = conn.execute(
                    "SELECT launch_handle, deadline_seconds FROM release_review_receipts WHERE receipt_id=?",
                    (receipt_id,),
                ).fetchone()
                if row[1] != "dispatching" or actual is None or actual[0] != expected_handle or row[3] != root_pid:
                    raise RuntimeError(f"receipt {receipt_id} is not bound to delegation {delegation_id}")
                now = time.time()
                conn.execute(
                    "UPDATE release_review_receipts SET state='running', deadline_at=?, updated_at=? "
                    "WHERE receipt_id=? AND state='dispatching'",
                    (now + float(actual[1]), now, receipt_id),
                )
        finally:
            conn.close()

    def assert_async_dispatch_binding(self, receipt_id: str, delegation_id: str, root_pid: int) -> None:
        """Refuse a dispatcher that was not bound by the receipt launcher."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT state, launch_handle, root_pid FROM release_review_receipts WHERE receipt_id=?",
                (receipt_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError(f"unknown release-review receipt: {receipt_id}")
            if row[0] != "dispatching" or row[1] != f"delegation:{delegation_id}" or row[2] != root_pid:
                raise RuntimeError(f"receipt {receipt_id} is not bound to this async dispatcher")
        finally:
            conn.close()

    def receipt_state(self, receipt_id: str) -> str:
        """Read the durable state for a narrow launcher recovery branch."""
        conn = self._connect()
        try:
            return self._require_receipt(conn, receipt_id)[1]
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
                now = time.time()
                state = self._expire_if_due(conn, receipt_id, row[1], row[2], now)
                if state != "launching":
                    raise RuntimeError(f"receipt {receipt_id} cannot attach processes from state {row[1]}")
                deadline_seconds = conn.execute(
                    "SELECT deadline_seconds FROM release_review_receipts WHERE receipt_id=?", (receipt_id,)
                ).fetchone()[0]
                conn.execute(
                    "UPDATE release_review_receipts SET root_pid=?, leaf_pid=?, launch_handle=?, state='running', deadline_at=?, updated_at=? WHERE receipt_id=? AND state='launching'",
                    (root_pid, leaf_pid, _normalized(launch_handle), now + float(deadline_seconds), now, receipt_id),
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
                if row[1] not in {"launching", "dispatching", "running"}:
                    raise RuntimeError(f"receipt {receipt_id} cannot fail launch from state {row[1]}")
                conn.execute(
                    "UPDATE release_review_receipts SET state=?, updated_at=? WHERE receipt_id=?",
                    (state, time.time(), receipt_id),
                )
        finally:
            conn.close()

    @staticmethod
    def _required_text(value: Any, name: str) -> str:
        normalized = _normalized(str(value or ""))
        if not normalized:
            raise ValueError(f"{name} is required")
        return normalized

    def record_decision(
        self, *, decision_id: str, scope: str, rationale: str, owner: str,
        safety_boundary: str, acceptance_criteria: str, classification: str,
    ) -> Dict[str, Any]:
        """Persist the plan decision that authorizes a scoped workflow action."""
        values = {
            "decision_id": self._required_text(decision_id, "decision_id"),
            "scope": self._required_text(scope, "scope"),
            "rationale": self._required_text(rationale, "rationale"),
            "owner": self._required_text(owner, "owner"),
            "safety_boundary": self._required_text(safety_boundary, "safety_boundary"),
            "acceptance_criteria": self._required_text(acceptance_criteria, "acceptance_criteria"),
            "classification": self._required_text(classification, "classification"),
        }
        fingerprint = _fingerprint({key: value for key, value in values.items() if key != "decision_id"})
        conn = self._connect()
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute(
                    "SELECT fingerprint FROM workflow_decisions WHERE decision_id=?", (values["decision_id"],)
                ).fetchone()
                if existing:
                    return {"status": "existing" if existing[0] == fingerprint else "conflict", "decision_id": values["decision_id"]}
                conn.execute(
                    """INSERT INTO workflow_decisions
                       (decision_id, fingerprint, scope, rationale, owner, safety_boundary, acceptance_criteria, classification, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (values["decision_id"], fingerprint, values["scope"], values["rationale"], values["owner"],
                     values["safety_boundary"], values["acceptance_criteria"], values["classification"], time.time()),
                )
        finally:
            conn.close()
        return {"status": "recorded", "decision_id": values["decision_id"], "fingerprint": fingerprint}

    def require_decision(self, decision_id: str) -> None:
        conn = self._connect()
        try:
            if conn.execute("SELECT 1 FROM workflow_decisions WHERE decision_id=?", (self._required_text(decision_id, "decision_id"),)).fetchone() is None:
                raise RuntimeError(f"required workflow decision is absent: {decision_id}")
        finally:
            conn.close()

    def admit_validation(
        self, *, candidate_hash: str, environment_fingerprint: str, evidence_fingerprint: str,
        command: Iterable[str], validation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Cache validation only when candidate, environment, evidence, and command all match."""
        command_values = [self._required_text(value, "validation command") for value in command]
        if not command_values:
            raise ValueError("validation command is required")
        identity = {
            "candidate_hash": self._required_text(candidate_hash, "candidate_hash"),
            "environment_fingerprint": self._required_text(environment_fingerprint, "environment_fingerprint"),
            "evidence_fingerprint": self._required_text(evidence_fingerprint, "evidence_fingerprint"),
            "command_hash": _fingerprint({"command": command_values}),
        }
        fingerprint = _fingerprint(identity)
        requested_id = validation_id or f"validation_{uuid.uuid4().hex[:12]}"
        conn = self._connect()
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute(
                    "SELECT validation_id, state, result_json FROM workflow_validation_receipts WHERE fingerprint=?", (fingerprint,)
                ).fetchone()
                if existing:
                    return {"status": "cached" if existing[1] == "passed" else "existing", "validation_id": existing[0],
                            "state": existing[1], "result": json.loads(existing[2])}
                conflict = conn.execute("SELECT fingerprint FROM workflow_validation_receipts WHERE validation_id=?", (requested_id,)).fetchone()
                if conflict:
                    return {"status": "conflict", "validation_id": requested_id}
                conn.execute(
                    """INSERT INTO workflow_validation_receipts
                       (validation_id, fingerprint, candidate_hash, environment_fingerprint, evidence_fingerprint, command_hash, state, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, 'admitted', ?, ?)""",
                    (requested_id, fingerprint, identity["candidate_hash"], identity["environment_fingerprint"],
                     identity["evidence_fingerprint"], identity["command_hash"], time.time(), time.time()),
                )
        finally:
            conn.close()
        return {"status": "admitted", "validation_id": requested_id, "identity": identity}

    def finalize_validation(self, validation_id: str, *, passed: bool, evidence: Mapping[str, Any]) -> None:
        if not isinstance(evidence, Mapping) or not evidence:
            raise ValueError("validation evidence is required")
        conn = self._connect()
        try:
            with conn:
                row = conn.execute("SELECT state FROM workflow_validation_receipts WHERE validation_id=?", (validation_id,)).fetchone()
                if row is None or row[0] != "admitted":
                    raise RuntimeError(f"validation {validation_id} cannot finalize from state {row[0] if row else 'missing'}")
                conn.execute(
                    "UPDATE workflow_validation_receipts SET state=?, result_json=?, updated_at=? WHERE validation_id=?",
                    ("passed" if passed else "failed", json.dumps(dict(evidence), sort_keys=True), time.time(), validation_id),
                )
        finally:
            conn.close()

    def record_operation_preflight(self, *, candidate_hash: str, operation: str, controls: Mapping[str, Mapping[str, Any]]) -> str:
        """Record early, operation-scoped controls without treating them as live evidence."""
        candidate = self._required_text(candidate_hash, "candidate_hash")
        operation_name = self._required_text(operation, "operation")
        if not isinstance(controls, Mapping) or not controls:
            raise ValueError("operation preflight controls are required")
        normalized: Dict[str, Dict[str, Any]] = {}
        for name, control in controls.items():
            key = self._required_text(name, "preflight control")
            if not isinstance(control, Mapping) or control.get("status") not in {"verified", "ready"} or not control.get("evidence"):
                raise ValueError(f"{key} preflight requires a verified status and evidence")
            normalized[key] = dict(control)
        fingerprint = _fingerprint({"candidate_hash": candidate, "operation": operation_name, "controls": normalized})
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "INSERT OR IGNORE INTO workflow_operation_preflights (fingerprint, candidate_hash, operation, controls_json, created_at) VALUES (?, ?, ?, ?, ?)",
                    (fingerprint, candidate, operation_name, json.dumps(normalized, sort_keys=True), time.time()),
                )
        finally:
            conn.close()
        return fingerprint

    def record_timing(self, *, receipt_id: str, phase: str, category: str, started_at: float, ended_at: float, evidence: str) -> None:
        if category not in {"active", "external_wait"}:
            raise ValueError("timing category must be active or external_wait")
        if not all(math.isfinite(float(value)) for value in (started_at, ended_at)) or ended_at < started_at:
            raise ValueError("timing interval is invalid")
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO workflow_timing_events (receipt_id, phase, category, started_at, ended_at, evidence) VALUES (?, ?, ?, ?, ?, ?)",
                    (self._required_text(receipt_id, "receipt_id"), self._required_text(phase, "phase"), category,
                     float(started_at), float(ended_at), self._required_text(evidence, "timing evidence")),
                )
        finally:
            conn.close()

    def timing_summary(self, receipt_id: str) -> Dict[str, float]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT category, COALESCE(SUM(ended_at - started_at), 0) FROM workflow_timing_events WHERE receipt_id=? GROUP BY category",
                (receipt_id,),
            ).fetchall()
        finally:
            conn.close()
        totals = {"active": 0.0, "external_wait": 0.0}
        totals.update({row[0]: float(row[1]) for row in rows})
        return totals

    def record_alert(
        self, *, fingerprint: str, candidate_hash: str, terminal_state: str,
        owner: str, evidence: str, ttl_seconds: float,
    ) -> Dict[str, Any]:
        if not math.isfinite(float(ttl_seconds)) or not 0 < float(ttl_seconds) <= 7 * 24 * 60 * 60:
            raise ValueError("alert ttl must be between 0 and 604800 seconds")
        key = _fingerprint({
            "fingerprint": self._required_text(fingerprint, "alert fingerprint"),
            "candidate_hash": self._required_text(candidate_hash, "candidate_hash"),
            "terminal_state": self._required_text(terminal_state, "terminal_state"),
        })
        now = time.time()
        conn = self._connect()
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT expires_at FROM workflow_alerts WHERE fingerprint=?", (key,)).fetchone()
                if row and float(row[0]) > now:
                    return {"status": "suppressed", "expires_at": float(row[0])}
                expires_at = now + float(ttl_seconds)
                conn.execute(
                    "INSERT OR REPLACE INTO workflow_alerts (fingerprint, owner, evidence, expires_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (key, self._required_text(owner, "alert owner"), self._required_text(evidence, "alert evidence"), expires_at, now, now),
                )
        finally:
            conn.close()
        return {"status": "recorded", "expires_at": expires_at}

    def record_cleanup_eligibility(self, *, receipt_id: str, candidate_hash: str, reason: str, evidence: str) -> None:
        """Mark a terminal build cache eligible for later cleanup; never delete it here."""
        conn = self._connect()
        try:
            row = conn.execute("SELECT state FROM release_review_receipts WHERE receipt_id=?", (receipt_id,)).fetchone()
            if row is None or row[0] != "completed":
                raise RuntimeError(f"cleanup requires a verified completed receipt: {receipt_id}")
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO workflow_cleanup_receipts (receipt_id, candidate_hash, eligible, reason, evidence, created_at) VALUES (?, ?, 1, ?, ?, ?)",
                    (receipt_id, self._required_text(candidate_hash, "candidate_hash"), self._required_text(reason, "cleanup reason"),
                     self._required_text(evidence, "cleanup evidence"), time.time()),
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
                # A timeout is terminal.  Preserve its first explicit completion
                # receipt, but never let a late worker result overwrite it.
                if row[1] == "timebox_expired":
                    if _normalized(status) != "timebox_expired":
                        return False
                    updated = conn.execute(
                        "UPDATE release_review_receipts SET terminal_json=?, updated_at=? "
                        "WHERE receipt_id=? AND state='timebox_expired' AND terminal_json='{}'",
                        (json.dumps(terminal, sort_keys=True), time.time(), receipt_id),
                    )
                    return updated.rowcount == 1
                if row[1] in {"completed", "failed", "unknown"}:
                    return False
                state = (
                    "completed" if status == "completed"
                    else "failed" if status in {"error", "failed", "stalled"}
                    else "timebox_expired" if _normalized(status) == "timebox_expired"
                    else "unknown"
                )
                conn.execute(
                    "UPDATE release_review_receipts SET state=?, terminal_json=?, updated_at=? WHERE receipt_id=?",
                    (state, json.dumps(terminal, sort_keys=True), time.time(), receipt_id),
                )
                return True
        finally:
            conn.close()

    def finalize_direct_receipt(self, receipt_id: str, return_code: int, evidence: Mapping[str, Any]) -> bool:
        return self.finalize_async_receipt(
            receipt_id,
            "completed" if return_code == 0 else "failed",
            {"return_code": return_code, **dict(evidence)},
        )

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
                    "WHERE state='running' AND deadline_at<=?",
                    (now, now),
                )
                return result.rowcount
        finally:
            conn.close()

    def deadline_watch_state(self, receipt_id: str, now: Optional[float] = None) -> str:
        """Transition one due receipt, without coupling its timeout to other rows.

        Returns ``pending``, ``expired``, or ``terminal``.  A watchdog stops
        permanently for a normal terminal result, and signals expiry exactly
        once for its own receipt.
        """
        now = time.time() if now is None else now
        conn = self._connect()
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                row = self._require_receipt(conn, receipt_id)
                state, deadline_at = row[1], row[2]
                if state in {"completed", "failed", "unknown", "launch_failed", "launch_rejected"}:
                    return "terminal"
                if state == "timebox_expired":
                    return "expired"
                if state != "running":
                    return "pending"
                if deadline_at > now:
                    return "pending"
                conn.execute(
                    "UPDATE release_review_receipts SET state='timebox_expired', updated_at=? WHERE receipt_id=?",
                    (now, receipt_id),
                )
                return "expired"
        finally:
            conn.close()

    def expire_receipt_if_due(self, receipt_id: str, now: Optional[float] = None) -> bool:
        return self.deadline_watch_state(receipt_id, now) == "expired"

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
                state = self.deadline_watch_state(receipt_id)
                if state == "expired" and callable(on_timeout):
                    on_timeout()
                elif state == "pending":
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
