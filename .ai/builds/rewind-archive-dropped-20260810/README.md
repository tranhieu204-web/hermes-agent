# Rewind Archive Dropped — Build Usage and Closure Note

Build ID: `rewind-archive-dropped-20260810`

Branch: `sakaan/rewind-archive-dropped-20260810`

Implementation terminal before review: `daf5dc1e9ae33ee0f2a269b0fe82732a7f2fcdfb`

Stage 6 repair code commit: `d540eaee9764fbc3194c493946cd6624f447c3a5`

Historical Final Inspection 4 code subject: `389640f70679257b7acf074808e44277f31fb92a`

M14-M16 candidate parent: `389640f70679257b7acf074808e44277f31fb92a`; the current candidate is the **uncommitted working-tree record/evidence set**.

Status: `M14-M16 RECORD-HYGIENE SUCCESSOR IN PROGRESS — FINAL INSPECTION 4 PASS SUPERSEDED — NEW FINAL INSPECTION PENDING`

Record-hygiene successor scope: correct the stale M14-M16 property-body binding, bind the governed gate to parent-side before/after identity snapshots, refresh the closure-summary timestamp, make the M14-M16 RED proofs durable, and mark the superseded M13 assurance node historical. This successor is record/evidence-only; no production or test source change is authorized or included.

## What changed

This build replaces destructive Restore/Edit/Re-run and retry truncation with recoverable session-level archival while preserving destructive behavior for callers that are intentionally destructive.

- **P1** added transactional active-suffix archival, persistence-before-memory/turn ordering, strict coordinates, Desktop/TUI routing, durable counters, and active-or-compacted dedupe handling.
- **P2** moved gateway `/retry` to recoverable archival, checks durable success before token reset or resend, preserves dirty state after persistence failure, and keeps `/undo`'s message-ID API as an explicit MED-3 divergence.
- **P3** exposes read-only, one-session JSONL recovery through `hermes sessions export --include-rewound`. Recovery reads raw rows, includes active rows plus `active=0, compacted=0` rewind rows, excludes compacted history, and preserves content bytes that conversation projection would sanitize or strip.
- **Stage 6 repair epoch 1** fixes B-1 through B-4 and M-1 and corrects B-5/F-R3 and MED-5. The independent defect-finding recheck judged all seven findings genuinely fixed but returned `RECHECK_HOLD` because the recorded B-1 guard limit was materially under-scoped. That acceptance claim is corrected in `repair-epoch-1/REPAIR-DECISION.md`; the recheck was not Final Inspection and did not clear the build.
- **Stage 6 close-out test commit** `c266dad38dcf1cbf1bcb67b859bd1ff8d0892463` adds three executable acceptance cases in `tests/test_hermes_state.py`. Without `api_content`, the rewind guard accepts sanitizer-erased drift for a 66-byte `<memory-context>...` value and the recalled-memory `[System note: ...]` form; with `api_content`, the same memory-context-bearing value raises `RewindHistoryConflict` and leaves durable state unchanged. This commit documents a representational limit and its sidecar compensation; it does not fix a production defect.
- **M13 assurance closure** added the independent content/`api_content` property and permanent symmetric-fold loosening mutation. Final Inspection 4 passed exact subject `389640f70679257b7acf074808e44277f31fb92a`, reproduced its `upper / Alpha` versus `lower / alpha` witness, and credited the record's pre-disclosure that `body_sha256` means normalized-LF full-function-source hash and that the historical manifest contains classified post-generation annotations. The inspector also demonstrated that M13's old `CLOSED_FOR_REWIND_HISTORY_GUARD_ONLY` label was too broad: role, the five `_REWIND_JSON_FIELDS`, and message order were not varied.
- **M14-M16 assurance extension** preserves all 33 content/`api_content` cases and extends the independent corpus to 51 histories and 2,601 ordered pairs across role (`user`/`assistant`/`tool`/`system`), all five JSON fields, and one- to three-message ordering. Exactly `2,494` pairs have unequal independent projections and therefore constrain rejection. Permanent loosening mutations M14 role-drop, M15 JSON-field-drop, and M16 order-insensitive comparison each fail on a named witness and restore production source byte-identically.

Default replay, export, and search remain active-only. Yuanbao recall/redaction, rotated compression, and ordinary hard replacement remain destructive.

## Governed gate

From repository root in Git Bash:

```bash
HERMES_PYTHON='/c/c/Users/HieuKa/Desktop/hermes-rewind-archive-20260810/.venv/Scripts/python.exe' \
  scripts/run_tests.sh --file-retries 0 \
  tests/test_hermes_state.py \
  tests/gateway/test_dedupe_user_turns.py \
  tests/test_tui_gateway_server.py \
  tests/cli/test_cli_retry.py \
  tests/gateway/test_retry_response.py \
  tests/gateway/test_session.py \
  tests/hermes_cli/test_sessions_export_rewound.py \
  tests/acp/test_session.py \
  tests/hermes_cli/test_sessions_export_md_cli.py \
  tests/agent/test_insights.py -q
```

Historical repair-code result at `d540eaee9764fbc3194c493946cd6624f447c3a5`: `1,282 passed / 0 failed` across ten files in `88.0s`, with 64 workers and zero file retries. The authoritative raw runner output is bound at `repair-epoch-1/FULL-GOVERNED-GATE.txt` SHA-256 `5f5c3bdfce8c01fbdb74603fdced5b2ba2fad341aac715d96b9bb7bf6015d887`.

Historical code-subject result at `c266dad38dcf1cbf1bcb67b859bd1ff8d0892463`: the author gate passed `1,285/0` in `90.8s`; Final Inspection 3 independently passed `1,285/0` in `88.9s`, 64 workers, zero file retries. That inspection and its submission-only clearance are historical and superseded for the new candidate.

Final Inspection 4 independently passed exact M13 subject `389640f70679257b7acf074808e44277f31fb92a`: `1,286/0`, `13/13` mutations killed, canary PASS, clean custody, and exact M13 witness reproduction. That submission-only clearance is historical and superseded by the M14-M16 test-code change.

Current M14-M16 candidate result: `1,286 passed / 0 failed` across the same ten governed files in `122.4s`, with 64 workers and zero file retries. Actual runner output: `assurance-gap-m14-m16/FULL-GOVERNED-GATE.txt`, SHA-256 `416e81f888c9b0dde40f9413ffade407d4022ad59c08f60f24c5de0a091269a9`.

Record-hygiene gate binding: `assurance-gap-m14-m16/RECORD-HYGIENE-GATE-BINDING.json`, SHA-256 `3946d59c1f23ab4609326f0a9bd1849e54fa51b275fd6a15fb3902f0b3971925`. Timestamps in the successor records are explicitly materialization timestamps, not execution timestamps.

Repair plus M13-M16 mutation runner:

```bash
./.venv/Scripts/python.exe \
  .ai/builds/rewind-archive-dropped-20260810/repair-epoch-1/run_mutations.py
```

Result: 16 mutations; all 16 failed their named tests, including permanent loosening mutations M13 symmetric fold, M14 role-drop, M15 five-JSON-field-drop, and M16 order-insensitive comparison. All five production sources restored byte-for-byte; restored focused gate exit `0`; runner exit `0`. Immutable manifest snapshot: `assurance-gap-m14-m16/MUTATION-MANIFEST.json`, SHA-256 `a7499d2254e06c1000045c3a082e9b6b12e95ba2d4fa40bd3b759c6e0a2b5258`.

The runner writes all mutable receipts and its live manifest only under gitignored `tmp/rewind-m14-m16-mutation-campaign/`. It did not overwrite any committed receipt. The committed manifest is an immutable post-run byte-exact snapshot; its receipt paths intentionally identify local gitignored evidence custody.

## Recovery export

Use a single session and JSONL output:

```bash
./.venv/Scripts/python.exe -c 'from hermes_cli.main import main; main()' \
  sessions export \
  --include-rewound \
  --session-id '<SESSION_ID>' \
  --format jsonl \
  '<OUTPUT.jsonl>'
```

`--include-rewound` is incompatible with `--only`, logical lineage, `--delete-after-verified`, redaction, session filters, dry-run, and trace-upload options.

## Frozen canary

The canary is runnable at:

```text
.ai/builds/rewind-archive-dropped-20260810/canary_rewind_archive.py
```

Exact invocation from repository root in Git Bash:

```bash
./.venv/Scripts/python.exe \
  .ai/builds/rewind-archive-dropped-20260810/canary_rewind_archive.py \
  --root 'C:/Users/HieuKa/AppData/Local/Temp/p3-canary-rewind-archive-<UNIQUE-RUN-ID>'
```

Requirements:

1. `--root` must be a new disposable path for that run.
2. The root path name must contain the mandatory `p3-canary` marker; the script refuses roots without it.
3. Never pass a live Hermes profile or live `HERMES_HOME`.
4. The script creates `<root>/hermes-home/state.db`; it must remain isolated from the running Desktop and gateway.
5. Expected result is JSON with `"status": "PASS"`, `recovered_sentinel_count: 1`, `retained_prefix_count: 1`, all three default-surface exclusions true, and `hard_replace_remains_destructive: true`.

## Rollback

Rollback was documented but **not executed** because no rollback was authorized.

The actual code-subject history contains a P1 prerequisite, three package-terminal commits, the Stage 6 repair code commit, and the Stage 6 close-out test commit:

```text
P1 prerequisite: 3235e6b1d41b3b225bc41c5ac8eed4c662a8666e
P1 terminal:     bc6c801a5734d30543a908c18233728b691e9e9e
P2 terminal:     1e8bf80feefd668c247e0c569e28131ae0a2bce4
P3 terminal:     daf5dc1e9ae33ee0f2a269b0fe82732a7f2fcdfb
Stage 6 repair:  d540eaee9764fbc3194c493946cd6624f447c3a5
Boundary test:   c266dad38dcf1cbf1bcb67b859bd1ff8d0892463
```

Forward-only behavioral rollback of the repair plus three package-terminal commits, newest first:

```bash
git revert --no-edit \
  c266dad38dcf1cbf1bcb67b859bd1ff8d0892463 \
  d540eaee9764fbc3194c493946cd6624f447c3a5 \
  daf5dc1e9ae33ee0f2a269b0fe82732a7f2fcdfb \
  1e8bf80feefd668c247e0c569e28131ae0a2bce4 \
  bc6c801a5734d30543a908c18233728b691e9e9e
```

That removes the P3 recovery surface, P2 recoverable retry integration, and P1 persistence-before-effects routing. Restore/Edit/Re-run and retry behavior becomes destructive again. The uncalled P1 archival primitive from `3235e6b1...` remains in source.

To revert all build bytes back toward exact base `872c341302b5ed8941f280c3b7939cabba930b5a`, also revert the P1 prerequisite in the same newest-to-oldest transaction:

```bash
git revert --no-edit \
  c266dad38dcf1cbf1bcb67b859bd1ff8d0892463 \
  d540eaee9764fbc3194c493946cd6624f447c3a5 \
  daf5dc1e9ae33ee0f2a269b0fe82732a7f2fcdfb \
  1e8bf80feefd668c247e0c569e28131ae0a2bce4 \
  bc6c801a5734d30543a908c18233728b691e9e9e \
  3235e6b1d41b3b225bc41c5ac8eed4c662a8666e
```

Any rollback is a new authority-gated transaction. The Final Inspector warned that rollback may conflict in `tests/test_hermes_state.py`; rollback remains documented and unexecuted. Stop on conflicts, rerun the governed ten-file gate, read back the new revert SHA, and record whether destructive rewind has returned.

## Code subject and record recursion

Final Inspection 4 **CODE SUBJECT** is `389640f70679257b7acf074808e44277f31fb92a` (tree `f850cb16b89bcb2629d1745eaac3631bdb254300`). It returned `PASS — CLEARED FOR SUBMISSION` for that exact subject only.

M14-M16 changes `tests/test_hermes_state.py`, the permanent mutation runner, and the evidence record. Therefore Final Inspection 4 clearance is historical and no longer current. The current code/test subject is the **uncommitted working-tree record/evidence set** and must receive a new independent Final Inspection before submission clearance can exist. No production source byte changed; `hermes_state.py` remains SHA-256 `57f8332f35c4b9080365ac0881db06e66ce00c2ec2d4e8115f8ed1ef9e8468cf`.

## Why the B-1 limit is pinned, not normalized

`expected_history` already carries replay-projected content and no longer contains bytes erased by the durable-side projection. Detecting every collision without another raw authority would require either a universal raw-content field or comparison of unlike raw/projected strings. The latter is the original B-1 failure mode: legitimate projection differences such as the trailing newline made `/retry` fail forever. The executable no-sidecar acceptance cases therefore document a forced information limit, while the companion `api_content` case pins the exact-byte compensation. The erased span is absent from model-visible replay too; the residual concern is durable-record/export fidelity, not model behavior.

