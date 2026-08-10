# P3.C2 Receipt — Read-only Rewind Recovery and Frozen Canary

Timestamp: 2026-08-11T01:11:38+07:00 (ICT)
P2 parent commit: `1e8bf80feefd668c247e0c569e28131ae0a2bce4`
Lifecycle: `P3_CANDIDATE_VERIFIED_UNCOMMITTED`

## P3.C1 valid RED

The new P3 test file was the only code added before RED. On the exact P2 commit, the governed runner discovered the file and returned 2 passed / 8 failed in 5.5s. All eight failures were absence of `--include-rewound`; the two default-path controls passed. No import, collection, fixture, setup, or zero-discovery failure was credited.

Receipt: `P3-C1-RED-RECEIPT.md` SHA-256 `fbfccb7829e0153833a5e0d4b887a30963610956debbc2504f4b311c29373f9b`.

Frozen decisive hashes remained byte-identical after build:

- raw byte recovery function `f4c24b3ddb28cceb98ee683c05b20af94759bf833911d0cc0054c73eaeb3a54e`; body `296c7ae0b386ba249a7ec476b116b2c0ae5cc07b306a3e4b51608b09d105b944`
- scope fence function `9db44eb2834f7deda0eaf0ad8ef8cded136f418c2cee4d9d17532324b639c43e`; body `aca4a2a08adfda3c2fef8bd2d3e00b4d4cf9e9c637a27f9a0072dc3bdcbd3a86`
- exact one-session I4 request function `e3761b7989e71f94fcc955d2eae86a9d7afa4b68bccd4ccb5578d971e8c522b0`; body `f125e33715af43b12ae7e694b9c7ce207f8e34c5fade67e6ba23fd8f59da81b2`
- default JSONL call-contract function `e2965286d4be6059f7892c726c904dda2988c09d7c7a9fed99ca799ca874aa84`; body `89ed4cf4b47b6e69bc312c76230d938901f50abd261f5b6f4dc5642c541c1a93`
- default replay/export/search function `668355f0ef64aff11651bb6555b63d43c69fadb97761cf9bc10da3d3e343b586`; body `788977afa0c5a3f40bad69f222e982fb84469ef791ea72ab199c63fdc0ffca03`

Freeze artifact SHA-256: `fcfe962bb275e6e3b9e1d3f8f08ffd4d34e42fefa2a3bfe35bfe3dda2bb269b2`.

## P3.C2 implementation

I4 remains the raw-row `SessionDB.export_session(..., include_rewound=True)` implementation landed earlier in the candidate. P3 binds the supported CLI to that path and proves it does not invoke `get_messages_as_conversation`, `sanitize_context`, or `.strip()`. A leading/trailing-whitespace sentinel is compared as UTF-8 bytes after JSONL recovery. Active rows and `active=0, compacted=0` rows are included in row order; `active=0, compacted=1` rows are excluded.

I5 adds `hermes sessions export --include-rewound` with a strict fence:

- exactly one resolved `--session-id`;
- JSONL only;
- incompatible with `--only`, logical lineage, `--delete-after-verified`, redaction, session filters, dry-run, and trace-upload options;
- default export continues to call `export_session(session_id)` with no recovery keyword.

Production/source SHA-256:

- `hermes_state.py`: `8432a42218866a507a7cefe0be61f534566d370bc7ef20f3b23def964e121d83`
- `hermes_cli/main.py`: `577298a0b4378f3160bdac31a42f3c53a9e2fe190b1a7cbc3359a28ba5b3de0d`
- `acp_adapter/session.py`: `39f2b146e044c193e464056f58b6a8ce2e988e18ab8a5983a107247294ad2a1d` (unchanged production control owner)

## MED-4 and ACP M-1

MED-4 is recorded as `ACCEPTED_CONSEQUENCE_V1`: active-only `--delete-after-verified` can delete a whole session after verifying an export that omitted its rewind rows. The recovery flag is therefore incompatible with deletion, and default deletion semantics remain unchanged. Decision SHA-256: `6172f2cad4276c5b0f21a54090f2397eba69180af697463feb1bea13527bc2d2`.

The ACP successful-probe control proves a non-owner fallback preserves exact `active=0, compacted=0` rows. Its green does not clear the fail-open-on-exception or TOCTOU residual. Residual record SHA-256: `b5e5edc06fa6b3a8143052f8fa167506fc10f3d315c8b382f0c295909e9de4d5`.

## Frozen canary

The canary ran twice against two fresh disposable HERMES_HOME roots and real isolated `state.db` files. Both returned PASS. Each run seeded three turns plus a pre-existing inactive compaction row, restarted before prompt submission, invoked Restore/Edit/Re-run through `prompt.submit`, restarted/read back afterward, and invoked the supported CLI recovery surface.

Both runs proved:

- sentinel absent from live history, default replay, default export, and ordinary search;
- exact sentinel recovered once through JSONL recovery;
- retained prefix present once;
- pre-existing inactive row remains `(active=0, compacted=1)`;
- non-rewind hard replacement remains destructive.

Canary record SHA-256: `56664c44c992de1d1e903d41e160b8c840708f75dbc18738446c1f58e11c892d`.

## Mutation evidence

Sixteen one-at-a-time mutations each exited 1 with its named AssertionError. Collection, import, syntax, setup, and zero-discovery failures were rejected. All three touched source owners were restored byte-identically.

- `all_mutations_failed_named_tests = true`
- `source_restored_byte_identical = true`
- fresh restored governed gate: 1,195 passed / 0 failed in 83.7s
- manifest SHA-256: `b5615adc2110366a013cb5a68fa6ac341bfd8d977d87fac0e6e76c47429e0217`

## Final governed P1 + P2 + P3 gate

Actual runner output:

- `tests/gateway/test_retry_response.py`: 4 passed
- `tests/cli/test_cli_retry.py`: 3 passed
- `tests/gateway/test_dedupe_user_turns.py`: 8 passed
- `tests/hermes_cli/test_sessions_export_rewound.py`: 13 passed
- `tests/gateway/test_session.py`: 152 passed
- `tests/acp/test_session.py`: 46 passed
- `tests/test_hermes_state.py`: 494 passed
- `tests/test_tui_gateway_server.py`: 475 passed

Total: 1,195 passed / 0 failed in 83.7s; eight files; no retries; exit 0.

No P3 commit, push, merge, protected-ref move, rebase, tag, deployment, activation, trading action, or order occurred. Exact provider token telemetry is unavailable and is not estimated.
