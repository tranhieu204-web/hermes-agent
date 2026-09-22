import base64
import os
import subprocess
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell 5.1 contract")


def _run(argv, **kwargs):
    return subprocess.run(argv, check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)


def _powershell() -> Path:
    return Path(os.environ["SystemRoot"]) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"


def test_exact_generated_cmd_preserves_arguments_and_exit_code(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    scripts_dir = tmp_path / "Hermes Root With Spaces" / ".venv" / "Scripts"
    scripts_dir.mkdir(parents=True)

    stub_source = tmp_path / "ArgvStub.cs"
    stub_source.write_text(
        """
using System;
using System.IO;
using System.Text;
public static class ArgvStub {
  public static int Main(string[] args) {
    var lines = new string[args.Length];
    for (var i = 0; i < args.Length; i++)
      lines[i] = Convert.ToBase64String(Encoding.UTF8.GetBytes(args[i]));
    File.WriteAllLines(Environment.GetEnvironmentVariable("SAKAAN_CMD_E2E_RECEIPT"), lines);
    Console.Out.Write("stub-out");
    Console.Error.Write("stub-err");
    return Int32.Parse(Environment.GetEnvironmentVariable("SAKAAN_CMD_E2E_EXIT"));
  }
}
""".strip(),
        encoding="utf-8",
    )
    csc = Path(os.environ["SystemRoot"]) / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe"
    if not csc.is_file():
        pytest.skip("Windows C# compiler is unavailable")
    stub = scripts_dir / "python.exe"
    _run([str(csc), "/nologo", f"/out:{stub}", str(stub_source)])

    launcher = tmp_path / "Harmless Launcher With Spaces.py"
    launcher.write_text("# argv stub placeholder\n", encoding="utf-8")

    generated_cmd = tmp_path / "generated hermes.cmd"
    _run([
        str(_powershell()), "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
        str(repo / "scripts" / "render-native-runtime-cmd.ps1"),
        "-PythonExecutable", str(stub), "-CliLauncherSource", str(launcher),
        "-InstalledCliLauncher", str(launcher),
        "-Destination", str(generated_cmd),
    ])
    body = generated_cmd.read_text(encoding="ascii")
    assert f'"{stub}" "{launcher}" %*' in body
    assert " -Cli -- %*" not in body

    cases = [(["--help"], 0), (["--version"], 0),
             (["space value", 'quote"inside', "", "trailing\\"], 23)]
    for index, (forwarded, expected_exit) in enumerate(cases):
        receipt = tmp_path / f"stub-args-{index}.txt"
        env = os.environ.copy()
        env["SAKAAN_CMD_E2E_RECEIPT"] = str(receipt)
        env["SAKAAN_CMD_E2E_EXIT"] = str(expected_exit)
        result = subprocess.run(
            [str(generated_cmd), *forwarded], shell=True, cwd=tmp_path, env=env,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        assert result.returncode == expected_exit, result.stderr
        assert result.stdout == "stub-out"
        assert result.stderr == "stub-err"
        encoded = receipt.read_text(encoding="utf-8-sig").splitlines()
        observed = [base64.b64decode(item).decode("utf-8") for item in encoded]
        assert observed == [str(launcher), *forwarded]
