# Execution Map — `rewind-port-0200`

## Pin and FORM

- Workspace: `C:\hrp`
- Branch: `sakaan/rewind-port-0200-20260812`
- Immutable base: `ee472a7fdbbc55924f91ab122dbaa29bd07668b0`
- Spec series: `18319f3f4`, `fc063c47b`, `c9101a651`, `4d1f52ffb`, `4793eb531`, `597142813`
- FORM: **DECOMPOSED** — rewind can irreversibly destroy transcript rows, so identity/submit safety and the independent REST transcript producer are separated by a contract gate and independently revertible checkpoints.
- Planning only: this map authorizes no implementation, commit, merge, push, deploy, or ref mutation.

## Evidence and resolved open question

### Current 0.20.0 topology

The spec's `prompt.submit` body is no longer owned directly by `tui_gateway/server.py`. On the pinned base:

- `tui_gateway/methods_prompt.py:67-68` defines the live `@method("prompt.submit")` handler.
- `tui_gateway/server.py:14413-14428` imports and registers `methods_prompt` against the server namespace.
- `tui_gateway/methods_prompt.py:104` reads the legacy ordinal target; `tui_gateway/methods_prompt.py:157-221` validates and resolves it; `tui_gateway/methods_prompt.py:221` computes the retained prefix; and `tui_gateway/methods_prompt.py:264-286` persists the rewrite before changing memory.
- `tui_gateway/server.py:7136-7231` owns `_history_to_messages`, the gateway display projection.
- `tui_gateway/server.py:8166-8211` owns `_live_session_payload`; line 8199 currently emits unannotated `_history_to_messages(history)`.
- `hermes_cli/web_server.py:11194-11289` owns the REST session-detail area on the base. The spec adds rewind annotation immediately before the messages endpoint.

Therefore the port's production topology is six files, not the spec branch's five: the current extraction requires a focused modification to `tui_gateway/methods_prompt.py`. Pretending all submit integration still belongs in `server.py` would ship inert code.

### A5: no state.db schema change is required

**Resolved: rewind identity is computed from history; no migration or schema change is required.** Evidence:

- The final spec adds only the five frozen files and does not modify `hermes_state.py`; the new identity module computes IDs from message role/content/history (`597142813:tui_gateway/rewind_identity.py:132-165`, `208-323`).
- The pinned base already has the needed storage contract. `hermes_state.py:8187-8193` exposes `SessionDB.replace_messages(..., active_only=False, archive_dropped=False)`.
- `hermes_state.py:8205-8212` explicitly defines `active_only=True` as replacing live `active=1` rows while preserving existing soft-archived `active=0` rows.
- `hermes_state.py:8242-8257` implements this with either `UPDATE ... SET active=0 WHERE ... active=1` for `archive_dropped`, or a DELETE constrained by the active-only clause.
- The current submit call path already invokes `replace_messages(..., active_only=True, archive_dropped=True)` at `tui_gateway/methods_prompt.py:264-286`. The port must preserve that stronger 0.20.0 behavior while adding ID addressing; it must not regress to the older spec call's merely `active_only=True` form.
- `hermes_state.py:8854-8903` derives model and display projections from existing active message rows; no rewind identity column is read or stored.

**Risk-boundary answer:** no migration package, schema version bump, or write to `hermes_state.py` belongs in this build. Discovery of any required schema/storage API change during implementation is a HOLD and requires a new reviewed map because it expands the data-loss boundary.

## Global forbidden touches

These apply to every package, checkpoint, command, and reviewer:

- Do not enter, modify, or run mutating git commands in `C:\Users\HieuKa\AppData\Local\New Hermes\hermes-agent`.
- Do not touch `refs/heads/main`, tags matching `preupdate-20260812/*`, reland-gate hooks, or `C:\Users\HieuKa\AppData\Local\hermes\hermes-agent`.
- Do not touch the desktop repository or desktop source files.
- No network push, merge, deploy, `git push`, or `fetch --unshallow`.
- Do not modify state.db schema, `hermes_state.py`, migration code, or config schema.
- During this planning mission, create only `.ai/builds/rewind-port-0200/execution-map.md`.
- During a later build-GO, limit source changes to the explicit writer manifest below. Any additional file is HOLD pending remap.

