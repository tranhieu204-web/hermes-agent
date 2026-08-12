# Execution Map v2 — `rewind-port-0200`

## Pin, FORM, authority, and planning boundary

- Workspace: `C:\hrp`
- Branch: `sakaan/rewind-port-0200-20260812`
- Immutable base: `ee472a7fdbbc55924f91ab122dbaa29bd07668b0`
- Frozen predecessor map: `.ai/builds/rewind-port-0200/execution-map.md`, SHA-256 `c617b7a0423ac9a7e4009253e74e63a8132770ee2f06c68015460e8ea7b3e1ce`; it remains unedited evidence.
- Spec objects, read semantically rather than replayed: `18319f3f4`, `fc063c47b`, `c9101a651`, `4d1f52ffb`, `4793eb531`, `597142813`.
- FORM: **DECOMPOSED**. CP-1 owns identity, all gateway/compute-host producers, and destructive submit safety. CP-2 independently adds REST producer parity against CP-1's frozen pure contract.
- Chairman D1 supersedes the old file-count assumption. The real implementation/test topology is eight files. The v1 predicate “seventh file = HOLD” is revoked and must never be used.
- Chairman D2 keeps REST parity in v1; CP-2 cannot be dropped.
- This document is planning only. It authorizes no source edit, commit, merge, push, fetch, tag, deploy, ref mutation, live-install access, or state mutation.

## Non-negotiable invariants

1. A1: a rewind ID resolves only by exact current occurrence/prefix identity; no fallback or nearest/content-only match.
2. A2: an ID never auto-confirms an empty retained prefix.
3. A3: identity, not rendered position, selects the model-history user turn.
4. A4: opening-turn ID restore is permitted only with dedicated whole-transcript consent.
5. A5: identity is computed, not stored. No state.db migration, schema/version, storage API, or `hermes_state.py` change.
6. `model_user_indices(history)` is the only ordinal address-space function and exactly equals current submit semantics: `isinstance(m, dict) and m.get("role") == "user" and not m.get("display_kind")`.
7. Persistence remains `replace_messages(session_key, truncated, active_only=True, archive_dropped=True)` and occurs before `session["history"]` mutation. A failed write starts no turn and changes neither durable nor in-memory history.
8. One writer per file. Eight edge types only: **data, contract, shared-state, environment, integration, evidence, topology, control**. Every unknown is AFFECTED until proven otherwise.

## Base topology inventory — every `_history_to_messages` and transcript-emitting site

The inventory below is binding. “Annotate” means the call must receive both the display projection and the exact model history whose ordinal space `prompt.submit` can cut. “Exclude” means the output is not a Restore-bearing client transcript; the exclusion is tested or evidenced, not silently ignored.

