# P2 Gateway /retry Parity Candidate Receipt

Timestamp: 2026-08-11T00:23:19+07:00 (ICT)
Lifecycle: P2 CANDIDATE_VERIFIED — UNCOMMITTED — STOPPED BEFORE P3
P1.C3 parent commit: `bc6c801a5734d30543a908c18233728b691e9e9e`

## P2.C1 RED

Governed two-file gate on the P1.C3 commit plus only the four new RED tests:

```text
Discovered 2 files (~151 estimated tests)
tests/gateway/test_retry_response.py: 2 passed / 2 failed
tests/gateway/test_session.py: 145 passed / 2 failed
Summary: 147 passed / 4 failed in 6.2s
```

RED receipt SHA-256: `f7e384bb59b682ad2e55b769feb796a76f5ee4f4ac53c164ad05ad682b413d04`
Frozen body record SHA-256: `83579506aa9471853768508c852f01debae5a4f2d1e883ea985732beec1cab70`

## P2.C2 BUILD / GREEN

- Added I3 `SessionStore.rewind_transcript` wrapping I1.
- Gateway `/retry` converts array index to user ordinal, checks I3 result, then resets tokens/resends.
- Failed persistence preserves token state and prevents resend.
- `rewrite_transcript` and `rewind_session` now clear dirty custody only after durable success.
- MED-3 disposition: `DELIBERATELY_DIVERGE`; decision record SHA-256: `d4274fe830bf61b72829bb0985031bd74414032f350fc02460243eaebe6cf2b4`.
- Synthetic retry `MessageEvent.message_id` remains `None`.

Focused P2 gate:

```text
Discovered 2 files (~154 estimated tests)
tests/gateway/test_retry_response.py: 4 passed
tests/gateway/test_session.py: 152 passed
Summary: 156 passed / 0 failed in 6.1s
```

## Mutation

```text
M01-M13: every named guard mutation failed its named test
all_mutations_failed_named_tests=true
source_restored_byte_identical=true
```

Mutation manifest SHA-256: `b9f7fcc48c702b3c80f772ccc36a3cfd5e1404f53809a266e68901d69d5008b1`

## Final governed P1+P2 gate

```text
Discovered 6 files (~1081 estimated tests)
tests/gateway/test_retry_response.py: 4 passed
tests/cli/test_cli_retry.py: 3 passed
tests/gateway/test_dedupe_user_turns.py: 8 passed
tests/gateway/test_session.py: 152 passed
tests/test_hermes_state.py: 494 passed
tests/test_tui_gateway_server.py: 475 passed
Summary: 1136 passed / 0 failed in 131.1s
```

Additional controls:

```text
Gateway retry replacement + compression controls: 14 passed / 0 failed in 2.1s
Yuanbao DB-only recall controls: 2 passed / 0 failed in 1.2s
```

## Frozen P2.C1 function bodies

- `test_retry_archives_latest_suffix_and_resends_without_platform_id`
  - function SHA-256: `692a38e09dcf5cb3915f06edf511a1eaa83f52fc661203a1b327d8230c90be0d`
  - body SHA-256: `8ae0e33b586ad70dc15c5af1ac8732b5681d2178aec54d27e6f817f9346af33b`
  - before/after byte-identical: true
- `test_retry_failed_persistence_preserves_tokens_and_prevents_resend`
  - function SHA-256: `7092296a67748cfd0d2b84ffcc9d73aa26ce7e241bef16bccf13cb9ba932f3a1`
  - body SHA-256: `6262ac42dbde140c1ef57cc1c4cb2b4a4732bc6574ae48dfa82aaf93670a002e`
  - before/after byte-identical: true
- `test_rewrite_transcript_failure_preserves_dirty_state`
  - function SHA-256: `5201f9001edda887c7c214c59a750197bc67f78da87d299990e0fcd66a8fc6de`
  - body SHA-256: `cbb97a9cb7da4be464b03a4c94aa57f78bff383ac7b1588a800c4cdfff41de5f`
  - before/after byte-identical: true
- `test_rewind_session_failure_preserves_dirty_state`
  - function SHA-256: `93035be9e572277e0b1effdcec5d20480b3b8e84dfd71b6362948c40f6157630`
  - body SHA-256: `093a0b97e49fa669879a951b2c4d06862dc19c977eca81b69c78874bd7de7d84`
  - before/after byte-identical: true

## Final source SHA-256

- `gateway/session.py`: `29b447cb6f3b0f54f980b94482ad3f00853de66b36de335b9df8cd6930ab0a40`
- `gateway/slash_commands.py`: `e7e3c3de15308a87e4a128afd2f0a9034d60dae728374c8f6859658804d1a04f`

Forbidden hard-rewrite owners remained byte-identical to P1.C3:

- `gateway/run.py`: `cc972b56b6987394188c2c52af0eee9d1ba639a445ebccd47de12f0d68ba56ff`
- `gateway/platforms/yuanbao.py`: `81f97e6ef01a3104b027e2dc0c16ca41327fa284ccd31a19fdbee677c6ed11eb`

No P2 commit, staging, push, merge, deployment, activation, trade, or P3 work occurred.
