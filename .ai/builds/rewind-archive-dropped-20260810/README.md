# Rewind Archive Dropped — Build Usage and Closure Note

Build ID: `rewind-archive-dropped-20260810`

Branch: `sakaan/rewind-archive-dropped-20260810`

Implementation commit: `daf5dc1e9ae33ee0f2a269b0fe82732a7f2fcdfb`

Status: `IMPLEMENTATION_COMMITTED — BUILD REVIEW PENDING — NOT CLEARED`

## What changed

This build replaces destructive Restore/Edit/Re-run and retry truncation with recoverable session-level archival while preserving destructive behavior for callers that are intentionally destructive.

- **P1** added transactional active-suffix archival, persistence-before-memory/turn ordering, strict coordinates, Desktop/TUI routing, durable counters, and active-or-compacted dedupe handling.
- **P2** moved gateway `/retry` to recoverable archival, checks durable success before token reset or resend, preserves dirty state after persistence failure, and keeps `/undo`'s message-ID API as an explicit MED-3 divergence.
- **P3** exposes read-only, one-session JSONL recovery through `hermes sessions export --include-rewound`. Recovery reads raw rows, includes active rows plus `active=0, compacted=0` rewind rows, excludes compacted history, and preserves content bytes that conversation projection would sanitize or strip.

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
  tests/acp/test_session.py -q
```

Bound result: `1,195 passed / 0 failed` across eight files. The COO independently reproduced `1,195 passed / 0 failed`.

P3 mutation runner:

```bash
./.venv/Scripts/python.exe \
  .ai/builds/rewind-archive-dropped-20260810/mutation-p3/run_mutations.py
```

Expected: 16 mutations, every mutation produces its named assertion failure, every source is restored byte-for-byte, and the runner exits `0`.

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

The actual build history contains a P1 prerequisite commit plus three package-terminal commits:

```text
P1 prerequisite: 3235e6b1d41b3b225bc41c5ac8eed4c662a8666e
P1 terminal:     bc6c801a5734d30543a908c18233728b691e9e9e
P2 terminal:     1e8bf80feefd668c247e0c569e28131ae0a2bce4
P3 terminal:     daf5dc1e9ae33ee0f2a269b0fe82732a7f2fcdfb
```

Forward-only behavioral rollback of the three package-terminal commits, newest first:

```bash
git revert --no-edit \
  daf5dc1e9ae33ee0f2a269b0fe82732a7f2fcdfb \
  1e8bf80feefd668c247e0c569e28131ae0a2bce4 \
  bc6c801a5734d30543a908c18233728b691e9e9e
```

That removes the P3 recovery surface, P2 recoverable retry integration, and P1 persistence-before-effects routing. Restore/Edit/Re-run and retry behavior becomes destructive again. The uncalled P1 archival primitive from `3235e6b1...` remains in source.

To revert all build bytes back toward exact base `872c341302b5ed8941f280c3b7939cabba930b5a`, also revert the P1 prerequisite in the same newest-to-oldest transaction:

```bash
git revert --no-edit \
  daf5dc1e9ae33ee0f2a269b0fe82732a7f2fcdfb \
  1e8bf80feefd668c247e0c569e28131ae0a2bce4 \
  bc6c801a5734d30543a908c18233728b691e9e9e \
  3235e6b1d41b3b225bc41c5ac8eed4c662a8666e