The earlier sanitizer examples are a **known lower bound, not an exhaustive enumeration**. The complete abstract boundary is equivalence under the whole `_normalize_rewind_message` projection. Known collapse classes include: `sanitize_context(...).strip()` for user/assistant strings; `_decode_content` sentinel decoding; semantic decoding of the five `_REWIND_JSON_FIELDS`, which discards JSON byte formatting and key order; and the deliberately excluded `id`, `session_id`, `active`, `compacted`, and `timestamp` columns. Tool-role string content remains byte-exact **except when it carries the `_encode_content` structured-content sentinel**: `_decode_content` runs for all roles, so two distinct durable tool-role byte strings that decode to the same structure (for example `\x00json:{"b":1,"a":2}` and `\x00json:{"a":2,"b":1}`) project identically. This is reachable in production wherever structured content is stamped, and is already covered by the `_decode_content` collapse class named above; the unqualified wording previously stated here was imprecise. `api_content` remains exact when present; its prevalence and post-write-drift coverage remain `NOT_ESTABLISHED`.

This decision is not permanent by assertion alone. Revisit it if expected history gains a universally carried raw authority with measured coverage, if a separate raw-versus-projected contract can detect drift without rejecting legitimate replay projections, or if evidence shows material post-write fence-bearing drift on originally sidecar-free rows. The executable pin currently covers only memory-context and system-note representatives plus sidecar compensation; it does not prove or parametrize every member of the full projection-equivalence class.

## ENUMERATED ASSURANCE FENCE — rewind-history guard

Classification: `FENCED FOR ENUMERATED DIMENSIONS — NEW FINAL INSPECTION PENDING`.

The old M13 label `CLOSED_FOR_REWIND_HISTORY_GUARD_ONLY` was too broad. Final Inspection 4 established that M13 covered content and `api_content` on single-message user-role histories, but did not constrain role, the five `_REWIND_JSON_FIELDS`, or message ordering. The M14-M16 extension keeps every one of the original 33 content/sidecar cases and adds role variation, two values for each JSON field, and ordered multi-message histories. The resulting corpus has 51 histories, 2,601 ordered pairs, and exactly 2,494 constraining pairs whose independent projections differ and therefore must raise `RewindHistoryConflict`.

Fenced dimensions are: the original content representatives; exact `api_content` when present; roles `user`, `assistant`, `tool`, and `system`; `tool_calls`, `reasoning_details`, `codex_reasoning_items`, `codex_message_items`, and `display_metadata`; and represented one-, two-, and three-message ordering including a full reversal and an adjacent transposition. The oracle remains independent of `_canonicalize_rewind_history` and `_normalize_rewind_message`.

The property fails under every permanent loosening direction M13-M16. M14 witnesses `role-user` versus `role-assistant`; M15 witnesses legitimate `real_tool` versus `ATTACKER_TOOL` and also varies `display_metadata`; M16 witnesses `order-forward` versus `order-reversed` and includes adjacent transposition. Each temporary mutation restored `hermes_state.py` byte-identically. The frozen property's normalized-LF full-function-source SHA-256 is `84e33c30dd8db64a36635b98145b1c58a5c0e0ffca24bdf3692da5bbca0f74ae`.

Known limits, stated without inflation: a widening inside `sanitize_context` source is undetectable because the guard and oracle share that defined replay projection; this is reasoned, not empirically proved. The property does not vary the other semantic fields (`tool_call_id`, `tool_name`, `effect_disposition`, `token_count`, `finish_reason`, `reasoning`, `reasoning_content`, `platform_message_id`/`message_id`, `observed`, `display_kind`), arbitrary lengths/permutations beyond represented cases, inactive archived-row integrity, or other guards in the build. Production currently compares those semantic fields, but this property does not claim to fence their widening.

All `16/16` mutations fail named tests; all five production sources restore byte-identically; the actual governed gate passes `1,286/0`. Closure record: `assurance-gap-m14-m16/ASSURANCE-GAP-CLOSURE.json`.

## Open remainders

