# Plan Review — `rewind-port-0200` execution map

- Reviewer role: independent blind plan review (BLIND_REVIEWER=YES)
- Reviewed artifact: `C:\hrp\.ai\builds\rewind-port-0200\execution-map.md`
- Input SHA-256 (dispatch-bound): `c617b7a0423ac9a7e4009253e74e63a8132770ee2f06c68015460e8ea7b3e1ce`
- Measured SHA-256: `c617b7a0423ac9a7e4009253e74e63a8132770ee2f06c68015460e8ea7b3e1ce` (MATCH)
- Workspace HEAD / base pin checked: `ee472a7fdbbc55924f91ab122dbaa29bd07668b0`
- Method: read-only map review + direct base code inspection. No implementation, no ref mutation, no live-install touch.
- Verdict: **HOLD** (see predicates)

---

## Executive summary

The map correctly stamps **DECOMPOSED**, correctly discovers that `prompt.submit` now lives in `tui_gateway/methods_prompt.py` (not monolith `server.py`), and correctly answers **A5 needs no state.db schema change** with line-level `replace_messages(active_only=..., archive_dropped=...)` evidence that I independently verified.

It still **does not bind the real 0.20.0 producer topology** end-to-end:

1. Gateway transcript producers that matter for cold/eager resume are concentrated in **`tui_gateway/methods_session.py`**, which is **not a writer** and is not in the six-file manifest.
2. REST `/api/sessions/{id}/messages` lives in **`hermes_cli/web_routers/sessions.py`**, not in `hermes_cli/web_server.py` as CP-2 claims. Editing only `web_server.py` is the same **inert-ship class** the map claims to avoid for submit.
3. On 0.20.0 the messages endpoint **always pages** (default latest 500). The map ports the pre-0.20 “complete unpaged read” annotation gate as if it still existed.

These are blocking plan defects for a HIGH_RISK rebuild that exists to prevent irreversible transcript loss. Do not build-GO on this map without remap.

---

## C1–C12 checklist

### C1 FORM — PASS (with note)
- Map stamps **DECOMPOSED** and justifies transcript irreversibility.
- Two WPs / two checkpoints; ATOMIC / ATOMIC-LIGHT not used.
- Note: decomposition is real for gateway vs REST, but CP-1 itself is still a very large multi-file behavioral unit (identity + all producers + submit). That is acceptable for HIGH_RISK only if producer ownership is complete; it is not (see F1).

### C2 COVERAGE A1–A5 — PASS on naming; FAIL on production ownership
| Criterion | Named owner | Issue |
|---|---|---|
| A1 exact match only | CP-1 (+ CP-2 REST parity) | CP-1 submit path is correctly identified (`methods_prompt`). Producer incompleteness undermines end-to-end A1 usability. |
| A2 ID never auto-confirms empty | CP-1 | Named; confirm-gate integration with 0.20 `confirm_truncate` underspecified (F5). |
| A3 identity not position | CP-1 (+ CP-2) | Named; `model_user_indices` vs base `display_kind` filter not contracted (F4). |
| A4 opening turn + dedicated confirm | CP-1 | Named; depends on F5. |
| A5 soft-archive preserve | CP-1 | Named; intentionally via ID production path + real SessionDB — good. |

No acceptance criterion is wholly unowned by name. Ownership of the **live call path that emits IDs** is incomplete (F1, F2).

### C3 RED-CAPABILITY — PARTIAL PASS / CONDITIONAL
Verified on base `ee472a7fd`:
- `tui_gateway/rewind_identity.py` is **absent** (`git cat-file` fatal; workspace missing).
- `truncate_before_message_id` is **absent** from `methods_prompt.py`; only `truncate_before_user_ordinal` exists.
- No `rewind_id` stamping on gateway payloads.
- Therefore pure “module missing / param ignored / key absent” REDs are real.

Caveats:
- Map allows “collection/import failure solely due to absent module” as initial RED. That is acceptable only as a bridge; map correctly requires later assertion RED. Keep that discipline.
- A2 RED wording (“base does not recognize ID rewind”) is weak if the test merely sends unknown params that base ignores and then asserts a **4028** that base only emits for ordinal-0 + missing `confirm_empty_truncate`. Tests must be written so each criterion fails for the **right reason** on base, not because an unrelated assertion is unreachable.
- A5 RED via “ID path absent” is valid only if the test would pass on base when run against the existing ordinal path with `active_only/archive_dropped` already true — map correctly rejects a non-RED standalone `replace_messages` unit test. Good.

