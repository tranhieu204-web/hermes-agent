"""Per-call-site guards: every fleet CLI subprocess path must suppress the console.

Under ``pythonw.exe`` (the detached gateway) a console-subsystem child allocates
its own console and flashes a terminal on the operator's screen. Incomplete
call-site coverage survived two submissions, so each site named by the
independent inspector gets a direct assertion that the PRODUCTION call passes
``creationflags``.

Every test drives real production code and asserts the kwarg that actually
reaches the process launcher, so removing the flag at a site fails that site.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli.fleet.usage_refresh import no_console_creationflags


EXPECTED = no_console_creationflags()


class _Completed:
    returncode = 0
    stdout = ""
    stderr = ""


def _recorder():
    seen = {}

    def _fake(*args, **kwargs):
        seen["called"] = True
        seen["creationflags"] = kwargs.get("creationflags")
        return _Completed()

    return seen, _fake


def _request(tmp_path, model="m"):
    return type(
        "R",
        (),
        {
            "prompt": "x",
            "cwd": str(tmp_path),
            "timeout_seconds": 5,
            "model": model,
            "effort": "high",
        },
    )()


def _qualification(exe_path):
    """Qualification stub whose executable matches, so guards pass."""
    return type("Q", (), {"executable": str(exe_path), "qualified": True, "models": ("m",)})()


def test_helper_matches_platform():
    if sys.platform == "win32":
        assert EXPECTED == subprocess.CREATE_NO_WINDOW
    else:
        assert EXPECTED == 0


def test_site_external_cli_execute_passes_flag(monkeypatch, tmp_path):
    """ExternalCliAdapter.execute -> subprocess.run"""
    from hermes_cli.fleet.adapters import external_cli

    exe = tmp_path / "dummy.exe"
    exe.write_text("", encoding="utf-8")

    seen, fake = _recorder()
    monkeypatch.setattr(external_cli.subprocess, "run", fake)
    monkeypatch.setattr(external_cli, "validate_execution", lambda *a, **k: None)

    adapter = external_cli.ExternalCliAdapter(str(exe))
    monkeypatch.setattr(adapter, "_resolved_executable", lambda: Path(exe).resolve())
    monkeypatch.setattr(adapter, "cancelled", lambda: False, raising=False)

    try:
        adapter.execute(_request(tmp_path), _qualification(exe))
    except Exception:
        pass

    assert seen.get("called"), "production path never reached subprocess.run"
    assert seen["creationflags"] == EXPECTED


def test_site_native_child_popen_passes_flag(monkeypatch, tmp_path):
    """run_native_hermes_child -> subprocess.Popen"""
    from hermes_cli.fleet.adapters import live_routes

    seen = {}

    class _P:
        returncode = 0

        def communicate(self, *a, **k):
            return "{}", ""

        def kill(self):
            pass

    def _fake_popen(*args, **kwargs):
        seen["called"] = True
        seen["creationflags"] = kwargs.get("creationflags")
        return _P()

    monkeypatch.setattr(live_routes.subprocess, "Popen", _fake_popen)
    try:
        live_routes.run_native_hermes_child(
            provider_id="p", model="m", effort="high", prompt="x",
            cwd=str(tmp_path), timeout_seconds=5,
        )
    except Exception:
        pass

    assert seen.get("called"), "production path never reached subprocess.Popen"
    assert seen["creationflags"] == EXPECTED


@pytest.mark.parametrize("lane", ["antigravity", "claude_code"])
def test_site_subscription_adapter_passes_flag(monkeypatch, tmp_path, lane):
    """_SubscriptionCliAdapter: _execute_agy (agy) and execute (generic/Claude).

    Site 5 (generic/Claude) is the path the inspector proved launched with
    creationflags=None.
    """
    from hermes_cli.fleet.adapters import live_routes
    from hermes_cli.fleet.adapters.live_routes import _AGY_MODEL_LABELS

    exe = tmp_path / "dummy.exe"
    exe.write_text("", encoding="utf-8")

    # The agy path validates request.model against its label table before
    # launching, so use a real key for that lane.
    model = next(iter(_AGY_MODEL_LABELS)) if lane == "antigravity" else "m"

    seen, fake = _recorder()
    monkeypatch.setattr(live_routes, "validate_execution", lambda *a, **k: None, raising=False)

    adapter = live_routes._SubscriptionCliAdapter(str(exe), lane=lane, run_process=fake)
    monkeypatch.setattr(adapter, "_resolved_executable", lambda: Path(exe).resolve(), raising=False)
    monkeypatch.setattr(adapter, "cancelled", lambda: False, raising=False)

    try:
        adapter.execute(_request(tmp_path, model=model), _qualification(exe))
    except Exception:
        pass

    if not seen.get("called"):
        pytest.fail(f"lane={lane}: production execute() never launched a child process")
    assert seen["creationflags"] == EXPECTED, f"lane={lane} launched without console suppression"


# ---------------------------------------------------------------- site 1/3
def test_site_doctor_agy_live_receipt_passes_flag(monkeypatch, tmp_path):
    """FleetQualificationDoctor._probe_agy_live_receipt -> self.run_process.

    Hard guard (no skips): the doctor's bounded print-mode probe must launch its
    child with console suppression. Required by the independent inspector after
    an earlier version of this test skipped instead of asserting.
    """
    from hermes_cli.fleet.live import FleetQualificationDoctor

    seen, fake = _recorder()
    doctor = FleetQualificationDoctor(run_process=fake, proof_cache_dir=tmp_path)

    profile = type(
        "P", (), {"lane_id": "antigravity", "provider_id": "agy", "executable": "agy.exe"}
    )()

    try:
        doctor._probe_agy_live_receipt(
            profile=profile,
            executable=str(tmp_path / "agy.exe"),
            version="1.1.7",
            model_id="gemini-3.1-pro-high",
            display_label="Antigravity",
        )
    except Exception:
        pass  # downstream parsing/receipt failures are irrelevant here

    assert seen.get("called"), "doctor probe never reached run_process"
    assert seen["creationflags"] == EXPECTED, "doctor probe launched without console suppression"
