#!/usr/bin/env python3
"""Pin a Sakaan required-context lineage onto session rows that have none.

Why this exists
---------------
``required_context.enabled`` makes every turn resolve its session's pinned instruction snapshot.
A row that already has messages but no ``_required_context_lineage`` can never be pinned by the
runtime — ``initialize_required_context_lineage`` only pins a PRISTINE row, and every other
lineage-less row goes to ``_restore_existing_lineage``, which fails closed with
``WORKFLOW_SOURCE_UNAVAILABLE: existing session has no pinned snapshot``. Sessions created while
the guard was OFF are all in that state, so turning it on would strand them.

The row classes (coordinator-measured, 2026-09-18)
--------------------------------------------------
``sessions.system_prompt`` is a write-NULL-only column in this tree: the real prompt is interned in
``system_prompts`` and only ever resolved through ``COALESCE(sp.prompt, s.system_prompt)``, which is
what ``SessionDB.get_session`` returns. Read that way, the unpinned rows fall into these classes and
this tool treats each differently:

* **Class 1 — no persisted prompt at all.** Pin the CURRENT generation. Honest: the row never
  carried a snapshot, so there is no earlier generation this could falsify, and the restore path's
  no-prompt branch accepts v1 metadata exactly as written.
* **Class 2 — the persisted prompt carries a block THIS code can reproduce.** Pin the generation
  **that block itself declares**, not today's pointer — the conversation really saw that snapshot.
  The block is only trusted after the named generation is rebuilt from disk and its ``prompt_block``
  matches the persisted one byte for byte.
* **Class 2b — the persisted prompt carries a block this code CANNOT reproduce.** Either the header
  is not the one ``_build_snapshot`` writes today (the 2026-09 rows carry ``# Required Context
  Snapshot`` and a ``generation_sha256:`` line that the current emitter does not write at all), or
  the generation it declares still loads but no longer rebuilds to those bytes. No metadata can ever
  satisfy such a row: ``_restore_existing_lineage`` rebuilds the block with today's emitter and
  compares byte for byte, so the declared generation is unusable however it is recorded. The only
  truthful fix is the class-3 one — clear the prompt so the next build re-emits it around a block
  this code owns — so it needs the same ``--clear-stale-prompt`` authorization and is refused
  without it. The output names the declared generation being dropped.
* **Class 4 — already pinned, but the persisted prompt does not carry THAT lineage's block.** The row
  is not stranded, it is bricked: ``_restore_existing_lineage`` rebuilds the pinned block and compares
  it against the prompt, so every turn fails closed. Two routes reach it — a turn landing in the
  ``--apply``→flag-flip window, and the G1 flag rollback, which clears every block-carrying prompt and
  leaves each of those rows pinned-and-blockless for the next enable. The repair is the class-3 one
  applied to a row that already has its lineage: clear the stale prompt and leave the metadata EXACTLY
  as it stands — not re-pinned, not moved to today's generation — so the next build re-emits the block
  the row is already pinned to. Same act as class 3, so the same ``--clear-stale-prompt``
  authorization; without it the row is reported and refused, as before. A row whose pinned metadata
  cannot be restored AT ALL is a different thing and is never repaired this way: see
  ``PINNED_METADATA_INVALID`` below.
* **Class 3 — a persisted prompt with NO block.** Cannot be pinned truthfully without first clearing
  the stale prompt so the next build re-emits it around the block (what ``/model`` already does via
  ``update_system_prompt(sid, None)``). That is a second, wider act, so it lives behind
  ``--clear-stale-prompt``, is default-off, is NOT implied by ``--apply``, and whether it is ever
  used is the operator's decision.

Safety contract
---------------
* DRY RUN BY DEFAULT. ``--apply`` is the only thing that writes.
* ``--apply`` REFUSES while any other process still holds the database (or a WAL sidecar) open —
  i.e. while Hermes is running against it. Detected, not merely documented: an exclusive-share open
  on Windows, the ``/proc`` descriptor scan elsewhere, and a failed probe is itself a refusal.
* ``--apply`` REFUSES unless ``--backup <path>`` first produced a verified copy: taken through
  SQLite's ONLINE BACKUP API (the store runs in WAL, so copying the main file is not a consistent
  snapshot), then asserted non-empty, ``PRAGMA integrity_check = ok``, and holding at least the
  ``sessions`` row count the source had before the copy. A backup that fails part-way removes its
  own partial file so the same path can be retried.
* Every write RE-VALIDATES the row's resolved prompt inside its own transaction
  (``pin_session_model_config_key_if_prompt_unchanged``). A row that took a turn between the plan
  and the write is reported, never pinned — otherwise a row could end up pinned to a lineage its own
  prompt does not carry, which then fails closed on every later turn.
* Idempotent, and a re-run re-VERIFIES what it already pinned: an existing lineage is restored and
  checked against the row's prompt exactly as the runtime would. A pinned-but-unrestorable row is
  reported as needing attention and exits non-zero — it is never waved through as ``already_pinned`` —
  and under ``--clear-stale-prompt`` it is repaired in place (class 4) instead of only being reported.
* Any refusal, any per-row failure, and any skip that is not simply "already pinned and healthy"
  exits non-zero.

Usage
-----
    python scripts/backfill_required_context_lineage.py --db ~/.hermes/state.db
    # then, with Hermes CLOSED:
    python scripts/backfill_required_context_lineage.py --db ~/.hermes/state.db \
        --backup ~/.hermes/backups/state.db.pre-lineage --apply
    ... --session-id 20260908_141233_ab12ef      # limit scope; repeatable
    ... --clear-stale-prompt                     # classes 2b, 3 and 4 only; separately authorized
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

# Exit codes: distinct so a wrapper can tell "nothing to do" from "refused" from "a row failed".
EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_ROW_FAILED = 3

# Row classes the plan pins (see the module docstring).
CLASS_NO_PROMPT = "class1_no_persisted_prompt"
CLASS_OWN_BLOCK = "class2_prompt_block_pins_its_own_generation"
CLASS_LEGACY_BLOCK = "class2b_legacy_block_dropped_then_pinned"
CLASS_STALE_PROMPT = "class3_stale_prompt_cleared_then_pinned"
# Class 4 repairs rather than pins: the lineage is already there and is left byte-identical.
CLASS_PINNED_STALE_PROMPT = "class4_pinned_row_stale_prompt_cleared_lineage_kept"

# Benign skip — the only reason that does NOT make the run exit non-zero.
ALREADY_PINNED = "already_pinned"

# Blocking skips: a row the backfill could not make safe. Reported, and non-zero exit.
CLASS_LEGACY_BLOCK_REFUSED = "class2b_block_this_code_cannot_reproduce_needs_--clear-stale-prompt"
CLASS_STALE_PROMPT_REFUSED = "class3_stale_prompt_without_block_needs_--clear-stale-prompt"
PINNED_BUT_UNRESTORABLE = "pinned_lineage_does_not_match_the_persisted_prompt"
# ...repairable under --clear-stale-prompt (class 4). PINNED_METADATA_INVALID is NOT: clearing the
# prompt would not help it, because the restore path rejects the metadata itself before it ever looks
# at a prompt. Such a row is always reported and refused, with or without the flag.
PINNED_METADATA_INVALID = "pinned_lineage_metadata_is_invalid"
BLOCK_GENERATION_UNAVAILABLE = "class2_declared_generation_could_not_be_loaded"
PROMPT_BLOCK_MALFORMED = "prompt_has_duplicate_or_unbalanced_block_markers"
MODEL_CONFIG_UNPARSEABLE = "model_config_unparseable"

# Why a block landed in class 2b; carried inside the two class-2b reasons above.
LEGACY_HEADER_UNRECOGNIZED = "legacy_or_unrecognized_block_header"
LEGACY_BLOCK_MISMATCH = "block_is_not_what_that_generation_rebuilds_to"

# Write-time failures.
WRITE_REFUSED = "store_refused_the_write"
WRITE_PROMPT_CHANGED = "row_took_a_turn_between_the_plan_and_the_write"
WRITE_RACED = "another_writer_pinned_a_lineage_first"
WRITE_LINEAGE_CHANGED = "the_pinned_lineage_changed_between_the_plan_and_the_write"


class Refused(RuntimeError):
    """A precondition failed; nothing was written."""


@dataclass(frozen=True)
class Plan:
    """What this run intends to do to one row, and the named reason for it."""
    session_id: str
    messages: int
    reason: str
    pin: bool = False
    metadata: Optional[dict] = None
    expected_prompt: Optional[str] = None
    clear_prompt: bool = False
    # Class 4: clear the stale prompt and keep the lineage the row already holds, byte for byte.
    repair: bool = False
    expected_metadata: Optional[dict] = None

    @property
    def writes(self) -> bool:
        """This plan intends a write — either a pin or a class-4 repair."""
        return self.pin or self.repair

    @property
    def blocking(self) -> bool:
        """A skip that needs attention (i.e. anything but a healthy already-pinned row)."""
        return not self.writes and self.reason != ALREADY_PINNED


def _enabled_config() -> dict:
    """The active config with ``required_context.enabled`` forced on, for loader/restore calls.

    Deliberately not gated on the live flag: the backfill is what makes enabling it safe, so it has
    to run while the flag is still off. Everything else — the canonical pointer, the reparse-point
    rejection, the five-file order, the digests — is the guard's own, unchanged.
    """
    from agent.required_context import _config, _section

    cfg = dict(_config(None))
    section = dict(cfg.get("required_context") or {})
    if not section:
        raise Refused("config has no required_context section to read the pointer and file list from")
    _section({"required_context": section})  # canonical pointer + five-file order, before enabling
    section["enabled"] = True
    cfg["required_context"] = section
    return cfg


def _load_snapshot(cfg: dict):
    """The CURRENT generation's snapshot, through the guard's own loader."""
    from agent.required_context import load_required_context_snapshot

    snapshot = load_required_context_snapshot(cfg)
    if snapshot is None:
        raise Refused("the current generation's snapshot could not be loaded")
    return snapshot


# ── precondition: nobody else is using this database ─────────────────────────────────────────────
def _windows_share_holders(paths) -> list[str]:
    """Reasons ``paths`` cannot be proved unused, via an exclusive-share ``CreateFileW``.

    Windows share modes are mandatory, so a zero ``dwShareMode`` open fails with
    ERROR_SHARING_VIOLATION exactly while some other handle is open — which is precisely "Hermes (or
    anything else) is running against this state.db". A missing sidecar is not a holder; every other
    failure is reported rather than assumed away.
    """
    import ctypes
    import ctypes.wintypes as wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
    ]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    invalid = ctypes.c_void_p(-1).value
    generic_read, open_existing = 0x80000000, 3
    not_found, path_not_found, sharing_violation = 2, 3, 32

    busy: list[str] = []
    for path in paths:
        handle = kernel32.CreateFileW(str(path), generic_read, 0, None, open_existing, 0, None)
        if handle is not None and handle != invalid:
            kernel32.CloseHandle(handle)
            continue
        code = ctypes.get_last_error()
        if code in (not_found, path_not_found):
            continue
        if code == sharing_violation:
            busy.append(f"{path} is held open by another process (ERROR_SHARING_VIOLATION)")
        else:
            busy.append(f"{path} could not be proved unused (WinError {code})")
    return busy


def _require_no_live_holder(db_path: Path) -> None:
    """Refuse while any other process still holds this database or a WAL sidecar open.

    This is the checked form of "stop Hermes first". Without it the backfill races the running app:
    a turn that persists a blockless system prompt between the plan and the write would leave the
    row pinned-but-blockless, and every later turn on it would then fail closed. The per-row write
    re-validates the prompt as a second line of defence, but a quiescent database is the first.
    """
    paths = [db_path, Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")]
    if sys.platform == "win32":
        busy = _windows_share_holders(paths)
    else:
        from hermes_state_holders import foreign_state_db_holders

        busy = [f"{db_path} is held by pid {pid} ({what})" for pid, what in foreign_state_db_holders(db_path)]
    if not busy:
        print("  no other process holds the database open")
        return
    # Deliberately no psutil ``open_files()`` sweep to name the holder: on Windows that walks every
    # handle of every process and takes minutes, and the refusal is already actionable without it.
    raise Refused(
        "the database is in use — close Hermes (desktop, gateway, CLI, ACP) and retry: " + "; ".join(busy))


def _backup(source: Path, dest: Path, expected: int) -> None:
    """Online-backup ``source`` to ``dest`` and verify the copy, or raise ``Refused``.

    ``sqlite3.Connection.backup`` is the only consistent way to copy this store: it runs in WAL, so
    the main file on its own is missing whatever is still in the -wal segment.

    ``expected`` is the source's ``sessions`` count read by the CALLER, before the copy, and the
    assertion is "the copy is not MISSING rows" (``got >= expected``) rather than exact equality.
    Exact equality cannot hold from either side of the copy: read after it, an insert that lands in
    between makes the source larger than the copy; read before it, an insert makes the copy larger
    than the reading. Neither means the backup lost anything, and only losing something matters here.

    A failure removes the file it created, so the same ``--backup`` path can be retried instead of
    being blocked forever by its own debris.
    """
    if dest.exists():
        raise Refused(f"backup path already exists, refusing to overwrite it: {dest}")
    created = False
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True)) as src_conn:
            created = True
            with contextlib.closing(sqlite3.connect(dest)) as dest_conn:
                src_conn.backup(dest_conn)

        size = dest.stat().st_size if dest.exists() else 0
        if size <= 0:
            raise Refused(f"backup is empty ({size} bytes): {dest}")
        with contextlib.closing(sqlite3.connect(f"file:{dest}?mode=ro", uri=True)) as check_conn:
            integrity = check_conn.execute("PRAGMA integrity_check").fetchone()[0]
            got = check_conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        if integrity != "ok":
            raise Refused(f"backup failed integrity_check ({integrity}): {dest}")
        if got < expected:
            raise Refused(f"backup holds {got} sessions, source had at least {expected}: {dest}")
        print(f"  backup verified: {dest} ({size} bytes, {got} sessions, integrity ok)")
    except BaseException:
        if created:
            # Leave no debris: `dest.exists()` above refuses any retry at this path otherwise.
            for leftover in (dest, Path(str(dest) + "-wal"), Path(str(dest) + "-shm")):
                with contextlib.suppress(OSError):
                    leftover.unlink()
        raise


def _read_only(db_path: Path):
    return contextlib.closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True))


def _session_count(db_path: Path) -> int:
    with _read_only(db_path) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])


def _session_ids(db_path: Path, only: Optional[list[str]]) -> list[str]:
    """Every session id, oldest first — a plain read, so it goes over a read-only connection rather
    than taking the store's writer. ``--session-id`` narrows it, and an id that does not exist is a
    refusal rather than a silent no-op."""
    with _read_only(db_path) as conn:
        known = [row[0] for row in conn.execute("SELECT id FROM sessions ORDER BY started_at, id")]
    if only is None:
        return known
    if missing := [sid for sid in only if sid not in set(known)]:
        raise Refused(f"--session-id not found in this database: {', '.join(missing)}")
    return [sid for sid in known if sid in set(only)]


# ── planning ─────────────────────────────────────────────────────────────────────────────────────
def _row_model_config(row: dict) -> Optional[dict]:
    """The row's parsed ``model_config``, or None when it is present but unparseable."""
    raw = row.get("model_config")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return {}


