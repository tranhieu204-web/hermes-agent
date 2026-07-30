import os
from pathlib import Path
import shutil
import subprocess

import pytest


REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "scripts" / "fleet_consult_cli.ps1"
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


pytestmark = pytest.mark.skipif(
    POWERSHELL is None,
    reason="PowerShell is required to exercise the consultation wrapper",
)


def _write_fake_cli(directory: Path, name: str) -> None:
    executable = directory / f"{name}.ps1"
    executable.write_text(
        "param(\n"
        "    [Parameter(ValueFromRemainingArguments = $true)]\n"
        "    [string[]]$RemainingArguments\n"
        ")\n"
        "[System.IO.File]::WriteAllLines(\n"
        "    $env:CAPTURE_FILE,\n"
        "    $RemainingArguments\n"
        ")\n"
        "Write-Output $env:FAKE_RESPONSE\n"
        "exit [int]$env:FAKE_EXIT_CODE\n",
        encoding="utf-8",
    )


def _invoke(
    tmp_path: Path,
    agent: str,
    *,
    response: str = "APPROVE",
    exit_code: int = 0,
) -> tuple[subprocess.CompletedProcess[str], str]:
    capture = tmp_path / "arguments.txt"
    command_name = agent.lower()
    _write_fake_cli(tmp_path, command_name)
    environment = os.environ.copy()
    environment.update(
        {
            "CAPTURE_FILE": str(capture),
            "FAKE_EXIT_CODE": str(exit_code),
            "FAKE_RESPONSE": response,
            "PATH": f"{tmp_path}{os.pathsep}{environment.get('PATH', '')}",
        }
    )
    result = subprocess.run(
        [
            str(POWERSHELL),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SCRIPT),
            "-Agent",
            agent,
            "-Prompt",
            "bounded consultation",
            "-RequireVerdict",
        ],
        cwd=REPOSITORY,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    arguments = capture.read_text(encoding="utf-8").strip() if capture.exists() else ""
    return result, arguments


def test_hermes_uses_explicit_verified_route(tmp_path: Path) -> None:
    result, arguments = _invoke(tmp_path, "Hermes")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "APPROVE"
    assert "--ignore-rules" in arguments
    assert "--provider\nopenai-codex" in arguments
    assert "--model\ngpt-5.6-sol" in arguments
    assert "--oneshot\nbounded consultation" in arguments


def test_claude_uses_first_party_safe_cli_boundary(tmp_path: Path) -> None:
    result, arguments = _invoke(tmp_path, "Claude")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "APPROVE"
    assert "-p" in arguments
    assert "--safe-mode" in arguments
    assert "--permission-mode\ndontAsk" in arguments
    assert "--tools=" in arguments
    assert "--no-session-persistence" in arguments
    assert "--system-prompt" in arguments


def test_exit_zero_without_verdict_fails_closed(tmp_path: Path) -> None:
    result, _ = _invoke(tmp_path, "Hermes", response="No technical verdict.")

    assert result.returncode != 0
    assert "without an explicit verdict" in result.stderr


def test_cli_failure_is_not_reported_as_a_valid_consultation(tmp_path: Path) -> None:
    result, _ = _invoke(tmp_path, "Claude", exit_code=17)

    assert result.returncode != 0
    assert "claude exited with code 17" in result.stderr
