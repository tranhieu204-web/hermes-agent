# P2.C1 RED Receipt

Timestamp: 2026-08-11T00:13:00+07:00 (ICT)
Base: `bc6c801a5734d30543a908c18233728b691e9e9e` (P1.C3 commit)
Lifecycle: P2.C1 RED_PROVEN — TEST BODIES FROZEN

## Governed command

```text
HERMES_PYTHON='/c/c/Users/HieuKa/Desktop/hermes-rewind-archive-20260810/.venv/Scripts/python.exe' scripts/run_tests.sh --file-retries 0 tests/gateway/test_retry_response.py tests/gateway/test_session.py -q
```

## Valid RED result

```text
Discovered 2 files (~151 estimated tests)
tests/gateway/test_retry_response.py: 2 passed / 2 failed
tests/gateway/test_session.py: 145 passed / 2 failed
Summary: 147 passed / 4 failed in 6.2s
```

Only the four new P2.C1 behavior assertions failed:

- `test_retry_archives_latest_suffix_and_resends_without_platform_id`: destructive base rewrite omitted the target-inclusive suffix from rewind recovery.
- `test_retry_failed_persistence_preserves_tokens_and_prevents_resend`: base ignored failed persistence and reset `last_prompt_tokens` to zero.
- `test_rewrite_transcript_failure_preserves_dirty_state`: base cleared dirty transcript custody before the failing destructive write.
- `test_rewind_session_failure_preserves_dirty_state`: base cleared dirty transcript custody before the failing `/undo` rewind.

Discovery, import, fixture setup, and both files' pre-existing tests passed. A prior attempted RED was rejected because the failure fixture omitted `runner.session_store` and produced `AttributeError`; that fixture was corrected before this valid RED receipt.

Synthetic retry refutation control is frozen in the archive test: the requeued `MessageEvent.message_id` must be `None`, so `/retry` remains outside platform-message-id dedupe.

Frozen body record: `P2-C1-TEST-BODY-HASHES.json`.
