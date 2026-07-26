"""Behavioral usage-provenance tests at real model response seams.

All provider traffic is faked.  These tests exercise ``AIAgent.run_conversation``
for OpenAI-wire, Anthropic Messages, Codex Responses, and Codex app-server
responses rather than testing a receipt-construction helper in isolation.
"""

from __future__ import annotations

import socket
import sys
import types
from types import SimpleNamespace
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _fail_fast_on_network(monkeypatch):
    """Make every producer test prove that its fake stays fully offline."""

    attempts = []

    def fail_network(*args, **kwargs):
        attempts.append((args, kwargs))
        pytest.fail(f"unexpected network attempt: {args!r} {kwargs!r}")

    monkeypatch.setattr(socket.socket, "connect", fail_network)
    monkeypatch.setattr(socket.socket, "connect_ex", fail_network)
    monkeypatch.setattr(socket, "create_connection", fail_network)
    monkeypatch.setattr(socket, "getaddrinfo", fail_network)
    monkeypatch.setattr(socket, "gethostbyname", fail_network)
    monkeypatch.setattr(socket, "gethostbyname_ex", fail_network)
    yield attempts
    assert attempts == [], f"producer test attempted network: {attempts!r}"

sys.modules.setdefault("fire", types.SimpleNamespace(Fire=lambda *a, **k: None))
sys.modules.setdefault("firecrawl", types.SimpleNamespace(Firecrawl=object))
sys.modules.setdefault("fal_client", types.SimpleNamespace())

import run_agent
from agent.iteration_budget import IterationBudget
from agent.progress_telemetry import ProgressTelemetry
from agent.transports.codex_app_server_session import TurnResult
from agent.usage_provenance import UsageProvenance


class _OfflineCompressor:
    awaiting_real_usage_after_compression = False
    threshold_tokens = 1_000_000
    context_length = 1_000_000
    _context_probed = False

    def should_defer_preflight_to_real_usage(self, _tokens):
        return False

    def get_active_compression_failure_cooldown(self):
        return None

    def should_compress(self, _tokens):
        return False

    def update_from_response(self, usage):
        self.last_usage = dict(usage)


class _OfflineTransport:
    def __init__(self, api_mode):
        self.api_mode = api_mode

    def preflight_kwargs(self, kwargs, **_ignored):
        return kwargs

    def validate_response(self, response):
        return response is not None

    def map_finish_reason(self, reason):
        return "stop" if reason == "end_turn" else str(reason or "stop")

    def normalize_response(self, response, **_ignored):
        if self.api_mode == "anthropic_messages":
            content = "".join(
                str(getattr(block, "text", "") or "")
                for block in response.content
                if getattr(block, "type", "") == "text"
            )
            finish_reason = self.map_finish_reason(response.stop_reason)
        elif self.api_mode == "codex_responses":
            content = "".join(
                str(getattr(part, "text", "") or "")
                for item in response.output
                if getattr(item, "type", "") == "message"
                for part in getattr(item, "content", ())
                if getattr(part, "type", "") == "output_text"
            )
            finish_reason = "stop"
        else:
            choice = response.choices[0]
            content = choice.message.content
            finish_reason = choice.finish_reason
        return SimpleNamespace(
            content=content,
            tool_calls=[],
            finish_reason=finish_reason,
            reasoning=None,
            reasoning_content=None,
            reasoning_details=None,
        )