def _pinned_health(metadata: Any, prompt: str, cfg: dict) -> str:
    """``ALREADY_PINNED`` when the runtime would restore this row, else the blocking reason.

    The whole point of re-checking: a row pinned in an earlier run that has since persisted a prompt
    without the block is BRICKED, and reporting it as ``already_pinned`` with exit 0 would say clean
    over a row that fails closed on every turn."""
    from agent.required_context import (
        RequiredContextError, extract_required_context_block, restore_required_context_lineage,
    )
    try:
        snapshot = restore_required_context_lineage(metadata, config=cfg)
    except RequiredContextError:
        return PINNED_METADATA_INVALID
    if not prompt:
        return ALREADY_PINNED  # the restore path's no-prompt branch rebuilds around this block
    try:
        block = extract_required_context_block(prompt)
    except RequiredContextError:
        return PINNED_BUT_UNRESTORABLE
    return ALREADY_PINNED if block == snapshot.prompt_block else PINNED_BUT_UNRESTORABLE


@dataclass(frozen=True)
class BlockVerdict:
    """What the required-context block persisted on a row turned out to be."""
    metadata: Optional[dict] = None  # class 2: the lineage that block's own generation vouches for
    reason: str = ""                 # the named detail, for class 2b and for a hard refusal
    legacy: bool = False             # class 2b: this code cannot reproduce that block
    declared: Optional[str] = None   # the generation the block declared, when it declared one


