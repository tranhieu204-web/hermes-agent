# Rewind Archive Dropped — Build Usage and Closure Note

Build ID: `rewind-archive-dropped-20260810`

Branch: `sakaan/rewind-archive-dropped-20260810`

Implementation terminal before review: `daf5dc1e9ae33ee0f2a269b0fe82732a7f2fcdfb`

Stage 6 repair code commit: `d540eaee9764fbc3194c493946cd6624f447c3a5`

Status: `BUILD_REVIEW_HOLD — STAGE 6 RECHECK_HOLD RECORDED — ACCEPTANCE CLAIM CORRECTED — FINAL INSPECTION NOT PERFORMED`

## What changed

This build replaces destructive Restore/Edit/Re-run and retry truncation with recoverable session-level archival while preserving destructive behavior for callers that are intentionally destructive.

- **P1** added transactional active-suffix archival, persistence-before-memory/turn ordering, strict coordinates, Desktop/TUI routing, durable counters, and active-or-compacted dedupe handling.
- **P2** moved gateway `/retry` to recoverable archival, checks durable success before token reset or resend, preserves dirty state after persistence failure, and keeps `/undo`'s message-ID API as an explicit MED-3 divergence.
- **P3** exposes read-only, one-session JSONL recovery through `hermes sessions export --include-rewound`. Recovery reads raw rows, includes active rows plus `active=0, compacted=0` rewind rows, excludes compacted history, and preserves content bytes that conversation projection would sanitize or strip.
- **Stage 6 repair epoch 1** fixes B-1 through B-4 and M-1 and corrects B-5/F-R3 and MED-5. The independent defect-finding recheck judged all seven findings genuinely fixed but returned `RECHECK_HOLD` because the recorded B-1 guard limit was materially under-scoped. That acceptance claim is corrected in `repair-epoch-1/REPAIR-DECISION.md`; the recheck was not Final Inspection and did not clear the build.

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

Repair result on exact restored code: `1,282 passed / 0 failed` across ten files in `88.0s`, with 64 workers and zero file retries. The authoritative raw runner output is bound at `repair-epoch-1/FULL-GOVERNED-GATE.txt` SHA-256 `5f5c3bdfce8c01fbdb74603fdced5b2ba2fad341aac715d96b9bb7bf6015d887`.

Stage 6 repair mutation runner:

```bash
./.venv/Scripts/python.exe \
  .ai/builds/rewind-archive-dropped-20260810/repair-epoch-1/run_mutations.py
```

Result: 12 mutations; every mutation failed its named test, all five production sources were restored byte-for-byte, the restored focused gate passed `10/10`, and the runner exited `0`. Manifest SHA-256: `f8fe158f93827f0b725a76a7eac9043e0ffcb703b54356229bafe0923da874b0`.

Manifest provenance: `run_mutations.py` does not emit `receipt_byte_policy`, per-mutation `output_byte_policy`, or `restored_focused_gate.output_byte_policy`. The committed first-run manifest is therefore generator output plus post-generation annotation; those three byte-policy field groups are annotations. The reviewer's unannotated regeneration is preserved separately under `repair-epoch-1/stage6-recheck-run-20260811-104048-ICT-c4057c52/mutation-regeneration/`.

Captured receipt bytes are authoritative, including trailing whitespace and mixed line endings. They are bound in `repair-epoch-1/RAW-RECEIPT-BINDINGS.json` SHA-256 `51e84f7d9060e648077e2fd7307701571f651685677aaa7c24318dd47b30d527`. A rejected packaging attempt had changed CRLF sequences to LF; the exact capture topology was restored before binding, and the full-gate and RED hashes match their pre-normalization anchors. No normalized receipt copy is required or presented as evidence.

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
  --root 'C:/Users/HieuKa/AppData/Local/Temp/rewind-archive-canary-<UNIQUE-RUN-ID>'
