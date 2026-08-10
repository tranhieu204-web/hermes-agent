#!/usr/bin/env python3
"""One-at-a-time P3 mutation runner; always restores source bytes."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[4]
OUT = Path(__file__).resolve().parent


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def mutation(mid, rel, old, new, node, purpose):
    return {
        "id": mid,
        "file": rel,
        "old": old.encode("utf-8"),
        "new": new.encode("utf-8"),
        "node": node,
        "purpose": purpose,
    }


SCOPE_NODE = "tests/hermes_cli/test_sessions_export_rewound.py::test_include_rewound_scope_fence_rejects_broad_or_destructive_combinations"
EXTRA_SCOPE_NODE = "tests/hermes_cli/test_sessions_export_rewound.py::test_include_rewound_rejects_other_broad_or_mode_specific_options"
RAW_NODE = "tests/hermes_cli/test_sessions_export_rewound.py::test_include_rewound_jsonl_exports_raw_bytes_without_conversation_projection"
REQUEST_NODE = "tests/hermes_cli/test_sessions_export_rewound.py::test_include_rewound_resolves_exactly_one_session_and_requests_i4"
DEFAULT_NODE = "tests/hermes_cli/test_sessions_export_rewound.py::test_default_jsonl_export_keeps_existing_active_only_call_contract"
ACP_NODE = "tests/acp/test_session.py::TestListAndCleanup::test_save_session_non_owner_preserves_rewind_rows_when_archive_probe_succeeds"

MUTATIONS = [
    mutation(
        "M01",
        "hermes_cli/main.py",
        "resolved_session_id, include_rewound=True",
        "resolved_session_id, include_rewound=False",
        REQUEST_NODE,
        "I5 must request the I4 recovery view",
    ),
    mutation(
        "M02",
        "hermes_cli/main.py",
        "                if not args.session_id:\n                    recovery_error = \"--include-rewound requires --session-id\"",
        "                if False and not args.session_id:\n                    recovery_error = \"--include-rewound requires --session-id\"",
        SCOPE_NODE,
        "recovery export requires exactly one session",
    ),
    mutation(
        "M03",
        "hermes_cli/main.py",
        "                elif args.format != \"jsonl\":\n                    recovery_error = \"--include-rewound requires --format jsonl\"",
        "                elif False and args.format != \"jsonl\":\n                    recovery_error = \"--include-rewound requires --format jsonl\"",
        SCOPE_NODE,
        "recovery export is JSONL-only",
    ),
    mutation(
        "M04",
        "hermes_cli/main.py",
        "                elif getattr(args, \"only\", None):\n                    recovery_error = \"--include-rewound is incompatible with --only\"",
        "                elif False and getattr(args, \"only\", None):\n                    recovery_error = \"--include-rewound is incompatible with --only\"",
        SCOPE_NODE,
        "prompt-only projection is incompatible",
    ),
    mutation(
        "M05",
        "hermes_cli/main.py",
        "                elif getattr(args, \"lineage\", \"single\") == \"logical\":",
        "                elif False and getattr(args, \"lineage\", \"single\") == \"logical\":",
        SCOPE_NODE,
        "logical-lineage aggregation is incompatible",
    ),
    mutation(
        "M06",
        "hermes_cli/main.py",
        "                elif getattr(args, \"delete_after_verified\", False):",
        "                elif False and getattr(args, \"delete_after_verified\", False):",
        SCOPE_NODE,
        "recovery and whole-session delete cannot be combined",
    ),
    mutation(
        "M07",
        "hermes_cli/main.py",
        "                elif getattr(args, \"redact\", False):\n                    recovery_error = \"--include-rewound is incompatible with --redact\"",
        "                elif False and getattr(args, \"redact\", False):\n                    recovery_error = \"--include-rewound is incompatible with --redact\"",
        SCOPE_NODE,
        "byte-exact recovery cannot redact content",
    ),
    mutation(
        "M08",
        "hermes_cli/main.py",
        "                elif _any_filters:\n                    recovery_error = (",
        "                elif False and _any_filters:\n                    recovery_error = (",
        EXTRA_SCOPE_NODE,
        "session filters cannot broaden or transform recovery",
    ),
    mutation(
        "M09",
        "hermes_cli/main.py",
        "                elif getattr(args, \"dry_run\", False):\n                    recovery_error = \"--include-rewound is incompatible with --dry-run\"",
        "                elif False and getattr(args, \"dry_run\", False):\n                    recovery_error = \"--include-rewound is incompatible with --dry-run\"",
        EXTRA_SCOPE_NODE,
        "dry-run mode cannot shadow recovery execution",
    ),
    mutation(
        "M10",
        "hermes_cli/main.py",
        "                elif any(\n                    getattr(args, name, False)",
        "                elif False and any(\n                    getattr(args, name, False)",
        EXTRA_SCOPE_NODE,
        "trace upload options cannot escape local recovery scope",
    ),
    mutation(
        "M11",
        "hermes_cli/main.py",
        "                        data = db.export_session(resolved_session_id)\n                    data = _redact(data)",
        "                        data = {\"id\": resolved_session_id, \"messages\": []}\n                    data = _redact(data)",
        DEFAULT_NODE,
        "default JSONL export must retain its established active-only DB call",
    ),
    mutation(
        "M12",
        "hermes_state.py",
        "        if include_rewound:\n            messages = [\n                message\n                for message in self.get_messages(session_id, include_inactive=True)\n                if message.get(\"active\") == 1\n                or (\n                    message.get(\"active\") == 0\n                    and message.get(\"compacted\") == 0\n                )\n            ]\n        else:",
        "        if include_rewound:\n            messages = self.get_messages_as_conversation(session_id)\n        else:",
        RAW_NODE,
        "I4 must never read through sanitizing conversation projection",
    ),
    mutation(
        "M13",
        "hermes_state.py",
        "        if include_rewound:\n            messages = [\n                message\n                for message in self.get_messages(session_id, include_inactive=True)\n                if message.get(\"active\") == 1\n                or (\n                    message.get(\"active\") == 0\n                    and message.get(\"compacted\") == 0\n                )\n            ]\n        else:",
        "        if include_rewound:\n            messages = self.get_messages(session_id, include_inactive=True)\n        else:",
        RAW_NODE,
        "I4 must exclude compaction-inactive rows",
    ),
    mutation(
        "M14",
        "hermes_state.py",
        "                if message.get(\"active\") == 1\n                or (",
        "                if False and message.get(\"active\") == 1\n                or (",
        RAW_NODE,
        "I4 must include the active retained prefix exactly once",
    ),
    mutation(
        "M15",
        "acp_adapter/session.py",
        "                    state.session_id, state.history, active_only=has_archived",
        "                    state.session_id, state.history, active_only=False",
        ACP_NODE,
        "successful ACP archive probe must protect inactive rewind rows",
    ),
    mutation(
        "M16",
        "hermes_state.py",
        '                "SELECT 1 FROM messages WHERE session_id = ? AND active = 0 LIMIT 1",',
        '                "SELECT 1 FROM messages WHERE session_id = ? AND active = 0 AND compacted = 1 LIMIT 1",',
        ACP_NODE,
        "ACP archive probe must recognize active=0 compacted=0 rows",
    ),
]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    sources = {rel: (ROOT / rel).read_bytes() for rel in {m["file"] for m in MUTATIONS}}
    results = []
    env = os.environ.copy()
    env.update({"TZ": "UTC", "LANG": "C.UTF-8", "PYTHONHASHSEED": "0"})
    try:
        for item in MUTATIONS:
            path = ROOT / item["file"]
            original = sources[item["file"]]
            assert path.read_bytes() == original, f"pre-mutation drift: {item['file']}"
            assert original.count(item["old"]) == 1, (
                item["id"], item["file"], original.count(item["old"])
            )
            mutated = original.replace(item["old"], item["new"], 1)
            path.write_bytes(mutated)
            started = time.time()
            try:
                completed = subprocess.run(
                    [sys.executable, "-m", "pytest", item["node"], "-q"],
                    cwd=ROOT,
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=90,
                    check=False,
                )
                output = completed.stdout + completed.stderr
            finally:
                path.write_bytes(original)
            restored = path.read_bytes() == original
            rejected_markers = [
                marker
                for marker in (
                    "ERROR collecting",
                    "SyntaxError",
                    "ImportError while importing",
                    "collected 0 items",
                )
                if marker in output
            ]
            named = item["node"].split("::")[-1]
            named_failure = (
                completed.returncode != 0
                and "FAILED" in output
                and named in output
                and "AssertionError" in output
                and not rejected_markers
            )
            output_path = OUT / f"{item['id']}-output.txt"
            clean_output = "\n".join(
                line.rstrip() for line in output.replace("\r\n", "\n").split("\n")
            ).rstrip() + "\n"
            output_path.write_bytes(clean_output.encode("utf-8"))
            result = {
                "id": item["id"],
                "file": item["file"],
                "purpose": item["purpose"],
                "named_test": item["node"],
                "exit_code": completed.returncode,
                "duration_seconds": round(time.time() - started, 3),
                "named_assertion_failure": named_failure,
                "rejected_failure_markers": rejected_markers,
                "source_restored_byte_identical": restored,
                "source_restored_sha256": sha(path.read_bytes()),
                "output_file": str(output_path.relative_to(ROOT)).replace("\\", "/"),
                "output_sha256": sha(output_path.read_bytes()),
            }
            results.append(result)
            print(json.dumps(result, sort_keys=True), flush=True)
            if not named_failure or not restored:
                raise RuntimeError(f"ineffective or unsafe mutation: {item['id']}")
    finally:
        for rel, data in sources.items():
            (ROOT / rel).write_bytes(data)

    manifest = {
        "schema": "sakaan.p3.mutation-manifest.v1",
        "python": sys.version,
        "mutations": results,
        "mutation_count": len(results),
        "all_mutations_failed_named_tests": all(
            r["named_assertion_failure"] for r in results
        ),
        "source_restored_byte_identical": all(
            (ROOT / rel).read_bytes() == data for rel, data in sources.items()
        ),
        "source_restored_sha256": {
            rel: sha((ROOT / rel).read_bytes()) for rel in sorted(sources)
        },
        "restored_gate": None,
    }
    (OUT / "manifest.json").write_bytes(
        (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
    )
    print(json.dumps(manifest, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
