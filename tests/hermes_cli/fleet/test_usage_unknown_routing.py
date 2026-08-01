from __future__ import annotations

import json
from datetime import datetime, timezone

from hermes_cli.fleet.usage_refresh import refresh_usage_document


NOW = datetime(2026, 7, 30, 7, 0, tzinfo=timezone.utc)


def test_stale_console_usage_is_explicitly_unknown_after_fresh_health_probe(
    tmp_path, monkeypatch
):
    path = tmp_path / "usage-weekly.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "plans-1",
                "checked_at": "2026-07-30T07:00:00Z",
                "plans": [
                    {"label": "ChatGPT Pro · Codex", "weekly_pct_used": 10},
                    {"label": "Claude Max 20x", "weekly_pct_used": 20},
                    {
                        "label": "SuperGrok",
                        "weekly_pct_used": 11,
                        "checked_at": "2026-07-28T00:00:00Z",
                        "measurement_kind": "measured",
                        "comparability_group": "subscription-weekly",
                        "quota_window_id": "subscription-weekly",
                    },
                    {
                        "label": "Google AI · Antigravity",
                        "weekly_pct_used": 56,
                        "checked_at": "2026-07-28T00:00:00Z",
                        "measurement_kind": "measured",
                        "comparability_group": "subscription-weekly",
                        "quota_window_id": "subscription-weekly",
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
        mirror_path=None,
        fetch_usage=lambda _provider: None,
        now=NOW,
    )

    assert report.ok
    rows = {
        row["label"]: row
        for row in json.loads(path.read_text(encoding="utf-8"))["plans"]
    }
    for label, historical_pct in (
        ("SuperGrok", 11),
        ("Google AI · Antigravity", 56),
    ):
        row = rows[label]
        assert row["weekly_pct_used"] == historical_pct
        assert row["measurement_kind"] == "unknown"
        assert row["usage_status"] == "STALE_UNKNOWN"
        assert row["health_status"] == "UP"
        assert "comparability_group" not in row
        assert "quota_window_id" not in row
