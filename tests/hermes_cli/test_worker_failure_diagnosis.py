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
    assert kb.diagnose_worker_failure("t_x") == expected


def test_quota_outranks_auth(logdir):
    """A 403 spending-limit is a QUOTA problem, not an auth problem.

    Both needles are present in the real Grok log; the ordering must not send the
    operator to re-authenticate when the account is simply out of credits.
    """
    _write(logdir, "t_x", 'PermissionDeniedError [HTTP 403] {"code":"personal-team-blocked:'
                          'spending-limit","error":"run out of credits"}')
    assert kb.diagnose_worker_failure("t_x") == "provider quota exhausted (out of credits/subscription)"


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
    assert kb.diagnose_worker_failure("t_x") == "provider unreachable (connection/TLS)"


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
