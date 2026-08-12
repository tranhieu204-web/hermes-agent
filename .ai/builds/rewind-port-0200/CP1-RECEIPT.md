# CP-1 Receipt — rewind-port-0200

Verdict: HOLD-INCOMPLETE. The implemented six-file candidate is green, but the binding map's complete producer-oracle and mutation obligations were not fully closed in this writer epoch. CP-2 was not started.

## Authority / workspace

- Map: `C:\hrp\.ai\builds\rewind-port-0200\execution-map-v2.md`
- Expected and measured SHA-256: `e73690005a1fc63e0c4c332ccd2cb4aa6312914cbbaabf27202177bcbc9a4c13`
- Branch: `sakaan/rewind-port-0200-20260812`
- HEAD/base: `ee472a7fdbbc55924f91ab122dbaa29bd07668b0`
- Candidate interpreter only: `C:\hrp\venv\Scripts\python.exe`
- No CP-2 source/test file was touched.

## RED first

Exact command:

`C:\hrp\venv\Scripts\python.exe -m pytest tests/test_tui_gateway_server.py -k "rewind_identity or rewind_id or drifted_message_id or first_turn_restore or rewind_preserves_soft_archived or rewind_display_kind or rewind_dual_target or rewind_confirm_matrix or rewind_producer or rewind_compute_host" -q`

Captured untouched-production-base output after the complete initial CP-1 slice was written (production files still at base; tests failed by explicit behavioral/interface assertions, not collection):

