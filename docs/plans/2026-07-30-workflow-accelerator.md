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

## Proposed decision: evidence-carrying retry ownership and Deep Mode

This user-requested proposal is `post-release` Accelerator work. It is recorded
before implementation and is awaiting the requested Hermes and Claude Code
consultations; their availability, conditions, and reconciliation will be
appended to this decision before the implementation is frozen.

| Field | Record |
| --- | --- |
| Scope | Classify failed build/review/CI/operational attempts; assign distinct retry owners; preserve a redacted cached evidence packet; and escalate to bounded Deep Mode investigation/reconciliation. |
| Rationale | Later attempts should verify and extend prior evidence, not rediscover the same failure. Deep investigation should add genuinely independent lenses without duplicate same-model work. |
| Owner | Hermes dispatches and reconciles Deep Mode when callable; one designated builder mutates the isolated candidate; Codex records receipts and presents status. |
| Safety boundary | No same effective provider/endpoint/account/model is assigned the identical task concurrently. Parallel Deep lanes use distinct lenses and effective route identities. One writer applies a reconciliation verdict. No live/runtime/release mutation is included. |
| Acceptance | Each retry stores/receives the prior packet hash; unchanged attempts cannot consume extra budget; alias routes collapse; a contamination exception is evidence-bound; and every Deep verdict records inputs, disagreements, owner, remedy, and verification command. |
| Timing | Post-release implementation; it cannot alter the current frozen release candidate. |

### Shared recovery packet

Each failure creates a redacted immutable packet keyed by candidate, normalized
scope, environment, and failure fingerprint. It contains receipt IDs/hashes,
the exact command, failure set, relevant tool/environment versions, prior
findings, attempted diff/remedy, and test references. A later owner receives
this packet and validates it before working. A new from-zero investigation is
allowed only after a recorded packet-integrity failure, candidate mismatch, or
failure-fingerprint mismatch. Raw credentials, authenticated responses, and
model reasoning are excluded.

### STANDARD mode

`STANDARD 1/3` assigns one strong qualified owner to investigate and try the
smallest remedy. `STANDARD 2/3` assigns a different effective lane, carries
the first packet forward, validates and extends its evidence, and avoids
unchanged reruns. `STANDARD 3/3` assigns a third effective lane and continues
from both packets. If distinct callable lanes are unavailable, the receipt
records `DEGRADED_ROUTE_CAPACITY`; labels or aliases never count as distinct.
Only one standard owner writes at a time.

### DEEP mode

Deep Mode begins only after all three standard attempts fail with a remaining
material fingerprint. `DEEP MODE 1/3` runs 2–3 distinct-lens lanes; `2/3` runs
3–4; `3/3` uses every available qualified lane within configured concurrency
and usage budgets. Those lanes perform blind hypothesis checks using the same
packet, rather than copying a proposed fix. Hermes reconciles structured
findings and disagreement into one verdict. A single designated writer then
applies any selected bounded fix and records its acceptance test. Insufficient
lane capacity is recorded accurately, never manufactured as a quorum.

### Runtime seams for implementation

- `tools/async_delegation.py` retains runtime lease ownership and stale-owner
  recovery.
- `ReleaseReviewLedger.record_alert` stores the durable failure fingerprint;
  `admit_validation`/`finalize_validation` provide exact successful evidence
  reuse; and `record_decision` stores retry/deep-reconciliation ownership.
- The review receipt remains the immutable no-reroll boundary. A changed
  candidate, environment, scope, or evidence fingerprint creates a successor
  packet linked to its predecessor; unchanged evidence is reused or rejected.

### Consultation reconciliation and mandatory implementation conditions

Governance was revalidated before this reconciliation (73,111 bytes,
SHA-256 `629f23ff6a629f602a3ffc035eb8d33c7364a797e73d5b684214c7758e8eb3f5`).
Hermes was **UNAVAILABLE**: its one-shot returned the prior HTTP 400
extra-usage/provider-overage error before a technical verdict. Claude Code was
**NO_VERDICT/UNAVAILABLE**: its tool-disabled one-shot exited successfully but
returned no APPROVE, CONDITIONAL, HOLD, or protocol analysis. Neither outcome
is approval and this consultation fingerprint must not be retried unchanged.

The following coordinator-audit conditions are adopted for the next safe,
isolated Accelerator slice:

1. An attempt identity is `candidate_hash + environment_fingerprint +
   normalized_scope_hash + failure_fingerprint + mode + ordinal/generation`.
   The ledger grants one writer lease with a monotonic fence; a successor starts
   only after the predecessor is terminal/interrupted, and late prior-generation
   writes are rejected.
2. Handoff packets are immutable and versioned. They retain predecessor
   packet/receipt hashes; all identity components; exact reproducer and failed
   set; relevant versions; attempted remedy/diff hash; verified facts with
   evidence references; unresolved questions; quarantined evidence and reason;
   and redaction attestation. They exclude secrets, authenticated responses,
   raw reasoning, live PIDs, and capacity claims as reusable facts.
3. Later STANDARD owners verify packet integrity and reproduce the current
   fingerprint before adopting its conclusions. From-zero work requires a
   recorded mismatch, tamper, or contamination reason; quarantined evidence is
   preserved rather than deleted.
4. Blind Deep lanes receive common verified facts but not prior remedy
   hypotheses until reconciliation. Each declares a distinct lens/coverage
   target; canonical effective route identity **and** lens are admission keys.
5. Actual Deep fan-out is the minimum of requested range, distinct qualified
   routes/lenses, configured concurrency, and token/time budget. Insufficient
   capacity records `DEGRADED_ROUTE_CAPACITY`; a criticality policy decides
   proceed versus HOLD, and a diminishing-return cutoff ends lanes that add no
   new evidence.
6. Duplicate suppression rejects concurrent **and sequential** same
   candidate/environment/failure/normalized-task/lens/effective-route work
   unless evidence generation changed. Distinct labels do not excuse materially
   overlapping hypothesis/locus coverage.
7. Hermes, when callable, produces a reconciliation decision receipt only; it
   does not mutate. If unavailable, the coordinator reconciles transparently.
   One fenced writer applies the selected bounded remedy and records rejected
   alternatives and disagreements.
8. Crash/cancel recovery terminalizes orphaned leases as interrupted/unknown,
   freezes the packet, rejects late writes, and requires exact packet hash plus
   a fresh fence to resume. PIDs/start times are liveness evidence only.
9. Progress requires a failed-set reduction, acceptance change, changed
   fingerprint, new reproduced root-cause evidence, or verified intentional
   artifact change. After `DEEP MODE 3/3` failure the workflow stops and
   reports; it also stops on external wait, unapproved scope expansion, packet
   mismatch, or unsafe evidence.
