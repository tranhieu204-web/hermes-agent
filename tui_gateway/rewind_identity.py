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
from typing import Any, Callable, Iterable, Sequence

REWIND_ID_PREFIX = "r2"


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


def _canonical(history: Sequence[dict], stop: int) -> str:
    """Serialise history[:stop] for hashing: role + text, unambiguously framed."""
    parts: list[str] = []
    for m in history[:stop]:
        if not isinstance(m, dict):
            continue
        parts.append(str(m.get("role") or ""))
        parts.append(chr(0))
        parts.append(coerce_message_text(m.get("content")))
        parts.append(chr(1))
    return "".join(parts)


def rewind_prefix_hash(history: Sequence[dict], user_index: int, text: str) -> str:
    """Hash the prefix a cut at ``user_index`` would KEEP, plus the turn itself.

    This is the identity that matters. Truncation replaces the session with
    ``history[:user_index]`` and re-runs ``text``, so binding the id to exactly
    those two things makes it self-describing: if either has changed since the
    transcript was rendered, resolution refuses instead of cutting somewhere
    the user never saw.
    """
    payload = _canonical(history, user_index) + chr(2) + text
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[:24]


def rewind_message_id(ordinal: int, prefix_hash: str) -> str:
    """``r2:<ordinal>:<prefix hash>`` — position plus the state it assumes.

    v1 was ``ordinal + digest(that turn's text alone)``, which is not occurrence
    identity: the same text at the same ordinal mints the same id even when the
    conversation before it is entirely different.
    """
    return f"{REWIND_ID_PREFIX}:{ordinal}:{prefix_hash}"


def model_user_indices(history: Iterable[dict] | None) -> list[int]:
    """Raw indices of the user rows, in the order ordinals count them.

    Must enumerate exactly like ``prompt.submit``'s own ``user_indices`` or an
    ordinal would name a different row on each side.
    """
    return [
        i
        for i, m in enumerate(history or [])
        if isinstance(m, dict) and m.get("role") == "user"
    ]


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


# Roles that appear, comparably, in BOTH projections. Tool rows are reshaped by
# _history_to_messages into {role, name, context} with no text, so they cannot
# take part in the comparison and are skipped on both sides.
_SPINE_ROLES = ("user", "assistant")


def annotate_rewind_ids(
    messages: list[dict],
    history: Iterable[dict] | None,
    *,
    text_of: Callable[[dict], str] = _display_text,
) -> list[dict]:
    """Stamp ``rewind_id`` on display rows PROVEN to be the same turn.

    Alignment walks both projections from the tail and compares the full
    user+assistant spine, not just the user rows. That distinction is the whole
    fix: comparing user rows alone let the two sides slip past each other when
    they diverged in between. After ``session.undo`` removes a turn from the
    model history but not from the DB, a user-only walk paired a displayed
    bubble with an EARLIER occurrence of the same text and stamped it with that
    occurrence's id — so clicking one turn cut another.

    Comparing the spine catches that: the assistant reply sitting between them
    differs, alignment stops, and the ambiguous rows are left unstamped.

    Every user row is given an explicit key: an id when it is provably
    rewindable, otherwise ``None``. A row with ``rewind_id: None`` is this
    gateway saying "not rewindable"; a row with no key at all is a gateway too
    old to know. The client must be able to tell those apart, or it cannot know
    whether silence means "no" or "unsupported".
    """
    hist = list(history or [])
    user_indices = model_user_indices(hist)
    ordinal_of = {idx: ordinal for ordinal, idx in enumerate(user_indices)}

    spine = [
        (i, m.get("role"), coerce_message_text(m.get("content")))
        for i, m in enumerate(hist)
        if isinstance(m, dict) and m.get("role") in _SPINE_ROLES
    ]
    display_positions = [
        k
        for k, msg in enumerate(messages)
        if isinstance(msg, dict) and msg.get("role") in _SPINE_ROLES
    ]

    matched: dict[int, int] = {}
    cursor = len(spine) - 1
    for k in reversed(display_positions):
        if cursor < 0:
            break
        hist_index, role, text = spine[cursor]
        message = messages[k]
        if message.get("role") != role or text_of(message) != text:
            break
        matched[k] = hist_index
        cursor -= 1

    for k, hist_index in matched.items():
        message = messages[k]
        if message.get("role") != "user":
            continue
        ordinal = ordinal_of.get(hist_index)
        if ordinal is None:
            continue
        text = coerce_message_text(hist[hist_index].get("content"))
        messages[k] = {
            **message,
            "rewind_id": rewind_message_id(
                ordinal, rewind_prefix_hash(hist, hist_index, text)
            ),
        }

    for k, message in enumerate(messages):
        if (
            isinstance(message, dict)
            and message.get("role") == "user"
            and "rewind_id" not in message
        ):
            messages[k] = {**message, "rewind_id": None}

    return messages


def resolve_rewind_ordinal(
    history: Iterable[dict] | None, message_id: str
) -> int | None:
    """Resolve a ``rewind_id`` against the CURRENT model history, or refuse.

    The ordinal locates a candidate; the prefix hash then has to reproduce
    exactly. Any change to what the cut would keep — an undo, a compaction, a
    turn landing in between, a different occurrence of the same text — changes
    the hash and the answer is ``None``.

    Refusing costs a 4018 the user clears by re-clicking on a refreshed
    transcript. Guessing costs a turn, or the transcript.
    """
    if not isinstance(message_id, str) or not message_id.startswith(
        f"{REWIND_ID_PREFIX}:"
    ):
        return None

    parts = message_id.split(":")
    if len(parts) != 3:
        return None
    try:
        ordinal = int(parts[1])
    except ValueError:
        return None
    expected_hash = parts[2]

    hist = list(history or [])
    user_indices = model_user_indices(hist)
    if ordinal < 0 or ordinal >= len(user_indices):
        return None

    index = user_indices[ordinal]
    text = coerce_message_text(hist[index].get("content"))
    if rewind_prefix_hash(hist, index, text) != expected_hash:
        return None

    return ordinal
