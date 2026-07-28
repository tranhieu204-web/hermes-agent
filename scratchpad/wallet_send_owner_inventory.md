# Wallet-cap send-owner inventory (v2)

Production send inventory reconciled against `guard_wallet_acceptance_contract.md` and the current tree.

| Owner / path | Origin | Wallet seam | Transport boundary |
|---|---|---|---|
| `agent/conversation_loop.py` standard chat loop | operator explicit by default; cron becomes automatic background | `AIAgent._build_api_kwargs` -> `agent.chat_completion_helpers.build_api_kwargs` | transport `create` / stream dispatch |
| `agent/codex_runtime.py::run_codex_app_server_turn` | same parent-turn origin | app-server turn setup currently bypasses `_build_api_kwargs`; tracked as an unresolved v2 hold finding | `CodexAppServerSession.run_turn` |
| `agent/chat_completion_helpers.py::handle_max_iterations` | same parent-turn origin | Codex summary uses `_build_api_kwargs`; direct chat/Anthropic summary branches remain an unresolved v2 hold finding | direct summary transport calls |
| `agent/moa_loop.py::_run_reference` | automatic child | `agent.auxiliary_client.call_llm(task="moa_reference")` | direct auxiliary client create |
| `agent/moa_loop.py::aggregate_moa_context` | automatic child | `agent.auxiliary_client.call_llm(task="moa_aggregator")` | direct auxiliary client create |
| `agent/moa_loop.py::MoAChatCompletions._call_prepared_aggregator` | automatic child | `agent.auxiliary_client.call_llm(task="moa_aggregator")` | direct auxiliary client create / stream |

Known direct retries inside `agent.auxiliary_client.call_llm` reuse the preflighted immutable local route. A fresh immediate pre-send revalidation and per-retry revalidation are not yet present and remain explicit hold findings rather than being represented as complete.
