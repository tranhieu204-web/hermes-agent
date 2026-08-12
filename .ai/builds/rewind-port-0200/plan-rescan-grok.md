# Plan Rescan — `rewind-port-0200` execution-map-v2

- Reviewer role: independent blind rescan (BLIND_REVIEWER=YES)
- Successor artifact: `C:\hrp\.ai\builds\rewind-port-0200\execution-map-v2.md`
- Dispatch-bound SHA-256: `e73690005a1fc63e0c4c332ccd2cb4aa6312914cbbaabf27202177bcbc9a4c13`
- Measured SHA-256: `e73690005a1fc63e0c4c332ccd2cb4aa6312914cbbaabf27202177bcbc9a4c13` (MATCH)
- Also read in full: `response-matrix.md`, frozen predecessor `execution-map.md`, prior HOLD review `plan-review-grok.md` (context only)
- Workspace HEAD / base pin checked: `ee472a7fdbbc55924f91ab122dbaa29bd07668b0` (exact match)
- Candidate venv: `C:\hrp\venv\Scripts\python.exe -m pytest` → pytest 9.1.1 (executable)
- Method: whole-successor reread + direct base inspection. Read-only. No implementation, no ref mutation, no live-install touch.
- Chairman bindings accepted: scope revision APPROVED (~8 files); REST parity STAYS in v1.
- Verdict: **SCAN_COMPLETE:YES PLAN_REVIEW_PASS COUNTER_UPHELD**

---

## Executive summary

The successor map binds the prior HOLD remedies in the map body itself (not only in the response matrix). Producer ownership now includes `methods_session.py` and `compute_host.py`; REST production ownership is the real router; the always-paged tip-window rule replaces the obsolete unpaged gate; `display_kind` is a hard ordinal-space contract; the confirm decision table is binding including dual-target 4004; gates use the candidate venv only.

**F7 counter-criticism is COUNTER_UPHELD.** Base `methods_prompt.py:157-303` consumes truncation under the history lock (including `replace_messages(..., active_only=True, archive_dropped=True)` then memory assign at `:302-303`); `:309-317` dispatches `_submit_prompt_to_compute_host(..., text)` with no target/confirm forwarding. Dropped truncation fields are not the defect class. The real cross-process gap is transcript egress annotation (`compute_host.py:741/755` and parent re-entry `methods_session.py:2536-2549`), which v2 owns.

No new BLOCKING plan defect found that requires another remap before build-GO. Residual risks remain as OPEN/UNKNOWN=AFFECTED obligations and soft review-pressure on CP-1 size — not silent passes.

---

## Counter-criticism adjudication (F7) — mandatory

### Builder claim
Base ordering proves truncation fields are parent-consumed before host dispatch; field forwarding is not required. Real gap is producer annotation; add `compute_host.py` to CP-1.

### Independent base evidence
| Site | Observation |
|---|---|
| `tui_gateway/methods_prompt.py:157-303` | Under `history_lock`: validate, resolve ordinal cut, `replace_messages(..., active_only=True, archive_dropped=True)` at `:281-286`, then `session["history"]=truncated` / version bump at `:302-303`. |
| `methods_prompt.py:309-317` | After the lock block: `_submit_prompt_to_compute_host(rid, sid, session, text)` — text only. No ordinal/confirm kwargs. |
| `tui_gateway/compute_host.py:718-758` | Host `control.ack` builds `messages` via `server._history_to_messages(list(session.get("history") or []))` at `:741`, emits at `:755`. |
| `tui_gateway/methods_session.py:2536-2549` | Parent host-compress fallback re-runs `_history_to_messages(ack.get("messages"))` before Desktop transcript replacement. |

### Ruling
**COUNTER_UPHELD.**

