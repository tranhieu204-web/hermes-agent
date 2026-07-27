"""Terminal-cause diagnosis for dead kanban workers (S7: lossy failure evidence).

30 of 50 runs crashed on 2026-07-27 and every one recorded `pid <N> not alive`.
The real causes sat in the per-task worker log and reached nobody — which is why
the operator learned Grok was out of credits only by asking.

Heavy emphasis on FALSE-POSITIVE rejection: a wrong cause on the board is worse
than "unknown", because it sends the operator after the wrong problem. A crude
`grep 429` over the same logs produced two phantom rate-limit incidents that were
really source line ranges (`L3250-3429`).
"""

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture()
def logdir(tmp_path, monkeypatch):
    d = tmp_path / "logs"
    d.mkdir()
    monkeypatch.setattr(kb, "worker_logs_dir", lambda board=None: d)
    return d


def _write(logdir, task_id, text):
    (logdir / f"{task_id}.log").write_text(text, encoding="utf-8")


# ------------------------------------------------------------ real signatures
@pytest.mark.parametrize(
    "blob,expected",
    [
        ('HTTP 403: {"code":"personal-team-blocked:spending-limit","error":"You have '
         'run out of credits or need a Grok subscription."}',
         "provider quota exhausted (out of credits/subscription)"),
        ("API call failed (attempt 1/3): APIConnectionError\n Error: Connection error.",
         "provider unreachable (connection/TLS)"),
        ("openai.RateLimitError: rate_limit_exceeded", "provider rate limited"),
        ("PermissionDeniedError [HTTP 401] invalid_api_key", "provider auth rejected"),
        ("Cannot compress further — context length exceeded", "context window exhausted"),
    ],
)
def test_recognises_real_failure_signatures(logdir, blob, expected):
    _write(logdir, "t_x", blob)
    got = kb.diagnose_worker_failure("t_x")
    assert got == "legacy-inference(low-confidence): " + expected


def test_quota_outranks_auth(logdir):
    """A 403 spending-limit is a QUOTA problem, not an auth problem.

    Both needles are present in the real Grok log; the ordering must not send the
    operator to re-authenticate when the account is simply out of credits.
    """
    _write(logdir, "t_x", 'PermissionDeniedError [HTTP 403] {"code":"personal-team-blocked:'
                          'spending-limit","error":"run out of credits"}')
    assert kb.diagnose_worker_failure("t_x") == (
        "legacy-inference(low-confidence): provider quota exhausted (out of credits/subscription)")


# ------------------------------------------------- false positives MUST be None
@pytest.mark.parametrize(
    "blob",
    [
        "  read      config.py L3250-3429  0.7s",       # real: line range, not HTTP 429
        "  read      integration.py L240-429  0.5s",    # real: line range
        "**Patching assertions and verifying rate limits**",  # prose about rate limits
        "Session: 20260726_182836 Duration: 18s\nCANARY2-OK",  # a SUCCESSFUL run
        "",
    ],
)
def test_does_not_invent_a_cause(logdir, blob):
    _write(logdir, "t_x", blob)
    assert kb.diagnose_worker_failure("t_x") is None


def test_missing_log_returns_none(logdir):
    assert kb.diagnose_worker_failure("t_absent") is None


def test_unreadable_log_never_raises(logdir, monkeypatch):
    """Diagnosis is enrichment: it must NEVER stop a crashed task being released.

    If this raised, detect_crashed_workers would die and crashed tasks would
    stay claimed forever — strictly worse than the lossy message it replaces.
    """
    _write(logdir, "t_x", "spending-limit")

    def _boom(*a, **k):
        raise OSError("disk gone")

    monkeypatch.setattr("builtins.open", _boom)
    assert kb.diagnose_worker_failure("t_x") is None


def test_scans_the_tail_of_a_large_log(logdir):
    """The cause is at the END of a long run; scanning the head would miss it."""
    _write(logdir, "t_x", ("noise line\n" * 60_000) + "\nAPIConnectionError\n")
    assert kb.diagnose_worker_failure("t_x") == (
        "legacy-inference(low-confidence): provider unreachable (connection/TLS)")


def test_scan_is_bounded_to_the_tail(logdir):
    """Only the TAIL is diagnosed — this pins the bound, not just the outcome.

    A signature that appears ONLY in the head of a log larger than the scan
    window must NOT be reported: the terminal cause is what killed the worker,
    not an error it already recovered from. Without this the earlier
    "scan the head instead" mutation passed, because an unbounded read()
    happened to reach the tail anyway.
    """
    head_only = "APIConnectionError\n" + ("x" * 400_000) + "\nclean shutdown\n"
    _write(logdir, "t_x", head_only)
    assert kb.diagnose_worker_failure("t_x") is None


# ------------------------ structured terminal record (durable D1 fix) --------
# Text-mining cannot separate a terminal report from a worker discussing,
# testing or WRITING ABOUT an error. A worker therefore declares its cause and
# diagnosis becomes a field lookup.

