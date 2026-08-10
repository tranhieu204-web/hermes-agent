"""Stable identity for the user turns a rewind is allowed to cut at.

The transcript a client renders is the DISPLAY projection: the persisted
lineage read with ``include_ancestors=True``, plus rows the model history
collapses out.  ``prompt.submit``'s truncation can only cut inside
``session["history"]`` — the MODEL projection for the current session record,
with the ancestor lineage held separately in ``display_history_prefix``.

The two disagree by construction.  Compaction replaces the model history with a
summary and rotates the DB session, so everything before the handoff moves into
the ancestor prefix; replay sanitisation drops a dangling tool tail; and
injected ``[System: …]`` pivot markers are stored as role=user rows.  A client
that addresses a turn *positionally* ("the Nth user bubble I am rendering")
therefore overshoots the model history and lands on 4018 — or, when the injected
markers pad the model side, silently truncates an EARLIER turn than the one that
was clicked, which is the same defect with the alarm removed.

Both directions are one bug: a position is not an identity.  A server stamps a
``rewind_id`` on exactly those display messages that still map to a truncatable
user row and resolves that id back to an ordinal at submit time.  A message
without a ``rewind_id`` is not rewindable — the client hides the affordance
rather than offering an action that cannot succeed.

This module is deliberately dependency-free so both the JSON-RPC gateway
(``tui_gateway.server``) and the REST transcript endpoint
(``hermes_cli.web_server``) can mint ids the gateway will resolve.
"""

from __future__ import annotations

import hashlib
from typing import Any, Callable, Iterable

REWIND_ID_PREFIX = "r1"


def coerce_message_text(content: Any) -> str:
    """Render ``message['content']`` as a plain string for transport.

    Provider-side, ``content`` may be a string (most common), a list of
    multimodal parts (e.g. ``[{"type": "text", "text": "..."},
    {"type": "image_url", "image_url": {...}}]``), or a single structured
    dict. Calling ``.strip()`` on a list raises ``'list' object has no
    attribute 'strip'`` and breaks session resume entirely.

    Image parts (``image_url``) are preserved by appending the underlying
    URL (data: or http:) into the text. The desktop renderer pulls these
    back out via ``extractEmbeddedImages`` so the user sees the image
    instead of the URL — and it stops the resume payload from disagreeing
    with the cached message (which would otherwise cause the inline image
    to flash, then disappear when the resume payload overwrites the cache).

    Other structured dict shapes (audio, unknown types) fall back to a
    bracketed placeholder so resume doesn't drop the message entirely.

    Both projections MUST normalise through this one function: the rewind
    alignment below compares their user text verbatim, so any divergence here
    silently costs a session its restore affordances.
    """
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
                continue
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if isinstance(text, str):
                chunks.append(text)
                continue
            kind = part.get("type")
            if kind in {"text", "input_text", "output_text"}:
                t = part.get("text") or part.get("content") or ""
                if t:
                    chunks.append(str(t))
                continue
            if kind in {"image_url", "input_image", "image"}:
                image_url = part.get("image_url")
                url = ""
                if isinstance(image_url, dict):
                    candidate = image_url.get("url")
                    if isinstance(candidate, str):
                        url = candidate
                elif isinstance(image_url, str):
                    url = image_url
                if url:
                    chunks.append(f"\n{url}")
                else:
                    chunks.append("\n[image]")
                continue
            if kind in {"input_audio", "audio"}:
                chunks.append("\n[audio]")
                continue
            if kind:
                chunks.append(f"\n[{kind}]")
        return "".join(chunks)
    if isinstance(content, dict):
        kind = content.get("type")
        if kind in {"text", "input_text", "output_text"}:
            return str(content.get("text") or content.get("content") or "")
        if kind in {"image_url", "input_image", "image"}:
            image_url = content.get("image_url")
            url = ""
            if isinstance(image_url, dict):
                candidate = image_url.get("url")
                if isinstance(candidate, str):
                    url = candidate
            elif isinstance(image_url, str):
                url = image_url
            return url or "[image]"
        if kind in {"input_audio", "audio"}:
            return "[audio]"
        if kind:
            return f"[{kind}]"
        if "text" in content:
            return str(content.get("text") or "")
        return "[structured content]"
    return str(content)


def rewind_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def rewind_message_id(ordinal: int, text: str) -> str:
    """Stable identity for the ``ordinal``-th user row of a model history."""
    return f"{REWIND_ID_PREFIX}:{ordinal}:{rewind_digest(text)}"


def model_user_texts(history: Iterable[dict] | None) -> list[str]:
    """The user rows of a model history, in the order ordinals count them."""
    return [
        coerce_message_text(m.get("content"))
        for m in (history or [])
        if isinstance(m, dict) and m.get("role") == "user"
    ]


def _display_text(message: dict) -> str:
    """Text of a display row, whichever projection produced it.

    The gateway's ``_history_to_messages`` output carries a flattened ``text``;
    the REST transcript ships raw DB rows whose ``content`` still needs the same
    normalisation.
    """
    if "text" in message:
        return message.get("text") or ""
    return coerce_message_text(message.get("content"))


def annotate_rewind_ids(
    messages: list[dict],
    history: Iterable[dict] | None,
    *,
    text_of: Callable[[dict], str] = _display_text,
) -> list[dict]:
    """Stamp ``rewind_id`` on the display messages that map to a live user row.

    Aligns the display transcript against the model history from the TAIL and
    stops at the first divergence, so every id that is handed out is backed by a
    verbatim match reachable through an unbroken run of matches — an id can
    never name a turn other than the bubble it rides on, even when the same
    prompt text was sent more than once.

    Everything above the divergence (ancestor lineage, pre-compaction turns,
    rows dropped by sanitisation) is deliberately left unstamped.  This is
    conservative by construction: its failure mode is a hidden restore button,
    never a wrong truncation.
    """
    user_texts = model_user_texts(history)
    ordinal = len(user_texts) - 1

    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        if ordinal < 0:
            break
        text = user_texts[ordinal]
        if text_of(message) != text:
            break
        messages[index] = {**message, "rewind_id": rewind_message_id(ordinal, text)}
        ordinal -= 1

    return messages


def resolve_rewind_ordinal(
    history: Iterable[dict] | None, message_id: str
) -> int | None:
    """Map a ``rewind_id`` back to a user ordinal in the CURRENT model history.

    Exact ``(ordinal, content)`` match first.  If the position drifted since the
    transcript was sent — a turn landed, an undo ran, or the id was minted by
    the REST endpoint against the DB projection — fall back to a *unique*
    content match so the intended turn is still reachable.  An ambiguous or
    absent match resolves to ``None`` and the caller refuses: guessing here is
    exactly the silent-wrong-cut this identity exists to prevent.
    """
    if not isinstance(message_id, str) or not message_id.startswith(
        f"{REWIND_ID_PREFIX}:"
    ):
        return None

    user_texts = model_user_texts(history)
    for ordinal, text in enumerate(user_texts):
        if rewind_message_id(ordinal, text) == message_id:
            return ordinal

    digest = message_id.rsplit(":", 1)[-1]
    matches = [
        ordinal
        for ordinal, text in enumerate(user_texts)
        if rewind_digest(text) == digest
    ]
    return matches[0] if len(matches) == 1 else None
