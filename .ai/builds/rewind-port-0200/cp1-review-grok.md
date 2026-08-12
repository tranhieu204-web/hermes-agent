# CP-1 Independent Code Review — rewind-port-0200

- Reviewer role: independent blind CODE review (BLIND_REVIEWER=YES)
- Workspace: `C:\hrp`  Branch: `sakaan/rewind-port-0200-20260812`
- Base/HEAD pin verified: `ee472a7fdbbc55924f91ab122dbaa29bd07668b0`
- Binding map: `execution-map-v2.md` SHA-256 `e73690005a1fc63e0c4c332ccd2cb4aa6312914cbbaabf27202177bcbc9a4c13` (MATCH)
- Pre-staged diff SHA-256: `cda73ba1c26962e6db171ca20a41fe0b4863259d3dd6c746acf8d50e4aae4787` (MATCH)
- Prior plan rescan: `plan-rescan-grok.md` (RF1/RF2/RF3 carried)
- Method: read-only inspection of the six CP-1 candidate files + focused behavioral reading of producers/consumer + independent pytest run. Receipts treated as claims only.
- Scope: CP-1 only. CP-2 absence is not a defect.

---

## Executive summary

CP-1 implements the cleared map's identity helper, gateway/host annotation adapter, destructive submit decision table, write-before-memory archive flags, RF3-conservative `session.history`, and host-annotation preservation. Independent suite run: **550 passed**. No BLOCKING contract, safety, non-regression, producer-gap, hygiene, or test-fraud finding that requires HOLD.

Residual non-blocking items remain (skill-replay expansion only gated on ordinal; producer dual-projection stubs mostly identical display/model; initial RED was capability-shaped). These do not overturn CP1_REVIEW_PASS.

---

## K1 — CONTRACT CONFORMANCE — PASS

| Requirement | Evidence | Verdict |
|---|---|---|
| `model_user_indices` exact predicate | `tui_gateway/rewind_identity.py:60-65`: `isinstance(m, dict) and m.get("role") == "user" and not m.get("display_kind")` | MATCH map invariant #6 |
| Submit CALLS it (no sibling comprehension) | `methods_prompt.py:241-243` imports/calls `model_user_indices(history)`; git diff removes the old inline listcomp | MATCH |
| `_history_to_client_messages` adapter | `server.py:7234-7239`: convert via `_history_to_messages` then `annotate_rewind_ids(...)` | MATCH |
| Dual coercion `text` AND `content` | `rewind_identity.py:86-90` (`_display_text`); pure test `test_rewind_identity_accepts_text_and_content_rows` | MATCH |
| r2 exact resolve | `rewind_identity.py:153-171`; refuse malformed/stale/prefix mismatch | MATCH A1 |
| Tri-state annotation | `annotate_rewind_ids` tip-backward spine (`:96-150`); null fill for unmatched users; non-users get no key | MATCH |

No adjacent “almost the contract” substitution found on the pure helper or submit address space.

---

## K2 — DESTRUCTIVE SAFETY — PASS

Walk of `methods_prompt.py:171-341` against the binding decision table:

| Shape | Code path | Result |
|---|---|---|
| Dual ID + ordinal | `:188-189` before resolve/write | 4004 |
| Targetless + any confirm flag | `:183-195` (`confirm_truncate` / `confirm_empty_truncate` / `confirm_delete_entire_transcript`) | 4004 |
| ID without `confirm_truncate` | `:203-205` | 4029 (ID-specific message) |
| Ordinal without `confirm_truncate` | `:224-240` | 4029 (legacy message) |
| Unresolvable/malformed ID | `:208-210` → 4018; flags do not rescue | 4018 |
| Ordinal 0 needs `confirm_empty_truncate` | `:257-264` with `id_target=False` | 4028 |
| ID-zero needs `confirm_delete_entire_transcript`; **ignores** `confirm_empty_truncate` | `:260-263` branch on `id_target` | 4028 unless dedicated flag |
| Flags never cross-arm | ID path never reads `confirm_empty_truncate`; ordinal path never reads `confirm_delete_entire_transcript` for the empty gate | MATCH |
| Busy + any target/confirm | `:141-153` returns 4009 **before** `_handle_busy_submit` | no queue leak |
| No targetless write | Truncation block only entered when ID or ordinal present (`:196`) | MATCH |

**Proof by reading (no mutation performed):** every path that can call `db.replace_messages` sits inside the confirmed-target block after the empty-prefix second gate. Ordinary submit, dual-target, targetless confirms, unconfirmed targets, bad IDs, and busy rewinds return before that call.