### C4 ORDERING — PASS
- CP-1 before CP-2; CP-2 consumes frozen annotate API.
- No checkpoint’s GREEN is defined to require a later checkpoint.
- HOLD predicates include “CP-1 test passes only after CP-2”.

### C5 ONE WRITER PER FILE — FAIL (BLOCKING)
Within the **declared** table, each listed file has one writer. Defect is **missing required writers**, not double-writers:

| Required production surface on 0.20.0 | Map writer | Actual |
|---|---|---|
| `tui_gateway/rewind_identity.py` | CP-1 | OK |
| `tui_gateway/methods_prompt.py` (`prompt.submit`) | CP-1 | OK — verified `@method("prompt.submit")` at L67 |
| `tui_gateway/server.py` (`_history_to_messages`, `_live_session_payload`, registration) | CP-1 | Necessary but **not sufficient** |
| `tui_gateway/methods_session.py` (session.create / session.resume message payloads) | **none** | **Required producer file** (F1) |
| `hermes_cli/web_routers/sessions.py` (`GET .../messages`) | **none** (map names `web_server.py`) | **Required REST file** (F2) |
| `hermes_cli/web_server.py` | CP-2 | Only router include + re-export; not the handler body |

Two packages do not both write `server.py` — good — but the map’s “six files, not five” correction stopped one extraction short on the gateway side and named the wrong file on the REST side.

### C6 EIGHT EDGES ONLY — PASS (with thin spots)
- Declared edge types are only the eight allowed.
- UNKNOWN=AFFECTED list exists and includes inventory of payload constructors, dual-target, compute-host, schema creep.
- Defect: several UNKNOWNs are treated as deferrable discovery during build, but the **methods_session / web_routers** facts are already knowable from the pinned base and should have been bound as topology edges before GO (not left as implementation-time HOLD). That is a planning completeness failure, not a ninth edge type.

### C7 INSPECTABILITY — PASS for WP split shape; FAIL for CP-1 producer claim
- WP-1 vs WP-2 split is inspectable in principle (gateway submit vs REST enrichment).
- CP-1 “independent acceptance: gateway payload supplies IDs” is **not true as written** if only `server.py` helpers are patched while `methods_session.py` cold/eager resume paths still emit bare `_history_to_messages(...)` (F1).
- CP-2 “independent REST producer” is not inspectable on the named file because the handler is elsewhere (F2).

### C8 ROLLBACK — PASS (conditional)
- CP-1 four-file revert / CP-2 two-file revert; no migration.
- Half-finished CP-1 is not truly “dark launch”: partial producer annotation + live ID submit changes destructive semantics. Map’s fail-closed resolver helps, but a half-annotated producer set is user-visible inconsistency. Acceptable only if CP-1 remains one atomic revert unit and is not released mid-way — map says that; keep it strict.
- Conditional on remap adding files: rollback manifests must be rewritten with the real file sets.

### C9 INERT-SHIP / production callers — FAIL (BLOCKING)
- Submit consumer path is correctly named: registration `server.py:14413-14428` → `methods_prompt.py:67+` → `replace_messages` at `methods_prompt.py:281-286`. **Good; this is the strongest part of the map.**
- Producer paths are **not** fully named. Evidence of live message emission outside owned files:
  - `methods_session.py:133` session.create messages
  - `methods_session.py:494`, `:581`, `:643` resume/lazy/cold display messages
  - additional host/watch paths at `:2461`, `:2650`, `:2936`
  - REST handler `hermes_cli/web_routers/sessions.py:601-654`
- CP-2 naming `hermes_cli/web_server.py` as the production edit is **inert** relative to the real handler (F2). This is exactly the inert-ship failure mode the map’s intro warns about.

### C10 A5 / state.db — PASS (verified)
Map claim: rewind identity is computed; **no schema/migration**; preserve `active_only=True, archive_dropped=True`.