10. Immutable release receipts remain retained. Cached summaries are concise,
    structured, hash-bound, and point to on-demand raw logs. TTL applies only
    to environment/capacity/liveness; telemetry tracks active time/tokens,
    external wait, cache-hit tokens avoided, duplicate calls, time-to-new-
    evidence, and finding yield per lane.

### Required acceptance proof

Focused tests must cover: three serial distinct-route STANDARD handoffs without
overlap; alias collapse; cross-process lease races; stale-generation late-result
rejection; crash/cancel resume; packet tamper/quarantine; candidate/environment/
failure invalidation; sequential duplicate rejection; blind-hypothesis
isolation; Deep min/max and unavailable capacity; degraded same-model N-gate
rejection; false-PROGRESS rejection for unchanged evidence; and the mandatory
`DEEP MODE 3/3` terminal stop. A benchmark must compare cold restart with packet
reuse and demonstrate lower median active time/tokens with equal-or-better
reproduced-finding and false-pass rates.

### Deterministic state-transition clarifications

The original failure is baseline evidence and consumes no retry slot. The only
executable standard sequence is `STANDARD 1/3 → 2/3 → 3/3`; failure of the
third standard attempt transitions once to `DEEP MODE 1/3`. Deep transitions
only `1/3 → 2/3 → 3/3`, and failure of Deep 3 emits terminal
`STOP_AND_REPORT`; no fresh automatic cycle is legal. A deduplicated launch
does not consume retry or usage budget. An executed retry that repeats the
failure is still counted at its ordinal but is marked `DEAD_LOOP` with prior
and current evidence hashes.

Each transition is atomic and records mode, ordinal/generation, predecessor,
logical owner, effective route identity, reason, packet hash, fence token,
start/end time, and terminal outcome. Before allocating a worker or budget, a
successor verifies the packet's hash, redaction/schema version, candidate,
scope, environment/runner, evidence, and failure fingerprint. Any mismatch,
stale edit, changed command or route, expiry, tamper, or redaction violation
creates a linked successor receipt with an invalidation reason; it never
silently starts clean. Only passed exact validation can be reused.

The inherited packet is rendered as a point-in-time snapshot, so the new owner
must re-verify its source facts when they may be stale. A restart between
packet persistence and successor launch resumes from that packet or
terminalizes unknown ownership; it never invents a completed handoff.

### Superseding consultation evidence: bounded first-party CLI routes

The prior unavailable/no-verdict consultation outcomes remain historical. A
changed invocation fingerprint fixed the communication path without changing
Hermes configuration or runtime: Hermes is explicitly consulted through
`--ignore-rules --provider openai-codex --model gpt-5.6-sol --oneshot`, while
Claude Code is invoked in safe, tool-disabled, no-session-persistence plan
mode. Both wrappers fail closed on exit-zero/no-verdict. The repo-owned
equivalent is `scripts/fleet_consult_cli.ps1`; it is a bounded consultation
contract, not a provider or credential configuration mechanism.

Both lanes returned **CONDITIONAL** verdicts under this route. The mandatory
conditions above therefore include: PREPARED→DISPATCHED→ACCEPTED→RUNNING→
RESULT_COMMITTED execution states; attempt consumption only after accepted
execution proof; CAS epoch/fenced coordinator, mutation, and terminal leases;
authoritative expiry; independently fresh reproduction; quarantined cancelled
results; atomic mutation journal/rollback; capacity holds and reserved recovery
capacity; blind findings committed before hypothesis reveal; exclusive terminal
states; route identity version/tool-config granularity; concrete TTL/hash
invalidation; per-attempt/lane/phase/global budgets with
`BUDGET_EXCEEDED→STOP_AND_REPORT`; and a cheap inherited-fix check before full
final regression. Reconciliation records contrary findings, rejected
alternatives, and immutable rationale. The benchmark compares continuation with
cold restart for token/time and defect-escape/fix-correctness.

### Implemented retry execution boundary

The isolated candidate now implements this protocol on the receipt-bound async
review rail: Standard attempts are serial and require the prior failed packet
plus a distinct effective route; Deep capacity is admitted before launch with
route/lens, concurrency, and token-budget evidence; material reviews cannot
fall back to generic batch dispatch; and candidate, effective route, recovery
attempt, and fence are persisted with the submission outbox.  Cancellation,
worker completion, submit-unknown, and abandoned-owner recovery terminalize
only the current fence.  After every admitted Deep 3 lane fails, the ledger
records `STOP_AND_REPORT` and rejects an automatic new cycle.  This remains
post-release candidate work only until its own acceptance and release gates
complete.

### Decision: hosted Windows cold-source diagnostic architecture

| Field | Record |
| --- | --- |
| Scope | New isolated diagnostic candidate for the hosted Windows `Add-Type -Path ... -OutputAssembly` failure at frozen `74a5be017`. It adds observable, fail-closed evidence around the actual cold source-compilation boundary without changing the current production 29-second contract or accepting cache/prebuilt output as proof. |
| Rationale | Deep 2 resolved the `C:\\tmp` fixture defect, but run `30544647183` still exceeded both the 29-second production preparation limit and the 45-second owned cold-test bound on GitHub-hosted Windows. The cause remains unattributed. |
| Owner | Codex is the sole writer. Hermes and Claude Code are independent, tool-free architecture reviewers; neither may mutate. |
| Safety and quality boundary | Preserve an independently mandatory cold source proof, exact owned cleanup, and the separate 29-second production gate. No timeout increase, cache-only/prebuilt proof, custom runner, global serialization, scanner weakening, secret/path leakage, bridge/main/live/config/restart/install/deploy mutation, or automatic retry-cycle continuation. |
| Acceptance criterion | Hosted Windows records a sanitized, candidate-bound outcome for the actual source compile; a fresh source-to-assembly identity proof cannot be satisfied by reuse; the unchanged 29-second production lane remains independently proven; incomplete telemetry, timeout, or ambiguous cleanup fails closed. |
| Placement | New user-authorized diagnostic scope after terminal Deep 3; it is neither `DEEP MODE 4/3` nor a retry-budget continuation. |

#### Consultation reconciliation (2026-07-30)

