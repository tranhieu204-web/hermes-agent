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
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence
from urllib.parse import urlsplit


_REQUIRED_PREFLIGHT = ("target", "install", "restart", "health", "rollback")
_MAX_DEADLINE_SECONDS = 60 * 60


def _normalized(value: str) -> str:
    return " ".join((value or "").split())


def _fingerprint(identity: Mapping[str, Any]) -> str:
    payload = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _canonical_token(value: Any) -> str:
    """Normalize presentation aliases without relying on config labels."""
    return "-".join(
        part for part in "".join(
            char.lower() if char.isalnum() else " " for char in str(value or "")
        ).split()
        if part
    )


def canonical_effective_route_identity(
    *,
    provider: str,
    base_url: str | None,
    account_secret: str | None,
    model: str,
    adapter_kind: str | None = None,
    auth_kind: str | None = None,
    auth_source: str | None = None,
    executable: str | None = None,
) -> str:
    """Secret-free identity for the resolved provider endpoint/account/model.

    A configured lane id is intentionally excluded. Endpoint and account inputs
    are hashed so aliases collapse without writing URLs or credentials into the
    review ledger.
    """
    raw_endpoint = str(base_url or "").strip()
    parsed = urlsplit(raw_endpoint) if raw_endpoint else None
    endpoint = (
        # URL scheme and host are case-insensitive, but endpoint path case is
        # not: lowercasing it could falsely collapse two distinct backing
        # provider routes into one independence lane.
        (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"))
        if parsed else ()
    )
    # ``account_secret`` is available on the direct provider path.  Fleet
    # adapters deliberately never receive it, so a stable non-secret
    # qualification source is the only permissible substitute there.  Both
    # values are hashed before the identity is returned.
    account_material = str(account_secret or auth_source or "")
    # Installation location is qualification evidence, not backing-route
    # identity.  Counting it would let two copies of one official CLI satisfy
    # a multi-model independence gate.
    _ = executable
    return "|".join((
        f"provider={_canonical_token(provider) or 'inherited'}",
        f"endpoint={_fingerprint({'endpoint': endpoint})[:24]}",
        f"account={_fingerprint({'account': account_material})[:24]}",
        f"model={_canonical_token(model) or 'inherited-model'}",
        f"adapter={_canonical_token(adapter_kind) or 'inherited-adapter'}",
        f"auth={_canonical_token(auth_kind) or 'unknown-auth'}",
    ))


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
    effective_route_identity: str = "",
    review_lens: str = "",
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
        "effective_route_identity": _normalized(effective_route_identity),
        "review_lens": _normalized(review_lens).lower(),
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
            "effective_route_identity",
            "review_lens",
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
                        effective_route_identity TEXT NOT NULL DEFAULT '', review_lens TEXT NOT NULL DEFAULT '',
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
                    ("effective_route_identity", "TEXT", "''"),
                    ("review_lens", "TEXT", "''"),
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
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS workflow_recovery_packets (
                        packet_hash TEXT PRIMARY KEY, identity_fingerprint TEXT NOT NULL,
                        schema_version INTEGER NOT NULL, packet_json TEXT NOT NULL,
                        predecessor_packet_hash TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL
                    )"""
                )
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS workflow_recovery_attempts (
                        attempt_id TEXT PRIMARY KEY, identity_fingerprint TEXT NOT NULL,
                        candidate_hash TEXT NOT NULL, environment_fingerprint TEXT NOT NULL,
                        scope_hash TEXT NOT NULL, failure_fingerprint TEXT NOT NULL,
                        normalized_task_hash TEXT NOT NULL, mode TEXT NOT NULL, ordinal INTEGER NOT NULL,
                        generation INTEGER NOT NULL, owner TEXT NOT NULL, effective_route_identity TEXT NOT NULL,
                        lens TEXT NOT NULL, packet_hash TEXT NOT NULL, predecessor_attempt_id TEXT,
                        fence_token INTEGER NOT NULL UNIQUE, state TEXT NOT NULL, outcome TEXT NOT NULL DEFAULT '',
                        created_at REAL NOT NULL, updated_at REAL NOT NULL,
                        UNIQUE(identity_fingerprint, mode, ordinal, generation)
                    )"""
                )
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS workflow_recovery_phase_admissions (
                        phase_fingerprint TEXT PRIMARY KEY, candidate_hash TEXT NOT NULL,
                        environment_fingerprint TEXT NOT NULL, failure_fingerprint TEXT NOT NULL,
                        mode TEXT NOT NULL, ordinal INTEGER NOT NULL, status TEXT NOT NULL,
                        selected_lanes_json TEXT NOT NULL, unavailable_lanes_json TEXT NOT NULL,
                        capacity_limit INTEGER NOT NULL, token_budget INTEGER NOT NULL,
                        created_at REAL NOT NULL
                    )"""
                )
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS workflow_material_route_plans (
                        receipt_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL,
                        fence_token INTEGER NOT NULL, route_identity TEXT NOT NULL,
                        review_lens TEXT NOT NULL, route_json TEXT NOT NULL,
                        plan_json TEXT NOT NULL, plan_hash TEXT NOT NULL UNIQUE,
                        saga_state TEXT NOT NULL DEFAULT 'SEALED',
                        terminal_json TEXT NOT NULL DEFAULT '{}',
                        external_handle_id TEXT NOT NULL DEFAULT '',
                        external_pid INTEGER, external_host_start_time INTEGER,
                        created_at REAL NOT NULL, updated_at REAL NOT NULL DEFAULT 0
                    )"""
                )
                material_columns = {row[1] for row in conn.execute("PRAGMA table_info(workflow_material_route_plans)")}
                for name, sql_type, default in (
                    ("saga_state", "TEXT", "'SEALED'"),
                    ("terminal_json", "TEXT", "'{}'"),
                    ("external_handle_id", "TEXT", "''"),
                    ("external_pid", "INTEGER", "0"),
                    ("external_host_start_time", "INTEGER", "0"),
                    ("updated_at", "REAL", "0"),
                ):
                    if name not in material_columns:
                        conn.execute(
                            f"ALTER TABLE workflow_material_route_plans ADD COLUMN {name} {sql_type} NOT NULL DEFAULT {default}"
                        )
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS workflow_recovery_stops (
                        stop_fingerprint TEXT PRIMARY KEY, candidate_hash TEXT NOT NULL,
                        environment_fingerprint TEXT NOT NULL, scope_hash TEXT NOT NULL,
                        failure_fingerprint TEXT NOT NULL, normalized_task_hash TEXT NOT NULL,
                        generation INTEGER NOT NULL, reason TEXT NOT NULL, created_at REAL NOT NULL
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
        effective_route_identity: str = "",
        review_lens: str = "",
    ) -> Dict[str, Any]:
        deadline_seconds = self._validate_deadline(deadline_seconds)
        identity = review_identity(
            candidate_hash, scope, lane, model, prompt, output_path, deadline_seconds,
            environment_fingerprint, evidence_fingerprint, effective_route_identity, review_lens,
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
                        prompt_hash, environment_fingerprint, evidence_fingerprint, effective_route_identity, review_lens, output_path, deadline_seconds,
                        deadline_at, state, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'admitted', ?, ?)""",
                    (
                        requested_id, fingerprint, logical_fingerprint, identity["candidate_hash"],
                        identity["normalized_scope"], identity["lane"], identity["model"], identity["prompt_hash"],
                        identity["environment_fingerprint"], identity["evidence_fingerprint"], identity["effective_route_identity"], identity["review_lens"],
                        identity["normalized_output_path"], deadline_seconds, now + deadline_seconds, now, now,
                    ),
                )
        finally:
            conn.close()
        return {"status": "admitted", "receipt_id": requested_id, "identity": identity}

    def admit_fleet_material_launch(
        self,
        *,
        attempt_id: str,
        fence_token: int,
        route_plan: Mapping[str, Any],
        preflight: Mapping[str, Any],
        candidate_hash: str,
        scope: str,
        lane: str,
        model: str,
        prompt: str,
        deadline_seconds: float,
        output_path: str,
        environment_fingerprint: str,
        evidence_fingerprint: str,
        effective_route_identity: str,
        review_lens: str,
        receipt_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Atomically admit, preflight, claim, and bind a fleet route plan.

        This is intentionally separate from generic review admission: material
        work must never have a durable outbox without its candidate-bound route
        plan, nor a route plan without an exact claimed review receipt.
        """
        deadline_seconds = self._validate_deadline(deadline_seconds)
        verified_preflight = self._validate_preflight(preflight)
        route_identity = self._required_text(effective_route_identity, "effective_route_identity")
        lens = self._required_text(review_lens, "review_lens").lower()
        if not isinstance(route_plan, Mapping):
            raise ValueError("material route plan is required")
        selected = route_plan.get("selected")
        if not isinstance(selected, Mapping):
            raise ValueError("material route plan requires selected route")
        if self._required_text(selected.get("effective_execution_identity"), "selected route identity") != route_identity:
            raise ValueError("selected route identity does not match material receipt")
        if self._required_text(selected.get("review_lens"), "selected route lens").lower() != lens:
            raise ValueError("selected route lens does not match material receipt")
        identity = review_identity(
            candidate_hash, scope, lane, model, prompt, output_path, deadline_seconds,
            environment_fingerprint, evidence_fingerprint, route_identity, lens,
        )
        if not all(identity[key] for key in (
            "candidate_hash", "normalized_scope", "lane", "model", "normalized_output_path",
            "environment_fingerprint", "evidence_fingerprint", "effective_route_identity", "review_lens",
        )):
            raise ValueError("material review identity is incomplete")
        fingerprint = _fingerprint(identity)
        logical_fingerprint = _fingerprint(_logical_identity(identity))
        normalized_plan = json.dumps(dict(route_plan), sort_keys=True, separators=(",", ":"))
        plan_hash = _fingerprint({"receipt": fingerprint, "route_plan": json.loads(normalized_plan)})
        requested_id = receipt_id or f"review_{uuid.uuid4().hex[:12]}"
        now = time.time()
        conn = self._connect()
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                attempt = conn.execute(
                    "SELECT candidate_hash, environment_fingerprint, scope_hash, fence_token, state, effective_route_identity, lens "
                    "FROM workflow_recovery_attempts WHERE attempt_id=?",
                    (self._required_text(attempt_id, "attempt_id"),),
                ).fetchone()
                expected_scope = _fingerprint({"scope": identity["normalized_scope"]})
                if (
                    attempt is None or attempt[0] != identity["candidate_hash"]
                    or attempt[1] != identity["environment_fingerprint"]
                    or attempt[2] != expected_scope or int(attempt[3]) != int(fence_token)
                    or attempt[4] != "PREPARED" or attempt[5] != route_identity or attempt[6] != lens
                ):
                    raise RuntimeError("material route plan is not bound to the current recovery fence")
                exact = conn.execute(
                    "SELECT receipt_id, state FROM release_review_receipts WHERE fingerprint=?", (fingerprint,)
                ).fetchone()
                if exact:
                    plan = conn.execute(
                        "SELECT plan_hash FROM workflow_material_route_plans WHERE receipt_id=?", (exact[0],)
                    ).fetchone()
                    if plan is None or plan[0] != plan_hash:
                        return {"status": "conflict", "receipt_id": exact[0], "reason": "material_route_plan_differs", "identity": identity}
                    return {"status": "existing", "receipt_id": exact[0], "state": exact[1], "identity": identity}
                if conn.execute(
                    "SELECT 1 FROM release_review_receipts WHERE logical_fingerprint=?", (logical_fingerprint,)
                ).fetchone():
                    return {"status": "conflict", "reason": "material_logical_identity_exists", "identity": identity}
                if conn.execute(
                    "SELECT 1 FROM release_review_receipts WHERE receipt_id=?", (requested_id,)
                ).fetchone():
                    return {"status": "conflict", "receipt_id": requested_id, "reason": "receipt_id_reused", "identity": identity}
                conn.execute(
                    """INSERT INTO release_review_receipts
                       (receipt_id, fingerprint, logical_fingerprint, candidate_hash, normalized_scope, lane, model,
                        prompt_hash, environment_fingerprint, evidence_fingerprint, effective_route_identity, review_lens,
                        output_path, deadline_seconds, deadline_at, state, preflight_json, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'launching', ?, ?, ?)""",
                    (
                        requested_id, fingerprint, logical_fingerprint, identity["candidate_hash"],
                        identity["normalized_scope"], identity["lane"], identity["model"], identity["prompt_hash"],
                        identity["environment_fingerprint"], identity["evidence_fingerprint"], route_identity, lens,
                        identity["normalized_output_path"], deadline_seconds, now + deadline_seconds,
                        json.dumps(verified_preflight, sort_keys=True), now, now,
                    ),
                )
                conn.execute(
                    """INSERT INTO workflow_material_route_plans
                       (receipt_id, attempt_id, fence_token, route_identity, review_lens, route_json, plan_json, plan_hash, saga_state, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'SEALED', ?, ?)""",
                    (
                        requested_id, attempt_id, int(fence_token), route_identity, lens,
                        json.dumps(dict(selected), sort_keys=True), normalized_plan, plan_hash, now, now,
                    ),
                )
        finally:
            conn.close()
        return {"status": "admitted", "receipt_id": requested_id, "identity": identity, "claim": {"status": "claimed", "receipt_id": requested_id, "state": "launching"}, "plan_hash": plan_hash}

    @staticmethod
    def assess_independent_review_gate(
        receipts: Iterable[Mapping[str, Any]], required_models: int,
    ) -> Dict[str, Any]:
        """Fail an N-model gate unless N different effective backing lanes exist.

        A distinct lens or delegation id is not evidence of independence.  The
        caller must pass the secret-free effective route identity captured at
        dispatch time.
        """
        identities: list[str] = []
        lenses: list[str] = []
        for receipt in receipts:
            if str(receipt.get("routing_mode", "")).upper() == "DEGRADED_SAME_MODEL":
                return {"status": "rejected", "reason": "DEGRADED_SAME_MODEL"}
            identity = _normalized(str(receipt.get("effective_route_identity", "")))
            if not identity:
                return {"status": "rejected", "reason": "missing_effective_route_identity"}
            identities.append(identity)
        if len(set(identities)) < required_models:
            return {"status": "rejected", "reason": "insufficient_distinct_effective_routes"}
        for receipt in receipts:
            lens = _normalized(str(receipt.get("review_lens", ""))).lower()
            if not lens:
                return {"status": "rejected", "reason": "missing_review_lens"}
            lenses.append(lens)
        if len(set(lenses)) < required_models:
            return {"status": "rejected", "reason": "insufficient_distinct_review_lenses"}
        return {"status": "accepted", "effective_route_identities": sorted(set(identities)), "review_lenses": sorted(set(lenses))}

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

    @staticmethod
    def _recovery_identity(
        *, candidate_hash: str, environment_fingerprint: str, normalized_scope: str,
        failure_fingerprint: str, normalized_task: str, effective_route_identity: str, lens: str,
    ) -> Dict[str, str]:
        return {
            "candidate_hash": _normalized(candidate_hash),
            "environment_fingerprint": _normalized(environment_fingerprint),
            "scope_hash": _fingerprint({"scope": _normalized(normalized_scope)}),
            "failure_fingerprint": _normalized(failure_fingerprint),
            "normalized_task_hash": _fingerprint({"task": _normalized(normalized_task)}),
            "effective_route_identity": _normalized(effective_route_identity),
            "lens": _normalized(lens),
        }

    def record_recovery_packet(self, packet: Mapping[str, Any]) -> Dict[str, str]:
        """Persist a redacted, content-addressed handoff packet before retry admission."""
        required = {
            "schema_version", "candidate_hash", "environment_fingerprint", "normalized_scope",
            "failure_fingerprint", "normalized_task", "failed_set", "reproducer", "versions",
            "attempted_remedy_hash", "verified_facts", "unresolved_questions", "quarantined", "redaction_attestation",
        }
        if not isinstance(packet, Mapping) or required.difference(packet):
            raise ValueError(f"recovery packet missing fields: {sorted(required.difference(packet or {}))}")
        if int(packet["schema_version"]) < 1 or not bool(packet["redaction_attestation"]):
            raise ValueError("recovery packet requires a supported schema and redaction attestation")
        forbidden = {"api_key", "token", "password", "chain_of_thought", "authenticated_response", "pid"}
        def _assert_redacted(value: Any) -> None:
            if isinstance(value, Mapping):
                for key, child in value.items():
                    normalized_key = _normalized(str(key)).lower().replace("-", "_")
                    if normalized_key in forbidden:
                        raise ValueError("recovery packet contains forbidden sensitive or transient fields")
                    _assert_redacted(child)
            elif isinstance(value, (list, tuple)):
                for child in value:
                    _assert_redacted(child)
        _assert_redacted(packet)
        identity = self._recovery_identity(
            candidate_hash=str(packet["candidate_hash"]), environment_fingerprint=str(packet["environment_fingerprint"]),
            normalized_scope=str(packet["normalized_scope"]), failure_fingerprint=str(packet["failure_fingerprint"]),
            normalized_task=str(packet["normalized_task"]), effective_route_identity="packet", lens="packet",
        )
        packet_json = json.dumps(dict(packet), sort_keys=True, separators=(",", ":"))
        packet_hash = hashlib.sha256(packet_json.encode("utf-8")).hexdigest()
        predecessor = _normalized(str(packet.get("predecessor_packet_hash", "")))
        conn = self._connect()
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT OR IGNORE INTO workflow_recovery_packets (packet_hash, identity_fingerprint, schema_version, packet_json, predecessor_packet_hash, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (packet_hash, _fingerprint(identity), int(packet["schema_version"]), packet_json, predecessor, time.time()),
                )
        finally:
            conn.close()
        return {"packet_hash": packet_hash, "identity_fingerprint": _fingerprint(identity)}

    def admit_recovery_attempt(
        self, *, packet_hash: str, candidate_hash: str, environment_fingerprint: str, normalized_scope: str,
        failure_fingerprint: str, normalized_task: str, mode: str, ordinal: int, owner: str,
        effective_route_identity: str, lens: str, predecessor_attempt_id: str = "", generation: int = 0,
    ) -> Dict[str, Any]:
        """Atomically reserve one fenced retry owner; duplicate admission spends nothing."""
        mode = self._required_text(mode, "recovery mode").upper()
        if mode not in {"STANDARD", "DEEP"} or ordinal not in {1, 2, 3} or generation < 0:
            raise ValueError("recovery mode, ordinal, or generation is invalid")
        identity = self._recovery_identity(
            candidate_hash=candidate_hash, environment_fingerprint=environment_fingerprint, normalized_scope=normalized_scope,
            failure_fingerprint=failure_fingerprint, normalized_task=normalized_task,
            effective_route_identity=effective_route_identity, lens=lens,
        )
        if not all(identity.values()):
            raise ValueError("recovery attempt identity is incomplete")
        fingerprint = _fingerprint(identity)
        conn = self._connect()
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                packet = conn.execute("SELECT identity_fingerprint FROM workflow_recovery_packets WHERE packet_hash=?", (packet_hash,)).fetchone()
                if packet is None:
                    raise RuntimeError("recovery packet is absent")
                if packet[0] != _fingerprint({**identity, "effective_route_identity": "packet", "lens": "packet"}):
                    raise RuntimeError("recovery packet does not match attempt candidate/environment/scope/failure identity")
                existing = conn.execute(
                    "SELECT attempt_id, state, fence_token FROM workflow_recovery_attempts WHERE identity_fingerprint=? AND mode=? AND ordinal=? AND generation=?",
                    (fingerprint, mode, ordinal, generation),
                ).fetchone()
                if existing:
                    return {"status": "existing", "attempt_id": existing[0], "state": existing[1], "fence_token": existing[2]}
                phase_rows = conn.execute(
                    "SELECT attempt_id, mode, ordinal, state, effective_route_identity FROM workflow_recovery_attempts "
                    "WHERE candidate_hash=? AND environment_fingerprint=? AND scope_hash=? AND failure_fingerprint=? "
                    "AND normalized_task_hash=? AND generation=?",
                    (
                        identity["candidate_hash"], identity["environment_fingerprint"], identity["scope_hash"],
                        identity["failure_fingerprint"], identity["normalized_task_hash"], generation,
                    ),
                ).fetchall()
                stopped = conn.execute(
                    "SELECT 1 FROM workflow_recovery_stops WHERE candidate_hash=? AND environment_fingerprint=? "
                    "AND scope_hash=? AND failure_fingerprint=? AND normalized_task_hash=? AND generation=?",
                    (identity["candidate_hash"], identity["environment_fingerprint"], identity["scope_hash"],
                     identity["failure_fingerprint"], identity["normalized_task_hash"], generation),
                ).fetchone()
                if stopped:
                    raise RuntimeError("recovery is terminal STOP_AND_REPORT for this generation")
                exact_ordinal = [row for row in phase_rows if row[1] == mode and int(row[2]) == ordinal]
                if mode == "STANDARD" and exact_ordinal:
                    raise RuntimeError("retry ordinal was already consumed by a different route or lens")
                if mode == "STANDARD":
                    previous = [row for row in phase_rows if row[1] == "STANDARD" and int(row[2]) == ordinal - 1]
                    if ordinal > 1 and (len(previous) != 1 or previous[0][3] != "FAILED" or predecessor_attempt_id != previous[0][0]):
                        raise RuntimeError("standard retry requires the immediately prior failed handoff")
                    prior_routes = {row[4] for row in phase_rows if row[1] == "STANDARD" and int(row[2]) < ordinal}
                    if identity["effective_route_identity"] in prior_routes:
                        raise RuntimeError("standard retries require a distinct effective route")
                else:
                    if ordinal == 1:
                        standard = [row for row in phase_rows if row[1] == "STANDARD"]
                        if {int(row[2]) for row in standard} != {1, 2, 3} or any(row[3] != "FAILED" for row in standard):
                            raise RuntimeError("deep mode requires three failed standard attempts")
                    else:
                        previous = [row for row in phase_rows if row[1] == "DEEP" and int(row[2]) == ordinal - 1]
                        if not previous or any(row[3] != "FAILED" for row in previous) or (
                            predecessor_attempt_id and predecessor_attempt_id not in {row[0] for row in previous}
                        ):
                            raise RuntimeError("deep retry requires all immediately prior failed deep lanes")
                if predecessor_attempt_id:
                    prior = conn.execute("SELECT state FROM workflow_recovery_attempts WHERE attempt_id=?", (predecessor_attempt_id,)).fetchone()
                    if prior is None or prior[0] not in {"FAILED", "INTERRUPTED", "CANCELLED", "COMMITTED"}:
                        raise RuntimeError("predecessor attempt is not terminal")
                active = conn.execute(
                    "SELECT attempt_id FROM workflow_recovery_attempts WHERE identity_fingerprint=? AND state IN ('PREPARED','ACCEPTED','RUNNING')",
                    (fingerprint,),
                ).fetchone()
                if active:
                    return {"status": "existing", "attempt_id": active[0], "state": "active"}
                fence = int(conn.execute("SELECT COALESCE(MAX(fence_token), 0) + 1 FROM workflow_recovery_attempts").fetchone()[0])
                attempt_id = f"retry_{uuid.uuid4().hex[:12]}"
                now = time.time()
                conn.execute(
                    "INSERT INTO workflow_recovery_attempts (attempt_id, identity_fingerprint, candidate_hash, environment_fingerprint, scope_hash, failure_fingerprint, normalized_task_hash, mode, ordinal, generation, owner, effective_route_identity, lens, packet_hash, predecessor_attempt_id, fence_token, state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PREPARED', ?, ?)",
                    (attempt_id, fingerprint, identity["candidate_hash"], identity["environment_fingerprint"], identity["scope_hash"], identity["failure_fingerprint"], identity["normalized_task_hash"], mode, ordinal, generation, self._required_text(owner, "recovery owner"), identity["effective_route_identity"], identity["lens"], packet_hash, predecessor_attempt_id or None, fence, now, now),
                )
                return {"status": "admitted", "attempt_id": attempt_id, "fence_token": fence, "identity": identity}
        finally:
            conn.close()

    def transition_recovery_attempt(self, attempt_id: str, *, fence_token: int, state: str, outcome: str = "") -> None:
        """Move a fenced attempt forward; late generations cannot overwrite it."""
        transitions = {
            "PREPARED": {"ACCEPTED", "CANCELLED", "INTERRUPTED"},
            "ACCEPTED": {"RUNNING", "CANCELLED", "INTERRUPTED"},
            "RUNNING": {"FAILED", "COMMITTED", "INTERRUPTED", "CANCELLED"},
        }
        state = self._required_text(state, "recovery state").upper()
        conn = self._connect()
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT state, fence_token FROM workflow_recovery_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
                if row is None or int(row[1]) != int(fence_token) or state not in transitions.get(row[0], set()):
                    raise RuntimeError("recovery transition rejected by state or fence")
                conn.execute("UPDATE workflow_recovery_attempts SET state=?, outcome=?, updated_at=? WHERE attempt_id=? AND fence_token=?", (state, _normalized(outcome), time.time(), attempt_id, int(fence_token)))
        finally:
            conn.close()

    def assert_current_recovery_attempt(
        self, attempt_id: str, *, fence_token: int, candidate_hash: str,
        environment_fingerprint: str, normalized_scope: str,
        effective_route_identity: str, review_lens: str,
    ) -> None:
        """Prove that a material-review outbox row belongs to its live retry lease."""
        conn = self._connect()
        try:
            row = conn.execute(
            "SELECT candidate_hash, environment_fingerprint, scope_hash, fence_token, state, effective_route_identity, lens "
                "FROM workflow_recovery_attempts WHERE attempt_id=?",
                (self._required_text(attempt_id, "attempt_id"),),
            ).fetchone()
            if row is None:
                raise RuntimeError("unknown recovery attempt")
            expected_scope = _fingerprint({"scope": _normalized(normalized_scope)})
            if (
                row[0] != self._required_text(candidate_hash, "candidate_hash")
                or row[1] != self._required_text(environment_fingerprint, "environment_fingerprint")
                or row[2] != expected_scope
                or int(row[3]) != int(fence_token)
                or row[4] not in {"PREPARED", "ACCEPTED", "RUNNING"}
                or row[5] != self._required_text(effective_route_identity, "effective_route_identity")
                or row[6] != self._required_text(review_lens, "review_lens")
            ):
                raise RuntimeError("recovery attempt is not the current candidate-bound fence")
        finally:
            conn.close()

    def admit_deep_capacity(
        self, *, candidate_hash: str, environment_fingerprint: str, failure_fingerprint: str,
        ordinal: int, lanes: Sequence[Mapping[str, Any]], configured_concurrency: int,
        token_budget: int,
    ) -> Dict[str, Any]:
        """Reserve bounded, distinct Deep lanes before any worker is launched."""
        if ordinal not in {1, 2, 3} or configured_concurrency < 1 or token_budget < 0:
            raise ValueError("deep ordinal, concurrency, or token budget is invalid")
        min_lanes, max_lanes = {1: (2, 3), 2: (3, 4), 3: (1, len(lanes))}[ordinal]
        qualified_routes: set[str] = set()
        qualified_lenses: set[str] = set()
        for raw in lanes:
            if bool(raw.get("available", False)):
                qualified_routes.add(self._required_text(raw.get("effective_route_identity"), "effective_route_identity"))
                qualified_lenses.add(self._required_text(raw.get("lens"), "lens"))
        if ordinal == 3:
            # The third Deep phase is intentionally every available qualified
            # distinct lane, rather than an arbitrary smaller quorum.
            min_lanes = max_lanes = min(len(qualified_routes), len(qualified_lenses))
        selected: list[Dict[str, Any]] = []
        unavailable: list[Dict[str, str]] = []
        seen_routes: set[str] = set()
        seen_lenses: set[str] = set()
        remaining_budget = int(token_budget)
        for raw in lanes:
            route = self._required_text(raw.get("effective_route_identity"), "effective_route_identity")
            lens = self._required_text(raw.get("lens"), "lens")
            lane = self._required_text(raw.get("lane"), "lane")
            if not bool(raw.get("available", False)):
                unavailable.append({"lane": lane, "reason": "unavailable"})
                continue
            if route in seen_routes or lens in seen_lenses:
                unavailable.append({"lane": lane, "reason": "duplicate_route_lens"})
                continue
            cost = int(raw.get("token_cost", 1))
            if cost < 1 or cost > remaining_budget:
                unavailable.append({"lane": lane, "reason": "budget"})
                continue
            if len(selected) >= min(max_lanes, configured_concurrency):
                unavailable.append({"lane": lane, "reason": "concurrency"})
                continue
            selected.append({"lane": lane, "effective_route_identity": route, "lens": lens, "token_cost": cost})
            seen_routes.add(route)
            seen_lenses.add(lens)
            remaining_budget -= cost
        status = "ADMITTED" if len(selected) >= min_lanes else (
            "BUDGET_EXCEEDED" if not selected and token_budget == 0 else "DEGRADED_ROUTE_CAPACITY"
        )
        fingerprint = _fingerprint({
            "candidate_hash": self._required_text(candidate_hash, "candidate_hash"),
            "environment_fingerprint": self._required_text(environment_fingerprint, "environment_fingerprint"),
            "failure_fingerprint": self._required_text(failure_fingerprint, "failure_fingerprint"),
            "mode": "DEEP", "ordinal": ordinal, "lanes": list(lanes),
            "configured_concurrency": configured_concurrency, "token_budget": token_budget,
        })
        conn = self._connect()
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT OR IGNORE INTO workflow_recovery_phase_admissions "
                    "(phase_fingerprint,candidate_hash,environment_fingerprint,failure_fingerprint,mode,ordinal,status,selected_lanes_json,unavailable_lanes_json,capacity_limit,token_budget,created_at) "
                    "VALUES (?, ?, ?, ?, 'DEEP', ?, ?, ?, ?, ?, ?, ?)",
                    (fingerprint, candidate_hash, environment_fingerprint, failure_fingerprint, ordinal, status,
                     json.dumps(selected, sort_keys=True), json.dumps(unavailable, sort_keys=True),
                     configured_concurrency, token_budget, time.time()),
                )
        finally:
            conn.close()
        return {"status": status, "selected": selected, "unavailable": unavailable, "fingerprint": fingerprint}

    def stop_and_report_after_deep_three(
        self, *, candidate_hash: str, environment_fingerprint: str, normalized_scope: str,
        failure_fingerprint: str, normalized_task: str, generation: int, reason: str,
    ) -> Dict[str, str]:
        """Terminalize an exhausted Deep cycle; no automatic fourth cycle exists."""
        identity = self._recovery_identity(
            candidate_hash=candidate_hash, environment_fingerprint=environment_fingerprint,
            normalized_scope=normalized_scope, failure_fingerprint=failure_fingerprint,
            normalized_task=normalized_task, effective_route_identity="stop", lens="stop",
        )
        stop_fingerprint = _fingerprint({**identity, "generation": generation})
        conn = self._connect()
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                rows = conn.execute(
                    "SELECT state FROM workflow_recovery_attempts WHERE candidate_hash=? AND environment_fingerprint=? "
                    "AND scope_hash=? AND failure_fingerprint=? AND normalized_task_hash=? "
                    "AND mode='DEEP' AND ordinal=3 AND generation=?",
                    (identity["candidate_hash"], identity["environment_fingerprint"], identity["scope_hash"],
                     identity["failure_fingerprint"], identity["normalized_task_hash"], generation),
                ).fetchall()
                if not rows or any(row[0] != "FAILED" for row in rows):
                    raise RuntimeError("STOP_AND_REPORT requires every Deep 3 lane to fail")
                conn.execute(
                    "INSERT OR IGNORE INTO workflow_recovery_stops "
                    "(stop_fingerprint,candidate_hash,environment_fingerprint,scope_hash,failure_fingerprint,normalized_task_hash,generation,reason,created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (stop_fingerprint, identity["candidate_hash"], identity["environment_fingerprint"], identity["scope_hash"],
                     identity["failure_fingerprint"], identity["normalized_task_hash"], generation,
                     self._required_text(reason, "stop reason"), time.time()),
                )
        finally:
            conn.close()
        return {"status": "STOP_AND_REPORT", "stop_fingerprint": stop_fingerprint}

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
                if row[1] in {"completed", "failed", "unknown", "cancelled"}:
                    return False
                state = (
                    "completed" if status == "completed"
                    else "failed" if status in {"error", "failed", "stalled"}
                    else "cancelled" if _normalized(status) == "cancelled"
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

    def finalize_material_saga(
        self,
        receipt_id: str,
        *,
        attempt_id: str,
        fence_token: int,
        status: str,
        evidence: Mapping[str, Any],
    ) -> bool:
        """Terminalize one material receipt, route plan, and retry fence together.

        The material route plan and recovery attempt live in this ledger, so
        their terminal state must never be published as separate writes.  The
        async state database may still need reconciliation after a host crash,
        but this transaction makes the material authority itself all-or-nothing.
        """
        status_normalized = _normalized(status)
        receipt_state = (
            "completed" if status_normalized == "completed"
            else "failed" if status_normalized in {"error", "failed", "stalled"}
            else "cancelled" if status_normalized == "cancelled"
            else "timebox_expired" if status_normalized == "timebox_expired"
            else "unknown"
        )
        attempt_state = {
            "completed": "COMMITTED",
            "cancelled": "CANCELLED",
            "unknown": "INTERRUPTED",
            "timebox_expired": "INTERRUPTED",
        }.get(receipt_state, "FAILED")
        terminal = {"status": status_normalized, "evidence": dict(evidence)}
        now = time.time()
        conn = self._connect()
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                plan = conn.execute(
                    "SELECT attempt_id, fence_token, saga_state, terminal_json FROM workflow_material_route_plans WHERE receipt_id=?",
                    (self._required_text(receipt_id, "receipt_id"),),
                ).fetchone()
                if plan is None:
                    raise RuntimeError("material route plan is absent for receipt")
                if plan[0] != self._required_text(attempt_id, "attempt_id") or int(plan[1]) != int(fence_token):
                    raise RuntimeError("material terminal publication rejected by route-plan fence")
                if plan[2] == "TERMINAL":
                    return False
                receipt = self._require_receipt(conn, receipt_id)
                if receipt[1] not in {"running", "timebox_expired"}:
                    raise RuntimeError(f"material receipt {receipt_id} cannot terminalize from state {receipt[1]}")
                attempt = conn.execute(
                    "SELECT state, fence_token FROM workflow_recovery_attempts WHERE attempt_id=?",
                    (attempt_id,),
                ).fetchone()
                if attempt is None or int(attempt[1]) != int(fence_token) or attempt[0] != "RUNNING":
                    raise RuntimeError("material terminal publication rejected by recovery fence")
                if receipt[1] == "timebox_expired" and receipt_state != "timebox_expired":
                    return False
                conn.execute(
                    "UPDATE release_review_receipts SET state=?, terminal_json=?, updated_at=? WHERE receipt_id=?",
                    (receipt_state, json.dumps(terminal, sort_keys=True), now, receipt_id),
                )
                conn.execute(
                    "UPDATE workflow_material_route_plans SET saga_state='TERMINAL', terminal_json=?, updated_at=? WHERE receipt_id=? AND saga_state!='TERMINAL'",
                    (json.dumps(terminal, sort_keys=True), now, receipt_id),
                )
                updated = conn.execute(
                    "UPDATE workflow_recovery_attempts SET state=?, outcome=?, updated_at=? "
                    "WHERE attempt_id=? AND fence_token=? AND state='RUNNING'",
                    (attempt_state, status_normalized, now, attempt_id, int(fence_token)),
                )
                if updated.rowcount != 1:
                    raise RuntimeError("material recovery terminal publication lost its current fence")
                return True
        finally:
            conn.close()

    def bind_material_owned_handle(
        self,
        receipt_id: str,
        *,
        attempt_id: str,
        fence_token: int,
        handle_id: str,
        pid: int,
        host_start_time: int | None,
    ) -> bool:
        """Bind the exact external child before material execution may run."""
        handle = self._required_text(handle_id, "external handle_id")
        if not isinstance(pid, int) or pid <= 0:
            raise ValueError("external PID must be positive")
        if host_start_time is not None and (not isinstance(host_start_time, int) or host_start_time < 0):
            raise ValueError("external process start time is invalid")
        now = time.time()
        conn = self._connect()
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                plan = conn.execute(
                    "SELECT attempt_id, fence_token, saga_state, external_handle_id FROM workflow_material_route_plans WHERE receipt_id=?",
                    (self._required_text(receipt_id, "receipt_id"),),
                ).fetchone()
                if plan is None or plan[0] != self._required_text(attempt_id, "attempt_id") or int(plan[1]) != int(fence_token):
                    raise RuntimeError("material external handle rejected by route-plan fence")
                if plan[2] == "OWNED":
                    return plan[3] == handle
                if plan[2] != "SEALED" or plan[3]:
                    raise RuntimeError("material external handle cannot replace an existing saga state")
                receipt = self._require_receipt(conn, receipt_id)
                attempt = conn.execute(
                    "SELECT state, fence_token FROM workflow_recovery_attempts WHERE attempt_id=?", (attempt_id,)
                ).fetchone()
                if receipt[1] != "running" or attempt is None or int(attempt[1]) != int(fence_token) or attempt[0] != "ACCEPTED":
                    raise RuntimeError("material external handle is not bound to the current accepted execution")
                updated = conn.execute(
                    "UPDATE workflow_material_route_plans SET saga_state='OWNED', external_handle_id=?, external_pid=?, external_host_start_time=?, updated_at=? "
                    "WHERE receipt_id=? AND saga_state='SEALED' AND external_handle_id=''",
                    (handle, pid, host_start_time, now, receipt_id),
                )
                if updated.rowcount != 1:
                    raise RuntimeError("material external handle binding lost its current fence")
                updated = conn.execute(
                    "UPDATE workflow_recovery_attempts SET state='RUNNING', outcome='owned external material process bound', updated_at=? "
                    "WHERE attempt_id=? AND fence_token=? AND state='ACCEPTED'",
                    (now, attempt_id, int(fence_token)),
                )
                if updated.rowcount != 1:
                    raise RuntimeError("material external handle binding lost recovery ownership")
                return True
        finally:
            conn.close()

    def interrupt_unowned_material_saga(
        self,
        receipt_id: str,
        *,
        attempt_id: str,
        fence_token: int,
        status: str = "unknown",
        evidence: Mapping[str, Any],
    ) -> bool:
        """Terminalize a sealed material saga that never bound a child handle.

        A material recovery attempt becomes ``RUNNING`` only after the exact
        external process handle is persisted.  If the async owner disappears
        before that boundary, a late or synthetic completion must not be
        accepted as a real review result.  This transaction records a fenced
        interrupted/cancelled terminal outcome without claiming that a child
        ever ran.
        """
        status_normalized = _normalized(status)
        if status_normalized not in {"unknown", "cancelled"}:
            raise ValueError("unowned material saga may only be unknown or cancelled")
        receipt_state = "cancelled" if status_normalized == "cancelled" else "unknown"
        attempt_state = "CANCELLED" if receipt_state == "cancelled" else "INTERRUPTED"
        terminal = {
            "status": status_normalized,
            "evidence": {**dict(evidence), "external_handle_bound": False},
        }
        now = time.time()
        conn = self._connect()
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                plan = conn.execute(
                    "SELECT attempt_id, fence_token, saga_state, external_handle_id FROM workflow_material_route_plans WHERE receipt_id=?",
                    (self._required_text(receipt_id, "receipt_id"),),
                ).fetchone()
                if plan is None:
                    raise RuntimeError("material route plan is absent for receipt")
                if plan[0] != self._required_text(attempt_id, "attempt_id") or int(plan[1]) != int(fence_token):
                    raise RuntimeError("unowned material terminal rejected by route-plan fence")
                if plan[2] == "TERMINAL":
                    return False
                if plan[2] != "SEALED" or plan[3]:
                    raise RuntimeError("unowned material terminal requires a sealed handle-free route plan")
                receipt = self._require_receipt(conn, receipt_id)
                if receipt[1] not in {"launching", "running", "timebox_expired"}:
                    raise RuntimeError(f"unowned material receipt {receipt_id} cannot terminalize from state {receipt[1]}")
                attempt = conn.execute(
                    "SELECT state, fence_token FROM workflow_recovery_attempts WHERE attempt_id=?",
                    (attempt_id,),
                ).fetchone()
                if attempt is None or int(attempt[1]) != int(fence_token) or attempt[0] not in {"PREPARED", "ACCEPTED"}:
                    raise RuntimeError("unowned material terminal rejected by recovery fence")
                conn.execute(
                    "UPDATE release_review_receipts SET state=?, terminal_json=?, updated_at=? WHERE receipt_id=?",
                    (receipt_state, json.dumps(terminal, sort_keys=True), now, receipt_id),
                )
                conn.execute(
                    "UPDATE workflow_material_route_plans SET saga_state='TERMINAL', terminal_json=?, updated_at=? "
                    "WHERE receipt_id=? AND saga_state='SEALED' AND external_handle_id=''",
                    (json.dumps(terminal, sort_keys=True), now, receipt_id),
                )
                updated = conn.execute(
                    "UPDATE workflow_recovery_attempts SET state=?, outcome=?, updated_at=? "
                    "WHERE attempt_id=? AND fence_token=? AND state IN ('PREPARED', 'ACCEPTED')",
                    (attempt_state, f"unowned_material:{status_normalized}", now, attempt_id, int(fence_token)),
                )
                if updated.rowcount != 1:
                    raise RuntimeError("unowned material terminal lost its current recovery fence")
                return True
        finally:
            conn.close()

    def has_material_route_plan(self, receipt_id: str) -> bool:
        """Return whether this receipt belongs to the sealed material saga."""
        conn = self._connect()
        try:
            return conn.execute(
                "SELECT 1 FROM workflow_material_route_plans WHERE receipt_id=?",
                (self._required_text(receipt_id, "receipt_id"),),
            ).fetchone() is not None
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
