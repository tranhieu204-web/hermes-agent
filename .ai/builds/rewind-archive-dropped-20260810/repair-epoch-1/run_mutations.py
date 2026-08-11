"""Repair-epoch mutation campaign. Mutate one guard, run its named test, restore exact bytes."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
EVIDENCE = ROOT / "tmp" / "rewind-m14-m16-mutation-campaign"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
BASE_COMMIT = "389640f70679257b7acf074808e44277f31fb92a"
PROPERTY_TEST_BODY_SHA256 = (
    "84e33c30dd8db64a36635b98145b1c58a5c0e0ffca24bdf3692da5bbca0f74ae"
)


@dataclass(frozen=True)
class Mutation:
    mutation_id: str
    path: str
    old: str
    new: str
    selector: str
    replace_all: bool = False


MUTATIONS = [
    Mutation(
        "M01_B1_REMOVE_STRING_REPLAY_PROJECTION",
        "hermes_state.py",
        "                    value = sanitize_context(value).strip()",
        "                    value = value",
        "tests/test_hermes_state.py::TestRewindActiveHistory::test_rewind_history_uses_projected_content_as_canonical_without_masking_drift",
    ),
    Mutation(
        "M02_B1_NORMALIZE_EXPECTED_SIDE_TOO",
        "hermes_state.py",
        "            if field == \"content\" and persisted and value is not cls._REWIND_MISSING:",
        "            if field == \"content\" and value is not cls._REWIND_MISSING:",
        "tests/test_hermes_state.py::TestRewindActiveHistory::test_rewind_history_uses_projected_content_as_canonical_without_masking_drift",
    ),
    Mutation(
        "M03_B1_STRINGIFY_STRUCTURED_CONTENT",
        "hermes_state.py",
        "                value = cls._decode_content(value)\n                if (",
        "                decoded_value = cls._decode_content(value)\n                value = (\n                    json.dumps(decoded_value, sort_keys=True)\n                    if isinstance(decoded_value, (dict, list))\n                    else decoded_value\n                )\n                if (",
        "tests/test_hermes_state.py::TestRewindActiveHistory::test_archives_suffix_repairs_counters_and_preserves_exact_rows",
    ),
    Mutation(
        "M04_B1_DROP_PERSISTED_API_CONTENT_AUTHORITY",
        "hermes_state.py",
        "            else:\n                value = message.get(field, cls._REWIND_MISSING)\n\n            if field == \"content\"",
        "            else:\n                value = (\n                    None\n                    if persisted and field == \"api_content\"\n                    else message.get(field, cls._REWIND_MISSING)\n                )\n\n            if field == \"content\"",
        "tests/test_hermes_state.py::TestRewindActiveHistory::test_archives_suffix_repairs_counters_and_preserves_exact_rows",
    ),
    Mutation(
        "M05_B2_BYPASS_UNCOVERED_REWIND_DELETE_GUARD",
        "hermes_cli/main.py",
        "                            if db.has_rewound_messages(target_id)",
        "                            if False and db.has_rewound_messages(target_id)",
        "tests/hermes_cli/test_sessions_export_md_cli.py::test_delete_after_verified_refuses_rewind_rows_missing_from_export",
    ),
    Mutation(
        "M06_B3_RESTORE_PRETRANSACTION_MESSAGE_ID_PATH",
        "gateway/session.py",
        "            result = self._db.rewind_recent_user_turns(session_id, n)",
        "            result = self._db.rewind_to_message(session_id, n)",
        "tests/gateway/test_session.py::TestGatewaySessionDbRecovery::test_rewind_session_selects_count_coordinate_inside_durable_transaction",
    ),
    Mutation(
        "M07_B3_DISABLE_COMPRESSION_GUARD",
        "hermes_state.py",
        "            if (\n                session is not None\n                and session[\"ended_at\"] is not None\n                and session[\"end_reason\"] == \"compression\"\n            ):\n                raise CompressionSessionClosedError(session_id)\n\n            user_rows =",
        "            if False:\n                raise CompressionSessionClosedError(session_id)\n\n            user_rows =",
        "tests/gateway/test_session.py::TestGatewaySessionDbRecovery::test_rewind_session_refuses_compression_closed_session",
    ),
    Mutation(
        "M08_B3_CORRUPT_COUNTER_RECONCILIATION",
        "hermes_state.py",
        "            target_row = user_rows[-1]\n            target_message = self._decode_rewind_target_row(target_row)\n            archive_cursor = conn.execute(\n                \"UPDATE messages SET active = 0, compacted = 0 \"\n                \"WHERE session_id = ? AND active = 1 AND id >= ?\",\n                (session_id, target_row[\"id\"]),\n            )\n            remaining_rows = conn.execute(\n                \"SELECT id, tool_calls FROM messages \"\n                \"WHERE session_id = ? AND active = 1 ORDER BY id\",\n                (session_id,),\n            ).fetchall()\n            message_count = len(remaining_rows)\n            tool_call_count = self._rewind_tool_call_count(list(remaining_rows))\n            conn.execute(\n                \"UPDATE sessions SET message_count = ?, tool_call_count = ?, \"\n                \"rewind_count = COALESCE(rewind_count, 0) + 1 WHERE id = ?\",\n                (message_count, tool_call_count, session_id),\n            )\n            head_row =",
        "            target_row = user_rows[-1]\n            target_message = self._decode_rewind_target_row(target_row)\n            archive_cursor = conn.execute(\n                \"UPDATE messages SET active = 0, compacted = 0 \"\n                \"WHERE session_id = ? AND active = 1 AND id >= ?\",\n                (session_id, target_row[\"id\"]),\n            )\n            remaining_rows = conn.execute(\n                \"SELECT id, tool_calls FROM messages \"\n                \"WHERE session_id = ? AND active = 1 ORDER BY id\",\n                (session_id,),\n            ).fetchall()\n            message_count = len(remaining_rows)\n            tool_call_count = self._rewind_tool_call_count(list(remaining_rows))\n            conn.execute(\n                \"UPDATE sessions SET message_count = ?, tool_call_count = ?, \"\n                \"rewind_count = COALESCE(rewind_count, 0) + 1 WHERE id = ?\",\n                (message_count + 1, tool_call_count, session_id),\n            )\n            head_row =",
        "tests/gateway/test_session.py::TestGatewaySessionDbRecovery::test_rewind_session_repairs_live_counters",
    ),
    Mutation(
        "M09_M1_DISABLE_ATOMIC_ARCHIVE_RECHECK",
        "acp_adapter/session.py",
        "                    preserve_archived=True,",
        "                    preserve_archived=False,",
        "tests/acp/test_session.py::TestListAndCleanup::test_save_session_archive_guard_is_fail_closed_and_atomically_rechecked",
    ),
    Mutation(
        "M10_B4_REINTRODUCE_DEAD_ROUND_TRIP_GUARD",
        "hermes_state.py",
        "    return sum(\n        1\n        for message in expected_history[:last_user_array_index]\n        if isinstance(message, dict) and message.get(\"role\") == \"user\"\n    )",
        "    ordinal = sum(\n        1\n        for message in expected_history[:last_user_array_index]\n        if isinstance(message, dict) and message.get(\"role\") == \"user\"\n    )\n    user_indices = [\n        index\n        for index, message in enumerate(expected_history)\n        if isinstance(message, dict) and message.get(\"role\") == \"user\"\n    ]\n    if user_indices[ordinal] != last_user_array_index:\n        raise RewindHistoryConflict(\n            \"<retry-coordinate>\", \"retry coordinate round-trip failed\"\n        )\n    return ordinal",
        "tests/test_hermes_state.py::TestRetryCoordinateConversion::test_retry_coordinate_has_no_dead_round_trip_guard",
    ),
    Mutation(
        "M11_MED5_REMOVE_REWIND_VISIBILITY_FILTERS",
        "agent/insights.py",
        "                     AND (m.active = 1 OR m.compacted = 1)\n",
        "",
        "tests/agent/test_insights.py::TestInsightsPopulated::test_insights_excludes_rewound_rows_without_excluding_compaction_archives",
        replace_all=True,
    ),
    Mutation(
        "M12_MED5_REMOVE_MESSAGE_STATS_VISIBILITY_FILTERS",
        "agent/insights.py",
        "                     AND (m.active = 1 OR m.compacted = 1)\"\"\"",
        "\"\"\"",
        "tests/agent/test_insights.py::TestInsightsPopulated::test_insights_excludes_rewound_rows_without_excluding_compaction_archives",
        replace_all=True,
    ),
    Mutation(
        "M13_WIDEN_ACCEPTANCE_CLASS_SYMMETRIC_FOLD",
        "hermes_state.py",
        "            return json.dumps(\n"
        "                projected,\n"
        "                ensure_ascii=False,\n"
        "                sort_keys=True,\n"
        "                separators=(\",\", \":\"),\n"
        "                allow_nan=False,\n"
        "            ).encode(\"utf-8\")",
        "            return json.dumps(\n"
        "                projected,\n"
        "                ensure_ascii=False,\n"
        "                sort_keys=True,\n"
        "                separators=(\",\", \":\"),\n"
        "                allow_nan=False,\n"
        "            ).encode(\"utf-8\").lower()",
        "tests/test_hermes_state.py::TestRewindActiveHistory::test_rewind_guard_rejects_every_independent_reference_projection_mismatch",
    ),
    Mutation(
        "M14_DROP_ROLE_FROM_REWIND_PROJECTION",
        "hermes_state.py",
        "        for field in cls._REWIND_SEMANTIC_FIELDS:",
        "        for field in (\n"
        "            item for item in cls._REWIND_SEMANTIC_FIELDS if item != \"role\"\n"
        "        ):",
        "tests/test_hermes_state.py::TestRewindActiveHistory::test_rewind_guard_rejects_every_independent_reference_projection_mismatch",
    ),
    Mutation(
        "M15_DROP_REWIND_JSON_FIELDS_FROM_PROJECTION",
        "hermes_state.py",
        "        for field in cls._REWIND_SEMANTIC_FIELDS:",
        "        for field in (\n"
        "            item\n"
        "            for item in cls._REWIND_SEMANTIC_FIELDS\n"
        "            if item not in cls._REWIND_JSON_FIELDS\n"
        "        ):",
        "tests/test_hermes_state.py::TestRewindActiveHistory::test_rewind_guard_rejects_every_independent_reference_projection_mismatch",
    ),
    Mutation(
        "M16_MAKE_REWIND_COMPARISON_ORDER_INSENSITIVE",
        "hermes_state.py",
        "        try:\n"
        "            return json.dumps(\n"
        "                projected,",
        "        try:\n"
        "            projected = sorted(\n"
        "                projected,\n"
        "                key=lambda item: json.dumps(\n"
        "                    item,\n"
        "                    ensure_ascii=False,\n"
        "                    sort_keys=True,\n"
        "                    separators=(\",\", \":\"),\n"
        "                    allow_nan=False,\n"
        "                ),\n"
        "            )\n"
        "            return json.dumps(\n"
        "                projected,",
        "tests/test_hermes_state.py::TestRewindActiveHistory::test_rewind_guard_rejects_every_independent_reference_projection_mismatch",
    ),
]

RESTORED_SELECTORS = sorted({mutation.selector for mutation in MUTATIONS})
INVALID_FAILURE_MARKERS = (
    "ERROR collecting",
    "found no collectors",
    "no tests ran",
    "SyntaxError",
    "ImportError while importing test module",
)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def receipt_bytes(text: str) -> bytes:
    """Stable UTF-8/LF evidence without pytest's presentation-only trailing spaces."""
    normalized = "\n".join(line.rstrip(" \t\r") for line in text.splitlines())
    if text.endswith(("\n", "\r")):
        normalized += "\n"
    return normalized.encode("utf-8")