```

Requirements:

1. `--root` must be a new disposable path for that run.
2. Never pass a live Hermes profile or live `HERMES_HOME`.
3. The script creates `<root>/hermes-home/state.db`; it must remain isolated from the running Desktop and gateway.
4. Expected result is JSON with `"status": "PASS"`, `recovered_sentinel_count: 1`, `retained_prefix_count: 1`, all three default-surface exclusions true, and `hard_replace_remains_destructive: true`.

## Rollback

Rollback was documented but **not executed** because no rollback was authorized.

The actual build history contains a P1 prerequisite, three package-terminal commits, and the Stage 6 repair code commit:

```text
P1 prerequisite: 3235e6b1d41b3b225bc41c5ac8eed4c662a8666e
P1 terminal:     bc6c801a5734d30543a908c18233728b691e9e9e
P2 terminal:     1e8bf80feefd668c247e0c569e28131ae0a2bce4
P3 terminal:     daf5dc1e9ae33ee0f2a269b0fe82732a7f2fcdfb
Stage 6 repair:  d540eaee9764fbc3194c493946cd6624f447c3a5
```

Forward-only behavioral rollback of the repair plus three package-terminal commits, newest first:

```bash
git revert --no-edit \
  d540eaee9764fbc3194c493946cd6624f447c3a5 \
  daf5dc1e9ae33ee0f2a269b0fe82732a7f2fcdfb \
  1e8bf80feefd668c247e0c569e28131ae0a2bce4 \
  bc6c801a5734d30543a908c18233728b691e9e9e
```

That removes the P3 recovery surface, P2 recoverable retry integration, and P1 persistence-before-effects routing. Restore/Edit/Re-run and retry behavior becomes destructive again. The uncalled P1 archival primitive from `3235e6b1...` remains in source.

To revert all build bytes back toward exact base `872c341302b5ed8941f280c3b7939cabba930b5a`, also revert the P1 prerequisite in the same newest-to-oldest transaction:

```bash
git revert --no-edit \
  d540eaee9764fbc3194c493946cd6624f447c3a5 \
  daf5dc1e9ae33ee0f2a269b0fe82732a7f2fcdfb \
  1e8bf80feefd668c247e0c569e28131ae0a2bce4 \
  bc6c801a5734d30543a908c18233728b691e9e9e \
  3235e6b1d41b3b225bc41c5ac8eed4c662a8666e