The immutable inputs are Deep 1, 2, and 3 evidence packets and the Deep 3
reconciliation for candidate `74a5be0173b77b5e79fd304cfc7fc2be4c3b3151`.
Hermes returned **CONDITIONAL**: it approves two independent gates (unchanged
29-second production preparation and a fresh 45-second cold source proof) plus
an external, allowlisted observer that is attached before the root runs and
can prove exact owned cleanup. Claude Code returned **CONDITIONAL**: it approves
strictly non-perturbing query-only diagnostic evidence and treats an
inconclusive result as an honest terminal diagnostic outcome. Both reject
timeout inflation, cache/prebuilt substitution, skipped/advisory CI, runner
substitution, global serialization, scanner weakening, and secret/path capture.

The reconciled implementation order is deliberately narrow:

1. Map the existing real paths before editing: `apps/desktop/scripts/windows-verifier-job-host.ps1`,
   `apps/desktop/scripts/windows-verifier-job-host.node-test.mjs`,
   `apps/desktop/scripts/desktop-verifier-lib.mjs`, and `.github/workflows/js-tests.yml`.
2. Add regression-first, nonsecret start/end phase evidence around the existing
   `Add-Type` call and a deterministic source/output identity check. A missing
   completion marker, invalid identity, or failed cleanup remains failure.
3. Add a job-scoped diagnostic observer only if it can attach before execution,
   account for the complete owned process tree, and remain observational. A
   named-handle or sidecar design that cannot prove those properties is rejected.
4. Keep the current production 29-second lane byte/argument equivalent and run
   a separate fresh-source cold proof under the existing 45-second bound. The
   diagnostic result cannot turn either failed gate into a pass.
5. Upload only sanitized failure evidence, then require fresh exact-diff review
   before one candidate-bound CI run.

Disagreement is recorded: Hermes proposed a stronger suspended-process Job
Object observer; Claude proposed a query-only observer. The latter is only
admissible if actual repository handles make it observable without weakening
ownership proof. The first implementation slice therefore starts with common
phase/identity evidence and rejects either observer until its process-ownership
contract is tested.

#### First implementation slice and measured boundary

The first slice changes only the existing test bootstrap and its Node test
harness. In diagnostic mode, `windows-verifier-job-host.ps1` now emits a
source-hash-bound `compile_start` marker before the real `Add-Type` call and a
source/output-hash-bound `compile_end` marker after it returns. The test harness
forwards only allowlisted marker forms whose source hash matches the checked-in
`windows-verifier-job-host.cs`; raw stderr, paths, and forged hash-shaped lines
are excluded. Fresh fixture coverage proves a cache-empty compile produces the
reported output hash, while timeout coverage proves only verified nonsecret
markers survive a failure. The existing 29-second production path remains
unchanged.

This creates a truthful next hosted-Windows observation: `compile_start` with
no `compile_end` means the actual source boundary did not return; both markers
bind a completed source-to-output result. It deliberately does not claim full
compiler-descendant attribution. A Job observer is deferred until a testable
pre-execution whole-tree ownership contract exists in the tracked harness.

#### Paired production-contract falsification — current diagnostic scope

**Scope:** one diagnostic-only paired invocation of the real
`prepareWindowsJobHost` contract under a short and a long, explicitly
controlled nonsecret `LOCALAPPDATA` root. **Rationale:** CI run `30553768551`
proved a direct short-root cold compile can finish while the existing
production preparation path times out, but those paths previously differed in
both contract and environment. **Owner:** one Codex writer. **Safety and
quality boundary:** source, command, cache-key inputs, 29-second bound, owned
cleanup, working directory, and all environment inputs other than
`LOCALAPPDATA` stay identical; the pre-existing default production test remains
separate. Diagnostics expose only a checked-in source hash and a completed
output hash, never roots, arguments, or raw stderr. No production cache-root
change, timeout increase, suppression, skip, bridge/main/live/config/restart/
install/deploy change is admitted. **Acceptance:** each root has a clean cache,
the real production preparation either returns source-bound compile evidence or
fails inside its unchanged bound with only sanitized evidence, and the pairing
can be classified without treating a diagnostic observation as a production
pass. **Placement:** isolated new diagnostic-architecture candidate, not a
retry-budget continuation.

The paired fixture must never accept an arbitrary long-root error: a failed
observation is valid only when the bootstrap emits the source-bound,
nonsecret `precompile_failure ... class=cache_root_unavailable` marker before
the actual compile boundary. The default flag-unset `prepareWindowsJobHost`
return remains `undefined`, and launch-spec isolation strips the opt-in
diagnostic flag from inherited environment/configuration routes. The
pre-existing cache-entry/directory-creation operation is retained from
`2570e711`; this slice adds only a narrow catch that classifies its existing
root-unavailable exception without forwarding the exception text. Therefore a
controlled-root failure is documented as a pre-existing cache-root limitation
to diagnose, not a behavior introduced or resolved by the Accelerator
candidate.

Changed-scope review tightened this slice further: the test compares the full
effective environment maps and permits exactly `LOCALAPPDATA` to differ, while
a deterministic nonzero-preparer regression proves the public failure summary
admits only the matching source-bound `precompile_failure` marker and excludes
forged hashes and path-like stderr. These are diagnostic integrity checks, not
new runtime policy.

### Baseline exclusions bound to Sakaan fork `556f5a56`

The broader `tests/tools/test_delegate.py` suite has three inherited failures,
reproduced unchanged on exact `sakaan/main=556f5a5649bcb67535b9b8a7a382d5c6f49e97fe`
with `uv run pytest` targeting each test: missing
`tools.child_runtime_registry` import, batch child-cost rollup, and single-child
cost rollup. Their failing paths are the registry import and parent telemetry
assertions; none reference async outbox identity, `recovery_attempt_id`,
material launch adapters, or the new SQLite trigger. They are retained as
baseline exclusions, never reclassified as passing evidence for this candidate.

### Material-review admission decision — current Accelerator candidate

**Scope:** candidate-bound material review ingress, retry ownership, and
independence gates. **Rationale:** the public generic delegation API does not
have the receipt, recovery packet, fenced attempt, or preflight identity needed
to prove safe material review admission. **Owner:** the durable dispatcher and
ledger; exactly one fenced writer applies a resulting remedy. **Safety and
quality boundary:** a material request bearing `candidate_hash` or
`review_lens` fails closed before generic child or batch construction. It can
run only through the private receipt-bound adapter; ordinary delegation remains
unchanged. **Acceptance:** the adapter persists a single outbox row before
acceptance, claims executor submission before `RUNNING`, retains an unknown
outbox on activation/submission failure, and requires distinct normalized
review lenses plus distinct canonical effective routes at an N-review gate.
This is post-release Accelerator work, not a current-release mutation.