## L1 work packages

### WP-1 — Gateway rewind identity and destructive-submit safety

- **Purpose:** Replace positional rewind targeting with occurrence-bound server-issued identity, exact-match resolution, explicit whole-transcript confirmation, and archive-preserving persistence on the actual 0.20.0 `prompt.submit` call path.
- **Acceptance criteria:** A1, A2, A3, A4, A5.
- **Risks:** wrong identity/display alignment; stale ID retargeting; duplicate prompts collapsing; hidden/synthetic user rows changing ordinal space; ordinary submits inheriting confirmation state; first-turn wipe without dedicated consent; DB/memory divergence; regression from `archive_dropped=True`; identity annotations added to only one gateway payload while another remains positional.
- **Inputs:** pinned base; all six spec commits; existing `_coerce_message_text`, `_history_to_messages`, display/model histories, `sanitize_replay_history`, `is_truthy_value`, `_session_db`/`_get_db`, and `SessionDB.replace_messages` contract.
- **Outputs:**
  - New dependency-free `tui_gateway/rewind_identity.py` implementing canonical content coercion, model-user indexing, occurrence/prefix-bound `r2` IDs, tail/spine annotation, and exact resolver.
  - `tui_gateway/server.py` imports/re-exports the identity helpers and annotates every transcript-bearing gateway payload whose user rows can expose Restore.
  - `tui_gateway/methods_prompt.py` accepts `truncate_before_message_id`, resolves it against current model history exactly, preserves the legacy ordinal path, enforces target-specific confirmation, and persists through the existing archive-preserving write-before-memory path.
  - `tests/test_tui_gateway_server.py` contains behavioral RED/GREEN evidence through real registered handlers plus pure identity tests.
- **Interfaces:**
  - Display row: user messages receive `rewind_id: "r2:<ordinal>:<prefix-hash>"` only when proven aligned; known-but-unreachable user rows receive `rewind_id: null`; non-user rows receive no key.
  - Submit request: preferred `truncate_before_message_id`; legacy `truncate_before_user_ordinal` remains compatibility-only. ID and ordinal must not silently substitute for one another.
  - Whole-transcript consent: ID path honors only `confirm_delete_entire_transcript`; blanket/generic `confirm_empty_truncate` must not auto-confirm it. Legacy ordinal retains its existing dedicated legacy gate.
  - Resolution return: exact current ordinal or `None`; `None` maps to safe 4018 refusal with no DB write, history mutation, running transition, or turn start.
  - Persistence: `replace_messages(session_key, truncated, active_only=True, archive_dropped=True)` remains write-before-memory.
- **Effect and rollback boundary:** Changes gateway payloads and submit semantics only. Roll back CP-1 as one unit by reverting its four-file patch; no schema/data downgrade is needed. A failed write must leave both in-memory and durable history untouched.
- **Builder-finalizer:** Codex builder-finalizer, route `openai-codex / gpt-5.6-sol`, in one implementation session; independent Grok review is required and the builder may not self-clear.
- **Predecessors:** build-GO; clean pinned worktree; CP-1 RED evidence captured before production edits.
- **Integration gate:** CP-1 tests pass under `scripts/run_tests.sh`; review confirms every identity helper has a named live producer/consumer and `methods_prompt` is the registered handler; diff touches only CP-1 files.
- **Forbidden touches:** global list plus `hermes_cli/web_server.py` and `tests/test_web_server.py` (owned by WP-2).
- **Terminal condition:** CP-1 is independently inspectable and green; A1-A5 tests are green; stale/malformed IDs and insufficient confirmations produce safe errors without state mutation; no later checkpoint is needed for gateway behavior.

### WP-2 — REST transcript identity producer

