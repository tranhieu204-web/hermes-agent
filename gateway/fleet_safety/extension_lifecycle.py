"""Durable default-continue extension lifecycle for fleet checkpoints.

The registry persists only opaque scope hashes and event records. Crossing a
resource checkpoint creates an ACTIVE extension immediately; approval is an
acknowledgement, timeout/expiry continues until renewal, and denial is the only
terminal stop decision represented here.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import asdict, dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Dict, Optional, Tuple


class ExtensionState(str, Enum):
    ACTIVE = "active"
    APPROVED = "approved"
    DENIED = "denied"
    TIMED_OUT_CONTINUING = "timed_out_continuing"
    EXPIRED_CONTINUING = "expired_continuing"


@dataclass(frozen=True)
class ExtensionRecord:
    event_id: str
    scope_hash: str
    session_hash: str
    state: ExtensionState
    issued_at: float
    expires_at: float
    grant_size: int
    revision: int
    notice_delivered: bool = False
    decision_at: Optional[float] = None

    @property
    def should_continue(self) -> bool:
        return self.state is not ExtensionState.DENIED

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["state"] = self.state.value
        return payload

    @classmethod
    def from_dict(cls, payload: dict) -> "ExtensionRecord":
        return cls(
            event_id=str(payload["event_id"]),
            scope_hash=str(payload["scope_hash"]),
            session_hash=str(payload.get("session_hash", "")),
            state=ExtensionState(str(payload["state"])),
            issued_at=float(payload["issued_at"]),
            expires_at=float(payload["expires_at"]),
            grant_size=int(payload["grant_size"]),
            revision=int(payload["revision"]),
            notice_delivered=bool(payload.get("notice_delivered", False)),
            decision_at=(
                None
                if payload.get("decision_at") is None
                else float(payload["decision_at"])
            ),
        )


class ExtensionRegistry:
    """Thread-safe extension records with atomic restart persistence."""

    _VERSION = 1

    def __init__(self, path: Optional[Path | str] = None) -> None:
        self.path = Path(path) if path is not None else None
        self._lock = threading.RLock()
        self._records: Dict[str, ExtensionRecord] = {}
        self._active_by_scope: Dict[str, str] = {}
        self._load()

    @staticmethod
    def _session_hash(session_id: str) -> str:
        return hashlib.sha256(
            str(session_id).encode("utf-8", errors="replace")
        ).hexdigest()

    @classmethod
    def _scope_hash(cls, session_id: str, checkpoint_key: str) -> str:
        raw = f"{cls._session_hash(session_id)}\0{checkpoint_key}".encode(
            "utf-8", errors="replace"
        )
        return hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _event_id(scope_hash: str, revision: int, now: float) -> str:
        raw = f"{scope_hash}:{revision}:{now:.9f}".encode("ascii")
        return hashlib.sha256(raw).hexdigest()[:32]

    def request(
        self,
        *,
        session_id: str,
        checkpoint_key: str,
        now: float,
        duration_seconds: float,
        grant_size: int,
    ) -> Tuple[ExtensionRecord, bool]:
        duration = max(1.0, float(duration_seconds))
        grant = max(1, int(grant_size))
        session_hash = self._session_hash(str(session_id))
        scope_hash = self._scope_hash(str(session_id), str(checkpoint_key))
        with self._lock:
            current_id = self._active_by_scope.get(scope_hash)
            current = self._records.get(current_id or "")
            if current is not None:
                if current.state is ExtensionState.DENIED:
                    return current, False
                if now < current.expires_at and current.state not in {
                    ExtensionState.TIMED_OUT_CONTINUING,
                    ExtensionState.EXPIRED_CONTINUING,
                }:
                    return current, not current.notice_delivered
                revision = current.revision + 1
                if current.state not in {
                    ExtensionState.TIMED_OUT_CONTINUING,
                    ExtensionState.EXPIRED_CONTINUING,
                }:
                    current = replace(
                        current,
                        state=ExtensionState.EXPIRED_CONTINUING,
                        decision_at=float(now),
                    )
                    self._records[current.event_id] = current
            else:
                revision = 1

            record = ExtensionRecord(
                event_id=self._event_id(scope_hash, revision, float(now)),
                scope_hash=scope_hash,
                session_hash=session_hash,
                state=ExtensionState.ACTIVE,
                issued_at=float(now),
                expires_at=float(now) + duration,
                grant_size=grant,
                revision=revision,
            )
            self._records[record.event_id] = record
            self._active_by_scope[scope_hash] = record.event_id
            self._persist()
            return record, True

    def get(self, event_id: str) -> ExtensionRecord:
        with self._lock:
            return self._records[str(event_id)]

    def mark_notice_delivered(self, event_id: str) -> ExtensionRecord:
        with self._lock:
            record = self.get(event_id)
            updated = replace(record, notice_delivered=True)
            self._records[updated.event_id] = updated
            self._persist()
            return updated

    def approve(self, event_id: str, *, now: float) -> ExtensionRecord:
        return self._transition(
            event_id,
            ExtensionState.APPROVED,
            now=now,
            denied_is_terminal=True,
        )

    def deny(self, event_id: str, *, now: float) -> ExtensionRecord:
        return self._transition(
            event_id,
            ExtensionState.DENIED,
            now=now,
            denied_is_terminal=False,
        )

    def deny_active_for_session(
        self,
        session_id: str,
        *,
        now: float,
    ) -> list[ExtensionRecord]:
        """Atomically deny every current extension for one opaque session."""
        session_hash = self._session_hash(str(session_id))
        with self._lock:
            active_ids = set(self._active_by_scope.values())
            denied: list[ExtensionRecord] = []
            for event_id in sorted(active_ids):
                record = self._records.get(event_id)
                if (
                    record is None
                    or record.session_hash != session_hash
                    or record.state is ExtensionState.DENIED
                ):
                    continue
                updated = replace(
                    record,
                    state=ExtensionState.DENIED,
                    decision_at=float(now),
                )
                self._records[event_id] = updated
                denied.append(updated)
            if denied:
                self._persist()
            return denied

    def timeout(self, event_id: str, *, now: float) -> ExtensionRecord:
        return self._transition(
            event_id,
            ExtensionState.TIMED_OUT_CONTINUING,
            now=now,
            denied_is_terminal=True,
        )

    def expire(self, event_id: str, *, now: float) -> ExtensionRecord:
        return self._transition(
            event_id,
            ExtensionState.EXPIRED_CONTINUING,
            now=now,
            denied_is_terminal=True,
        )

    def _transition(
        self,
        event_id: str,
        state: ExtensionState,
        *,
        now: float,
        denied_is_terminal: bool,
    ) -> ExtensionRecord:
        with self._lock:
            record = self.get(event_id)
            if denied_is_terminal and record.state is ExtensionState.DENIED:
                return record
            updated = replace(record, state=state, decision_at=float(now))
            self._records[updated.event_id] = updated
            self._persist()
            return updated

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if int(payload.get("version", 0)) != self._VERSION:
                return
            records = {
                record.event_id: record
                for record in (
                    ExtensionRecord.from_dict(item)
                    for item in payload.get("records", [])
                )
            }
            active = {
                str(scope): str(event_id)
                for scope, event_id in (payload.get("active_by_scope") or {}).items()
                if str(event_id) in records
            }
            self._records = records
            self._active_by_scope = active
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            # Corrupt/unknown state fails open for continuation but never invents
            # a denial. The next mutation atomically replaces it.
            self._records = {}
            self._active_by_scope = {}

    def _persist(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": self._VERSION,
            "records": [
                self._records[key].to_dict() for key in sorted(self._records)
            ],
            "active_by_scope": dict(sorted(self._active_by_scope.items())),
        }
        temp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, self.path)


__all__ = ["ExtensionRecord", "ExtensionRegistry", "ExtensionState"]
