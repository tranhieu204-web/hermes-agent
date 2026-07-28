"""Authenticated, in-process receipts for landed built-in file mutations.

Tool output is model-visible and therefore cannot authenticate its own effects.
This module keeps the public result string unchanged while attaching a typed
receipt carrying an issuer capability that cannot survive JSON serialization.
Only the built-in file executor calls the issuer after a mutation has landed;
the common terminal-event seam validates every identity before counting progress.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
import re
from typing import Any, Iterable

_SCHEMA_VERSION = "hermes.post_effect_receipt.v1"
_ISSUER = "hermes.builtin_file_executor.v1"
_EFFECT_KIND = "landed_file_mutation"
_RECOGNIZED_TOOLS = frozenset({"write_file", "patch"})
_ISSUER_CAPABILITY = object()
_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class _PostEffectReceipt:
    schema_version: str
    issuer: str
    receipt_id: str
    event_identity: str
    effect_kind: str
    tool_name: str
    target_identity: str
    landed_path_identity: str
    effect_identity: str
    landed: bool
    _issuer_capability: object = field(repr=False, compare=False)


class _AuthenticatedToolResult(str):
    """Public tool text plus non-serializable, core-issued effect evidence."""

    def __new__(cls, public_result: str, receipt: _PostEffectReceipt):
        value = super().__new__(cls, public_result)
        value._post_effect_receipt = receipt
        return value


def _digest(label: str, *parts: str) -> str:
    payload = json.dumps(
        [label, *parts],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _normalized_target(path: Any) -> str | None:
    if not isinstance(path, (str, os.PathLike)):
        return None
    value = os.fspath(path).strip()
    if not value:
        return None
    # Lexical normalization is deliberate: issuer and verifier see the same
    # invocation arguments even when the executor uses a remote/task-specific
    # cwd. The normalized value is immediately hashed and is never logged.
    return os.path.normcase(os.path.normpath(os.path.expanduser(value)))


def _target_paths(tool_name: str, function_args: dict[str, Any]) -> list[str]:
    if tool_name not in _RECOGNIZED_TOOLS or not isinstance(function_args, dict):
        return []
    if tool_name == "write_file":
        path = _normalized_target(function_args.get("path"))
        return [path] if path else []

    mode = function_args.get("mode") or "replace"
    if mode == "replace":
        path = _normalized_target(function_args.get("path"))
        return [path] if path else []
    if mode != "patch":
        return []

    body = function_args.get("patch")
    if not isinstance(body, str) or not body:
        return []
    targets: list[str] = []
    for match in re.finditer(
        r"^\*\*\*\s*(?:Update|Add|Delete)\s+File:\s*(.+)$",
        body,
        re.MULTILINE,
    ):
        normalized = _normalized_target(match.group(1))
        if normalized:
            targets.append(normalized)
    for match in re.finditer(
        r"^\*\*\*\s*Move\s+File:\s*(.+?)\s*->\s*(.+)$",
        body,
        re.MULTILINE,
    ):
        for path in match.groups():
            normalized = _normalized_target(path)
            if normalized:
                targets.append(normalized)
    return targets


def _path_set_identity(label: str, paths: Iterable[Any]) -> str | None:
    normalized = sorted(
        {
            value
            for path in paths
            if (value := _normalized_target(path)) is not None
        }
    )
    if not normalized:
        return None
    # Do not place raw paths in receipt IDs or logs. Individual path hashes
    # prevent delimiter ambiguity before the stable set identity is computed.
    path_hashes = [_digest("normalized_path", value) for value in normalized]
    return _digest(label, *path_hashes)


def _target_identity(tool_name: str, function_args: dict[str, Any]) -> str | None:
    return _path_set_identity("target_set", _target_paths(tool_name, function_args))


def _event_identity(session_id: str, tool_call_id: str) -> str | None:
    if not isinstance(session_id, str) or not session_id:
        return None
    if not isinstance(tool_call_id, str) or not tool_call_id:
        return None
    return _digest("terminal_tool_event", session_id, tool_call_id)


def _effect_identity(tool_name: str, target_identity: str) -> str:
    return _digest(
        "file_effect",
        _SCHEMA_VERSION,
        _EFFECT_KIND,
        tool_name,
        target_identity,
    )


def _receipt_identity(
    event_identity: str,
    effect_identity: str,
    landed_path_identity: str,
) -> str:
    return _digest(
        "post_effect_receipt",
        _SCHEMA_VERSION,
        _ISSUER,
        event_identity,
        effect_identity,
        landed_path_identity,
    )


def issue_landed_file_mutation_result(
    public_result: str,
    *,
    tool_name: str,
    function_args: dict[str, Any],
    landed_paths: Iterable[Any],
    tool_call_id: str,
    session_id: str,
) -> str:
    """Attach trusted evidence after a built-in file mutation has landed.

    Missing identity inputs conservatively return the ordinary public string.
    Callers must invoke this only on the executor's successful post-effect path.
    """

    if type(public_result) is not str or tool_name not in _RECOGNIZED_TOOLS:
        return public_result
    target_identity = _target_identity(tool_name, function_args)
    landed_path_identity = _path_set_identity("landed_path_set", landed_paths)
    event_identity = _event_identity(session_id, tool_call_id)
    if not target_identity or not landed_path_identity or not event_identity:
        return public_result
    effect_identity = _effect_identity(tool_name, target_identity)
    receipt = _PostEffectReceipt(
        schema_version=_SCHEMA_VERSION,
        issuer=_ISSUER,
        receipt_id=_receipt_identity(
            event_identity,
            effect_identity,
            landed_path_identity,
        ),
        event_identity=event_identity,
        effect_kind=_EFFECT_KIND,
        tool_name=tool_name,
        target_identity=target_identity,
        landed_path_identity=landed_path_identity,
        effect_identity=effect_identity,
        landed=True,
        _issuer_capability=_ISSUER_CAPABILITY,
    )
    return _AuthenticatedToolResult(public_result, receipt)


def validate_landed_file_mutation_result(
    result: Any,
    *,
    tool_name: str,
    function_args: dict[str, Any],
    tool_call_id: str,
    session_id: str,
) -> _PostEffectReceipt | None:
    """Return the receipt only when all trusted post-effect identities match."""

    if type(result) is not _AuthenticatedToolResult:
        return None
    receipt = getattr(result, "_post_effect_receipt", None)
    if type(receipt) is not _PostEffectReceipt:
        return None
    if receipt._issuer_capability is not _ISSUER_CAPABILITY:
        return None
    if (
        receipt.schema_version != _SCHEMA_VERSION
        or receipt.issuer != _ISSUER
        or receipt.effect_kind != _EFFECT_KIND
        or receipt.tool_name != tool_name
        or tool_name not in _RECOGNIZED_TOOLS
        or receipt.landed is not True
        or not _HEX_DIGEST.fullmatch(receipt.landed_path_identity)
    ):
        return None

    expected_event = _event_identity(session_id, tool_call_id)
    expected_target = _target_identity(tool_name, function_args)
    if not expected_event or not expected_target:
        return None
    expected_effect = _effect_identity(tool_name, expected_target)
    expected_receipt_id = _receipt_identity(
        expected_event,
        expected_effect,
        receipt.landed_path_identity,
    )
    if (
        receipt.event_identity != expected_event
        or receipt.target_identity != expected_target
        or receipt.effect_identity != expected_effect
        or receipt.receipt_id != expected_receipt_id
    ):
        return None
    return receipt