Oracle coverage: `test_rewind_confirm_matrix_and_dual_target`, `test_rewind_id_busy_submit_does_not_queue_destructive_state`, plus pre-existing ordinal 4028/4029/4004 suite (`test_prompt_submit_refuses_confirm_truncate_without_target`, empty-truncate tests, etc.).

---

## K3 — NON-REGRESSION (write-before-memory + archive flags) — PASS

Evidence `methods_prompt.py:294-341`:

1. `db.replace_messages(..., active_only=True, archive_dropped=True)` at `:319-324`
2. On exception → 5008, **no** memory assign (`:325-339`)
3. Only then `session["history"] = truncated` and version bump (`:340-341`)
4. `session["running"] = True` / turn start after successful truncate path (`:342+`)
5. Compute-host dispatch remains after the lock (`:347-348`), text-only

Tests:
- `test_rewind_confirm_matrix_and_dual_target` asserts kwargs on success cut
- `test_rewind_id_db_failure_prevents_memory_and_turn` asserts history unchanged + no turn
- `test_rewind_preserves_soft_archived_rows` real SessionDB: pre-inactive rows + newly dropped live tail remain inactive
- Pre-existing archive/active_only assertions still in file (~17468+)

**No silent regression to weaker kwargs.** BLOCKING class clear.

---

## K4 — ARE THE TESTS REAL? — PASS (with noted limits)

Nine named producer assertions (completion epoch) plus safety/identity tests:

| Test | Path exercised | What it would REFUSE |
|---|---|---|
| `test_rewind_producer_session_create_ids_resolve_on_registered_path` | `handle_request(session.create)` | missing/non-resolving IDs; IDs on non-users |
| `test_rewind_producer_child_watch_resume_ids_resolve_on_registered_path` | registered `session.resume` lazy | marker non-null; unreachable ordinals |
| `test_rewind_producer_deferred_cold_resume_ids_resolve_on_registered_path` | default cold resume | same tri-state break |
| `test_rewind_producer_eager_resume_ids_resolve_on_registered_path` | `eager_build=true` resume | same |
| `test_rewind_producer_live_reuse_payload_ids_resolve_on_registered_path` | live reuse via resume | live payload without resolvable IDs |
| `test_rewind_producer_session_history_is_conservative_for_db_display` | `session.history` | **any** non-null ID (all_null oracle) |
| `test_rewind_producer_local_compression_ids_resolve_on_registered_path` | `session.compress` local | unannotated replacement transcript |
| `test_rewind_producer_session_branch_ids_resolve_on_registered_path` | `session.branch` + real temp SessionDB | branch messages without resolving IDs |
| `test_rewind_producer_compute_host_control_ack_ids_resolve_in_host_frame` | real `ComputeHost._handle_control` JSON frame | host frame users lacking rewind_id / non-resolving |

Shared oracle `_assert_cp1_producer_ids` (`tests/test_tui_gateway_server.py:3728-3744`):
- key presence on users only
- marker null
- reachable non-null
- `resolve_rewind_ordinal` against the same model history → expected ordinals

These are **not** source-text snapshots or frozen catalogs. They are resolution-backed behavioral contracts on registered handlers/frames.

**Limits (non-blocking):** resume DB is a stub returning identical display/model lists; divergence is only forced in `session.history` (ancestor) and `display_kind` pure/adapter tests. That is weaker than a full cold-resume dual-projection E2E with ancestor-inclusive display + sanitized model, but map-required branches are still entered.

---

## K5 — MUTATION CREDIBILITY (host egress dependency) — PASS by dependency proof

Reviewer did **not** mutate the tree. Dependency analysis of
`test_rewind_producer_compute_host_control_ack_ids_resolve_in_host_frame`
(`tests/test_tui_gateway_server.py:3984-4003`):

1. Instantiates real `ComputeHost`, calls `host._handle_control({..., "route_name": "slash.model", ...})`.
2. Parses stdout JSON `control.ack` and runs `_assert_cp1_producer_ids(frame["messages"], history)`.
3. Production annotation site for that route is **only** `tui_gateway/compute_host.py:741-742`:
   `messages = server._history_to_client_messages(history, history)` then emit at `:756`.
4. Parent-side code (`methods_session.py` compress preserve) is **not on this call graph**.
5. If egress used `_history_to_messages` alone, user rows would lack `rewind_id` keys and the oracle's `all("rewind_id" in message for message in users)` would fail.