- Prior F7 framing that centered “may drop truncation fields” is weaker than the base ordering evidence. Parent-side write-before-memory already finishes before dispatch; host does not need target/confirm params for submit correctness.
- Builder remedy direction is correct: keep submit consumption parent-side; own host transcript annotation; forbid unsafe double conversion on the parent re-entry path.
- Residual (still OPEN, not a counter-reject): full host frame-field inventory and proof that parent persistence always precedes every host turn path remain evidence obligations (map §UNKNOWN #2; required tests list DB-failure ordering and host producer). That is execution evidence, not a map ownership hole.

Disagreement with prior self (v1 review F7 severity framing): the open question was over-weighted toward param forwarding; producer boundary is the load-bearing gap. Corrected here.

---

## R1 — Full C1–C12 checklist against SUCCESSOR

### C1 FORM — PASS
- Stamps **DECOMPOSED**. CP-1 identity+gateway/host producers+submit; CP-2 REST parity against frozen pure contract.
- Chairman D1/D2 bound; old “seventh file = HOLD” revoked.
- Planning-only boundary explicit.

### C2 COVERAGE A1–A5 — PASS
| Criterion | Owner | Bound? |
|---|---|---|
| A1 exact only | CP-1 (+ REST A1 via CP-2) | Yes — pure helper + resolve + producers |
| A2 no auto-empty | CP-1 | Yes — decision table ID-zero needs dedicated flag |
| A3 identity not position | CP-1 (+ CP-2) | Yes — occurrence/prefix + display_kind filter |
| A4 opening turn | CP-1 | Yes — `confirm_truncate` + `confirm_delete_entire_transcript` |
| A5 archive preserve | CP-1 | Yes — no migration; retain write kwargs; real SessionDB oracle |

### C3 RED-CAPABILITY — PASS (on base, conditional on test authorship discipline)
Verified on pin:
- `tui_gateway/rewind_identity.py` ABSENT
- No `truncate_before_message_id` / `rewind_id` consumer on base submit
- `pytest -k rewind_identity` on gateway file: exit 5 / no tests (capability evidence only)
- Map RED honesty (F8 remedy) forbids treating collection-error or unrelated 4028 as sole RED for A2/A4 — BOUND

### C4 ORDERING — PASS
- CP-1 before CP-2; CP-2 imports frozen pure helper; no reverse GREEN dependency.
- Submit ordering: validate → resolve → `replace_messages` → memory → turn/host dispatch — BOUND at map L89 and invariant #7.

### C5 ONE WRITER PER FILE — PASS
Ownership table (map L237-246): eight files, sole writers, cross-CP forbidden. No double-writer. Missing-writer class from v1 is closed for the inventoried Restore-bearing set.

### C6 EIGHT EDGE TYPES ONLY — PASS
Declared edges use only the eight allowed types. UNKNOWN=AFFECTED list retained (not emptied by prose).

### C7 INSPECTABILITY — PASS with residual pressure
- WP split is independently inspectable (gateway vs REST).
- CP-1 is large (six files, many producer sites). Map soft budget ≤950 net / HOLD >1150 or “cannot review in one sitting” is an explicit safety valve — acceptable given Chairman D1, not a silent free pass.
- See R8.

### C8 ROLLBACK — PASS
- CP-1 six-file atomic revert; CP-2 two-file independent revert; no migration.
- Partial CP-1 release forbidden.

### C9 INERT-SHIP / production callers — PASS (for named surfaces)
- Submit live path remains `methods_prompt.py` with archive flags — verified.
- Gateway producers owned at real emission sites (inventory table).
- REST producer is `hermes_cli/web_routers/sessions.py:601-652` — verified; `web_server.py` correctly non-writer (include/re-export only at `:11314-11328`).
- Exclusions (`/history` text, `/context` text, browser diagnostic, session.save raw export, SpikeAgent internal) are named and reason-bound.

### C10 A5 / no-migration — PASS
- Identity computed; no `hermes_state.py` / schema / storage API writer.
- `archive_dropped=True` + `active_only=True` immutable in invariants and decision-table footer.

### C11 FORBIDDEN TOUCHES — PASS
- Live installs, main/preupdate refs, desktop source, schema, builder self-clear, cross-CP writes — forbidden.
- Repair-mission artifact list respected (this rescan writes only the allowed verdict file outside builder epoch).

### C12 DELIBERATE OMISSIONS — PASS
- Safe omissions retained (no desktop UX, no persisted IDs, no ordinal removal, no broad refactors).
- Prior unsafe omissions (methods_session, web_routers, paged REST redesign) are no longer omitted.

---

## R2 — F1–F8 and F-COO-1 remedies: bound in map vs prose-only

| ID | Disposition | Bound in successor map? | Evidence |
|---|---|---|---|
| F1 producers | AGREE | **YES** | Inventory table L30-51; CP-1 owns `methods_session.py` L127; shared `_history_to_client_messages` L66 |
| F2 REST file | AGREE | **YES** | CP-2 owns `hermes_cli/web_routers/sessions.py` L159; web_server non-writer L162 |
| F3 always-paged | AGREE | **YES** | §REST always-paged safe-window L91-115 |
| F4 display_kind | AGREE | **YES** | Invariant #6 L22; hard interface L61-64 |
| F5 confirm matrix | AGREE | **YES** | Decision table L68-89 including dual-target 4004 |
| F6 REST tests | AGREE | **YES** | CP-2 owns `tests/hermes_cli/test_web_server.py` L160 |
| F7 compute-host | PARTIAL + counter | **YES** | `compute_host.py` CP-1 writer L129; host annotate L47-48; no field forwarding L89; double-convert fix L43 |
| F8 RED honesty | AGREE | **YES** | RED honesty paragraph L147; A2/A4 capability-first L185-188 |
| F-COO-1 gates | AGREE | **YES** | All gate commands use `C:\hrp\venv\Scripts\python.exe -m pytest`; HOLD if `run_tests.sh` L269; sequence L225 |

No finding is “fixed only in response-matrix.” Matrix and map agree on dispositions; map carries the binding design.

---

## R3 — Producer inventory completeness

### Base `_history_to_messages` call sites (re-enumerated on pin)

| Site | Map disposition | Assessment |
|---|---|---|
| `server.py:7136-7231` definition | Keep conversion; separate annotating helper | Correct |
| `server.py:8199` `_live_session_payload` | Annotate | Correct; covers reuse + `session.focus`-class live payload (`methods_session.py:417/686/962`) |
| `server.py:12725` `/history` text | Exclude | Correct |
| `server.py:12760/:12769` `/context` text | Exclude | Correct |
| `methods_session.py:133` create | Annotate | Correct |
| `methods_session.py:494` child-watch | Annotate | Correct |
| `methods_session.py:581` deferred cold | Annotate | Correct |
| `methods_session.py:643` + payload `:792-807` eager | Annotate once | Correct |
| `methods_session.py:2461` session.history | Annotate / conservative nulls | Correct (dual-projection hard here) |
| `methods_session.py:2536` host compress re-entry | Preserve annotated host rows; no unsafe reconvert | Correct and necessary |
| `methods_session.py:2650` local compress | Annotate | Correct |
| `methods_session.py:2737` session.save raw | Exclude | Correct (file export schema) |
| `methods_session.py:2936` branch | Annotate | Correct |
| `compute_host.py:741/755` control.ack | Annotate on host | Correct |
| `compute_host.py:46-79` SpikeAgent internal | Exclude internal; feeds later egress | Correct |
| `compute_host.py:425` turn.end message_count only | Not a transcript row list | Not a Restore producer |
| REST `web_routers/sessions.py:601-652` | CP-2 tip-window | Correct |
| `web_server.py:11314-11328` | No edit | Correct |

### Still unowned / deliberately open (not silent pass)
1. **Non-messages REST surfaces** that might feed Restore (e.g. export stream `sessions.py:722+`) — map UNKNOWN #5; HOLD/remap if a second writer appears after route inventory. **Not proven clear.**
2. **Desktop client event streams** (turn deltas without full annotated transcript) — desktop forbidden; UNKNOWN #3/#7.
3. **No additional `_history_to_messages` site found unowned** on the gateway/host tree beyond the table.

**R3 result:** Claimed inventory is complete for `_history_to_messages` Restore-bearing emitters on base. Builder line list matches. No ninth mandatory implementation file discovered this rescan.

---

## R4 — Always-paged REST tip-window safety (attack)

Rule restated: mint only when tip-ordered latest window, `offset==0`, non-empty page, complete sanitized model history from same read-only profile DB handle, suffix-align via pure annotator; else no new `rewind_id` keys (absence ≠ annotator null).

| Attack | Map behavior | Verdict |
|---|---|---|
| >500 rows, default GET | IDs from **complete** model history; stamp only returned tip suffix | SAFE if GREEN tests force >500 seed (bound L109) |
| `offset>0` | No minted keys | SAFE (L110) |
| Oldest / effective oldest (`limit` set, order omitted) | No IDs | SAFE; matches base `latest_page = order=="latest" or (order is None and default_page)` at `sessions.py:627-628` |
| `limit=100&offset=0&order=latest` | Qualifying tip window | SAFE (L112) |
| Empty page | No mint | SAFE |
| Annotation/history failure | 200 + original rows | SAFE fail-closed (L114) |
| Concurrent writes between page read and resume-history read | Not transaction-bound explicitly | **RESIDUAL RISK, fail-closed in practice:** spine mismatch → stop alignment → null/no IDs. Should be same `_read()`/handle (bound). Not BLOCKING; optional strengthen with single snapshot/transaction note at implement time |
| Raw REST rows vs gateway `{text}` rows | Spec annotator `_display_text` accepts both `text` and `content` (`git show` identity module) | SAFE **if** CP-1 ports that dual coercion (map says final-spec semantics + content coercion L57) — must not drop `_display_text` |
| Skill-scaffold / structured divergence | OPEN #8; null never guess | Acceptable fail-closed |

**R4 result:** Rule is genuinely safer than the obsolete unpaged gate. No BLOCKING hole found. Residual: concurrent-read consistency and dual-shape coercion must survive implementation review.

---

## R5 — Confirm-flag decision table

| Check | Result |
|---|---|
| Dual ID+ordinal → 4004 before resolve | BOUND L76 |
| Targetless confirms → 4004 | BOUND L75 (stricter than base bare-`confirm_truncate` only — OK) |
| Ordinal requires `confirm_truncate` | BOUND L77 |
| Ordinal 0 needs `confirm_empty_truncate` | BOUND L79-80 |
| ID requires `confirm_truncate` | BOUND L81-82 |
| ID-zero ignores `confirm_empty_truncate`; needs `confirm_delete_entire_transcript` | BOUND L83-84 — non-cross-arming |
| Flags never rescue bad identity | BOUND L85-86 |
| Busy/queue non-leakage | BOUND L87 |
| write-before-memory + `archive_dropped=True` | BOUND L23, L89 |
| Host dispatch after parent consume; no forward | BOUND L89 |

Implicit implementation obligation (not a table hole): base `methods_prompt.py:169-174` currently treats “no ordinal” as no target for bare `confirm_truncate`. ID must count as a target when introduced — required by L75/L81. Decision table is sufficiently binding.

**R5 result:** PASS — complete enough and non-cross-arming; preserves base durability ordering.

---

## R6 — Gate commands on this machine

- All RED/GREEN commands: `C:\hrp\venv\Scripts\python.exe -m pytest ...`
- No `scripts/run_tests.sh` in successor gates
- Verified executable: pytest 9.1.1; gateway collect works; `tests/hermes_cli/test_web_server.py` messages default test collects

**R6 result:** PASS

---

## R7 — Eight carried-forward OPEN gaps

Map §UNKNOWN L211-221 and matrix L74-85 list the same eight themes. None are quietly marked closed without evidence:

1. Spec patches line-by-line — OPEN until pre-edit extraction
2. Compute-host frame fields — OPEN; file owned, fields still AFFECTED
3. Desktop omit_messages/Restore shape — OPEN; desktop forbidden
4. Truncate-suite inventory — OPEN before adding REDs
5. Non-manage REST Restore feeders — OPEN before CP-2
6. Profile DB handle equivalence — OPEN with test obligation
7. Desktop live/cold/watch/eager/host matrix — OPEN; producers conservatively AFFECTED
8. Structured/skill coercion — OPEN; null/shared coercion, never guess

**R7 result:** PASS — genuinely open, not papered over.

---

## R8 — New defects from repair / CP-1 cut quality

### Expanded write set
v1 CP-1: 4 files → v2 CP-1: 6 files (adds `methods_session.py`, `compute_host.py`). Total build: 8 files. Chairman-approved.

### Is CP-1 still one-sitting reviewable?
- **Not mis-cut on ownership grounds:** identity, gateway producers, host producer, and destructive consumer share one atomic fail-closed release unit; splitting producers from submit would ship half-annotated Restore surfaces.
- **Pressure is real:** many call sites inside `methods_session.py` + dual-projection subtlety (`_live_visible_history` merge at `server.py:8139+`) + host double-convert footgun. Budget HOLD (>1150 net or unreviewable) is necessary and present.
- **No further mandatory split found** that preserves independent usefulness without product regression.

### Repair-introduced defects?
- No new inert-ship file naming.
- No reintroduction of unpaged REST gate.
- No weakening of archive flags.
- Soft note: CP-1 line soft-allocation sums above target (overlap admitted); total-target governance is the real control — OK.

**R8 result:** No BLOCKING mis-cut. Residual execution risk only.

---

## End-of-scan findings

### NF1 — COUNTER_UPHELD (F7) — not a defect
- Evidence: `methods_prompt.py:157-317`; `compute_host.py:741-755`; `methods_session.py:2536-2549`
- Severity: n/a (adjudication)
- Confidence: 0.93

### NF2 — No BLOCKING residual plan defect
Prior F1–F6, F8, F-COO-1 remedies are map-bound and base-verified.

### RF1 — RESIDUAL / NON-BLOCKING — REST multi-query consistency
- Evidence: map L100 fetches page + `get_resume_conversations` in same DB scope; base handler today is a single `get_messages` call (`sessions.py:615-637`)
- Violated requirement: none yet; defense-in-depth
- Severity: LOW
- Root cause: NOT ESTABLISHED as a plan bug; concurrent writer can desync two reads
- Post-fix invariant: annotation fail-closed on spine mismatch; prefer one snapshot/connection for both reads
- Recommendation: implement both reads inside one `_read()` with one `db`; tests optional race only if cheap
- Confidence: 0.7

### RF2 — RESIDUAL / NON-BLOCKING — CP-1 review pressure
- Evidence: map L149 budgets; six-file CP-1; dense producer set
- Severity: LOW–MEDIUM process risk
- Root cause: inherent to complete producer ownership (correct expansion)
- Post-fix invariant: HOLD if unreviewable or >1150 net without redesign
- Recommendation: enforce inventory checklist in CP-1 diff inspection step L229
- Confidence: 0.75

### RF3 — RESIDUAL OPEN (carried) — session.history model projection
- Evidence: base `methods_session.py:2442-2463` may replace history with ancestor-inclusive DB display only
- Map L42 allows conservative nulls — correct fail-closed
- Severity: LOW if nulls used; HIGH only if implementer stamps IDs from display-as-model
- Recommendation: test forces null or dual-correct IDs; never display-only ordinals
- Confidence: 0.8

### Disagreements (explicit)
1. **Disagree with prior F7 primary framing** (param drop as main risk): **COUNTER_UPHELD** toward builder — producer annotation is the real gap; ordering disproves required field forwarding.
2. **Agree with builder** on F1–F6, F8, F-COO-1 dispositions and chairman D1/D2 application.
3. **Disagree lightly with any implication that OPEN gaps are cleared** by ownership alone: ownership ≠ closed evidence (map correctly keeps them OPEN).
4. **No disagreement** that forces HOLD on this successor.

### ONE item least sure of
Whether **cold Desktop Restore** will actually consume gateway-annotated resume payloads vs REST tip-window IDs in the field configurations users run (`omit_messages`, host isolation). Chairman D2 keeps REST in scope; gateway producers are owned — product path matrix remains OPEN #3/#7. This does not block plan GO; it bounds acceptance evidence the builder must still gather read-only without editing desktop.

### Unexamined → OPEN (not pass)
- Full six-spec-object line-by-line semantic extraction (builder pre-edit obligation)
- Full host control-frame schema dump beyond the messages field
- Desktop source Restore click path
- Every existing truncate test’s exact assertion text in `tests/test_tui_gateway_server.py`
- Export/other REST transcript consumers beyond messages GET

---

## Acceptance vs prior HOLD predicates

| Prior required remap item | Successor status |
|---|---|
| Add `methods_session.py` to CP-1 | DONE |
| Move CP-2 to `web_routers/sessions.py` + real tests | DONE |
| Replace unpaged REST gate with 0.20-safe window | DONE |
| Contract `model_user_indices` ≡ submit incl. `display_kind` | DONE |
| Confirm decision table + write-before-memory | DONE |
| Rewrite file-count HOLD; don’t forbid real topology | DONE (D1) |
| Keep A5 + archive_dropped | DONE |
| Executable candidate-venv gates | DONE |

---

## Verdict

Independent whole-successor rescan complete enough to judge the plan.

**SCAN_COMPLETE:YES PLAN_REVIEW_PASS COUNTER_UPHELD**

Build-GO is not blocked by plan defects found in this rescan. Execution must still close OPEN evidence obligations before claiming GREEN, and must treat the producer inventory table as a checklist, not a memoir.