- **Purpose:** Ensure a cold-opened Desktop/dashboard transcript receives the same rewind identities even when gateway resume omits messages, without duplicating identity logic.
- **Acceptance criteria:** A1 and A3 at the independent REST producer boundary; supports A2/A4 by ensuring the client sends the server-minted ID rather than a positional fallback.
- **Risks:** REST and gateway normalize content differently; paginated windows mint unverifiable identities; stale REST data targets shifted live history; import or DB failures break transcript loading; helper duplication diverges from WP-1 contract.
- **Inputs:** CP-1's public `annotate_rewind_ids` contract; `SessionDB.get_resume_conversations`; `sanitize_replay_history`; existing messages endpoint.
- **Outputs:** `hermes_cli/web_server.py` annotates only complete, unpaged transcript reads using the shared identity module and degrades safely to unmodified rows; `tests/test_web_server.py` proves reachable, unreachable, paged, and failure behavior.
- **Interfaces:** `_with_rewind_ids(db, sid, messages, limit, offset)` returns annotated complete rows; `limit is not None` or nonzero `offset` skips annotation; stale IDs remain harmless because CP-1 resolves exactly against current history.
- **Effect and rollback boundary:** REST response enrichment only; no writes. Roll back CP-2 independently by reverting its two-file patch; CP-1 gateway behavior remains intact.
- **Builder-finalizer:** Same Codex builder-finalizer, but only after CP-1 is integrated; one implementation session for CP-2; independent Grok review required.
- **Predecessors:** CP-1 contract gate green.
- **Integration gate:** CP-2 RED captured before edits; focused web tests and full two-file integration suite pass; import uses CP-1 rather than copied hashing/alignment logic.
- **Forbidden touches:** global list plus all CP-1-owned files.
- **Terminal condition:** cold, complete REST transcript rows expose exactly the identities CP-1 can resolve; unsafe windows/failures expose none; package can be inspected and reverted without changing submit behavior.

## L2 checkpoints — ordered and immutable

The checkpoint order and ownership are immutable after build-GO. Splitting or reassigning a file requires HOLD and reviewer-approved remap.

### CP-1 / parent WP-1 — Identity contract plus registered gateway consumer

- **Scope/output:** One atomic behavioral checkpoint spanning the shared identity module, all gateway transcript producers, and the extracted submit handler. Production call path is `server.py` registration (`server.py:14413-14428`) → `methods_prompt.py:@method("prompt.submit")` (`methods_prompt.py:67-68`) → exact resolver → existing write-before-memory `SessionDB.replace_messages` call (`methods_prompt.py:264-286`). Producer paths include initial/resume/live payload constructors and `_history_to_messages` callers that expose transcript rows.
- **Sole writer:** Codex builder-finalizer in one session.
- **Owned files:**
  - CREATE `tui_gateway/rewind_identity.py`
  - MODIFY `tui_gateway/server.py`
  - MODIFY `tui_gateway/methods_prompt.py`
  - MODIFY `tests/test_tui_gateway_server.py`
- **FAIL-CAPABLE gate:** Before any production edit, add the complete CP-1 test slice and run it on immutable base with `scripts/run_tests.sh tests/test_tui_gateway_server.py -k "rewind_identity or rewind_id or drifted_message_id or first_turn_restore or rewind_preserves_soft_archived"`. Required RED: base lacks the shared identity module/ID payload and does not consume `truncate_before_message_id`; at least one assertion must fail for each A1-A5 mapping below. A collection/import failure caused solely by the deliberately absent new module is valid initial RED only until the first pure helper exists; thereafter each behavior must fail by assertion, not collection.
- **Predecessors:** none beyond build-GO and pinned base.
- **Forbidden touches:** global list and WP-2 files.
- **Tests/oracles/evidence:** pure tests for normalization, prefix-bound duplicate occurrence IDs, full-spine tail alignment, explicit `None` on unreachable rows, malformed/stale/exact resolution; registered `handle_request` tests for no-write refusal and successful retained prefix; real `SessionDB(tmp_path/state.db)` test for A5. Assert replacement kwargs include both `active_only=True` and `archive_dropped=True` where a stub is used, and independently inspect real inactive rows where durability is the oracle. Run all of `scripts/run_tests.sh tests/test_tui_gateway_server.py` after focused GREEN.
- **Budget:** 4.5 engineering hours; stop after one failed porting approach and HOLD rather than replaying spec patches. Review budget: one sitting, target ≤700 net new/changed lines excluding comments/fixtures; if larger, HOLD for checkpoint redesign because file ownership cannot be split safely.
- **Integration position:** 1 of 2; establishes the sole identity contract and destructive consumer before REST begins.
- **Independent acceptance:** Does not require CP-2. A direct gateway session payload supplies IDs and `prompt.submit` consumes them end-to-end.
- **Revertibility:** one four-file revert; no migration, no data conversion.
- **Terminal condition:** focused and whole gateway test file green, writer manifest clean, independent review requested, no inert helper.

