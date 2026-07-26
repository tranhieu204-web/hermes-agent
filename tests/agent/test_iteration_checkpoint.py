"""Outer conversation-loop iteration checkpoint contracts.

These tests deliberately exercise ``AIAgent.run_conversation`` for provider-call
continuation.  The lower-level budget tests cover only the atomic race/replay
invariants that cannot be made deterministic through a single conversation.
"""

from __future__ import annotations

import copy
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.iteration_budget import (
    IterationBudget,
    IterationCheckpointOutcome,
)
from run_agent import AIAgent


def _tool_defs(*names: str) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": "deterministic checkpoint probe",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for name in names
    ]


def _tool_call(call_id: str, arguments: str = "{}") -> SimpleNamespace:
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name="checkpoint_probe", arguments=arguments),
    )


def _response(
    *,
    content: str = "",
    finish_reason: str = "tool_calls",
    tool_calls: list[SimpleNamespace] | None = None,
) -> SimpleNamespace:
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=message, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], model="test/model", usage=None)


def _make_agent(*, max_iterations: int = 2) -> AIAgent:
    hermes_home = Path(tempfile.mkdtemp(prefix="hermes-iteration-checkpoint-"))
    (hermes_home / "logs").mkdir(parents=True, exist_ok=True)
    with (
        patch(
            "run_agent.get_tool_definitions",
            return_value=_tool_defs("checkpoint_probe"),
        ),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch("run_agent._hermes_home", hermes_home),
        patch("agent.model_metadata.fetch_model_metadata", return_value={}),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            max_iterations=max_iterations,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            session_id="iteration-checkpoint-session",
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "Stable cached system prefix."
    agent._use_prompt_caching = False
    agent.valid_tool_names = {"checkpoint_probe"}
    agent.tool_delay = 0
    agent.compression_enabled = False
    agent.save_trajectories = False
    return agent


def _run(agent: AIAgent, *, tool_effect) -> dict:
    with (
        patch("run_agent.handle_function_call", side_effect=tool_effect),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        return agent.run_conversation("complete the checkpoint task")


def _checkpoint_receipts(result: dict) -> list[dict]:
    return result["iteration_checkpoint_receipts"]


def test_productive_terminal_progress_renews_real_outer_provider_loop():
    """Budget 2 + verified terminal progress reaches provider call 3.

    The captured provider payloads also pin cache-prefix/context preservation:
    renewal is accounting-only and never appends a synthetic user message.
    """

    agent = _make_agent()
    agent.client.chat.completions.create.side_effect = [
        _response(tool_calls=[_tool_call("c1", '{"step": 1}')]),
        _response(tool_calls=[_tool_call("c2", '{"step": 2}')]),
        _response(content="completed after verified progress", finish_reason="stop"),
    ]
    tool_calls = 0

    def _productive_tool(*_args, **_kwargs):
        nonlocal tool_calls
        tool_calls += 1
        if tool_calls == 1:
            # An authoritative producer recorded real, terminal progress.  The
            # generic tool observer may subsequently record UNKNOWN; the
            # monotonic progress sequence must still be sufficient evidence.
            agent._progress_telemetry.record_attempt_completion(
                "artifact_create",
                {"path": "artifact.txt"},
                {"created": "artifact.txt"},
                is_failure=False,
                event_id="productive-terminal-event-1",
                call_id="artifact:create",
                adapter="checkpoint-test",
                source="productive-terminal-producer",
                session_id=agent.session_id,
                verified_progress=True,
            )
        return f"probe-result-{tool_calls}"

    result = _run(agent, tool_effect=_productive_tool)

    assert result["api_calls"] == 3
    assert result["final_response"] == "completed after verified progress"
    receipts = _checkpoint_receipts(result)
    assert [receipt["outcome"] for receipt in receipts] == [
        IterationCheckpointOutcome.EXTENDED.value
    ]
    assert receipts[0]["evidence"] == "verified_progress"
    assert receipts[0]["progress_sequence"] == 1
    assert receipts[0]["session_id"] == agent.session_id
    assert receipts[0]["generation"] == 1
    assert receipts[0]["extension_calls"] == 1
    assert receipts[0]["used"] == 2
    assert receipts[0]["max_total"] == 3
    assert receipts[0]["hard_max_total"] == 4

    provider_payloads = [
        copy.deepcopy(call.kwargs["messages"])
        for call in agent.client.chat.completions.create.call_args_list
    ]
    assert len(provider_payloads) == 3
    for earlier, later in zip(provider_payloads, provider_payloads[1:]):
        assert later[: len(earlier)] == earlier
    assert [message["role"] for message in provider_payloads[2]] == [
        "system",
        "user",
        "assistant",
        "tool",
        "assistant",
        "tool",
    ]
    assert provider_payloads[2][0]["content"] == "Stable cached system prefix."
    assert [
        message["content"]
        for message in provider_payloads[2]
        if message["role"] == "user"
    ] == ["complete the checkpoint task"]


def test_unknown_terminal_work_gets_bounded_extension_without_progress_label():
    agent = _make_agent()
    agent.client.chat.completions.create.side_effect = [
        _response(tool_calls=[_tool_call("c1", '{"query": 1}')]),
        _response(tool_calls=[_tool_call("c2", '{"query": 2}')]),
        _response(content="completed after unknown work", finish_reason="stop"),
    ]
    counter = 0

    def _unknown_tool(*_args, **_kwargs):
        nonlocal counter
        counter += 1
        return f"distinct-result-{counter}"

    result = _run(agent, tool_effect=_unknown_tool)

    assert result["api_calls"] == 3
    assert result["final_response"] == "completed after unknown work"
    receipts = _checkpoint_receipts(result)
    assert [receipt["outcome"] for receipt in receipts] == [
        IterationCheckpointOutcome.EXTENDED.value
    ]
    assert receipts[0]["evidence"] == "unknown_terminal_event"
    assert receipts[0]["progress_sequence"] == 0
    assert receipts[0]["event_sequence"] >= 2


def test_verified_no_progress_denies_extension_before_provider_call_three():
    agent = _make_agent()
    agent._iteration_no_progress_limit = 1
    agent.client.chat.completions.create.side_effect = [
        _response(tool_calls=[_tool_call("c1", '{"poll": true}')]),
        _response(tool_calls=[_tool_call("c2", '{"poll": true}')]),
        _response(content="must not be called", finish_reason="stop"),
    ]

    with patch.object(
        agent,
        "_handle_max_iterations",
        side_effect=AssertionError("no-progress is not budget exhaustion"),
    ):
        result = _run(agent, tool_effect=lambda *_args, **_kwargs: "same-result")

    assert agent.client.chat.completions.create.call_count == 2
    assert result["api_calls"] == 2
    assert result["turn_exit_reason"] == "iteration_checkpoint_no_progress"
    assert result["completed"] is False
    receipts = _checkpoint_receipts(result)
    assert [receipt["outcome"] for receipt in receipts] == [
        IterationCheckpointOutcome.DENIED_VERIFIED_NO_PROGRESS.value
    ]
    assert receipts[0]["granted"] is False
    assert receipts[0]["no_progress_streak"] >= 1
    assert receipts[0]["claimed_side_effects"] == []
    assert agent.iteration_budget.extension_calls == 0
    assert agent.iteration_budget.max_total == 2


def test_hard_interrupt_wins_before_iteration_extension():
    """The existing STOP interrupt boundary runs before checkpoint renewal."""

    agent = _make_agent()
    agent.client.chat.completions.create.side_effect = [
        _response(tool_calls=[_tool_call("c1", '{"step": 1}')]),
        _response(tool_calls=[_tool_call("c2", '{"step": 2}')]),
        _response(content="must not be called", finish_reason="stop"),
    ]
    counter = 0

    def _interrupt_after_final_slot(*_args, **_kwargs):
        nonlocal counter
        counter += 1
        if counter == 2:
            agent.interrupt("Stop requested")
        return f"result-{counter}"

    result = _run(agent, tool_effect=_interrupt_after_final_slot)

    assert agent.client.chat.completions.create.call_count == 2
    assert result["interrupted"] is True
    assert result["turn_exit_reason"] == "interrupted_by_user"
    assert _checkpoint_receipts(result) == []
    assert agent.iteration_budget.extension_calls == 0
    assert agent.iteration_budget.max_total == 2


def test_shared_final_slot_and_checkpoint_are_single_owner_single_grant():
    budget = IterationBudget(2)
    assert budget.consume(owner_id="seed", expected_generation=0)

    barrier = threading.Barrier(2)
    claims: dict[str, bool] = {}

    def _claim(owner_id: str) -> None:
        barrier.wait(timeout=5)
        claims[owner_id] = budget.consume(
            owner_id=owner_id,
            expected_generation=0,
        )

    threads = [
        threading.Thread(target=_claim, args=(owner_id,))
        for owner_id in ("runner-a", "runner-b")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    winners = [owner_id for owner_id, claimed in claims.items() if claimed]
    losers = [owner_id for owner_id, claimed in claims.items() if not claimed]
    assert len(winners) == 1
    assert len(losers) == 1
    winner, loser = winners[0], losers[0]

    race_receipt = budget.checkpoint(
        owner_id=loser,
        expected_generation=0,
        session_id="session-1",
        evidence_session_id="session-1",
        event_sequence=1,
        progress_sequence=0,
        no_progress_streak=0,
        no_progress_limit=1,
    )
    assert race_receipt.outcome is IterationCheckpointOutcome.RACE_LOST
    assert race_receipt.granted is False
    assert budget.max_total == 2
    assert budget.extension_calls == 0

    grant = budget.checkpoint(
        owner_id=winner,
        expected_generation=0,
        session_id="session-1",
        evidence_session_id="session-1",
        event_sequence=1,
        progress_sequence=0,
        no_progress_streak=0,
        no_progress_limit=1,
    )
    assert grant.outcome is IterationCheckpointOutcome.EXTENDED
    assert grant.granted is True
    assert grant.generation == 1
    assert grant.owner_id == winner
    assert budget.max_total == 3

    assert budget.consume(owner_id=winner, expected_generation=1)
    checkpoint_barrier = threading.Barrier(2)
    checkpoint_receipts = []
    receipt_lock = threading.Lock()

    def _checkpoint() -> None:
        checkpoint_barrier.wait(timeout=5)
        receipt = budget.checkpoint(
            owner_id=winner,
            expected_generation=1,
            session_id="session-1",
            evidence_session_id="session-1",
            event_sequence=2,
            progress_sequence=0,
            no_progress_streak=0,
            no_progress_limit=1,
        )
        with receipt_lock:
            checkpoint_receipts.append(receipt)

    threads = [threading.Thread(target=_checkpoint) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert sum(receipt.granted for receipt in checkpoint_receipts) == 1
    assert budget.extension_calls == 2
    assert budget.max_total == 4


def test_checkpoint_requires_new_event_and_has_hard_extension_ceiling():
    budget = IterationBudget(2)
    owner = "outer-loop-owner"
    assert budget.consume(owner_id=owner, expected_generation=0)
    assert budget.consume(owner_id=owner, expected_generation=0)

    first = budget.checkpoint(
        owner_id=owner,
        expected_generation=0,
        session_id="session-1",
        evidence_session_id="session-1",
        event_sequence=7,
        progress_sequence=0,
        no_progress_streak=0,
        no_progress_limit=1,
    )
    assert first.outcome is IterationCheckpointOutcome.EXTENDED
    assert budget.consume(owner_id=owner, expected_generation=1)

    replay = budget.checkpoint(
        owner_id=owner,
        expected_generation=1,
        session_id="session-1",
        evidence_session_id="session-1",
        event_sequence=7,
        progress_sequence=0,
        no_progress_streak=0,
        no_progress_limit=1,
    )
    assert replay.outcome is IterationCheckpointOutcome.DENIED_NO_NEW_EVIDENCE
    assert replay.granted is False
    assert budget.extension_calls == 1
    assert budget.max_total == 3

    second = budget.checkpoint(
        owner_id=owner,
        expected_generation=1,
        session_id="session-1",
        evidence_session_id="session-1",
        event_sequence=8,
        progress_sequence=0,
        no_progress_streak=0,
        no_progress_limit=1,
    )
    assert second.outcome is IterationCheckpointOutcome.EXTENDED
    assert budget.consume(owner_id=owner, expected_generation=2)

    ceiling = budget.checkpoint(
        owner_id=owner,
        expected_generation=2,
        session_id="session-1",
        evidence_session_id="session-1",
        event_sequence=9,
        progress_sequence=1,
        no_progress_streak=0,
        no_progress_limit=1,
    )
    assert ceiling.outcome is IterationCheckpointOutcome.DENIED_EXTENSION_LIMIT
    assert ceiling.granted is False
    assert budget.extension_calls == budget.extension_limit == 2
    assert budget.max_total == budget.hard_max_total == 4