```

Any rollback is a new authority-gated transaction. Stop on conflicts, rerun the governed ten-file gate, read back the new revert SHA, and record whether destructive rewind has returned.

## Open remainders

- **Independent Stage 6 recheck:** `RECHECK_HOLD`. Receipt: `C:\Users\HieuKa\AppData\Local\New Hermes\evidence\rewind-recheck-20260811-104048-ICT-c4057c52`. The reviewer reproduced `1,282/0` in `90.5s`, killed `12/12` mutations with byte-identical restoration independently verified by `git hash-object`, passed the canary with all exclusions true, and reproduced RED with zero collection/import errors. All seven findings were judged genuinely fixed. The deciding HOLD item was the under-scoped B-1 acceptance wording. This was defect-finding recheck, not Final Inspection or clearance.
- **B-1:** `CODE FIX CONFIRMED; ACCEPTANCE CLAIM CORRECTED; BUILD HOLD`. Root cause: byte-exact comparison was specified without stating which representation was canonical. Replay/projected content is canonical, structured JSON stays structured, and `api_content` is compared exactly when present. Without a sidecar, every difference collapsed by `sanitize_context(...).strip()` is undetectable—not merely whitespace—including arbitrary-length `<memory-context>...</memory-context>` spans, the recalled-memory `[System note: ...]` form, and fence tags (`agent/memory_manager.py:163-181`, including `:168-181`). The earlier whitespace-only wording came from the COO instruction and was recorded faithfully by the builder; the COO corrected it after empirical review.
- **`api_content` prevalence:** `NOT ESTABLISHED`. Code proves it is nullable and conditional: no injection or sanitization-changing content can yield zero sidecars; only the current user row is composed/stamped for injected context, run-agent stamping is conditional, gateway/branch sites forward only an existing sidecar, and content rewrites drop stale sidecars. No repository telemetry establishes a percentage, and coverage of post-write drift on originally sidecar-free rows is not established.
- **RED weight:** 16 failed cases total and zero collection/import/syntax errors, but only 9 are genuine behavioural REDs: B-1, B-2, B-3 ×3, B-4, M-1 ×2, and MED-5. The remaining 7 are parametrizations of one closure-key assertion. B-5 has no behavioural RED because it is a record correction.
- **B-2 / MED-4:** `FIX CONFIRMED BY RECHECK`. Destructive export refuses deletion when ordinary export did not cover rewind rows, including probe uncertainty.
- **B-3:** `FIX CONFIRMED BY RECHECK`. `/undo` count selection, compression guard, archival, counters, and returned head are one durable transaction; its count coordinate and return shape remain deliberately distinct from `/retry`.
- **M-1:** `DEFECT FIX CONFIRMED EMPIRICALLY`. The accepted-residual classification is withdrawn. `has_archived_messages` had no retry while `replace_messages` retried through `_execute_write`, so lock contention systematically selected destruction. The reviewer verified the stale-`False` case: a poisoned preliminary probe cannot select destruction because replacement rechecks under its write transaction.
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

- trigram path at `hermes_state.py:7904`;
- main FTS path at `hermes_state.py:8087`;
- CJK path at `hermes_state.py:8186`.

Therefore rewind rows (`active=0, compacted=0`) are excluded while compaction archives (`compacted=1`) remain discoverable. A missing predicate on any one of the three paths would have leaked rewind rows; none is missing. Direct source inspection also found the same predicate on the later trigram fallback (`:8275`), LIKE fallback (`:8369`), and unindexed-gap path (`:8611`).

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

- **Retry-path safety:** `_execute_write` calls rollback at `hermes_state.py:2147` before any lock/busy retry. A retried callback therefore re-runs against an unchanged database when rollback succeeds. If rollback fails and leaves the transaction open, the later transaction-nesting error propagates at `:2170`; it fails closed. Compare-then-mutate is idempotent in effect across successful retries.
- **I1 and M-1 TOCTOU:** the original compare/mutate lock remains. Stage 6 additionally closed M-1 with a fail-closed retrying probe and an authoritative archive recheck inside the replacement write transaction.
- **Normalizer correction:** the earlier audit missed B-1 because v3.2 never selected the canonical representation. The repair projection and its full sanitize-equivalence limit in `repair-epoch-1/REPAIR-DECISION.md` supersede both the older broad mismatch-resistance claim and the later under-scoped whitespace-only wording.
- **Ordinal boundaries:** an empty active-user set makes every ordinal out of range and fails closed. Ordinal `0` is the valid whole-transcript archive when an active user exists and is gated upstream by confirmation error `4028`. Negative, non-integer, and boolean values are refused. The guard at `:7500` uses `type(x) is not int`, not `isinstance`, which correctly prevents Python booleans from passing as integers.
- **Mutation spot-check:** the COO independently replaced `type(x) is not int` with `not isinstance(x, int)`. Exactly `test_rejects_invalid_ordinal_without_any_mutation[True]` failed; no other parametrized case failed. Source was then restored byte-identical to SHA-256 `8432a42218866a507a7cefe0be61f534566d370bc7ef20f3b23def964e121d83`. This directly supports the P1.C2 manifest's ordinal-boolean claim and shows the test discriminates the boolean boundary precisely.

Audit limit:

- The COO materially contributed to the build's technical direction and is ineligible to perform independent BUILD inspection.
- Stage 6 answered the seventh area: the cleared plan was wrong because v3.2 demanded byte-exact comparison without selecting the canonical representation.
- The resulting repair was independently defect-found and judged functionally correct, but the recheck verdict was `RECHECK_HOLD`. It was not Final Inspection, so build clearance remains absent.

## Mechanical closure gate

The lifecycle remains `HOLD`. The Stage 6 defect-finding recheck occurred and its acceptance-claim HOLD is recorded accurately; Final Inspection and clearance have not occurred. Submission, integration, push/remote readback/CI, deployment, activation, and rollback proof also remain absent or unauthorized. No finding or recheck verdict was rewritten to manufacture PASS.

See `BUILD-CLOSURE-SUMMARY.json` and `repair-epoch-1/REPAIR-DECISION.md` for the bound repair record. This note documents repair completion only; it does not claim build clearance or pipeline closure.