An unaccepted launch remains `PREPARED` and does not spend an ordinal or budget.
After durable persistence it becomes `ACCEPTED`; only an atomic executor claim
moves it to `RUNNING`. Any failure after that point preserves enough durable
state to terminalize the current fence as `INTERRUPTED`, so a process crash
cannot strand a retry lease. Regression coverage includes generic-ingress
rejection, unaccepted reuse, activation failure with retained unknown outbox,
two-connection identity mutation rejection, stale-fence completion, and
duplicate/alias/lens rejection.

### STANDARD 1/3 CI-repair decision — exact `a57c898b`

**Scope:** only the two candidate-caused fingerprints from exact CI run
`30523267994`: bounded Windows Job-host preparation and completion-envelope
identity/persistence. **Rationale:** 86 of 87 Python failure identities match
the completed `sakaan/main` run `30518252106`; the aggregate gate and those
baseline failures are excluded rather than repaired here. **Owner:** one Codex
writer. **Safety and quality boundary:** no workflow trigger, main, bridge,
runtime, config, install, restart, or live mutation; preparation remains
handle-scoped and a corrupt cache is a cold miss, never a usable cache.
**Acceptance:** a producer envelope is durable before enqueue and stable across
registry reconstruction; stale/failed persistence never enqueues; the Windows
cache passes cold, warm, corrupt, concurrent, and timeout coverage while the
controller receipt budget retains a margin.

Hermes was consulted once through the repo-owned wrapper and timed out without
a verdict; it will not be retried for this unchanged scope. Claude returned
**CONDITIONAL** and required a distinct preparation fingerprint, cache
atomicity, handle-only termination, monotonic fence behavior, and unchanged
baseline identities. The implementation binds preparation below the controller
deadline, derives reconstructed process stream identity from durable producer
facts, and adds isolated cache and enqueue-acknowledgement regression tests.

The Windows lifecycle delta review found and then cleared one teardown gap:
on preparation timeout the exact directly spawned preparer is now asked to
exit, and the caller waits for that owned handle's exit before reporting the
timeout. A bounded fail-closed fallback reports an unexited owned preparer;
there is no PID scan, process-name kill, or broad process-tree action. A real
child/sentinel regression proves the owned preparer exits while an unrelated
same-executable process survives. The async/recovery review separately
approved a bounded retry for only SQLite's expected schema-bootstrap lock;
malformed and unrelated database errors still raise. The two-process durable
capacity admission regression passes. These are current candidate repairs,
not changes to baseline exclusions or live runtime behavior.

### Fleet-bridge runtime integration decision — current Accelerator candidate

**Scope:** wire the verified six-file AGY/Grok bridge only through the
Accelerator's durable material-review path. The bridge must use the ledger's
canonical backing-route identity, a receipt-bound private ingress, durable
route-plan/degraded-capacity evidence, and owned external cancellation before
Claude or Antigravity work can be considered submitted. **Rationale:** the
frozen bridge has validated route planning but no safe material ingress; its
standalone `FleetService.run` path cannot prove candidate/scope/lens/fence
uniqueness or stop external CLI work. **Owner:** Codex is the single mutation
writer; Hermes and Claude are bounded design reviewers; the ledger/outbox owns
admission and terminal state. **Safety and quality boundary:** generic
`delegate_task` remains fail-closed for material work. No route, endpoint,
executable, PID, raw provider output, or credentials enter public receipts.
No main, runtime, configuration, restart, install, or deployment change is
allowed until this candidate has local and exact-CI evidence.

**Acceptance criterion:** one immutable identity binds provider/account/
endpoint/model/adapter evidence across route plan, receipt, outbox, and
fence; a candidate/environment/scope/lens/route/fence has only one durable
submission; cancellation before or after owned external handle binding cannot
leave an orphan or publish a late result; an unavailable/degraded roster is
candidate-bound and cannot satisfy an independence gate; ordinary delegation
is unchanged. **Placement:** current Accelerator candidate after its
foundation Windows gate; not a current Hermes runtime mutation.

#### Design consultation reconciliation (2026-07-31)

Claude returned **CONDITIONAL** for the bridge design. Its adopted conditions
are: exact identity enforcement at the private adapter boundary; retry attempt
and fence pinning; an idempotency key linking persisted route plan to outbox;
automatic termination of a spawned handle if binding fails; atomic
current-fence terminal publication; and a deny-by-default public receipt
schema. Hermes was launched through the first-party wrapper on the same
changed scope and remains pending at this record's creation; it will be
recorded once or as one bounded unavailable result, never duplicated.

Hermes subsequently returned **HOLD** on the same bounded design. The HOLD is
substantive and supersedes the provisional implementation scope: a
caller-supplied async `runner` could satisfy a route receipt without executing
the persisted assignment; external adapters have no durable owned process
handle; the Fleet store, review ledger, and async outbox are separate
databases without a reconciled saga; executable location must not define
backing-route independence; and the public completion path still carries raw
execution objects. The next implementation slice is therefore a sealed
material state machine, not a partial bridge launch: `PLANNED → OUTBOX_READY
→ STARTING → OWNED → RUNNING → FINALIZING → terminal`, keyed by material plan,
candidate/environment/scope/lens/route, attempt, and fence. It must persist a
recoverable plan/outbox saga, construct the runner internally, bind owned
external process identity before `RUNNING`, heartbeat the Fleet lease, and
construct public receipts from a recursive allowlist only. The bridge remains
isolated and cannot be published until those tests clear.

#### Owned external-material execution decision (2026-07-31)

**Scope:** material-only Claude Code and Antigravity execution after sealed
route-plan admission. **Rationale:** both live adapters currently use blocking
`subprocess.run`, which has neither a durable process identity nor a safe
current-fence cancellation path. **Owner:** one Codex writer implements a
restricted Fleet-service owned-execution seam; the durable outbox and ledger
remain the authority for admission and terminal publication. **Safety and
quality boundary:** ordinary adapter `execute()` and generic delegation remain
unchanged. Material execution uses direct `argv` only (`shell=False`), a
sanitized environment, opaque non-secret handle facts, PID-plus-start-time
verification, and the existing exact-owner process termination mechanism. It
never persistently records argv, prompts, executable paths, environment, raw
stdout/stderr, or credentials. **Acceptance:** a handle is durably bound in
both rails before `RUNNING`; stale fence or PID-start mismatch cannot cancel or
terminalize; cancellation versus completion releases exactly one lease and
publishes exactly one terminal state; crash recovery never relaunches an
ambiguous external process. **Placement:** current isolated Accelerator
candidate; no main or live Hermes mutation.

#### Sealed owned-external ingress implementation record (2026-08-01)

