# Rewind Archive Dropped — Build Usage and Closure Note

Build ID: `rewind-archive-dropped-20260810`

Branch: `sakaan/rewind-archive-dropped-20260810`

Implementation terminal before review: `daf5dc1e9ae33ee0f2a269b0fe82732a7f2fcdfb`

Stage 6 repair code commit: `d540eaee9764fbc3194c493946cd6624f447c3a5`

Reviewed code subject through: `c266dad38dcf1cbf1bcb67b859bd1ff8d0892463`

Status: `FINAL_INSPECTION PASS — CLEARED FOR SUBMISSION ONLY — LATER LIFECYCLE ACTIONS NOT AUTHORIZED`

## What changed

This build replaces destructive Restore/Edit/Re-run and retry truncation with recoverable session-level archival while preserving destructive behavior for callers that are intentionally destructive.

- **P1** added transactional active-suffix archival, persistence-before-memory/turn ordering, strict coordinates, Desktop/TUI routing, durable counters, and active-or-compacted dedupe handling.
- **P2** moved gateway `/retry` to recoverable archival, checks durable success before token reset or resend, preserves dirty state after persistence failure, and keeps `/undo`'s message-ID API as an explicit MED-3 divergence.
- **P3** exposes read-only, one-session JSONL recovery through `hermes sessions export --include-rewound`. Recovery reads raw rows, includes active rows plus `active=0, compacted=0` rewind rows, excludes compacted history, and preserves content bytes that conversation projection would sanitize or strip.
- **Stage 6 repair epoch 1** fixes B-1 through B-4 and M-1 and corrects B-5/F-R3 and MED-5. The independent defect-finding recheck judged all seven findings genuinely fixed but returned `RECHECK_HOLD` because the recorded B-1 guard limit was materially under-scoped. That acceptance claim is corrected in `repair-epoch-1/REPAIR-DECISION.md`; the recheck was not Final Inspection and did not clear the build.
- **Stage 6 close-out test commit** `c266dad38dcf1cbf1bcb67b859bd1ff8d0892463` adds three executable acceptance cases in `tests/test_hermes_state.py`. Without `api_content`, the rewind guard accepts sanitizer-erased drift for a 66-byte `<memory-context>...` value and the recalled-memory `[System note: ...]` form; with `api_content`, the same memory-context-bearing value raises `RewindHistoryConflict` and leaves durable state unchanged. This commit documents a representational limit and its sidecar compensation; it does not fix a production defect.

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

Current code-subject result at `c266dad38dcf1cbf1bcb67b859bd1ff8d0892463`: the author gate passed `1,285/0` in `90.8s`; fresh Final Inspection 3 independently passed `1,285/0` in `88.9s`, 64 workers, zero file retries. The Final Inspection PASS receipt is `C:\Users\HieuKa\AppData\Local\New Hermes\evidence\rewind-final-inspection-3-20260811-124215-ICT\stdout.raw.json`, SHA-256 `2a679056e0bdd6647747e66d8ebf6803d13fce136f60e30d06d6058b7d1718de`; wrapper SHA-256 `9e6e702c13c3a2b94d8fdd53fcbc7a82db332468d543e3d4e1de8bde9553e58e`.

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

The reviewed **CODE SUBJECT** is the code and tests exactly as committed at `c266dad38dcf1cbf1bcb67b859bd1ff8d0892463` (tree `2095ee450d2582f765c604bb579cc6a93397c1ee`). Commit `b3880ee2253ebd12f3ae9e6fdb3c755845dfed50` is the inspected evidence-only record envelope for that subject. This cleanup is another record-only commit in the same contiguous envelope; no record commit is part of the code subject.

Therefore `candidate.sourceSha`, `candidate.workingTreeParent`, `closure.exactSha`, `clearance.exactSha`, and `finalInspection.exactSha` intentionally bind `c266dad38`, not the later record-envelope HEAD. This is sound only while the complete `c266dad38..HEAD` diff—whether one record commit or a finite contiguous chain—contains exclusively paths under `.ai/builds/rewind-archive-dropped-20260810/` and no code or test path. Any code/test byte change after `c266dad38` creates a new subject and invalidates this binding. The inspected record head remains `b3880ee22`; later cleanup-record HEAD identity is discovered from Git history rather than recursively embedded in its own subject fields.

## Why the B-1 limit is pinned, not normalized

`expected_history` already carries replay-projected content and no longer contains bytes erased by the durable-side projection. Detecting every collision without another raw authority would require either a universal raw-content field or comparison of unlike raw/projected strings. The latter is the original B-1 failure mode: legitimate projection differences such as the trailing newline made `/retry` fail forever. The executable no-sidecar acceptance cases therefore document a forced information limit, while the companion `api_content` case pins the exact-byte compensation. The erased span is absent from model-visible replay too; the residual concern is durable-record/export fidelity, not model behavior.

The earlier sanitizer examples are a **known lower bound, not an exhaustive enumeration**. The complete abstract boundary is equivalence under the whole `_normalize_rewind_message` projection. Known collapse classes include: `sanitize_context(...).strip()` for user/assistant strings; `_decode_content` sentinel decoding; semantic decoding of the five `_REWIND_JSON_FIELDS`, which discards JSON byte formatting and key order; and the deliberately excluded `id`, `session_id`, `active`, `compacted`, and `timestamp` columns. Tool-role string content remains byte-exact. `api_content` remains exact when present; its prevalence and post-write-drift coverage remain `NOT_ESTABLISHED`.

