from pathlib import Path
from types import SimpleNamespace

from scripts import native_runtime_cli_launcher as launcher


def test_sanitized_environment_removes_credential_and_runtime_overrides():
    source = {
        "PATH": "kept",
        "OPENAI_API_KEY": "remove",
        "XAI_TOKEN": "remove",
        "HERMES_HOME": "remove",
        "PYTHONPATH": "remove",
        "NODE_OPTIONS": "remove",
        "UNRELATED": "kept",
    }
    clean = launcher.sanitized_environment(source)
    assert clean["PATH"] == "kept"
    assert clean["UNRELATED"] == "kept"
    assert clean["HERMES_HOME"] == str(launcher.CANONICAL_HOME)
    assert clean["PYTHONPATH"] == str(launcher.CANONICAL_ROOT)
    assert clean["PYTHONNOUSERSITE"] == "1"
    for name in ("OPENAI_API_KEY", "XAI_TOKEN", "NODE_OPTIONS"):
        assert name not in clean


def test_main_preserves_exact_args_and_child_exit(tmp_path, monkeypatch):
    root = tmp_path / "root"
    python = root / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"stub")
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("model: stub\n", encoding="utf-8")
    monkeypatch.setattr(launcher, "CANONICAL_ROOT", root)
    monkeypatch.setattr(launcher, "CANONICAL_HOME", home)
    monkeypatch.setattr(launcher, "CUTOVER_HOLD", tmp_path / "absent.lock")
    observed = {}

    def fake_run(argv, **kwargs):
        observed["argv"] = argv
        observed["kwargs"] = kwargs
        return SimpleNamespace(returncode=37)

    monkeypatch.setattr(launcher.subprocess, "run", fake_run)
    args = ["--help", "space value", 'quote"inside', "", "trailing\\"]
    assert launcher.main(args) == 37
    assert observed["argv"] == [str(python), "-m", "hermes_cli.main", *args]
    assert observed["kwargs"]["cwd"] == root
    assert observed["kwargs"]["check"] is False


def test_main_fails_closed_under_cutover_hold(tmp_path, monkeypatch):
    hold = tmp_path / "CUTOVER.lock"
    hold.write_text("held", encoding="utf-8")
    monkeypatch.setattr(launcher, "CUTOVER_HOLD", hold)
    invoked = False

    def forbidden(*args, **kwargs):
        nonlocal invoked
        invoked = True
        raise AssertionError("child must not start under hold")

    monkeypatch.setattr(launcher.subprocess, "run", forbidden)
    assert launcher.main(["--help"]) == 78
    assert not invoked
