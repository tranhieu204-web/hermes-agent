# PLAN REVIEW RESCAN HANDOFF — MAP V2.1 — READ ONLY

Build: `rewind-archive-dropped-20260810`
Candidate/base: `872c341302b5ed8941f280c3b7939cabba930b5a`
Frozen INTAKE: `ad08d3458d97a25f2daf7976b5dd0746c35945b5ebb204c464ff4313f4e2992e`
Superseded map-v1 root: `69e41064cbcf0b602a4c75569c7d9c6ac17040e60f7df991a1f8062490e1121c`
Superseded map-v2 root: `a53e73093e0ade6ffc6bdb4b03dae88e4aa22e5464bde8d7dbde6f1b8299adc0`
Map-v2.1 structure root: `fe1ae82cfa830da45d01ff9df51491c922efe2e34515a9753300176ee49d7028`
Map-v2.1 artifact SHA-256: `102e06ef861c6bdf587f8d79dca8ae5b636148c480b707e1d3fa4d898554e83d`

Reviewer: the SAME independently qualified reviewer, in a fresh top-level Claude Code `claude-opus-5` xhigh epoch. COO transports and independently verifies. Read-only: do not modify files or run Git writes.

Map-v2.1 is additive record-keeping only. Relative to map-v2 it adds exactly four explicit scope adjudications under `structure.scopeAdjudications`; the prior 14 edges, I1, package/checkpoint records, blocking remediations, file ownership and attempt counters remain unchanged.

Required scope adjudication audit:
1. `acp_adapter/session.py:491` — NOT_APPLICABLE only under its complete-live-history, non-owner fallback and archive-preserving `active_only` gate.
2. `gateway/platforms/api_server.py:3263` — NOT_APPLICABLE only as initialization of a proven-new empty fork child, with no source truncation.
3. `tui_gateway/server.py:15360` — IN_SCOPE under existing P1 ownership; verify the bound P1.C3/I1 fail-closed durable-before-memory gate is sufficient.
4. `apps/desktop/src/i18n/types.ts` — NOT_APPLICABLE only while the correction rewords existing `restoreBody`/`restoreBodyWipes` keys; any new key requires a map revision.

Then rescan the original fourteen repairs unchanged. Return exactly `PLAN REVIEW: PASS` or `PLAN REVIEW: HOLD`, followed by finding IDs/severity/evidence and required map changes. Record model/effort/freshness, input hash, token spend and no-mutation evidence. A PASS permits Stage 3 RED only.