```

Any rollback is a new authority-gated transaction. Stop on conflicts, rerun the governed eight-file gate, read back the new revert SHA, and record whether destructive rewind has returned.

## Open remainders

- **Independent BUILD review:** `PENDING`. Only execution plan v3.2 was independently reviewed. This committed implementation has not been independently reviewed and is not cleared.
- **M-1:** `OPEN_ACCEPTED_RESIDUAL`. ACP's archive probe remains fail-open for destruction on exception and TOCTOU-exposed. The green control covers only the successful probe.
- **MED-4:** `ACCEPTED_CONSEQUENCE_V1`. `--delete-after-verified` verifies active-only export before whole-session deletion and can omit rewind rows; recovery export is therefore incompatible with that option.
- **MED-5:** `ACCEPTED_CONSEQUENCE_V1 — INSIGHTS_COUNTS_EXECUTED_REWOUND_ACTIVITY`. Insights analytics will count rewound turns. All eight message reads in `agent/insights.py` omit `m.active` and `m.compacted` filters. This is not a regression: `agent/insights.py` is byte-identical between base `872c341302b5ed8941f280c3b7939cabba930b5a` and implementation commit `daf5dc1e9ae33ee0f2a269b0fe82732a7f2fcdfb`, and both versions contain zero `m.active` or `m.compacted` references. The pre-build implementation already counted `active=0, compacted=1` compaction rows. The new consequence comes from the population change: rewound rows now remain at `active=0, compacted=0`, so tool-usage and tool-call totals can shift after a rewind. Counting them is defensible because those calls executed and consumed time and money. No v1 code change is proposed. A future change must explicitly choose either an `m.active` visibility filter or an intentional declaration that insights count executed activity irrespective of current transcript visibility.
- **LOW-1:** `OBSERVED_LOW_DIAGNOSABILITY_V1 — SWALLOWED_ROLLBACK_CAUSE`. In `hermes_state.py:2145-2150`, `_execute_write` attempts rollback after a failed write but discards a rollback exception. If the original error is lock/busy and rollback leaves the transaction open, the retry can fail at the next `BEGIN IMMEDIATE` with a transaction-nesting error; because that message is neither lock nor busy, it propagates at `:2170`. This fails closed, so it is not a correctness or data-loss finding. It is pre-existing, was not introduced by this build, and is diagnosability-only: the operator loses the underlying rollback cause. No v1 fix is proposed. Suggested future remedy: log or otherwise preserve the rollback exception at the boundary instead of silently passing.
- **F-R3:** `PRESERVE_4004_PRE_I1`. A present non-integer prompt ordinal retains the pre-I1 `4004` response decision.
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

Status: `DIRECT_VERIFIED — NOT_INDEPENDENTLY_CLEARED`.

The cumulative COO technical audit covered six areas:

1. read-path visibility and analytics population;
2. `_execute_write` retry-path safety;
3. I1 compare/mutate TOCTOU closure;
4. rewind-history normalizer mismatch resistance;
5. ordinal and whole-transcript boundaries;
6. an independent ordinal-boolean mutation spot-check.

Verified safe by COO code reading:

- **Retry-path safety:** `_execute_write` calls rollback at `hermes_state.py:2147` before any lock/busy retry. A retried callback therefore re-runs against an unchanged database when rollback succeeds. If rollback fails and leaves the transaction open, the later transaction-nesting error propagates at `:2170`; it fails closed. Compare-then-mutate is idempotent in effect across successful retries.
- **I1 TOCTOU:** `BEGIN IMMEDIATE` at `:2141` takes the write lock before the callback. The durable-history comparison at `:7493` and the archive mutation at `:7511-7515` occur inside that transaction, so no writer can interleave between compare and mutate. This is distinct from the M-1 ACP probe/write window, which remains open.
- **Normalizer:** non-mappings are refused at `:7331`; unknown expected-history fields at `:7342`; disagreeing `platform_message_id`/`message_id` aliases at `:7359`; non-serializable values at `:7430`; and non-finite JSON numbers by `allow_nan=False` at `:7428`. `_REWIND_MISSING` at `:7320` distinguishes absence from `None`. The one deliberate normalization is the documented persisted-`INTEGER` versus omitted-default handling for `observed` at `:7373-7387`.
- **Ordinal boundaries:** an empty active-user set makes every ordinal out of range and fails closed. Ordinal `0` is the valid whole-transcript archive when an active user exists and is gated upstream by confirmation error `4028`. Negative, non-integer, and boolean values are refused. The guard at `:7500` uses `type(x) is not int`, not `isinstance`, which correctly prevents Python booleans from passing as integers.
- **Mutation spot-check:** the COO independently replaced `type(x) is not int` with `not isinstance(x, int)`. Exactly `test_rejects_invalid_ordinal_without_any_mutation[True]` failed; no other parametrized case failed. Source was then restored byte-identical to SHA-256 `8432a42218866a507a7cefe0be61f534566d370bc7ef20f3b23def964e121d83`. This directly supports the P1.C2 manifest's ordinal-boolean claim and shows the test discriminates the boolean boundary precisely.

Audit limit:

- The COO materially contributed to the build's technical direction and is ineligible to perform independent BUILD inspection.
- The seventh area—whether the cleared PLAN was itself wrong—is structurally unavailable to the COO and remains open for Stage 6.
- Stage 6 independent BUILD review remains `OPEN` and is unaffected by these direct-verification findings.

## Mechanical closure gate

The pinned pipeline checker was run against `ledger.json` after the implementation commit. It returned `HOLD — PIPELINE_GATE_FAILED` / exit `1`, so this build is not closed. The material blockers include missing independent BUILD review, no builder submission GO, no integration/push/remote readback/CI, no final inspection or clearance, and rollback not executed or read back. Historical RED and mutation entries also do not satisfy the checker's final-green-only closure schema. No finding was bypassed or rewritten to manufacture PASS.

See `BUILD-CLOSURE-SUMMARY.json` for the hash-bound closure summary. This note documents implementation completion only; it does not claim build clearance or pipeline closure.
