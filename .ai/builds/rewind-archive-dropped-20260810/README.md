# Rewind Archive Dropped — Build Usage and Closure Note

Build ID: `rewind-archive-dropped-20260810`

Branch: `sakaan/rewind-archive-dropped-20260810`

Implementation terminal before review: `daf5dc1e9ae33ee0f2a269b0fe82732a7f2fcdfb`

Stage 6 repair code commit: `d540eaee9764fbc3194c493946cd6624f447c3a5`

Historical Final Inspection 3 code subject: `c266dad38dcf1cbf1bcb67b859bd1ff8d0892463`

M13 candidate parent: `af49192172e1b3635490fcb019bda0e481b71292`; the new exact candidate SHA is the commit containing this record.

Status: `M13 CANDIDATE VERIFIED — PRIOR FINAL INSPECTION PASS SUPERSEDED — NEW FINAL INSPECTION PENDING`

## What changed

This build replaces destructive Restore/Edit/Re-run and retry truncation with recoverable session-level archival while preserving destructive behavior for callers that are intentionally destructive.

- **P1** added transactional active-suffix archival, persistence-before-memory/turn ordering, strict coordinates, Desktop/TUI routing, durable counters, and active-or-compacted dedupe handling.
- **P2** moved gateway `/retry` to recoverable archival, checks durable success before token reset or resend, preserves dirty state after persistence failure, and keeps `/undo`'s message-ID API as an explicit MED-3 divergence.
- **P3** exposes read-only, one-session JSONL recovery through `hermes sessions export --include-rewound`. Recovery reads raw rows, includes active rows plus `active=0, compacted=0` rewind rows, excludes compacted history, and preserves content bytes that conversation projection would sanitize or strip.
- **Stage 6 repair epoch 1** fixes B-1 through B-4 and M-1 and corrects B-5/F-R3 and MED-5. The independent defect-finding recheck judged all seven findings genuinely fixed but returned `RECHECK_HOLD` because the recorded B-1 guard limit was materially under-scoped. That acceptance claim is corrected in `repair-epoch-1/REPAIR-DECISION.md`; the recheck was not Final Inspection and did not clear the build.
- **Stage 6 close-out test commit** `c266dad38dcf1cbf1bcb67b859bd1ff8d0892463` adds three executable acceptance cases in `tests/test_hermes_state.py`. Without `api_content`, the rewind guard accepts sanitizer-erased drift for a 66-byte `<memory-context>...` value and the recalled-memory `[System note: ...]` form; with `api_content`, the same memory-context-bearing value raises `RewindHistoryConflict` and leaves durable state unchanged. This commit documents a representational limit and its sidecar compensation; it does not fix a production defect.
- **M13 assurance closure** adds a deterministic 33-case, 1,089-pair cross-product property whose oracle is independent of `_canonicalize_rewind_history`: user/assistant strings are projected by the replay path's `agent.memory_manager.sanitize_context(...).strip()`, structured values by independent canonical JSON, and `api_content` by exact authority when present. Every unequal reference projection must raise `RewindHistoryConflict`. The exact symmetric `.lower()` widening was proved RED on `upper / Alpha` versus `lower / alpha`, production bytes were restored, and permanent mutation `M13_WIDEN_ACCEPTANCE_CLASS_SYMMETRIC_FOLD` now fences that direction.

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

Current M13 candidate result: `1,286 passed / 0 failed` across the same ten governed files in `94.0s`, with 64 workers and zero file retries. Actual runner output: `assurance-gap-m13/FULL-GOVERNED-GATE.txt`, SHA-256 `a1b5dcd10601fba649bf5de5f17122925f71c3b360b0087d3c086c9750f97961`.

Repair plus M13 mutation runner:

```bash
./.venv/Scripts/python.exe \
  .ai/builds/rewind-archive-dropped-20260810/repair-epoch-1/run_mutations.py
```

