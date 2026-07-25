from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hermes_cli.fleet.capacity import BridgeUsageAdapter
from hermes_cli.fleet.config import parse_fleet_config
from hermes_cli.fleet.parent_models import (
    ADMITTED_CLAUDE_PARENT_MODEL,
    is_admitted_parent_model,
    is_sonnet_model,
)
from hermes_cli.fleet.usage_paths import (
    DEFAULT_USAGE_RELATIVE,
    default_native_usage_path,
    resolve_usage_path,
)
from hermes_cli.fleet.usage_refresh import (
    UsageRefreshError,
    refresh_usage_document,
)
from hermes_cli.fleet.types import Freshness, ReasonCode


NOW = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _disable_live_console_health_probe(monkeypatch):
    """Keep unit tests independent of installed console-agent state."""
    monkeypatch.setattr(
        "hermes_cli.fleet.usage_refresh._probe_console_lane_health",
        lambda lane_id: (None, f"{lane_id} health probe disabled in unit test"),
    )


@dataclass(frozen=True)
class _Window:
    label: str
    used_percent: float


@dataclass(frozen=True)
class _Snapshot:
    windows: tuple[_Window, ...]


def test_default_usage_path_is_profile_home_relative(tmp_path, monkeypatch):
    home = tmp_path / "profile-a"
    monkeypatch.setenv("HERMES_HOME", str(home))

    path = default_native_usage_path()

    assert path == (home / DEFAULT_USAGE_RELATIVE).resolve()
    assert "HermesBridge" not in str(path)


def test_resolve_usage_path_profile_isolation(tmp_path):
    a = resolve_usage_path(None, home=tmp_path / "a")
    b = resolve_usage_path(None, home=tmp_path / "b")
    relative = resolve_usage_path("custom/usage.json", home=tmp_path / "a")
    absolute = resolve_usage_path(tmp_path / "abs.json", home=tmp_path / "a")

    assert a != b
    assert a.parent.name == "fleet"
    assert relative == (tmp_path / "a" / "custom" / "usage.json").resolve()
    assert absolute == (tmp_path / "abs.json")


def test_parse_fleet_config_defaults_to_native_home_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    config = parse_fleet_config({})

    assert config.bridge_usage_file == default_native_usage_path()
    assert config.bridge_usage_file.name == "usage-weekly.json"


