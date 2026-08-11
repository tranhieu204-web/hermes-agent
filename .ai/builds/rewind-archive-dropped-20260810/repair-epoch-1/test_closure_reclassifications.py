import json
from pathlib import Path

import pytest


EVIDENCE_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("finding_id", "expected_disposition"),
    [
        ("B-1", "FIXED_ONE_WAY_CANONICAL_PROJECTION"),
        ("B-2", "FIXED_REFUSE_DESTRUCTIVE_EXPORT_WITH_UNCOVERED_REWIND_ROWS"),
        ("B-3", "FIXED_TRANSACTIONAL_UNDO_PARITY"),
        ("B-4", "FIXED_DEAD_GUARD_REMOVED_AND_M03_PROOF_RETIRED"),
        ("B-5", "CORRECTED_NARROWER_ACCEPTED_INPUT_SET_NOT_PRESERVATION"),
        ("M-1", "FIXED_FAIL_CLOSED_AND_ATOMICALLY_RECHECKED"),
        ("MED-5", "FIXED_REWOUND_ROWS_EXCLUDED_COMPACTION_ARCHIVES_RETAINED"),
    ],
)
def test_stage6_overturned_claims_have_explicit_dispositions(
    finding_id, expected_disposition
):
    """Record corrections are acceptance behavior, not optional prose."""
    summary = json.loads(
        (EVIDENCE_ROOT / "BUILD-CLOSURE-SUMMARY.json").read_text(encoding="utf-8")
    )
    epoch = summary.get("stage6_repair_epoch_1")
    assert epoch is not None, "Stage 6 HOLD repair epoch is absent from closure"
    findings = {item["id"]: item for item in epoch.get("findings", [])}
    assert finding_id in findings, f"{finding_id} is absent from Stage 6 dispositions"
    assert findings[finding_id]["disposition"] == expected_disposition

    readme = (EVIDENCE_ROOT / "README.md").read_text(encoding="utf-8")
    ledger = json.loads((EVIDENCE_ROOT / "ledger.json").read_text(encoding="utf-8"))
    assert finding_id in readme
    ledger_epoch = ledger.get("closureRecord", {}).get("stage6RepairEpoch1")
    assert ledger_epoch is not None
    assert any(
        item.get("id") == finding_id
        and item.get("disposition") == expected_disposition
        for item in ledger_epoch.get("findings", [])
    )