Result: 13 mutations; all 13 failed their named tests, including permanent loosening mutation `M13_WIDEN_ACCEPTANCE_CLASS_SYMMETRIC_FOLD`; all five production sources were restored byte-for-byte; the restored focused gate passed `11/11`; runner exit `0`. Manifest SHA-256: `3abcaa32afba95c1d7d4df0c1fcf01bf66a5b574e4b1bb8c7228a4eb47f0c3fa`.

M13's receipt is normalized deterministically to UTF-8/LF with pytest's presentation-only trailing spaces removed; that policy is emitted by the runner in the manifest and on every mutation entry. The historical Stage 6 first-run raw receipts and their separate raw bindings remain historical evidence and are not represented as byte-identical to the M13 regeneration.

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

The historical Final Inspection 3 **CODE SUBJECT** is `c266dad38dcf1cbf1bcb67b859bd1ff8d0892463` (tree `2095ee450d2582f765c604bb579cc6a93397c1ee`), with inspected record head `b3880ee2253ebd12f3ae9e6fdb3c755845dfed50`. Record-only commit `af49192172e1b3635490fcb019bda0e481b71292` followed it.

M13 changes `tests/test_hermes_state.py` and the mutation/evidence record. Therefore the `c266dad38` inspection binding is no longer current: the new code/test subject is the commit containing this record. It must be read mechanically from Git after commit and must receive a new independent Final Inspection before any submission clearance can exist. No production source byte changed in M13; `hermes_state.py` remains SHA-256 `57f8332f35c4b9080365ac0881db06e66ce00c2ec2d4e8115f8ed1ef9e8468cf`.

## Why the B-1 limit is pinned, not normalized

`expected_history` already carries replay-projected content and no longer contains bytes erased by the durable-side projection. Detecting every collision without another raw authority would require either a universal raw-content field or comparison of unlike raw/projected strings. The latter is the original B-1 failure mode: legitimate projection differences such as the trailing newline made `/retry` fail forever. The executable no-sidecar acceptance cases therefore document a forced information limit, while the companion `api_content` case pins the exact-byte compensation. The erased span is absent from model-visible replay too; the residual concern is durable-record/export fidelity, not model behavior.

The earlier sanitizer examples are a **known lower bound, not an exhaustive enumeration**. The complete abstract boundary is equivalence under the whole `_normalize_rewind_message` projection. Known collapse classes include: `sanitize_context(...).strip()` for user/assistant strings; `_decode_content` sentinel decoding; semantic decoding of the five `_REWIND_JSON_FIELDS`, which discards JSON byte formatting and key order; and the deliberately excluded `id`, `session_id`, `active`, `compacted`, and `timestamp` columns. Tool-role string content remains byte-exact. `api_content` remains exact when present; its prevalence and post-write-drift coverage remain `NOT_ESTABLISHED`.

This decision is not permanent by assertion alone. Revisit it if expected history gains a universally carried raw authority with measured coverage, if a separate raw-versus-projected contract can detect drift without rejecting legitimate replay projections, or if evidence shows material post-write fence-bearing drift on originally sidecar-free rows. The executable pin currently covers only memory-context and system-note representatives plus sidecar compensation; it does not prove or parametrize every member of the full projection-equivalence class.

## CLOSED ASSURANCE GAP — rewind-guard acceptance-class widening

Classification: `CLOSED FOR THIS GUARD — NEW FINAL INSPECTION PENDING`.

The gap is closed by executable, non-circular evidence. The new deterministic property constructs a 33-case adversarial corpus—case, edge/interior whitespace, NFC/NFD, zero-width and bidi characters, sanitizer-erased memory/system/fence spans, list/dict structured content, `None`/absent/empty, and `api_content` present/absent variants—and exercises all `1,089` ordered pairs through `rewind_active_history`. Its oracle never calls `_canonicalize_rewind_history` or `_normalize_rewind_message`: it derives user/assistant strings directly from replay-path `sanitize_context(...).strip()`, structured values from independent canonical JSON, and sidecars from exact values. Any unequal reference projection must produce `RewindHistoryConflict`; equal projections remain permitted to be accepted.