```text
FFFFFFFFF                                                                [100%]
================================== FAILURES ===================================
______________ test_rewind_identity_exact_occurrence_and_prefix _______________

    def test_rewind_identity_exact_occurrence_and_prefix():
>       ri = _cp1_identity()
             ^^^^^^^^^^^^^^^

tests\test_tui_gateway_server.py:3716: 
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _

    def _cp1_identity():
        import importlib.util
>       assert importlib.util.find_spec("tui_gateway.rewind_identity") is not None, "rewind identity interface is absent"
E       AssertionError: rewind identity interface is absent
E       assert None is not None
E        +  where None = <function find_spec at 0x0000024FD73B6C00>('tui_gateway.rewind_identity')
E        +    where <function find_spec at 0x0000024FD73B6C00> = <module 'importlib.util' (frozen)>.find_spec
E        +      where <module 'importlib.util' (frozen)> = <module 'importlib' from 'C:\\Users\\HieuKa\\AppData\\Roaming\\uv\\python\\cpython-3.11-windows-x86_64-none\\Lib\\importlib\\__init__.py'>.util

tests\test_tui_gateway_server.py:3701: AssertionError
___________ test_rewind_display_kind_does_not_occupy_ordinal_space ____________

    def test_rewind_display_kind_does_not_occupy_ordinal_space():
>       ri = _cp1_identity()
             ^^^^^^^^^^^^^^^

tests\test_tui_gateway_server.py:3731: 
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _

    def _cp1_identity():
        import importlib.util
>       assert importlib.util.find_spec("tui_gateway.rewind_identity") is not None, "rewind identity interface is absent"
E       AssertionError: rewind identity interface is absent
E       assert None is not None
E        +  where None = <function find_spec at 0x0000024FD73B6C00>('tui_gateway.rewind_identity')
E        +    where <function find_spec at 0x0000024FD73B6C00> = <module 'importlib.util' (frozen)>.find_spec
E        +      where <module 'importlib.util' (frozen)> = <module 'importlib' from 'C:\\Users\\HieuKa\\AppData\\Roaming\\uv\\python\\cpython-3.11-windows-x86_64-none\\Lib\\importlib\\__init__.py'>.util

tests\test_tui_gateway_server.py:3701: AssertionError
_____________ test_rewind_identity_accepts_text_and_content_rows ______________

    def test_rewind_identity_accepts_text_and_content_rows():
>       ri = _cp1_identity()
             ^^^^^^^^^^^^^^^

tests\test_tui_gateway_server.py:3751: 
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _

    def _cp1_identity():
        import importlib.util
>       assert importlib.util.find_spec("tui_gateway.rewind_identity") is not None, "rewind identity interface is absent"
E       AssertionError: rewind identity interface is absent
E       assert None is not None
E        +  where None = <function find_spec at 0x0000024FD73B6C00>('tui_gateway.rewind_identity')
E        +    where <function find_spec at 0x0000024FD73B6C00> = <module 'importlib.util' (frozen)>.find_spec
E        +      where <module 'importlib.util' (frozen)> = <module 'importlib' from 'C:\\Users\\HieuKa\\AppData\\Roaming\\uv\\python\\cpython-3.11-windows-x86_64-none\\Lib\\importlib\\__init__.py'>.util

tests\test_tui_gateway_server.py:3701: AssertionError
________________ test_rewind_producer_create_and_live_payload _________________

monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x0000024FE0B42DD0>

    def test_rewind_producer_create_and_live_payload(monkeypatch):
>       assert hasattr(server, "_history_to_client_messages"), "annotating gateway producer is absent"
E       AssertionError: annotating gateway producer is absent
E       assert False
E        +  where False = hasattr(server, '_history_to_client_messages')

tests\test_tui_gateway_server.py:3759: AssertionError
_____ test_rewind_producer_session_history_is_conservative_for_db_display _____

monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x0000024FE0BFFF10>

    def test_rewind_producer_session_history_is_conservative_for_db_display(monkeypatch):
>       assert hasattr(server, "_history_to_client_messages"), "annotating gateway producer is absent"
E       AssertionError: annotating gateway producer is absent
E       assert False
E        +  where False = hasattr(server, '_history_to_client_messages')

tests\test_tui_gateway_server.py:3774: AssertionError
_________________ test_rewind_confirm_matrix_and_dual_target __________________

monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x0000024FE0844950>

    def test_rewind_confirm_matrix_and_dual_target(monkeypatch):
>       ri = _cp1_identity()
             ^^^^^^^^^^^^^^^

tests\test_tui_gateway_server.py:3804: 
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _

    def _cp1_identity():
        import importlib.util
>       assert importlib.util.find_spec("tui_gateway.rewind_identity") is not None, "rewind identity interface is absent"
E       AssertionError: rewind identity interface is absent
E       assert None is not None
E        +  where None = <function find_spec at 0x0000024FD73B6C00>('tui_gateway.rewind_identity')
E        +    where <function find_spec at 0x0000024FD73B6C00> = <module 'importlib.util' (frozen)>.find_spec
E        +      where <module 'importlib.util' (frozen)> = <module 'importlib' from 'C:\\Users\\HieuKa\\AppData\\Roaming\\uv\\python\\cpython-3.11-windows-x86_64-none\\Lib\\importlib\\__init__.py'>.util

tests\test_tui_gateway_server.py:3701: AssertionError
_____________ test_rewind_id_db_failure_prevents_memory_and_turn ______________

monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x0000024FDA985850>

    def test_rewind_id_db_failure_prevents_memory_and_turn(monkeypatch):
>       assert hasattr(server, "_history_to_client_messages"), "ID-addressed submit interface is absent"
E       AssertionError: ID-addressed submit interface is absent
E       assert False
E        +  where False = hasattr(server, '_history_to_client_messages')

tests\test_tui_gateway_server.py:3832: AssertionError
__________________ test_rewind_preserves_soft_archived_rows ___________________

tmp_path = WindowsPath('C:/Users/HieuKa/AppData/Local/Temp/pytest-of-HieuKa/pytest-4519/test_rewind_preserves_soft_arc0')
monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x0000024FE0AB4350>

    def test_rewind_preserves_soft_archived_rows(tmp_path, monkeypatch):
>       assert hasattr(server, "_history_to_client_messages"), "ID-addressed submit interface is absent"
E       AssertionError: ID-addressed submit interface is absent
E       assert False
E        +  where False = hasattr(server, '_history_to_client_messages')

tests\test_tui_gateway_server.py:3848: AssertionError
__________ test_rewind_compute_host_parent_preserves_annotated_rows ___________

monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x0000024FE0BB1A10>

    def test_rewind_compute_host_parent_preserves_annotated_rows(monkeypatch):
>       assert hasattr(server, "_history_to_client_messages"), "annotated compute-host interface is absent"
E       AssertionError: annotated compute-host interface is absent
E       assert False
E        +  where False = hasattr(server, '_history_to_client_messages')

tests\test_tui_gateway_server.py:3872: AssertionError
=========================== short test summary info ===========================
FAILED tests/test_tui_gateway_server.py::test_rewind_identity_exact_occurrence_and_prefix
FAILED tests/test_tui_gateway_server.py::test_rewind_display_kind_does_not_occupy_ordinal_space
FAILED tests/test_tui_gateway_server.py::test_rewind_identity_accepts_text_and_content_rows
FAILED tests/test_tui_gateway_server.py::test_rewind_producer_create_and_live_payload
FAILED tests/test_tui_gateway_server.py::test_rewind_producer_session_history_is_conservative_for_db_display
FAILED tests/test_tui_gateway_server.py::test_rewind_confirm_matrix_and_dual_target
FAILED tests/test_tui_gateway_server.py::test_rewind_id_db_failure_prevents_memory_and_turn
FAILED tests/test_tui_gateway_server.py::test_rewind_preserves_soft_archived_rows
FAILED tests/test_tui_gateway_server.py::test_rewind_compute_host_parent_preserves_annotated_rows
9 failed, 532 deselected in 2.43s
```

