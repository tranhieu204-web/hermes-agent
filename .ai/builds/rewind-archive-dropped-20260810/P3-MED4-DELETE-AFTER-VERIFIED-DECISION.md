# P3 MED-4 Decision — Delete After Verified — Historical Consequence Superseded

Historical P3 disposition: `ACCEPTED_CONSEQUENCE_V1`.

Current disposition: `SUPERSEDED_BY_STAGE6_B2_REFUSAL_FIX`.

The accepted consequence recorded below — that `--delete-after-verified` silently omits the
`active=0, compacted=0` rewind rows — **no longer describes shipped behavior**. The Stage 6 B-2
repair replaced omission with refusal: `hermes_cli/main.py` now refuses the deletion when a selected
session has rewind rows that ordinary export did not cover, and fails closed if the coverage probe
itself fails. The authoritative current statement is in `README.md` (B-2 / MED-4); everything below
is retained only as historical provenance for the P3 checkpoint.

`hermes sessions export --delete-after-verified` verifies the established active-only Markdown/QMD export and then deletes the whole session. Therefore that existing backup-then-delete path omits exactly the `active=0, compacted=0` rewind rows introduced for recovery before whole-session deletion.

This is explicit and accepted for v1, not fixed or hidden:

- `--include-rewound` is a separate read-only, single-session JSONL recovery surface.
- `--include-rewound` is incompatible with `--delete-after-verified`.
- Default export and deletion behavior remains unchanged.
- A successful `--delete-after-verified` operation must not be described as a verified backup of rewind rows.
- An operator who needs rewind recovery must export it separately before choosing whole-session deletion.

Rationale: silently broadening the destructive path would change an established backup format and deletion contract. P3 provides explicit recovery without changing that lifecycle.