| Base site | Emitted surface / projections | CP owner | Required disposition |
|---|---|---|---|
| `tui_gateway/server.py:7136-7231` | `_history_to_messages`, pure display conversion only | CP-1 | Keep conversion semantics; add/re-export a separate `_history_to_client_messages(display_history, model_history)` annotating helper. Never mint inside one-argument conversion because it lacks the model projection. |
| `server.py:8166-8211` (`:8199`) | `_live_session_payload`; persisted/reconciled display lineage plus live session model history | CP-1 | Annotate through shared helper; `omit_messages=True` stays `[]` and does no annotation work. |
| `server.py:12725` | `/history` textual formatting from converted rows | CP-1 | Exclude: output is a formatted text command, not a transcript replacement/Restore surface. Keep behavior unchanged and cover exclusion in inventory evidence. |
| `server.py:12760`, `:12769` | `/context` textual formatting, DB/fallback converted rows | CP-1 | Exclude for the same reason; no `rewind_id` text leakage. |
| `server.py:14359`, `:14387-14389` | browser-connect diagnostic string list named `messages` | CP-1 | Exclude: not conversation rows and does not call `_history_to_messages`. |
| `tui_gateway/methods_session.py:127-159` (`:133`) | `session.create`; display/model are the initial `history` | CP-1 | Annotate; user rows follow tri-state ID/null contract. |
| `methods_session.py:476-510` (`:494`) | child-watch/lazy resume; verbatim child display and repaired child model history | CP-1 | Annotate with `display_history, history`; omit path remains empty. |
| `methods_session.py:534-602` (`:581`) | deferred cold resume; `get_resume_conversations` display plus sanitized `raw_history` | CP-1 | Annotate with display and sanitized model history. |
| `methods_session.py:621-807` (`:643`, payload `:792-807`) | eager resume; same dual projections | CP-1 | Annotate once and return that `messages` variable. |
| reuse branches `methods_session.py:476-477`, `:574-575` | delegates to `server.py:_live_session_payload` | CP-1 | Covered by owned annotating live helper; tests exercise cold and live reuse. |
| `methods_session.py:2442-2463` (`:2461`) | `session.history`; user-visible transcript replacement | CP-1 | Annotate using DB display lineage and current session model history (or conservative nulls if exact model projection cannot be obtained); test exact branch. |
| `methods_session.py:2536-2549` | compute-host compression acknowledgement returned to Desktop | CP-1 | Preserve the host's already-projected, already-annotated rows; remove unsafe double conversion. Do not recompute against parent mirror history. |
| `methods_session.py:2634-2652` (`:2650`) | local `session.compress` replacement; post-compression model history | CP-1 | Annotate with the same post-compression history as display/model; unreachable rows remain null. |
| `methods_session.py:2676-2745` (`:2737`) | `session.save` raw JSON export | CP-1 | Exclude: file export is not a renderer transcript/Restore request source; preserve raw saved schema. |
| `methods_session.py:2927-2939` (`:2936`) | `session.branch` response; copied branch history | CP-1 | Annotate with copied branch model history. |
| `tui_gateway/compute_host.py:46-79` | `SpikeAgent.run_conversation` internal result with raw model messages | CP-1 | Exclude as an internal agent return, but it supplies the history to later host egress. No transport annotation here. |
| `compute_host.py:718-758` (`:741`, `:755`) | host `control.ack` transcript crossing process boundary | CP-1 | Annotate in host against its own raw history before emit. Parent must preserve it. |
| `tui_gateway/methods_session.py:2650` and compute-host counterpart | compression replacement sibling paths | CP-1 | Tests assert equivalent ID semantics across local and host paths. |
| `hermes_cli/web_routers/sessions.py:601-652` | REST `GET /api/sessions/{id}/messages`; always paged raw DB rows | CP-2 | Apply the safe tip-window rule below using CP-1's pure annotator. |
| `hermes_cli/web_server.py:11314-11328` | router include/re-export only | none | No edit. It is topology evidence, not a producer implementation. |

Before CP-1 GREEN, a read-only AST/text inventory must be rerun on the pinned/base-plus-test tree. Any newly found Restore-bearing `_history_to_messages` or conversation-row `messages` emitter is AFFECTED. If it requires a ninth implementation/test file, HOLD for a narrowly updated reviewed manifest; **file number alone is never a HOLD**.

## Shared identity and gateway projection contract

CP-1 creates dependency-free `tui_gateway/rewind_identity.py` with content coercion, canonical prefix hashing, `r2:<ordinal>:<prefix-hash>`, exact resolution, and conservative tail/spine annotation from the final spec semantics, adapted to 0.20.0.

Hard interfaces:

