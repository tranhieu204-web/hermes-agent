# Stage 6 Repair Epoch 1 Decision and Evidence

Timestamp (ICT): `2026-08-11T14:33:22+07:00`

Lifecycle: `M13 CANDIDATE VERIFIED — PRIOR FINAL INSPECTION SUPERSEDED — NEW FINAL INSPECTION PENDING`

Repair base: `9048fea8b930600f95790e4d25eb30f3dbdf13cb`

Repair code commit: `d540eaee9764fbc3194c493946cd6624f447c3a5`

Repair code tree: `07eded674d1b1352759ef7ff65bd5a84a4314575`

Reviewed code subject: `c266dad38dcf1cbf1bcb67b859bd1ff8d0892463`

Reviewed code-subject tree: `2095ee450d2582f765c604bb579cc6a93397c1ee`

This is the same single authorized bounded repair epoch. It is not a second attempt.

## B-1 — canonical representation and guard limit

Disposition: `CODE_FIX_CONFIRMED; M13_WIDENING_FENCE_VERIFIED; NEW_FINAL_INSPECTION_PENDING`.

The v3.2 plan required a byte-exact comparison but did not state which representation was canonical. That omission was the plan-level cause of B-1. Durable rows may contain a raw string such as `"answer text\n"`, while the in-memory replay history already contains `"answer text"` after `sanitize_context(content).strip()`. Comparing those unlike representations made `/retry` fail forever with `RewindHistoryConflict`, incorrectly blaming a state change.

The repair makes the caller's replay/history projection canonical and projects only the durable side into that representation:

- String content for user/assistant rows uses the established replay projection: `sanitize_context(decoded_content).strip()`.
- Structured content retains the decoded canonical JSON representation. It is never coerced to a string projection.
- `api_content`, when present, remains an independently compared byte-fidelity sidecar and is exact.
- Expected/caller history is not sanitized again. Genuine drift that is still represented by the caller therefore remains fail-closed.

Guard limit, stated explicitly and corrected after the Stage 6 recheck:

- The sanitizer-specific examples are a **known lower bound, not an exhaustive characterization**. The complete abstract boundary is equivalence under the whole `_normalize_rewind_message` projection. Known additional collapse classes include `_decode_content` sentinel decoding, semantic decoding of the five `_REWIND_JSON_FIELDS` (which discards JSON byte formatting and key order), and the deliberately excluded `id`, `session_id`, `active`, `compacted`, and `timestamp` columns. Tool-role string content remains byte-exact. For user/assistant string `content` without a sidecar, differences collapsed by `sanitize_context(...).strip()` remain one important class: arbitrary-length `<memory-context>...</memory-context>` spans, recalled-memory `[System note: ...]` forms, fence tags, and final-strip differences. See `agent/memory_manager.py:163-181`.
- The reviewer demonstrated the boundary empirically: an 81-byte durable value containing an injected 66-byte memory-context payload projected to the same 11-byte `"answer text"` value and was accepted without a sidecar; the same drift failed closed when a sidecar was present.
- The earlier “whitespace-only” wording originated in the COO instruction and was recorded faithfully by the builder. The COO corrected that instruction after reading and measuring the sanitizer. The under-scoped wording was not a builder analysis error.
- `api_content` is compared exactly when present, but its actual numeric prevalence is **NOT ESTABLISHED**. It is nullable and conditional, not universal: `compose_user_api_content` returns `None` for non-string content or when no memory/plugin context is injected (`agent/turn_context.py:53-85`); the turn prologue stamps at most the current user row when composed bytes differ (`:1148-1176`); `run_agent.py:2167-2173` stamps user/assistant rows only when sanitization changes the content; the gateway and branch persistence sites only forward an already-present sidecar (`gateway/session.py:3092`, `gateway/slash_commands.py:4261`, `hermes_cli/cli_commands_mixin.py:979`); rewrite paths remove stale sidecars (`agent/turn_context.py:111-120`). A no-injection, no-sanitization-changing session may therefore have zero sidecars, and sidecar coverage for rows that drift after an originally sidecar-free write is not established.
- Therefore the guard is not described as unqualified byte-exact. It compares the canonical replay projection and, separately, exact `api_content` only when that nullable sidecar exists.

