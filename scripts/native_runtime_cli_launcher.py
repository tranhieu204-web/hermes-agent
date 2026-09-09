from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path


CANONICAL_ROOT = Path(r"C:\SakaanAIAgentWorkspace\Hermes\main")
CANONICAL_HOME = Path(r"C:\Users\HieuKa\.hermes")
CUTOVER_HOLD = Path(
    r"C:\Users\HieuKa\.codex-agent-relay\tasks\hermes-native-runtime-release-20260909\CUTOVER.lock"
)
_SENSITIVE_PREFIX = re.compile(
    r"^(HERMES_|TERMINAL_|PYTHONPATH$|PYTHONHOME$|VIRTUAL_ENV$|CONDA_PREFIX$|"
    r"ELECTRON_RUN_AS_NODE$|NODE_OPTIONS$|AWS_|GOOGLE_APPLICATION_CREDENTIALS$)"
)
_SENSITIVE_SUFFIX = re.compile(
    r"(_API_KEY|_BASE_URL|_TOKEN|_SECRET|_PASSWORD|_CREDENTIALS|_ACCESS_KEY|_PRIVATE_KEY)$"
)


def sanitized_environment(source: dict[str, str]) -> dict[str, str]:
    clean = {
        name: value
        for name, value in source.items()
        if not _SENSITIVE_PREFIX.search(name) and not _SENSITIVE_SUFFIX.search(name)
    }
    clean.update(
        HERMES_HOME=str(CANONICAL_HOME),
        PYTHONPATH=str(CANONICAL_ROOT),
        PYTHONNOUSERSITE="1",
    )
    return clean


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    python = CANONICAL_ROOT / ".venv" / "Scripts" / "python.exe"
    if CUTOVER_HOLD.exists():
        print("Hermes release transition is held; nothing was started.", file=sys.stderr)
        return 78
    if not python.is_file():
        print("Canonical Hermes Python is missing.", file=sys.stderr)
        return 78
    if not (CANONICAL_HOME / "config.yaml").is_file():
        print("Canonical Hermes configuration is missing.", file=sys.stderr)
        return 78
    completed = subprocess.run(
        [str(python), "-m", "hermes_cli.main", *args],
        cwd=CANONICAL_ROOT,
        env=sanitized_environment(dict(os.environ)),
        check=False,
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
