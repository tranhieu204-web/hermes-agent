"""Fresh authoritative capacity for the canonical fleet composition root."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from threading import Lock
from typing import Callable

from .capacity import (
    _QUANTUM,
    _TOTAL,
    BridgeUsageAdapter,
    _percentage,
    _plan_percentage,
)
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


RELEVANT_WEEKLY_WINDOWS = {
    "chatgpt_codex": frozenset({"weekly"}),
    # Claude's aggregate weekly cap and the Opus-specific cap can both
    # constrain the admitted Opus lane. Session and Sonnet-only windows do
    # not describe this lane's weekly headroom.
    "claude_code": frozenset({"current week", "opus week"}),
}


class LiveUsageAdapter:
    """Prefer live subscription usage for natively queryable lanes."""

    _PROVIDERS = {
        "chatgpt_codex": "openai-codex",
        "claude_code": "anthropic",
    }

    def __init__(
        self,
        bridge: BridgeUsageAdapter,
        *,
        fetch_usage: Callable[[str], object | None] | None = None,
        max_age: timedelta = timedelta(minutes=15),
        future_tolerance: timedelta = timedelta(seconds=30),
        cache_ttl: timedelta = timedelta(seconds=30),
    ) -> None:
        self.bridge = bridge
        self.fetch_usage = fetch_usage
        self.max_age = max_age
        self.future_tolerance = future_tolerance
        self.cache_ttl = cache_ttl
        self._source_cache: dict[str, tuple[datetime, object | None]] = {}
        self._cache_lock = Lock()

    @property
    def path(self) -> Path:
        return self.bridge.path

    def _fetch(self, provider: str) -> object | None:
        if self.fetch_usage is None:
            from agent.account_usage import fetch_account_usage

            return fetch_account_usage(provider)
        return self.fetch_usage(provider)

    def _fetch_cached(self, provider: str, *, read_at: datetime) -> object | None:
        """Fetch at most once per provider during a short inspection cycle."""

        with self._cache_lock:
            cached = self._source_cache.get(provider)
            if cached is not None:
                cached_at, source = cached
                if cached_at <= read_at <= cached_at + self.cache_ttl:
                    return source
            source = self._fetch(provider)
            self._source_cache[provider] = (read_at, source)
            return source

    @classmethod
    def _weekly_used_values(cls, lane_id: str, source: object) -> list[Decimal]:
        relevant = RELEVANT_WEEKLY_WINDOWS.get(lane_id, frozenset())
        values: list[Decimal] = []
        for window in getattr(source, "windows", ()) or ():
            label = str(getattr(window, "label", "") or "").strip().casefold()
            used = getattr(window, "used_percent", None)
            if label not in relevant or used is None:
                continue
            values.append(_plan_percentage(used))
        return values

    def read(
        self,
        lane_id: str,
        *,
        now: datetime | None = None,
        reserved_pct: Decimal = Decimal("0"),
    ) -> CapacityRead:
        provider = self._PROVIDERS.get(lane_id)
        if provider is None:
            return self.bridge.read(
                lane_id,
                now=now,
                reserved_pct=reserved_pct,
            )

        read_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        bridge_read = self.bridge.read(
            lane_id,
            now=read_at,
            reserved_pct=reserved_pct,
        )

        def bridge_fallback(detail: str) -> CapacityRead:
            if bridge_read.snapshot is None:
                return CapacityRead(
                    None,
                    bridge_read.reason or ReasonCode.CAPACITY_MISSING,
                    detail,
                    health=bridge_read.health,
                )
            bridge_detail = "; ".join(
                item for item in (detail, bridge_read.detail) if item
            )
            return CapacityRead(
                bridge_read.snapshot,
                bridge_read.reason,
                bridge_detail,
                health=bridge_read.health,
            )

        try:
            source = self._fetch_cached(provider, read_at=read_at)
        except Exception as exc:  # noqa: BLE001 - live evidence fails closed
            return bridge_fallback(
                f"{lane_id}: authoritative usage unavailable ({type(exc).__name__})",
            )
        if source is None or not bool(getattr(source, "available", False)):
            return bridge_fallback(
                f"{lane_id}: authoritative usage unavailable",
            )
        try:
            captured_at = getattr(source, "fetched_at")
            if not isinstance(captured_at, datetime):
                raise ValueError("authoritative fetched_at is missing")
            captured_at = (
                captured_at
                if captured_at.tzinfo is not None
                else captured_at.replace(tzinfo=timezone.utc)
            ).astimezone(timezone.utc)
            if captured_at > read_at + self.future_tolerance:
                raise ValueError("authoritative fetched_at is in the future")
            used_values = self._weekly_used_values(lane_id, source)
            if not used_values:
                raise ValueError(
                    "authoritative usage has no lane-relevant weekly window"
                )
            used = max(used_values)
            remaining = (_TOTAL - used).quantize(_QUANTUM)
            reserved = _percentage(str(reserved_pct))
            expires_at = captured_at + self.max_age
            freshness = (
                Freshness.FRESH if read_at <= expires_at else Freshness.STALE
            )
            source_id = (
                f"authoritative_account_usage:{provider}:"
                f"{captured_at.isoformat()}"
            )
            overage_disabled = (
                bridge_read.snapshot.overage_disabled
                if bridge_read.snapshot is not None
                else None
            )
            snapshot = CapacitySnapshot(
                lane_id=lane_id,
                used_pct=used,
                remaining_pct=remaining,
                reserved_pct=reserved,
                effective_remaining_pct=max(
                    Decimal("0"), remaining - reserved
                ).quantize(_QUANTUM),
                source_kind="authoritative_account_usage",
                source_id=source_id,
                captured_at=captured_at,
                read_at=read_at,
                expires_at=expires_at,
                freshness=freshness,
                confidence=Confidence.HIGH,
                schema_version="account-usage-1",
                overage_disabled=overage_disabled,
                comparability_group="subscription-weekly",
                quota_window_id="subscription-weekly",
                measurement_kind=MeasurementKind.MEASURED,
            )
            return CapacityRead(
                snapshot,
                (
                    None
                    if freshness is Freshness.FRESH
                    else ReasonCode.CAPACITY_STALE
                ),
                health=bridge_read.health
                or HealthRead(
                    status=LaneHealth.UP,
                    captured_at=captured_at,
                    read_at=read_at,
                    expires_at=expires_at,
                    freshness=freshness,
                    source_id=source_id,
                ),
            )
        except (TypeError, ValueError, InvalidOperation) as exc:
            return bridge_fallback(
                f"{lane_id}: {exc}",
            )