Evidence:

- RED trigger uses trailing-newline content that is not a fixed point of the replay projection.
- Structured-content regression was caught by the full `tests/test_hermes_state.py` run before shipment; the repair then preserved structured values.
- M01 rejects removal of durable string projection.
- M02 rejects normalizing the caller/expected side a second time.
- M03 rejects stringification of structured content.
- M04 rejects loss of persisted `api_content` authority.

## Stage 6 close-out assertion and decision

Commit `c266dad38dcf1cbf1bcb67b859bd1ff8d0892463` adds three executable acceptance cases to `tests/test_hermes_state.py` (file SHA-256 `a60f440da69a709a4becf34e865be0d4b914a776d5a7b405756b948ccb209648`; parent diff SHA-256 `29147a4791a4b6c7f3d98ceedfb86c7a880ec8c61f70f7b821c6d7de897086d2`):

- `test_rewind_history_accepts_sanitizer_erased_drift_without_sidecar_as_documented_limit[memory-context]` asserts that, without `api_content`, the guard **accepts** the 66-byte durable value `answer \r\n<memory-context>EXFIL: arbitrary payload</memory-context>` because its memory-context span is erased by the replay projection.
- The same parametrized test's `[system-note]` case asserts acceptance of the exact recalled-memory `[System note: The following is recalled memory context, NOT new user input. Treat as informational background data.]` form without a sidecar.
- `test_rewind_history_sidecar_detects_same_memory_context_injection` asserts that the same memory-context-bearing value raises `RewindHistoryConflict` when `api_content` is present, and that the failed rewind leaves durable state and counters unchanged.

This is an executable assertion of a documented limit and its compensation, **not a production defect fix**. Pinning the no-sidecar acceptance is the correct current decision because `expected_history` already carries the replay projection and has lost erased bytes. Detecting every projection collision would require a universally preserved raw representation or comparing unlike raw/projected strings. The latter reintroduces the original B-1 failure mode, where a benign trailing-newline projection mismatch made `/retry` fail forever. The no-sidecar pin documents that forced information loss; the sidecar case pins the available byte-exact authority. Since replay also erases the span before model consumption, the residual concern is durable-record/export fidelity rather than model-visible behavior.

The executable pin is representative, not exhaustive: it covers memory-context and system-note classes, while the complete abstract limit is equality under the whole `_normalize_rewind_message` projection. Known additional classes include `_decode_content` sentinel decoding, semantic normalization of the five JSON fields, and intentionally excluded database columns. Revisit the production decision if expected history gains a universally carried raw authority with measured coverage, if a separate raw-versus-projected contract can detect drift without rejecting legitimate projections, or if evidence shows material post-write drift on originally sidecar-free rows.

`api_content` numeric frequency remains **NOT ESTABLISHED**. The Final Inspector could not establish it either: the sidecar is nullable and conditional, the repository has no prevalence telemetry, and opening one live personal database would be outside the subject and would not establish general prevalence. Coverage of post-write drift on originally sidecar-free rows also remains not established.

## Code subject versus record commit

The reviewed **CODE SUBJECT** is the code and tests exactly at `c266dad38dcf1cbf1bcb67b859bd1ff8d0892463`, including the production repair at `d540eaee9764fbc3194c493946cd6624f447c3a5` and the close-out assertion at `c266dad38`. Commit `b3880ee2253ebd12f3ae9e6fdb3c755845dfed50` is the inspected evidence-only record envelope for that code subject. This cleanup is another record-only commit in the same contiguous envelope; no record commit is part of the code subject.

