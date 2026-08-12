# Repair Response Matrix — `rewind-port-0200`

## Verification frame

- Builder-finalizer repair epoch; planning only.
- Workspace/base independently checked: `C:\hrp` at `ee472a7fdbbc55924f91ab122dbaa29bd07668b0` on `sakaan/rewind-port-0200-20260812`.
- Frozen input independently hashed: `execution-map.md` is `c617b7a0423ac9a7e4009253e74e63a8132770ee2f06c68015460e8ea7b3e1ce`.
- I read both inputs in full and re-inspected the pinned working tree and readable spec objects with `git show`; the dispositions below do not adopt reviewer assertions without re-checking them.

## Finding dispositions

### F1 — AGREE

**Independent verification.** The frozen map owns only `rewind_identity.py`, `server.py`, `methods_prompt.py`, and the gateway test in CP-1 (`execution-map.md:101-105`). On base, live transcript conversions are outside that set in `tui_gateway/methods_session.py`: session.create at `:127-159` (`_history_to_messages` at `:133`), child-watch resume at `:481-510` (`:494`), deferred cold resume at `:534-602` (`:581`), eager resume at `:621-643` and final payload at `:789-807`, session.history at `:2442-2463`, compute-host compression response at `:2536-2550`, local compression response at `:2634-2652`, and session.branch at `:2927-2939`. `tui_gateway/server.py:_history_to_messages` starts at `:7136`; it sees only the display projection. `server.py:_live_session_payload` at `:8166-8211` covers reuse only. The final spec annotator requires both display rows and model history (`git show 597142813:tui_gateway/rewind_identity.py`, `annotate_rewind_ids` at lines 208-283).

**Remedy.** CP-1 now owns `tui_gateway/methods_session.py` and routes every Restore-bearing producer through one shared annotating projection helper that receives display and model histories explicitly. The full producer inventory, including exclusions, is bound in execution-map-v2.

### F2 — AGREE

**Independent verification.** The live handler is `hermes_cli/web_routers/sessions.py:601-652`. `hermes_cli/web_server.py:11314-11328` only includes/re-exports the extracted router symbols; `get_session_messages` is re-exported at `:11323`. Thus the v1 CP-2 write to `web_server.py` would not change endpoint behavior.

**Remedy.** CP-2 production ownership moves to `hermes_cli/web_routers/sessions.py`; `web_server.py` is not a writer. The REST tests move to `tests/hermes_cli/test_web_server.py`, beside the endpoint's pagination tests.

### F3 — AGREE

**Independent verification.** `hermes_cli/web_routers/sessions.py:622-635` always pages: omitted limit becomes 500 (`:627-630`) and defaults to latest ordering (`:628-635`). The old spec helper skipped any limited read (`git show 18319f3f4:hermes_cli/web_server.py:11212-11245`), so copying it either never annotates or mistakes a suffix for a complete transcript. Existing tests prove 501 rows produce a latest-500 response (`tests/hermes_cli/test_web_server.py:1880-1908`) and that offset/latest changes the window (`:1910-1927`).

**Remedy.** REST v1 parity remains in scope. The safe rule is: annotate only a non-empty **tip window** where effective ordering is latest and `offset == 0`; fetch the resolved session's complete model resume history only for identity derivation, sanitize it exactly as gateway resume does, and tail-align the returned page against that model history. Default latest-500 and explicit `order=latest&offset=0` qualify, including >500-row sessions. Any oldest-ordered request, any `offset > 0`, empty page, lookup/sanitization/annotation failure, or inability to prove tip alignment returns rows with no minted IDs. Tests cover >500 and offset>0.

### F4 — AGREE

**Independent verification.** Base submit defines the ordinal address space at `tui_gateway/methods_prompt.py:210-213` as `role == "user" and not m.get("display_kind")`. Existing behavior tests state and exercise this at `tests/test_tui_gateway_server.py:9722-9790`. The final spec's `model_user_indices` counts every user dict and omits that filter (`git show 597142813:tui_gateway/rewind_identity.py:168-178`).

**Remedy.** `model_user_indices` becomes the single hard interface for annotation, resolution, and submit, with the exact predicate `isinstance(m, dict) and m.get("role") == "user" and not m.get("display_kind")`. Submit must call it rather than duplicate a comprehension. Pure tests include interleaved `display_kind` user rows and prove they receive no truncatable ordinal/ID and cannot shift neighboring real turns.

### F5 — AGREE

**Independent verification.** Base rejects bare `confirm_truncate` at `methods_prompt.py:165-174`, rejects bool ordinals at `:175-183`, requires `confirm_truncate` for ordinal cuts at `:184-209`, requires `confirm_empty_truncate` for ordinal-zero wipe at `:221-244`, persists with `active_only=True, archive_dropped=True` at `:256-301`, and mutates memory only afterward at `:302-307`. The final spec ID path preferred ID when both targets were present (`git show 4793eb531:tui_gateway/server.py:9593-9627`), did not require `confirm_truncate` for IDs, and made empty-wipe consent path-specific at `:9645-9675`; its tests cover dedicated ID wipe consent but contain no dual-target test.

