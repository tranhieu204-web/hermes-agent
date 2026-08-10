# P3.C1 RED Receipt — Recovery Export

Timestamp: 2026-08-11T00:53:58+07:00 (ICT)
Exact parent commit: `1e8bf80feefd668c247e0c569e28131ae0a2bce4`
Production changes before RED: none

Governed command:

```text
HERMES_PYTHON=/c/c/Users/HieuKa/Desktop/hermes-rewind-archive-20260810/.venv/Scripts/python.exe scripts/run_tests.sh --file-retries 0 tests/hermes_cli/test_sessions_export_rewound.py -q
```

Actual result:

```text
Discovered 1 test file (~5 test functions)
tests/hermes_cli/test_sessions_export_rewound.py: 2 passed / 8 failed
Summary: 2 passed / 8 failed in 5.5s
Exit: 1
```

The eight failures are all parser/behavior absence: `--include-rewound` is not registered. The two frozen default-path controls pass on the P2 commit, proving the active-only export/replay/search defaults before P3 production changes. No collection, import, fixture, database-open, or zero-discovery failure is credited as RED.

The failing tests require: one resolved session, JSONL only, raw-row byte fidelity, active plus rewind-only rows, compacted-row exclusion, and refusal of broad/destructive/non-byte-exact option combinations.