Independent verification on base:
- `hermes_state.py:8187-8193` — `replace_messages(..., active_only=False, archive_dropped=False)` signature defaults.
- `hermes_state.py:8205-8212` — `active_only=True` preserves soft-archived rows.
- `hermes_state.py:8214+` / `:8242-8257` — `archive_dropped=True` soft-archives live rows via `UPDATE ... SET active=0` instead of DELETE.
- `methods_prompt.py:281-286` already calls `replace_messages(..., active_only=True, archive_dropped=True)`.
- `hermes_state.py:8854-8903` — `get_resume_conversations` projects active rows; no rewind identity column.
- Spec tip `597142813` only added a gateway test; did not migrate schema. Spec call used `active_only=True` only; **0.20 is stronger** with `archive_dropped=True` — map correctly forbids regressing that.

**Risk-boundary answer accepted.** Discovery of required schema change must HOLD — correct.

### C11 FORBIDDEN TOUCHES — PASS
- Global forbidden list includes live install, main, preupdate tags, other hermes-agent path, schema/hermes_state, desktop tree, push/unshallow.
- Per-package forbidden lists cross-exclude WP files.
- No issue found on this axis.

### C12 DELIBERATE OMISSIONS — MIXED
Safe / agreed omissions:
- No cherry-pick/merge/unshallow of shallow history
- No desktop client changes
- No schema / hermes_state / config version
- No broad `/undo` `/retry` `/compress` rewrite
- No builder self-clear

Unsafe / incomplete omissions:
- Omitting **`methods_session.py`** as if server.py annotation covers “every transcript-bearing gateway payload” — **unsafe** (F1)
- Omitting **`web_routers/sessions.py`** while claiming REST parity — **unsafe** (F2)
- Omitting an explicit redesign of REST annotation under **always-paged** messages API — **unsafe** (F3)
- Leaving dual ID+ordinal and compute-host param forwarding as UNKNOWN without a pre-bound refuse/accept matrix — acceptable as HOLD triggers, but dual-target should be decided from spec tests **before** GO if those tests are readable (they are, via `git show`)

---

## Findings (end-of-scan bundle)

### F1 — BLOCKING — Gateway producers not owned: `methods_session.py` missing writer
- **Evidence:**
  - Map CP-1 owned files: `rewind_identity.py`, `server.py`, `methods_prompt.py`, `tests/test_tui_gateway_server.py` only (map L101-105).
  - Base emission sites using `_history_to_messages` in `tui_gateway/methods_session.py` at least: L133 (session.create), L494, L581, L643 (resume family), plus L2461/L2650/L2936.
  - Spec stamps IDs at payload construction with **both** display and model history (`annotate_rewind_ids(messages, history)`), not inside `_history_to_messages` alone (`git show 4d1f52ffb:tui_gateway/server.py` call sites).
  - `_history_to_messages` (`server.py:7136+`) only sees one projection; it cannot mint occurrence-bound r2 IDs safely by itself.
  - `_live_session_payload` (`server.py:8166-8211`) covers some live-reuse paths only.
- **Violated requirement:** C5/C9; map’s own claim that CP-1 annotates every transcript-bearing gateway payload; A1/A3 usability for gateway resume clients.
- **Severity:** BLOCKING
- **Root cause:** ESTABLISHED — map corrected the submit extraction (`methods_prompt`) but did not inventory the parallel session-handler extraction (`methods_session`) for producers.
- **Post-fix invariant:** Every user-visible gateway transcript payload that can expose Restore is annotated via a named owned call path; `methods_session.py` is either a CP-1 writer or all of its message returns demonstrably flow through an owned server.py helper that performs annotate with the correct model history.
- **Options:** (1) Add `tui_gateway/methods_session.py` to CP-1 ownership and bump the “seventh file” HOLD predicate; (2) introduce a single owned helper in `server.py` (e.g. `_history_to_client_messages(display, model)`) and change every methods_session return site to use it — still a methods_session write set; (3) narrow acceptance to “live payload only” and explicitly drop resume/create stamping — rejects A1/A3 for normal desktop/TUI resume.
- **Recommended:** (1)+(2) hybrid — one helper, CP-1 owns `methods_session.py` edits, remap file count and budgets.
- **Regression test:** handle_request `session.resume` (cold + live) and `session.create` fixtures assert user rows carry `rewind_id` / `null` tri-state; omit_messages path remains empty messages.
- **Confidence:** 0.93
- **Unknowns:** exact desktop which-resume-branch matrix; whether some host_* paths must stay unannotated.

