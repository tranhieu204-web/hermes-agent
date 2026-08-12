# Final Inspection — rewind-port-0200 (CP-2 focused + whole candidate)

- Reviewer: independent blind inspection (BLIND_REVIEWER=YES, READ-ONLY except this verdict)
- Workspace: `C:\hrp`  Branch: `sakaan/rewind-port-0200-20260812`
- Base/HEAD pin verified: `ee472a7fdbbc55924f91ab122dbaa29bd07668b0`
- Binding map: `execution-map-v2.md`
- Pre-staged full candidate diff SHA-256: `6c02a52baf2e93a4639917e68da1f8e5e291167147bdcb937311798a480f5549` (MATCH)
- Prior: `cp1-review-grok.md` (CP1_REVIEW_PASS), `plan-rescan-grok.md`, CP1/CP2 receipts (claims only)
- Method: code/diff read of all eight owned surfaces + SessionDB transaction path analysis + independent combined pytest. Receipts not treated as evidence.

---

## Executive summary

CP-2 REST tip-window producer reuses CP-1's pure annotator, fails closed on unsafe windows and errors, and keeps page + resume history on one read-only handle under an explicit SQLite transaction. Combined gate observed **694 passed, 4 skipped**. CP-1 five tracked files remain **537 insertions / 37 deletions** vs pin (byte-budget/clearance intact; CP-2 did not cross the ownership boundary). No BLOCKING safety, contract-divergence, hygiene, or test-fraud finding requiring HOLD.

Residual non-blocking items remain (F-CP1-1 skill expansion on ID path; F-CP1-2 dual-projection stub thinness; empty CP1-PRODUCTION-PROBE artifact; Desktop consumer not yet fielded for REST IDs). These do not overturn INSPECTION_PASS.

---

## PART A — CP-2 focused review

### A1 SAFE-WINDOW RULE — PASS

Production gate `hermes_cli/web_routers/sessions.py:645`:

```text
if latest_page and offset == 0 and messages and snapshot_started:
```

plus post-annotate accept only when every returned user has a truthy `rewind_id` (`:654-656`).

| Attack | Observed code/test disposition |
|---|---|
| >500-row default GET | `latest_page` true (order omitted + default_page); offset 0; non-empty; full model via `get_resume_conversations` + `sanitize_replay_history`; test seeds 502 rows, expects 500 page and ordinals `1..250` |
| offset > 0 | gate false; test unsafe URL `limit=100&offset=1&order=latest` — no `rewind_id` keys; annotate spy not called |
| explicit oldest | `latest_page` false; no IDs |
| effective oldest (`limit` set, order omitted) | `latest_page = order=="latest" or (order is None and default_page)` → false when limit set; no IDs |
| empty page | `messages` falsy; no annotate; empty response |
| page larger than history | annotate + all-users-truthy gate; incomplete tip match → nulls → reject annotated payload → raw rows |
| tip changed between reads | BEGIN on same `db._conn` before both reads (see A3); concurrent-writer test still mints resolving ID |

**Can a NON-TIP window receive an ID?** Not via this gate. Annotate is never entered for offset>0 / non-latest / empty / no-snapshot. Even inside the gate, partial spine match cannot ship mixed/guessed IDs because REST requires the entire user spine truthy before swapping `messages`.

BLOCKING class (non-tip mint): **clear**.

### A2 FAIL-CLOSED — PASS

`sessions.py:646-661`: lookup/sanitize/annotate wrapped in `except Exception: pass`. Outer GET still returns 200 with pre-annotation `messages`. Snapshot BEGIN failure leaves `snapshot_started=False` → annotation skipped entirely (`:633-641`, `:645`).

Test `test_get_session_messages_rewind_failure_is_unmodified_and_uses_one_profile_db`: annotate raises `RuntimeError` → HTTP 200 and `messages == raw_rows` byte-for-value.

Never a 500 and never partial/guessed IDs on the failure path.

### A3 RF1 (one handle / one snapshot) — PASS (with mechanism note)

