# PLAN REVIEW RESCAN HANDOFF — MAP V3 — READ ONLY

Build: `rewind-archive-dropped-20260810`
Candidate/base: `872c341302b5ed8941f280c3b7939cabba930b5a`
Frozen INTAKE: `ad08d3458d97a25f2daf7976b5dd0746c35945b5ebb204c464ff4313f4e2992e`
Supersedes chain: v1 `69e41064…e1121c` -> v2 `a53e7309…9adc0` -> v2.1 `fe1ae82cfa830da45d01ff9df51491c922efe2e34515a9753300176ee49d7028` -> v3 `898741e5925f00e6bc4da949877c98709f9ee02f3bb6fc79c317a2b09f7ace12`
Map-v3 artifact SHA-256: `9e46a8590caf081630bdeca2e2014d9f1719be14da544a16a8e72e37e4b11ded`

Reviewer: the SAME independent reviewer in a fresh top-level Claude Code `claude-opus-5` xhigh epoch. COO transports and verifies. Read-only; no mutation or Git writes.

F01-F14 remain closed. Rescan only v3 changes:
- M-3a: named S04 RED is in P1.C1; TUI arithmetic is 475 discovered / 473 pass / 2 fail. Companion CLI retry RED is separately 3/2/1.
- M-3b: I2b binds full expected_history, validates direct array index, converts it to user-turn ordinal, refuses mismatch, commits I1 before memory/send/requeue, and freezes TUI 4001/4009/4018/5008 behavior.
- M-1: verify the ACP exception path is explicitly fail-open for destruction and TOCTOU-exposed; judge the MEDIUM residual/control-only proportionality. P3 control is required but does not claim the residual fixed.
- M-5: I6 explicitly defines rewound-id redelivery resurrection and tests/gateway/test_dedupe_user_turns.py is owned and gated.
- M-6: audit the exact-base search method and all 22 per-site dispositions, especially indirect api_server to_thread, the four already-correct reviewer sites, and sweep-found CLI /retry.
- M-2/M-4/M-7/M-8: source transcript-row wording, current v3 RED receipt with v2.1 HOLD history, complete I1 exclusions, and explicit S01.

Return exactly `PLAN REVIEW: PASS` or `PLAN REVIEW: HOLD`, then finding IDs/severity/evidence/required map changes. Record exact input hash, provider/model/effort/freshness, token spend and no-mutation evidence. PASS permits Stage 3 RED only.