def test_declared_record_beats_the_log_heuristic(logdir):
    """THE case that defeated every heuristic round.

    The log looks exactly like quota exhaustion because the worker was authoring
    a test containing those strings. The declaration is authoritative.
    """
    _write(logdir, "t_x",
           '+ log_text="run out of credits"\n'
           '+ "personal-team-blocked:spending-limit"\n'
           + ("filler\n" * 500))
    assert "quota" in (kb.diagnose_worker_failure("t_x") or "")   # heuristic is fooled

    kb.write_terminal_record("t_x", cause="iteration_ceiling",
                             provider="openai-codex", code=180, retryable=True)
    out = kb.diagnose_worker_failure("t_x")
    assert out.startswith("declared:iteration_ceiling")
    assert "quota" not in out


def test_record_round_trips(logdir):
    assert kb.write_terminal_record("t_x", cause="provider_quota_exhausted",
                                    provider="xai-oauth", code=403, retryable=False)
    state, rec = kb.read_terminal_record("t_x")
    assert state == "valid"
    assert rec["cause"] == "provider_quota_exhausted"
    assert rec["retryable"] is False


@pytest.mark.parametrize("blob", ['{not json', '{"version":99,"cause":"x"}', '{"version":1}'])
def test_unusable_record_is_ignored_not_trusted(logdir, blob):
    """A corrupt, mis-versioned or causeless record must fall through, never
    become a fabricated diagnosis."""
    (logdir / f"t_x{kb.TERMINAL_RECORD_SUFFIX}").write_text(blob, encoding="utf-8")
    state, _ = kb.read_terminal_record("t_x")
    assert state == "invalid"


# --------------------- inspector HOLD findings: run-binding + tri-state -------
def test_declaration_is_bound_to_the_run(logdir):
    """A declaration from a PREVIOUS run must not be read as this run's cause.

    Inspector HOLD: the task-scoped path allowed exactly that — a cleaner false
    positive than the one this feature exists to remove.
    """
    kb.write_terminal_record("t_x", cause="iteration_ceiling", run_id=7)
    assert kb.diagnose_worker_failure("t_x", run_id=7).startswith("declared:")
    assert kb.diagnose_worker_failure("t_x", run_id=8) is None


def test_record_content_must_agree_with_the_run_it_is_filed_under(logdir):
    """Defence in depth: the PATH separates runs, but a record whose CONTENT
    names a different run must still be rejected rather than trusted.

    Without this the in-record run_id check is unreachable — the earlier
    mutation removing it passed, because the path alone hid the mismatch.
    """
    path = logdir / f"t_x.run8{kb.TERMINAL_RECORD_SUFFIX}"
    path.write_text(
        '{"version":1,"task_id":"t_x","run_id":"7","cause":"iteration_ceiling"}',
        encoding="utf-8")
    state, rec = kb.read_terminal_record("t_x", run_id=8)
    assert state == "invalid"
    assert rec["reason"] == "run_id_mismatch"


def test_invalid_record_does_not_reactivate_the_heuristic(logdir):
    """Invalid is a FACT, not an absence — it must not be replaced by a guess."""
    _write(logdir, "t_x", "Error: APIConnectionError")   # heuristic would fire
    (logdir / f"t_x{kb.TERMINAL_RECORD_SUFFIX}").write_text("{corrupt", encoding="utf-8")
    out = kb.diagnose_worker_failure("t_x")
    assert out.startswith(kb.TERMINAL_RECORD_INVALID)
    assert "legacy-inference" not in out


@pytest.mark.parametrize("blob,reason", [
    ("{corrupt", "unparseable"),
    ('["not","an","object"]', "not_an_object"),
    ('{"version":99,"cause":"x","task_id":"t_x"}', "version_mismatch"),
    ('{"version":1,"task_id":"t_x"}', "no_cause"),
    ('{"version":1,"cause":"x","task_id":"OTHER"}', "task_id_mismatch"),
])
def test_invalid_reasons_are_specific(logdir, blob, reason):
    (logdir / f"t_x{kb.TERMINAL_RECORD_SUFFIX}").write_text(blob, encoding="utf-8")
    state, rec = kb.read_terminal_record("t_x")
    assert state == "invalid"
    assert rec["reason"] == reason


def test_legacy_inference_is_labelled_low_confidence(logdir):
    """The residual heuristic is quarantined advisory, never presented as fact."""
    _write(logdir, "t_x", "Error: APIConnectionError")
    assert kb.diagnose_worker_failure("t_x").startswith("legacy-inference(low-confidence):")


def test_declared_cause_is_labelled_declared(logdir):
    """Never 'diagnosed' or 'authoritative' — it is the worker's own claim."""
    kb.write_terminal_record("t_x", cause="provider_quota_exhausted")
    assert kb.diagnose_worker_failure("t_x").startswith("declared:")


def test_write_never_raises(logdir, monkeypatch):
    """Declaring a cause must never be able to kill the worker doing it."""
    monkeypatch.setattr(kb, "worker_logs_dir",
                        lambda board=None: (_ for _ in ()).throw(OSError("gone")))
    assert kb.write_terminal_record("t_x", cause="whatever") is False
