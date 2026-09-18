"""ACP session manager — maps ACP sessions to Hermes AIAgent instances.

Sessions are persisted to the shared SessionDB (``~/.hermes/state.db``) so they
survive process restarts and appear in ``session_search``; ``load_session`` /
``resume_session`` after an editor reconnect restore the full history from there.
"""
from __future__ import annotations

from hermes_constants import get_hermes_home, translate_cwd_for_wsl_backend, windows_path_to_wsl

import copy
import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _translate_acp_cwd(cwd: str) -> str:
    """Translate Windows ACP cwd values (``E:\\Projects``, ``\\\\wsl.localhost\\``) to POSIX form
    when Hermes runs in WSL so agents, tools, and persisted sessions agree; no-op elsewhere."""
    return translate_cwd_for_wsl_backend(str(cwd))


def _normalize_cwd_for_compare(cwd: str | None) -> str:
    expanded = os.path.expanduser(str(cwd or ".").strip() or ".")

    # Windows drive paths -> WSL mount form so history filters match across hosts.
    translated = windows_path_to_wsl(expanded)
    if translated is not None:
        expanded = translated
    elif re.match(r"^/mnt/[A-Za-z]/", expanded):
        expanded = f"/mnt/{expanded[5].lower()}/{expanded[7:]}"

    # realpath resolves symlink aliases (macOS ``/var`` vs ``/private/var``, ``/tmp`` vs
    # ``/private/tmp``) that otherwise drop a workspace's own sessions; it is lexical
    # for missing paths (e.g. WSL-translated drives).
    try:
        # Resolve symlink aliases so equivalent spellings of the same directory compare equal — macOS
        # reports editor workspaces as ``/var/...`` while sessions get stored under ``/private/var/...``
        # (and ``/tmp`` vs ``/private/tmp``), which made ACP history filters silently drop a workspace's own
        # sessions. WSL-translated Windows drives — keep the previous normpath behavior. Ported from
        # PrimeIntellect-ai/prime-agent#628.
        return os.path.realpath(expanded)
    except OSError:
        return os.path.normpath(expanded)


def _build_session_title(title: Any, preview: Any, cwd: str | None) -> str:
    leaf = os.path.basename(str(cwd or "").rstrip("/\\"))
    return str(title or "").strip() or str(preview or "").strip() or leaf or "New thread"


def _format_updated_at(value: Any) -> str | None:
    if value is None or (isinstance(value, str) and value.strip()):
        return value
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    except Exception:
        return None


