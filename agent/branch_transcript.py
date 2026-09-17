"""The transcript a branch copies — one definition for every branch writer (desktop ``session.branch``, seeded
``session.create`` with a parent, CLI ``/branch``).

A branch continues the parent's conversation, so the child model needs the evidence behind the earlier answers:
assistant ``tool_calls`` and their ``tool`` results, user turns with their attachment references (``@image:``
lines and the ``api_content`` sidecar holding the exact bytes the model saw) and provider reasoning state — in the
original order, one row per row, never merged.

Providers reject a tool call without its result (Anthropic: in the IMMEDIATELY following turn) and a result
without its call, so :func:`pair_branch_tool_turns` makes the copy self-consistent: a result is kept only in the
run of ``tool`` rows directly after the assistant row that issued it, and an unanswered call — typically the
parent's in-flight or interrupted final tool turn — is removed from its assistant row. That row is dropped when
nothing visible remains, and it loses its provider-signed reasoning (signed against the original calls)."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List

from agent.message_sanitization import tool_call_id_variants, tool_result_id_variants

# Columns a branch row carries. ``_row_id``/``platform_message_id`` are parent-row identity and never copied;
# ``timestamp`` is kept because branch copies are history, not new activity; display_kind/metadata keep timeline
# markers (role=user rows) out of the truncate ordinal address space after a restart. ``_compressed_summary`` keeps a
# compacted parent's summary carrier marked in the child (its model projection is what a compacted parent's branch
# copies).
BRANCH_ROW_FIELDS = (
    "content", "api_content", "tool_calls", "tool_call_id", "tool_name", "effect_disposition", "observed",
    "finish_reason", "reasoning", "reasoning_content", "reasoning_details", "codex_reasoning_items",
    "codex_message_items", "display_kind", "display_metadata", "timestamp", "_compressed_summary")

# Opaque provider state bound to the assistant turn exactly as issued; invalid once a call is removed from it.
_SIGNED_REASONING_FIELDS = ("reasoning_details", "codex_reasoning_items", "codex_message_items")


def branch_row(message: Dict[str, Any]) -> Dict[str, Any]:
    """One branch row: role plus the present :data:`BRANCH_ROW_FIELDS` (live tool dicts name the tool ``name``)."""
    role = message.get("role", "user")
    row = {"role": role, **{field: message[field] for field in BRANCH_ROW_FIELDS if message.get(field) is not None}}
    if role == "tool" and not row.get("tool_name") and message.get("name"):
        row["tool_name"] = message["name"]
    return row


def _has_visible_content(content: Any) -> bool:
    if isinstance(content, str):
        return bool(content.strip())
    return bool(content)


def pair_branch_tool_turns(messages: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Copies of ``messages`` in order with every assistant tool call answered by an adjacent result and no
    orphaned result (see the module docstring for what is dropped and why)."""
    rows = [dict(message) for message in messages if isinstance(message, dict)]
    out: List[Dict[str, Any]] = []
    index = 0
    while index < len(rows):
        row = rows[index]
        index += 1
        if row.get("role") == "tool":
            continue  # not in the result run of an assistant tool turn: orphaned
        calls = row.get("tool_calls") if row.get("role") == "assistant" else None
        if not calls:
            out.append(row)
            continue
        results = []
        while index < len(rows) and rows[index].get("role") == "tool":
            results.append(rows[index])
            index += 1
        kept_results, answered = [], set()
        for result in results:
            variants = tool_result_id_variants(result.get("tool_call_id"))
            call_index = next((i for i, call in enumerate(calls)
                               if i not in answered and variants & tool_call_id_variants(call)), None)
            if call_index is not None:
                answered.add(call_index)
                kept_results.append(result)
        if len(answered) < len(calls):
            row["tool_calls"] = [call for i, call in enumerate(calls) if i in answered]
            if not row["tool_calls"]:
                del row["tool_calls"]
            for field in _SIGNED_REASONING_FIELDS:
                row.pop(field, None)
            if not row.get("tool_calls") and not _has_visible_content(row.get("content")):
                continue
        out.append(row)
        out.extend(kept_results)
    return out


def branch_rows(messages: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The rows a branch writes and hands its agent: :func:`branch_row` of each message, tool turns paired."""
    return pair_branch_tool_turns(branch_row(message) for message in messages if isinstance(message, dict))
