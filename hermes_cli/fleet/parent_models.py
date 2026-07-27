"""Parent-route model admission helpers.

Fleet parent routes are an exact allowlist. Catalog visibility of Sonnet (or
any other non-commissioned id) must never admit a Desktop parent.
"""

from __future__ import annotations

import re

# Lead Claude parent model (auto-selection target on the plan-CLI lane).
# Sonnet is never AUTO-selected; explicit operator picks are validated
# against the lane's ordered_models + live qualification instead of this
# allowlist (operator decision 2026-07-27 evening).
ADMITTED_CLAUDE_PARENT_MODEL = "claude-fable-5"
ADMITTED_PARENT_MODELS = frozenset(
    {
        "gpt-5.6-sol",
        ADMITTED_CLAUDE_PARENT_MODEL,
        "claude-opus-5",
        "grok-4.5",
        "gemini-3.1-pro-high",
    }
)

_SONNET_RE = re.compile(r"(^|/)claude-sonnet|sonnet", re.IGNORECASE)


def normalize_model_id(model: object) -> str:
    text = str(model or "").strip()
    if not text:
        return ""
    # Drop provider prefix (anthropic/claude-…).
    if "/" in text:
        text = text.rsplit("/", 1)[-1]
    return text.strip().lower().replace("_", "-")


def is_sonnet_model(model: object) -> bool:
    """True when the model id is any Claude Sonnet variant."""

    normalized = normalize_model_id(model)
    if not normalized:
        return False
    if normalized == ADMITTED_CLAUDE_PARENT_MODEL:
        return False
    return bool(_SONNET_RE.search(normalized))


def is_admitted_parent_model(model: object) -> bool:
    """Exact fleet parent allowlist (after provider-prefix strip)."""

    normalized = normalize_model_id(model)
    if not normalized:
        return False
    for admitted in ADMITTED_PARENT_MODELS:
        if normalized == admitted or normalized.startswith(f"{admitted}-"):
            # date-pinned variants of admitted ids are not auto-allowed
            if normalized == admitted:
                return True
    return normalized in {item.lower() for item in ADMITTED_PARENT_MODELS}


def reject_sonnet_parent_reason(model: object) -> str:
    shown = str(model or "").strip() or "<empty>"
    return (
        f"Sonnet is not an auto-routed fleet parent target ({shown}). "
        "Pick it explicitly from the Claude · Fleet section of the model "
        "picker instead."
    )
