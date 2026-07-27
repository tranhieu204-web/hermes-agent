"""Weekly-window classification: exact label first, narrow horizon fallback.

chatgpt_codex was UNMEASURED — OpenAI's usage API returns a single
``primary_window`` labelled "Session" resetting ~5.7 days out and no
``secondary_window`` percentage, while the matcher only accepted the exact label
"weekly". The router was choosing lanes blind.

The fallback is deliberately narrow (ruling: Hermes 2026-07-27) — exactly ONE
usable window AND a 5-8 day horizon. A generic ">24h" rule was rejected: reading
a short burn window as weekly headroom would corrupt builder routing and the
reserve floor. Most tests here assert the REFUSALS, because staying unmeasured is
the safe failure and a fallback that fires too eagerly is the real hazard.
"""

from datetime import datetime, timedelta, timezone

import pytest

from hermes_cli.fleet.live_capacity import weekly_used_percents

NOW = datetime(2026, 7, 27, 2, 30, tzinfo=timezone.utc)


class _W:
    def __init__(self, label, used_percent, reset_in_days=None):
        self.label = label
        self.used_percent = used_percent
        self.reset_at = None if reset_in_days is None else NOW + timedelta(days=reset_in_days)


class _Snap:
    def __init__(self, *windows):
        self.windows = tuple(windows)


def _pcts(*windows, lane_id="chatgpt_codex"):
    return weekly_used_percents(_Snap(*windows), lane_id=lane_id, now=NOW)


# ------------------------------------------------------------ label is primary
def test_exact_weekly_label_still_wins():
    assert _pcts(_W("Session", 39.0, 0.2), _W("Weekly", 61.0, 5.7)) == [61.0]


def test_label_match_preferred_over_horizon_fallback():
    """A labelled window must win even when a lone window would also qualify."""
    assert _pcts(_W("Weekly", 61.0, 6.0)) == [61.0]


def test_claude_lane_labels_unchanged():
    assert _pcts(_W("Current session", 4.0, 0.1), _W("Current week", 12.0, 5.0),
                 lane_id="claude_code") == [12.0]


# ---------------------------------------------------------------- fallback ON
def test_lone_window_in_horizon_band_is_accepted():
    """The live Codex shape: one 'Session' window resetting ~5.7 days out."""
    assert _pcts(_W("Session", 39.0, 5.7)) == [39.0]


@pytest.mark.parametrize("days", [5.0, 6.5, 8.0])
def test_band_edges_inclusive(days):
    assert _pcts(_W("Session", 39.0, days)) == [39.0]


# --------------------------------------------------------------- fallback OFF
@pytest.mark.parametrize("days", [0.05, 0.9, 4.99, 8.01, 30.0])
def test_horizon_outside_band_stays_unmeasured(days):
    """A 5h session window or a monthly window must NEVER read as weekly."""
    assert _pcts(_W("Session", 39.0, days)) == []


def test_multiple_windows_never_trigger_fallback():
    """Ambiguity means unmeasured — we cannot tell which window is the weekly one."""
    assert _pcts(_W("Session", 39.0, 5.7), _W("Other", 10.0, 6.0)) == []


def test_missing_reset_time_stays_unmeasured():
    assert _pcts(_W("Session", 39.0, None)) == []


def test_no_windows_stays_unmeasured():
    assert _pcts() == []


def test_window_without_percentage_is_not_usable():
    assert _pcts(_W("Session", None, 5.7)) == []


def test_unknown_lane_gets_no_fallback_by_label_but_still_needs_evidence():
    """An unknown lane has no relevant labels; the same narrow rule applies."""
    assert _pcts(_W("Session", 39.0, 5.7), lane_id="mystery_lane") == [39.0]
    assert _pcts(_W("Session", 39.0, 1.0), lane_id="mystery_lane") == []
