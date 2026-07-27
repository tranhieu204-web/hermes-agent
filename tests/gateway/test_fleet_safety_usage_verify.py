"""Unit tests for usage verification (the 10%-vs-100% tracker-bug fix)."""

import json
from datetime import datetime, timezone

from gateway.fleet_safety.usage_verify import (
    extract_authoritative,
    load_cached_percent,
    verify_usage,
    worst_used_percent,
)


class _Win:
    def __init__(self, used_percent):
        self.used_percent = used_percent


class _Snap:
    def __init__(self, available, windows, fetched_at):
        self._available = available
        self.windows = windows
        self.fetched_at = fetched_at

    @property
    def available(self):
        return self._available


# -- the core reconciliation --------------------------------------------------


def test_authoritative_wins_and_flags_suspect_on_divergence():
    # The incident: cache says 10%, authoritative says 100%.
    v = verify_usage(
        "grok",
        cached_percent=10.0, cached_fetched_at=1000.0,
        authoritative_percent=100.0, authoritative_available=True,
        now=1000.0, divergence_points=15.0,
    )
    assert v.used_percent == 100.0          # trust authoritative, not the cache
    assert v.source == "authoritative"
    assert v.suspect is True                # 90-pt gap → flagged
    assert any("diverges" in r for r in v.reasons)


def test_authoritative_agreeing_cache_is_trustworthy():
    v = verify_usage(
        "grok",
        cached_percent=88.0, cached_fetched_at=1000.0,
        authoritative_percent=90.0, authoritative_available=True,
        now=1000.0, divergence_points=15.0,
    )
    assert v.used_percent == 90.0
    assert v.suspect is False
    assert v.trustworthy is True


def test_cache_used_but_suspect_when_authoritative_unavailable():
    v = verify_usage(
        "grok",
        cached_percent=10.0, cached_fetched_at=1000.0,
        authoritative_percent=None, authoritative_available=False,
        now=1000.0,
    )
    assert v.used_percent == 10.0
    assert v.source == "cache"
    assert v.suspect is True                # unverifiable → never fully trusted
    assert v.trustworthy is False


def test_stale_cache_flagged():
    v = verify_usage(
        "grok",
        cached_percent=50.0, cached_fetched_at=0.0,
        authoritative_percent=None, authoritative_available=False,
        now=100_000.0, max_age_seconds=900.0,
    )
    assert v.stale is True
    assert any("old" in r for r in v.reasons)


def test_nothing_available_returns_unknown():
    v = verify_usage(
        "grok",
        cached_percent=None, cached_fetched_at=None,
        authoritative_percent=None, authoritative_available=False,
        now=1000.0,
    )
    assert v.used_percent is None
    assert v.source == "none"
    assert v.suspect is True and v.stale is True


def test_authoritative_available_is_fresh_even_with_old_cache():
    v = verify_usage(
        "grok",
        cached_percent=10.0, cached_fetched_at=0.0,
        authoritative_percent=95.0, authoritative_available=True,
        now=100_000.0, max_age_seconds=900.0,
    )
    # authoritative is fresh by definition, so not stale despite ancient cache
    assert v.stale is False
    assert v.used_percent == 95.0


def test_stale_authoritative_snapshot_is_not_trustworthy():
    v = verify_usage(
        "grok",
        cached_percent=None,
        cached_fetched_at=None,
        authoritative_percent=95.0,
        authoritative_available=True,
        authoritative_fetched_at=0.0,
        now=100_000.0,
        max_age_seconds=900.0,
    )
    assert v.stale is True
    assert v.authoritative_age_seconds == 100_000.0
    assert v.trustworthy is False


# -- adapters -----------------------------------------------------------------


def test_worst_used_percent_takes_max_across_windows():
    assert worst_used_percent([_Win(10.0), _Win(88.0), _Win(None), _Win(42.0)]) == 88.0
    assert worst_used_percent([{"used_percent": 5}, {"used_percent": 99}]) == 99.0
    assert worst_used_percent([_Win(None)]) is None
    assert worst_used_percent([]) is None


def test_extract_authoritative_from_snapshot():
    dt = datetime(2026, 7, 25, 0, 0, tzinfo=timezone.utc)
    snap = _Snap(available=True, windows=[_Win(10.0), _Win(100.0)], fetched_at=dt)
    available, percent, epoch = extract_authoritative(snap)
    assert available is True
    assert percent == 100.0
    assert epoch == dt.timestamp()


def test_extract_authoritative_marks_unavailable_without_percent():
    snap = _Snap(available=True, windows=[_Win(None)], fetched_at=None)
    available, percent, epoch = extract_authoritative(snap)
    assert available is False           # no numeric window → can't verify
    assert percent is None


def test_extract_authoritative_none_snapshot():
    assert extract_authoritative(None) == (False, None, None)


def test_load_cached_percent_flat_schema(tmp_path):
    p = tmp_path / "usage-weekly.json"
    p.write_text(json.dumps({"used_percent": 42.0, "fetched_at": 1000.0}))
    pct, ts = load_cached_percent(p, "grok")
    assert pct == 42.0 and ts == 1000.0


def test_load_cached_percent_provider_map_and_iso(tmp_path):
    p = tmp_path / "usage-weekly.json"
    p.write_text(json.dumps({
        "providers": {
            "grok": {"used_percent": 10.0, "updated_at": "2026-07-25T00:00:00Z"},
        }
    }))
    pct, ts = load_cached_percent(p, "grok")
    assert pct == 10.0
    assert ts == datetime(2026, 7, 25, tzinfo=timezone.utc).timestamp()


def test_load_cached_percent_missing_file(tmp_path):
    assert load_cached_percent(tmp_path / "nope.json", "grok") == (None, None)


def test_load_cached_percent_garbage_file(tmp_path):
    p = tmp_path / "usage-weekly.json"
    p.write_text("{not valid json")
    assert load_cached_percent(p, "grok") == (None, None)
