from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from hermes_cli.fleet import live


@pytest.fixture
def server():
    from hermes_cli import web_server

    return web_server


def _qualification(**patch):
    now = datetime.now(timezone.utc)
    values = {
        "qualified": True,
        "captured_at": now,
        "expires_at": now + timedelta(minutes=5),
        "auth_kind": "cli_subscription",
        "auth_source": "antigravity:agy-live-receipt",
        "provider_id": "antigravity-subscription",
        "models": ("gemini-3.1-pro-high", "gemini-3.1-pro-low"),
        "executable": "C:/tools/agy.exe",
        "subscription_only_proven": True,
        "paid_fallback_absent": True,
        "parent_session_proven": True,
    }
    values.update(patch)
    return SimpleNamespace(**values)


def _doctor_for(monkeypatch, qualification):
    class Doctor:
        def qualify(self, profiles):
            assert [profile.lane_id for profile in profiles] == ["antigravity"]
            return {"antigravity": qualification}

    monkeypatch.setattr(live, "FleetQualificationDoctor", Doctor)


def test_antigravity_account_descriptor_is_external_and_curated_between_grok_and_anthropic(server):
    rows = server._build_oauth_catalog()
    ids = [row["id"] for row in rows]
    row = next(row for row in rows if row["id"] == "antigravity-subscription")

    assert row["name"] == "Antigravity · Gemini 3.1 Pro High"
    assert row["flow"] == "external"
    assert row["cli_command"] == "agy"
    assert (
        ids.index("xai-oauth")
        < ids.index("antigravity-subscription")
        < ids.index("anthropic")
    )


def test_qualified_antigravity_inventory_row_is_a_draft_only_fleet_parent(monkeypatch):
    from hermes_cli import inventory

    _doctor_for(monkeypatch, _qualification())

    row = inventory._antigravity_parent_provider_row()

    assert row["slug"] == "antigravity-subscription"
    assert row["selection_kind"] == "fleet_parent"
    assert row["fleet_lane_id"] == "antigravity"
    assert row["selectable"] is True
    assert row["source"] == "fleet_auto"
    assert row["models"][0] == "gemini-3.1-pro-high"
    assert set(row["models"]) <= {
        "gemini-3.1-pro-high",
        "gemini-3.1-pro-low",
        "gemini-3.6-flash-high",
        "gemini-3.6-flash-medium",
        "gemini-3.6-flash-low",
        "gemini-3.5-flash-high",
        "gemini-3.5-flash-medium",
        "gemini-3.5-flash-low",
    }


def test_antigravity_account_connects_only_from_live_consumer_subscription_proof(monkeypatch, server):
    _doctor_for(monkeypatch, _qualification())

    status = server._antigravity_status()

    assert status["logged_in"] is True
    assert status["source"] == "antigravity_subscription"
    assert status["source_label"] == "Live Antigravity consumer subscription · Gemini 3.1 Pro High"
    assert status["token_preview"] is None
    assert status["has_refresh_token"] is False


@pytest.mark.parametrize(
    "patch",
    [
        {"qualified": False},
        {"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)},
        {"auth_kind": "api_key"},
        {"auth_source": "google:adc"},
        {"provider_id": "gemini"},
        {"models": ("gemini-3.6-flash-high",)},
        {"executable": None},
        {"subscription_only_proven": False},
        {"paid_fallback_absent": False},
        {"parent_session_proven": False},
    ],
)
def test_antigravity_account_fails_closed_for_unqualified_or_wrong_route(monkeypatch, patch, server):
    _doctor_for(monkeypatch, _qualification(**patch))

    assert server._antigravity_status()["logged_in"] is False


def test_antigravity_account_fails_closed_when_doctor_raises(monkeypatch, server):
    class Doctor:
        def qualify(self, profiles):
            raise RuntimeError("receipt unavailable")

    monkeypatch.setattr(live, "FleetQualificationDoctor", Doctor)

    status = server._antigravity_status()

    assert status["logged_in"] is False
    assert "receipt unavailable" not in str(status)


def test_raw_google_credentials_never_create_an_antigravity_account(monkeypatch, server):
    monkeypatch.setenv("GEMINI_API_KEY", "should-not-authorize-account")
    monkeypatch.setenv("GOOGLE_API_KEY", "should-not-authorize-account")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "should-not-authorize-account")
    _doctor_for(monkeypatch, _qualification(qualified=False))

    status = server._antigravity_status()
    rows = server._build_oauth_catalog()

    assert status["logged_in"] is False
    assert not ({row["id"] for row in rows} & {"gemini", "google", "vertex", "google-adc"})