- `model_user_indices(history)` uses exactly: `isinstance(m, dict) and m.get("role") == "user" and not m.get("display_kind")`.
- Submit imports/calls that function; it does not retain a sibling comprehension.
- `annotate_rewind_ids(display_rows, model_history)` compares the user/assistant spine from the live tip backward. It gives every display user row either a proven `rewind_id` or explicit `rewind_id: null`; non-user rows get no key.
- A `display_kind` user row is not in ordinal space, never gets a non-null ID, and cannot shift IDs of real user rows. Pure tests cover markers before, between, and after real turns.
- `resolve_rewind_ordinal(model_history, id)` returns the exact ordinal or `None`; malformed, negative/out-of-range, stale, shifted, duplicate-occurrence drift, or prefix mismatch returns `None`.
- `_history_to_client_messages(display_history, model_history)` is the single gateway annotation adapter. It first uses existing `_history_to_messages`, then calls the pure annotator. It does not fetch DB state or guess a model history.

## Submit confirmation decision table (binding)

The current 0.20 safety gate is stronger than the older spec and survives. `confirm_truncate` is required for **both** target types. Dual targeting is refused even though the historical spec silently preferred ID; the spec tests are silent on dual requests, and fail-closed ambiguity is the only behavior consistent with current stale-state hardening.

| Request shape | Required result | Accepted confirmation(s) | Ignored / non-substituting flags |
|---|---|---|---|
| No ID, no ordinal, no confirmation flags | Ordinary submit | none | none |
| No target + any truthy `confirm_truncate`, `confirm_empty_truncate`, or `confirm_delete_entire_transcript` | Refuse 4004; no queue/write/mutation/turn | none | all confirmations cannot create a target |
| ID and ordinal both present | Refuse 4004 before resolution; no state change | none | no precedence, even if IDs/ordinals agree |
| Ordinal present, `confirm_truncate` false/missing | Refuse existing 4029 | none | wipe flags do not substitute for destructive-cut consent |
| Valid ordinal > 0 + `confirm_truncate=true` | Accept legacy cut | `confirm_truncate` | ID-only whole-delete flag is irrelevant |
| Ordinal 0 + `confirm_truncate=true`, `confirm_empty_truncate` false/missing | Refuse 4028 | none | `confirm_delete_entire_transcript` does not cross-arm ordinal path |
| Ordinal 0 + both `confirm_truncate=true` and `confirm_empty_truncate=true` | Accept legacy whole-transcript cut | both named flags | dedicated ID flag irrelevant |
| ID present, `confirm_truncate` false/missing | Refuse 4029 (ID-specific message); do not resolve/write | none | ID existence is not consent |
| Valid ID resolving to retained non-empty prefix + `confirm_truncate=true` | Accept ID cut | `confirm_truncate` | empty-wipe flags unnecessary |
| ID resolving to ordinal 0 + `confirm_truncate=true`, but no dedicated flag | Refuse 4028 | none | `confirm_empty_truncate` is explicitly ignored on ID path |
| ID resolving to ordinal 0 + both `confirm_truncate=true` and `confirm_delete_entire_transcript=true` | Accept opening-turn restore | both named flags | `confirm_empty_truncate` irrelevant |
| Malformed/stale/unresolvable ID with any confirmations | Refuse 4018; confirmations never rescue identity | none | all flags |
| Bool/non-integer/out-of-range ordinal with any confirmations | Preserve current 4004/4018 validation; no state change | none | all flags |
| Busy session carrying any rewind target/confirmation | Preserve current busy/queue safety: destructive target must not be reduced to queued text or leak confirmation into a later ordinary turn | none until a fresh non-busy validated request | all stale queued rewind state |

Ordering is immutable: validate ambiguity and consent under the existing lock; resolve; calculate `truncated`; call `replace_messages(..., active_only=True, archive_dropped=True)`; on success assign memory/version; then mark running/start turn. Compute-host dispatch remains after this parent-side sequence (`methods_prompt.py:309-317` on base); target/confirmation fields are not forwarded because they have already been consumed. A test proves a failed DB write prevents both dispatch and inline turn start.

## REST always-paged safe-window design

### Precise rule

`hermes_cli/web_routers/sessions.py:get_session_messages` may mint rewind IDs **only** when all conditions hold:

