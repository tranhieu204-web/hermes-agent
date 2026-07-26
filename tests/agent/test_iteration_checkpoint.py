"""RED-first contracts for renewable iteration checkpoints."""

from types import SimpleNamespace

import pytest

from agent.iteration_budget import IterationBudget


def _agent(*, no_progress_streak=0, quality="unknown"):
    statuses = []
    telemetry = SimpleNamespace(
        get_activity_snapshot=lambda: {
            "no_progress_streak": no_progress_streak,
            "progress_seq": 0,
            "attempt_seq": no_progress_streak,
            "last_progress_kind": None,
            "usage": {
                "input_tokens": 11,
                "output_tokens": 7,
                "cache_read_tokens": 13,
                "cache_write_tokens": 5,
                "reasoning_tokens": 3,
                "cost": 0.25,
                "quality": quality,
                "cost_status": "estimated",
                "cost_source": "test",
            },
        }
    )
    budget = IterationBudget(2)
    assert budget.consume() is True
    assert budget.consume() is True
    return SimpleNamespace(
        max_iterations=2,
        iteration_budget=budget,
        _interrupt_requested=False,
        _progress_telemetry=telemetry,
        _emit_status=statuses.append,
        statuses=statuses,
    )


def test_checkpoint_extends_unknown_work_and_emits_plain_status():
    from agent.conversation_loop import _handle_iteration_checkpoint

    agent = _agent(no_progress_streak=0, quality="unknown")
    action = _handle_iteration_checkpoint(
        agent,
        api_call_count=2,
        block_size=2,
        no_progress_limit=3,
    )

    assert action == "extend"
    assert agent.max_iterations == 4
    assert agent.iteration_budget.max_total == 4
    assert agent.iteration_budget.remaining == 2
    assert agent.iteration_budget.extensions_count == 1
    assert len(agent.statuses) == 1
    status = agent.statuses[0]
    assert "Extension checkpoint" in status
    assert "Progress: unknown" in status
    assert "Usage provenance: unknown" in status
    assert "Input tokens: 11" in status
    assert "Output tokens: 7" in status
    assert "Cache read tokens: 13" in status
    assert "Cache write tokens: 5" in status
    assert "Reasoning tokens: 3" in status
    assert "Cost: 0.250000 (estimated; test)" in status
    assert "Continuing by default. Send STOP or /stop to cancel." in status
    assert "<br>" not in status
    assert "&nbsp;" not in status


def test_checkpoint_renews_again_at_next_expiry_without_approval():
    from agent.conversation_loop import _handle_iteration_checkpoint

    agent = _agent()
    assert _handle_iteration_checkpoint(agent, 2, 2, 3) == "extend"
    for _ in range(2):
        assert agent.iteration_budget.consume() is True
    assert _handle_iteration_checkpoint(agent, 4, 2, 3) == "extend"
    assert agent.max_iterations == 6
    assert agent.iteration_budget.max_total == 6
    assert agent.iteration_budget.extensions_count == 2
    assert len(agent.statuses) == 2


def test_checkpoint_verified_no_progress_stops_without_grant():
    from agent.conversation_loop import _handle_iteration_checkpoint

    agent = _agent(no_progress_streak=3, quality="measured")
    action = _handle_iteration_checkpoint(agent, 2, 2, 3)

    assert action == "stop_verified_no_progress"
    assert agent.max_iterations == 2
    assert agent.iteration_budget.max_total == 2
    assert agent.iteration_budget.extensions_count == 0
    assert len(agent.statuses) == 1
    assert "Safety stop" in agent.statuses[0]
    assert "verified no progress" in agent.statuses[0].lower()
    assert "before another provider call" in agent.statuses[0].lower()
    assert "no summary call will run" in agent.statuses[0].lower()


def test_checkpoint_interrupt_wins_before_extension():
    from agent.conversation_loop import _handle_iteration_checkpoint

    agent = _agent()
    agent._interrupt_requested = True
    action = _handle_iteration_checkpoint(agent, 2, 2, 3)

    assert action == "stop_interrupted"
    assert agent.max_iterations == 2
    assert agent.iteration_budget.max_total == 2
    assert agent.statuses == []


def test_checkpoint_does_not_stop_for_resource_volume_or_unknown_progress():
    from agent.conversation_loop import _handle_iteration_checkpoint

    agent = _agent(no_progress_streak=0, quality="estimated")
    agent._progress_telemetry.get_activity_snapshot = lambda: {
        "no_progress_streak": 0,
        "attempt_seq": 1000,
        "progress_seq": 0,
        "usage": {
            "input_tokens": 10_000_000,
            "output_tokens": 1_000_000,
            "cache_read_tokens": 50_000_000,
            "cache_write_tokens": 0,
            "reasoning_tokens": 1_000_000,
            "cost": 999.0,
            "quality": "estimated",
            "cost_status": "estimated",
            "cost_source": "test",
        },
    }
    assert _handle_iteration_checkpoint(agent, 1000, 2, 3) == "extend"


def test_iteration_budget_rejects_nonpositive_grant():
    budget = IterationBudget(2)
    with pytest.raises(ValueError):
        budget.extend_grant(0)
