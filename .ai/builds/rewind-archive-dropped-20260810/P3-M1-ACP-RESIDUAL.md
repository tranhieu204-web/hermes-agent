# P3 M-1 ACP Control — Historical Residual Superseded

Historical P3 disposition: `ACCEPTED_RESIDUAL_NOT_CLEARED`.

Current disposition: `SUPERSEDED_BY_STAGE6_M1_DEFECT_FIX`.

At the P3 checkpoint, the successful-probe control established only that a non-owner ACP persistence fallback preserved compaction rows (`active=0, compacted=1`) and rewind rows (`active=0, compacted=0`) when `has_archived_messages(session_id)` returned true. The real-DB control asserted that the exact rewind-row dictionaries remained unchanged across that fallback save.

The following risks were open at P3 and are retained here only as historical provenance:

1. The archive probe failed open for destruction when an exception selected `has_archived=False`.
2. The probe and replacement were separate operations and were TOCTOU-exposed.
3. The successful-probe GREEN control did not establish either risk was fixed.

Both risks were fixed in Stage 6 repair epoch 1. `has_archived_messages` now uses bounded lock/busy retry and ACP returns without destructive replacement when the probe is uncertain. `replace_messages(..., preserve_archived=True)` also performs an authoritative archived-row recheck inside the same `BEGIN IMMEDIATE` transaction that scopes the deletion, so a stale advisory `False` cannot select destruction.

Current fix and evidence are recorded in:

- `repair-epoch-1/REPAIR-DECISION.md`, M-1 disposition;
- `BUILD-CLOSURE-SUMMARY.json`, `stage6_repair_epoch_1` M-1 finding;
- `ledger.json`, `closureRecord.stage6RepairEpoch1` M-1 finding;
- mutation cases M08 and M09, including the `[locked]` and `[stale_false]` named tests.

This file is not current evidence of an open product residual. It is a superseded historical P3 record.