def _make_loop_agent(monkeypatch, *, api_mode, provider, response):
    """Build only a ``__new__``/DI fake; never run provider initialization."""

    turn_counter = {"value": 0}

    def fake_turn_context(_agent, message, *_args, **_kwargs):
        turn_counter["value"] += 1
        return SimpleNamespace(
            user_message=message,
            original_user_message=message,
            messages=[{"role": "user", "content": message}],
            conversation_history=None,
            active_system_prompt="",
            effective_task_id=f"offline-task-{turn_counter['value']}",
            turn_id=f"offline-turn-{turn_counter['value']}",
            current_turn_user_idx=0,
            should_review_memory=False,
            plugin_user_context="",
            ext_prefetch_cache=None,
            preflight_compression_blocked=False,
        )

    monkeypatch.setattr("agent.conversation_loop.build_turn_context", fake_turn_context)
    monkeypatch.setattr(
        "agent.agent_runtime_helpers.repair_message_sequence_with_cursor",
        lambda *_args, **_kwargs: 0,
    )
    monkeypatch.setattr(
        "agent.agent_runtime_helpers.intent_ack_continuation_mode",
        lambda _agent: "off",
    )
    monkeypatch.setattr(
        "hermes_cli.middleware.apply_llm_request_middleware",
        lambda payload, **_kwargs: SimpleNamespace(
            payload=payload,
            original_payload=dict(payload),
            trace=[],
        ),
    )
    monkeypatch.setattr(
        "hermes_cli.middleware.run_llm_execution_middleware",
        lambda payload, execute, **_kwargs: execute(payload),
    )
    monkeypatch.setattr(
        "agent.conversation_loop.estimate_usage_cost",
        lambda *_args, **_kwargs: SimpleNamespace(
            amount_usd=None,
            status="unknown",
            source="offline-test",
        ),
    )
    monkeypatch.setattr("agent.verification_stop.verify_on_stop_enabled", lambda: False)
    monkeypatch.setattr(
        "agent.turn_finalizer.finalize_turn",
        lambda _agent, **kwargs: {
            "final_response": kwargs["final_response"],
            "messages": kwargs["messages"],
            "api_calls": kwargs["api_call_count"],
            "completed": bool(kwargs["final_response"]) and not kwargs["failed"],
            "failed": kwargs["failed"],
        },
    )

    agent = run_agent.AIAgent.__new__(run_agent.AIAgent)
    agent.model = "test-model"
    agent.api_key = "test-key"
    agent.base_url = "offline://provider"
    agent.provider = provider
    agent.api_mode = api_mode
    agent.platform = "test"
    agent.session_id = "offline-final-session"
    agent._parent_session_id = None
    agent._session_db = None
    agent._session_db_created = False
    agent._progress_telemetry = ProgressTelemetry(session_id=agent.session_id)
    agent.context_compressor = _OfflineCompressor()
    agent.iteration_budget = IterationBudget(4)
    agent.max_iterations = 4
    agent._checkpoint_mgr = SimpleNamespace(new_turn=lambda: None)
    agent._iteration_no_progress_limit = 3
    agent._interrupt_requested = False
    agent._interrupt_message = None
    agent._budget_grace_call = False
    agent.quiet_mode = True
    agent.verbose_logging = False
    agent.log_prefix = ""
    agent.suppress_status_output = True
    agent.compression_enabled = False
    agent.max_compression_attempts = 0
    agent.tools = []
    agent.valid_tool_names = set()
    agent.step_callback = None
    agent.tool_progress_callback = None
    agent.thinking_callback = None
    agent.status_callback = None
    agent.ephemeral_system_prompt = None
    agent.prefill_messages = []
    agent.request_overrides = {}
    agent.max_tokens = None
    agent._skill_nudge_interval = 0
    agent._iters_since_skill = 0
    agent._auth_pool_refresh_counts = {}
    agent._api_max_retries = 1
    agent._disable_streaming = True
    agent._force_ascii_payload = False
    agent._use_prompt_caching = False
    agent._is_anthropic_oauth = False
    agent._is_user_initiated_turn = False
    agent._model_request_active = None
    agent._pending_redirect_lock = None
    agent._pending_redirect = None
    agent._pending_steer = None
    agent._pending_steer_lock = None
    agent._fallback_chain = []
    agent._fallback_index = 0
    agent._empty_content_retries = 0
    agent._thinking_prefill_retries = 0
    agent._incomplete_scratchpad_retries = 0
    agent._codex_incomplete_retries = 0
    agent._invalid_tool_retries = 0
    agent._dropped_toolcall_retries = 0
    agent._last_content_with_tools = None
    agent._last_content_tools_all_housekeeping = False
    agent._mute_post_response = False
    agent._current_streamed_assistant_text = ""
    agent._turn_file_mutation_paths = set()
    agent._verification_stop_nudges = 0
    agent._pre_verify_nudges = 0
    agent._kanban_stop_nudges = 0
    agent._response_was_previewed = False
    agent._last_flushed_db_idx = 0
    agent.client = None
    agent.session_prompt_tokens = 0
    agent.session_completion_tokens = 0
    agent.session_total_tokens = 0
    agent.session_api_calls = 0
    agent.session_input_tokens = 0
    agent.session_output_tokens = 0
    agent.session_cache_read_tokens = 0
    agent.session_cache_write_tokens = 0
    agent.session_reasoning_tokens = 0
    agent.session_estimated_cost_usd = 0.0
    agent.session_cost_status = "unknown"
    agent.session_cost_source = "none"

    transport = _OfflineTransport(api_mode)
    agent._get_transport = lambda: transport
    agent._interruptible_api_call = lambda _kwargs: response
    agent._interruptible_streaming_api_call = lambda _kwargs, **_extra: response
    agent._drain_pending_redirect = lambda: None
    agent._drain_pending_steer = lambda: None
    agent._has_pending_redirect = lambda: False
    agent._touch_activity = lambda *_args, **_kwargs: None
    agent._persist_session = lambda *_args, **_kwargs: None
    agent._cleanup_task_resources = lambda *_args, **_kwargs: None
    agent._save_trajectory = lambda *_args, **_kwargs: None
    agent._sanitize_tool_call_arguments = lambda *_args, **_kwargs: 0
    agent._copy_reasoning_content_for_api = lambda *_args, **_kwargs: None
    agent._should_sanitize_tool_calls = lambda: False
    agent._sanitize_api_messages = lambda messages: messages
    agent._drop_thinking_only_and_merge_users = lambda messages, **_kwargs: messages
    agent._reapply_reasoning_echo_for_provider = lambda _messages: None
    agent._build_api_kwargs = lambda messages: {"messages": messages}
    agent._is_copilot_url = lambda: False
    agent._has_stream_consumers = lambda: False
    agent._should_start_quiet_spinner = lambda: False
    agent._reset_stream_delivery_tracking = lambda: None
    agent._should_treat_stop_as_truncated = lambda *_args, **_kwargs: False
    agent._has_content_after_think_block = lambda text: bool(str(text or "").strip())
    agent._strip_think_blocks = lambda text: str(text or "")
    agent._build_assistant_message = lambda message, finish_reason: {
        "role": "assistant",
        "content": message.content,
        "finish_reason": finish_reason,
    }
    agent._emit_pending_fallback_notice = lambda: None
    agent._clear_status_buffer = lambda: None
    agent._emit_status = lambda *_args, **_kwargs: None
    agent._emit_interim_assistant_message = lambda *_args, **_kwargs: None
    agent._interim_content_was_streamed = lambda _text: False
    agent._try_activate_fallback = lambda: False
    agent._has_pending_fallback = lambda: False
    agent._sync_external_memory_for_turn = lambda **_kwargs: None
    agent._spawn_background_review = lambda **_kwargs: None
    agent._flush_messages_to_session_db = lambda *_args, **_kwargs: None
    return agent