**Remedy.** The successor publishes a binding decision table: every destructive target requires `confirm_truncate`; ordinal-zero additionally requires `confirm_empty_truncate`; ID-zero additionally requires `confirm_delete_entire_transcript` and ignores `confirm_empty_truncate`; dual ID+ordinal is refused with 4004 rather than silently selecting ID; confirmation flags cannot cross-arm paths; targetless destructive confirmation is malformed. Existing write-before-memory ordering and archive flags are immutable. This intentionally layers current 0.20 consent hardening over the older spec rather than porting its weaker gate.

### F6 — AGREE

**Independent verification.** `tests/hermes_cli/test_web_server.py:1831-1927` contains the real messages endpoint validation, cap, default latest-500, explicit oldest, and offset/latest coverage. The root `tests/test_web_server.py` was the pre-extraction historical spec target, not the current pagination suite.

**Remedy.** CP-2 owns `tests/hermes_cli/test_web_server.py`; root `tests/test_web_server.py` is not touched.

### F7 — PARTIAL

**Independent verification.** Truncation occurs under the parent handler lock before compute-host dispatch: `methods_prompt.py:157-303` resolves/persists/mutates the retained prefix, then `:309-317` calls `_submit_prompt_to_compute_host` with text only. Therefore forwarding the new target/confirm fields to the child is not required for submit correctness. However, compute-host transcript egress is a real producer: `tui_gateway/compute_host.py:718-758` builds a `control.ack` and converts history at `:741`; `methods_session.py:2536-2549` converts that returned transcript again for Desktop.

**Remedy.** Keep compute-host as UNKNOWN=AFFECTED but sharpen it: CP-1 owns `tui_gateway/compute_host.py` for annotating the host-owned transcript before control acknowledgement, while submit truncation remains parent-side and no target/confirm forwarding is added. Tests/trace evidence must prove parent persistence precedes dispatch and host response IDs resolve against the same host history.

**Evidence-bound counter-criticism.** The review frames the open question primarily as dropped truncation fields. Base ordering proves those fields are consumed before dispatch, so field forwarding is not the likely defect. The remaining real gap is producer annotation across the process boundary, which v1 did not inventory.

### F8 — AGREE

**Independent verification.** Base does not know `truncate_before_message_id`; an ID-only request can be treated as an ordinary submit rather than reaching a future 4028 branch. The v1 A2 RED text at `execution-map.md:138` therefore risks failing on a downstream expected code instead of on the missing feature itself.

**Remedy.** RED tests assert direct base capabilities: no `rewind_id` output; no ID resolver/consumer; ID-only input does not truncate by identity; no dedicated ID-empty consent behavior. After the helper can import, each acceptance test must fail by its own behavioral assertion. New error-code assertions are GREEN contract tests, not the sole base RED oracle.

### F-COO-1 — AGREE

**Independent verification.** Running the candidate-local interpreter succeeded: `C:\hrp\venv\Scripts\python.exe -m pytest --version` reported pytest 9.1.1; gateway collection selected 532 base tests and `-k rewind_identity` returned exit 5/no tests; `tests/hermes_cli/test_web_server.py -k get_session_messages_omitted_limit_defaults_to_500 --collect-only` selected the real test and returned 0. The supplied `scripts/run_tests.sh` failure is consistent with this linked worktree's mixed Windows/WSL gitdir path. Per binding instruction, I did not retry it and do not plan to fix it.

**Remedy.** Every RED/GREEN gate uses only `C:\hrp\venv\Scripts\python.exe -m pytest ...`; no borrowed venv and no `scripts/run_tests.sh` command appears in execution-map-v2.

## Reviewer uncertainty and coverage gaps carried forward

### Least-confident item

The reviewer was least sure whether cold-open Restore must receive REST-minted IDs. **Disposition: resolved by Chairman D2 — REST parity stays in v1.** CP-2 remains mandatory and is redesigned for always-paged 0.20.0 rather than de-scoped.

### OPEN gaps (not clean bills of health)

These remain explicitly OPEN/UNKNOWN=AFFECTED until the named checkpoint evidence closes them:

1. Full body of all six spec patches line-by-line — OPEN; builder must finish a line-by-line semantic extraction before CP-1 production edits, without replay/cherry-pick.
2. Every compute-host frame field list — OPEN; CP-1 traces submit parent ordering and all transcript-returning control acknowledgements; `compute_host.py` is now owned.
3. Desktop client `omit_messages` / Restore request shape — OPEN; desktop source remains forbidden, so read-only trace evidence is required; any required client change causes HOLD/remap.
4. Full `tests/test_tui_gateway_server.py` truncate-suite semantics — OPEN; builder must inventory existing confirmation, busy queue, display_kind, write-failure, compute-host, and archive tests before adding REDs.
5. Any non-manage REST transcript endpoint feeding Restore — OPEN; CP-2 performs a read-only route/caller inventory; any additional production writer causes HOLD/remap.
6. Profile DB selection equivalence (`_get_db`, `_session_db`, `_open_session_db_for_profile`) — OPEN; preserve each current selection path and add profile-local behavior evidence; any storage API/schema change causes HOLD.
7. Exact Desktop branch matrix among live reuse, child watch, deferred cold, eager resume, REST fallback, and host responses — OPEN from F1; every producer is conservatively AFFECTED until read-only client tracing proves exclusion.
8. Whether host/watch transcript paths should expose Restore — OPEN from F1/F7; successor defaults them AFFECTED and requires annotating or an evidence-backed exclusion before GREEN.
