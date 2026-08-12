# CP-1 Spec Extraction Completion — rewind-port-0200

Source objects were read semantically in commit order. This record maps each specified behavior to the 0.20.0 port; line numbers are from the completed CP-1 candidate.

## 18319f3f4 — identity rather than rendered position

Specified behavior:
- Display and model projections can diverge; Restore must address a model user occurrence rather than count rendered bubbles.
- Producers annotate only proven reachable user rows; structured content uses one shared coercion.
- `prompt.submit` accepts an identity target while retaining the legacy ordinal path.
- A cut before the opening turn must not silently delete the transcript.
- The original patch also stamped a cold REST transcript and carried unrelated pre-existing teardown/operator-policy hunks.

0.20.0 location/disposition:
- Shared coercion: `tui_gateway/rewind_identity.py:11-57`; display `text`/raw `content` dual coercion: `rewind_identity.py:86-90`.
- Model ordinal predicate: `rewind_identity.py:60-65`.
- Conservative spine annotation and tri-state: `rewind_identity.py:96-150`.
- Gateway display-to-client adapter: `tui_gateway/server.py:7234-7239`.
- Registered producer paths: `tui_gateway/methods_session.py:133,494,581,643,2462,2651,2937`, live payload `tui_gateway/server.py:8208-8210`, compute-host egress `tui_gateway/compute_host.py:740-756`.
- ID consumer and opening-cut safety live in `tui_gateway/methods_prompt.py:105,190-278`.
- REST stamping is deliberately not ported in CP-1; it is CP-2 scope by the binding map. The unrelated teardown/operator-policy hunks were not ported as rewind work.

## fc063c47b — exact resolution only

Specified behavior:
- Remove content-only fallback. A shifted, compacted, stale, malformed, or otherwise non-exact identity refuses instead of re-targeting a different ordinal and risking a wipe.

0.20.0 location:
- Strict parser, bounds check, and exact prefix-hash equality: `tui_gateway/rewind_identity.py:153-171`.
- Refusal at the registered submit consumer: `tui_gateway/methods_prompt.py:224-246` (4018 before persistence/turn start).

## c9101a651 — an ID is not implicit whole-transcript consent

Specified behavior:
- Even an exactly resolving ID must not auto-confirm an empty retained prefix.
- Generic `confirm_empty_truncate` must not cross-arm the ID path.

0.20.0 location:
- Confirmation matrix and ID-specific whole-delete check: `tui_gateway/methods_prompt.py:177-205,251-278`.
- The port additionally preserves 0.20.0's stronger `confirm_truncate` requirement and fail-closed dual-target/no-target rules; these are deliberate current hardening, not omissions.

## 4d1f52ffb — occurrence-bound r2 identity and archive-safe live replacement

Specified behavior:
- Replace r1 ordinal+text digest with `r2:<ordinal>:<prefix-hash>`, binding the exact retained prefix plus selected turn text.
- Align the complete comparable user/assistant spine from the tip and stop at first divergence.
- Every display user row has explicit tri-state (`rewind_id` string or null); non-user rows have no key.
- Producer and consumer share one ordinal function.
- Rewind replaces only active rows so previously soft-archived rows survive.

0.20.0 location:
- r2 prefix and canonical prefix hashing: `tui_gateway/rewind_identity.py:8,68-83`.
- Exact shared `model_user_indices` predicate: `rewind_identity.py:60-65`; imported/called by submit in `tui_gateway/methods_prompt.py:212-218`.
- Full-spine alignment and tri-state: `rewind_identity.py:93-150`.
- Active-only replacement plus archive of newly dropped live rows: `tui_gateway/methods_prompt.py:307-324` (`active_only=True, archive_dropped=True`). `archive_dropped=True` is required by 0.20.0's storage contract and strengthens the older spec while preserving it.

## 4793eb531 — opening-turn restore behind dedicated consent

Specified behavior:
- Restore/edit/rerun of the first turn is allowed only with a dedicated `confirm_delete_entire_transcript` signal.
- Generic empty-truncation confirmation remains ignored for ID addressing.

0.20.0 location:
- Dedicated confirmation evaluation and 4028 refusal: `tui_gateway/methods_prompt.py:251-278`.
- Successful persistence remains after resolution/confirmation and before in-memory mutation: `methods_prompt.py:307-329`.

## 597142813 — preserve soft-archived rows

Specified behavior:
- A real SessionDB regression must prove rewinding live history does not delete pre-existing inactive rows.

0.20.0 location:
- Production contract: `tui_gateway/methods_prompt.py:319-324`.
- Real SessionDB oracle: `tests/test_tui_gateway_server.py` test `test_rewind_preserves_soft_archived_rows` (line follows the CP-1 producer block in the completed file). It verifies both old inactive rows and newly dropped live rows remain inactive.

## Deliberate non-ports / decomposition boundary

- REST transcript annotation from the historical patches is not missing CP-1 work: the binding map assigns safe always-paged REST parity to CP-2 (`hermes_cli/web_routers/sessions.py` and its own test file). Those files remain untouched.
- Client/Desktop affordance changes are outside CP-1 and were not ported.
- Persisted rewind IDs, schema/version changes, and `hermes_state.py` changes are prohibited by A5; identity remains computed.
- The original first patch's unrelated teardown and operator-policy hunks are not rewind specification and were deliberately excluded.