- One open: `db = _open_session_db_for_profile(profile, read_only=True)` at `:619`.
- Explicit `db._conn.execute("BEGIN")` before page SELECT; `ROLLBACK` after (`:638-639`, `:662-666`).
- `SessionDB._checkout_read_conn` returns `None` when `self.read_only` is true, so `_read_ctx()` always yields **`self._conn`** (not a WAL read-pool peer). Therefore page (`get_messages`) and model (`get_resume_conversations`) share the connection that holds the transaction.
- Isolation level observed `None` (explicit-BEGIN autocommit style); deferred read snapshot starts at first SELECT inside the transaction.

Test `test_get_session_messages_rewind_page_and_history_share_snapshot` writes a concurrent tip row after the page read returns and still asserts a minted `rewind_id` — which fails if page/history desync forces the all-users truthy gate to reject.

If snapshot cannot start, annotation is disabled (fail closed), not best-effort dual-read minting.

**Disagree lightly with receipt wording that “a shared SessionDB handle alone is insufficient” as the full story** — correct for writable/WAL-pool instances; the load-bearing fact is **read_only disables the pool + BEGIN on `_conn`**. Mechanism is sound for the production call shape.

### A4 CONTRACT REUSE — PASS

`sessions.py:25-26`:

```text
from agent.replay_cleanup import sanitize_replay_history
from tui_gateway.rewind_identity import annotate_rewind_ids
```

No local hash/prefix/spine reimplementation in the router. CP-2 only supplies windowing, snapshot, sanitize, and the stricter all-users accept gate.

Divergent second implementation: **not present**.

### A5 RESOLVABILITY — PASS

`test_get_session_messages_rewind_ids_only_on_proven_tip_windows` does **not** stop at key presence:

- builds complete sanitized model history from the same seeded DB
- asserts `resolve_rewind_ordinal(model_history, row["rewind_id"])` for every returned user equals `list(range(1, 251))`
- asserts distinct IDs on the final two identical `"duplicate"` user contents

REST-minted IDs resolve against the same model history used for minting.

### A6 TESTS REAL — PASS

| Test | Path | Would REFUSE |
|---|---|---|
| `..._ids_only_on_proven_tip_windows` | Starlette `TestClient` → mounted manage route → real `SessionDB` seed 502 rows; annotate spy | non-tip mint; annotate on unsafe/empty; non-resolving IDs; content-only duplicate IDs; ordinals not 1..250 |
| `..._failure_is_unmodified_and_uses_one_profile_db` | TestClient + profile open spy + annotate boom | 500 on annotate failure; mutated rows; second DB open; missing history read |
| `..._page_and_history_share_snapshot` | TestClient + real DB + concurrent writer between page/history | silent desync that prevents consistent mint (expects ID present) |

Not source-regex tests. Not pure unit of a private helper in isolation for the happy path — the tip test goes through the real router.

**Limit (non-blocking):** failure-path DB is a TrackingDB stub (appropriate for fail-closed/open-once). Snapshot test asserts mint success, not an explicit “second read saw pre-write tip” row dump — still behavioral.

### A7 MUTATION CREDIBILITY — PASS by assertion-chain dependency (no live mutation)

Reviewer did not mutate.

1. **Tip-window gate dependency:** spy counts `annotation_calls == 1` after one default GET + three unsafe URLs + empty page. Removing `latest_page and offset == 0 and …` (builder mutation) makes unsafe URLs enter annotate → count 4 → assert fails. Dependency is real in the test as written.
2. **Fail-closed dependency:** annotate `side_effect=RuntimeError`; expects status 200 and unmodified rows. Narrowing `except Exception` to `except KeyError` would surface 500/error → test fails. Dependency is real.

---

## PART B — Whole-candidate final inspection

### B1 CP-1 CLEARANCE INTACT — PASS

`git diff --numstat` vs pin for the five tracked CP-1 files:

```text
461  13  tests/test_tui_gateway_server.py
2    1   tui_gateway/compute_host.py
52   14  tui_gateway/methods_prompt.py
9    8   tui_gateway/methods_session.py
13   1   tui_gateway/server.py
```

