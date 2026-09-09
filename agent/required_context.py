"""Immutable, persisted Sakaan instruction snapshot per conversation lineage."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Tuple


REQUIRED_CONTEXT_BEGIN = "<!-- hermes-required-context:begin -->"
REQUIRED_CONTEXT_END = "<!-- hermes-required-context:end -->"
_FILES = (
    "sakaan-workflow.md",
    "GRANTS.md",
    "ROUTING.md",
    "procedures/hardened-review.md",
    "Sakaan-crew.md",
)
_METADATA_KEY = "_required_context_lineage"
_CANONICAL_SAKAAN_ROOT = Path(r"C:\Users\HieuKa\.sakaan")
_CANONICAL_POINTER = _CANONICAL_SAKAAN_ROOT / "current.txt"
_CANONICAL_GENERATIONS_ROOT = _CANONICAL_SAKAAN_ROOT / "generations"


class RequiredContextError(RuntimeError):
    pass


class RequiredContextCapacityError(RequiredContextError):
    pass


@dataclass(frozen=True)
class RequiredContextFile:
    path: str
    sha256: str
    raw: bytes
    text: str


@dataclass(frozen=True)
class RequiredContextSnapshot:
    generation: str
    files: Tuple[RequiredContextFile, ...]
    prompt_block: str
    prompt_sha256: str


def _fail(detail: str) -> RequiredContextError:
    return RequiredContextError(f"WORKFLOW_SOURCE_UNAVAILABLE: {detail}")


def _config(config: Optional[Mapping[str, Any]]) -> Mapping[str, Any]:
    if config is not None:
        return config
    try:
        from hermes_cli.config import get_active_config_parse_failure, load_config_readonly

        loaded = load_config_readonly()
        if get_active_config_parse_failure() is not None or not isinstance(loaded, Mapping):
            raise ValueError("invalid configuration")
        return loaded
    except Exception as exc:
        raise _fail("required_context configuration is unavailable") from exc


def required_context_enabled(config: Optional[Mapping[str, Any]] = None) -> bool:
    section = _config(config).get("required_context")
    return isinstance(section, Mapping) and section.get("enabled") is True


def _reject_reparse(path: Path) -> None:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise _fail(f"path is missing or unreadable: {path}") from exc
    attrs = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if stat.S_ISLNK(info.st_mode) or attrs & reparse:
        raise _fail(f"reparse paths are forbidden: {path}")


def _reject_reparse_chain(root: Path, target: Path) -> None:
    """Reject every path component from the canonical trust root to target."""
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise _fail(f"path escapes canonical Sakaan trust root: {target}") from exc
    current = root
    _reject_reparse(current)
    for part in relative.parts:
        current = current / part
        _reject_reparse(current)


def _read_stable_bytes(path: Path) -> bytes:
    before = os.stat(path, follow_symlinks=False)
    raw = path.read_bytes()
    after = os.stat(path, follow_symlinks=False)
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_after or len(raw) != after.st_size:
        raise _fail(f"file changed while loading: {path}")
    return raw


def _decode(raw: bytes, label: str) -> str:
    if raw.startswith(b"\xef\xbb\xbf"):
        raise _fail(f"UTF-8 BOM is forbidden: {label}")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise _fail(f"invalid UTF-8: {label}") from exc
    if not text.strip() or "\x00" in text:
        raise _fail(f"empty or invalid content: {label}")
    return text


def _section(config: Mapping[str, Any]) -> tuple[Path, tuple[str, ...]]:
    section = config.get("required_context")
    if not isinstance(section, Mapping):
        raise _fail("required_context must be a mapping")
    configured_pointer = Path(str(section.get("pointer") or _CANONICAL_POINTER))
    names = section.get("files")
    if not configured_pointer.is_absolute():
        raise _fail("required_context.pointer must be absolute")
    if os.path.normcase(os.path.abspath(configured_pointer)) != os.path.normcase(os.path.abspath(_CANONICAL_POINTER)):
        raise _fail("required_context.pointer must be the canonical Sakaan current.txt")
    if not isinstance(names, list) or tuple(names) != _FILES:
        raise _fail("required_context.files must match the canonical five-file order")
    return _CANONICAL_POINTER, _FILES


def _build_snapshot(generation: Path, files: tuple[RequiredContextFile, ...]) -> RequiredContextSnapshot:
    body = [REQUIRED_CONTEXT_BEGIN, "# Required Sakaan Context", f"generation: {generation}"]
    for item in files:
        body.extend(("", f"## {item.path}", f"sha256: {item.sha256}", "", item.text))
    body.append(REQUIRED_CONTEXT_END)
    prompt = "\n".join(body)
    return RequiredContextSnapshot(
        generation=str(generation), files=files, prompt_block=prompt,
        prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
    )


def load_required_context_snapshot(
    config: Optional[Mapping[str, Any]] = None,
) -> Optional[RequiredContextSnapshot]:
    cfg = _config(config)
    if not required_context_enabled(cfg):
        return None
    pointer, names = _section(cfg)
    _reject_reparse_chain(_CANONICAL_SAKAAN_ROOT, pointer)
    pointer_text = _decode(_read_stable_bytes(pointer), str(pointer))
    lines = [line.strip() for line in pointer_text.splitlines() if line.strip()]
    if len(lines) != 1:
        raise _fail("pointer must contain exactly one non-empty absolute path")
    generation = Path(lines[0])
    if not generation.is_absolute():
        raise _fail("pointer generation must be absolute")
    expected_parent = os.path.normcase(os.path.abspath(_CANONICAL_GENERATIONS_ROOT))
    if os.path.normcase(os.path.abspath(generation.parent)) != expected_parent:
        raise _fail("pointer generation must be a direct child of the canonical generations root")
    _reject_reparse_chain(_CANONICAL_SAKAAN_ROOT, generation)
    resolved_generation = generation.resolve(strict=True)
    resolved_root = _CANONICAL_SAKAAN_ROOT.resolve(strict=True)
    try:
        resolved_generation.relative_to(resolved_root / "generations")
    except ValueError as exc:
        raise _fail("pointer generation escapes canonical Sakaan trust root") from exc
    if resolved_generation.parent != resolved_root / "generations":
        raise _fail("pointer generation must be a direct canonical generation")
    generation = resolved_generation
    if not generation.is_dir():
        raise _fail("generation is not a directory")
    loaded = []
    for name in names:
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise _fail(f"required file escapes generation: {name}")
        target = generation.joinpath(*relative.parts)
        _reject_reparse_chain(generation, target)
        resolved = target.resolve(strict=True)
        try:
            resolved.relative_to(generation)
        except ValueError as exc:
            raise _fail(f"required file escapes generation: {name}") from exc
        if not resolved.is_file():
            raise _fail(f"required file is not regular: {name}")
        raw = _read_stable_bytes(resolved)
        text = _decode(raw, name)
        loaded.append(RequiredContextFile(name, hashlib.sha256(raw).hexdigest(), raw, text))
    return _build_snapshot(generation, tuple(loaded))


def snapshot_to_metadata(
    snapshot: RequiredContextSnapshot, *, system_prompt: Optional[str] = None,
) -> dict[str, Any]:
    metadata = {
        "version": 2 if system_prompt is not None else 1,
        "generation": snapshot.generation,
        "prompt_sha256": snapshot.prompt_sha256,
        "files": [
            {"path": item.path, "sha256": item.sha256, "raw_b64": base64.b64encode(item.raw).decode("ascii")}
            for item in snapshot.files
        ],
    }
    if system_prompt is not None:
        metadata["system_prompt_sha256"] = hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()
    return metadata


def restore_required_context_lineage(
    metadata: Any, *, config: Optional[Mapping[str, Any]] = None,
) -> RequiredContextSnapshot:
    if not required_context_enabled(config):
        raise _fail("persisted required context cannot be restored while disabled")
    if not isinstance(metadata, Mapping) or metadata.get("version") not in {1, 2}:
        raise _fail("persisted snapshot integrity is invalid")
    rows = metadata.get("files")
    if not isinstance(rows, list) or [row.get("path") for row in rows if isinstance(row, Mapping)] != list(_FILES):
        raise _fail("persisted snapshot integrity is invalid")
    generation = Path(str(metadata.get("generation") or ""))
    if not generation.is_absolute():
        raise _fail("persisted snapshot integrity is invalid")
    files = []
    try:
        for row in rows:
            raw = base64.b64decode(row["raw_b64"], validate=True)
            if hashlib.sha256(raw).hexdigest() != row["sha256"]:
                raise ValueError("digest mismatch")
            files.append(RequiredContextFile(row["path"], row["sha256"], raw, _decode(raw, row["path"])))
    except Exception as exc:
        raise _fail("persisted snapshot integrity is invalid") from exc
    snapshot = _build_snapshot(generation, tuple(files))
    if snapshot.prompt_sha256 != metadata.get("prompt_sha256"):
        raise _fail("persisted snapshot integrity is invalid")
    return snapshot


def _model_config(row: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = row.get("model_config")
    if isinstance(raw, Mapping):
        return raw
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
            return decoded if isinstance(decoded, Mapping) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def initialize_required_context_lineage(
    agent: Any, *, config: Optional[Mapping[str, Any]] = None,
    before_provider: Any = None,
) -> None:
    cfg = _config(config)
    if not required_context_enabled(cfg):
        agent._required_context_snapshot = None
        if before_provider is not None:
            before_provider()
        return
    row = agent._session_db.get_session(agent.session_id) if getattr(agent, "_session_db", None) else None
    if row is not None:
        metadata = _model_config(row).get(_METADATA_KEY)
        if metadata is None:
            raise _fail("existing session has no pinned snapshot; start a new lineage")
        snapshot = restore_required_context_lineage(metadata, config=cfg)
        prompt = row.get("system_prompt") or ""
        if prompt.count(REQUIRED_CONTEXT_BEGIN) != 1 or prompt.count(REQUIRED_CONTEXT_END) != 1:
            raise _fail("persisted prompt integrity is invalid; start a new lineage")
        start = prompt.index(REQUIRED_CONTEXT_BEGIN)
        end = prompt.index(REQUIRED_CONTEXT_END, start) + len(REQUIRED_CONTEXT_END)
        if prompt[start:end] != snapshot.prompt_block:
            raise _fail("persisted prompt integrity is invalid; start a new lineage")
        full_prompt_sha256 = metadata.get("system_prompt_sha256")
        if full_prompt_sha256 is not None and (
            not isinstance(full_prompt_sha256, str)
            or not hmac_compare_digest(
                hashlib.sha256(prompt.encode("utf-8")).hexdigest(), full_prompt_sha256,
            )
        ):
            raise _fail("persisted full prompt integrity is invalid; start a new lineage")
    else:
        snapshot = load_required_context_snapshot(cfg)
        if snapshot is None:
            raise _fail("enabled snapshot is unavailable")
        agent._session_init_model_config[_METADATA_KEY] = snapshot_to_metadata(snapshot)
    agent._required_context_snapshot = snapshot
    if before_provider is not None:
        before_provider()


def adopt_current_generation(
    agent: Any, *, new_session_id: str, copy_history: bool = False,
    history: Optional[Iterable[Mapping[str, Any]]] = None,
    config: Optional[Mapping[str, Any]] = None,
) -> RequiredContextSnapshot:
    """Create a child lineage pinned to the current canonical generation.

    The old lineage is first restored and integrity-checked without rereading
    the pointer.  The pointer is read only for the new child.  Provider/client
    capabilities are cleared before the caller can run the child's first turn.
    """
    cfg = _config(config)
    old_session_id = str(getattr(agent, "session_id", "") or "")
    new_session_id = str(new_session_id or "").strip()
    if not old_session_id or not new_session_id or new_session_id == old_session_id:
        raise _fail("migration requires distinct old and new session ids")
    session_db = getattr(agent, "_session_db", None)
    if session_db is None:
        raise _fail("migration requires a session database")
    # Existing/pre-feature/corrupt parents fail here before any capability is
    # cleared or a child row is created.
    initialize_required_context_lineage(agent, config=cfg)
    if session_db.get_session(new_session_id) is not None:
        raise _fail("migration target session already exists")
    snapshot = load_required_context_snapshot(cfg)
    if snapshot is None:
        raise _fail("enabled snapshot is unavailable")

    old_row = session_db.get_session(old_session_id)
    old_snapshot = snapshot_for_agent(agent)
    old_prompt = old_row.get("system_prompt") if isinstance(old_row, Mapping) else None
    if not isinstance(old_prompt, str) or old_snapshot is None:
        raise _fail("migration parent has no complete persisted system prompt")
    if old_prompt.count(old_snapshot.prompt_block) != 1:
        raise _fail("migration parent prompt boundary is invalid")
    full_prompt = old_prompt.replace(old_snapshot.prompt_block, snapshot.prompt_block, 1)
    if full_prompt.count(REQUIRED_CONTEXT_BEGIN) != 1 or full_prompt.count(REQUIRED_CONTEXT_END) != 1:
        raise _fail("migration child prompt boundary is invalid")
    validate_required_context_capacity(agent, full_prompt)

    model_config = dict(getattr(agent, "_session_init_model_config", {}) or {})
    model_config[_METADATA_KEY] = snapshot_to_metadata(snapshot, system_prompt=full_prompt)
    model_config["_sakaan_generation_migration"] = {
        "parent_session_id": old_session_id,
        "cache_reset": True,
    }
    session_db.create_session(
        session_id=new_session_id,
        source=str(getattr(agent, "session_source", "migration") or "migration"),
        model=str(getattr(agent, "model", "") or ""),
        parent_session_id=old_session_id,
        model_config=model_config,
        system_prompt=full_prompt,
    )
    if copy_history:
        rows = history if history is not None else session_db.get_messages_as_conversation(old_session_id)
        copied = sanitize_adoption_history(rows)
        if copied:
            session_db.append_messages_batch(new_session_id, copied)

    agent.session_id = new_session_id
    agent._parent_session_id = old_session_id
    agent._required_context_snapshot = snapshot
    agent._session_init_model_config = model_config
    agent._subscription_route_permit = None
    agent._subscription_auth_generation = ""
    agent.client = None
    agent._primary_runtime = None
    agent._cached_system_prompt = full_prompt
    agent._cached_system_prompt_static = None
    for slot_name in ("_request_client_cache", "_request_anthropic_client_cache"):
        setattr(agent, slot_name, {})
    return snapshot


def sanitize_adoption_history(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
    """Copy only the stable user-visible conversation contract into a new lineage."""
    return [
        {"role": str(row["role"]), "content": str(row.get("content") or "")}
        for row in rows
        if isinstance(row, Mapping) and row.get("role") in {"user", "assistant"}
    ]


def append_required_context(prompt: str, snapshot: Optional[RequiredContextSnapshot]) -> str:
    begins, ends = prompt.count(REQUIRED_CONTEXT_BEGIN), prompt.count(REQUIRED_CONTEXT_END)
    if begins or ends:
        if begins != 1 or ends != 1:
            raise _fail("duplicate or malformed required-context block")
        if snapshot is None:
            raise _fail("required-context block exists without a pinned snapshot")
        start = prompt.index(REQUIRED_CONTEXT_BEGIN)
        end = prompt.index(REQUIRED_CONTEXT_END, start) + len(REQUIRED_CONTEXT_END)
        if prompt[start:end] != snapshot.prompt_block:
            raise _fail("required-context block does not match pinned lineage")
        return prompt
    if snapshot is None:
        return prompt
    return f"{prompt}\n\n{snapshot.prompt_block}" if prompt else snapshot.prompt_block


def snapshot_for_agent(agent: Any) -> Optional[RequiredContextSnapshot]:
    """Return an explicitly installed snapshot without triggering mock/dynamic attributes."""
    state = getattr(agent, "__dict__", {})
    snapshot = state.get("_required_context_snapshot") if isinstance(state, dict) else None
    if snapshot is not None and not isinstance(snapshot, RequiredContextSnapshot):
        raise _fail("runtime required-context snapshot is malformed")
    return snapshot


def hmac_compare_digest(left: str, right: str) -> bool:
    """Constant-time digest comparison without importing credential-oriented helpers."""
    import hmac

    return hmac.compare_digest(left, right)


def validate_required_context_capacity(agent: Any, prompt: str) -> None:
    if REQUIRED_CONTEXT_BEGIN not in prompt:
        return
    context_length = getattr(getattr(agent, "context_compressor", None), "context_length", None)
    if not isinstance(context_length, int) or context_length <= 0:
        return
    from agent.model_metadata import _estimate_tools_tokens_rough, estimate_tokens_rough

    needed = estimate_tokens_rough(prompt) + _estimate_tools_tokens_rough(getattr(agent, "tools", None) or [])
    output = getattr(agent, "max_tokens", 0)
    needed += output if isinstance(output, int) and output > 0 else 0
    if needed > context_length:
        raise RequiredContextCapacityError(
            f"REQUIRED_CONTEXT_CAPACITY: ~{needed} tokens exceed context length {context_length}"
        )


__all__ = [
    "REQUIRED_CONTEXT_BEGIN", "REQUIRED_CONTEXT_END", "RequiredContextCapacityError",
    "RequiredContextError", "RequiredContextFile", "RequiredContextSnapshot",
    "adopt_current_generation", "append_required_context", "initialize_required_context_lineage",
    "load_required_context_snapshot", "required_context_enabled",
    "restore_required_context_lineage", "snapshot_for_agent", "snapshot_to_metadata",
    "sanitize_adoption_history",
    "validate_required_context_capacity",
]
