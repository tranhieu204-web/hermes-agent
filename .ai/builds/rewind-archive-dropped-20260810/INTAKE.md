# INTAKE — Recoverable Desktop/Gateway Rewind

- Build ID: `rewind-archive-dropped-20260810`
- Frozen at: `2026-08-10 17:01:52 ICT (UTC+07:00)`
- Decision authority: Sakaan
- Approval: explicit typed `GO`
- Frozen choices: `1a 2b 3a 4a 5a`
- Base repository identity: `fork/main` = `872c341302b5ed8941f280c3b7939cabba930b5a`
- Form: `HIGH_RISK -> DECOMPOSED`
- State: `INTAKE_FROZEN`; this record is immutable. Any scope/acceptance/interface change requires a new version and plan re-audit.

## Goal

Make every user-facing rewind/truncate operation preserve the exact dropped active rows as inactive rewind history while leaving the visible live transcript unchanged and keeping legitimate redaction/full-rewrite operations destructive.

## Approved decisions

1. `1a` — Add one atomic, fail-closed soft-rewind storage operation that validates the resolved `r2` state, archives only the dropped suffix, updates active counters, and leaves the retained prefix in place.
2. `2b` — Include Desktop Restore/Edit/Re-run and gateway `/retry`; preserve hard replacement for redaction and rotated compression.
3. `3a` — Add a supported read-only recovery/export view for `active=0, compacted=0` rewind rows; no mutating one-click restoration.
4. `4a` — Retain rewind rows with their session. Do not add rewind-specific hard deletion; existing whole-session pruning remains the deletion authority.
5. `5a` — Hermes / `openai-codex` / `gpt-5.6-sol` / HIGH is sole builder-finalizer and is excluded from assurance. A fresh, separately attested Claude Code / `claude-opus-5` / HIGH invocation performs independent plan and final review, transported by the COO and forbidden from mutation.

## In scope

- Exact base `872c341302b5ed8941f280c3b7939cabba930b5a`.
- Desktop Restore/Edit/Re-run through `prompt.submit`.
- Gateway `/retry` truncation.
- An atomic storage boundary derived from existing `rewind_to_message`, with active-state validation, suffix-only archival, counter repair, and fail-closed DB/history mismatch behavior.
- Commit in-memory truncated history only after the canonical DB mutation succeeds.
- A read-only `hermes sessions export` recovery option that includes live rows plus rewound rows (`active=0, compacted=0`) while excluding compaction archives unless already requested by an existing explicit forensic path.
- Read-path isolation tests for live replay, default search, export/recovery, and pre-existing inactive rows.
- Opening-turn Desktop warning copy corrected in every shipped Desktop locale so it no longer claims the rows are unrecoverably deleted.
- Same inspected source change ported to the backend and Desktop repositories without including their unrelated dirty files.

## Out of scope

- Recovering rows already hard-deleted by prior rewinds.
- Mutating one-click branch restoration, branch merging, or archive-generation selection UX.
- A new archive-generation schema or rewind-specific TTL.
- Changing Yuanbao recall/redaction or rotated-compression hard-rewrite semantics.
- Merge, push, deploy, clearance, or live activation without later Sakaan authority.

## Acceptance criteria

1. A RED regression on exact base proves Desktop rewind leaves no inactive copy of a dropped sentinel.
2. After the fix, the exact dropped suffix is `active=0, compacted=0`; the retained prefix remains active once, with no inactive duplicate prefix.
3. Live replay after restart is the intended truncated prefix plus the rerun turn.
4. Pre-existing inactive compaction/undo rows remain unchanged.
5. `sessions.message_count` and `tool_call_count` equal the active set after every rewind, including opening-turn rewind.
6. Repeated-text/r2 identity, stale-ID refusal, and opening-turn confirmation from `872c341302` remain passing.
7. A forced persistence failure leaves both canonical DB state and in-memory history at the pre-rewind state and returns an error.
8. Default search does not reveal rewind rows; the supported recovery/export surface retrieves the exact bytes.
9. Gateway `/retry` archives its dropped turn and does not resend if persistence fails.
10. Yuanbao redaction remains destructive; rotated compression retains current semantics.
11. No schema migration, new dependency, credential reach, network egress, or live-profile test contamination.
12. The reland gate reports no new import failure and at least the 434-module backend baseline.
13. The exact candidate is independently reviewed before either repository is landed.

## Frozen canary

Against an isolated real `state.db` and isolated Desktop/gateway runtime:

1. Create a three-turn conversation containing a unique byte-exact sentinel and a pre-existing inactive row.
2. Restart/reopen the store.
3. Invoke Restore/Edit/Re-run on the turn before the sentinel through `prompt.submit`.
4. Prove the sentinel is absent from live replay and ordinary search.
5. Prove the sentinel is present byte-for-byte through the supported read-only recovery/export option.
6. Prove the retained prefix appears once, the pre-existing inactive row survives, and non-rewind hard-replace callers retain existing semantics.

## Stack and targets

Python, SQLite/FTS5, TUI/Desktop JSON-RPC gateway, gateway slash-command path, Hermes sessions export CLI, and Desktop React i18n copy. Backend is the source candidate; Desktop receives a controlled port of the exact accepted diff.

## Data and migration

No schema migration. Existing `messages.active` and `messages.compacted` columns are the archive contract. Existing databases must open unchanged. Previously deleted rows are not recoverable from this build.

## Retention

Rewind rows remain with the session. `sessions.auto_prune` remains opt-in and defaults false. If the user enables it, the configured whole-ended-session retention policy remains authoritative. No independent rewind-row hard deletion is introduced.

## Must not break

- Rewind identity baseline `872c341302`.
- Exact `r2` occurrence binding and stale-ID refusal.
- Opening-turn dedicated confirmation.
- Prompt-cache-stable replay.
- Existing compaction archives and default search isolation.
- Redaction/privacy semantics and rotated compression.
- The unrelated backend `skills/autonomous-ai-agents/claude-code/SKILL.md` change.
- The unrelated Desktop `apps/desktop/electron/main.ts` change.

## Performance and quality budget

- One bounded SQLite write transaction per rewind.
- Queries scoped by `session_id` and indexed row identity; no global message-table scan.
- No inactive retained-prefix duplication.
- No prompt-schema/core-tool growth and no new dependency.
- All Python tests run through `scripts/run_tests.sh`; Desktop tests use repository Vitest/build commands and the isolated verifier only.

## Rollback

Code-only revert/port rollback; no schema rollback. Rows already archived by the candidate remain valid inactive data. Reverting restores destructive future rewind behavior and is containment only, not an acceptable steady state.

## Roles

- Sole builder-finalizer: Hermes, `openai-codex`, `gpt-5.6-sol`, requested HIGH. Excluded from every assurance seat.
- Independent plan/final inspector: fresh Claude Code, `claude-opus-5`, requested HIGH; COO-transported; no mutation.
- COO/executor: transport, reland receipt, controlled port/readback, and closure; no semantic clearance.

## Estimated budget

- Builder: approximately 4–6 hours and 80k–140k tokens.
- Independent review: approximately 1–2 hours and 40k–80k tokens.
- Dual-repository port, reland receipt, and readback: approximately 1–2 hours after applicable approvals.