This decision is not permanent by assertion alone. Revisit it if expected history gains a universally carried raw authority with measured coverage, if a separate raw-versus-projected contract can detect drift without rejecting legitimate replay projections, or if evidence shows material post-write fence-bearing drift on originally sidecar-free rows. The executable pin currently covers only memory-context and system-note representatives plus sidecar compensation; it does not prove or parametrize every member of the full projection-equivalence class.

## OPEN ASSURANCE GAP — rewind-guard acceptance-class widening

Classification: `OPEN_ASSURANCE_GAP — NOT A PRODUCT DEFECT — NOT CLOSED`.

Final Inspection applied a strictly weakening, **symmetric** mutation that lowercased both canonicalized sides of the rewind-history comparison. That mutation widened the guard's acceptance class by allowing case-differing durable drift that the committed guard rejects, yet the full governed gate still passed `1,285/0` with zero failures. An earlier asymmetric fold was caught only because it also introduced false rejections; that was not a valid widening-only experiment. The symmetric experiment is the decisive evidence.

This is the same structural blind spot that allowed B-1 to survive the earlier `1,195`-test gate and `62` mutations (`14+19+13+16`). It persists after the repair. All 12 repair mutations are removal-or-break mutations: they establish that existing behavior is load-bearing, but none constrains the guard against accepting **more**. The repair fixtures are example-based and pin the classes already found; they do not provide a property or generative invariant over the acceptance boundary and cannot detect the next Class B.

Candidate remedy, **proposal only**: add a property-based, differential, or generative check that constrains `_normalize_rewind_message`/rewind comparison against unintended widening of its accepted-drift class while preserving explicitly documented projection equivalences. This requires Sakaan's decision and a separately authorized build/test change. It is not authorized by this record cleanup and is not implemented here.

Positive assurance finding: mutation clustering is materially better than the earlier P3 campaign. P3 placed `11/16` mutations in `hermes_cli/main.py`, including 9 on two option-rejection assertions, and exercised only 6 distinct named tests. The repair's 12 mutations span 5 files, 9 distinct named tests, at least 11 distinct code anchors, and no more than 2 mutations per named test; its 7 `hermes_state.py` mutations hit 7 genuinely different sites. This campaign is substantive, not clustered, even though the widening-direction assurance gap remains open.

## Open remainders

- **Final Inspection:** `PASS — CLEARED FOR SUBMISSION ONLY`. Fresh `claude-opus-5`/max inspection of code subject `c266dad38` and inspected record head `b3880ee22` reproduced `1,285/0`, `12/12` mutations with byte-identical restoration, a fresh marker-bearing canary, map chain and structure roots under the map canonicalizer, the three decisive bodies, five production hashes, and all seven dispositions with zero divergence. Wrapper `eligible:true`, `mutation_performed:false`, zero auxiliary models, identical custody roots, clean status at both ends, and no reflog move. Receipt: `C:\Users\HieuKa\AppData\Local\New Hermes\evidence\rewind-final-inspection-3-20260811-124215-ICT\stdout.raw.json`, SHA-256 `2a679056e0bdd6647747e66d8ebf6803d13fce136f60e30d06d6058b7d1718de`; wrapper SHA-256 `9e6e702c13c3a2b94d8fdd53fcbc7a82db332468d543e3d4e1de8bde9553e58e`. Scope is submission only; no merge, push, deploy, activation, trading, order, or rollback authority follows.
- **Independent Stage 6 recheck:** `RECHECK_HOLD`. Receipt: `C:\Users\HieuKa\AppData\Local\New Hermes\evidence\rewind-recheck-20260811-104048-ICT-c4057c52`. The reviewer reproduced `1,282/0` in `90.5s`, killed `12/12` mutations with byte-identical restoration independently verified by `git hash-object`, passed the canary with all exclusions true, and reproduced RED with zero collection/import errors. All seven findings were judged genuinely fixed. The deciding HOLD item was the under-scoped B-1 acceptance wording. This was defect-finding recheck, not Final Inspection or clearance.
- **B-1:** `CODE FIX CONFIRMED; ACCEPTANCE CLAIM CORRECTED; FINAL INSPECTION PASS`. Root cause: byte-exact comparison was specified without stating which representation was canonical. Replay/projected content is canonical, structured JSON stays structured, and `api_content` is compared exactly when present. The earlier sanitizer examples are a known lower bound, not a complete characterization. The actual boundary is equality under the whole `_normalize_rewind_message` projection, including user/assistant sanitizer collapse, `_decode_content` sentinel decoding, semantic normalization of the five JSON fields, and deliberately excluded database columns; tool-role strings remain byte-exact. The earlier whitespace-only wording came from the COO instruction and was recorded faithfully by the builder; the COO corrected it after empirical review.
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

Final Inspection 3 returned `PASS — CLEARED FOR SUBMISSION` for exact code subject `c266dad38` and inspected record head `b3880ee22`. That clearance is submission-only. The mechanical pipeline checker remains fail-closed, submission itself has not been executed, and integration, merge, push, remote readback, CI, artifact, deployment, activation, trading, orders, and rollback proof remain absent or unauthorized. The build is not pipeline-closed and the PASS must not be read as shipping authority.

See `BUILD-CLOSURE-SUMMARY.json` and `repair-epoch-1/REPAIR-DECISION.md` for the bound repair record. This cleanup records the PASS, corrects five LOW record defects, and preserves the open widening-direction assurance gap; it changes no code or test.