def _classify_block(block: str, cfg: dict, cache: dict) -> BlockVerdict:
    """Class 2, class 2b, or a hard refusal, for a row whose persisted prompt carries a block.

    Class 2 is the byte-verified case: the generation the block itself declares is rebuilt from disk
    and its ``prompt_block`` equals the persisted block byte for byte — which also proves the five
    files have not changed under it, since the block embeds their full text and digests.

    ``legacy`` marks class 2b, a block this code cannot reproduce, so NO metadata could satisfy it:
    ``_restore_existing_lineage`` rebuilds the block with today's emitter and compares byte for byte.
    Two shapes reach it — a header the current ``_build_snapshot`` does not write (the 2026-09 rows
    carry ``# Required Context Snapshot`` plus a ``generation_sha256:`` line), and a declared
    generation that loads but no longer rebuilds to these bytes. Both are cleared, never pinned as
    they are. A generation that simply could not be READ is deliberately NOT class 2b: that can be a
    transient or offline directory, and clearing the prompt over it would destroy the only surviving
    record of what the conversation saw.
    """
    from agent.required_context import (
        RequiredContextError, declared_block_generation, load_generation_snapshot, snapshot_to_metadata,
    )
    declared = declared_block_generation(block)
    if not declared:
        return BlockVerdict(reason=LEGACY_HEADER_UNRECOGNIZED, legacy=True)
    if declared not in cache:
        try:
            cache[declared] = load_generation_snapshot(declared, config=cfg)
        except (RequiredContextError, OSError) as exc:
            cache[declared] = exc
    snapshot = cache[declared]
    if isinstance(snapshot, BaseException):
        return BlockVerdict(reason=f"{BLOCK_GENERATION_UNAVAILABLE} ({declared}: {snapshot})",
                            declared=declared)
    if snapshot.prompt_block != block:
        return BlockVerdict(reason=LEGACY_BLOCK_MISMATCH, legacy=True, declared=declared)
    return BlockVerdict(metadata=snapshot_to_metadata(snapshot), declared=declared)


