"""Ordered, fail-closed lane profiles.

Profiles describe allowed targets; current qualification evidence must still
prove every model, effort, auth, billing, and execution control before use.
"""

from __future__ import annotations

from .types import AdapterKind, LaneProfile


def ordered_profiles() -> tuple[LaneProfile, ...]:
    return (
        LaneProfile(
            lane_id="chatgpt_codex",
            order=0,
            adapter_kind=AdapterKind.NATIVE_PROVIDER,
            provider_id="openai-codex",
            ordered_models=("gpt-5.6-sol",),
            supported_efforts=(
                "minimal", "low", "medium", "high", "xhigh", "max", "ultra"
            ),
            capabilities=frozenset({"workspace_read", "workspace_write", "shell"}),
            allowed_auth_kinds=frozenset({"oauth_subscription"}),
            fast_mode_supported=False,
            fast_off_verifiable=True,
            supports_parent_session=True,
        ),
        LaneProfile(
            lane_id="claude_code",
            order=1,
            # OPERATOR RULE 2026-07-27: no agent may run through an API route.
            # This lane was NATIVE_PROVIDER/"anthropic" — the Anthropic API — which
            # returns HTTP 400 "Third-party apps now draw from your extra usage,
            # not your plan limits" on every call, so the lane advertised 85%
            # headroom while being unable to execute anything. It now runs through
            # the Claude Code CLI on the plan/subscription, exactly like the
            # antigravity lane runs through `agy`. Plan only; if plan usage is
            # exhausted the lane WAITS FOR RESET and never falls back to API.
            adapter_kind=AdapterKind.EXTERNAL_CLI,
            provider_id="claude-code-subscription",
            # Top-model policy (operator 2026-07-27): Fable 5 while under 50% of the
            # weekly window, Opus 5 for the remainder. Selection is made by
            # _resolve_claude_model in the selector; both entries are top-class and
            # Sonnet is deliberately absent from a leader lane.
            ordered_models=("claude-fable-5", "claude-opus-5"),
            supported_efforts=("low", "medium", "high", "max"),
            capabilities=frozenset({"workspace_read", "workspace_write", "shell"}),
            allowed_auth_kinds=frozenset({"oauth_subscription"}),
            fast_mode_supported=False,
            fast_off_verifiable=True,
            supports_parent_session=True,
        ),
        LaneProfile(
            lane_id="grok",
            order=2,
            adapter_kind=AdapterKind.NATIVE_PROVIDER,
            provider_id="xai-oauth",
            ordered_models=("grok-4.5",),
            supported_efforts=(
                "minimal", "low", "medium", "high", "xhigh", "max", "ultra"
            ),
            capabilities=frozenset({"workspace_read", "workspace_write", "shell"}),
            allowed_auth_kinds=frozenset({"oauth_subscription"}),
            fast_mode_supported=False,
            fast_off_verifiable=True,
            supports_parent_session=True,
        ),
        LaneProfile(
            lane_id="antigravity",
            order=3,
            adapter_kind=AdapterKind.EXTERNAL_CLI,
            provider_id="antigravity-subscription",
            ordered_models=(
                "gemini-3.1-pro-high",
                "gemini-3.1-pro-low",
                "gemini-3.6-flash-high",
                "gemini-3.6-flash-medium",
                "gemini-3.6-flash-low",
                "gemini-3.5-flash-high",
                "gemini-3.5-flash-medium",
                "gemini-3.5-flash-low",
            ),
            supported_efforts=("low", "medium", "high"),
            capabilities=frozenset({"workspace_read", "workspace_write", "shell"}),
            allowed_auth_kinds=frozenset({"cli_subscription"}),
            executable="agy",
            implemented=True,
            fast_off_verifiable=True,
            supports_parent_session=True,
        ),
        LaneProfile(
            lane_id="kimi",
            order=4,
            adapter_kind=AdapterKind.EXTERNAL_CLI,
            provider_id="kimi-subscription",
            ordered_models=(),
            supported_efforts=(),
            capabilities=frozenset(),
            allowed_auth_kinds=frozenset({"cli_subscription"}),
            implemented=False,
            fast_off_verifiable=False,
            supports_task_worker=False,
            supports_parent_session=False,
        ),
    )


def profile_map() -> dict[str, LaneProfile]:
    return {profile.lane_id: profile for profile in ordered_profiles()}