- **Final Inspection:** Attempt 4 returned `PASS — CLEARED FOR SUBMISSION ONLY` for exact subject `389640f70679257b7acf074808e44277f31fb92a`. Wrapper `eligible:true`; Opus 5/max throughout; clean custody; `1,286/0`; `13/13`; canary PASS; M13 witness reproduced. M14-M16 changes test code, so that clearance is `SUPERSEDED / NOT CURRENT`; the new commit requires a fresh independent inspection. Receipt: `C:\\Users\\HieuKa\\AppData\\Local\\New Hermes\\evidence\\rewind-final-inspection-4-20260811-145322-ICT\\stdout.raw.json`, SHA-256 `8781b13a0c588cecdd915701c522a683598a7be8034e5911b3ce407b2afc4761`.
- **Enumerated assurance fence:** content/`api_content`, role, all five JSON fields, and represented ordering are fenced by 2,494 exact constraining pairs plus permanent M13-M16 loosening mutations. A widening inside `sanitize_context` source remains a reasoned, unproven blind spot; other semantic fields, broader history shapes, archived-row integrity, and other guards remain unassessed by this property.
- **Independent Stage 6 recheck:** `RECHECK_HOLD`. Receipt: `C:\Users\HieuKa\AppData\Local\New Hermes\evidence\rewind-recheck-20260811-104048-ICT-c4057c52`. The reviewer reproduced `1,282/0` in `90.5s`, killed `12/12` mutations with byte-identical restoration independently verified by `git hash-object`, passed the canary with all exclusions true, and reproduced RED with zero collection/import errors. All seven findings were judged genuinely fixed. The deciding HOLD item was the under-scoped B-1 acceptance wording. This was defect-finding recheck, not Final Inspection or clearance.
- **B-1:** `CODE FIX CONFIRMED; ACCEPTANCE CLAIM CORRECTED; M13-M16 ENUMERATED WIDENING FENCE ADDED; NEW FINAL INSPECTION PENDING`. Root cause: byte-exact comparison was specified without stating which representation was canonical. Replay/projected content is canonical, structured JSON stays structured, and `api_content` is compared exactly when present. The earlier sanitizer examples are a known lower bound, not a complete characterization. The actual boundary is equality under the whole `_normalize_rewind_message` projection, including user/assistant sanitizer collapse, `_decode_content` sentinel decoding, semantic normalization of the five JSON fields, and deliberately excluded database columns; tool-role strings remain byte-exact. The earlier whitespace-only wording came from the COO instruction and was recorded faithfully by the builder; the COO corrected it after empirical review.
- **`api_content` prevalence:** `NOT ESTABLISHED`. Code proves it is nullable and conditional: no injection or sanitization-changing content can yield zero sidecars; only the current user row is composed/stamped for injected context, run-agent stamping is conditional, gateway/branch sites forward only an existing sidecar, and content rewrites drop stale sidecars. No repository telemetry establishes a percentage, and coverage of post-write drift on originally sidecar-free rows is not established.
- **RED weight:** 16 failed cases total and zero collection/import/syntax errors, but only 9 are genuine behavioural REDs: B-1, B-2, B-3 ×3, B-4, M-1 ×2, and MED-5. The remaining 7 are parametrizations of one closure-key assertion. B-5 has no behavioural RED because it is a record correction.
- **B-2 / MED-4:** `FIX CONFIRMED BY RECHECK`. Destructive export refuses deletion when ordinary export did not cover rewind rows, including probe uncertainty.
- **B-3:** `FIX CONFIRMED BY RECHECK`. `/undo` count selection, compression guard, archival, counters, and returned head are one durable transaction; its count coordinate and return shape remain deliberately distinct from `/retry`.
- **M-1:** `DEFECT FIX CONFIRMED EMPIRICALLY`. The accepted-residual classification is withdrawn. `has_archived_messages` had no retry while `replace_messages` retried through `_execute_write`, so lock contention systematically selected destruction. The reviewer verified the stale-`False` case: a poisoned preliminary probe cannot select destruction because replacement rechecks under its write transaction. `P3-M1-ACP-RESIDUAL.md` is explicitly superseded historical P3 evidence, not a current open residual.
- **B-4:** `FIX CONFIRMED BY RECHECK`. The dead coordinate round-trip guard is removed; exact type, range, and target-role checks remain.
- **B-5 / F-R3:** `CORRECTED_RECORD — NARROWS_ACCEPTED_INPUT_SET`. Retaining pre-I1 4004 for a present non-integer prompt ordinal narrows accepted input; it is not described as preservation.
- **MED-5:** `REGRESSION DEFECT FIX CONFIRMED BY RECHECK`. The earlier “not a regression” framing is withdrawn. Retained rewind rows caused retried activity to be double-counted. Insights now applies active-or-compacted visibility to every message read, excluding rewind rows and retaining compaction archives.
- **LOW-1:** `OBSERVED_LOW_DIAGNOSABILITY_V1 — SWALLOWED_ROLLBACK_CAUSE`. `_execute_write` may discard a rollback exception. This remains pre-existing and diagnosability-only; the path fails closed. No repair-epoch change was made.
- Merge: not done.
- Push or remote publication: not done.
- CI for a remote/integrated SHA: not done.
- Deployment: not done.
- Activation: not done.
- Trading or orders: not done.

## Positive read-path finding