1. The resolved response window is tip-ordered: `latest_page is True` (`order == "latest"`, or omitted order with omitted limit/default latest-500).
2. `offset == 0`.
3. The returned page is non-empty.
4. In the same read-only DB scope, the server obtains the resolved session's complete model resume projection via `get_resume_conversations(sid)`, then applies the same `sanitize_replay_history` used by gateway resume.
5. `annotate_rewind_ids(returned_messages, sanitized_model_history)` proves suffix alignment from the live tip. Only proven rows get IDs; unmatched user rows get null.

This safely supports default latest 500 even when the transcript has >500 rows: IDs encode ordinals and prefix hashes from the **complete model history**, while annotation only stamps rows in the returned suffix that align with that history's tip. It does not infer ordinal zero from page position.

Never mint IDs for a non-tip window. Therefore these return the original rows without any newly added `rewind_id` key: `offset > 0` under either ordering; effective oldest ordering (including explicit `limit` with omitted `order`); empty pages; inability to obtain/sanitize the complete model history; annotation/import/DB failure; or any other unproven alignment. Do not label unsafe rows `null` at the REST wrapper level, because absence means “this endpoint/window did not assert identity”; null remains the annotator's tri-state for a safe tip window containing known-but-unreachable user rows.

### REST RED/GREEN behavior

- Seed >500 alternating/user-assistant rows in a real temporary SessionDB. Default GET must remain `limit=500, offset=0, order=latest`, return the latest 500 chronologically, and after GREEN give only tip-aligned user rows IDs whose ordinals resolve against the complete sanitized model history.
- For the same >500 session, `?limit=100&offset=1&order=latest` returns no `rewind_id` keys.
- `?limit=100&offset=0` (effective oldest) returns no IDs even if fewer rows happen to be returned.
- `?limit=100&offset=0&order=latest` is a qualifying tip window and can annotate from full model history.
- Ancestor/compaction divergence in a qualifying tip window yields null for known but unreachable users, never a guessed ID.
- Annotation/model-history lookup failure returns status 200 and original rows/pagination unchanged.
- Profile-scoped request opens and derives both page and model projection from the same `_open_session_db_for_profile(profile, read_only=True)` handle.

## Work packages and immutable checkpoints

### WP-1 / CP-1 — Identity, complete gateway producers, and destructive consumer

**Purpose/acceptance:** Deliver A1-A5 through direct gateway and compute-host transcript paths without CP-2.

**Owned files — sole writer is the builder-finalizer in one CP-1 writer epoch:**

1. CREATE `tui_gateway/rewind_identity.py`
2. MODIFY `tui_gateway/server.py`
3. MODIFY `tui_gateway/methods_session.py`
4. MODIFY `tui_gateway/methods_prompt.py`
5. MODIFY `tui_gateway/compute_host.py`
6. MODIFY `tests/test_tui_gateway_server.py`

**TDD sequence and executable gates:**

1. Read all six spec diffs line-by-line and inventory the existing truncate/queue/compute-host/profile tests. Add the full CP-1 tests before production edits.
2. Base RED command:
   `C:\hrp\venv\Scripts\python.exe -m pytest tests/test_tui_gateway_server.py -k "rewind_identity or rewind_id or drifted_message_id or first_turn_restore or rewind_preserves_soft_archived or rewind_display_kind or rewind_dual_target or rewind_confirm_matrix or rewind_producer or rewind_compute_host" -q`
   Required: once tests exist, nonzero by criterion-specific assertion. A first collection error solely because the deliberately new module is absent is allowed only until the pure-helper skeleton exists; it is not final RED evidence. Exit 5 before tests are added is capability evidence only, not the recorded RED.
3. Pure GREEN command after identity implementation:
   `C:\hrp\venv\Scripts\python.exe -m pytest tests/test_tui_gateway_server.py -k "rewind_identity or rewind_display_kind" -q`
