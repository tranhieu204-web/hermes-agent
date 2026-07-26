"""Faulthandler enablement must survive a console-less (pythonw) start.

Regression cover for the detached-gateway crash: under ``pythonw.exe`` there is
no console, so ``sys.stderr`` is None and a bare ``faulthandler.enable()``
raises ``RuntimeError("sys.stderr is None")`` -- which aborted every detached
gateway start before it could serve.

These tests bind to the PRODUCTION helper ``gateway.run.install_startup_faulthandler``
(the exact function ``GatewayRunner.start()`` calls), so a regression in
production fails them.
"""

import faulthandler
import io
import types

import pytest

from gateway.run import install_startup_faulthandler


class _EnableRecorder:
    """Records how faulthandler.enable was called and can simulate failure."""

    def __init__(self, exc=None):
        self.files = []
        self.exc = exc

    def __call__(self, *args, **kwargs):
        stream = kwargs.get("file")
        self.files.append(stream)
        if self.exc is not None and stream is not None:
            raise self.exc


def _patch(monkeypatch, tmp_path, *, stderr_is_none, exc=None):
    rec = _EnableRecorder(exc=exc)
    monkeypatch.setattr(faulthandler, "enable", rec)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import gateway.run as gwrun

    monkeypatch.setattr(gwrun.sys, "stderr", None if stderr_is_none else io.StringIO())
    return rec


def test_console_start_uses_plain_enable_and_publishes_nothing(monkeypatch, tmp_path):
    rec = _patch(monkeypatch, tmp_path, stderr_is_none=False)
    owner = types.SimpleNamespace()
    install_startup_faulthandler(owner)
    assert rec.files == [None], "console start must call enable() with no file"
    assert not hasattr(owner, "_faulthandler_stream"), "no fallback stream on the console path"


def test_console_less_start_falls_back_and_retains_open_handle(monkeypatch, tmp_path):
    rec = _patch(monkeypatch, tmp_path, stderr_is_none=True)
    owner = types.SimpleNamespace()
    install_startup_faulthandler(owner)
    assert len(rec.files) == 1 and rec.files[0] is not None, "must enable against a file"
    stream = getattr(owner, "_faulthandler_stream", None)
    assert stream is not None, "successful enable must publish the stream"
    assert stream.closed is False, "retained handle must stay open for fatal-error writes"
    assert stream is rec.files[0], "the published handle must be the one enable() received"
    stream.close()


@pytest.mark.parametrize("exc", [RuntimeError("boom"), KeyboardInterrupt(), SystemExit(1)])
def test_enable_failure_closes_handle_and_never_publishes(monkeypatch, tmp_path, exc):
    """Leak-safety predicate: a failing enable() must close, not strand, the handle."""
    rec = _patch(monkeypatch, tmp_path, stderr_is_none=True, exc=exc)
    owner = types.SimpleNamespace()
    if isinstance(exc, Exception):
        install_startup_faulthandler(owner)  # swallowed: startup must continue
    else:
        with pytest.raises(BaseException):
            install_startup_faulthandler(owner)
    assert len(rec.files) == 1 and rec.files[0] is not None
    assert rec.files[0].closed is True, "the opened handle MUST be closed on failure"
    assert not hasattr(owner, "_faulthandler_stream"), "must not publish a failed stream"