**Scope:** implement the approved material-only ingress from route plan to
owned external process and terminal receipt. **Rationale:** a durable route
plan alone was insufficient: a host crash before a child-handle bind could
otherwise leave an accepted retry or turn a late completion into a false pass.
**Owner:** one Codex writer. **Safety/quality boundary:** `FleetService.run`,
generic `delegate_task`, public material launcher calls, provider secrets, and
live Hermes remain unchanged. The only executable paths are Claude Code and
Antigravity argv invocations through the module-private ingress.

The implementation now creates one internal runner only after a receipt and
delegation id are durable; starts the external child with `shell=False` and a
sanitized environment; registers its opaque handle/PID/start identity; binds
the ledger and async outbox fence before consuming output; and releases the
Fleet lease after adapter completion. A handle-free crash terminalizes the
sealed saga as `UNKNOWN/INTERRUPTED` (or `CANCELLED`) rather than accepting
synthetic completion. The public async envelope is rebuilt from allowlisted
facts and excludes raw provider I/O.

**Measured acceptance so far:** a temporary-HERMES_HOME integration test
proves admission → dual bind → external completion → receipt/plan/retry
terminalization; raw stderr is absent from durable async state. The current
focused material/async/ledger/process test matrix passed 226 tests (16
skipped), compilation and diff checks passed. Remaining acceptance: explicit
cancel/owner-death race coverage for this ingress, recursive completion-value
validation, full Accelerator suite, changed-scope reviews, and exact CI.

**Correction (2026-08-01, sequence 17).** The paragraph above is superseded and
is retained only as history. Two independent Final Inspections returned FAIL /
RELEASE HOLD against it, and both were right on the substance:

- The 226-test figure is stale. The current focused seven-file matrix is 286
  passed / 16 skipped, and the canonical 28-file broad suite is 599 passed with
  one failure that is a harness precondition, not a product defect
  (`test_long_worktree_path_does_not_inflate_tr` asserts a >60-character or
  `worktrees` repository root; the candidate root is 53 characters). The same
  file passes 25/25 from a 69-character root.
- "Binds the ledger and async outbox fence before consuming output" understated
  the requirement. Binding happened only *after* the child existed, so a
  failure between child creation and binding left a live unowned child and an
  unreleased Fleet lease, and the Antigravity prompt reached an executable argv
  before any durable rail owned it.
- The ingress was not reachable from shipped code: it had zero non-test
  callers, so the rail described here was effectively dead.

#### Sequence-17 repair record (2026-08-01)

**Owner:** one Claude Opus 5 writer, bounded to eighteen leased paths.

1. **Pre-execution durable ownership.** Both rails now bind an opaque
   provisional handle *before* any provider argv can be constructed
   (`bind_material_provisional_handle` on the review ledger and the async
   outbox; saga state `SEALED → STARTING → OWNED`). A crash inside that window
   leaves a PID-free saga that recovery terminalizes, and a direct
   `SEALED → OWNED` transition is now rejected, so an owned PID can never be
   the first durable trace of external work.
2. **Exactly-one lease finalization, structurally.** Registration, both binds,
   `run.finish()` and completion bookkeeping all run inside cleanup scope, and
   a `try/finally` net around the whole owned run guarantees the Fleet lease is
   released exactly once even on an unenumerated escape. Every post-creation
   failure terminates that exact child — registry PID-plus-start-time path
   first, held process handle as fallback, and no PID scan or process-tree
   sweep. Cancellation arriving between child creation and registration is no
   longer lost.
3. **Real production composition root.** `dispatch_material_review()` in
   `tools/fleet_delegation.py` resolves the canonical ledger, builds the live
   fleet service through `build_fleet_service()`, plans the single route and
   calls the sealed ingress. Generic `delegate_task` and the public async
   launcher stay fail-closed for material work.
4. **Ordinary capacity policy restored.** The parent `evaluate_lane` rule that
   promotes stale, absent or non-comparable capacity evidence into hard reasons
   for verified-health task workers is restored verbatim. It had been deleted
   to satisfy a test fixture; the fixture was repaired instead. This is
   fleet-wide ordinary routing, not a bridge-local concern.
5. **Truthful model evidence.** Antigravity's
   `Propagating selected model override to backend` line is written by the
   client before the request leaves the host, so it proves the
   *requested/selected* model, not a served identity. The owned-material proof
   now carries no bare `served_model_id`/`served_model_label` at all and
   publishes `model_evidence_kind=requested_selected_propagation`,
   `served_model_proven=false`, `served_model_evidence=NOT_PROVEN`. Claude
   Code's `modelUsage` response envelope is genuinely served evidence and is
   published as `PROVEN`. The proven Antigravity subscription route stays
   enabled; no API-key, paid, cloud or fallback route is introduced.

**Deep-mode continuation benchmark.** The mechanical half is MEASURED and
deterministic: a successor that can prove exact validation reuse executes 0 of
24 verification units where a cold restart executes all 24 (median over seven
trials). The provider half — median active model time, token spend,
reproduced-finding rate and false-pass rate — is an explicit **HOLD**: each
requires executing real Deep-Mode lanes against live providers, which this
writer lane is forbidden to do. No value is estimated in their place. Unblock
condition: a separately authorized lane with live Deep-Mode capacity for at
least two distinct qualified effective routes, running STANDARD 1..3 → DEEP
1..3 twice (cold restart vs packet reuse) over one identical candidate,
environment, scope and failure fingerprint.

**Carried, not discharged.** The ordinary `execute()` Antigravity proof still
carries its historical `served_model_id`/`served_model_label` aliases, now
accompanied by the explicit `NOT_PROVEN` class. Removing them outright would
require editing `tests/hermes_cli/fleet/test_agy_adapter.py`, which is outside
the leased path set, so it is held for a separately scoped change rather than
done silently. Likewise `launch_shell_review`, `launch_async_review` and
`execute_material_route` still have no production caller; only the sealed
owned-material ingress was in scope here. Release remains HOLD pending a fresh
commit and an independent, provider-distinct Final Inspection.

#### Fence-7 repair record (2026-08-01, commit `a9a2a6db`)

A held-out non-Claude Final Inspection of `a9a2a6db` returned **FAIL / HOLD**
and was right on all three counts. Its receipt is retained at SHA-256
`083f051d5d10ddc967f6e63bad6f8e4e8d88a016ce9125697ddcfc32b4495d55`. The second
inspector in that batch produced no verdict and carries zero credit; a
replacement investigation runs separately. Nothing below discharges any finding.

**B1 — true owner death.** The prior coverage called `runner.interrupt()`
inside a live owner, which proves an in-process cancel race, not owner death. A
sudden owner death runs no userspace code at all, so the guarantee is now
kernel-enforced and armed *before* the child may execute:

- Windows: a Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`. The child is
  created suspended, assigned to the job, and only then resumed, so it cannot
  execute a single instruction unconstrained. Owner death closes the last job
  handle and the kernel terminates the job.
- Linux: `prctl(PR_SET_PDEATHSIG, SIGKILL)` set in the forked child before exec.
- Everything else (notably macOS): containment reports **unavailable** and the
  material rail refuses to spawn, releasing its lease and terminalizing the
  saga without claiming a review ran. It never downgrades silently.

Proof is out-of-process: a real owner subprocess spawns a real long-lived child,
the owner is hard-killed, and the exact child identity (`pid` + `create_time`,
captured before the kill so PID reuse cannot fake a pass) must be gone. A
negative control confirms the same scenario **without** containment leaves the
child alive, so the test is not vacuous. Exact-child discipline is preserved:
the job holds only this child, thread resume is filtered to that exact PID, and
no PID scan or process-name match is used anywhere.

**B2 — real production ingress.** `tools/delegate_tool.py` is the shipped model
tool and the real composition root. Its material branch previously identified
material fields and then rejected them. It now hands a request carrying
**complete** receipt/fence/route evidence to `dispatch_material_review`, giving
the runtime-reachable chain
`delegate_task -> _dispatch_material_review_task -> dispatch_material_review ->
launch_fleet_owned_material_review -> launch_material_async_review`. No new core
model tool was added, no test-only wrapper, and no self-call. Material work
still fails closed when any of `candidate_hash, review_lens, scope, lane,
prompt, attempt_id, fence_token, environment_fingerprint, evidence_fingerprint,
preflight, deadline_seconds, output_path` is absent, cannot be mixed into a
generic batch, and cannot be dispatched two-at-a-time. Generic non-material
delegation is untouched.

**B4 — unproven qualification removed.** `model_qualification: "agy
client-propagated selected model"` is gone. Client propagation cannot qualify
the model that served a request, so the proof now carries
`model_evidence_source` — naming the artifact, not asserting proof — and the
receipt allowlist refuses `model_qualification` outright, so no adapter can
republish a qualification claim. Requested/selected provenance and the
`NOT_PROVEN` served-evidence class are unchanged.

**Fence-7 evidence.** Focused leased files 479 passed / 3 failed, those 3 being
the documented inherited `test_delegate.py` baseline exclusions reproduced
identically from a pristine archive of `a9a2a6db`. Canonical 28-file broad suite
from a 71-character root: **614 passed, 0 failed**. All changed Python compiles;
`git diff --check` clean; 9 changed paths, all inside the 21 leased writes; no
staged content. Mutation guards for the three fixes: **10/10 killed**, every
mutated file restored to its exact pre-mutation SHA-256.

**Carried, not closed.** `execute_material_route` still has no production
caller. It runs through `FleetService.run`, the rail Hermes placed on HOLD;
wiring a caller would resurrect that held defect and inventing one would be a
meaningless self-call. `launch_shell_review` / `launch_async_review` remain
intentional public dependency-injected entrypoints. Deep-mode provider metrics
remain HOLD. Release remains HOLD pending a coordinator forward commit and fresh
non-Claude inspection.

#### Fence-8 repair record (2026-08-01, commit `d6897eec5`)

Three blind Final Inspectors examined `d6897eec5e58dba8fd9afa34e3feb7da9a2fd392`.
Codex returned FAIL, Grok returned a detailed FAIL (then, with no product change and
no finding discharged, a contradictory short PASS), and AGY/Gemini returned PASS.
The reconciled outcome is **FAIL / RELEASE HOLD**, decided on evidentiary coverage
rather than a 2-to-1 count: AGY's PASS is superseded only on B1 and B2, the two rows
where it did not examine the disputed artifact (it read the `prctl` call but never the
post-`prctl` race or return code, and it read the internal Python call chain but never
validated a payload against the registered `additionalProperties=False` schema). Full
reconciliation, all four verdicts, all corrections and all four least-certain items are
preserved in the external builder meta-audit.

**B1 — Linux fork→`prctl` race closed and `prctl` failure checked.** The previous
implementation was a single bare `prctl(PR_SET_PDEATHSIG, SIGKILL)`. Two holes follow
from that and both are now closed:

- the expected parent PID is captured in the **spawning** process, before any child
  exists, and bound into the `preexec_fn` closure;
- the child arms SIGKILL first, then **fails closed on a non-zero `prctl` return**
  rather than proceeding to exec uncontained;
- it then compares `getppid()` against the captured parent and, on mismatch, terminates
  itself with a distinguishable status **before exec**, so the provider argv never runs
  for a child whose owner already died.

The guard takes its primitives by injection, so all branches are falsifiable as unit
controls: `prctl` failure, parent-died-before-arming, correct-parent success, arming
ordering, PID-capture binding, and a Windows no-regression control. The two AST
predicates the inspectors used (`has_getppid_race_check`, `prctl_return_checked`) now
both read **True**, having read **False** at `d6897eec5`.

**This host is Windows. No live Linux proof is claimed.** The Linux guard is proven by
injected-dependency unit controls over the exact logic that runs in the forked child;
a real Linux kernel has not executed it here, and that limitation is stated wherever
the result is reported.

**B2 — the registered model-tool schema can now express complete material evidence.**
`_MATERIAL_REVIEW_REQUIRED_FIELDS` needs 12 fields; the registered task-item schema
previously declared 5 with `additionalProperties: False`, so a complete payload was
rejected with *"Additional properties are not allowed"* and the passing test had been
calling `delegate_task()` directly as a Python function with fields the model boundary
forbids. The task-item schema now declares all 12 (plus optional `cwd`) with strict
types and bounds — non-empty strings, `fence_token` integer ≥ 0, `deadline_seconds`
number > 0, `preflight` object — and `additionalProperties` stays **false**. Proof runs
through the live registry: a complete payload validates against
`registry.get_entry("delegate_task").schema`, and the **registered handler** (not
`delegate_task()` directly) delivers that evidence to `dispatch_material_review`.
Unknown fields, wrong types, out-of-bound values, missing evidence, mixed
generic/material batches and more than one material review all still fail closed.
No new core tool was added and no schema bypass is used anywhere in the proof.

