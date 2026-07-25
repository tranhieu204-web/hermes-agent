from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.skipif(os.name != "nt", reason="Windows environment forwarding contract")
def test_canonical_runner_forwards_nonsecret_windows_runtime_environment(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(tmp_path / "bash-home"),
            "USERPROFILE": str(tmp_path / "user-profile"),
            "LOCALAPPDATA": str(tmp_path / "local-app-data"),
            "APPDATA": str(tmp_path / "roaming-app-data"),
            "HOMEDRIVE": "Q:",
            "HOMEPATH": r"\test-home",
            "PATHEXT": os.environ.get(
                "PATHEXT", ".COM;.EXE;.BAT;.CMD;.VBS;.VBE;.JS;.JSE;.WSF;.WSH;.MSC"
            ),
            "HERMES_PYTHON": os.environ.get("HERMES_PYTHON", os.sys.executable),
        }
    )

    bash_exe = Path(os.environ.get("HERMES_TEST_BASH", r"C:\Program Files\Git\bin\bash.exe"))
    if not bash_exe.exists():
        pytest.skip("Git Bash is not installed at the configured path")

    completed = subprocess.run(
        [
            str(bash_exe),
            "scripts/run_tests.sh",
            "tests/ci/fixtures/windows_runner_env_probe.py",
            "-q",
        ],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
