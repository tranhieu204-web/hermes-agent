"""Regression guard for the narrow, explicit Accelerator fork-CI route."""

from pathlib import Path

from ruamel.yaml import YAML


WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
REQUIRED_BRANCHES = {
    "main",
    "feat/workflow-accelerator-20260730",
    "feat/workflow-accelerator-sakaan-20260730",
}


def test_ci_push_trigger_is_the_closed_required_branch_allowlist():
    workflow = YAML(typ="safe").load(WORKFLOW.read_text(encoding="utf-8"))
    triggers = workflow["on"]
    assert set(triggers) == {"pull_request", "push"}
    assert set(triggers["push"]) == {"branches"}
    assert set(triggers["push"]["branches"]) == REQUIRED_BRANCHES