def _openai_response(*, usage=True, response_id="chatcmpl-prov-1"):
    return SimpleNamespace(
        id=response_id,
        choices=[
            SimpleNamespace(
                index=0,
                message=SimpleNamespace(
                    role="assistant",
                    content="ok",
                    tool_calls=None,
                    reasoning_content=None,
                ),
                finish_reason="stop",
            )
        ],
        usage=(
            SimpleNamespace(
                prompt_tokens=5000,
                completion_tokens=100,
                total_tokens=5100,
            )
            if usage
            else None
        ),
        model="gpt-4o",
    )


def _anthropic_response():
    return SimpleNamespace(
        id="msg-prov-1",
        content=[SimpleNamespace(type="text", text="ok")],
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=3,
            output_tokens=10,
            cache_read_input_tokens=15,
            cache_creation_input_tokens=2,
        ),
        model="claude-sonnet-4-6",
    )


def _codex_response():
    return SimpleNamespace(
        id="resp-prov-1",
        output=[
            SimpleNamespace(
                type="message",
                content=[SimpleNamespace(type="output_text", text="ok")],
            )
        ],
        usage=SimpleNamespace(
            input_tokens=3000,
            output_tokens=50,
            total_tokens=3050,
        ),
        status="completed",
        model="gpt-5-codex",
    )


@pytest.mark.parametrize(
    ("api_mode", "provider", "response", "expected_tokens"),
    [
        ("chat_completions", "openrouter", _openai_response(), 5100.0),
        ("anthropic_messages", "anthropic", _anthropic_response(), 30.0),
        ("codex_responses", "openai-codex", _codex_response(), 3050.0),
    ],
)
def test_real_conversation_loop_records_measured_provider_receipt(
    monkeypatch, api_mode, provider, response, expected_tokens
):
    agent = _make_loop_agent(
        monkeypatch,
        api_mode=api_mode,
        provider=provider,
        response=response,
    )

    result = agent.run_conversation("hi")

    assert result["completed"] is True
    receipts = [
        receipt
        for receipt in agent._progress_telemetry.usage_aggregate.components
        if receipt.source == "model_response"
    ]
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt.session_id == agent.session_id
    assert receipt.provenance is UsageProvenance.MEASURED
    assert receipt.value == expected_tokens
    assert receipt.authority == "provider_response"
    assert receipt.authoritative is True
    assert receipt.details["api_mode"] == api_mode


