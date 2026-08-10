# P1.C2 BUILD / GREEN / ANTI-VACUITY RECEIPT

- Build: `rewind-archive-dropped-20260810`
- Recorded: `2026-08-10T22:33:40+07:00`
- Authorized scope: `P1.C2 only — I1, I4, I6 plus P1.C2 tests`
- Candidate/base HEAD during work: `872c341302b5ed8941f280c3b7939cabba930b5a`
- Map-v3.2 root: `f9797c045646be5349f0a9b70f88770414f14cef606fc8efa200b24736a118a7`
- Plan review: `PASS`, epoch-4 fresh `claude-opus-5` `xhigh`; COO independently verified before relay.

## Changed tracked paths

- `hermes_state.py` — RewindHistoryConflict plus I1/I4/I6.
- `tests/test_hermes_state.py` — P1.C2 transaction, normalizer, range, compression, export and byte-identity tests.
- `tests/gateway/test_dedupe_user_turns.py` — I6 resurrection regression.

P1.C1 test hashes remained byte-identical:
- TUI `985ff64021be21bf1d72ceece9e134117086cb6a38eb07bbaabcf278ec8ed81c`
- CLI `3f76263ed3d0d9b6f6186897da9588ed9303f2a62162edf164fb2200b9a59434`

## P1.C2 gate — actual output

```text
HERMES_PYTHON='/c/c/Users/HieuKa/Desktop/hermes-rewind-archive-20260810/.venv/Scripts/python.exe' scripts/run_tests.sh --file-retries 0 tests/test_hermes_state.py tests/gateway/test_dedupe_user_turns.py -q
Discovered 2 test files (~449 tests) under ['tests\test_hermes_state.py', 'tests\gateway\test_dedupe_user_turns.py']; running with -j 64
[  1.8% |     8/~449 | ✓8 | ✗0] ✓ tests\gateway\test_dedupe_user_turns.py (8✓, 1.9s)
[100.0% |   449/~449 | ✓481 | ✗  0] ✓ tests\test_hermes_state.py (473✓, 78.3s)
=== Summary: 2 files, 481 tests passed, 0 failed (100% complete) in 78.3s (64 workers) ===
```

## Anti-vacuity

Fourteen load-bearing mutations were applied one at a time. Each made its named test fail. The source was restored to SHA-256 `6f8ead07a5e53d040ca1112c9040584f97d6b465e6d178f3a3ce5e5b381d3d94`, then the full gate returned `481 passed / 0 failed`.

Mutation manifest: `mutation-p1c2/manifest.json` SHA-256 `ffbbb3ac207ad3025fde70b3312f5f1617c0680cb5d0c39bf5962e3aad1fde5c`.

Covered guards:
1. 23-column normalizer partition.
2. exact semantic compare.
3. non-integer/boolean ordinal rejection.
4. negative ordinal rejection.
5. compression-closed guard.
6. BEGIN IMMEDIATE compare/update lock.
7. target-row inclusion in archived suffix.
8. pre-existing inactive preservation.
9. rewind state active=0,compacted=0.
10. active counter repair.
11. rewind_count increment.
12. committed new-head result.
13. I4 rewind-only recovery filter.
14. I6 active-or-compacted dedupe filter.

Mutation harness incidents:
- Initial preflight stopped before any mutation because one string anchor had wrong indentation; classified harness setup, corrected without spending a semantic mutation attempt.
- The 14-mutation automation hit the tool-call ceiling after M13's named failure and before its restore/receipt write. M13 was immediately restored to exact source hash; M14 ran separately. The complete full GREEN gate above was run only after exact restoration.

## P1.C1 boundary remains RED

```text
TUI: 473 passed / 2 failed — only the two named P1.C1 assertions.
CLI: 2 passed / 1 failed — only the named CLI companion.
```

The failures remained absence of gateway/CLI routing behavior, proving P1.C3 was not implemented.

## State

`P1.C2 CANDIDATE_VERIFIED — P1.C3 NOT STARTED`.
No schema migration, gateway route, CLI route, Desktop route, deployment, activation, trade or order was touched.