def test_bridge_adapter_default_path_is_native(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    adapter = BridgeUsageAdapter()
    assert adapter.path == default_native_usage_path()


def test_refresh_atomic_write_and_per_lane_freshness(tmp_path, monkeypatch):
    path = tmp_path / "fleet" / "usage-weekly.json"
    mirror = tmp_path / "mirror" / "usage-weekly.json"

    def fetch(provider: str):
        if provider == "openai-codex":
            return _Snapshot((_Window("Weekly", 33.0),))
        if provider == "anthropic":
            return _Snapshot((_Window("Current week", 44.0),))
        return None

    monkeypatch.setattr(
        "hermes_cli.fleet.usage_refresh._probe_console_lane_health",
        lambda lane_id: (None, "health probe unavailable in unit test"),
    )

    report = refresh_usage_document(
        path=path,
        mirror_path=mirror,
        fetch_usage=fetch,
        now=NOW,
    )

    assert report.ok
    assert report.path == path
    assert report.mirrored_to == mirror
    document = json.loads(path.read_text(encoding="utf-8"))
    plans = {row["label"]: row for row in document["plans"]}
    assert plans["ChatGPT Pro · Codex"]["weekly_pct_used"] == 33.0
    assert plans["ChatGPT Pro · Codex"]["checked_at"].startswith("2026-07-24T12:00:00")
    assert plans["Claude Max 20x"]["checked_at"].startswith("2026-07-24T12:00:00")
    assert "checked_at" not in plans["SuperGrok"]
    assert "checked_at" not in plans["Google AI · Antigravity"]
    assert mirror.exists()

    # Failure preserves prior bytes.
    prior = path.read_bytes()

    def boom(provider: str):
        raise RuntimeError("network down")

    failed = refresh_usage_document(
        path=path,
        mirror_path=None,
        fetch_usage=boom,
        now=NOW,
        create_if_missing=False,
    )
    assert failed.ok is False
    assert path.read_bytes() == prior


def test_claude_refresh_persists_most_exhausted_relevant_weekly_window(
    tmp_path, monkeypatch
):
    path = tmp_path / "fleet" / "usage-weekly.json"

    def fetch(provider: str):
        if provider == "openai-codex":
            return _Snapshot((_Window("Weekly", 5.0),))
        if provider == "anthropic":
            return _Snapshot(
                (
                    _Window("Current week", 20.0),
                    _Window("Opus week", 80.0),
                    _Window("Sonnet week", 99.0),
                    _Window("Five hour session", 100.0),
                )
            )
        return None

    monkeypatch.setattr(
        "hermes_cli.fleet.usage_refresh._probe_console_lane_health",
        lambda lane_id: (None, "health probe unavailable in unit test"),
    )

    report = refresh_usage_document(
        path=path,
        mirror_path=None,
        fetch_usage=fetch,
        now=NOW,
    )

    assert report.ok
    plans = {
        row["label"]: row
        for row in json.loads(path.read_text(encoding="utf-8"))["plans"]
    }
    assert plans["Claude Max 20x"]["weekly_pct_used"] == 80.0


def test_refresh_console_only_stale_cannot_win_capacity(tmp_path):
    path = tmp_path / "usage.json"
    path.write_text(
        json.dumps(
            {
                "checked_at": "2026-07-20T00:00:00Z",
                "plans": [
                    {
                        "label": "ChatGPT Pro · Codex",
                        "weekly_pct_used": 10,
                        "checked_at": "2026-07-24T11:00:00Z",
                    },
                    {
                        "label": "SuperGrok",
                        "weekly_pct_used": 0,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    def fetch(provider: str):
        if provider == "openai-codex":
            return _Snapshot((_Window("Weekly", 12.0),))
        return None

    refresh_usage_document(path=path, mirror_path=None, fetch_usage=fetch, now=NOW)
    adapter = BridgeUsageAdapter(path)
    grok = adapter.read("grok", now=NOW)
    codex = adapter.read("chatgpt_codex", now=NOW)

    assert grok.snapshot is not None
    assert grok.snapshot.freshness is Freshness.STALE
    assert grok.reason is ReasonCode.CAPACITY_STALE
    assert codex.snapshot is not None
    assert codex.snapshot.freshness is Freshness.FRESH


def test_sonnet_is_never_an_admitted_parent_model():
    assert is_sonnet_model("claude-sonnet-4-6")
    assert is_sonnet_model("anthropic/claude-sonnet-4.6")
    assert is_sonnet_model("Sonnet 4 6")
    assert not is_sonnet_model(ADMITTED_CLAUDE_PARENT_MODEL)
    assert not is_admitted_parent_model("claude-sonnet-4-6")
    assert is_admitted_parent_model("gpt-5.6-sol")
    assert is_admitted_parent_model(ADMITTED_CLAUDE_PARENT_MODEL)


def test_missing_usage_file_create_shell_without_fabricating_console_freshness(tmp_path):
    path = tmp_path / "missing.json"

    def fetch(provider: str):
        if provider == "openai-codex":
            return _Snapshot((_Window("Weekly", 1.0),))
        return None

    report = refresh_usage_document(
        path=path, mirror_path=None, fetch_usage=fetch, now=NOW
    )
    document = report.document
    grok = next(row for row in document["plans"] if "Grok" in row["label"])
    assert "checked_at" not in grok


def _codex_ok_claude_fail(provider: str):
    if provider == "openai-codex":
        return _Snapshot((_Window("Weekly", 18.0),))
    if provider == "anthropic":
        raise RuntimeError("claude oauth unavailable")
    return None


def test_failed_claude_auto_fetch_from_fresh_shell_cannot_inherit_root_freshness(
    tmp_path,
):
    """Fresh shell + Codex success must not make unfetched Claude appear FRESH@0%."""
    path = tmp_path / "fresh-shell.json"

    report = refresh_usage_document(
        path=path,
        mirror_path=None,
        fetch_usage=_codex_ok_claude_fail,
        now=NOW,
    )
    assert report.ok
    document = json.loads(path.read_text(encoding="utf-8"))
    plans = {row["label"]: row for row in document["plans"]}
    assert plans["ChatGPT Pro · Codex"]["checked_at"].startswith("2026-07-24T12:00:00")
    assert "checked_at" not in plans["Claude Max 20x"]
    assert document["checked_at"].startswith("2026-07-24T12:00:00")

    adapter = BridgeUsageAdapter(path)
    claude = adapter.read("claude_code", now=NOW)
    codex = adapter.read("chatgpt_codex", now=NOW)

    assert codex.snapshot is not None
    assert codex.snapshot.freshness is Freshness.FRESH
    assert codex.reason is None
    assert claude.snapshot is not None
    assert claude.snapshot.freshness is Freshness.STALE
    assert claude.snapshot.confidence.name == "LOW"
    assert claude.reason is ReasonCode.CAPACITY_STALE
    assert "checked_at absent" in claude.detail


def test_failed_claude_auto_fetch_from_existing_document_cannot_inherit_root_freshness(
    tmp_path,
):
    """Existing doc without Claude row time stays stale after sibling refresh advances root."""
    path = tmp_path / "existing.json"
    path.write_text(
        json.dumps(
            {
                "checked_at": "2026-07-20T00:00:00Z",
                "plans": [
                    {
                        "label": "ChatGPT Pro · Codex",
                        "weekly_pct_used": 55,
                        "checked_at": "2026-07-24T10:00:00Z",
                    },
                    {
                        "label": "Claude Max 20x",
                        "weekly_pct_used": 0,
                        # Never had a per-lane checked_at.
                    },
                    {
                        "label": "SuperGrok",
                        "weekly_pct_used": 0,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    report = refresh_usage_document(
        path=path,
        mirror_path=None,
        fetch_usage=_codex_ok_claude_fail,
        now=NOW,
    )
    assert report.ok
    document = json.loads(path.read_text(encoding="utf-8"))
    plans = {row["label"]: row for row in document["plans"]}
    assert plans["ChatGPT Pro · Codex"]["weekly_pct_used"] == 18.0
    assert plans["ChatGPT Pro · Codex"]["checked_at"].startswith("2026-07-24T12:00:00")
    assert "checked_at" not in plans["Claude Max 20x"]
    assert plans["Claude Max 20x"]["weekly_pct_used"] == 0
    assert document["checked_at"].startswith("2026-07-24T12:00:00")

    adapter = BridgeUsageAdapter(path)
    claude = adapter.read("claude_code", now=NOW)
    codex = adapter.read("chatgpt_codex", now=NOW)
    grok = adapter.read("grok", now=NOW)

    assert codex.snapshot is not None
    assert codex.snapshot.freshness is Freshness.FRESH
    assert claude.snapshot is not None
    assert claude.snapshot.freshness is Freshness.STALE
    assert claude.snapshot.confidence.name == "LOW"
    assert claude.reason is ReasonCode.CAPACITY_STALE
    assert claude.snapshot.used_pct == 0  # decimal-comparable via == 0
    assert grok.snapshot is not None
    assert grok.snapshot.freshness is Freshness.STALE


def test_failed_claude_preserves_prior_valid_row_timestamp(tmp_path):
    """A previously valid Claude timestamp stays authoritative when Claude fetch fails."""
    path = tmp_path / "prior-claude.json"
    path.write_text(
        json.dumps(
            {
                "checked_at": "2026-07-24T11:00:00Z",
                "plans": [
                    {
                        "label": "ChatGPT Pro · Codex",
                        "weekly_pct_used": 10,
                        "checked_at": "2026-07-24T11:00:00Z",
                    },
                    {
                        "label": "Claude Max 20x",
                        "weekly_pct_used": 42,
                        "checked_at": "2026-07-24T11:30:00Z",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    refresh_usage_document(
        path=path,
        mirror_path=None,
        fetch_usage=_codex_ok_claude_fail,
        now=NOW,
    )
    document = json.loads(path.read_text(encoding="utf-8"))
    plans = {row["label"]: row for row in document["plans"]}
    assert plans["Claude Max 20x"]["checked_at"] == "2026-07-24T11:30:00Z"
    assert plans["Claude Max 20x"]["weekly_pct_used"] == 42
    assert plans["ChatGPT Pro · Codex"]["checked_at"].startswith("2026-07-24T12:00:00")

    adapter = BridgeUsageAdapter(path)
    claude = adapter.read("claude_code", now=NOW)
    assert claude.snapshot is not None
    assert claude.snapshot.freshness is Freshness.FRESH
    assert claude.snapshot.confidence.name == "HIGH"
    assert claude.reason is None


def test_fleet_refresh_usage_ps1_has_no_dated_worktree_fallback():
    script = (
        Path(__file__).resolve().parents[3]
        / "scripts"
        / "fleet_refresh_usage.ps1"
    )
    text = script.read_text(encoding="utf-8")
    assert "fleet-parent-routing-20260724" not in text
    assert "PSScriptRoot" in text
    assert "hermes-agent" in text


def test_console_health_probe_never_makes_stale_usage_fresh(tmp_path, monkeypatch):
    path = tmp_path / "usage-weekly.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "plans-1",
                "checked_at": "2026-07-01T00:00:00.000Z",
                "plans": [
                    {
                        "label": "SuperGrok",
                        "weekly_pct_used": 12,
                        "resets": "weekly",
                    },
                    {
                        "label": "Google AI · Antigravity",
                        "weekly_pct_used": 18,
                        "resets": "weekly",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    def fetch(provider: str):
        if provider == "openai-codex":
            return _Snapshot((_Window("Weekly", 20.0),))
        if provider == "anthropic":
            return _Snapshot((_Window("Weekly", 30.0),))
        return None

    monkeypatch.setattr(
        "hermes_cli.fleet.usage_refresh._probe_console_lane_health",
        lambda lane_id: (True, f"{lane_id} healthy"),
    )

    report = refresh_usage_document(
        path=path,
        home=tmp_path,
        mirror_path=None,
        fetch_usage=fetch,
        now=NOW,
    )
    assert report.ok
    document = json.loads(path.read_text(encoding="utf-8"))
    by_label = {row["label"]: row for row in document["plans"]}
    grok = by_label["SuperGrok"]
    agy = by_label["Google AI · Antigravity"]
    assert grok["weekly_pct_used"] == 12
    assert agy["weekly_pct_used"] == 18
    assert "checked_at" not in grok
    assert "checked_at" not in agy
    assert "measurement_kind" not in agy
    assert "comparability_group" not in grok
    assert grok["health_status"] == "UP"
    assert agy["health_status"] == "UP"
    assert grok["health_checked_at"] == "2026-07-24T12:00:00.000Z"

    read = BridgeUsageAdapter(path).read("grok", now=NOW)
    assert read.snapshot is not None
    assert read.snapshot.freshness is Freshness.STALE
    assert read.health is not None
    assert read.health.freshness is Freshness.FRESH



def test_console_health_persists_when_all_auto_usage_refreshes_fail(
    tmp_path, monkeypatch
):
    path = tmp_path / "usage-weekly.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "plans-1",
                "checked_at": "2026-07-01T00:00:00.000Z",
                "source": "prior usage evidence",
                "plans": [
                    {
                        "label": "ChatGPT Pro · Codex",
                        "weekly_pct_used": 10,
                        "checked_at": "2026-07-01T00:00:00.000Z",
                    },
                    {
                        "label": "Claude Max 20x",
                        "weekly_pct_used": 20,
                        "checked_at": "2026-07-01T00:00:00.000Z",
                    },
                    {
                        "label": "SuperGrok",
                        "weekly_pct_used": 30,
                        "checked_at": "2026-07-01T00:00:00.000Z",
                    },
                    {
                        "label": "Google AI · Antigravity",
                        "weekly_pct_used": 40,
                        "checked_at": "2026-07-01T00:00:00.000Z",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "hermes_cli.fleet.usage_refresh._probe_console_lane_health",
        lambda lane_id: (True, f"{lane_id} healthy"),
    )

    report = refresh_usage_document(
        path=path,
        home=tmp_path,
        mirror_path=None,
        fetch_usage=lambda _provider: None,
        now=NOW,
    )

    assert report.ok
    document = json.loads(path.read_text(encoding="utf-8"))
    by_label = {row["label"]: row for row in document["plans"]}
    assert document["checked_at"] == "2026-07-01T00:00:00.000Z"
    assert by_label["SuperGrok"]["weekly_pct_used"] == 30
    assert by_label["SuperGrok"]["checked_at"] == "2026-07-01T00:00:00.000Z"
    assert by_label["SuperGrok"]["health_status"] == "UP"
    assert by_label["SuperGrok"]["health_checked_at"] == "2026-07-24T12:00:00.000Z"
    assert by_label["Google AI · Antigravity"]["weekly_pct_used"] == 40
    assert by_label["Google AI · Antigravity"]["health_status"] == "UP"



def test_confirmed_console_down_persists_when_auto_usage_fails(
    tmp_path, monkeypatch
):
    path = tmp_path / "usage-weekly.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "plans-1",
                "checked_at": "2026-07-01T00:00:00.000Z",
                "plans": [
                    {"label": "ChatGPT Pro · Codex", "weekly_pct_used": 10},
                    {"label": "Claude Max 20x", "weekly_pct_used": 20},
                    {
                        "label": "SuperGrok",
                        "weekly_pct_used": 30,
                        "health_status": "UP",
                        "health_checked_at": "2026-07-24T11:00:00.000Z",
                    },
                    {
                        "label": "Google AI · Antigravity",
                        "weekly_pct_used": 40,
                        "health_status": "UP",
                        "health_checked_at": "2026-07-24T11:00:00.000Z",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "hermes_cli.fleet.usage_refresh._probe_console_lane_health",
        lambda lane_id: (False, f"{lane_id} confirmed down"),
    )

    report = refresh_usage_document(
        path=path,
        home=tmp_path,
        mirror_path=None,
        fetch_usage=lambda _provider: None,
        now=NOW,
    )

    assert report.ok
    document = json.loads(path.read_text(encoding="utf-8"))
    by_label = {row["label"]: row for row in document["plans"]}
    assert by_label["SuperGrok"]["health_status"] == "DOWN"
    assert by_label["SuperGrok"]["health_checked_at"] == "2026-07-24T12:00:00.000Z"
    assert by_label["Google AI · Antigravity"]["health_status"] == "DOWN"


def test_timestamped_console_attestation_is_fresh_and_comparable(
    tmp_path, monkeypatch
):
    path = tmp_path / "usage-weekly.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "plans-1",
                "checked_at": "2026-07-01T00:00:00.000Z",
                "plans": [
                    {"label": "ChatGPT Pro · Codex", "weekly_pct_used": 10},
                    {"label": "Claude Max 20x", "weekly_pct_used": 20},
                    {"label": "SuperGrok", "weekly_pct_used": 30},
                    {
                        "label": "Google AI · Antigravity",
                        "weekly_pct_used": 40,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    attestation = tmp_path / "fleet" / "usage-console-attestation.json"
    attestation.parent.mkdir(parents=True)
    attestation.write_text(
        json.dumps(
            {
                "checked_at": "2026-07-24T12:00:00.000Z",
                "lanes": {"grok": {"weekly_pct_used": 5}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "hermes_cli.fleet.usage_refresh._probe_console_lane_health",
        lambda lane_id: (True, f"{lane_id} healthy"),
    )

    report = refresh_usage_document(
        path=path,
        home=tmp_path,
        mirror_path=None,
        fetch_usage=lambda _provider: None,
        now=NOW,
    )

    assert report.ok
    document = json.loads(path.read_text(encoding="utf-8"))
    grok = next(row for row in document["plans"] if row["label"] == "SuperGrok")
    assert grok["weekly_pct_used"] == 5
    assert grok["checked_at"] == "2026-07-24T12:00:00.000Z"
    assert grok["measurement_kind"] == "measured"
    assert grok["comparability_group"] == "subscription-weekly"
    assert grok["quota_window_id"] == "subscription-weekly"

    read = BridgeUsageAdapter(path).read("grok", now=NOW)
    assert read.snapshot is not None
    assert read.snapshot.freshness is Freshness.FRESH
    assert read.reason is None


@pytest.mark.parametrize(
    "invalid_pct",
    [True, False, -1, 101, 10**400, float("nan"), float("inf"), float("-inf"), "bad"],
)
def test_invalid_console_attestation_percentage_is_ignored(
    tmp_path, invalid_pct
):
    path = tmp_path / "usage-weekly.json"
    old_checked_at = "2026-07-01T00:00:00.000Z"
    path.write_text(
        json.dumps(
            {
                "schema_version": "plans-1",
                "checked_at": old_checked_at,
                "plans": [
                    {"label": "ChatGPT Pro · Codex", "weekly_pct_used": 10},
                    {"label": "Claude Max 20x", "weekly_pct_used": 20},
                    {
                        "label": "SuperGrok",
                        "weekly_pct_used": 30,
                        "checked_at": old_checked_at,
                    },
                    {"label": "Google AI · Antigravity", "weekly_pct_used": 40},
                ],
            }
        ),
        encoding="utf-8",
    )
    attestation = tmp_path / "fleet" / "usage-console-attestation.json"
    attestation.parent.mkdir(parents=True)
    attestation.write_text(
        json.dumps(
            {
                "checked_at": "2026-07-24T12:00:00.000Z",
                "lanes": {"grok": {"weekly_pct_used": invalid_pct}},
            }
        ),
        encoding="utf-8",
    )

    def fetch(provider):
        if provider == "openai-codex":
            return _Snapshot((_Window("Weekly", 5.0),))
        if provider == "anthropic":
            return _Snapshot((_Window("Current week", 10.0),))
        return None

    report = refresh_usage_document(
        path=path,
        home=tmp_path,
        mirror_path=None,
        fetch_usage=fetch,
        now=NOW,
    )

    assert report.ok
    document = json.loads(path.read_text(encoding="utf-8"))
    grok = next(row for row in document["plans"] if row["label"] == "SuperGrok")
    assert grok["weekly_pct_used"] == 30
    assert grok["checked_at"] == old_checked_at
    read = BridgeUsageAdapter(path).read("grok", now=NOW)
    assert read.snapshot is not None
    assert read.snapshot.freshness is Freshness.STALE