def _updated_at_sort_key(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    raw = str(value).strip() if value is not None else ""
    if not raw:
        return float("-inf")
    for parse in (lambda s: datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp(), float):
        try:
            return parse(raw)
        except Exception:
            continue
    return float("-inf")


def _acp_stderr_print(*args, **kwargs) -> None:
    """Route incidental AIAgent output to stderr; ACP reserves stdout for JSON-RPC."""
    kwargs.setdefault("file", sys.stderr)
    print(*args, **kwargs)


def _register_task_cwd(task_id: str, cwd: str) -> None:
    """Bind a task/session id to the editor cwd for tools. Zed may send a Windows cwd while
    the ACP process runs in WSL; tools need the WSL mount or subprocess creation fails."""
    if not task_id:
        return
    try:
        from tools.terminal_tool import register_task_env_overrides
        register_task_env_overrides(task_id, {"cwd": _translate_acp_cwd(cwd)})
    except Exception:
        logger.debug("Failed to register ACP task cwd override", exc_info=True)


def _expand_acp_enabled_toolsets(toolsets: List[str] | None = None,
                                 mcp_server_names: List[str] | None = None) -> List[str]:
    """Return ACP toolsets plus explicit MCP server toolsets for this session."""
    names = [n for n in (toolsets or ["hermes-acp"]) if n]
    names += [f"mcp-{s}" for s in (mcp_server_names or []) if s]
    return list(dict.fromkeys(names))


def _parse_model_config(mc: Any) -> dict:
    """Decode a persisted model_config JSON blob; ``{}`` when absent/invalid/non-dict."""
    try:
        meta = json.loads(mc) if mc else None
    except (json.JSONDecodeError, TypeError):
        meta = None
    return meta if isinstance(meta, dict) else {}


def _session_info(sid: str, cwd: str, model: Any, history_len: int, title: Any, preview: Any,
                  updated_at: Any) -> Dict[str, Any]:
    return {"session_id": sid, "cwd": cwd, "model": model, "history_len": history_len,
            "title": _build_session_title(title, preview, cwd), "updated_at": _format_updated_at(updated_at)}


def _first_user_preview(history: List[Dict[str, Any]], default: str) -> str:
    return next((str(m.get("content") or "").strip() for m in history
                 if m.get("role") == "user" and str(m.get("content") or "").strip()), default)


@dataclass
class SessionState:
    """Tracks per-session state for an ACP-managed Hermes agent."""

    session_id: str
    agent: Any  # AIAgent instance
    cwd: str = "."
    model: str = ""
    history: List[Dict[str, Any]] = field(default_factory=list)
    cancel_event: Any = None  # threading.Event
    is_running: bool = False
    queued_prompts: List[str] = field(default_factory=list)
    runtime_lock: Any = field(default_factory=threading.Lock)
    current_prompt_text: str = ""
    interrupted_prompt_text: str = ""
    # Session this one's history was COPIED from (``fork_session`` only). Its row must inherit that
    # parent's required-context lineage: a fork is born with the parent's turns, so pinning the current
    # generation onto them would be a lineage lie. None for an ordinary session, which starts its own.
    parent_session_id: Optional[str] = None
    # Per-session allocator for ACP assistant messageIds (lazily created by
    # the server so streamed chunks group into distinct assistant replies).
    message_ids: Any = None


class SessionManager:
    """Thread-safe manager for ACP sessions backed by Hermes AIAgent instances.

    Sessions are held in-memory for fast access **and** persisted to the shared
    SessionDB so they survive restarts and are searchable via ``session_search``."""

    def __init__(self, agent_factory=None, db=None):
        """``agent_factory``: AIAgent-like factory (tests); default builds a real AIAgent from
        the runtime provider config. ``db``: SessionDB; default lazily opens ``~/.hermes/state.db``."""
        self._sessions: Dict[str, SessionState] = {}
        self._lock = threading.Lock()
        self._agent_factory = agent_factory
        self._db_instance = db  # None → lazy-init on first use

    # ---- public API ---------------------------------------------------------

    def create_session(self, cwd: str = ".") -> SessionState:
        """Create a new session with a unique ID and a fresh AIAgent."""
        cwd = _translate_acp_cwd(cwd)
        session_id = str(uuid.uuid4())
        agent = self._make_agent(session_id=session_id, cwd=cwd)
        state = self._install_state(session_id, agent, cwd, getattr(agent, "model", "") or "", [])
        logger.info("Created ACP session %s (cwd=%s)", session_id, cwd)
        return state

    def get_session(self, session_id: str) -> Optional[SessionState]:
        """Return the session, transparently restoring it from the DB (e.g. after
        a process restart) when it is not in memory; ``None`` if unknown."""
        with self._lock:
            state = self._sessions.get(session_id)
        return state if state is not None else self._restore(session_id)

    def fork_session(self, session_id: str, cwd: str = ".") -> Optional[SessionState]:
        """Deep-copy a session's history into a new session."""
        cwd = _translate_acp_cwd(cwd)
        original = self.get_session(session_id)  # checks DB too
        if original is None:
            return None
        new_id = str(uuid.uuid4())
        # BEFORE the agent exists: an agent built against a missing row pins the CURRENT generation
        # (``initialize_required_context_lineage``'s no-row branch) and writes it on its first turn — onto
        # the parent's copied history. With the row already carrying the parent's lineage, that same agent
        # restores it instead. No-op (and byte-identical to before) while the guard is off.
        self._precreate_fork_row(new_id, original, cwd)
        agent = self._make_agent(session_id=new_id, cwd=cwd, model=original.model or None)
        model = getattr(agent, "model", original.model) or original.model
        state = self._install_state(new_id, agent, cwd, model, copy.deepcopy(original.history),
                                    parent_session_id=session_id)
        logger.info("Forked ACP session %s -> %s", session_id, new_id)
        return state

    def _precreate_fork_row(self, new_id: str, original: SessionState, cwd: str) -> None:
        """Create a fork's row up front carrying the parent's pinned lineage, while the guard is on.

        Raises ``RequiredContextError`` when the parent has no pinned snapshot — the fork cannot inherit
        one and must not invent one, so the fork fails instead of producing a mispinned child.

        No ``config=``/``config_for_profile_home`` here, unlike the multiplexed gateway's branch paths: an
        ACP server is single-profile by construction — ``_get_db`` resolves ONE ``get_hermes_home()``
        state.db for the whole process (see its docstring) and ``_make_agent`` loads that same home's
        config, so the launch profile IS the session's profile and a bare read cannot answer for another."""
        from agent.required_context import LINEAGE_METADATA_KEY, branch_lineage_metadata, required_context_enabled
        if not required_context_enabled():
            return
        db = self._get_db()
        if db is None:
            return
        lineage = branch_lineage_metadata(db, original.session_id)
        if lineage is None:
            return
        db.create_session(session_id=new_id, source="acp", model=str(original.model) if original.model else None,
                          parent_session_id=original.session_id,
                          # ``_branched_from`` is the durable branch marker every other fork site writes
                          # (api_server, CLI /branch, desktop): without it ``_LISTABLE_CHILD_SQL`` drops the
                          # fork from list_sessions_rich(include_children=False) and ``_ephemeral_child_sql``
                          # re-reads it as a subagent run, so it looks DELETED after a restart.
                          model_config={"cwd": cwd, "_branched_from": original.session_id,
                                        LINEAGE_METADATA_KEY: lineage})

    def list_sessions(self, cwd: str | None = None) -> List[Dict[str, Any]]:
        """Return lightweight info dicts for all sessions (memory + database)."""
        normalized_cwd = _normalize_cwd_for_compare(cwd) if cwd else None
        db = self._get_db()
        persisted_rows: dict[str, dict[str, Any]] = {}
        try:
            for row in (db.list_sessions_rich(source="acp", limit=1000) if db is not None else ()):
                persisted_rows[str(row["id"])] = dict(row)
        except Exception:
            logger.debug("Failed to load ACP sessions from DB", exc_info=True)

        def _matches(session_cwd: str) -> bool:
            return not normalized_cwd or _normalize_cwd_for_compare(session_cwd) == normalized_cwd

        # In-memory sessions first.
        with self._lock:
            seen_ids = set(self._sessions.keys())
            results = []
            for s in self._sessions.values():
                if not s.history or not _matches(s.cwd):
                    continue
                persisted = persisted_rows.get(s.session_id, {})
                results.append(_session_info(
                    s.session_id, s.cwd, s.model, len(s.history), persisted.get("title"),
                    _first_user_preview(s.history, persisted.get("preview") or ""),
                    persisted.get("last_active") or persisted.get("started_at") or time.time(),
                ))

        # Then persisted sessions not currently in memory.
        for sid, row in persisted_rows.items():
            message_count = int(row.get("message_count") or 0)
            session_cwd = _parse_model_config(row.get("model_config")).get("cwd", ".")
            if sid in seen_ids or message_count <= 0 or not _matches(session_cwd):
                continue
            results.append(_session_info(
                sid, session_cwd, row.get("model") or "", message_count, row.get("title"),
                row.get("preview"), row.get("last_active") or row.get("started_at"),
            ))

        results.sort(key=lambda item: _updated_at_sort_key(item.get("updated_at")), reverse=True)
        return results

    def update_cwd(self, session_id: str, cwd: str) -> Optional[SessionState]:
        """Update the working directory for a session and its tool overrides."""
        cwd = _translate_acp_cwd(cwd)
        state = self.get_session(session_id)  # checks DB too
        if state is None:
            return None
        state.cwd = cwd
        _register_task_cwd(session_id, cwd)
        self._persist(state)
        return state

    def save_session(self, session_id: str) -> None:
        """Persist a session; called by the server after prompt completion,
        history-mutating slash commands, and model switches."""
        with self._lock:
            state = self._sessions.get(session_id)
        if state is not None:
            self._persist(state)

    # ---- persistence via SessionDB ------------------------------------------

    def _install_state(self, session_id: str, agent: Any, cwd: str, model: str,
                       history: List[Dict[str, Any]], *, persist: bool = True,
                       parent_session_id: Optional[str] = None) -> SessionState:
        """Build a SessionState, register it in memory, bind its cwd for tools, optionally persist."""
        state = SessionState(session_id=session_id, agent=agent, cwd=cwd, model=model,
                             history=history, cancel_event=threading.Event(),
                             parent_session_id=parent_session_id)
        with self._lock:
            self._sessions[session_id] = state
        _register_task_cwd(session_id, cwd)
        if persist:
            self._persist(state)
        return state

    def _get_db(self):
        """Lazily acquire the process-shared SessionDB; ``None`` if unavailable (e.g. import
        error in a minimal test env). ``HERMES_HOME`` is resolved here, not via the import-time
        ``DEFAULT_DB_PATH``, so test fixtures that change the env var later are honoured. The
        registry handle is the one in-process tools (delegation, session_search, goals) also
        acquire, so the ACP server holds ONE writer on state.db instead of two (#100896)."""
        if self._db_instance is None:
            try:
                from hermes_state_registry import acquire
                self._db_instance = acquire(get_hermes_home() / "state.db")
            except Exception:
                logger.debug("SessionDB unavailable for ACP persistence", exc_info=True)
        return self._db_instance

    @staticmethod
    def _persist_lineage(db, state: SessionState):
        """Required-context lineage for a non-pristine ACP row's INSERT; None while the guard is off.

        A fork INHERITS its parent's (raising when the parent has none, rather than inventing one); any
        other row starts its own lineage at the current generation. Single-profile, so no ``config=`` —
        see ``_precreate_fork_row``."""
        from agent.required_context import branch_lineage_metadata, new_lineage_metadata
        if state.parent_session_id:
            return branch_lineage_metadata(db, state.parent_session_id)
        return new_lineage_metadata()

    def _persist(self, state: SessionState) -> None:
        """Create/update the session record, then sync the live message set."""
        db = self._get_db()
        if db is None:
            return

        # Ensure model is a plain string (not a MagicMock or other proxy).
        model_str = str(state.model) if state.model else None
        session_meta = {"cwd": state.cwd}
        for key in ("provider", "base_url", "api_mode"):
            value = getattr(state.agent, key, None)
            if isinstance(value, str) and value.strip():
                session_meta[key] = value.strip()
        # The durable branch marker, on BOTH the create and the update path (the update replaces
        # model_config wholesale, so omitting it here would delete it on the fork's first save) — and
        # regardless of the required-context guard, which this has nothing to do with. Without it
        # ``_LISTABLE_CHILD_SQL`` drops the fork from list_sessions_rich(include_children=False) and
        # ``_ephemeral_child_sql`` re-reads it as a subagent run: the fork looks deleted after a restart.
        # Re-written from ``state`` rather than preserved from the row, so a fork row minted before this
        # existed gains the marker on its next save; ``_restore`` refills ``state`` from the row.
        if state.parent_session_id:
            session_meta["_branched_from"] = state.parent_session_id

        from agent.required_context import LINEAGE_METADATA_KEY
        try:
            if db.get_session(state.session_id) is None:
                if not state.history:
                    # Empty editor probes stay ephemeral; copied fork history persists.
                    return
                # This row is born WITH history, so it is never pristine and the agent's pristine pin can
                # never fire on it: its lineage has to be in the INSERT. A fork inherits its parent's (its
                # turns are the parent's); anything else — a session whose own first turns got here before
                # the agent's lazy create — starts its own lineage at the current generation.
                if (lineage := self._persist_lineage(db, state)) is not None:
                    session_meta[LINEAGE_METADATA_KEY] = lineage
                db.create_session(session_id=state.session_id, source="acp", model=model_str,
                                  parent_session_id=state.parent_session_id,
                                  model_config=session_meta)
            else:
                try:
                    # update_session_meta REPLACES model_config wholesale, and session_meta holds only
                    # cwd/provider/base_url/api_mode — so every save used to delete the pinned lineage, and
                    # the next turn on that session failed closed with "no pinned snapshot". Re-read the key
                    # inside the write transaction (same contract the desktop gateway's runtime persist uses).
                    db.update_session_meta_preserving_keys(
                        state.session_id, session_meta, model_str, preserve_keys=(LINEAGE_METADATA_KEY,))
                except Exception:
                    logger.debug("Failed to update ACP session metadata", exc_info=True)

            # An agent that owns persistence to this same DB already flushed the transcript
            # incrementally (append_message) and keeps pre-compaction turns as archived
            # active=0 rows; replace_messages() would DELETE those (and, after a compression
            # id rotation, clobber the ended parent transcript). Skip it in that case.
            # Calling replace_messages() here would then be a redundant double-write that DELETEs exactly
            # those archived rows (and, after a compression-driven id rotation where agent.session_id no
            # longer equals state.session_id, clobbers the ended parent transcript) — silent data loss for
            # any ACP conversation long enough to compress. Only fall back to the destructive atomic replace
            # when the agent is NOT persisting itself to this DB (e.g. a test agent factory, or a fresh
            # create/fork whose copied history the agent has not flushed yet). That path still rolls back on
            # a mid-rewrite failure so the previously persisted conversation survives (salvaged from
            # #13675).
            agent = state.agent
            if getattr(agent, "_session_db", None) is db and getattr(agent, "_session_db_created", False):
                return
            # A non-owning agent (model switch, /restore: fresh agent, _session_db_created=False)
            # may still sit on archived rows, so replace ONLY the active=1 set: on a fresh
            # create/fork every row is active (== full replace), and archived rows survive.
            # Unconditional because an existence probe would fail OPEN on DB error and can
            # race a concurrent archive_and_compact. Still rolls back on mid-rewrite failure.
            db.replace_messages(state.session_id, state.history, active_only=True)
        except Exception:
            logger.warning("Failed to persist ACP session %s", state.session_id, exc_info=True)

    def _restore(self, session_id: str) -> Optional[SessionState]:
        """Load an ACP session from the database into memory, recreating the AIAgent."""
        db = self._get_db()
        if db is None:
            return None
        try:
            row = db.get_session(session_id)
        except Exception:
            logger.debug("Failed to query DB for ACP session %s", session_id, exc_info=True)
            return None
        if row is None or row.get("source") != "acp":
            return None

        meta = _parse_model_config(row.get("model_config"))
        cwd, model = meta.get("cwd", "."), row.get("model") or None

        # repair_alternation: this list becomes the resumed agent's LIVE conversation; a durable
        # ``user;user`` violation in state.db would otherwise re-fire the pre-request repair every request.
        try:
            history = db.get_messages_as_conversation(session_id, repair_alternation=True)
        except Exception:
            logger.warning("Failed to load messages for ACP session %s", session_id, exc_info=True)
            history = []

        try:
            agent = self._make_agent(
                session_id=session_id, cwd=cwd, model=model, api_mode=meta.get("api_mode") or None,
                requested_provider=meta.get("provider") or row.get("billing_provider"),
                base_url=meta.get("base_url") or row.get("billing_base_url"))
        except Exception:
            logger.warning("Failed to recreate agent for ACP session %s", session_id, exc_info=True)
            return None
        # A restored fork keeps its parent: the next ``save_session`` rebuilds model_config from
        # ``session_meta``, and without this the ``_branched_from`` marker would be dropped there and the
        # fork would disappear from the listings again. Taken from the MARKER, never from the
        # ``parent_session_id`` column: that column is also set on compression continuations and
        # subagent runs, and stamping ``_branched_from`` onto one of those would make
        # ``_LISTABLE_CHILD_SQL`` show it as a separate conversation beside its own parent.
        state = self._install_state(session_id, agent, cwd, model or getattr(agent, "model", "") or "",
                                    history, persist=False, parent_session_id=meta.get("_branched_from"))
        logger.info("Restored ACP session %s from DB (%d messages)", session_id, len(history))
        return state

    # ---- internal -----------------------------------------------------------

    def _make_agent(self, *, session_id: str, cwd: str, model: str | None = None,
                    requested_provider: str | None = None, base_url: str | None = None, api_mode: str | None = None):
        if self._agent_factory is not None:
            return self._agent_factory()

        from run_agent import AIAgent
        from hermes_cli.config import load_config
        from hermes_cli.runtime_provider import resolve_runtime_provider

        config = load_config()
        model_cfg = config.get("model")
        default_model, config_provider = "", None
        if isinstance(model_cfg, dict):
            default_model, config_provider = str(model_cfg.get("default") or ""), model_cfg.get("provider")
        elif isinstance(model_cfg, str):
            default_model = model_cfg.strip()

        configured_mcp_servers = [
            name for name, cfg in (config.get("mcp_servers") or {}).items()
            if not isinstance(cfg, dict) or cfg.get("enabled", True) is not False
        ]
        kwargs = {
            "platform": "acp", "quiet_mode": True, "session_id": session_id, "session_db": self._get_db(),
            "enabled_toolsets": _expand_acp_enabled_toolsets(["hermes-acp"], mcp_server_names=configured_mcp_servers),
            "model": model or default_model,
            "cwd": cwd,
        }
        try:
            runtime = resolve_runtime_provider(requested=requested_provider or config_provider)
            kwargs.update({
                "provider": runtime.get("provider"), "api_mode": api_mode or runtime.get("api_mode"),
                "base_url": base_url or runtime.get("base_url"), "api_key": runtime.get("api_key"),
                "command": runtime.get("command"), "args": list(runtime.get("args") or []),
            })
        except Exception:
            logger.debug("ACP session falling back to default provider resolution", exc_info=True)

        _register_task_cwd(session_id, cwd)

        # Bounded wait for the background MCP discovery started by entry.py: the agent
        # snapshots tools once at build and never re-reads the registry, so without this
        # join a slow-but-reachable server would be invisible all session. ensure_* also
        # (re)starts discovery if the entry spawn never ran or connected zero servers.
        # Bounded by ``mcp_discovery_timeout`` (config.yaml, ~1.5s); late servers are
        # picked up by HermesACPAgent._schedule_mcp_late_refresh.
        try:
            from hermes_cli.mcp_startup import ensure_mcp_discovery_before_agent_build

            ensure_mcp_discovery_before_agent_build(logger=logger, thread_name="acp-mcp-discovery")
        except Exception:
            logger.debug("ACP: bounded MCP discovery wait failed", exc_info=True)

        agent = AIAgent(**kwargs)
        # ACP stdio: stdout is protocol-only JSON-RPC; agent chatter goes to stderr.
        agent._print_fn = _acp_stderr_print
        return agent


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
from threading import Lock  # noqa: F401,E402
# ---- END PLUGIN-COMPAT ----