### F2 — BLOCKING — CP-2 production file is wrong (`web_server.py` vs `web_routers/sessions.py`)
- **Evidence:**
  - Map L23, L84, L121, ownership table L201: CP-2 modifies `hermes_cli/web_server.py`.
  - Base handler: `hermes_cli/web_routers/sessions.py:601` `@manage_router.get("/api/sessions/{session_id}/messages")` through ~L654.
  - `web_server.py:11314` only `include_router(manage_router)`; `11323` re-exports `get_session_messages` for legacy imports.
  - Map’s cited band `web_server.py:11194-11289` is import/router glue and comments, **not** the messages endpoint body.
  - In-tree note already documents extraction: `tests/test_web_server_sessiondb_eventloop.py` states session route handlers were extracted to `web_routers/sessions.py`.
  - Spec diff landed on pre-extraction `hermes_cli/web_server.py` (`git show 18319f3f4 -- hermes_cli/web_server.py`).
- **Violated requirement:** C5/C9 inert-ship; WP-2 purpose (“cold-opened Desktop/dashboard transcript receives identities”).
- **Severity:** BLOCKING
- **Root cause:** ESTABLISHED — topology correction applied to submit extraction but not to REST router extraction.
- **Post-fix invariant:** CP-2 sole writer includes the module that defines `get_session_messages` production behavior (`web_routers/sessions.py`); `web_server.py` touched only if re-export/helper placement truly requires it.
- **Options:** (1) Remap CP-2 owned files to `hermes_cli/web_routers/sessions.py` (+ tests); (2) move handler back into web_server (out of scope / forbidden broad refactor).
- **Recommended:** (1)
- **Regression test:** HTTP GET messages on TestClient imports app from web_server but asserts annotation behavior implemented in sessions router; monkeypatch/failure degrade tests target the real helper site.
- **Confidence:** 0.97

### F3 — BLOCKING — REST “complete unpaged transcript” annotation gate is obsolete on 0.20.0
- **Evidence:**
  - Spec helper skipped annotation when `limit is not None or offset` and annotated full `get_messages` when limit was None (`18319f3f4` web_server diff).
  - Base `web_routers/sessions.py:627-645`: **always pages**; `default_page = limit is None` → `_limit = 500`; default order is **latest** page.
  - Map L85 still specifies: “`limit is not None` or nonzero `offset` skips annotation”.
  - If applied to **effective** `_limit`, annotation never runs (all responses have a limit).
  - If applied to **request** `limit is None`, default dashboard reads annotate a **latest-500 suffix**, not a full root-to-tip transcript — tail alignment assumptions from the spec must be re-proven; ancestor/lineage + mid-window pages remain unsafe.
- **Violated requirement:** A1/A3 REST producer parity; map CP-2 interfaces; HIGH_RISK fail-closed identity.
- **Severity:** BLOCKING (design hole; shipping either never-annotate or unsafe partial-window IDs)
- **Root cause:** ESTABLISHED — 0.20 pagination safety change invalidated the spec’s complete-read gate; map did not redesign the gate.
- **Post-fix invariant:** Annotation runs only when the returned window is a **suffix of the full display lineage ending at the live tip** (or another proven-safe window), using the same model history the gateway would resume; otherwise no `rewind_id` keys (or explicit nulls only if tri-state is still meaningful). Never mint IDs for arbitrary mid-pages.
- **Options:** (1) Annotate only when `offset==0` and response is tip-aligned latest/full-enough suffix and `returned < limit` or explicit complete flag; (2) fetch full resume conversations server-side for id minting while still returning the page (careful with huge transcripts); (3) drop REST producer from this build and require gateway resume without omit_messages for Restore — product regression; (4) HOLD for desktop contract decision.
- **Recommended:** Remap with (1) or (2) explicitly proven against `get_resume_conversations` + default latest page; add adversarial tests for >500-message sessions and `offset>0`.
- **Regression test:** seed >500 user/assistant rows; default GET messages; assert either safe suffix IDs resolvable by CP-1 or deliberate absence — never IDs for non-tip windows.
- **Confidence:** 0.9

### F4 — HIGH (treat as BLOCKING for implementation contract) — `model_user_indices` must match 0.20 `display_kind` filter
- **Evidence:**
  - Base submit ordinal enumeration (`methods_prompt.py:208-211`): `role == "user" and not m.get("display_kind")`.
  - Existing base test intent at `tests/test_tui_gateway_server.py` ~L9723: ordinal counts only real user turns.
  - Spec `model_user_indices` (`4d1f52ffb:tui_gateway/rewind_identity.py`): all `role == "user"` dicts — **no** `display_kind` exclusion.
  - Map L61/L69 says resolver/indexing must align but never states the 0.20 filter as a hard interface invariant; UNKNOWN on structured/skill text is weaker than this concrete mismatch.
