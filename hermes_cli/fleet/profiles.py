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
            # Plan-CLI lane (operator rule 2026-07-27: no agent ever runs through
            # an API route). provider_id stays "anthropic" — the Claude Code route
            # is an AUTH MODE of the anthropic provider, not its own provider; the
            # 2026-07-27 revert was caused by inventing "claude-code-subscription".
            lane_id="claude_code",
            order=1,
            adapter_kind=AdapterKind.EXTERNAL_CLI,
            provider_id="anthropic",
            ordered_models=("claude-fable-5", "claude-opus-5"),
            supported_efforts=("low", "medium", "high", "xhigh", "max"),
            capabilities=frozenset({"workspace_read", "workspace_write", "shell"}),
            allowed_auth_kinds=frozenset({"cli_subscription"}),
            executable="claude",
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