**= 537 insertions / 37 deletions** — matches CP-1 clearance/completion receipts. CP-2 numstat only:

```text
37  5  hermes_cli/web_routers/sessions.py
159 0  tests/hermes_cli/test_web_server.py
```

Name-only set is exactly the eight owned sources/tests (+ untracked `tui_gateway/rewind_identity.py`, `.ai/`). No evidence CP-2 edited across the CP-1 boundary.

### B2 DESTRUCTIVE SAFETY ACROSS BOTH HALVES — PASS

**Gateway half (re-verified on current tree):** `methods_prompt.py:171-341`

- Dual target → 4004 before resolve/write
- Targetless confirm flags → 4004
- ID without `confirm_truncate` → 4029
- Ordinal without `confirm_truncate` → 4029
- Unresolvable ID → 4018 (flags do not rescue)
- Empty retained prefix: ID path requires `confirm_delete_entire_transcript`; ordinal path requires `confirm_empty_truncate`; flags do not cross-arm (`:257-264`)
- Busy + any target/confirm → 4009 before queue (`:141-153`)
- `replace_messages` only inside confirmed-target block after empty gate; on failure 5008 and no memory assign

**No sequence reaches a transcript-emptying write without the dedicated consent flag for that arm.**

**Cross-half REST ID → submit replay (highest-value check):**

1. Non-tip / partial / failed REST windows mint **no** `rewind_id` keys → nothing to replay into `truncate_before_message_id`.
2. Tip-window IDs embed ordinal + 24-hex prefix hash over complete sanitized model history (`rewind_identity.py:77-83,153-171`).
3. Submit resolves **only** via `resolve_rewind_ordinal(session["history"], id)` (`methods_prompt.py:206-210`). Stale/shifted/wrong-history IDs → `None` → 4018; no write.
4. Even a still-valid ID still requires `confirm_truncate` (+ dedicated empty flag for ordinal 0). ID presence is not consent.
5. REST page vs live in-memory history divergence fails closed at resolve rather than cutting the wrong turn by position.

**REST-minted ID from a stale/partial window cannot be produced by this producer; a tip-minted ID cannot cut a wrong turn without exact current occurrence identity and explicit confirms.**

### B3 CARRIED FINDINGS — dispositions

#### F-CP1-1 — STILL OPEN — MEDIUM product residual — NON-BLOCKING

- **Evidence:** `methods_prompt.py:122-129` still gates skill expansion on `truncate_user_ordinal is not None` only; ID-only rewind skips `_expand_skill_invocation_for_replay`.
- **Violated requirement:** none of A1–A5; ordinal/ID UX parity residual.
- **Severity:** MEDIUM product / NON-BLOCKING for destructive safety.
- **Root cause:** ID target added beside ordinal without extending the expansion predicate.
- **Post-fix invariant:** expand when either target is present and text is str.
- **Options:** (a) one-line predicate fix; (b) document deferral until Desktop ships ID restore for skill turns.
- **Recommendation:** (a) fast-follow before Desktop ID rewind on skill turns.
- **Exact regression test:** skill-scaffold user turn; submit its `rewind_id` + confirms; assert expanded body, not short invocation.
- **Confidence:** 0.9
- **Unknowns:** whether Desktop will send display invocation text or already-expanded content on ID restore.

#### F-CP1-2 — STILL OPEN — LOW residual evidence pressure — NON-BLOCKING

- Resume/create/compress stubs largely use identical display/model lists; divergence is forced in `session.history` (empty model → all null) and pure spine tests.
- Not a production defect. Optional fast-follow stronger dual-projection E2E.

#### COO-1 — CP1-PRODUCTION-PROBE.txt is 0 bytes — RECORD DEFECT, claim independently true — NON-BLOCKING

