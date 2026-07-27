"""Deterministic supervisor writer: observed evidence outranks worker claims.

Inspector ruling: "deterministic supervisor evidence (exit status, signal,
provider envelope) should outrank an LLM-authored cause where available."

The dispatcher already computed the terminal facts at reap time and discarded
them in favour of text mining — which produced the false positive that started
all of this (a build diagnosed "quota exhausted" because the worker was WRITING
A TEST FILE containing that phrase).
"""

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture()
def logdir(tmp_path, monkeypatch):
    d = tmp_path / "logs"
    d.mkdir()
    monkeypatch.setattr(kb, "worker_logs_dir", lambda board=None: d)
    return d


def _log(logdir, task_id, text):
    (logdir / f"{task_id}.log").write_text(text, encoding="utf-8")


# ------------------------------------------------------------- precedence ----
def test_observed_evidence_outranks_a_worker_claim(logdir):
    """A worker cannot talk its way out of what the OS reported."""
    kb.write_terminal_record("t_x", cause="i_totally_succeeded", run_id=3)
    kb.write_supervisor_record("t_x", kind="signaled", code=9, run_id=3)
    out = kb.diagnose_worker_failure("t_x", run_id=3)
    assert out.startswith("observed:killed_by_signal")


def test_the_worker_claim_is_preserved_not_erased(logdir):
    """The two answer different questions: what happened to the process, versus
    what the worker believed it was doing. Both are evidence."""
    kb.write_terminal_record("t_x", cause="iteration_ceiling", run_id=3)
    kb.write_supervisor_record("t_x", kind="signaled", code=9, run_id=3)
    assert "declared:iteration_ceiling" in kb.diagnose_worker_failure("t_x", run_id=3)


def test_observed_beats_the_legacy_heuristic(logdir):
    """Inference must never win over an observation."""
    _log(logdir, "t_x", "Error: APIConnectionError")
    kb.write_supervisor_record("t_x", kind="rate_limited", code=75, run_id=1)
    out = kb.diagnose_worker_failure("t_x", run_id=1)
    assert out.startswith("observed:provider_rate_limited")
    assert "legacy-inference" not in out


def test_without_observation_the_declaration_still_wins(logdir):
    """Precedence is a chain, not a replacement: observed > declared > inferred."""
    _log(logdir, "t_x", "Error: APIConnectionError")
    kb.write_terminal_record("t_x", cause="iteration_ceiling", run_id=1)
    assert kb.diagnose_worker_failure("t_x", run_id=1).startswith("declared:")


# ------------------------------------------------------ deterministic slugs ---
@pytest.mark.parametrize("kind,cause", [
    ("rate_limited", "provider_rate_limited"),
    ("protocol_violation", "worker_protocol_violation"),
    ("signaled", "killed_by_signal"),
    ("nonzero_exit", "nonzero_exit"),
    ("unknown", "process_vanished"),
])
def test_every_exit_kind_maps_to_a_stable_slug(logdir, kind, cause):
    kb.write_supervisor_record("t_x", kind=kind, code=1, run_id=1)
    assert kb.diagnose_worker_failure("t_x", run_id=1).startswith("observed:" + cause)


def test_unknown_exit_kind_does_not_invent_a_cause(logdir):
    """An unrecognised kind falls back to the neutral slug, never a guess."""
    kb.write_supervisor_record("t_x", kind="something_new", code=1, run_id=1)
    assert kb.diagnose_worker_failure("t_x", run_id=1).startswith("observed:nonzero_exit")


# ------------------------------------------------------------- run binding ---
def test_supervisor_record_is_run_bound(logdir):
    """A record from a PREVIOUS run must not be read as this run's cause."""
    kb.write_supervisor_record("t_x", kind="signaled", code=9, run_id=5)
    assert kb.diagnose_worker_failure("t_x", run_id=5).startswith("observed:")
    assert kb.diagnose_worker_failure("t_x", run_id=6) is None


def test_content_must_agree_with_the_run_it_is_filed_under(logdir):
    """Defence in depth — the path separates runs, but mismatched CONTENT is
    still rejected rather than trusted."""
    path = logdir / f"t_x.run8{kb.SUPERVISOR_RECORD_SUFFIX}"
    path.write_text(
        '{"version":1,"task_id":"t_x","run_id":"7","cause":"killed_by_signal"}',
        encoding="utf-8")
    state, rec = kb.read_supervisor_record("t_x", run_id=8)
    assert state == "invalid"
    assert rec["reason"] == "run_id_mismatch"


# --------------------------------------------------------------- fail closed --
@pytest.mark.parametrize("blob,reason", [
    ("{corrupt", "unparseable"),
    ('["not","an","object"]', "not_an_object"),
    ('{"version":99,"cause":"x","task_id":"t_x"}', "version_mismatch"),
    ('{"version":1,"task_id":"t_x"}', "no_cause"),
    ('{"version":1,"cause":"x","task_id":"OTHER"}', "task_id_mismatch"),
])
def test_unusable_record_is_invalid_with_a_specific_reason(logdir, blob, reason):
    (logdir / f"t_x{kb.SUPERVISOR_RECORD_SUFFIX}").write_text(blob, encoding="utf-8")
    state, rec = kb.read_supervisor_record("t_x")
    assert state == "invalid"
    assert rec["reason"] == reason


def test_absent_record_is_absent_not_invalid(logdir):
    assert kb.read_supervisor_record("t_nothing") == ("absent", None)


def test_write_never_raises(logdir, monkeypatch):
    """Recording terminal evidence must never be able to kill the reaper.

    If this raised, detect_crashed_workers would die mid-sweep and crashed tasks
    would stay claimed forever — strictly worse than the lossy message it
    replaces.
    """
    monkeypatch.setattr(kb, "worker_logs_dir",
                        lambda board=None: (_ for _ in ()).throw(OSError("gone")))
    assert kb.write_supervisor_record("t_x", kind="signaled") is False


def test_round_trip_carries_the_observed_facts(logdir):
    kb.write_supervisor_record("t_x", kind="rate_limited", code=75, pid=4242, run_id=2)
    state, rec = kb.read_supervisor_record("t_x", run_id=2)
    assert state == "valid"
    assert rec["source"] == "supervisor"
    assert rec["exit_kind"] == "rate_limited"
    assert rec["code"] == 75
    assert rec["pid"] == 4242
