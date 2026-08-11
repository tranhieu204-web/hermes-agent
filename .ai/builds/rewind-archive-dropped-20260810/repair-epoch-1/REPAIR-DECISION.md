# Stage 6 Repair Epoch 1 Decision and Evidence

Timestamp (ICT): `2026-08-11T10:36:39+07:00`

Lifecycle: `BUILD_REVIEW_HOLD — REPAIR IMPLEMENTED — INDEPENDENT RECHECK PENDING`

Repair base: `9048fea8b930600f95790e4d25eb30f3dbdf13cb`

Repair code commit: `d540eaee9764fbc3194c493946cd6624f447c3a5`

Repair code tree: `07eded674d1b1352759ef7ff65bd5a84a4314575`

This is the same single authorized bounded repair epoch. It is not a second attempt.

## B-1 — canonical representation and guard limit

Disposition: `FIXED_PENDING_INDEPENDENT_RECHECK`.

The v3.2 plan required a byte-exact comparison but did not state which representation was canonical. That omission was the plan-level cause of B-1. Durable rows may contain a raw string such as `"answer text\n"`, while the in-memory replay history already contains `"answer text"` after `sanitize_context(content).strip()`. Comparing those unlike representations made `/retry` fail forever with `RewindHistoryConflict`, incorrectly blaming a state change.

The repair makes the caller's replay/history projection canonical and projects only the durable side into that representation:

- String content for user/assistant rows uses the established replay projection: `sanitize_context(decoded_content).strip()`.
- Structured content retains the decoded canonical JSON representation. It is never coerced to a string projection.
- `api_content`, when present, remains an independently compared byte-fidelity sidecar and is exact.
- Expected/caller history is not sanitized again. Genuine drift that is still represented by the caller therefore remains fail-closed.

Guard limit, stated explicitly:

- Whitespace-only drift in ordinary string `content` is not detectable when both raw durable values project to the same `sanitize_context(...).strip()` result. This is undetectable by construction because `expected_history` has already erased that information before the comparison receives it; the repair did not choose to discard information that remained available.
- `api_content` closes that byte-fidelity gap wherever the sidecar is present because it is compared exactly.
- Therefore the guard is not described as unqualified byte-exact. It is exact over canonical replay fields and exact over `api_content`, after the one-way durable string projection above.

Evidence:

- RED trigger uses trailing-newline content that is not a fixed point of the replay projection.
- Structured-content regression was caught by the full `tests/test_hermes_state.py` run before shipment; the repair then preserved structured values.
- M01 rejects removal of durable string projection.
- M02 rejects normalizing the caller/expected side a second time.
- M03 rejects stringification of structured content.
- M04 rejects loss of persisted `api_content` authority.

## Other Stage 6 dispositions

- B-2 (`MED`): `FIXED_PENDING_INDEPENDENT_RECHECK`. `--delete-after-verified` now refuses deletion when any selected session has rewind rows that ordinary export did not cover, and it fails closed if that coverage probe fails.
- B-3 (`MED`): `FIXED_PENDING_INDEPENDENT_RECHECK`. `/undo` count selection, compression guard, archival, active counters, rewind counter, and returned head now share one `BEGIN IMMEDIATE` transaction. Caller count coordinates and return shape remain deliberately distinct from `/retry`.
- M-1 (`MED`, upgraded): `DEFECT_FIXED_PENDING_INDEPENDENT_RECHECK`. `has_archived_messages` had no lock/busy retry while `replace_messages` retried through `_execute_write`, so lock contention systematically selected the destructive branch. The archive probe now has bounded lock/busy retry and fails closed on uncertainty; destructive replacement independently rechecks archived-row presence under its own write transaction, so stale `False` cannot select destruction.
- B-4 (`LOW`): `FIXED_PENDING_INDEPENDENT_RECHECK`. The dead coordinate round-trip guard was removed; exact type, range, and target-role checks remain. M10 proves its absence is asserted, rather than claiming the old dead mutation proved behavior.
- B-5 (`LOW`): `CORRECTED_RECORD`. F-R3 is not “preservation”; it narrows the accepted input set by retaining the pre-I1 4004 refusal for present non-integer prompt ordinals.
- MED-5: `REGRESSION_DEFECT_FIXED_PENDING_INDEPENDENT_RECHECK`. Retried/rewound turns were double-counted because the build introduced retained `active=0, compacted=0` rows into insights queries that lacked visibility predicates. The old “not a regression” framing is withdrawn. All insights message reads now exclude rewind rows while retaining `compacted=1` archives.

## RED, gate, freeze, and mutation evidence

RED evidence:

- `.ai/builds/rewind-archive-dropped-20260810/repair-epoch-1/RED-HEAD-9048fea8b.txt`
- Raw-capture SHA-256: `17bc0860d0bf9f2c808287fe7beb68023009c143356e2d0cf6bafe18cc0c9215`
- Exact base: `9048fea8b930600f95790e4d25eb30f3dbdf13cb`
- Result: `16 failed`; failures were required-behavior absence, not import/setup/collection failures.

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

## Lifecycle boundary

The owner independently executed the documented B-1 trailing-newline trigger and the structured-content case against `d540eaee9764fbc3194c493946cd6624f447c3a5`; both succeeded. That execution confirmation is not the formal Stage 6 independent recheck. The repaired code has had no independent Stage 6 review, and the HOLD is not self-cleared by the builder. The same eligible independent route must recheck exact repair code commit `d540eaee9764fbc3194c493946cd6624f447c3a5` plus the closure-record commit. No push, merge, rebase, protected-ref move, deployment, activation, trade, or order occurred.