- **Evidence:** `C:\hrp\.ai\builds\rewind-port-0200\CP1-PRODUCTION-PROBE.txt` size 0; early `CP1-RECEIPT.md` claimed a production probe under that name.
- **Underlying claim:** production call paths / host egress annotation are load-bearing. **Independently true now:** registered-path producer tests + `compute_host.py:741-742` annotation + host-frame resolution oracle; this inspection re-read those sites and re-ran the suite (550 gateway tests inside the combined 694).
- **Empty artifact matter?** Yes for **evidence-chain hygiene** of the early receipt epoch; **no** for candidate code correctness after CP1-COMPLETION-RECEIPT closed the obligations with real tests and this inspection re-verified them.
- **Disposition:** process/record residual; do not HOLD the candidate on the empty file alone.
- **Confidence:** 0.85

### B4 NON-REGRESSION — PASS

| Invariant | Evidence |
|---|---|
| write-before-memory | `methods_prompt.py:302-341`: `replace_messages` then `session["history"]=truncated`; exception → 5008, no assign |
| `active_only=True, archive_dropped=True` | `:319-324` exact kwargs |
| `model_user_indices` exact predicate | `rewind_identity.py:60-65`; submit calls it `:241-243` (no sibling listcomp) |
| `_display_text` dual coercion | `rewind_identity.py:86-90` (`text` then `content`) |

### B5 SUPPLY CHAIN / HYGIENE — PASS

- Diff name set: seven tracked paths + untracked `tui_gateway/rewind_identity.py` + `.ai/` evidence. No desktop, no `hermes_state.py`, no schema/migration, no config version, no `pyproject.toml`/`package.json`/`uv.lock`.
- Secrets scan on unified diff: no credentials/tokens/private keys (only incidental test token-clearing stubs / pre-existing `tokens = _set_session_context`).
- No new dependency, network egress, elevated permission, or forbidden live-install path.
- CP-2 net ~196 lines under 260/340; whole candidate tracked 733/42 + 171 new helper under soft/hard CP-1 budgets when counted with prior clearance.

### B6 INERT-SHIP — PASS

| New helper / surface | Named non-test production caller |
|---|---|
| `tui_gateway.rewind_identity.annotate_rewind_ids` | `server._history_to_client_messages` (`server.py:7234-7239`); REST `get_session_messages` (`sessions.py:648-651`) |
| `resolve_rewind_ordinal` | `methods_prompt.py:206-210` submit ID path |
| `model_user_indices` | `methods_prompt.py:241-243` |
| `_history_to_client_messages` | create/resume/history/compress/branch/live payload/compute_host ack sites (methods_session, server, compute_host — inventory in cp1-review K7, re-checked via diff) |
| REST tip-window block | `get_session_messages` itself is the mounted manage-router handler (`sessions.py:604+`), included from `web_server` manage mount |

No orphan helper ships without a production caller.

### B7 OPEN GAPS (map UNKNOWN=AFFECTED) — follow-up vs must-close

