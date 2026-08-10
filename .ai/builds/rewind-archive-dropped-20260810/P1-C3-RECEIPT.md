# P1.C3 Candidate Verification Receipt

Timestamp: 2026-08-10T23:49:58+07:00 (ICT)
Lifecycle: P1.C3 CANDIDATE_VERIFIED — UNCOMMITTED — STOPPED BEFORE P2/P3
Parent commit: `3235e6b1d41b3b225bc41c5ac8eed4c662a8666e`
Branch: `sakaan/rewind-archive-dropped-20260810`

## Implemented scope

- `prompt.submit` commits `SessionDB.rewind_active_history` before history, history_version, running state, launch, or send effects.
- TUI `/retry` and CLI `/retry` convert the last user array index to a user-turn ordinal, prove the target/round-trip coordinate, then commit I1 before memory/send/requeue effects.
- Gateway conflict maps to `4018`; missing persistence, compression-closed refusal, and write failures map to `5008`.
- CLI failures return no retry payload, mutate no memory, and enqueue nothing.
- F-R3 is explicitly preserved: a present non-integer prompt ordinal returns `4004` before I1.
- Existing r2 rewind identity and opening-turn confirmation guards remain exercised by the complete TUI file.
- Five `restoreBodyWipes` locale values now state that later turns are archived and recoverable instead of irreversibly deleted.

## Governed GREEN gate

Command:

```text
HERMES_PYTHON='/c/c/Users/HieuKa/Desktop/hermes-rewind-archive-20260810/.venv/Scripts/python.exe' scripts/run_tests.sh --file-retries 0 tests/test_hermes_state.py tests/gateway/test_dedupe_user_turns.py tests/test_tui_gateway_server.py tests/cli/test_cli_retry.py -q
```

Current-byte independent readback supplied by the COO after the final generic-5008 response patch:

```text
Four-file gate: 980 tests passed, 0 failed in 91.8s
Three decisive RED regressions on the same final bytes: 3 passed in 2.93s
```

Hermes did not rerun either gate during bounded finalization; these are COO current-byte receipts. The three
decisive function-source hashes below independently match commit `3235e6b1d` and the working tree, so the same
test bytes that were RED on base `872c34130` are GREEN on the final P1.C3 candidate.

## Decisive frozen function evidence

Canonical before bytes come from immutable commit `3235e6b1d41b3b225bc41c5ac8eed4c662a8666e`; after bytes come from the current candidate. The full function source and body digests match for all three tests.

| Function | Function source SHA-256 before/after | Body SHA-256 before/after |
|---|---|---|
| `test_prompt_submit_rewind_archives_newly_dropped_suffix` | `11fad7cdeeae0973a70444515592e736a18d7603eafaf39b250a456bb669d6cd` | `c2d862ef838eca450d7e597391f1ef78e4ae72b1bbe2fc79199ab096ecabe44f` |
| `test_retry_command_converts_array_index_to_user_ordinal_and_archives_suffix` | `55660025556b02cd44f5b3ef317595ba023aa5162f28027a4031186ed24bdabd` | `5cd0f368d73bece9806284f42b09209717cc631ca6338a3e0d11320551e25f40` |
| `test_cli_retry_converts_array_index_to_user_ordinal_and_archives_suffix_before_requeue` | `772c2b7bafaee60eb7b9e00abba60b2ee19bd18fe415e12a010eba4dc3686c59` | `8f42d7f09044ea1ddec97512368ea3beaa2725323e31481c7df27dda7a87e65c` |

Evidence: `P1-C3-TEST-BODY-HASHES.json`.

New whole-file pins:

```text
tests/test_tui_gateway_server.py d9d7d6ced3e876c23126b98d6f9684d0086607a2c7e5b739b15638f3cc87c88f
tests/cli/test_cli_retry.py       d5e6c8fe66c5bc89d7c1fc4893ea3998ecd3ff28cac072a2c7fc0294b8754fc4
```

## Obsolete fixture audit

No existing test function was removed. Seven obsolete test fixtures changed: five TUI tests that encoded destructive `replace_messages`/partial-DB behavior and two CLI tests that existed only in memory. Every changed test retained or increased its AST assertion count; none asserted less. The CLI fixtures now seed SessionDB and add durable active/rewound-row assertions.

Evidence:

- `P1-C3-TEST-FIXTURE-AUDIT.json`
- `P1-C3-OBSOLETE-FIXTURE-DIFF.patch`

## Mutation evidence

Nineteen routing mutations M01–M19 each made its named test fail. No mutation result was accepted from zero discovery, collection/import failure, or syntax failure. Production bytes were restored after every mutation.

```text
all_mutations_failed_named_tests=true
source_restored_byte_identical=true
hermes_state.py        8432a42218866a507a7cefe0be61f534566d370bc7ef20f3b23def964e121d83
tui_gateway/server.py  4ccefc6928444339fa64ee02d88dd6f48f0cd0fab003cf89e0ffccf9d02dc168
cli.py                 a399abf9ecbd8d7745a1bcf60694b3ac4861a358fed9b235ff4a9eaef0f899b0
manifest SHA-256       867a1e2190209451645e6c3c4668f9f276a68a0c297e0bad058573ce9e57341b
```

M19 was rerun specifically against final server SHA-256
`4ccefc6928444339fa64ee02d88dd6f48f0cd0fab003cf89e0ffccf9d02dc168`. Mutating the revised generic write-failure
mapping from `5008` to `4018` produced `1 failed, 2 passed in 0.71s`; the named OSError/5008 assertion failed.
The server was then restored to the exact hash above.

## Locale verification

- Exactly one `restoreBodyWipes` value was found in each of `ar.ts`, `en.ts`, `ja.ts`, `zh-hant.ts`, and `zh.ts`.
- No old irreversible-deletion phrase remained in those five values.
- TypeScript isolated syntax transpilation rerun on the final tree: 5 files checked, 0 syntax errors.
- Full Desktop typecheck could not be used as acceptance evidence: the candidate has no node_modules and the borrowed original dependency tree lacks `@assistant-ui/core`/related declarations. The failure is dependency-resolution-wide and not localized to the changed string literals.
- Residue readback: candidate `node_modules` is absent. Both original clones' pre-existing `node_modules`
  directories are present as ordinary directories, not symlinks/junctions; no temporary junction remains.

## Record decisions

- F-R1: `planReviewSemanticAttemptsConsumed` is verified at the map-correct value `2`; no counter change was required.
- F-R3: resolved as `4004` for a present non-integer prompt ordinal before I1, with `test_fr3_prompt_submit_keeps_4004_for_non_integer_before_i1` attached.
- P2 and P3 were not started.
- No Git staging/commit, push, merge, rebase, tag, protected-ref movement, deployment, activation, trading, or order occurred in P1.C3.
