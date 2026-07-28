"""Progress contract: a live worker is not necessarily a working worker (S1).

On 2026-07-26 a kanban worker sat at ~0 CPU for 45 minutes producing nothing
while the dispatcher counted it healthy the entire time, because the only signal
was PID liveness. An earlier `codex exec` hung the same way for ~4.5 hours.

The decision logic is pure so it can be exercised exhaustively. The bias
throughout is AGAINST false stalls: killing slow-but-live work is worse than
catching a real hang late, so anything unobservable must resolve to
UNOBSERVABLE, never STALLED.
"""

import pytest

from hermes_cli.kanban_db import (
    DEFAULT_STALL_SECONDS,
    PROGRESS_PROGRESSING,
    PROGRESS_STALLED,
    PROGRESS_UNOBSERVABLE,
    evaluate_worker_progress,
    worker_progress_fingerprint,
)

NOW = 1_785_100_000


def _ev(prev, cur, last, now=NOW, stall=DEFAULT_STALL_SECONDS):
    return evaluate_worker_progress(prev, cur, last, now, stall)


# ------------------------------------------------------------- real progress
def test_changed_fingerprint_is_progress():
    assert _ev("100:5", "220:9", NOW - 10_000)[0] == PROGRESS_PROGRESSING


def test_progress_resets_the_stall_clock():
    state, stalled_for = _ev("100:5", "220:9", NOW - 10_000)
    assert (state, stalled_for) == (PROGRESS_PROGRESSING, 0)


def test_unchanged_but_within_window_is_not_yet_stalled():
    state, stalled_for = _ev("100:5", "100:5", NOW - 60)
    assert state == PROGRESS_PROGRESSING
    assert stalled_for == 60


# -------------------------------------------------------------- real stalls
def test_unchanged_past_the_window_is_stalled():
    state, stalled_for = _ev("100:5", "100:5", NOW - 3600)
    assert state == PROGRESS_STALLED
    assert stalled_for == 3600


def test_the_45_minute_delegation_loop_would_be_caught():
    """The actual 2026-07-26 incident: alive, zero output, 45 minutes."""
    state, stalled_for = _ev("4096:1", "4096:1", NOW - 45 * 60)
    assert state == PROGRESS_STALLED
    assert stalled_for == 2700


def test_boundary_is_inclusive():
    assert _ev("a", "a", NOW - DEFAULT_STALL_SECONDS)[0] == PROGRESS_STALLED
    assert _ev("a", "a", NOW - (DEFAULT_STALL_SECONDS - 1))[0] == PROGRESS_PROGRESSING


# ---------------------------------------------- must NEVER be a false stall
def test_unobservable_when_there_is_no_current_fingerprint():
    """No log yet is not a hang. Absence of evidence is not evidence."""
    assert _ev("100:5", None, NOW - 99_999)[0] == PROGRESS_UNOBSERVABLE


def test_first_observation_never_stalls():
    """No previous fingerprint = no baseline; start the clock, don't accuse."""
    assert _ev(None, "100:5", None)[0] == PROGRESS_PROGRESSING


def test_unchanged_with_no_baseline_timestamp_is_unobservable():
    assert _ev("100:5", "100:5", None)[0] == PROGRESS_UNOBSERVABLE


def test_a_worker_that_never_wrote_a_log_is_not_stalled():
    assert _ev(None, None, NOW - 99_999)[0] == PROGRESS_UNOBSERVABLE


# ------------------------------------------------------------- fingerprints
def test_fingerprint_tracks_growth(tmp_path, monkeypatch):
    import hermes_cli.kanban_db as kb

    monkeypatch.setattr(kb, "worker_logs_dir", lambda board=None: tmp_path)
    log = tmp_path / "t_x.log"
    log.write_text("start", encoding="utf-8")
    first = worker_progress_fingerprint("t_x")
    assert first is not None
    log.write_text("start and then considerably more output", encoding="utf-8")
    assert worker_progress_fingerprint("t_x") != first


def test_fingerprint_is_none_when_absent(tmp_path, monkeypatch):
    import hermes_cli.kanban_db as kb

    monkeypatch.setattr(kb, "worker_logs_dir", lambda board=None: tmp_path)
    assert worker_progress_fingerprint("t_missing") is None


def test_fingerprint_never_raises(tmp_path, monkeypatch):
    import hermes_cli.kanban_db as kb

    def _boom(board=None):
        raise OSError("gone")

    monkeypatch.setattr(kb, "worker_logs_dir", _boom)
    assert worker_progress_fingerprint("t_x") is None


def test_fingerprint_is_not_self_authored(tmp_path, monkeypatch):
    """A worker cannot fake progress by claiming health.

    The fingerprint is derived from bytes it actually produced, so a hung child
    with a fresh heartbeat still fingerprints identically.
    """
    import hermes_cli.kanban_db as kb

    monkeypatch.setattr(kb, "worker_logs_dir", lambda board=None: tmp_path)
    log = tmp_path / "t_x.log"
    log.write_text("output", encoding="utf-8")
    fp = worker_progress_fingerprint("t_x")
    # heartbeat updated, no new output -> identical fingerprint -> stall clock runs
    assert worker_progress_fingerprint("t_x") == fp
    assert _ev(fp, fp, NOW - 3600)[0] == PROGRESS_STALLED