### CP-2 / parent WP-2 — REST producer parity

- **Scope/output:** Add shared identity annotation to the complete `/api/sessions/{session_id}/messages` read path. Production call path is REST endpoint → DB `get_messages` plus `get_resume_conversations` → `sanitize_replay_history` → CP-1 `annotate_rewind_ids` → response rows.
- **Sole writer:** Codex builder-finalizer in one session after CP-1 gate.
- **Owned files:**
  - MODIFY `hermes_cli/web_server.py`
  - MODIFY `tests/test_web_server.py`
- **FAIL-CAPABLE gate:** Before web production edits, add CP-2 tests and run `scripts/run_tests.sh tests/test_web_server.py -k "rest_transcript and rewind"` on the base-plus-CP-1 integration position. Required RED: complete REST rows lack `rewind_id`. Also preserve a recorded run against immutable base (without CP-1) showing the same behavioral absence; tests may import the identity contract only after CP-1 exists, so the base RED oracle may use response-key absence rather than importing the new module.
- **Predecessors:** CP-1 green and contract-frozen.
- **Forbidden touches:** global list and every CP-1-owned file.
- **Tests/oracles/evidence:** complete read stamps reachable user rows; ancestor/unreachable rows get `None`; assistants lack the key; duplicate occurrences receive distinct IDs; paginated reads remain unannotated; annotation/DB-helper failure returns the original transcript instead of failing the endpoint. Run `scripts/run_tests.sh tests/test_web_server.py`, then the combined final command.
- **Budget:** 1.5 engineering hours; one failed integration approach maximum; review budget one sitting, target ≤180 net lines.
- **Integration position:** 2 of 2; consumes but does not alter CP-1 contract.
- **Independent acceptance:** On base-plus-CP-1, CP-2's RED/GREEN is checkable without any later work. Its endpoint result can be inspected independently.
- **Revertibility:** one two-file revert; gateway ID emission/consumption remains functional.
- **Terminal condition:** focused and whole web test file green; no duplicated hash/alignment implementation; only CP-2 files changed.

## Acceptance-to-checkpoint proof matrix

| Criterion | Checkpoint | RED test/oracle that fails on immutable base | GREEN proof |
|---|---|---|---|
| A1 exact match only | CP-1 | `test_resolve_rewind_ordinal_accepts_only_exact_matches` and end-to-end drift test: base lacks r2 resolver/ID handling and therefore cannot return safe 4018 for a shifted ID. | Exact candidate ordinal plus prefix hash required; malformed, shifted, compacted, stale IDs return `None`/4018; no write or turn start. |
| A2 ID never auto-confirms emptying | CP-1 | ID-addressed first-turn request carrying only generic `confirm_empty_truncate` is not recognized by base as an ID rewind and does not produce the required 4028 safe refusal. | `test_first_turn_restore_needs_the_dedicated_wipe_confirmation` proves generic consent is insufficient and DB is untouched. |
| A3 identity, not position | CP-1 | Duplicate user text receives no distinct server IDs on base; `truncate_before_message_id` is not consumed. | Annotation produces distinct r2 occurrence/prefix IDs and registered submit cuts exactly the named occurrence. |
| A4 opening turn restore with explicit confirmation | CP-1 | Base cannot execute an ID-addressed opening-turn restore using `confirm_delete_entire_transcript`; request does not establish the required empty prefix via ID. | Same first-turn test proves refusal without dedicated consent and success with it, with empty retained history persisted before the new turn. |
| A5 preserve soft-archived rows | CP-1 | Real-DB ID-addressed rewind test fails on base because the ID path is absent; it must assert the operation succeeds and inactive preexisting rows remain byte/content present. | Real `SessionDB` inspection after successful ID rewind shows preexisting inactive rows preserved; call retains `active_only=True, archive_dropped=True`. |
| A1 REST producer parity | CP-2 | Complete REST response on base has no `rewind_id`. | REST-minted ID equals the shared contract and remains exactly resolvable by CP-1. |
| A3 REST occurrence identity | CP-2 | Two identical REST user turns on base have no distinct occurrence IDs. | Complete response gives distinct r2 IDs; paged/ambiguous rows receive no unsafe target. |

