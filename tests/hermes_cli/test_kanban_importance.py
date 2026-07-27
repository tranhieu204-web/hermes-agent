"""Kanban side of the importance wiring: storage, validation, dispatch export.

Binds to production ``kanban_db`` functions. The dispatcher test asserts on the
env dict that the REAL ``_default_spawn`` hands to ``subprocess.Popen`` — if the
export line is deleted, this fails.
"""

from pathlib import Path

import pytest

from gateway.fleet_safety.selector import TASK_IMPORTANCE_ENV
from hermes_cli import kanban_db as kb


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    """Isolated board, built the way the existing kanban tests build one."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kb.connect() as c:
        yield c


# ------------------------------------------------------------------- storage
@pytest.mark.parametrize(
    "importance",
    ["ultra", "critically_important", "semi_critical", "normal"],
)
def test_importance_round_trips(conn, importance):
    task_id = kb.create_task(conn, title="t", assignee="claude", importance=importance)
    assert kb.get_task(conn, task_id).importance == importance


def test_importance_defaults_to_none_ungraded(conn):
    task_id = kb.create_task(conn, title="t", assignee="claude")
    assert kb.get_task(conn, task_id).importance is None


def test_typo_importance_raises_not_silently_ungraded(conn):
    """A typo must fail loudly — silently storing NULL would look like it worked."""
    with pytest.raises(ValueError) as exc:
        kb.create_task(conn, title="t", assignee="claude", importance="criticaly_important")
    assert "criticaly_important" in str(exc.value)


def test_normalizer_accepts_spelling_variants():
    assert kb._normalized_task_importance(" Ultra ") == "ultra"
    # retired spelling still accepted, canonicalized to the new name
    assert kb._normalized_task_importance("money_critical") == "ultra"
    assert kb._normalized_task_importance("") is None
    assert kb._normalized_task_importance(None) is None


# ------------------------------------------------------------------ dispatch
def _spawn_capturing_env(monkeypatch, tmp_path, importance):
    """Run the REAL _default_spawn and return the env it launches with."""
    seen = {}

    class _P:
        pid = 4242

    def _fake_popen(cmd, **kwargs):
        seen["env"] = kwargs.get("env") or {}
        return _P()

    import subprocess

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(
        kb, "_resolve_hermes_argv", lambda: ["hermes"], raising=False
    )

    task = kb.Task(
        id="t_test",
        title="x",
        body="",
        assignee="claude",
        status="running",
        priority=0,
        created_by="test",
        created_at=0,
        started_at=None,
        completed_at=None,
        workspace_kind="scratch",
        workspace_path=str(tmp_path),
        claim_lock=None,
        claim_expires=None,
        tenant=None,
        importance=importance,
    )
    try:
        kb._default_spawn(task, str(tmp_path))
    except Exception:
        pass
    return seen.get("env")


def test_dispatch_exports_importance(monkeypatch, tmp_path):
    env = _spawn_capturing_env(monkeypatch, tmp_path, "ultra")
    assert env is not None, "production _default_spawn never reached Popen"
    assert env.get(TASK_IMPORTANCE_ENV) == "ultra"


def test_dispatch_omits_importance_when_ungraded(monkeypatch, tmp_path):
    """An ungraded task must not inherit a stale importance from the dispatcher."""
    monkeypatch.setenv(TASK_IMPORTANCE_ENV, "ultra")
    env = _spawn_capturing_env(monkeypatch, tmp_path, None)
    assert env is not None, "production _default_spawn never reached Popen"
    assert TASK_IMPORTANCE_ENV not in env