| Map item | Status after this candidate | Must-close before use? |
|---|---|---|
| Spec line-by-line extraction | CLOSED by `CP1-SPEC-EXTRACTION.md` (trusted + code-mapped; not fully re-`git show`'d this pass) | No |
| Compute-host frames | CLOSED for owned ack transcript annotation | No |
| Desktop omit_messages / Restore field config | OPEN product integration; Desktop types still lack `rewind_id`; rewind client still ordinal (`CP2-RECEIPT` claim consistent with no desktop diff) | **No for backend candidate**; **Yes before Desktop can submit REST-minted IDs** |
| Gateway truncate suite inventory | Exercised via expanded confirm matrix tests | No |
| Non-message REST feeders | CLOSED for CP-2 scope: other `messages` sites in sessions router are stats/export, not Restore transcript replacement (receipt + file structure); no second REST mint site found in owned router | No for this ship |
| Profile DB equivalence | CLOSED for CP-2: one `read_only` open, page+history on that handle | No |
| Live/cold/watch/eager/host matrix | CP-1 producer tests cover registered branches; field Desktop matrix still OPEN evidence | Follow-up |
| Structured/skill content divergence | Coercion exists; F-CP1-1 skill **expansion** still open | Follow-up (MEDIUM UX) |
| Schema/storage requirement | None introduced | N/A |

**Nothing remaining is a backend PRE-USE blocker for gateway ID rewind + REST tip-window minting under the map.** Desktop consumption of REST IDs is intentionally out of ownership and remains a product follow-up.

### B8 INDEPENDENT TEST RUN — PASS

Exact command:

`C:\hrp\venv\Scripts\python.exe -m pytest tests/test_tui_gateway_server.py tests/hermes_cli/test_web_server.py -q`

**Observed: `694 passed, 4 skipped, 1 warning in 54.55s` (exit 0).**

Matches builder claim (694 passed, 4 skipped). Focused CP-2 rewind trio: `3 passed, 145 deselected`.

---

## Bound findings (this inspection)

### F-FINAL-1 (= F-CP1-1 carried) — NON-BLOCKING MEDIUM
See B3.

### F-FINAL-2 (= F-CP1-2 carried) — NON-BLOCKING LOW
See B3.

### F-FINAL-3 (= COO-1) — NON-BLOCKING record hygiene
Empty `CP1-PRODUCTION-PROBE.txt`; underlying production-path claim re-verified true by code + pytest. See B3.

### No new BLOCKING findings.

---

## Checklist scorecard

| ID | Result |
|---|---|
| A1 safe-window | PASS |
| A2 fail-closed | PASS |
| A3 RF1 snapshot/handle | PASS |
| A4 contract reuse | PASS |
| A5 resolvability | PASS |
| A6 tests real | PASS |
| A7 mutation dependency | PASS (by chain) |
| B1 CP-1 intact | PASS |
| B2 cross-half destructive safety | PASS |
| B3 carried findings dispositioned | PASS |
| B4 non-regression | PASS |
| B5 hygiene | PASS |
| B6 inert-ship | PASS |
| B7 open gaps classified | PASS |
| B8 pytest 694/4 | PASS (observed) |

---

## Disagreements (explicit)

1. **Disagree with treating CP1/CP2 receipts as evidence** — re-verified in code and by running tests.
2. **Agree with builder A1–A7 direction** on tip-window, fail-closed, import reuse, and resolution-backed tests.
3. **Disagree lightly with over-crediting “shared handle” alone for RF1** — correctness requires read_only pool bypass + BEGIN on `_conn`; that combination is what the code actually does.
4. **Agree F-CP1-1 remains open and MEDIUM** — not a reason to HOLD the safety candidate.
5. **Disagree that an empty production-probe file is a code HOLD** — it is an evidence-chain defect; the claim was closed by later completion work and re-verified here.
6. **Agree Desktop REST ID consumption is outside this candidate** and must not be silently marked done.

---

## ONE item least sure of

Whether every deployed `SessionDB(read_only=True)` topology permanently keeps `_checkout_read_conn → None` (today: gated on `self.read_only`). If a future SessionDB change enabled pooled readers for RO instances without updating this router, the BEGIN-on-`_conn` snapshot would silently stop covering the real SELECTs. Current code and the concurrent-writer test protect today's tree; the coupling is implicit. Confidence on today's correctness: ~0.9; residual is future-coupling, not a present defect.

---

## Unexamined → OPEN (not pass)

- Full byte-level re-read of all six historical spec commits (extraction doc exists; not re-done end-to-end this pass).
- Desktop Restore click path / TypeScript field wiring (forbidden desktop edit; not a backend gate).
- Live mutation of tip-gate/fail-closed (forbidden; dependency proven by assertion chains instead).
- Non-`sessions.py` HTTP surfaces outside the owned router for exotic Restore feeders (stats/export checked at receipt level; no full ASGI route census this pass).
- Multi-process writers against the same state.db under exotic NFS/non-WAL fallback beyond the concurrent unit test.

---

## Verdict

Whole-candidate final inspection complete enough to judge CP-2 and the combined eight-file candidate against the binding map. No BLOCKING defect found. Residual items are product/evidence follow-ups, not safety HOLDs.

SCAN_COMPLETE:YES INSPECTION_PASS
