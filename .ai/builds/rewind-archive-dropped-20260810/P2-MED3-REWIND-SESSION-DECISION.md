# P2 MED-3 Decision — Deliberate Divergence

Timestamp: 2026-08-11T00:23:19+07:00 (ICT)
Decision: `DELIBERATELY_DIVERGE`

`SessionStore.rewind_transcript(session_id, expected_history, truncate_before_user_ordinal) -> bool` is I3 for gateway `/retry`. It passes the complete expected active history and exact user-turn ordinal to I1, fails closed on stale/compression/storage errors, and exposes only success/failure to the retry caller.

`SessionStore.rewind_session(session_id, n=1) -> Optional[dict]` remains the established gateway `/undo [N]` API. It resolves a count of user turns to a durable message id through `rewind_to_message` and returns target metadata (`rewound_count`, `turns_undone`, `target_text`).

Both paths deliberately share recoverable soft-archive semantics and clear dirty transcript custody only after a successful durable write. They do not share caller coordinates or return shape. Reconciling them would change the established `/undo [N]` API and its metadata contract, while deprecating either would remove an active caller. P2 therefore keeps both explicit adapters rather than hiding one behind the other.

`rewrite_transcript` remains the separate destructive primitive for redaction/recall, rotated compression, and genuine whole-transcript replacement.
