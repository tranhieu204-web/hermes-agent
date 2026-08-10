# STAGE 3 RED RECEIPT

- Build: `rewind-archive-dropped-20260810`
- Recorded: `2026-08-10T21:24:33+07:00`
- Authorized scope: `P1.C1 / Stage 3 RED only`
- Exact production base and candidate HEAD: `872c341302b5ed8941f280c3b7939cabba930b5a`
- Frozen map root: `171b74978d53f9edbd03c9dff1bc67d2b871c4a7c3e6827bd1224487435d80d3`
- Plan review: `PASS`, fresh epoch-3 `claude-opus-5` `xhigh`, user independently verified; reviewer token spend not supplied.
- Semantic RED executions: one TUI file execution and its one separately bound CLI companion execution; automatic file retries disabled.

## Test candidate hashes

- `tests/test_tui_gateway_server.py`: `985ff64021be21bf1d72ceece9e134117086cb6a38eb07bbaabcf278ec8ed81c`
- `tests/cli/test_cli_retry.py`: `3f76263ed3d0d9b6f6186897da9588ed9303f2a62162edf164fb2200b9a59434`

Only those two tracked test files differ from exact base. No production source differs.

## TUI RED command and actual runner output

```text
HERMES_PYTHON='/c/c/Users/HieuKa/Desktop/hermes-rewind-archive-20260810/.venv/Scripts/python.exe' scripts/run_tests.sh --file-retries 0 tests/test_tui_gateway_server.py -q
Discovered 1 test files (~463 tests) under ['tests\test_tui_gateway_server.py']; running with -j 64
[100.0% |   463/~463 | ✓473 | ✗  2] ✗ tests\test_tui_gateway_server.py (473✓ 2✗, 88.3s)
2 failed, 473 passed in 87.36s (0:01:27)
=== Summary: 1 files, 473 tests passed, 2 failed (100% complete) in 88.3s (64 workers) ===
FAILED tests/test_tui_gateway_server.py::test_prompt_submit_rewind_archives_newly_dropped_suffix
FAILED tests/test_tui_gateway_server.py::test_retry_command_converts_array_index_to_user_ordinal_and_archives_suffix
```

Executed arithmetic: `475 total = 473 passed + 2 intended failed`.

Prompt-submit failure:

```text
AssertionError: base hard-deleted the newly dropped prompt.submit suffix instead of preserving it as active=0, compacted=0
assert [] == ['second', 'second reply']
```

TUI `/retry` failure:

```text
AssertionError: base /retry changed only memory; its dropped suffix has no durable rewind archive
assert [] == ['retry me', 'old answer']
```

The S04 fixture establishes `user_indices == [0, 4]`, `last_user_array_index == 4`, `last_user_ordinal == 1`, and `4 >= len(user_indices)`. A wrong implementation that passes array index 4 as an ordinal must refuse. The test requires the successful send result before checking archive state, so wrong-coordinate refusal cannot satisfy it.

## CLI companion RED command and actual runner output

```text
HERMES_PYTHON='/c/c/Users/HieuKa/Desktop/hermes-rewind-archive-20260810/.venv/Scripts/python.exe' scripts/run_tests.sh --file-retries 0 tests/cli/test_cli_retry.py -q
Discovered 1 test files (~3 tests) under ['tests\cli\test_cli_retry.py']; running with -j 64
[100.0% |     3/~3 | ✓2 | ✗1] ✗ tests\cli\test_cli_retry.py (2✓ 1✗, 1.7s)
1 failed, 2 passed in 1.02s
=== Summary: 1 files, 2 tests passed, 1 failed (100% complete) in 1.7s (64 workers) ===
FAILED tests/cli/test_cli_retry.py::test_cli_retry_converts_array_index_to_user_ordinal_and_archives_suffix_before_requeue
```

Executed arithmetic: `3 total = 2 passed + 1 intended failed`.

CLI failure:

```text
AssertionError: base CLI retry changed only memory; its dropped suffix has no durable rewind archive
assert [] == ['retry me', 'old answer']
```

## Classification

`VALID_RED`

- Discovery was non-vacuous and exact.
- Imports and setup completed.
- Both runner invocations reached behavioral assertions.
- All 473 pre-existing TUI tests and both pre-existing CLI tests passed.
- The three failures are exactly the three named new tests and exactly the missing durable archive behavior.
- S04/CLI fixtures preserve the total coordinate-discrimination property.
- Model-catalog certificate warnings in the TUI log were non-fatal background probes; they did not cause or alter either assertion failure.

## Stop boundary

Stopped after P1.C1 RED. P1.C2 and all production implementation remain unauthorized and untouched. F-M1 is carried forward unresolved and must be discharged before P1.C2.