def run_pytest(selectors: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(PYTHON), "-m", "pytest", "-q", *selectors],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )


def main() -> int:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    touched = sorted({mutation.path for mutation in MUTATIONS})
    originals = {path: (ROOT / path).read_bytes() for path in touched}
    baseline_sha = {path: sha256(data) for path, data in originals.items()}
    results = []

    try:
        for mutation in MUTATIONS:
            path = ROOT / mutation.path
            original = originals[mutation.path]
            old = mutation.old.encode("utf-8")
            new = mutation.new.encode("utf-8")
            occurrence_count = original.count(old)
            expected_count = occurrence_count if mutation.replace_all else 1
            if occurrence_count < 1 or (not mutation.replace_all and occurrence_count != 1):
                raise RuntimeError(
                    f"{mutation.mutation_id}: expected unique source anchor, found {occurrence_count}"
                )
            mutated = original.replace(old, new) if mutation.replace_all else original.replace(old, new, 1)
            path.write_bytes(mutated)
            output_path = EVIDENCE / f"{mutation.mutation_id}.txt"
            try:
                completed = run_pytest([mutation.selector])
                output_path.write_bytes(receipt_bytes(completed.stdout))
            finally:
                path.write_bytes(original)
            restored_sha = sha256(path.read_bytes())
            named_failure = (
                completed.returncode == 1
                and "FAILED" in completed.stdout
                and mutation.selector.split("::")[-1].split("[")[0] in completed.stdout
                and not any(marker in completed.stdout for marker in INVALID_FAILURE_MARKERS)
            )
            result = {
                "id": mutation.mutation_id,
                "file": mutation.path,
                "selector": mutation.selector,
                "replacement_occurrences": expected_count,
                "mutated_exit_code": completed.returncode,
                "named_test_failed": named_failure,
                "output": str(output_path.relative_to(ROOT)).replace("\\", "/"),
                "output_sha256": sha256(output_path.read_bytes()),
                "source_restored_sha256": restored_sha,
                "source_restored_byte_identical": restored_sha == baseline_sha[mutation.path],
                "output_byte_policy": "PYTHON_SUBPROCESS_TEXT_CAPTURE_UTF8_LF_RSTRIP_PRESENTATION_WHITESPACE",
            }
            results.append(result)
            print(
                f"{mutation.mutation_id}: exit={completed.returncode} "
                f"named_failure={named_failure} restored={result['source_restored_byte_identical']}"
            )
            if not named_failure or not result["source_restored_byte_identical"]:
                raise RuntimeError(f"mutation evidence failed: {mutation.mutation_id}")

        restored = run_pytest(RESTORED_SELECTORS)
        restored_path = EVIDENCE / "RESTORED-FOCUSED-GREEN.txt"
        restored_path.write_bytes(receipt_bytes(restored.stdout))
        if restored.returncode != 0:
            raise RuntimeError("restored focused gate failed")

        final_sha = {path: sha256((ROOT / path).read_bytes()) for path in touched}
        all_restored = final_sha == baseline_sha
        manifest = {
            "schema": "rewind-m13-m16-assurance-mutation-manifest-v1",
            "base_commit": BASE_COMMIT,
            "campaign_scope": (
                "stage6 repair guards plus M13 content/api_content, M14 role, "
                "M15 rewind JSON-field and M16 history-order widening fences"
            ),
            "property_test_body_sha256": PROPERTY_TEST_BODY_SHA256,
            "widening_mutations": [
                "M13_WIDEN_ACCEPTANCE_CLASS_SYMMETRIC_FOLD",
                "M14_DROP_ROLE_FROM_REWIND_PROJECTION",
                "M15_DROP_REWIND_JSON_FIELDS_FROM_PROJECTION",
                "M16_MAKE_REWIND_COMPARISON_ORDER_INSENSITIVE",
            ],
            "evidence_custody": "GITIGNORED_LOCAL_TMP_NOT_COMMITTED",
            "mutation_count": len(results),
            "all_mutations_failed_named_tests": all(
                result["named_test_failed"] for result in results
            ),
            "all_sources_restored_byte_identical": all_restored,
            "receipt_byte_policy": "PYTHON_SUBPROCESS_TEXT_CAPTURE_UTF8_LF_RSTRIP_PRESENTATION_WHITESPACE",
            "per_file_source_baseline_sha256": baseline_sha,
            "per_file_source_restored_sha256": final_sha,
            "mutations": results,
            "restored_focused_gate": {
                "selectors": RESTORED_SELECTORS,
                "exit_code": restored.returncode,
                "output": str(restored_path.relative_to(ROOT)).replace("\\", "/"),
                "output_sha256": sha256(restored_path.read_bytes()),
            },
        }
        manifest_path = EVIDENCE / "mutation-manifest.json"
        manifest_path.write_bytes(
            (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
        )
        print(
            json.dumps(
                {
                    "manifest": str(manifest_path.relative_to(ROOT)).replace("\\", "/"),
                    "manifest_sha256": sha256(manifest_path.read_bytes()),
                    "mutation_count": len(results),
                    "all_mutations_failed_named_tests": manifest[
                        "all_mutations_failed_named_tests"
                    ],
                    "all_sources_restored_byte_identical": all_restored,
                    "restored_focused_exit_code": restored.returncode,
                },
                indent=2,
            )
        )
        return 0
    finally:
        for path, data in originals.items():
            (ROOT / path).write_bytes(data)


if __name__ == "__main__":
    raise SystemExit(main())