def _unverified_block_generation(block: str) -> str:
    """The ``generation:`` line inside a block ``declared_block_generation`` refuses — FOR REPORTING.

    The legacy rows do name their generation; it is the header AROUND it that this code does not
    recognize. An operator clearing such a row should still see which generation is being dropped,
    so the value is read back here, labelled unverified, and never fed to a pin — which is exactly
    why ``declared_block_generation`` goes on refusing it.
    """
    for line in block.splitlines():
        if line.startswith("generation: ") and (value := line[len("generation: "):].strip()):
            return f"{value} (unverified: read out of a block this code cannot parse)"
    return "none"


def _plan_row(session_id: str, row: dict, current, cfg: dict, cache: dict, *, allow_clear: bool) -> Plan:
    """Classify one already-read session row into exactly one named class or blocking reason."""
    from agent.required_context import (
        LINEAGE_METADATA_KEY, RequiredContextError, extract_required_context_block, snapshot_to_metadata,
    )

    messages = int(row.get("message_count") or 0)

    def plan(reason: str, **kwargs) -> Plan:
        return Plan(session_id=session_id, messages=messages, reason=reason, **kwargs)

    config = _row_model_config(row)
    if config is None:
        return plan(MODEL_CONFIG_UNPARSEABLE)
    # ``get_session`` resolves the interned prompt (COALESCE(system_prompts.prompt, system_prompt)),
    # so a row whose own column is NULL but whose hash points at a stored prompt is handled here too.
    prompt = row.get("system_prompt") or ""
    if (existing := config.get(LINEAGE_METADATA_KEY)) is not None:
        health = _pinned_health(existing, prompt, cfg)
        if health != PINNED_BUT_UNRESTORABLE or not allow_clear:
            # Healthy, or a row this run may not touch: reported exactly as before. That deliberately
            # includes PINNED_METADATA_INVALID even under the flag — clearing a prompt cannot repair
            # metadata the restore path rejects on its own, and doing it anyway would destroy the
            # prompt that is the only remaining evidence of what such a row actually carried.
            return plan(health)
        # Class 4. The lineage restores; it is the PROMPT that is stale. Clear it and leave the
        # metadata untouched, so the next build re-emits the block this row is already pinned to.
        return plan(CLASS_PINNED_STALE_PROMPT, repair=True, expected_prompt=prompt,
                    expected_metadata=existing)

    if not prompt:
        # Class 1. Nothing can disagree with the metadata, and the next turn builds the prompt around
        # this block; the restore path's no-prompt branch accepts v1 metadata exactly as it stands.
        return plan(CLASS_NO_PROMPT, pin=True, metadata=snapshot_to_metadata(current))
    try:
        block = extract_required_context_block(prompt)
    except RequiredContextError:
        return plan(PROMPT_BLOCK_MALFORMED)
    if block is not None:
        verdict = _classify_block(block, cfg, cache)
        if verdict.metadata is not None:
            # Class 2 — pin the generation this conversation actually saw, not today's pointer.
            return plan(CLASS_OWN_BLOCK, pin=True, metadata=verdict.metadata, expected_prompt=prompt)
        if not verdict.legacy:
            return plan(verdict.reason)
        # Class 2b — a block this code cannot reproduce, so it can only be dropped, never pinned as
        # it stands. Same act as class 3, and therefore the same separate authorization.
        prior = f"declared generation: {verdict.declared or _unverified_block_generation(block)}"
        if not allow_clear:
            return plan(f"{CLASS_LEGACY_BLOCK_REFUSED} ({verdict.reason}; {prior})")
        return plan(f"{CLASS_LEGACY_BLOCK} ({verdict.reason}; dropping {prior})", pin=True,
                    metadata=snapshot_to_metadata(current), expected_prompt=prompt, clear_prompt=True)
    # Class 3 — a stale prompt with no block. Pinning it as-is would leave metadata and prompt
    # disagreeing and the row would fail closed anyway; the only truthful fix is to clear the prompt
    # so the next build re-emits it, and that is a separately authorized act.
    if not allow_clear:
        return plan(CLASS_STALE_PROMPT_REFUSED)
    return plan(CLASS_STALE_PROMPT, pin=True, metadata=snapshot_to_metadata(current),
                expected_prompt=prompt, clear_prompt=True)