4. Consumer/producer GREEN:
   `C:\hrp\venv\Scripts\python.exe -m pytest tests/test_tui_gateway_server.py -k "rewind_id or drifted_message_id or first_turn_restore or rewind_preserves_soft_archived or rewind_dual_target or rewind_confirm_matrix or rewind_producer or rewind_compute_host" -q`
5. Whole CP-1 file:
   `C:\hrp\venv\Scripts\python.exe -m pytest tests/test_tui_gateway_server.py -q`

**Required tests/oracles:** pure occurrence/prefix/exactness and `display_kind` parity; session.create; cold deferred resume; eager resume; live reuse; child watch; omit_messages; session.history; local compression; host compression/control ack; branch; malformed/stale/dual target; complete decision table; busy queue target leakage; DB failure ordering; real `SessionDB(tmp_path/state.db)` A5 inspection proving inactive rows remain and dropped live rows are soft-archived. Stubs additionally assert both write kwargs.

**RED honesty:** A1/A3 fail because base emits/consumes no identity. A2/A4 tests first assert the new ID path/consent interface is absent, then GREEN asserts 4029/4028 and no state mutation. A5 fails because the ID-addressed production operation is absent, not because base's existing ordinal archive behavior is wrong.

**Budget/line target:** 6.0 engineering hours; one failed porting approach maximum. Target ≤950 net new/changed production+test lines across six files, excluding comments/fixtures; soft allocation identity 320, server 100, methods_session 180, methods_prompt 180, compute_host 60, tests 450 (overlap means total target governs). If >1,150 net or semantics cannot be reviewed in one sitting, HOLD for checkpoint redesign. The old 700/four-file budget is superseded.

**Rollback:** one six-file CP-1 revert. No migration or data conversion. CP-1 may not be released partially.

### WP-2 / CP-2 — Always-paged REST producer parity

**Purpose/acceptance:** Deliver A1/A3 identities to cold REST transcripts under the safe tip-window rule, consuming but not changing CP-1's pure contract.

**Owned files — sole writer is the builder-finalizer in one later CP-2 writer epoch:**

7. MODIFY `hermes_cli/web_routers/sessions.py`
8. MODIFY `tests/hermes_cli/test_web_server.py`

`hermes_cli/web_server.py` and root `tests/test_web_server.py` are explicitly not writers.

**TDD sequence and executable gates:**

1. Add REST tests on base-plus-CP-1 before router production edits.
2. CP-2 RED:
   `C:\hrp\venv\Scripts\python.exe -m pytest tests/hermes_cli/test_web_server.py -k "rewind and (messages or transcript)" -q`
   Required: qualifying default latest responses lack IDs. Also retain immutable-base response-key absence evidence without importing the new module.
3. Focused pagination + rewind GREEN:
   `C:\hrp\venv\Scripts\python.exe -m pytest tests/hermes_cli/test_web_server.py -k "get_session_messages and (rewind or omitted_limit_defaults_to_500 or negative_offset or limit_above_500)" -q`
4. Whole CP-2 file:
   `C:\hrp\venv\Scripts\python.exe -m pytest tests/hermes_cli/test_web_server.py -q`
5. Final combined gate:
   `C:\hrp\venv\Scripts\python.exe -m pytest tests/test_tui_gateway_server.py tests/hermes_cli/test_web_server.py -q`

**Budget/line target:** 2.0 engineering hours; one failed integration approach maximum; target ≤260 net new/changed lines across the two files, hard review threshold 340. The obsolete 180-line web_server target is superseded.

**Rollback:** independent two-file CP-2 revert leaves CP-1 gateway emission/consumption working.

## Acceptance-to-checkpoint proof

