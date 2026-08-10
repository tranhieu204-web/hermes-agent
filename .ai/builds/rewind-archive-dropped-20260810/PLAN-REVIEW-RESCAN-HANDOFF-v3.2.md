# PLAN REVIEW RESCAN HANDOFF — MAP V3.2 — READ ONLY

Build: `rewind-archive-dropped-20260810`
Candidate/base: `872c341302b5ed8941f280c3b7939cabba930b5a`
Frozen INTAKE: `ad08d3458d97a25f2daf7976b5dd0746c35945b5ebb204c464ff4313f4e2992e`
Supersedes chain: v1 `69e41064…e1121c` -> v2 `a53e7309…9adc0` -> v2.1 `fe1ae82c…9d7028` -> v3 `898741e5…f7ace12` -> v3.1 `171b74978d53f9edbd03c9dff1bc67d2b871c4a7c3e6827bd1224487435d80d3` -> v3.2 `f9797c045646be5349f0a9b70f88770414f14cef606fc8efa200b24736a118a7`
Map-v3.2 artifact SHA-256: `a29b4219580ee4fb8f9713c91a457b08f80c383fcdaca8e80ce649ce9b0dbca5`

Reviewer: same independent `claude-opus-5` `xhigh` reviewer in a fresh read-only epoch. COO transports and independently verifies. No mutation or Git writes.

## Review scope — exact and bounded

1. F-M1 blocking discharge:
   - I1.ordinalValidation requires an integer, non-boolean ordinal with `0 <= ordinal < active user-turn count` inside the same BEGIN IMMEDIATE transaction before indexing/mutation.
   - Negative, non-integer, boolean and upper-out-of-range values raise RewindHistoryConflict and leave rows, counters, rewind_count and head byte-identical.
   - I1.transaction and P1.C2 oracle/scope bind this behavior.
2. F-L1: P1.C1 says all three RED tests use real SessionDB instances.
3. F-L2: TUI and CLI baseExpected each say every named new test collects exactly once.
4. F-I1: E11 explicitly governs the two TUI failures and binds the third CLI test through gate.companionRed.
5. F-I3: ledger T3b pending counts are null, not 0/0/0.
6. M-1 ground correction: ACP source is outside choice 2b and all package file lists; touching it is an INTAKE/file-ownership scope change, in addition to being disproportionate.

The three independently verified RED test files are byte-identical to the prior receipt:
- TUI `985ff64021be21bf1d72ceece9e134117086cb6a38eb07bbaabcf278ec8ed81c`
- CLI `3f76263ed3d0d9b6f6186897da9588ed9303f2a62162edf164fb2200b9a59434`

Return exactly `PLAN REVIEW: PASS` or `PLAN REVIEW: HOLD`, followed by finding IDs/severity/evidence/required changes. Record exact input hash, provider/model/effort/freshness, token spend and no-mutation evidence. PASS discharges F-M1 at map level only; it does not authorize P1.C2.