# ── the run ──────────────────────────────────────────────────────────────────────────────────────
def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Pin a Sakaan required-context lineage onto session rows that have none.")
    parser.add_argument("--db", required=True, type=Path, help="state.db to read (and, with --apply, write)")
    parser.add_argument("--apply", action="store_true",
                        help="actually write; without it this is a dry run and nothing is modified")
    parser.add_argument("--backup", type=Path,
                        help="destination for the verified online backup --apply requires")
    parser.add_argument("--session-id", action="append", dest="session_ids",
                        help="limit to this session id (repeatable)")
    parser.add_argument("--clear-stale-prompt", action="store_true", dest="clear_stale_prompt",
                        help="ALSO clear a stale system prompt so the next build re-emits it (what /model "
                             "already does): a class-3 row's blockless prompt, a class-2b row's block "
                             "this code cannot reproduce, and a class-4 row that is already pinned but "
                             "whose prompt no longer carries its own pinned block (repaired in place, "
                             "its lineage left byte-identical). Separately authorized; never implied by "
                             "--apply. Without it class-2b, class-3 and class-4 rows are refused.")
    args = parser.parse_args(argv)

    from agent.required_context import LINEAGE_METADATA_KEY, RequiredContextError
    from hermes_state import SessionDB

    try:
        db_path = args.db.expanduser()
        if not db_path.is_file():
            raise Refused(f"no such database: {db_path}")
        cfg = _enabled_config()
        snapshot = _load_snapshot(cfg)
        print(f"current generation: {snapshot.generation}")
        print(f"prompt_sha256: {snapshot.prompt_sha256}")
        print(f"mode: {'APPLY (writes)' if args.apply else 'DRY RUN (no writes)'}")
        print("class 2b/3 stale-prompt clearing: "
              f"{'ENABLED' if args.clear_stale_prompt else 'off (refuses)'}")

        if args.apply:
            # Both preconditions of writing, not options beside it.
            if args.backup is None:
                raise Refused("--apply requires --backup <path>; refusing to write without a verified backup")
            _require_no_live_holder(db_path)
            _backup(db_path, args.backup.expanduser(), _session_count(db_path))
        elif args.backup is not None:
            print("  note: --backup is ignored in a dry run (nothing is written, so nothing to back up)")

        ids = _session_ids(db_path, args.session_ids)
    except (Refused, RequiredContextError, OSError, sqlite3.Error) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    cache: dict[str, Any] = {}
    plans: list[Plan] = []
    written, repaired, failed = [], [], []

    with SessionDB(db_path=db_path) as db:
        for session_id in ids:
            row = db.get_session(session_id)
            if row is None:  # deleted between the id read and here
                continue
            try:
                plan = _plan_row(session_id, row, snapshot, cfg, cache,
                                 allow_clear=args.clear_stale_prompt)
            except (RequiredContextError, OSError, sqlite3.Error) as exc:
                plan = Plan(session_id=session_id, messages=int(row.get("message_count") or 0),
                            reason=f"row could not be classified ({exc})")
            plans.append(plan)
            label = f"{session_id}  messages={plan.messages}  {plan.reason}"
            if not plan.writes:
                print(f"  {'SKIP ' if plan.reason == ALREADY_PINNED else 'BLOCK'} {label}")
                continue
            if not args.apply:
                print(f"  PLAN  {label}")
                continue
            if plan.repair:
                # Class 4: only the prompt is written. The lineage is re-checked in the same
                # transaction and left exactly as it stands, so the row is repaired, never re-pinned.
                status = db.clear_session_prompt_if_lineage_unchanged(
                    session_id, LINEAGE_METADATA_KEY, plan.expected_metadata,
                    expected_prompt=plan.expected_prompt or "")
                if status == "cleared":
                    repaired.append(session_id)
                    print(f"  CLEAR {label}")
                    continue
                reason = {
                    "prompt_changed": WRITE_PROMPT_CHANGED, "lineage_changed": WRITE_LINEAGE_CHANGED,
                }.get(status, f"{WRITE_REFUSED} ({status})")
                failed.append((session_id, reason))
                print(f"  FAIL  {label} -> {reason}")
                continue
            status = db.pin_session_model_config_key_if_prompt_unchanged(
                session_id, LINEAGE_METADATA_KEY, plan.metadata,
                expected_prompt=plan.expected_prompt, clear_prompt=plan.clear_prompt)
            if status == "pinned":
                written.append(session_id)
                print(f"  PIN   {label}")
                continue
            reason = {
                "prompt_changed": WRITE_PROMPT_CHANGED, "already_pinned": WRITE_RACED,
            }.get(status, f"{WRITE_REFUSED} ({status})")
            failed.append((session_id, reason))
            print(f"  FAIL  {label} -> {reason}")

    blocking = [(plan.session_id, plan.reason) for plan in plans if plan.blocking]
    planned = [plan for plan in plans if plan.pin]
    repairs = [plan for plan in plans if plan.repair]
    print(
        f"\n{len(plans)} sessions examined | {len(planned)} to pin | {len(written)} pinned | "
        f"{len(repairs)} to repair | {len(repaired)} repaired | "
        f"{len(plans) - len(planned) - len(repairs)} skipped ({len(blocking)} needing attention) | "
        f"{len(failed)} failed")
    for session_id, reason in blocking + failed:
        print(f"  needs attention: {session_id}: {reason}", file=sys.stderr)
    if failed:
        return EXIT_ROW_FAILED
    if blocking:
        return EXIT_REFUSED
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