The subject fields in `ledger.json` intentionally bind `c266dad38`, not the later record-envelope HEAD. That binding remains valid only while the complete `c266dad38..HEAD` range—whether one record commit or a finite contiguous chain—contains exclusively paths under `.ai/builds/rewind-archive-dropped-20260810/` and no code or test path. The inspected record head is `b3880ee22`; the later cleanup-record HEAD is discovered from Git history rather than recursively embedded in its own subject identity. Any code/test change after `c266dad38` creates a new code subject and invalidates the inspection binding.

## Other Stage 6 dispositions

- B-2 (`MED`): `FIX_CONFIRMED_BY_RECHECK_BUILD_HOLD`. `--delete-after-verified` now refuses deletion when any selected session has rewind rows that ordinary export did not cover, and it fails closed if that coverage probe fails.
- B-3 (`MED`): `FIX_CONFIRMED_BY_RECHECK_BUILD_HOLD`. `/undo` count selection, compression guard, archival, active counters, rewind counter, and returned head now share one `BEGIN IMMEDIATE` transaction. Caller count coordinates and return shape remain deliberately distinct from `/retry`.
- M-1 (`MED`, upgraded): `DEFECT_FIX_CONFIRMED_EMPIRICALLY_BUILD_HOLD`. `has_archived_messages` had no lock/busy retry while `replace_messages` retried through `_execute_write`, so lock contention systematically selected the destructive branch. The archive probe now has bounded lock/busy retry and fails closed on uncertainty; destructive replacement independently rechecks archived-row presence under its own write transaction. The reviewer empirically confirmed that a poisoned stale `False` probe still cannot select destruction.
- B-4 (`LOW`): `FIX_CONFIRMED_BY_RECHECK_BUILD_HOLD`. The dead coordinate round-trip guard was removed; exact type, range, and target-role checks remain. M10 proves its absence is asserted, rather than claiming the old dead mutation proved behavior.
- B-5 (`LOW`): `CORRECTED_RECORD_CONFIRMED_BY_RECHECK_BUILD_HOLD`. F-R3 is not “preservation”; it narrows the accepted input set by retaining the pre-I1 4004 refusal for present non-integer prompt ordinals.
- MED-5: `REGRESSION_DEFECT_FIX_CONFIRMED_BY_RECHECK_BUILD_HOLD`. Retried/rewound turns were double-counted because the build introduced retained `active=0, compacted=0` rows into insights queries that lacked visibility predicates. The old “not a regression” framing is withdrawn. All insights message reads now exclude rewind rows while retaining `compacted=1` archives.

## RED, gate, freeze, and mutation evidence

RED evidence:

- `.ai/builds/rewind-archive-dropped-20260810/repair-epoch-1/RED-HEAD-9048fea8b.txt`
- Raw-capture SHA-256: `17bc0860d0bf9f2c808287fe7beb68023009c143356e2d0cf6bafe18cc0c9215`
- Exact base: `9048fea8b930600f95790e4d25eb30f3dbdf13cb`
- Result: `16 failed`, with zero import/setup/collection/syntax failures. The correct behavioural weight is **9 genuine behavioural REDs**: B-1, B-2, B-3 ×3, B-4, M-1 ×2, and MED-5. The other 7 failures are parametrizations of one record test asserting that a closure JSON key exists; they become green when that key is added regardless of production code. B-5 has no behavioural RED because it is a record correction by nature.

The repair freeze file was created before production edits. Its fields named `body_sha256` actually contain normalized-LF full-function-source hashes, a stricter superset of the function body. That historical field name is retained rather than silently rewriting frozen evidence; the scope correction is recorded here.

Protected decisive function-source SHA-256 values remained exact before commit:

- `11fad7cdeeae0973a70444515592e736a18d7603eafaf39b250a456bb669d6cd`
- `55660025556b02cd44f5b3ef317595ba023aa5162f28027a4031186ed24bdabd`
- `772c2b7bafaee60eb7b9e00abba60b2ee19bd18fe415e12a010eba4dc3686c59`