- **Violated requirement:** A1/A3 exact identity; “enumerate exactly like prompt.submit”.
- **Severity:** HIGH → **BLOCKING contract gap** if builder ports spec module verbatim.
- **Root cause:** ESTABLISHED — base hardened ordinal space after/beside spec series.
- **Post-fix invariant:** `model_user_indices` / submit `user_indices` / annotate ordinal space are one function; skill/scaffold/`display_kind` rows do not receive truncatable ordinals.
- **Recommended:** Bind the filter in CP-1 interfaces; pure tests with `display_kind` rows.
- **Confidence:** 0.91

### F5 — HIGH — ID path vs 0.20 `confirm_truncate` / busy-queue / write-before-memory order underspecified
- **Evidence:**
  - Base requires `confirm_truncate=true` with ordinal (`methods_prompt.py:189-207`); bare `confirm_truncate` without target → 4004 (L168-173); bool ordinal rejected (L176-179).
  - Base empty wipe uses `confirm_empty_truncate` for ordinal path (L221-243).
  - Spec final empty wipe: ID path uses only `confirm_delete_entire_transcript`; ordinal keeps `confirm_empty_truncate` (`4793eb531`).
  - Map L68 states dedicated ID wipe consent but does **not** specify:
    - whether ID rewind also requires `confirm_truncate` (or a renamed confirm);
    - how `confirm_truncate` without ordinal but with message_id interacts with L168-173;
    - dual-target ID+ordinal refuse matrix beyond UNKNOWN;
    - preserving write-before-memory order (base writes DB **before** mutating `session["history"]` at L257-307 — reverse of older spec snippet order).
- **Violated requirement:** A2/A4; non-regression of #80763/#82756 class bugs; C6 UNKNOWN should be pre-bound where code already dictates.
- **Severity:** HIGH (can become irreversible-loss bug if mis-integrated)
- **Root cause:** ESTABLISHED for underspecification; exact dual-target desired behavior NOT FULLY ESTABLISHED without re-reading all spec tests (partially examined).
- **Post-fix invariant:** No truncate write without explicit rewind consent; ID and ordinal consent flags do not cross-arm; wipe consents remain path-specific; memory/DB order remains fail-closed write-before-memory as on base.
- **Recommended:** Expand CP-1 interface section with a decision table before GO; port base confirm_truncate gates rather than spec’s older pre-confirm_truncate shape.
- **Confidence:** 0.86

### F6 — MEDIUM — CP-2 test file target likely wrong or weak
- **Evidence:** Map owns `tests/test_web_server.py`. Root file is a small uvicorn keepalive suite (~245 lines). Substantial session messages tests live in `tests/hermes_cli/test_web_server.py` (pagination defaults, etc.). Spec historically edited `tests/test_web_server.py` on the old tree.
- **Severity:** MEDIUM (process/evidence), can become HIGH if tests do not exercise real router.
- **Root cause:** ESTABLISHED path drift.
- **Recommended:** Place REST rewind tests beside existing messages pagination tests (`tests/hermes_cli/test_web_server.py`) or explicitly justify root file + TestClient coverage of manage_router.
- **Confidence:** 0.8

### F7 — MEDIUM — Compute-host isolation may drop truncation fields (acknowledged UNKNOWN, still open)
- **Evidence:** `methods_prompt.py:310` dispatches `_submit_prompt_to_compute_host(rid, sid, session, text)` after in-lock truncation. If truncation is applied **before** dispatch on parent, compute-host may only need text — OK. Map UNKNOWN says trace forwarding; parent-side truncate appears to happen first (L157-307 then L310). Likely OK, but frame builder not fully audited this review.
- **Severity:** MEDIUM open gap
- **Root cause:** NOT FULLY ESTABLISHED for child host path.
- **Recommended:** Keep UNKNOWN=AFFECTED; require explicit trace note in CP-1 evidence bundle.
- **Confidence:** 0.55

