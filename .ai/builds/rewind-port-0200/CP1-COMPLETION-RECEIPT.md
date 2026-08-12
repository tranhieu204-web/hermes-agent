# CP-1 Completion Receipt — rewind-port-0200

Verdict: CP1_GREEN. This completion epoch closed exactly the three dispatched CP-1 evidence obligations. CP-2 was not started and its files remain untouched.

## Authority / workspace

- Workspace: `C:\hrp`
- Branch: `sakaan/rewind-port-0200-20260812`
- Base/HEAD pin: `ee472a7fdbbc55924f91ab122dbaa29bd07668b0`
- Binding map: `C:\hrp\.ai\builds\rewind-port-0200\execution-map-v2.md`
- Binding map SHA-256: `e73690005a1fc63e0c4c332ccd2cb4aa6312914cbbaabf27202177bcbc9a4c13`
- Only interpreter used: `C:\hrp\venv\Scripts\python.exe`
- No commit, push, merge, tag, deploy, schema, Desktop, live-install, CP-2, main-ref, or protected evidence-map mutation.

## 1. Dedicated per-producer registered-path assertions

Shared oracle `_assert_cp1_producer_ids` asserts the complete tri-state contract on each exercised payload:

- every user row has the key;
- provably reachable model user rows carry a non-null ID;
- known-unreachable/display-only user rows carry explicit null;
- non-user rows carry no key;
- every non-null ID resolves through `resolve_rewind_ordinal` against the same model history to the expected ordinal.

Dedicated tests:

1. `test_rewind_producer_session_create_ids_resolve_on_registered_path` — calls `server.handle_request(session.create)` and proves create IDs resolve.
2. `test_rewind_producer_child_watch_resume_ids_resolve_on_registered_path` — calls the registered `session.resume` lazy/watch branch and proves marker null plus reachable IDs.
3. `test_rewind_producer_deferred_cold_resume_ids_resolve_on_registered_path` — calls default deferred cold `session.resume` and proves the returned dual-projection transcript contract.
4. `test_rewind_producer_eager_resume_ids_resolve_on_registered_path` — calls `session.resume` with `eager_build=true`, initializes a real registered live record, and proves returned IDs resolve.
5. `test_rewind_producer_live_reuse_payload_ids_resolve_on_registered_path` — calls registered `session.resume`, forces the live-session reuse branch, and proves the live payload IDs resolve.
6. `test_rewind_producer_session_history_is_conservative_for_db_display` — calls registered `session.history`; now uses the shared tri-state oracle with `all_null=True`, proving every user row is explicit null and every non-user row has no key. It never accepts display-derived IDs.
7. `test_rewind_producer_local_compression_ids_resolve_on_registered_path` — calls registered local `session.compress` and proves the replacement transcript IDs resolve against the post-compression model history.
8. `test_rewind_producer_session_branch_ids_resolve_on_registered_path` — calls registered `session.branch` against a real temporary SessionDB and proves branch response IDs resolve against copied branch history.
9. `test_rewind_producer_compute_host_control_ack_ids_resolve_in_host_frame` — invokes the real compute-host control handler, parses its emitted JSON `control.ack` frame, and proves host-minted IDs resolve against host history.

Focused result:

`9 passed, 541 deselected in 1.95s`

### Production defects exposed

None. Every owned producer branch already annotated correctly. The dedicated tests confirmed behavior rather than requiring a production adjustment. No test was weakened to match a defect.

## 2. Compute-host egress mutation spy

The host-frame test above independently depends on `tui_gateway/compute_host.py` egress annotation, not merely parent preservation.

| State | Mutation / action | Observed outcome |
|---|---|---|
| Broken | Replaced `server._history_to_client_messages(history, history)` with `server._history_to_messages(history)` at compute-host egress | `test_rewind_producer_compute_host_control_ack_ids_resolve_in_host_frame` failed; shared oracle reported user frames had no `rewind_id`; command exit 1 |
| Restored | Restored the exact original annotation call byte-for-byte | Same test passed: `1 passed in 0.94s`; command exit 0 |

The failure stack identified the emitted unannotated frame rows directly, so this is a load-bearing host-egress spy.

## 3. Spec extraction completion

Written: `C:\hrp\.ai\builds\rewind-port-0200\CP1-SPEC-EXTRACTION.md`.

It records, in commit order, the behavior specified by all six objects and its completed 0.20.0 file:line location or deliberate decomposition exclusion:

- `18319f3f4`
- `fc063c47b`
- `c9101a651`
- `4d1f52ffb`
- `4793eb531`
- `597142813`

REST annotation is explicitly recorded as CP-2 scope, not silently omitted. Unrelated historical teardown/operator-policy hunks, persisted IDs/schema work, Desktop edits, and client work are explicitly excluded.

## Test counts and final gate

- Before this completion epoch: `542 passed`.
- New tests: 8 (the existing dedicated `session.history` test was strengthened into the shared per-producer oracle, yielding nine named producer assertions total).
- After: `550 passed`.

Required whole-file command:

`C:\hrp\venv\Scripts\python.exe -m pytest tests/test_tui_gateway_server.py -q`

Observed:

`550 passed in 40.99s`

## Diff / line budget

`git diff --name-only`:

```text
tests/test_tui_gateway_server.py
tui_gateway/compute_host.py
tui_gateway/methods_prompt.py
tui_gateway/methods_session.py
tui_gateway/server.py
```

Untracked owned source remains `tui_gateway/rewind_identity.py`; `.ai/` contains the authorized build evidence/receipts. Together the source changes are exactly the six CP-1 files.

Final numstat for tracked files:

```text
461  13  tests/test_tui_gateway_server.py
2    1   tui_gateway/compute_host.py
52   14  tui_gateway/methods_prompt.py
9    8   tui_gateway/methods_session.py
13   1   tui_gateway/server.py
```

- Tracked net: `500` lines.
- New `tui_gateway/rewind_identity.py`: `171` lines.
- Updated total net line count: `671` (`500 + 171`), below the 950 soft target and 1,150 hard threshold.

`git diff --check`: exit 0, no output.

`git status --short` confirms only the five tracked CP-1 files are modified, with `.ai/` and `tui_gateway/rewind_identity.py` untracked. No CP-2 or forbidden path appears.

## Updated status of all five original open items

1. Six historical spec patches lacked a defensible line-by-line completion record — CLOSED by `CP1-SPEC-EXTRACTION.md`.
2. Dedicated assertions were missing for every map-named producer branch — CLOSED by the nine registered-path assertions listed above; focused 9/9 green and whole file green.
3. Compute-host egress lacked an independently mutation-proven host-frame spy — CLOSED; observed red after removing annotation and green after byte-for-byte restoration.
4. Desktop cold-gateway versus REST field configuration — remains CP-2 scope and stays CLOSED TO THIS EPOCH; CP-2 was not started and Desktop was not edited.
5. Profile DB equivalence and non-message REST feeder inventory — remains CP-2 scope and stays CLOSED TO THIS EPOCH; no CP-2 file was touched.

## Final assessment

Exactly the three dispatched CP-1 obligations are complete. The producer matrix now exercises real registered handlers/frames with resolution-backed tri-state assertions, the compute-host egress spy has demonstrated red/green mutation sensitivity, the six-commit extraction record exists, the owned full test file is green at 550 tests, and all forbidden CP-2 work remains untouched.