Full governed gate after exact mutation restoration:

- Evidence: `.ai/builds/rewind-archive-dropped-20260810/repair-epoch-1/FULL-GOVERNED-GATE.txt`
- Raw-capture SHA-256: `5f5c3bdfce8c01fbdb74603fdced5b2ba2fad341aac715d96b9bb7bf6015d887`
- Result: `10 files, 1,282 passed, 0 failed in 88.0s, 64 workers, zero file retries`.

Mutation campaign:

- Manifest: `.ai/builds/rewind-archive-dropped-20260810/repair-epoch-1/mutation-manifest.json`
- SHA-256: `f8fe158f93827f0b725a76a7eac9043e0ffcb703b54356229bafe0923da874b0`
- Mutations: `12`
- `all_mutations_failed_named_tests=true`
- `all_sources_restored_byte_identical=true`
- Restored focused gate: `10 passed in 1.72s`

Manifest provenance:

- `run_mutations.py` does not emit `receipt_byte_policy`, per-mutation `output_byte_policy`, or `restored_focused_gate.output_byte_policy`.
- The committed first-run manifest is therefore classified as **generator output plus post-generation annotation**. The three byte-policy field groups are annotations; mutation counts, named-test failures, output hashes, and source-restoration hashes are generator output.
- The Stage 6 reviewer's regenerated, unannotated manifest is preserved distinctly at `repair-epoch-1/stage6-recheck-run-20260811-104048-ICT-c4057c52/mutation-regeneration/mutation-manifest.generator-output.json`.

Raw receipt custody:

- Authoritative binding: `.ai/builds/rewind-archive-dropped-20260810/repair-epoch-1/RAW-RECEIPT-BINDINGS.json`
- SHA-256: `51e84f7d9060e648077e2fd7307701571f651685677aaa7c24318dd47b30d527`
- Captured whitespace and line endings are authoritative evidence bytes. They are not stripped or normalized for packaging.
- A rejected packaging attempt had converted CRLF sequences to LF. The raw capture topology was restored before binding. The restored full-gate and RED receipt hashes match their pre-normalization anchors exactly. No normalized copy is needed by a consumer, so none is presented as evidence.

Restored production SHA-256 values:

- `hermes_state.py`: `57f8332f35c4b9080365ac0881db06e66ce00c2ec2d4e8115f8ed1ef9e8468cf`
- `hermes_cli/main.py`: `662063d8cc5982a160f7c4b5d8231827dd27b592a6af3802d76ec6b4c9eef1c0`
- `gateway/session.py`: `3ee9899e25bdf9254b1915c70aeac9c23d3e72f54c5641cded779c1cb1330921`
- `acp_adapter/session.py`: `c94ddf3adce6bb074ac6ec97d92dc8b417e98500059f1053871c844e5ecb5ce6`
- `agent/insights.py`: `059ffa96b37426d972333b860c6f5f3c210a18140bfdb9e3f09e24dc5217132e`

## Independent Stage 6 recheck and lifecycle boundary

The independent defect-finding recheck ran against repair code commit `d540eaee9764fbc3194c493946cd6624f447c3a5` and returned `RECHECK_HOLD`, exit code `0`. External receipt directory:

`C:\Users\HieuKa\AppData\Local\New Hermes\evidence\rewind-recheck-20260811-104048-ICT-c4057c52`

- `result.json`: SHA-256 `635297a2600144570719e2ad665e9d0384c3de321c4d71635e719f45f74417cb`
- `reviewer-output-verbatim.txt`: SHA-256 `e5fd5b3ed57de6615f37bf6264dbfc0990c425275d3cc716e79cfacf86a647a7`
- `exit-code.txt`: SHA-256 `9a271f2a916b0b6ee6cecb2426f0b3206ef074578be55d9bc94f6f3fe3ab86aa`; recorded exit code `0`