A1/A3 failed on missing computed identity/annotation interfaces. A2/A4 failed on the absent ID-addressed submit/consent interface rather than expecting a new base error code. A5 failed on the absent ID-addressed operation.

## GREEN

Focused CP-1 command (same expression as RED): `9 passed, 532 deselected in 1.31s`.

Required whole gateway-file command:

`C:\hrp\venv\Scripts\python.exe -m pytest tests/test_tui_gateway_server.py -q`

Final captured output:

```text
........................................................................ [ 13%]
........................................................................ [ 26%]
........................................................................ [ 39%]
........................................................................ [ 53%]
........................................................................ [ 66%]
........................................................................ [ 79%]
........................................................................ [ 92%]
......................................                                   [100%]
542 passed in 39.21s
```

## Implemented contracts

- Dependency-free computed `r2:<ordinal>:<prefix-hash>` identity; no schema/storage/config migration.
- Exact resolution; malformed, negative, stale, shifted, and prefix-mismatched IDs refuse.
- `model_user_indices` uses exactly `isinstance(m, dict) and m.get("role") == "user" and not m.get("display_kind")`; submit imports and calls it.
- `_display_text` retains dual coercion for `text` and `content` rows.
- Gateway adapter converts display rows then annotates against an explicit model projection.
- `session.history` uses conservative null IDs (RF3), never ancestor-inclusive display ordinals.
- ID and ordinal dual target refused; targetless confirmation refused; confirmation flags do not cross-arm opening-turn deletion.
- Busy rewinds are refused before queueing.
- Persistence remains write-before-memory with `active_only=True, archive_dropped=True`.
- Host compression parent preserves host-projected annotated rows instead of double-converting.

## Mutation/adversarial table

| Guard | Break | Observed failure | Restored | Observed pass |
|---|---|---|---|---|
| Exact prefix hash | Replaced hash comparison with `if False` | `r2:1:bad` resolved to ordinal 1; focused test exit 1 | Byte-for-byte backup restore | 1 passed |
| `display_kind` ordinal exclusion | Removed `not m.get("display_kind")` | indices became `[0,1,3,4,6]`; exit 1 | Byte-for-byte | 1 passed |
| Dual-target refusal | Disabled dual-target branch | request ran instead of 4004; exit 1 | Byte-for-byte | 1 passed |
| Dedicated ID whole-delete consent | Cross-armed ID path with `confirm_empty_truncate` | matrix case ran instead of 4028; exit 1 | Byte-for-byte | 1 passed |
| Archive dropped rows | Changed `archive_dropped=True` to false | dropped live tail was not archived; exit 1 | Byte-for-byte | 1 passed |
| RF3 conservative history | Annotated DB display against itself | ancestor/display users received IDs; exit 1 | Byte-for-byte | 1 passed |
| Host annotation preservation | Reintroduced parent `_history_to_messages` conversion | annotated host transcript collapsed to empty; exit 1 | Byte-for-byte | 1 passed |
| Busy queue guard | Disabled busy destructive-state refusal | `_handle_busy_submit` spy fired; exit 1 | Byte-for-byte | 1 passed |

Raw evidence: `C:\hrp\.ai\builds\rewind-port-0200\CP1-MUTATIONS.txt`.

Still missing mutation evidence: the compute-host egress call in `tui_gateway/compute_host.py` itself was not independently broken and caught by a host-frame test; only parent preservation was mutated. This is one HOLD-INCOMPLETE reason.

## Production call paths / real request probe

Non-test caller chain:

- `session.create` registered from `tui_gateway/methods_session.py:14` and installed by `tui_gateway/server.py:14429-14436` -> `_history_to_client_messages` at `methods_session.py:133` -> `annotate_rewind_ids` -> `_display_text`, `model_user_indices`, `rewind_prefix_hash`, and `rewind_message_id`.
- `prompt.submit` registered/installed through the same method registry -> `resolve_rewind_ordinal` and `model_user_indices` in `tui_gateway/methods_prompt.py:190-229` before persistence.
- Live payload production calls `_history_to_client_messages` at `tui_gateway/server.py:8208`.
- Compute-host control egress calls `_history_to_client_messages` at `tui_gateway/compute_host.py:742`; parent host compression preserves it at `tui_gateway/methods_session.py:2537`.

A real in-process JSON-RPC `session.create` followed by `prompt.submit` produced:

```text
prompt.submit: REFUSED empty truncation of session 660c4ed4 (2 messages would be wiped; ordinal=0).
{'create_has_rewind_id': True, 'submit_code': 4028, 'helper_calls': {'annotate': 1, 'indices': 3, 'resolve': 1}}
```

This proves a production request reached annotation, index derivation, and exact resolution. It also proves opening-turn ID + generic confirmation is refused with 4028.

## Inventory / exclusions

Re-scan found production `_history_to_messages` calls only in the conversion adapter and textual `/history`/`/context` command formatters (`server.py:12737,12772,12781`). Those are non-Restore text surfaces and remain unannotated. Restore-bearing producers found are routed through `_history_to_client_messages` at create, child watch, deferred/eager resume, live payload, local compression, branch, and compute-host egress. `session.history` is conservative-null. `session.save` remains raw export.

Read-only Desktop inventory found `omit_messages: true` call sites but no `rewind_id` or `truncate_before_message_id` symbol in `apps/desktop/src`; Desktop was not edited.

## Diff / budget

`git diff --name-only` (tracked files):

```text
tests/test_tui_gateway_server.py
tui_gateway/compute_host.py
tui_gateway/methods_prompt.py
tui_gateway/methods_session.py
tui_gateway/server.py
```

Untracked owned source: `tui_gateway/rewind_identity.py`. Together these are exactly the six CP-1 owned files. `.ai/` contains evidence only.

`git diff --check`: exit 0, no output.

Numstat plus new file:

- tracked additions/deletions: 324 / 37
- new `rewind_identity.py`: 171 lines
- net line count: 458 (`324 + 171 - 37`), below soft 950 and hard 1150 thresholds.

## OPEN-gap accounting

Closed with evidence:

1. Identity/consumer address-space parity: exact predicate and submit call, focused tests and display-kind mutation.
2. Host fields relevant to transcript egress: host `messages` annotation and parent preservation identified; parent mutation test.
3. `omit_messages`: live and resume paths return `[]`; Desktop read-only inventory confirms active callers.
4. Current truncation safety: decision matrix, DB failure, archive, and busy queue tests; whole legacy gateway file green.
5. Structured content divergence: shared coercion and both `text`/`content` test.
6. RF3: `session.history` explicitly null and mutation-proven.
7. A5: real SessionDB proves pre-existing inactive rows and newly dropped live rows remain inactive; no schema change.
8. Producer inventory: call-site scan recorded above; textual/export/internal exclusions retained.

Still open / HOLD-INCOMPLETE:

1. The six historical spec patches were extracted to `spec-diffs-cp1.txt` and inspected selectively, but a defensible line-by-line completion record was not produced.
2. Required dedicated ID assertions were not added for every producer branch named by the map (cold deferred resume, eager resume, live reuse, child watch, local compression, branch, and host control ack). Existing whole-file behavior is green and implementation sites are wired, but wiring plus unrelated existing tests is not the binding producer-oracle requirement.
3. Compute-host egress annotation itself lacks a mutation-seen-fail host-frame spy; the completed host mutation covers only parent preservation.
4. The Desktop field configurations consuming cold gateway versus REST transcript identity remain unproven; CP-2 is intentionally not started.
5. Profile DB equivalence and non-message REST feeder inventory are CP-2 obligations and remain open without touching CP-2 files.

## Final assessment

The six-file candidate is self-contained test-green and within budget, but the missing per-producer ID oracles and direct compute-host egress mutation mean this epoch cannot honestly claim the binding CP-1 evidence gate. No forbidden production file, CP-2 file, commit, ref, tag, live install, schema, or desktop source was touched.
