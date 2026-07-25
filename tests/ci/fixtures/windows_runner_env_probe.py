from __future__ import annotations

import os
from pathlib import Path
import sys


def test_canonical_runner_preserves_required_windows_environment() -> None:
    assert os.environ["PYTHONUTF8"] == "1"
    assert os.environ["PYTHONIOENCODING"].lower().replace("_", "-") == "utf-8"
    assert sys.stdout.encoding.lower().replace("_", "-") == "utf-8"
    assert Path.home() == Path(os.environ["USERPROFILE"])
    assert os.environ["LOCALAPPDATA"].endswith("local-app-data")
    assert os.environ["APPDATA"].endswith("roaming-app-data")
    assert Path(os.environ["SYSTEMROOT"]).name.lower() == "windows"
    assert os.environ["HOMEDRIVE"] == "Q:"
    assert os.environ["HOMEPATH"] == r"\test-home"
    assert Path(os.environ["COMSPEC"]).name.lower() == "cmd.exe"
    assert ".exe" in os.environ["PATHEXT"].lower().split(";")
