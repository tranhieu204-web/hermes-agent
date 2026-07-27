"""End-to-end guards for importance -> reasoning-effort wiring.

The grading table shipped once already and was INERT: nothing in production
passed ``importance``, so every dispatch resolved at the lane default. These
tests bind to the real production chain —

    kanban task.importance
      -> _default_spawn exports HERMES_TASK_IMPORTANCE
      -> resolve_reasoning_config reads it
      -> resolve_effort_from_map grades the lane

— so that removing any link fails a test rather than silently going quiet again.
"""

import os

import pytest

from gateway.fleet_safety.selector import (
    TASK_IMPORTANCE_ENV,
    normalize_importance,
    resolve_task_importance,
    shadowed_graded_lanes,
)
from hermes_constants import resolve_reasoning_config


# Operator's live config shape WITHOUT the graded-lane pins, so grading governs.
UNSHADOWED_CFG = {
    "agent": {
        "reasoning_effort": {"grok": "high", "antigravity": "high", "default": "medium"}
    }
}

FOUR_TIERS = [
    ("normal", "medium"),
    ("semi_critical", "high"),
    ("critically_important", "xhigh"),
    ("money_critical", "max"),
]


def _effort(cfg, model):
    result = resolve_reasoning_config(cfg, model)
    return result.get("effort") if isinstance(result, dict) else result


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(TASK_IMPORTANCE_ENV, raising=False)


# --------------------------------------------------------------- normalization
def test_absent_importance_is_empty_not_normal():
    """Blank/unknown must NOT become "normal".

    DEFAULT_LANE_EFFORTS is "xhigh" for the graded lanes, so mapping an untagged
    task to "normal" would silently downgrade every existing dispatch
    xhigh -> medium. Absent means ungraded.
    """
    for value in ("", None, "   ", "criticaly_important", "urgent", "P0"):
        assert normalize_importance(value) == ""


@pytest.mark.parametrize("raw", ["MONEY_CRITICAL", " money-critical ", "Money Critical"])
def test_importance_spelling_tolerance(raw):
    assert normalize_importance(raw) == "money_critical"


def test_precedence_explicit_over_env_over_default(monkeypatch):
    monkeypatch.setenv(TASK_IMPORTANCE_ENV, "semi_critical")
    assert resolve_task_importance("money_critical", default_importance="normal") == "money_critical"
    assert resolve_task_importance("", default_importance="normal") == "semi_critical"
    monkeypatch.delenv(TASK_IMPORTANCE_ENV)
    assert resolve_task_importance("", default_importance="normal") == "normal"
    assert resolve_task_importance("") == ""


# ------------------------------------------------------------ end-to-end grade
@pytest.mark.parametrize("importance,expected", FOUR_TIERS)
def test_env_importance_grades_claude_lane(monkeypatch, importance, expected):
    """The whole chain, driven exactly as a dispatched worker sees it."""
    monkeypatch.setenv(TASK_IMPORTANCE_ENV, importance)
    assert _effort(UNSHADOWED_CFG, "claude-opus-5") == expected


@pytest.mark.parametrize("importance,expected", FOUR_TIERS)
def test_env_importance_grades_codex_lane(monkeypatch, importance, expected):
    monkeypatch.setenv(TASK_IMPORTANCE_ENV, importance)
    assert _effort(UNSHADOWED_CFG, "gpt-5.6-sol") == expected


def test_untagged_task_keeps_lane_default_no_regression():
    """ANTI-REGRESSION: an untagged dispatch must still resolve xhigh."""
    assert _effort(UNSHADOWED_CFG, "claude-opus-5") == "xhigh"


@pytest.mark.parametrize("importance", ["normal", "money_critical"])
def test_pinned_lanes_ignore_importance(monkeypatch, importance):
    """Chairman rule: Grok and Antigravity are always high, never graded."""
    monkeypatch.setenv(TASK_IMPORTANCE_ENV, importance)
    assert _effort(UNSHADOWED_CFG, "grok-4") == "high"
    assert _effort(UNSHADOWED_CFG, "gemini-3.1-pro-high") == "high"


# ------------------------------------------------------------------- shadowing
def test_shadowed_graded_lanes_detects_config_pins():
    """The exact defect that made grading unreachable in the live config."""
    assert shadowed_graded_lanes(
        {"claude_code": "xhigh", "chatgpt_codex": "xhigh", "default": "medium"}
    ) == ["chatgpt_codex", "claude_code"]
    assert shadowed_graded_lanes({"grok": "high", "default": "medium"}) == []
    assert shadowed_graded_lanes("medium") == []


def test_config_pin_actually_shadows_grading(monkeypatch):
    """Documents the real precedence: an explicit lane pin beats importance.

    This is why wiring alone was not enough — it is asserted so nobody
    "fixes" the wiring later without noticing the config still wins.
    """
    monkeypatch.setenv(TASK_IMPORTANCE_ENV, "normal")
    shadowed = {"agent": {"reasoning_effort": {"claude_code": "xhigh", "default": "medium"}}}
    assert _effort(shadowed, "claude-opus-5") == "xhigh"


# ------------------------------------------------- operator default (2026-07-27)
# Operator decision: untagged work is normal work. `agent.default_importance:
# normal` makes an untagged dispatch grade to medium instead of inheriting the
# xhigh lane default. This is the live production shape, so it gets a guard.
DEFAULT_NORMAL_CFG = {
    "agent": {
        "default_importance": "normal",
        "reasoning_effort": {"grok": "high", "antigravity": "high", "default": "medium"},
    }
}


def test_default_importance_normal_grades_untagged_to_medium():
    assert _effort(DEFAULT_NORMAL_CFG, "claude-opus-5") == "medium"
    assert _effort(DEFAULT_NORMAL_CFG, "gpt-5.6-sol") == "medium"


def test_default_importance_does_not_override_an_explicit_tag(monkeypatch):
    monkeypatch.setenv(TASK_IMPORTANCE_ENV, "money_critical")
    assert _effort(DEFAULT_NORMAL_CFG, "claude-opus-5") == "max"


def test_default_importance_never_lifts_pinned_lanes():
    assert _effort(DEFAULT_NORMAL_CFG, "grok-4") == "high"
    assert _effort(DEFAULT_NORMAL_CFG, "gemini-3.1-pro-high") == "high"