The property passed on restored production code, then failed under the exact strictly weakening symmetric fold applied inside `_canonicalize_rewind_history`. The named witness was durable `upper / "Alpha"` versus expected `lower / "alpha"`. After byte-identical restoration it passed again. The frozen property body SHA-256 is `36bf16189b09cd05f0c06513c7b7c01a86a32ad3fbb8e0431d714158965cb478`.

Permanent mutation `M13_WIDEN_ACCEPTANCE_CLASS_SYMMETRIC_FOLD` now keeps this widening direction in the campaign. All `13/13` mutations fail named tests, all five production sources restore byte-identically, and the restored focused gate passes `11/11`. Closure record: `assurance-gap-m13/ASSURANCE-GAP-CLOSURE.json`.

Scope limit: this closes the demonstrated acceptance-class-widening gap for the rewind-history guard only. The removal-only mutation shape may leave equivalent widening gaps on other guards in this build. Those guards were not audited by this work, and no broader build-wide claim is made.

Positive assurance finding remains: mutation distribution is materially better than earlier P3, and M13 adds the previously absent loosening direction rather than replacing the existing removal-or-break coverage.

## Open remainders

- **Final Inspection:** historical `PASS — CLEARED FOR SUBMISSION ONLY` for exact code subject `c266dad38` and record head `b3880ee22`. M13 changes test code, so that clearance is now `SUPERSEDED / NOT CURRENT`; the new commit has no submission clearance until a fresh independent Final Inspection passes it. The historical receipt remains intact at `C:\\Users\\HieuKa\\AppData\\Local\\New Hermes\\evidence\\rewind-final-inspection-3-20260811-124215-ICT\\stdout.raw.json`, SHA-256 `2a679056e0bdd6647747e66d8ebf6803d13fce136f60e30d06d6058b7d1718de`.
- **M13 assurance closure:** `CLOSED_FOR_REWIND_HISTORY_GUARD_ONLY`. Independent 1,089-pair property, exact widening RED witness, restored GREEN, 13/13 permanent mutations, and 1,286/0 governed gate are recorded. Other guards remain `NOT_ASSESSED_FOR_EQUIVALENT_WIDENING_GAPS`.
- **Independent Stage 6 recheck:** `RECHECK_HOLD`. Receipt: `C:\Users\HieuKa\AppData\Local\New Hermes\evidence\rewind-recheck-20260811-104048-ICT-c4057c52`. The reviewer reproduced `1,282/0` in `90.5s`, killed `12/12` mutations with byte-identical restoration independently verified by `git hash-object`, passed the canary with all exclusions true, and reproduced RED with zero collection/import errors. All seven findings were judged genuinely fixed. The deciding HOLD item was the under-scoped B-1 acceptance wording. This was defect-finding recheck, not Final Inspection or clearance.
- **B-1:** `CODE FIX CONFIRMED; ACCEPTANCE CLAIM CORRECTED; M13 WIDENING FENCE ADDED; NEW FINAL INSPECTION PENDING`. Root cause: byte-exact comparison was specified without stating which representation was canonical. Replay/projected content is canonical, structured JSON stays structured, and `api_content` is compared exactly when present. The earlier sanitizer examples are a known lower bound, not a complete characterization. The actual boundary is equality under the whole `_normalize_rewind_message` projection, including user/assistant sanitizer collapse, `_decode_content` sentinel decoding, semantic normalization of the five JSON fields, and deliberately excluded database columns; tool-role strings remain byte-exact. The earlier whitespace-only wording came from the COO instruction and was recorded faithfully by the builder; the COO corrected it after empirical review.
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

See `assurance-gap-m13/ASSURANCE-GAP-CLOSURE.json`, `BUILD-CLOSURE-SUMMARY.json`, and `repair-epoch-1/REPAIR-DECISION.md`. M13 closes the demonstrated widening-direction gap for the rewind-history guard only and explicitly does not claim equivalent coverage for other guards.