**Fence-8 evidence.** Canonical wrapper command A: **4 files, 203 passed, 0 failed**
(197 at `d6897eec5`, plus the six new B1 controls). Canonical wrapper command B:
**1 file, 13 passed, 0 failed**. Broad 28-file suite from a 69-character root:
**620 passed, 0 failed**. Both changed test files: 299 passed with only the three
documented inherited `test_delegate.py` baselines failing. All changed Python compiles;
`git diff --check` clean; 4 changed paths, all inside the 5 leased writes; nothing
staged; quarantine untouched. Mutation guards for both repairs: **11/11 killed**, every
mutated file restored to its exact pre-mutation SHA-256. Per the Grok correction, that
mutation receipt is builder evidence only and is not independent inspection evidence.

**Carried, not closed.** `execute_material_route` still has no production caller — both
Codex and Grok note it is unchanged from base and not a newly introduced speculative
caller. `launch_shell_review` / `launch_async_review` remain intentional public
dependency-injected entrypoints. Grok's NEW-F3 (clean root has no git object store, so
commit-SHA binding is freeze/manifest/content rather than `git cat-file`) is carried and
coordinator-owned. Deep-mode provider metrics remain HOLD. Release remains HOLD pending
a coordinator forward commit and a fresh non-Claude Final Inspection.

#### Fence-9 repair record (2026-08-01, rejected commit `101150ba5`)

Three blind Final Inspectors examined `101150ba56df76cd982003f8187c9eeea83d6445`:
Codex returned **FAIL / HOLD**, Grok and AGY both returned **PASS / CLEARED**. The
reconciled outcome is **FAIL / HOLD**, and the reason is not arithmetic: a concrete
reproduction against the actual registered handler outweighs two broader reviews that
never exercised the disputed input. Before accepting the finding this builder attempted
to falsify it and could not — the reproduction succeeded and the defect is **wider** than
reported.

**B2 — dual-discriminator fail-open.** Material detection asked only whether
`candidate_hash` or `review_lens` was non-empty. Anything else was routed as generic
work. Probing `registry.get_entry("delegate_task").handler` with `_build_child_agent`
instrumented showed generic child construction reached for: both discriminators omitted
while the other ten material fields were present; both discriminators emptied; and — not
previously reported — **each material-only field on its own**. Sequence 21 had made this
model-reachable by widening the schema to accept all twelve fields, so the pre-existing
detector weakness stopped being masked.

Detection is now: presence of **any** key reserved to `_MATERIAL_REVIEW_REQUIRED_FIELDS`
classifies the task as material, *before* any generic route planning or child
construction. Presence of the key is the signal — an empty string or `None` is still a
presence — after which the material path fails closed on every missing or empty required
field. Tasks carrying only generic keys (`goal`, `context`, `role`) stay generic. The
fail-closed direction is deliberate: misclassifying generic work as material yields a
clear error, whereas misclassifying material work as generic spends provider capacity on
an unreceipted child.

Nothing else was relaxed to achieve this: `additionalProperties: false`, the strict
per-field types and bounds, route ownership, B1 and B4 are all unchanged.

**Correction to this builder's own prior work.** The sequence-21 missing-field test
skipped both discriminators with the comment *"removing these makes it a non-material
task entirely"* — which states the fail-open as if it were the specification. That test
shared the premise of the defect it was meant to guard, so no amount of green could have
caught it. The skip is removed; every required field is now covered, and the mutation
suite restores the exact `101150ba5` detector to prove the new controls kill it.

**Fence-9 evidence.** ReviewBatchAdmission canonical: 13 passed, 0 failed. Broad 28-file
canonical suite from a 69-character root: **620 passed, 0 failed, zero FLAKY lines, zero
retries**. `tests/tools/test_delegate.py`: 190 passed plus 75 subtests, with only the
three documented inherited baselines failing. All changed Python compiles; `git diff
--check` clean; 2 changed paths, both inside the 3 leased writes; nothing staged;
quarantine untouched. Mutation guards: **9/9 killed**, every mutated file restored to its
exact pre-mutation SHA-256 — builder evidence only, not independent inspection evidence.

**`test_process_registry.py` retry-only report — SCOPE_EXPANSION_REQUIRED, not edited.**
Codex reported a first-attempt failure where the test killed its child without waiting
for the marker-file handle to close. The brief's premise that the new Fence-8 tests do
not use marker files is **incorrect**: exactly one does. Source inspection confirms the
race is real — `child.kill()` is not awaited before `tempfile.TemporaryDirectory` runs
`rmtree`, and Windows termination is asynchronous, so the child can still hold the marker
open. It did **not** reproduce here in 40 executions (10 canonical file runs plus 30
targeted repetitions, all from a 69-character root), but absence of reproduction does not
disprove a handle-close race, so it is dispositioned **OPEN-WITH-KNOWN-CAUSE** rather than
green. The fix — a bounded `child.wait()` after `kill()` — requires
`tests/tools/test_process_registry.py`, which is outside this lease's allowed writes, so
this builder stopped and did not edit it. No retry-only pass is counted as green anywhere
in this sequence.

**Carried, not closed.** Codex's Integrity NOT MET (assigned root has no `.git`, so tree
and parent cannot be independently recomputed) is coordinator-owned. The short-root
619/620 result is an environment precondition on repository path length, agreed by all
sources. `execute_material_route` still has no production caller. Deep-mode provider
metrics remain HOLD. Release remains HOLD pending a coordinator forward commit and a fresh
non-Claude Final Inspection.

#### Fence-9 sequence-23 record — deterministic test teardown (2026-08-01)

Sequence 22 dispositioned the Codex-observed retry-only `test_process_registry.py`
result as OPEN-WITH-KNOWN-CAUSE and stopped, because the fix needed a path outside
that lease. This sequence carries out that scoped follow-up. It is **test
stabilization only**: `tools/process_registry.py` production bytes are unchanged, and
the sequence-22 B2 repair is protected by hash and left byte-identical.

**The defect.** The Fence-8 marker-file test tore down with `child.kill()` followed
immediately by `containment.close()`, inside a `tempfile.TemporaryDirectory`. Windows
`TerminateProcess` is asynchronous: `kill()` returns before the process has exited and
released its handles, so `rmtree` could delete a directory in which the dying child
still held `child-ran.marker` open, raising `PermissionError`. That is exactly the
first-attempt failure the inspector reported, and why the file passed only on retry.

**The repair.** Teardown is now a single helper with a fixed, observable ordering:
terminate → **bounded wait** → containment close, after which the temporary directory
may be removed. The wait carries an explicit finite bound so a wedged child cannot hang
the suite, and a timeout is **not** treated as success — containment is still released,
then the timeout is raised as a failure.

**Directly testable, with fakes, not source text.** The helper takes an optional
ordering record and its dependencies are injected, so five behavioural controls assert
the contract without inspecting any source text or regex: ordering is exactly
`kill → wait → close`; the wait receives a finite bound; a non-exiting child raises and
still closes containment exactly once with `wait` never recorded; a kill that raises
still waits and still closes; containment closes exactly once.