**Conclusion:** the host-frame assertion chain **requires** `compute_host.py` egress annotation; it would **not** pass with parent-side annotation alone.

Separate parent-preservation test (`:4120-4133`) covers the non-reconvert path at `methods_session.py:2537`.

Note: host `session.compress` route (`compute_host.py:698-735`) emits `result` from the local compress method (already annotated at `methods_session.py:2651`) rather than the generic `:742` messages field. Parent returns `host_result` verbatim when dict. That path is covered by local compress producer + pre-existing host compress tests, not by the `:742` spy — acceptable; not a missing Restore producer.

---

## K6 — RF3 (`session.history` conservative nulls) — PASS

Prior plan RF3 (HIGH if mishandled): never stamp IDs from display-only projection.

Production: `methods_session.py:2461-2462`
```text
"messages": _history_to_client_messages(history, []),
```
Empty model history ⇒ annotator cannot prove spine matches ⇒ every user gets explicit `rewind_id: null` (`rewind_identity.py:147-149`). No ordinals derived from ancestor-inclusive display.

Test: `test_rewind_producer_session_history_is_conservative_for_db_display` builds ancestor+leaf display via DB stub and asserts `all_null=True`. Would fail if display were passed as model history.

**RF3 residual closed for CP-1 implementation.**

---

## K7 — PRODUCER COMPLETENESS — PASS

Re-verified Restore-bearing emitters on the candidate:

| Site | Disposition |
|---|---|
| `compute_host.py:742` | annotated |
| `methods_session.py:133` create | annotated |
| `methods_session.py:494` child-watch | annotated |
| `methods_session.py:581` deferred cold | annotated |
| `methods_session.py:643` eager | annotated |
| `methods_session.py:2462` session.history | conservative null annotate |
| `methods_session.py:2651` local compress | annotated |
| `methods_session.py:2937` branch | annotated |
| `server.py:7234-7239` helper def | OK |
| `server.py:8208-8210` live payload | annotated (display, model=`session["history"]`) |
| `methods_session.py:2537` host compress parent | preserve list(ack.messages); no double convert |
| `server.py:12737/12772/12781` /history /context text | bare `_history_to_messages` — map EXCLUDE |
| `methods_session.py:2738` session.save | raw history export — map EXCLUDE |
| `compute_host.py` SpikeAgent internal / turn.end count | not Restore row lists — EXCLUDE |

**No Restore-bearing emitter left bare** on the gateway/host tree beyond intentional exclusions.

---

## K8 — RED HONESTY — PASS with residual note

- Initial RED (receipt claim, consistent with capability asserts in helpers): module/`_history_to_client_messages` absent — map explicitly allows this only until skeleton exists; not sole final RED evidence.
- Final acceptance tests fail for **criterion-shaped** reasons when broken:
  - A1: resolve/prefix/ordinal assertions
  - A2/A4: 4028/4029 + empty `calls` (no write/turn)
  - A3: display_kind indices + distinct duplicate IDs
  - A5: real DB inactive contents + kwargs
  - Producers: resolution-backed tri-state
- Residual: several tests still open with `hasattr`/`find_spec` gates; after GREEN those are dead weight, not false greens. A subtly wrong annotator that emits plausible but non-resolving IDs is refused by `resolve_rewind_ordinal` checks.

Not a HOLD.

---

## K9 — DIFF HYGIENE / SCOPE — PASS

Worktree vs pin:

```text
M tests/test_tui_gateway_server.py
M tui_gateway/compute_host.py
M tui_gateway/methods_prompt.py
M tui_gateway/methods_session.py
M tui_gateway/server.py
?? tui_gateway/rewind_identity.py
?? .ai/   (evidence only)
```

- Exactly six CP-1 owned source/test files; no CP-2 (`hermes_cli/web_routers/sessions.py`, `tests/hermes_cli/test_web_server.py` diff empty).
- Pre-staged unified diff covers the five tracked files only; sixth file is the new untracked `rewind_identity.py` (present in worktree, 171 lines). Candidate set still matches ownership table.
- No secrets/credentials/tokens in added diff lines (scanned).
- No new dependency, network egress, or elevated permission.
- Approximate net ~671 lines (tracked 537−37+ insertions accounting + 171 new) under soft 950 / hard 1150.
- No `hermes_state.py` / schema / desktop / forbidden path touches.

---

## K10 — INDEPENDENT TEST RUN

