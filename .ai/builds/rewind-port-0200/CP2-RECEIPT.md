# CP-2 Build Receipt — rewind-port-0200

## Pin and scope

- Workspace: `C:\hrp`
- Branch: `sakaan/rewind-port-0200-20260812`
- Base/HEAD: `ee472a7fdbbc55924f91ab122dbaa29bd07668b0`
- Production/test writers: `hermes_cli/web_routers/sessions.py`, `tests/hermes_cli/test_web_server.py`
- CP-1's six frozen files were not edited during CP-2.

## RED (before production edit)

Exact command:

`C:\hrp\venv\Scripts\python.exe -m pytest tests/hermes_cli/test_web_server.py -k "rewind and (messages or transcript)" -q`

Captured result: exit 1, `3 failed, 145 deselected, 1 warning in 2.41s`.

Behavioral failures:

1. Default >500-row latest response had no truthy `rewind_id` values on its user rows (`assert all(row.get("rewind_id") for row in users)`).
2. The same-profile handle never performed the complete resume-history read (`assert ("history", "profile-session") in db.calls`).
3. A request with a concurrent writer had no minted identity (`assert response.json()["messages"][0].get("rewind_id")`).

No import or collection error occurred. The duplicate identical user-turn assertion was behind the missing-ID assertion and became active on GREEN.

## GREEN

Focused command:

`C:\hrp\venv\Scripts\python.exe -m pytest tests/hermes_cli/test_web_server.py -k "get_session_messages and (rewind or omitted_limit_defaults_to_500 or negative_offset or limit_above_500)" -q`

Observed: `6 passed, 142 deselected, 1 warning in 2.44s`.

Whole CP-2 test file command:

`C:\hrp\venv\Scripts\python.exe -m pytest tests/hermes_cli/test_web_server.py -q`

Observed: `144 passed, 4 skipped, 1 warning in 18.16s`.

Final rewind-focused recheck:

`C:\hrp\venv\Scripts\python.exe -m pytest tests/hermes_cli/test_web_server.py -k "get_session_messages_rewind" -q`

Observed: `3 passed, 145 deselected, 1 warning in 1.18s`.

## Mutation evidence

| Load-bearing behavior broken | Mutation | Observed failure | Restored | Observed pass |
|---|---|---|---|---|
| Tip-window gate | Replaced `if latest_page and offset == 0 and messages and snapshot_started:` with `if messages and snapshot_started:` | `test_get_session_messages_rewind_ids_only_on_proven_tip_windows` failed: annotation spy saw 4 calls instead of 1 (`assert 4 == 1`); unsafe offset/oldest windows reached annotation | Exact original gate restored | Same test: `1 passed, 147 deselected, 1 warning in 1.15s` |
| Fail-closed annotation path | Narrowed `except Exception:` to `except KeyError:` | `test_get_session_messages_rewind_failure_is_unmodified_and_uses_one_profile_db` failed with uncaught `RuntimeError: annotation failed` instead of HTTP 200/raw rows | Exact `except Exception:` restored | Same test: `1 passed, 147 deselected, 1 warning in 0.83s` |

## Production call path and real request proof

Non-test caller chain:

1. Desktop `getLatestSessionMessages` calls `getSessionMessages(..., {limit: 500, order: 'latest'})` at `apps/desktop/src/hermes.ts:710-712`.
2. `getSessionMessages` emits `GET /api/sessions/{id}/messages` at `apps/desktop/src/hermes.ts:679-707`.
3. FastAPI includes `manage_router` at `hermes_cli/web_server.py:11314` and re-exports the handler at `hermes_cli/web_server.py:11315-11324`.
4. The real handler is `hermes_cli/web_routers/sessions.py:604-684`; its same-handle snapshot/read/annotation path is `:618-667`, and CP-1's frozen pure annotator is called at `:648-651`.

A real authenticated Starlette `TestClient` GET traversed the mounted production app/route. The annotation spy in `test_get_session_messages_rewind_ids_only_on_proven_tip_windows` observed exactly one annotation call for the qualifying default request and none for three unsafe windows plus the empty page. The response contained resolving IDs. This proves the mounted request reaches the production annotation site, not a direct unit-only helper.

## Adversarial matrix