**Call-site binding.** The first negative-control run showed that restoring the old
inline teardown at the real marker-file call site **survived** the helper's unit tests,
because those target the helper rather than the call site. Rather than record that as a
known limit, a behavioural binding test was added: the real marker-file test is executed
with the helper wrapped in a recording delegate that still performs the real teardown,
and the wrapper must be invoked exactly once. The negative control is now killed.

**Evidence.** Negative controls: **6/6 killed**, every mutated file restored to its
exact pre-mutation SHA-256 — including a mutant that removes the bounded wait, i.e. the
verbatim pre-repair teardown. Marker-file test: **50 repetitions, 0 failures**.
`test_process_registry.py` under the canonical wrapper: **10 repetitions, 0 non-zero
exits, 0 flaky runs**, 124 passed each time. Protected sequence-22 repair re-verified:
ReviewBatchAdmission 13/13 and the B2 discriminator/schema suites 17 passed with 65
subtests, with `tests/tools/test_delegate.py` and `tools/delegate_tool.py` byte-identical
to their protected hashes. All runs used a 68-character root.

**Broad-suite count, stated plainly.** The canonical 28-file suite is **626 passed, 0
failed, zero FLAKY lines**. It is 626 rather than 620 because this sequence adds six
tests (five teardown controls plus the call-site binding); 620 + 6 = 626. The invariant
that matters — full 28 files, zero failures, no flaky summary, no retry counted as green
— is met.

**Carried, unchanged.** Codex's Integrity NOT MET on independent tree/parent
recomputation from a `.git`-less root remains coordinator-owned. The short-root 619/620
result remains an environment precondition on repository path length.
`execute_material_route` still has no production caller. Deep-mode provider metrics
remain HOLD. Release remains HOLD pending a coordinator forward commit and a fresh
non-Claude Final Inspection.

#### Fence-10 repair record (2026-08-01, rejected commit `caa7784f6`)

Three blind Final Inspectors examined `caa7784f6b3c269b22095b4039174ae7a6a48890`:
Codex returned **FAIL**, Grok returned **PASS**, and AGY returned a prose **PASS** while
its native envelope status was **ERROR** (credit INCONCLUSIVE). The reconciled outcome is
**FAIL / RELEASE HOLD**, decided on evidence rather than count: registered-handler
behaviour and production tracing defeat an untested assertion. Grok's own receipt claims
all reserved fields were covered, but it iterated the 12-entry required tuple and never
probed `cwd`; AGY rests on the same incomplete premise and did not complete natively.
Full reconciliation, every contradiction and all four least-certain items are preserved in
the external builder meta-audit.

**The defect.** `cwd` is exposed by the shipped task schema specifically as the material
review's working directory. Direct production tracing finds **zero** generic task-level
consumers and exactly one material consumer (`tools/delegate_tool.py:2670`, inside the
material dispatcher). It was nonetheless absent from material classification, so a task
carrying only `cwd` — with a real value, an empty string, or `null` — was classified
generic and reached generic child construction. The coordinator reproduced this through
the registered handler (`schema_accepted True`, `generic_child_called True`,
`material_dispatch_called False`), and this builder reproduced all three forms
independently before accepting the finding.

**Why `cwd` is not simply added to the required set.** It is optional: a complete material
review may omit it, and the dispatcher defaults it to `os.getcwd()`. Making it required
would break legitimate complete payloads. The distinction the previous repair collapsed is
that the set used to **classify** a task as material is not the set of evidence a material
review must **supply**.

**The repair — one source of truth.** Task fields are now partitioned once, in source,
into three sets: `_GENERIC_TASK_FIELDS`, `_MATERIAL_REVIEW_REQUIRED_FIELDS` and
`_MATERIAL_REVIEW_OPTIONAL_FIELDS`. The classification marker set is derived, never
hand-maintained:
`_MATERIAL_REVIEW_MARKER_FIELDS = required + optional`. Classification keys off that
marker set by **key presence**, including empty and null values; a marker-bearing task
then fails closed on every missing or empty required field. `cwd` is a marker and stays
optional.

**Drift prevention.** A behavioural invariant binds the **registered** schema to that
partition without reading source text: the schema's task-item properties must equal
generic ∪ markers exactly, with an empty intersection, and the marker set must equal
required ∪ optional. A future schema property that is added without being classified —
material or generic — fails the suite immediately. A second control derives the
material-only property list from the schema and asserts each one alone fails closed, so a
newly added optional field is covered the moment it appears.

**Correction to this builder's own prior work.** Sequence 22 reported that classification
covered "any key reserved to `_MATERIAL_REVIEW_REQUIRED_FIELDS`" and presented that as
closing the fail-open class. The claim was too strong: the implementation keyed off the
required tuple and the guards iterated that same tuple, so implementation and tests shared
one blind spot and no amount of green could reveal it. This is the same failure mode as
the earlier C10 correction, one level up — a derived set used as if it were the defining
set. The remedy is structural rather than an added entry, because adding `cwd` alone would
leave the next optional field equally invisible.

**Fence-10 evidence.** Canonical material suite (ReviewBatchAdmission +
RegisteredMaterialSchema + MaterialDiscriminator + OptionalMaterialMarkerCwd): **38
passed, 0 failed**. Full `test_delegate.py`: 198 passed with 94 subtests, only the three
documented inherited baselines failing. `test_process_registry.py`: 124 passed, 0 failed,
protected file byte-identical. Canonical 28-file suite from a 69-character root: **626
passed, 0 failed, zero FLAKY lines, zero retries**. Mutation controls: **10/10 killed**,
every mutated file restored to its exact pre-mutation SHA-256 — including a mutant that
removes `cwd` from the marker set and one that restores the sequence-22 classifier. All
changed Python compiles; `git diff --check` clean; 2 changed paths, both inside the 3
leased writes; nothing staged; quarantine untouched.

**Preserved unchanged.** B1 owner-death containment, the sequence-23 bounded process
cleanup, B4 truthful model evidence, schema transport with `additionalProperties: false`
and per-field bounds, and ordinary generic delegation behaviour.

**Carried, not closed.** Codex's least-certain item on whether `cwd` was intended
generic-compatible is carried open, though the repair is fail-closed either way. Grok's
Linux live PDEATHSIG limitation on a Windows host and AGY's Windows handle-latency item
remain open. `execute_material_route` still has no production caller. Deep-mode provider
metrics remain HOLD. Release remains HOLD pending a coordinator forward commit and a fresh
non-Claude Final Inspection.
