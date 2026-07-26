"""Turn-scoped fallback identities for provider tool calls.

Provider-supplied IDs are always preserved by callers.  This module handles
only the omission case and deliberately accepts a narrow metadata vocabulary:
raw prompts, arguments, commands, and tool output can never enter an ID or a
log message.  The visible fallback consists of turn generation, a monotonic
allocation ordinal, and a digest of sanitized provider item metadata.
"""

from __future__ import annotations

from collections import defaultdict, deque
import hashlib
import json
from typing import Any, Mapping


class ToolCallIdentityError(RuntimeError):
    """Raised before execution when a missing tool call cannot be identified."""


_ALLOWED_METADATA = frozenset(
    {
        "api_mode",
        "api_request_id",
        "batch_index",
        "event_sequence",
        "item_id",
        "item_type",
        "lifecycle",
        "provider",
        "server",
        "source",
        "thread_id",
        "tool_name",
        "turn_id",
    }
)


def _metadata_digest(metadata: Mapping[str, Any]) -> str:
    """Hash only explicitly allow-listed scalar provider metadata."""

    clean: dict[str, str | int | bool] = {}
    for key, value in sorted(metadata.items()):
        if key not in _ALLOWED_METADATA:
            raise ToolCallIdentityError("unsafe provider metadata for tool-call identity")
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            clean[key] = value
        elif isinstance(value, int):
            clean[key] = value
        elif isinstance(value, str):
            # Provider IDs and tool names are metadata, but bound their in-memory
            # representation.  Only the digest is ever returned or logged.
            clean[key] = value[:512]
        else:
            raise ToolCallIdentityError("non-scalar provider metadata for tool-call identity")
    if not clean:
        raise ToolCallIdentityError("tool-call identity requires stable provider event metadata")
    encoded = json.dumps(clean, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class TurnToolCallIdentityAllocator:
    """Allocate deterministic, collision-safe IDs within one turn generation.

    Event digests make notification replay stable.  Codex start/completion
    notifications without an item ID are correlated FIFO by sanitized item
    metadata; the completion event is then aliased to the same allocation so
    the live card and projected ``role=tool`` message cannot diverge.
    """

    def __init__(self, turn_generation: int) -> None:
        generation = int(turn_generation or 0)
        if generation <= 0:
            raise ToolCallIdentityError("tool-call identity requires a positive turn generation")
        self.turn_generation = generation
        self._ordinal = 0
        self._event_ids: dict[str, str] = {}
        self._pending: dict[str, deque[str]] = defaultdict(deque)

    def allocate_missing(
        self,
        *,
        tool_name: str,
        provider_metadata: Mapping[str, Any],
        event_identity: Mapping[str, Any],
        lifecycle: str = "call",
    ) -> str:
        if lifecycle not in {"call", "start", "complete"}:
            raise ToolCallIdentityError("invalid tool-call identity lifecycle")
        if not str(tool_name or "").strip():
            raise ToolCallIdentityError("tool-call identity requires a tool name")

        event_digest = _metadata_digest(
            {
                **event_identity,
                "tool_name": str(tool_name),
                "lifecycle": lifecycle,
            }
        )
        replay_key = hashlib.sha256(
            f"g={self.turn_generation}|event={event_digest}".encode("ascii")
        ).hexdigest()
        existing = self._event_ids.get(replay_key)
        if existing is not None:
            return existing

        item_digest = _metadata_digest(
            {**provider_metadata, "tool_name": str(tool_name)}
        )
        call_id: str | None = None
        if lifecycle == "complete" and self._pending[item_digest]:
            call_id = self._pending[item_digest].popleft()

        if call_id is None:
            self._ordinal += 1
            call_id = (
                f"call_fallback_g{self.turn_generation}_o{self._ordinal}_"
                f"{item_digest[:16]}"
            )
            if lifecycle == "start":
                self._pending[item_digest].append(call_id)

        self._event_ids[replay_key] = call_id
        return call_id


def allocator_for_owner(owner: Any) -> TurnToolCallIdentityAllocator:
    """Return the allocator matching ``owner``'s live telemetry generation."""

    telemetry = getattr(owner, "_progress_telemetry", None)
    generation = int(getattr(telemetry, "turn_generation", 0) or 0)
    if generation <= 0:
        raise ToolCallIdentityError("tool-call identity requires live turn context")
    allocator = getattr(owner, "_tool_call_identity_allocator", None)
    if not isinstance(allocator, TurnToolCallIdentityAllocator) or (
        allocator.turn_generation != generation
    ):
        allocator = TurnToolCallIdentityAllocator(generation)
        owner._tool_call_identity_allocator = allocator
    return allocator
