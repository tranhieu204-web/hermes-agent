# Stage 6 Repair Epoch 1 Decision and Evidence

Timestamp (ICT): `2026-08-11T11:05:33+07:00`

Lifecycle: `BUILD_REVIEW_HOLD — STAGE 6 RECHECK_HOLD RECORDED — ACCEPTANCE CLAIM CORRECTED — FINAL INSPECTION NOT PERFORMED`

Repair base: `9048fea8b930600f95790e4d25eb30f3dbdf13cb`

Repair code commit: `d540eaee9764fbc3194c493946cd6624f447c3a5`

Repair code tree: `07eded674d1b1352759ef7ff65bd5a84a4314575`

This is the same single authorized bounded repair epoch. It is not a second attempt.

## B-1 — canonical representation and guard limit

Disposition: `CODE_FIX_CONFIRMED_BY_INDEPENDENT_DEFECT-FINDING RECHECK — BUILD NOT CLEARED`.

The v3.2 plan required a byte-exact comparison but did not state which representation was canonical. That omission was the plan-level cause of B-1. Durable rows may contain a raw string such as `"answer text\n"`, while the in-memory replay history already contains `"answer text"` after `sanitize_context(content).strip()`. Comparing those unlike representations made `/retry` fail forever with `RewindHistoryConflict`, incorrectly blaming a state change.

The repair makes the caller's replay/history projection canonical and projects only the durable side into that representation:

- String content for user/assistant rows uses the established replay projection: `sanitize_context(decoded_content).strip()`.
- Structured content retains the decoded canonical JSON representation. It is never coerced to a string projection.
- `api_content`, when present, remains an independently compared byte-fidelity sidecar and is exact.
- Expected/caller history is not sanitized again. Genuine drift that is still represented by the caller therefore remains fail-closed.

Guard limit, stated explicitly and corrected after the Stage 6 recheck:

- For ordinary string `content` without an `api_content` sidecar, **every difference collapsed by `sanitize_context(...).strip()` is undetectable to this comparison**. This is materially broader than whitespace. It includes arbitrary-length `<memory-context>...</memory-context>` spans removed by `_INTERNAL_CONTEXT_RE`, the recalled-memory `[System note: ...]` form removed by `_INTERNAL_NOTE_RE`, and fence tags removed by `_FENCE_TAG_RE`. See `agent/memory_manager.py:163-181`, including the requested source window `:168-181`.
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

This recheck was scoped as **independent defect finding**, not Final Inspection and not clearance. Its historical verdict remains `RECHECK_HOLD`; this builder-side record correction cannot transform it into PASS. Final Inspection has not occurred, so merge, push, deployment, activation, trading, and orders remain unauthorized.

The reviewer's mutation run regenerated 14 first-run evidence paths. Those reviewer-run bytes are preserved as a distinct second-run set under `repair-epoch-1/stage6-recheck-run-20260811-104048-ICT-c4057c52/mutation-regeneration/`; the original paths were restored byte-for-byte from evidence commit `6df1c46afb7848e6c18ff6510186c2b2f8d09910`. `PROVENANCE.json` binds both sets. No reviewer-run duplicate silently overwrote first-run evidence.