Command:
`C:\hrp\venv\Scripts\python.exe -m pytest tests/test_tui_gateway_server.py -q`

**Observed: `550 passed in 43.83s` (exit 0).**

Matches completion-receipt claim of 550. Builder whole-file green claim verified independently.

---

## Findings

### F-CP1-1 — NON-BLOCKING — ID-path skill replay expansion not mirrored

- **Evidence:** `methods_prompt.py:122-129` expands skill invocation only when `truncate_user_ordinal is not None`. ID-only rewind does not enter this branch.
- **Violated requirement:** none of A1–A5 directly; parity with pre-existing ordinal rewind UX (comment at `:122-126`).
- **Severity:** MEDIUM product residual / NON-BLOCKING for CP-1 safety gate.
- **Root cause:** port added ID target beside ordinal without extending the skill-expansion predicate to `truncate_message_id is not None`.
- **Post-fix invariant:** `if (truncate_user_ordinal is not None or truncate_message_id is not None) and isinstance(text, str):` expand.
- **Options:** (a) extend predicate; (b) document deliberate deferral.
- **Recommendation:** (a) one-line fix in a fast-follow before Desktop ships ID rewind for skill turns.
- **Exact regression test:** seed a skill-scaffold user turn; submit with its `rewind_id` + confirms; assert expanded skill body reaches agent/history, not the short invocation.
- **Confidence:** 0.88
- **Unknowns:** whether Desktop will send display invocation text or raw content on ID restore.

### F-CP1-2 — NON-BLOCKING — Producer dual-projection under-tested for divergence

- **Evidence:** `_CP1ResumeDB.get_resume_conversations` returns `(list(self.history), list(self.history))` (`tests/...:3766-3767`); create/compress/branch similarly align.
- **Severity:** LOW residual evidence pressure (RF2-class), not a production defect.
- **Root cause:** stub simplicity.
- **Post-fix invariant:** at least one resume producer test with ancestor-inclusive display ≠ sanitized model proving tip IDs + leading nulls.
- **Recommendation:** optional fast-follow test; not required to clear CP-1 given RF3 history test + pure spine tests.
- **Confidence:** 0.8

### No BLOCKING findings.

---

## Checklist scorecard

| ID | Result |
|---|---|
| K1 contract | PASS |
| K2 destructive safety | PASS |
| K3 write-before-memory / archive flags | PASS |
| K4 tests real | PASS |
| K5 host egress dependency | PASS (by read proof) |
| K6 RF3 session.history | PASS |
| K7 producer completeness | PASS |
| K8 RED honesty | PASS (residual note) |
| K9 hygiene/scope | PASS |
| K10 pytest 550 | PASS (observed) |

---

## Disagreements (explicit)

1. **Disagree with treating completion receipts as evidence** — ignored; re-verified in code and by running tests.
2. **Agree with completion claim that nine producer paths are wired and green** — confirmed by code inventory + suite.
3. **Agree with builder K5 claim direction** — host-frame test depends on `compute_host.py:742`, not parent annotation alone.
4. **Disagree lightly with any implication that producer E2E dual-projection divergence is fully proven** — wiring is real; divergence oracles are thin outside `session.history` / pure tests (F-CP1-2).
5. **Disagree that CP-1 is “done for all ordinal-path side effects”** — skill expansion gap (F-CP1-1) remains; not a safety HOLD.

---

## ONE item least sure of

Whether **Desktop cold Restore** will consume gateway-annotated resume payloads (vs future REST tip-window IDs) with `omit_messages` / host isolation configurations in the field. That is still OPEN product-path evidence (plan RF residual / map UNKNOWN #3/#7), outside CP-1 code correctness, and does not block CP1_REVIEW_PASS. CP-2 remains the REST half.

---

## Unexamined → OPEN (not pass)

- Full six historical patch bytes vs every non-rewind hunk (spec extraction exists; this review trusted semantic mapping + code, not a second full `git show` of all six objects).
- Desktop Restore click path / field configs (forbidden desktop edit; read-only not fully re-done this pass).
- Non-messages REST feeders (CP-2 / map UNKNOWN #5).
- Concurrent REST page vs model-history read (CP-2 / prior RF1).
- Live mutation of host egress (forbidden this pass; dependency proven by call graph instead).

---

## Verdict

Independent CP-1 code review complete enough to judge the candidate against the cleared map.

**SCAN_COMPLETE:YES CP1_REVIEW_PASS**