Search filtering is correct and consistent on the three named FTS paths. When `include_inactive` is false, each carries `(m.active = 1 OR m.compacted = 1)`:

- trigram path at `hermes_state.py:8035`;
- main FTS path at `hermes_state.py:8218`;
- CJK path at `hermes_state.py:8317`.

Therefore rewind rows (`active=0, compacted=0`) are excluded while compaction archives (`compacted=1`) remain discoverable. A missing predicate on any one of the three paths would have leaked rewind rows; none is missing. Direct source inspection at code subject `c266dad38` also found the same predicate on the later trigram fallback (`:8406`), LIKE fallback (`:8500`), and unindexed-gap path (`:8742`).

## COO technical audit scope, safe findings, and limit

Historical status: `DIRECT_VERIFIED — NOT_INDEPENDENTLY_CLEARED`. This audit was superseded by the independent Stage 6 HOLD and the directly verified repair; it is not current clearance.

The cumulative COO technical audit covered six areas:

1. read-path visibility and analytics population;
2. `_execute_write` retry-path safety;
3. I1 compare/mutate TOCTOU closure;
4. rewind-history normalizer mismatch resistance;
5. ordinal and whole-transcript boundaries;
6. an independent ordinal-boolean mutation spot-check.

Verified safe by COO code reading:

- **Retry-path safety:** at code subject `c266dad38`, `_execute_write` calls rollback at `hermes_state.py:2139` before any lock/busy retry. A retried callback therefore re-runs against an unchanged database when rollback succeeds. If rollback fails and leaves the transaction open, the next `BEGIN IMMEDIATE` at `:2133` can raise a transaction-nesting error that propagates through `:2161-2162`; it fails closed. Compare-then-mutate is idempotent in effect across successful retries.
- **I1 and M-1 TOCTOU:** the original compare/mutate lock remains. Stage 6 additionally closed M-1 with a fail-closed retrying probe and an authoritative archive recheck inside the replacement write transaction.
- **Normalizer correction:** the earlier audit missed B-1 because v3.2 never selected the canonical representation. The repair projection and its full sanitize-equivalence limit in `repair-epoch-1/REPAIR-DECISION.md` supersede both the older broad mismatch-resistance claim and the later under-scoped whitespace-only wording.
- **Ordinal boundaries:** an empty active-user set makes every ordinal out of range and fails closed. Ordinal `0` is the valid whole-transcript archive when an active user exists and is gated upstream by confirmation error `4028`. Negative, non-integer, and boolean values are refused. At code subject `c266dad38`, the guard at `hermes_state.py:7556` uses `type(x) is not int`, not `isinstance`, which correctly prevents Python booleans from passing as integers.
- **Mutation spot-check:** the COO independently replaced `type(x) is not int` with `not isinstance(x, int)`. Exactly `test_rejects_invalid_ordinal_without_any_mutation[True]` failed; no other parametrized case failed. Source was then restored byte-identical to SHA-256 `8432a42218866a507a7cefe0be61f534566d370bc7ef20f3b23def964e121d83`. This directly supports the P1.C2 manifest's ordinal-boolean claim and shows the test discriminates the boolean boundary precisely.

Audit limit:

- The COO materially contributed to the build's technical direction and is ineligible to perform independent BUILD inspection.
- Stage 6 answered the seventh area: the cleared plan was wrong because v3.2 demanded byte-exact comparison without selecting the canonical representation.
- The resulting repair was independently defect-found and judged functionally correct. Historical `RECHECK_HOLD` remains part of the record, and later Final Inspection 3 cleared code subject `c266dad38` for submission only.

## Mechanical closure gate

The historical Final Inspection 3 PASS remains valid only for exact code subject `c266dad38` and inspected record head `b3880ee22`; it is superseded for the M13 candidate because `tests/test_hermes_state.py` changed. The new commit has no submission clearance and requires fresh independent Final Inspection. The mechanical pipeline checker remains fail-closed; integration, merge, push, remote readback, CI, artifact, deployment, activation, trading, orders, and rollback proof remain absent or unauthorized.

The current M14-M16 candidate remains fenced by the exact current record set: `assurance-gap-m14-m16/ASSURANCE-GAP-CLOSURE.json` (current closure), `BUILD-CLOSURE-SUMMARY.json`, and `repair-epoch-1/REPAIR-DECISION.md`. The historical M13 closure is retained separately for provenance only. The current M14-M16 closure covers the demonstrated widening-direction gap for the rewind-history guard only and explicitly does not claim equivalent coverage for other guards.