| Criterion | Owner | Base RED capability | GREEN oracle |
|---|---|---|---|
| A1 exact only | CP-1 | no r2 helper/consumer/output | exact prefix occurrence resolves; all drift/malformed/stale cases return 4018 with no write/turn |
| A2 no auto-empty | CP-1 | no ID consent interface | valid ID-zero + generic/insufficient flags returns 4028; DB/memory untouched |
| A3 identity not position | CP-1 | duplicates/display markers have no server ID | duplicate occurrences differ; display_kind does not occupy ordinal space; named ID cuts exact turn |
| A4 opening turn | CP-1 | dedicated ID request not consumed | only `confirm_truncate` + `confirm_delete_entire_transcript` permits empty retained prefix |
| A5 archive preserve | CP-1 | ID-addressed operation absent | real DB keeps preexisting inactive rows and soft-archives dropped live rows; write-before-memory retained |
| REST A1/A3 | CP-2 | qualifying latest page has no IDs | >500 latest suffix IDs resolve against full history; offset/non-tip windows mint none |

## Dependency graph — eight edge types only

Nodes: `BASE`, `SPEC`, `CP1_TEST`, `IDENTITY`, `GW_HELPER`, `GW_PRODUCERS`, `HOST_PRODUCER`, `SUBMIT`, `STATE_DB`, `CP1_GATE`, `CP2_TEST`, `REST_PRODUCER`, `CP2_GATE`, `FINAL_GATE`, `GROK`.

- `BASE -> CP1_TEST` (**environment**): candidate-local venv executes RED at pin.
- `SPEC -> IDENTITY` (**contract**): six objects define identity semantics; 0.20 hardening overrides old mechanics.
- `IDENTITY -> GW_HELPER` (**contract**); `GW_HELPER -> GW_PRODUCERS` (**data**).
- `HOST_PRODUCER -> GW_PRODUCERS` (**integration**): parent preserves host annotation.
- `GW_PRODUCERS -> SUBMIT` (**control**): client returns minted ID.
- `server.py registration -> methods_prompt.py/methods_session.py` (**topology**): extracted handlers are live.
- `SUBMIT -> STATE_DB` (**shared-state**): archive-preserving write-before-memory.
- `CP1_TEST -> CP1_GATE` (**evidence**).
- `CP1_GATE -> REST_PRODUCER` (**contract**): CP-2 imports frozen pure helper.
- `BASE+CP1 -> CP2_TEST` (**environment**).
- `REST_PRODUCER -> GW_PRODUCERS` (**integration**): same identity semantics.
- `CP2_TEST -> CP2_GATE` (**evidence**).
- `CP1_GATE -> FINAL_GATE`, `CP2_GATE -> FINAL_GATE` (**integration**).
- `FINAL_GATE -> GROK` (**control**): only independent reviewer rescans/clears.

## UNKNOWN = AFFECTED / open evidence obligations

1. Full line-by-line semantics of all spec patches: AFFECTED until recorded before CP-1 edits.
2. Every compute-host frame field and transcript return: AFFECTED; `compute_host.py` is owned. Parent-side target consumption must be evidenced; no speculative forwarding.
3. Desktop `omit_messages` and Restore request shape: AFFECTED, read-only inspection only. Any needed desktop edit is HOLD/remap.
4. Full current gateway truncate-suite semantics: AFFECTED; inventory before test additions.
5. Non-manage REST endpoints that might feed Restore: AFFECTED; route/caller inventory before CP-2. Additional writer requires remap.
6. Profile DB equivalence: AFFECTED; test same-handle profile page/model derivation and preserve gateway selection paths.
7. Exact live/cold/watch/eager/host Desktop branch matrix: AFFECTED; conservative producer coverage above remains unless proven irrelevant.
8. Structured/skill content divergence: AFFECTED; shared coercion or null, never guessed identity.
9. Any schema/storage/config/version requirement: AFFECTED and immediate HOLD under A5 boundary.

## One-writer execution and inspection sequence

