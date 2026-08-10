# P3 M-1 ACP Control and Accepted Residual

Control: when a non-owner ACP persistence fallback calls `has_archived_messages(session_id)` successfully and it reports archived rows, `replace_messages(..., active_only=True)` preserves both compaction rows (`active=0, compacted=1`) and rewind rows (`active=0, compacted=0`).

The added real-DB control asserts the exact rewind-row dictionaries remain unchanged across that fallback save.

Accepted residuals remain open:

1. The archive probe is fail-open for destruction: an exception sets `has_archived=False`, allowing the full destructive replacement.
2. The probe and replacement are separate operations and therefore remain TOCTOU-exposed.
3. A green control for the successful-probe branch is not evidence that either residual is fixed.

Disposition: `ACCEPTED_RESIDUAL_NOT_CLEARED`.
