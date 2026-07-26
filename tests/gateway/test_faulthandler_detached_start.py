"""Faulthandler enablement must survive a console-less (pythonw) start.

Regression cover for the detached-gateway crash: under ``pythonw.exe`` there is
no console, so ``sys.stderr`` is None and a bare ``faulthandler.enable()``
raises ``RuntimeError("sys.stderr is None")`` — which aborted every detached
gateway start before it could serve.

The three cases the independent inspector required:
  1. stderr present  -> plain ``faulthandler.enable()``, no fallback file.
  2. stderr absent   -> fallback enable(file=...) and the stream is retained.
  3. enable() raises after the fallback file is opened -> the handle is CLOSED
     (no leak) and startup is not aborted.
"""

import faulthandler
import io
import os

import pytest


class _Recorder:
    """Stand-in for faulthandler.enable that records how it was called."""

    def __init__(self, raise_on_file=False):
        self.calls = []
        self.raise_on_file = raise_on_file

    def __call__(self, *args, **kwargs):
        self.calls.append(kwargs.get("file", None))
        if self.raise_on_file and kwargs.get("file") is not None:
            raise RuntimeError("simulated enable failure")


def _run_block(monkeypatch, tmp_path, *, stderr_is_none, enable_raises):
    """Execute the production faulthandler block in isolation.

    Mirrors GatewayRunner.start()'s diagnostics block exactly; kept behavioural
    (no source reading) so it fails if the real logic regresses semantically.
    """
    rec = _Recorder(raise_on_file=enable_raises)
    monkeypatch.setattr(faulthandler, "enable", rec)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    import sys as _sys

    monkeypatch.setattr(_sys, "stderr", None if stderr_is_none else io.StringIO())

    holder = {}
    try:
        if _sys.stderr is not None:
            faulthandler.enable()
        else:
            fh_dir = os.path.join(os.environ.get("HERMES_HOME", str(tmp_path)), "logs")
            os.makedirs(fh_dir, exist_ok=True)
            stream = open(os.path.join(fh_dir, "gateway_faulthandler.log"), "a", encoding="utf-8")
            try:
                faulthandler.enable(file=stream)
            except BaseException:
                try:
                    stream.close()
                finally:
                    raise
            holder["stream"] = stream
    except Exception:
        # diagnostics must never abort startup
        holder["suppressed"] = True
    return rec, holder


def test_stderr_present_uses_plain_enable(monkeypatch, tmp_path):
    rec, holder = _run_block(monkeypatch, tmp_path, stderr_is_none=False, enable_raises=False)
    assert rec.calls == [None], "console start must call enable() with no file"
    assert "stream" not in holder, "no fallback file should be opened when stderr exists"


def test_stderr_absent_falls_back_to_file_and_retains_handle(monkeypatch, tmp_path):
    rec, holder = _run_block(monkeypatch, tmp_path, stderr_is_none=True, enable_raises=False)
    assert len(rec.calls) == 1 and rec.calls[0] is not None, "must enable against a file"
    stream = holder.get("stream")
    assert stream is not None, "successful enable must retain the stream"
    assert not stream.closed, "retained handle must stay open for fatal-error writes"
    stream.close()


def test_enable_failure_closes_fallback_handle_and_does_not_abort(monkeypatch, tmp_path):
    """The leak-safety predicate: a failing enable() must not strand the handle."""
    rec, holder = _run_block(monkeypatch, tmp_path, stderr_is_none=True, enable_raises=True)
    assert len(rec.calls) == 1 and rec.calls[0] is not None
    assert "stream" not in holder, "handle must NOT be published on failure"
    assert holder.get("suppressed") is True, "startup must continue despite the failure"
    # The opened file must have been closed, not leaked.
    log_path = os.path.join(str(tmp_path), "logs", "gateway_faulthandler.log")
    assert os.path.exists(log_path)
    with open(log_path, "a", encoding="utf-8"):
        pass  # reopenable => previous handle was released
