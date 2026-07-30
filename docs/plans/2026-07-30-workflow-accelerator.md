# Workflow Accelerator implementation decision

## Boundary

This is post-release implementation work.  SakaanAgentUsage release terminal
evidence is immutable at
`C:/Users/HieuKa/AppData/Local/hermes/cache/SakaanAgentUsage-admission-receipts/final-1d872d4-release-terminal.json`.
This branch must not modify live `HERMES_HOME`, `config.yaml`, services,
scheduled tasks, or shared branches.

## Decision: extend the receipt-bound async lifecycle

| Field | Record |
| --- | --- |
| Scope | Add a canonical environment/evidence identity, atomic async binding, candidate validation reuse, scoped preflight, alert correlation, timing/wait telemetry, terminal cleanup eligibility, and durable decision records to the existing release-review ledger. |
| Rationale | Existing receipts prevent same-lens duplication, but omit environment/evidence identity, have a post-dispatch attachment race, do not distinguish active work from waits, and have no lifecycle cleanup or decision enforcement. |
| Owner | Codex is the sole builder on `feat/workflow-accelerator-20260730`; two independent Codex reviews audit design and the later exact diff. |
| Consultations | Hermes is unavailable because of recorded provider overage; Claude Code is unavailable because its client is not logged in. Neither is rerun with the unchanged failure fingerprint. Codex review one approved an async-only increment; Codex review two found the dispatch/attachment race and required atomic binding before a worker runs. |
| Reconciliation | Implement the async rail first. It has a real tracked production dispatcher and temp-`HERMES_HOME` test seam. Direct-shell integration is deferred because no tracked production caller exists; no unrelated subprocess call will be routed through the ledger. |
| Safety boundary | Every reuse requires exact candidate, environment, command/scope, lane/lens, prompt, and immutable-evidence identities. No reusable secret, auth response, live target, PID after restart, stale capacity, or unverified artifact is accepted. Unknown owner state terminalizes without rerun. |
| Acceptance | A temporary-HERMES_HOME test proves exact reuse, changed identity misses, atomic dispatch binding, stale-owner terminal state, distinct active versus wait timing, deduplicated alert, terminal cleanup eligibility, and decision-record enforcement. |
| Timing | `post-release` implementation. Integration/restart/deployment remains separately gated after candidate review and temporary-home proof. |

## Component map

1. `tools/release_review_ledger.py` owns normalized environment/evidence
   fingerprints, lease/binding state, receipts, timings, alert correlation,
   cleanup eligibility, and decision records.
2. `tools/release_review_launch.py` consumes the canonical identity and binds
   an async delegation before it can execute.
3. `tools/async_delegation.py` supplies real async lifecycle events; it must
   not accept caller-controlled ledger paths or attach a completed receipt.
4. `tests/tools/test_release_review_ledger.py` exercises pure lifecycle
   invariants.  An end-to-end temporary-home test covers the real async path.
5. `scripts/run_tests.sh` preserves the Windows profile discovery variables
   and forces UTF-8 inside its hermetic runner. This is part of the candidate:
   without it, the real async-suite import path cannot resolve a home on
   Windows and the runner can fail after otherwise-passing results.
6. `tools/delegate_tool.py` rejects a same-candidate, same-model, same-lens
   review before child construction. Review batches declare `review_lens` and
   `candidate_hash` when inference is insufficient. Distinct lenses may run
   together; the receipt reports model reuse honestly rather than calling it
   cross-model independence. Fleet lane selection remains a separate runtime
   routing seam and must not be simulated by changing a prompt label.
7. Dispatch and live-transcript status payloads lead with the effective model,
   role, and review lens. `deleg_*` remains a durable trace/correlation ID,
   never the primary human-facing worker name. When qualified distinct lanes
   are available, the future routing seam must prefer model diversity; when
   one model is the only available lane, distinct lenses may be consolidated
   but must not be presented as independent models.

## Explicit deferrals

- No generic terminal or gateway interception.
- No global stale-monitor or force-finalization policy for ordinary async
  delegation. Receipt-bound review timeboxing remains scoped to its own
  durable receipt and is the only terminal control in this candidate.
- No direct-shell production integration without a designated caller.
- No provider capacity policy, secrets, live health response reuse, service
  mutation, or stale process/PID recovery.