| Case | Observed result |
|---|---|
| >500 rows, default GET | 502 stored alternating rows; response stayed latest 500 in chronological order and all 250 returned user rows received proven IDs |
| offset > 0, latest | HTTP 200; no `rewind_id` keys; annotation spy not called |
| explicit oldest | HTTP 200; no `rewind_id` keys; annotation spy not called |
| effective oldest (explicit limit, omitted order) | HTTP 200; no `rewind_id` keys; annotation spy not called |
| empty latest page | HTTP 200, `messages == []`; annotation spy not called |
| annotation failure | HTTP 200 with rows byte-for-value equal to the pre-annotation raw rows |
| same DB/profile handle | One `_open_session_db_for_profile("work", read_only=True)` result served page and resume-history reads |
| concurrent writer between page/history hooks | Explicit read transaction preserved the original tip snapshot; response ID remained minted/resolving rather than desynchronizing |
| duplicate identical user turns | Final two identical user contents received distinct `r2` IDs |
| REST ID resolution | Every returned user ID resolved through CP-1 `resolve_rewind_ordinal` against the same complete sanitized model history; ordinals were exactly `1..250` |
| spine mismatch / inability to prove complete user suffix | Router accepts annotated rows only when every returned user has a non-null ID; otherwise it returns untouched raw rows |

## RF1 and implementation notes

The router opens one profile-aware read-only SessionDB handle and begins an explicit SQLite read transaction on that handle before the page SELECT. `SessionDB(read_only=True)` routes `_read_ctx()` through its shared `_conn`, so both `get_messages` and `get_resume_conversations` execute inside that same snapshot. Failure to begin the snapshot disables annotation. Resume projection is sanitized with the same `agent.replay_cleanup.sanitize_replay_history` used by gateway resume, then passed to CP-1 `annotate_rewind_ids`; no hashing/alignment logic was copied.

## CP-1 non-regression gate

Command:

`C:\hrp\venv\Scripts\python.exe -m pytest tests/test_tui_gateway_server.py -q`

Observed: `550 passed in 38.31s`.

## Diff and line budget

`git diff --numstat -- hermes_cli/web_routers/sessions.py tests/hermes_cli/test_web_server.py`:

- `37  5  hermes_cli/web_routers/sessions.py`
- `159 0  tests/hermes_cli/test_web_server.py`

Net CP-2 line count: `+191` (196 insertions, 5 deletions), below the map's 260 target and 340 hard threshold.

`git diff --name-only`:

- `hermes_cli/web_routers/sessions.py`
- `tests/hermes_cli/test_web_server.py`
- `tests/test_tui_gateway_server.py` (pre-existing frozen CP-1)
- `tui_gateway/compute_host.py` (pre-existing frozen CP-1)
- `tui_gateway/methods_prompt.py` (pre-existing frozen CP-1)
- `tui_gateway/methods_session.py` (pre-existing frozen CP-1)
- `tui_gateway/server.py` (pre-existing frozen CP-1)

Untracked CP-1 `tui_gateway/rewind_identity.py` and `.ai/` do not appear in `git diff --name-only`; both are present in `git status`.

`git diff --check`: exit 0, no output (`DIFF_CHECK_OK`).

## CP-2-scope open-item status

- Profile DB equivalence: CLOSED for CP-2. Profile-aware test proves one explicit `work` handle supplies page and complete resume reads; production opens once with `read_only=True`.
- Non-message REST feeder inventory: CLOSED for CP-2. In `hermes_cli/web_routers/sessions.py`, the other `messages` emissions are session stats aggregation (`:551`) and streamed raw export (`:779`); neither is a Restore-bearing transcript replacement. The only Restore-bearing REST transcript handler is the modified `GET .../messages` response (`:677`).
- Desktop field configuration: OPEN / outside CP-2 ownership. Read-only inspection shows `SessionMessage` at `apps/desktop/src/types/hermes.ts:544-582` has no `rewind_id` field, and current rewind submission in `apps/desktop/src/app/session/hooks/use-prompt-actions/rewind.ts:29-47,62-91` remains ordinal-based. No Desktop file was edited. This does not invalidate REST producer parity but requires a separately owned Desktop consumer update before Desktop can submit REST-minted IDs.