A5 is intentionally proven through the new ID-addressed production operation, not by a standalone `replace_messages(active_only=True)` unit test that already passes on 0.20.0 and would not be RED-capable.

## Dependency graph

Only the following eight edge types are valid in this graph: **data, contract, shared-state, environment, integration, evidence, topology, control**.

Nodes: `BASE`, `SPEC`, `CP-1-TEST`, `IDENTITY`, `GW-PRODUCERS`, `SUBMIT`, `STATE-DB`, `CP-1-GATE`, `CP-2-TEST`, `REST-PRODUCER`, `CP-2-GATE`, `FINAL-GATE`, `GROK`.

- `BASE -> CP-1-TEST` (**environment**): RED executes at immutable SHA.
- `SPEC -> IDENTITY` (**contract**): six commits define r2 behavior, not patch mechanics.
- `IDENTITY -> GW-PRODUCERS` (**data**): annotation stamps display rows.
- `IDENTITY -> SUBMIT` (**contract**): resolver returns exact ordinal or refusal.
- `GW-PRODUCERS -> SUBMIT` (**control**): client returns the server-minted ID on submit.
- `server.py registration -> methods_prompt.py` (**topology**): extracted handler is the live call path.
- `SUBMIT -> STATE-DB` (**shared-state**): write-before-memory transcript replacement; existing schema only.
- `CP-1-TEST -> CP-1-GATE` (**evidence**): behavioral proof for A1-A5.
- `CP-1-GATE -> REST-PRODUCER` (**contract**): REST imports frozen shared annotation API.
- `BASE+CP-1 -> CP-2-TEST` (**environment**): CP-2 RED position.
- `REST-PRODUCER -> GW-PRODUCERS` (**integration**): both producers emit the same identity semantics.
- `CP-2-TEST -> CP-2-GATE` (**evidence**): REST behavior proof.
- `CP-1-GATE -> FINAL-GATE` (**integration**); `CP-2-GATE -> FINAL-GATE` (**integration**).
- `FINAL-GATE -> GROK` (**control**): builder submits evidence; Grok independently reviews and alone may clear.

### UNKNOWN = AFFECTED

Every unresolved possibility is treated as affected, never presumed safe:

- **UNKNOWN = AFFECTED:** the exact set of 0.20.0 transcript-bearing gateway payload constructors beyond `_live_session_payload` and eager resume. CP-1 must inventory all `_history_to_messages` response call sites before editing; any user-visible constructor not proven irrelevant is affected and must be annotated in CP-1 without creating another writer.
- **UNKNOWN = AFFECTED:** whether current desktop clients send both legacy and ID targets. Submit validation must define/refuse ambiguous dual-target requests rather than choosing silently; confirm expected behavior from the spec tests. If the spec is silent, HOLD for reviewer decision.
- **UNKNOWN = AFFECTED:** compute-host forwarding of truncation params. Trace `prompt.submit` isolation dispatch before GREEN; if IDs/confirmation fields are dropped, it is affected. If the fix requires a file outside CP-1 ownership, HOLD and remap.
- **UNKNOWN = AFFECTED:** profile-local DB selection on rewrite. Preserve the current 0.20 call path exactly; if ID work reveals `_get_db` is wrong and requires broader profile work, HOLD.
- **UNKNOWN = AFFECTED:** structured/skill display text divergence. Shared coercion must preserve current renderer behavior; any mismatch means the row is non-rewindable, never guessed.
- **UNKNOWN = AFFECTED:** a required schema/migration/storage API modification. This is explicitly outside the resolved design and triggers HOLD.

## One-writer execution plan

### Ordered sequence

