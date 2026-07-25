"""Read-only capacity evidence adapters and normalization."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .types import (
    CapacityRead,
    CapacitySnapshot,
    Confidence,
    Freshness,
    HealthRead,
    LaneHealth,
    MeasurementKind,
    ReasonCode,
)
from .usage_paths import default_native_usage_path


_QUANTUM = Decimal("0.001")
_TOTAL = Decimal("100.000")
_PLAN_LABELS = {
    "chatgpt": "chatgpt_codex",
    "chatgpt codex": "chatgpt_codex",
    "chatgpt pro codex": "chatgpt_codex",
    "claude": "claude_code",
    "claude code": "claude_code",
    "claude max 20x": "claude_code",
    "grok": "grok",
    "supergrok": "grok",
    "antigravity": "antigravity",
    "google ai antigravity": "antigravity",
}


class _DuplicateKey(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = value
    return result


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _percentage(value: object) -> Decimal:
    # JSON numbers become int/float; string-only percentages prevent binary
    # float ambiguity and make non-finite values explicit failures.
    if not isinstance(value, str):
        raise ValueError("percentage must be a decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("invalid decimal") from exc
    if not parsed.is_finite() or parsed < 0 or parsed > 100:
        raise ValueError("percentage outside [0, 100]")
    return parsed.quantize(_QUANTUM)


def _plan_percentage(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("weekly_pct_used must be numeric")
    return _percentage(str(value))


def _plan_lane(label: object) -> str | None:
    if not isinstance(label, str):
        raise ValueError("plan label must be a string")
    normalized = " ".join(
        label.strip().lower().replace("-", " ").replace("·", " ").split()
    )
    return _PLAN_LABELS.get(normalized)


def _optional_identifier(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _health_read(
    row: dict[str, Any],
    *,
    read_at: datetime,
    max_age: timedelta,
    future_tolerance: timedelta,
    source_id: str,
) -> HealthRead | None:
    status_raw = row.get("health_status")
    checked_raw = row.get("health_checked_at")
    if status_raw is None and checked_raw is None:
        return None
    if status_raw is None or checked_raw is None:
        raise ValueError(
            "health_status and health_checked_at must be supplied together"
        )
    status = LaneHealth(str(status_raw).strip().upper())
    captured_at = _parse_timestamp(checked_raw)
    if captured_at > read_at + future_tolerance:
        raise ValueError("health_checked_at is in the future")
    expires_at = captured_at + max_age
    return HealthRead(
        status=status,
        captured_at=captured_at,
        read_at=read_at,
        expires_at=expires_at,
        freshness=(
            Freshness.FRESH if read_at <= expires_at else Freshness.STALE
        ),
        source_id=source_id,
    )


class BridgeUsageAdapter:
    """Normalize the native Hermes-home usage JSON without mutating it.

    The historical class name remains for compatibility; the default path is
    the profile-scoped native file under the active Hermes home, not a
    hardcoded bridge directory.
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        max_age: timedelta = timedelta(hours=2),
        max_bytes: int = 256 * 1024,
        future_tolerance: timedelta = timedelta(seconds=30),
    ) -> None:
        self.path = Path(path) if path is not None else default_native_usage_path()
        self.max_age = max_age
        self.max_bytes = max_bytes
        self.future_tolerance = future_tolerance

    def read(
        self,
        lane_id: str,
        *,
        now: datetime | None = None,
        reserved_pct: Decimal = Decimal("0"),
    ) -> CapacityRead:
        read_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        try:
            with self.path.open("rb") as handle:
                before = handle.seek(0, 2)
                if before > self.max_bytes:
                    raise ValueError("document exceeds size limit")
                handle.seek(0)
                raw = handle.read(self.max_bytes + 1)
                if len(raw) > self.max_bytes:
                    raise ValueError("document exceeds size limit")
        except FileNotFoundError:
            return CapacityRead(
                None, ReasonCode.CAPACITY_MISSING, f"{lane_id}: bridge file absent"
            )
        except (OSError, ValueError) as exc:
            return CapacityRead(None, ReasonCode.CAPACITY_INVALID, str(exc))

        health: HealthRead | None = None
        try:
            document = json.loads(raw, object_pairs_hook=_unique_object)
            if not isinstance(document, dict):
                raise ValueError("bridge document must be an object")
            if "plans" in document:
                return self._read_plans(
                    document,
                    raw=raw,
                    lane_id=lane_id,
                    read_at=read_at,
                    reserved_pct=reserved_pct,
                )
            if document.get("schema_version") != "1":
                raise ValueError("unsupported bridge schema")
            lanes = document.get("lanes")
            if not isinstance(lanes, dict):
                raise ValueError("lanes must be an object")
            lane = lanes.get(lane_id)
            if lane is None:
                return CapacityRead(
                    None,
                    ReasonCode.CAPACITY_MISSING,
                    f"{lane_id}: no bridge evidence",
                )
            if not isinstance(lane, dict):
                raise ValueError("lane evidence must be an object")
            digest = hashlib.sha256(raw).hexdigest()
            source_id = f"bridge_file:{self.path}#{digest}"
            health = _health_read(
                lane,
                read_at=read_at,
                max_age=self.max_age,
                future_tolerance=self.future_tolerance,
                source_id=source_id,
            )
            captured_at = _parse_timestamp(document.get("captured_at"))
            if captured_at > read_at + self.future_tolerance:
                raise ValueError("capture timestamp is in the future")
            used = _percentage(lane.get("used_pct"))
            remaining = _percentage(lane.get("remaining_pct"))
            if abs((used + remaining) - _TOTAL) > _QUANTUM:
                raise ValueError("used and remaining percentages disagree")
            confidence = Confidence(lane.get("confidence"))
            comparability_group = _optional_identifier(
                lane.get("comparability_group"), "comparability_group"
            )
            quota_window_id = _optional_identifier(
                lane.get("quota_window_id"), "quota_window_id"
            )
            measurement_kind = MeasurementKind(
                lane.get("measurement_kind", MeasurementKind.UNKNOWN.value)
            )
            overage_disabled = lane.get("overage_disabled")
            if not isinstance(overage_disabled, bool):
                raise ValueError("overage_disabled must be explicit")
            reserved = _percentage(str(reserved_pct))
            effective = max(Decimal("0"), remaining - reserved).quantize(_QUANTUM)
            expires_at = captured_at + self.max_age
            freshness = (
                Freshness.FRESH if read_at <= expires_at else Freshness.STALE
            )
            snapshot = CapacitySnapshot(
                lane_id=lane_id,
                used_pct=used,
                remaining_pct=remaining,
                reserved_pct=reserved,
                effective_remaining_pct=effective,
                source_kind="bridge_file",
                source_id=source_id,
                captured_at=captured_at,
                read_at=read_at,
                expires_at=expires_at,
                freshness=freshness,
                confidence=confidence,
                schema_version="1",
                overage_disabled=overage_disabled,
                comparability_group=comparability_group,
                quota_window_id=quota_window_id,
                measurement_kind=measurement_kind,
            )
            reason = (
                None if freshness is Freshness.FRESH else ReasonCode.CAPACITY_STALE
            )
            return CapacityRead(
                snapshot,
                reason,
                health=health,
            )
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            return CapacityRead(
                None, ReasonCode.CAPACITY_INVALID, str(exc), health=health
            )

    def _read_plans(
        self,
        document: dict[str, Any],
        *,
        raw: bytes,
        lane_id: str,
        read_at: datetime,
        reserved_pct: Decimal,
    ) -> CapacityRead:
        health: HealthRead | None = None
        try:
            plans = document.get("plans")
            if not isinstance(plans, list):
                raise ValueError("plans must be an array")
            matched: dict[str, Any] | None = None
            for row in plans:
                if not isinstance(row, dict):
                    raise ValueError("plan row must be an object")
                mapped = _plan_lane(row.get("label"))
                if mapped == lane_id:
                    if matched is not None:
                        raise ValueError(f"duplicate plan label for {lane_id}")
                    matched = row
            if matched is None:
                return CapacityRead(
                    None, ReasonCode.CAPACITY_MISSING, f"{lane_id}: no bridge plan"
                )
            digest = hashlib.sha256(raw).hexdigest()
            source_id = f"bridge_plans:{self.path}#{digest}"
            health = _health_read(
                matched,
                read_at=read_at,
                max_age=self.max_age,
                future_tolerance=self.future_tolerance,
                source_id=source_id,
            )
            root_checked_at = _parse_timestamp(document.get("checked_at"))
            if root_checked_at > read_at + self.future_tolerance:
                raise ValueError("checked_at is in the future")
            used = _plan_percentage(matched.get("weekly_pct_used"))
            remaining = (_TOTAL - used).quantize(_QUANTUM)
            row_time = matched.get("checked_at")
            # Per-lane freshness is authoritative for every lane. A missing row
            # checked_at must never inherit a sibling-advanced root stamp and
            # masquerade as FRESH capacity (including auto lanes like Claude).
            missing_row_time = row_time is None
            captured_at = (
                _parse_timestamp(row_time) if row_time is not None else root_checked_at
            )
            if captured_at > read_at + self.future_tolerance:
                raise ValueError("plan checked_at is in the future")
            reserved = _percentage(str(reserved_pct))
            expires_at = captured_at + self.max_age
            stale = missing_row_time or read_at > expires_at
            freshness = Freshness.STALE if stale else Freshness.FRESH
            confidence = Confidence.LOW if missing_row_time else Confidence.HIGH
            overage_raw = matched.get("overage_disabled")
            if overage_raw is not None and not isinstance(overage_raw, bool):
                raise ValueError("overage_disabled must be a boolean when set")
            snapshot = CapacitySnapshot(
                lane_id=lane_id,
                used_pct=used,
                remaining_pct=remaining,
                reserved_pct=reserved,
                effective_remaining_pct=max(
                    Decimal("0"), remaining - reserved
                ).quantize(_QUANTUM),
                source_kind="bridge_plans",
                source_id=source_id,
                captured_at=captured_at,
                read_at=read_at,
                expires_at=expires_at,
                freshness=freshness,
                confidence=confidence,
                schema_version="plans-1",
                overage_disabled=overage_raw,
                comparability_group=_optional_identifier(
                    matched.get("comparability_group"), "comparability_group"
                ),
                quota_window_id=_optional_identifier(
                    matched.get("quota_window_id"), "quota_window_id"
                ),
                measurement_kind=MeasurementKind(
                    matched.get(
                        "measurement_kind", MeasurementKind.UNKNOWN.value
                    )
                ),
            )
            return CapacityRead(
                snapshot,
                ReasonCode.CAPACITY_STALE if stale else None,
                (
                    f"{lane_id}: row checked_at absent; stale low-confidence evidence"
                    if missing_row_time
                    else ""
                ),
                health=health,
            )
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            return CapacityRead(
                None, ReasonCode.CAPACITY_INVALID, str(exc), health=health
            )