def test_missing_provider_usage_is_unknown_not_measured_zero(monkeypatch):
    agent = _make_loop_agent(
        monkeypatch,
        api_mode="chat_completions",
        provider="openrouter",
        response=_openai_response(usage=False),
    )

    agent.run_conversation("hi")

    receipt = next(
        receipt
        for receipt in agent._progress_telemetry.usage_aggregate.components
        if receipt.source == "model_response"
    )
    assert receipt.session_id == agent.session_id
    assert receipt.provenance is UsageProvenance.UNKNOWN
    assert receipt.value is None
    assert receipt.authoritative is False
    assert receipt.reason == "provider_usage_missing"


def test_replayed_provider_response_id_is_not_double_counted(monkeypatch):
    response = _openai_response(response_id="chatcmpl-replayed")
    agent = _make_loop_agent(
        monkeypatch,
        api_mode="chat_completions",
        provider="openrouter",
        response=response,
    )

    agent.run_conversation("first")
    agent.run_conversation("poll replay")

    receipts = [
        receipt
        for receipt in agent._progress_telemetry.usage_aggregate.components
        if receipt.source == "model_response"
    ]
    assert len(receipts) == 1
    assert agent._progress_telemetry.usage_aggregate.known_total == 5100.0


def test_session_reset_rebinds_receipts_to_final_session(monkeypatch):
    agent = _make_loop_agent(
        monkeypatch,
        api_mode="chat_completions",
        provider="openrouter",
        response=_openai_response(response_id="chatcmpl-new-session"),
    )
    old_session_id = agent.session_id
    agent.session_id = "final-session-id"
    agent.reset_session_state(old_session_id=old_session_id)

    agent.run_conversation("hi")

    aggregate = agent._progress_telemetry.usage_aggregate
    assert aggregate.session_id == "final-session-id"
    assert {receipt.session_id for receipt in aggregate.components} == {
        "final-session-id"
    }


class _InjectedCodexSession:
    def __init__(self, result):
        self.result = result

    def run_turn(self, user_input: str, **kwargs):
        return self.result


def _make_codex_app_server_agent(monkeypatch, turn_result):
    """Reuse the pure ``__new__`` fake and inject an app-server session."""
    agent = _make_loop_agent(
        monkeypatch,
        api_mode="codex_app_server",
        provider="openai",
        response=None,
    )
    agent._codex_session = _InjectedCodexSession(turn_result)
    return agent


def test_codex_app_server_records_measured_receipt_at_real_seam(monkeypatch):
    turn_result = TurnResult(
            final_text="done",
            projected_messages=[{"role": "assistant", "content": "done"}],
            turn_id="turn-provenance-1",
            thread_id="thread-provenance-1",
            token_usage_last={
                "totalTokens": 130,
                "inputTokens": 80,
                "cachedInputTokens": 20,
                "outputTokens": 25,
                "reasoningOutputTokens": 5,
            },
    )
    agent = _make_codex_app_server_agent(monkeypatch, turn_result)

    with patch.object(agent, "_spawn_background_review", return_value=None):
        agent.run_conversation("hi")

    receipt = next(
        receipt
        for receipt in agent._progress_telemetry.usage_aggregate.components
        if receipt.source == "codex_app_server_response"
    )
    assert receipt.component_id.endswith("turn-provenance-1")
    assert receipt.session_id == agent.session_id
    assert receipt.provenance is UsageProvenance.MEASURED
    assert receipt.value == 130.0
    assert receipt.authority == "provider_response"
    assert receipt.authoritative is True


def test_codex_app_server_missing_usage_unknown_and_replay_idempotent(monkeypatch):
    turn_result = TurnResult(
            final_text="done",
            projected_messages=[{"role": "assistant", "content": "done"}],
            turn_id="turn-provenance-missing",
            thread_id="thread-provenance-missing",
            token_usage_last=None,
    )
    agent = _make_codex_app_server_agent(monkeypatch, turn_result)

    with patch.object(agent, "_spawn_background_review", return_value=None):
        agent.run_conversation("hi")
        agent.run_conversation("poll replay")

    receipts = [
        receipt
        for receipt in agent._progress_telemetry.usage_aggregate.components
        if receipt.source == "codex_app_server_response"
    ]
    assert len(receipts) == 1
    assert receipts[0].session_id == agent.session_id
    assert receipts[0].provenance is UsageProvenance.UNKNOWN
    assert receipts[0].value is None
    assert receipts[0].reason == "provider_usage_missing"