1. Verify branch, HEAD, clean status, and forbidden-tree avoidance; do not mutate refs.
2. CP-1 test writer adds all CP-1 tests to its owned test file; run and record base RED per A1-A5.
3. The same CP-1 sole writer creates the identity module and modifies gateway producers and the actual extracted submit handler; run focused then whole gateway tests.
4. Stop for CP-1 integration gate and independent inspectability check. Do not start CP-2 if CP-1 is not self-contained green.
5. CP-2 test writer (same named builder-finalizer, later single session) adds REST tests; record base response-key RED and base-plus-CP-1 RED.
6. CP-2 sole writer modifies only web server integration; run focused then whole web tests.
7. Run final combined verification: `scripts/run_tests.sh tests/test_tui_gateway_server.py tests/test_web_server.py`.
8. Inspect `git diff --name-only`, `git diff --check`, and the diff itself; verify only six source/test files plus the execution map are present. Do not commit unless a later dispatch explicitly asks.
9. Submit test output and diff evidence to Grok. Builder-finalizer does not approve its own work.

### File ownership proof

| File | Sole checkpoint/writer | Other checkpoints |
|---|---|---|
| `tui_gateway/rewind_identity.py` | CP-1 / Codex builder-finalizer | read-only consumer in CP-2 |
| `tui_gateway/server.py` | CP-1 / Codex builder-finalizer | forbidden in CP-2 |
| `tui_gateway/methods_prompt.py` | CP-1 / Codex builder-finalizer | forbidden in CP-2 |
| `tests/test_tui_gateway_server.py` | CP-1 / Codex builder-finalizer | forbidden in CP-2 |
| `hermes_cli/web_server.py` | CP-2 / Codex builder-finalizer | forbidden in CP-1 |
| `tests/test_web_server.py` | CP-2 / Codex builder-finalizer | forbidden in CP-1 |

No file appears in two write sets. CP-2 imports CP-1's module but cannot edit it. Final verification and Grok are read-only. The execution map itself is written only during this planning mission and is not an implementation checkpoint file.

## Final integration gate and self-sanity checks

Required before any build verdict:

- Each checkpoint's own RED is captured before its production edit and GREEN does not depend on a later checkpoint.
- CP-1 is useful without CP-2: live/eager gateway payload → r2 ID → registered submit resolver → safe DB rewrite.
- CP-2 is useful and inspectable on top of the frozen CP-1 contract: cold REST transcript receives the same IDs.
- No test merely inspects source text. Tests call pure functions, registered JSON-RPC handlers, REST helpers/endpoints, and a real temporary SessionDB.
- No change-detector snapshots: assertions are behavior/relationship invariants.
- No file has two writers.
- No package lacks a named user outcome/internal consumer.
- No checkpoint is too broad to review in one sitting; crossing its stated line budget triggers HOLD, not silent expansion.
- Final test command is green with no flaky retry; `git diff --check` is clean.
- No schema/config/version change and no forbidden path/ref/network operation occurred.

Sanity-refusal predicates:

- HOLD if a CP-1 test can pass only after CP-2.
- HOLD if CP-2 requires editing a CP-1 file.
- HOLD if any seventh implementation/test file is needed.
- HOLD if the extracted `methods_prompt.py` production path is not exercised.
- HOLD if A5 can only be met by changing schema/storage APIs.
- HOLD if ambiguous dual ID/ordinal behavior is not specified by evidence.
- HOLD if any package cannot be reverted and inspected independently.

## Deliberately not in this map

- Cherry-picking, merging, rebasing, graft repair, unshallowing, or replaying the six commits.
- Any desktop renderer/client change; the frozen spec has none and the desktop tree is forbidden.
- Any state.db schema migration, new identity column, ID persistence, `hermes_state.py` change, or config-version change.
- Changes to legacy `/undo`, `/retry`, `/compress`, snapshot restore, CLI rewind, or unrelated transcript rewrite paths.
- Replacing/removing legacy ordinal compatibility beyond what is necessary to define safe coexistence.
- Broad refactoring of `server.py`, `methods_prompt.py`, web routes, replay sanitization, or SessionDB.
- Network operations, commits, pushes, merges, deployments, tags, reland hooks, live-install validation, or mutation of user state.
- Frontend UX wording/dialog implementation; the backend only validates the dedicated consent contract.
- Performance optimization beyond skipping unsafe paged annotation and avoiding duplicate identity implementations.
- Self-review or self-clearance by the builder-finalizer; Grok remains the independent reviewer.