### F8 — LOW — A2/A4 RED narrative must not piggyback on unrelated base behavior
- Map L138: base “does not produce required 4028” for ID+generic confirm. Base may ignore unknown fields and run a normal submit (no truncate) — test would fail for “DB unchanged / no 4028” only if it asserts 4028 specifically. Ensure RED assertions match “feature absent” rather than expecting base to speak new error codes.
- **Severity:** LOW/MEDIUM test-design
- **Confidence:** 0.75

### Non-findings / agreements
- **A5/schema:** agree with map; verified (C10 PASS).
- **FORM DECOMPOSED:** appropriate for HIGH_RISK.
- **archive_dropped=True retention:** correct non-regression vs weaker spec.
- **Forbidden trees / no desktop / no cherry-pick:** sound.
- **SHA pin / base absence of rewind_identity:** confirmed.
- **Intent to avoid inert submit integration in server.py only:** correct impulse; incompletely executed for producers/REST.

---

## Disagreements with the map (explicit)

1. **Disagree:** “Production topology is six files… focused modification to `methods_prompt.py`” as the full extraction story. **Also require `methods_session.py` and `web_routers/sessions.py`.**
2. **Disagree:** CP-2 ownership of `hermes_cli/web_server.py` as the messages producer. **Handler is `web_routers/sessions.py`.**
3. **Disagree:** CP-2 interface “complete unpaged read” / `limit is not None skips`. **No such production path on 0.20 default API.**
4. **Disagree (soft):** that UNKNOWN inventory of `_history_to_messages` sites can wait until implementation. On a pinned base those sites are already enumerable; planning should bind them.
5. **Agree:** A5 is compute-from-history; no migration package.
6. **Agree:** submit consumer is `methods_prompt.py`, not a dead server.py body.

**ONE item I am least sure of:** whether desktop cold-open Restore **must** get REST-minted IDs in this build for acceptance, or whether fixing gateway resume `omit_messages=false` paths would suffice product-wise. Mission text includes REST in the historical five-file net diff, so I treat REST parity as in-scope; if product owners de-scope it, F3 severity could drop only after explicit acceptance-criteria change.

---

## Coverage honesty / gaps not fully examined

Examined:
- Map full text + SHA
- Base presence/absence of rewind_identity
- `methods_prompt.py` submit truncate/confirm/replace_messages path
- `hermes_state.py` replace_messages / get_resume_conversations
- `_history_to_messages`, `_live_session_payload`, methods registration tail
- `methods_session.py` message emission sites (listed, not every branch fully semantic-traced)
- REST messages handler + web_server router include
- Spec commits metadata + key diffs (18319f3f4 web/server, 4d1f52ffb identity, c9101a651/4793eb531 confirm semantics, 597142813 A5 test)
- Test file placement for web/gateway

Not fully examined (open gaps, not clean bills of health):
- Full body of all six spec patches line-by-line
- Every compute-host frame field list
- Desktop client omit_messages / Restore request shape in apps/desktop
- Full `tests/test_tui_gateway_server.py` truncate suite semantics
- Whether any non-manage REST transcript endpoint also feeds Restore
- Profile DB selection `_get_db` / `_open_session_db_for_profile` equivalence

---

## Required remap predicates (HOLD)

Build-GO is blocked until a revised map:

1. **Adds `tui_gateway/methods_session.py` to CP-1 write set** (or proves every message-returning path uses an owned annotating helper — proof by file:line inventory, not aspiration).
2. **Moves CP-2 production ownership to `hermes_cli/web_routers/sessions.py`** (and the real test module that exercises messages pagination).
3. **Replaces the obsolete unpaged REST gate** with a 0.20-safe window rule and RED/GREEN tests for default latest-500 and offset pages.
4. **Contracts `model_user_indices` ≡ submit user_indices including `not display_kind`.**
5. **Publishes a confirm-flag decision table** integrating `confirm_truncate`, `confirm_empty_truncate`, `confirm_delete_entire_transcript`, and dual ID/ordinal refuse behavior with base write-before-memory order.
6. **Rewrites file-count HOLD predicates** (seven+ implementation files may be required; “seventh file = HOLD” must not forbid the real topology).
7. Keeps A5 no-migration boundary and archive_dropped non-regression.

---

## Verdict line

Independent review complete enough to judge the plan: **not safe to execute as written**.

SCAN_COMPLETE:YES HOLD—remap required: own methods_session producers + web_routers/sessions REST path; redesign always-paged REST annotation; bind display_kind index parity and confirm_truncate decision table before build-GO
