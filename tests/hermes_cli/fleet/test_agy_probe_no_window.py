"""The agy console probe must not flash a terminal on Windows.

The pre-dispatch usage refresh probes ``agy models`` on every ~60s dispatcher
tick. Under ``pythonw.exe`` (the detached gateway) a console-subsystem child
allocates its own console, so without ``CREATE_NO_WINDOW`` the operator sees a
terminal appear and vanish every minute (reported 2026-07-26).

Production-bound: imports the real helper and the real probe.
"""

import subprocess
import sys

import pytest

from hermes_cli.fleet import usage_refresh


def test_no_console_creationflags_uses_create_no_window_on_windows():
    flags = usage_refresh.no_console_creationflags()
    if sys.platform == "win32":
        assert flags == subprocess.CREATE_NO_WINDOW, "Windows must suppress the console"
        assert flags != 0
    else:
        assert flags == 0, "non-Windows must be a no-op"


def test_agy_probe_passes_no_window_flag(monkeypatch, tmp_path):
    """The probe must actually pass the suppression flag to subprocess.run."""
    fake = tmp_path / "agy.exe"
    fake.write_text("", encoding="utf-8")
    monkeypatch.setattr(usage_refresh, "_resolve_agy_executable", lambda: str(fake))

    seen = {}

    class _Completed:
        returncode = 0
        stdout = "gemini-3.1-pro-high"
        stderr = ""

    def _fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["creationflags"] = kwargs.get("creationflags")
        return _Completed()

    monkeypatch.setattr(subprocess, "run", _fake_run)
    usage_refresh._probe_agy_health()

    assert seen["cmd"][1] == "models", "probe must still run `agy models`"
    assert seen["creationflags"] == usage_refresh.no_console_creationflags()
    if sys.platform == "win32":
        assert seen["creationflags"] == subprocess.CREATE_NO_WINDOW


def test_live_command_probe_passes_no_window_flag(monkeypatch):
    """grok/xai + claude receipts go through fleet.live._command — same suppression."""
    from hermes_cli.fleet import live as live_mod

    seen = {}

    class _C:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def _fake_run(cmd, **kwargs):
        seen["creationflags"] = kwargs.get("creationflags")
        return _C()

    monkeypatch.setattr(subprocess, "run", _fake_run)
    live_mod._command(["grok", "--version"])
    assert seen["creationflags"] == usage_refresh.no_console_creationflags()
    if sys.platform == "win32":
        assert seen["creationflags"] == subprocess.CREATE_NO_WINDOW