1. Verify pin/branch/status and candidate venv; do not use or fix `scripts/run_tests.sh`.
2. CP-1 read-only spec/client/test/topology inventory; record the OPEN-gap evidence.
3. CP-1 sole writer adds tests; capture behavioral RED.
4. Same CP-1 writer edits only its six files; run pure, focused, then whole-file GREEN.
5. Inspect CP-1 diff/name set, producer inventory, write ordering, and rollback as one unit. Stop if not independently useful.
6. CP-2 sole writer adds tests to its owned test file; capture RED on base-plus-CP-1.
7. Same CP-2 writer edits only sessions router; run focused and whole-file GREEN.
8. Run combined gate, `git diff --check`, `git diff --name-only`, and inspect full diff. No commit unless a later dispatch explicitly authorizes it.
9. Submit evidence to the same independent blind reviewer. Builder-finalizer does not review or clear its work.

## File ownership proof

| File | Sole checkpoint/writer | Other checkpoint |
|---|---|---|
| `tui_gateway/rewind_identity.py` | CP-1 | CP-2 read-only import |
| `tui_gateway/server.py` | CP-1 | forbidden CP-2 |
| `tui_gateway/methods_session.py` | CP-1 | forbidden CP-2 |
| `tui_gateway/methods_prompt.py` | CP-1 | forbidden CP-2 |
| `tui_gateway/compute_host.py` | CP-1 | forbidden CP-2 |
| `tests/test_tui_gateway_server.py` | CP-1 | forbidden CP-2 |
| `hermes_cli/web_routers/sessions.py` | CP-2 | forbidden CP-1 |
| `tests/hermes_cli/test_web_server.py` | CP-2 | forbidden CP-1 |

No file has two writers. Read-only imports, final verification, and reviewer inspection are not writers.

## Global and package forbidden touches

- Do not enter or mutate `C:\Users\HieuKa\AppData\Local\New Hermes\hermes-agent`.
- Do not touch `refs/heads/main`, `preupdate-20260812/*`, reland-gate hooks, or `C:\Users\HieuKa\AppData\Local\hermes\hermes-agent`.
- No desktop source edit; no push, merge, deploy, commit, tag, rebase, graft, cherry-pick, or `fetch --unshallow`.
- No state.db schema/migration, `hermes_state.py`, config schema/version, or storage API change.
- During this repair mission, create only `response-matrix.md` and `execution-map-v2.md`; never edit v1 or the review bundle.
- During build, CP-1 may not touch CP-2 files; CP-2 may not touch CP-1 files.
- No broad `/undo`, `/retry`, `/compress`, SessionDB, replay, router, server, or client refactor.

## Sanity/HOLD predicates (revised)

- HOLD if CP-1 tests require CP-2 to pass, or CP-2 requires editing a CP-1 file.
- HOLD if a Restore-bearing producer is unowned or cannot receive the correct model projection.
- HOLD if any non-tip REST window can receive a minted ID, or default latest-500 cannot be proven safe against complete model history.
- HOLD if submit/identity ordinal spaces differ, including `display_kind` handling.
- HOLD if dual target is not refused, confirmation flags cross-arm, busy queue leaks destructive state, or write-before-memory/archive flags regress.
- HOLD if A5 needs schema/storage changes.
- HOLD if a newly required file lies outside the reviewed eight-file manifest until a reviewer-approved remap names its writer and budget. **Do not HOLD merely because it is a ninth file; HOLD because it is currently unowned.**
- HOLD if any gate uses `scripts/run_tests.sh`, another worktree's venv, or a command not executable through `C:\hrp\venv\Scripts\python.exe -m pytest` on this machine.
- HOLD if a package cannot be independently inspected and reverted, line thresholds are exceeded without redesign, or a forbidden touch occurs.

## Deliberate omissions

No desktop UX changes; no persisted rewind ID; no migration; no legacy ordinal removal; no unrelated transcript rewrite; no source-text tests or change-detector snapshots; no performance redesign beyond bounded REST response and safe full-history identity derivation; no builder self-clear. The same blind reviewer rescans the entire successor and alone may issue GO.