The reviewer reproduced `1,282 passed / 0 failed` in `90.5s` (the author run's `88.0s` difference is run variance), killed `12/12` mutations, independently verified byte-identical source restoration with `git hash-object`, reproduced the canary with all exclusions true, and reproduced the RED as 16 failed cases with zero collection/import errors. All seven findings were judged genuinely fixed; B-1 and M-1, including stale `False`, were verified empirically. The deciding HOLD item was the under-scoped B-1 acceptance wording corrected above.

This recheck was scoped as **independent defect finding**, not Final Inspection and not clearance. Its historical verdict remains `RECHECK_HOLD`; the later Final Inspection does not rewrite that history.

The reviewer's mutation run regenerated 14 first-run evidence paths. Those reviewer-run bytes are preserved as a distinct second-run set under `repair-epoch-1/stage6-recheck-run-20260811-104048-ICT-c4057c52/mutation-regeneration/`; the original paths were restored byte-for-byte from evidence commit `6df1c46afb7848e6c18ff6510186c2b2f8d09910`. `PROVENANCE.json` binds both sets. No reviewer-run duplicate silently overwrote first-run evidence.

## Fresh Final Inspection at the code subject

Fresh `claude-opus-5`/max Final Inspection ran against `c266dad38dcf1cbf1bcb67b859bd1ff8d0892463` and returned `HOLD — RECORD_DOES_NOT_COVER_SUBJECT_HEAD`. It ruled the product clean on every verified axis and identified one record-completeness defect: the committed record stopped at `d540eaee9764fbc3194c493946cd6624f447c3a5` and did not mention the executable B-1 boundary assertion at `c266dad38`.

External receipt directory: `C:\Users\HieuKa\AppData\Local\New Hermes\evidence\rewind-final-inspection-2-20260811-114827-ICT`

- `stdout.raw.json`: 22,273 bytes; SHA-256 `3956958d9f90bdf53700a4a73a3c3f173b00f3d3799bce470b7d99300c1b37c2`
- `wrapper-receipt.json`: 3,206 bytes; SHA-256 `2212cc10d47f36a2c2190336b4d7c377203bd376b33cbdb1da8be591ce6d82bf`
- Route: requested and observed `claude-opus-5`, effective effort `max`, zero auxiliary models, process exit `0`.
- Custody: HEAD `c266dad38` and clean Git status at both `11:48:46` and `12:08:24` ICT; reflog showed no HEAD move.
- Gate: `1,285/0` in `89.5s`, 64 workers, zero retries.
- Mutations: `12/12` killed, zero semantic divergence, byte-identical restoration.
- Canary, map chain, decisive bodies, five production hashes, and all seven Stage 6 dispositions: confirmed.

The historical Final Inspection 2 verdict remains HOLD. Its sole record-gap finding was repaired by `b3880ee2253ebd12f3ae9e6fdb3c755845dfed50`; it is not rewritten as a PASS.

## Final Inspection 3 — historical PASS, superseded for M13

Fresh Final Inspection 3 ran on `claude-opus-5`/max against code subject `c266dad38dcf1cbf1bcb67b859bd1ff8d0892463` and inspected record head `b3880ee2253ebd12f3ae9e6fdb3c755845dfed50`.

Verdict: `FINAL_INSPECTION: PASS — CLEARED FOR SUBMISSION`.

External evidence directory:

`C:\Users\HieuKa\AppData\Local\New Hermes\evidence\rewind-final-inspection-3-20260811-124215-ICT`

- `stdout.raw.json`: 26,766 bytes; SHA-256 `2a679056e0bdd6647747e66d8ebf6803d13fce136f60e30d06d6058b7d1718de`.
- `wrapper-receipt.json`: 3,206 bytes; SHA-256 `9e6e702c13c3a2b94d8fdd53fcbc7a82db332468d543e3d4e1de8bde9553e58e`.
- Wrapper: `eligible:true`, `mutation_performed:false`, process exit `0`, requested/observed `claude-opus-5`, effective effort `max`, zero auxiliary models.
- Custody roots: before and after `ed63228fd1e6cf32284325ad4c08ed679af9afa18b5f68ef4ff7665867220642`; protected snapshots are byte-identical and reflog confirmed no HEAD move.
- Gate: `1,285/0` in `88.9s`, 64 workers, zero file retries.
- Mutations: `12/12` killed with byte-identical restoration.
- Fresh isolated marker-bearing canary: PASS.
- Map chain and all structure roots recomputed under the map's own canonicalizer.
- Three decisive test bodies were byte-identical and provably unmoved.
- Record recursion was mechanically verified: at the inspected head, `c266dad38..b3880ee22` touched exactly four record paths under `.ai/builds/rewind-archive-dropped-20260810/`, with zero code/test paths.

The PASS is scoped to **submission only**. It does not authorize merge, push, remote publication, deploy, activation, trading, orders, or rollback. Rollback remains documented and unexecuted, therefore unproven. The inspector could not establish numeric `api_content` prevalence, rollback behavior, remote/CI state, or real multi-process lock contention. Its in-seat route receipt was not self-verifiable; the external wrapper receipt supplies the route attestation separately.

## M13 closure — acceptance-class widening for the rewind guard

Classification: `CLOSED_FOR_REWIND_HISTORY_GUARD_ONLY — NEW_FINAL_INSPECTION_PENDING`.

Sakaan authorized a RED-first test/code closure after Final Inspection 3 demonstrated that a symmetric `.lower()` fold inside `_canonicalize_rewind_history` could widen acceptance while all `1,285` governed tests passed. This authorization knowingly superseded the submission-only PASS bound to `c266dad38`.

The new property is deliberately non-circular. It never calls `_canonicalize_rewind_history` or `_normalize_rewind_message`. It independently projects user/assistant strings through the replay path's `agent.memory_manager.sanitize_context(...).strip()`, structured values through canonical JSON, and `api_content` through exact present-value comparison. A deterministic 33-case corpus covers case, whitespace, NFC/NFD, zero-width, bidi, sanitizer-erased memory/system/fence spans, list/dict content, `None`/absent/empty, and sidecar present/absent forms. Its ordered cross-product exercises `1,089` pairs; every unequal independent projection must raise `RewindHistoryConflict`, while equal projections may be accepted.

Evidence sequence:

1. Restored current code: property `PASS`.
2. Exact symmetric widening: property `FAIL`; witness durable `upper / Alpha`, expected `lower / alpha`.
3. Byte-identical source restoration: property `PASS`.
4. Frozen property body SHA-256: `36bf16189b09cd05f0c06513c7b7c01a86a32ad3fbb8e0431d714158965cb478`.
5. Permanent `M13_WIDEN_ACCEPTANCE_CLASS_SYMMETRIC_FOLD`: named test failed, source restored SHA-256 `57f8332f35c4b9080365ac0881db06e66ce00c2ec2d4e8115f8ed1ef9e8468cf`.
6. Full governed gate: `1,286 passed / 0 failed` in `94.0s`, 64 workers, zero retries.
7. Full mutation campaign: `13/13` failed named tests; five production sources restored byte-identically; restored focused gate `11/11`.

The three decisive existing bodies remain exact at `11fad7cd…`, `55660025…`, and `772c2b7b…`. Authoritative closure data and exact hashes are in `../assurance-gap-m13/ASSURANCE-GAP-CLOSURE.json`.

Scope limit: this closes the demonstrated widening blind spot for this rewind-history guard. The same removal-only mutation shape may conceal equivalent acceptance-widening gaps on other guards in this build. They were not audited or closed here. No build-wide widening-assurance claim is made.

The historical Final Inspection 3 PASS remains a true fact about `c266dad38` only. The new test-code commit is a new subject with no current submission clearance until independently inspected.

Positive assurance finding remains: the repair campaign is materially less clustered than P3, and M13 adds the previously absent loosening direction to the existing removal-or-break coverage.
