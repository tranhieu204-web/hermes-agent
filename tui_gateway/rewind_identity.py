"""Computed, occurrence-bound identity for model-history user turns."""

from __future__ import annotations

import hashlib
from typing import Any, Callable, Iterable, Sequence

REWIND_ID_PREFIX = "r2"


def coerce_message_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, (int, float)):
        return str(content)
    if isinstance(content, list):
        chunks: list[str] = []
        for part in content:
            if isinstance(part, str):
                chunks.append(part)
            elif isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    chunks.append(text)
                    continue
                kind = part.get("type")
                if kind in {"text", "input_text", "output_text"}:
                    value = part.get("text") or part.get("content") or ""
                    if value:
                        chunks.append(str(value))
                elif kind in {"image_url", "input_image", "image"}:
                    image = part.get("image_url")
                    url = image.get("url", "") if isinstance(image, dict) else image
                    chunks.append(f"\n{url}" if url else "\n[image]")
                elif kind in {"input_audio", "audio"}:
                    chunks.append("\n[audio]")
                elif kind:
                    chunks.append(f"\n[{kind}]")
        return "".join(chunks)
    if isinstance(content, dict):
        kind = content.get("type")
        if kind in {"text", "input_text", "output_text"}:
            return str(content.get("text") or content.get("content") or "")
        if kind in {"image_url", "input_image", "image"}:
            image = content.get("image_url")
            url = image.get("url", "") if isinstance(image, dict) else image
            return str(url or "[image]")
        if kind in {"input_audio", "audio"}:
            return "[audio]"
        if kind:
            return f"[{kind}]"
        if "text" in content:
            return str(content.get("text") or "")
        return "[structured content]"
    return str(content)


def model_user_indices(history: Iterable[dict] | None) -> list[int]:
    return [
        i
        for i, m in enumerate(history or [])
        if isinstance(m, dict) and m.get("role") == "user" and not m.get("display_kind")
    ]


def _canonical(history: Sequence[dict], stop: int) -> str:
    parts: list[str] = []
    for message in history[:stop]:
        if not isinstance(message, dict):
            continue
        parts.extend((str(message.get("role") or ""), "\0", coerce_message_text(message.get("content")), "\1"))
    return "".join(parts)


def rewind_prefix_hash(history: Sequence[dict], user_index: int, text: str) -> str:
    payload = _canonical(history, user_index) + "\2" + text
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[:24]


def rewind_message_id(ordinal: int, prefix_hash: str) -> str:
    return f"{REWIND_ID_PREFIX}:{ordinal}:{prefix_hash}"


def _display_text(message: dict) -> str:
    """Accept flattened gateway ``text`` and raw DB/REST ``content`` rows."""
    if "text" in message:
        return message.get("text") or ""
    return coerce_message_text(message.get("content"))


_SPINE_ROLES = ("user", "assistant")


def annotate_rewind_ids(
    display_rows: list[dict],
    model_history: Iterable[dict] | None,
    *,
    text_of: Callable[[dict], str] = _display_text,
) -> list[dict]:
    history = list(model_history or [])
    user_indices = model_user_indices(history)
    ordinal_of = {index: ordinal for ordinal, index in enumerate(user_indices)}
    spine = [
        (i, message.get("role"), coerce_message_text(message.get("content")))
        for i, message in enumerate(history)
        if isinstance(message, dict)
        and message.get("role") in _SPINE_ROLES
        and not message.get("display_kind")
    ]
    display_positions = [
        i
        for i, message in enumerate(display_rows)
        if isinstance(message, dict)
        and message.get("role") in _SPINE_ROLES
        and not message.get("display_kind")
    ]

    matched: dict[int, int] = {}
    cursor = len(spine) - 1
    for position in reversed(display_positions):
        if cursor < 0:
            break
        history_index, role, text = spine[cursor]
        row = display_rows[position]
        if row.get("role") != role or text_of(row) != text:
            break
        matched[position] = history_index
        cursor -= 1

    for position, history_index in matched.items():
        row = display_rows[position]
        if row.get("role") != "user":
            continue
        ordinal = ordinal_of.get(history_index)
        if ordinal is None:
            continue
        text = coerce_message_text(history[history_index].get("content"))
        display_rows[position] = {
            **row,
            "rewind_id": rewind_message_id(
                ordinal, rewind_prefix_hash(history, history_index, text)
            ),
        }

    for position, row in enumerate(display_rows):
        if isinstance(row, dict) and row.get("role") == "user" and "rewind_id" not in row:
            display_rows[position] = {**row, "rewind_id": None}
    return display_rows


def resolve_rewind_ordinal(model_history: Iterable[dict] | None, message_id: str) -> int | None:
    if not isinstance(message_id, str):
        return None
    parts = message_id.split(":")
    if len(parts) != 3 or parts[0] != REWIND_ID_PREFIX:
        return None
    try:
        ordinal = int(parts[1])
    except ValueError:
        return None
    history = list(model_history or [])
    user_indices = model_user_indices(history)
    if ordinal < 0 or ordinal >= len(user_indices):
        return None
    index = user_indices[ordinal]
    text = coerce_message_text(history[index].get("content"))
    if rewind_prefix_hash(history, index, text) != parts[2]:
        return None
    return ordinal
