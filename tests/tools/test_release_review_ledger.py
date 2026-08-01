import json
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from tools.release_review_ledger import ReleaseReviewLedger, canonical_effective_route_identity
from tools.release_review_launch import (
    _MATERIAL_LAUNCH_CAPABILITY,
    launch_async_review,
    launch_material_async_review as _public_material_launch,
    launch_shell_review,
)


def launch_material_async_review(*args, **kwargs):
    """Trusted test-only helper for the otherwise sealed ingress API."""
    kwargs["_capability"] = _MATERIAL_LAUNCH_CAPABILITY
    return _public_material_launch(*args, **kwargs)


def _preflight():
    ready = {"status": "verified", "evidence": "captured"}
    return {
        "target": dict(ready),
        "install": dict(ready),
        "restart": dict(ready),
        "rollback": dict(ready),
        "health": {**ready, "authenticated": True, "method": "token probe", "endpoint": "/health"},
    }


def _request(**overrides):
    request = {
        "candidate_hash": "a" * 64,
        "scope": "runtime and tests",
        "lane": "codex",
        "model": "m",
        "prompt": "p",
        "deadline_seconds": 60,
        "output_path": "out.json",
        "environment_fingerprint": "test-environment",
        "evidence_fingerprint": "test-evidence",
        "effective_route_identity": "route-a",
        "review_lens": "runtime",
    }
    request.update(overrides)
    return request


def _recovery_packet(**overrides):
    packet = {
        "schema_version": 1, "candidate_hash": "a" * 64, "environment_fingerprint": "env-a",
        "normalized_scope": "release tests", "failure_fingerprint": "failed:test_one",
        "normalized_task": "reproduce test one", "failed_set": ["test_one"],
        "reproducer": ["pytest", "test_one"], "versions": {"python": "3.12"},
        "attempted_remedy_hash": "none", "verified_facts": ["reproduced"],
        "unresolved_questions": ["why"], "quarantined": [], "redaction_attestation": True,
    }
    packet.update(overrides)
    return packet


def _admit_recovery(ledger, packet_hash, **overrides):
    args = {
        "packet_hash": packet_hash, "candidate_hash": "a" * 64, "environment_fingerprint": "env-a",
        "normalized_scope": "release tests", "failure_fingerprint": "failed:test_one",
        "normalized_task": "reproduce test one", "mode": "STANDARD", "ordinal": 1,
        "owner": "codex", "effective_route_identity": "route-a", "lens": "runtime",
    }
    args.update(overrides)
    return ledger.admit_recovery_attempt(**args)


def test_same_review_returns_existing_receipt(tmp_path):
    ledger = ReleaseReviewLedger(tmp_path / "reviews.db")
    first = ledger.admit(**_request(scope=" runtime   and tests "))
    second = ledger.admit(**_request())
    assert first["status"] == "admitted"
    assert second["status"] == "existing"
    assert second["receipt_id"] == first["receipt_id"]


def test_recovery_attempt_requires_packet_then_fences_terminal_handoff(tmp_path):
    ledger = ReleaseReviewLedger(tmp_path / "reviews.db")
    packet = ledger.record_recovery_packet(_recovery_packet())
    first = _admit_recovery(ledger, packet["packet_hash"])
    duplicate = _admit_recovery(ledger, packet["packet_hash"])
    assert first["status"] == "admitted"
    assert duplicate["status"] == "existing"
    ledger.transition_recovery_attempt(first["attempt_id"], fence_token=first["fence_token"], state="ACCEPTED")
    ledger.transition_recovery_attempt(first["attempt_id"], fence_token=first["fence_token"], state="RUNNING")
    ledger.transition_recovery_attempt(first["attempt_id"], fence_token=first["fence_token"], state="FAILED", outcome="unchanged")
    second = _admit_recovery(
        ledger, packet["packet_hash"], ordinal=2, owner="claude", effective_route_identity="route-b",
        lens="contract", predecessor_attempt_id=first["attempt_id"],
    )
    assert second["status"] == "admitted"
    with pytest.raises(RuntimeError, match="state or fence"):
        ledger.transition_recovery_attempt(first["attempt_id"], fence_token=first["fence_token"] + 1, state="COMMITTED")


def test_recovery_protocol_requires_serial_standard_then_deep(tmp_path):
    ledger = ReleaseReviewLedger(tmp_path / "reviews.db")
    packet = ledger.record_recovery_packet(_recovery_packet())
    first = _admit_recovery(ledger, packet["packet_hash"])
    with pytest.raises(RuntimeError, match="immediately prior failed"):
        _admit_recovery(ledger, packet["packet_hash"], ordinal=2, owner="claude", effective_route_identity="route-b", lens="contract")
    ledger.transition_recovery_attempt(first["attempt_id"], fence_token=first["fence_token"], state="ACCEPTED")
    ledger.transition_recovery_attempt(first["attempt_id"], fence_token=first["fence_token"], state="RUNNING")
    ledger.transition_recovery_attempt(first["attempt_id"], fence_token=first["fence_token"], state="FAILED")
    second = _admit_recovery(
        ledger, packet["packet_hash"], ordinal=2, owner="claude", effective_route_identity="route-b",
        lens="contract", predecessor_attempt_id=first["attempt_id"],
    )
    for attempt in (second,):
        ledger.transition_recovery_attempt(attempt["attempt_id"], fence_token=attempt["fence_token"], state="ACCEPTED")
        ledger.transition_recovery_attempt(attempt["attempt_id"], fence_token=attempt["fence_token"], state="RUNNING")
        ledger.transition_recovery_attempt(attempt["attempt_id"], fence_token=attempt["fence_token"], state="FAILED")
    third = _admit_recovery(
        ledger, packet["packet_hash"], ordinal=3, owner="grok", effective_route_identity="route-c",
        lens="security", predecessor_attempt_id=second["attempt_id"],
    )
    ledger.transition_recovery_attempt(third["attempt_id"], fence_token=third["fence_token"], state="ACCEPTED")
    ledger.transition_recovery_attempt(third["attempt_id"], fence_token=third["fence_token"], state="RUNNING")
    ledger.transition_recovery_attempt(third["attempt_id"], fence_token=third["fence_token"], state="FAILED")
    deep = _admit_recovery(
        ledger, packet["packet_hash"], mode="DEEP", ordinal=1, owner="hermes", effective_route_identity="route-d",
        lens="reconciliation",
    )
    assert deep["status"] == "admitted"
    parallel_deep = _admit_recovery(
        ledger, packet["packet_hash"], mode="DEEP", ordinal=1, owner="claude", effective_route_identity="route-e",
        lens="dependency",
    )
    assert parallel_deep["status"] == "admitted"
    with pytest.raises(ValueError, match="invalid"):
        _admit_recovery(ledger, packet["packet_hash"], mode="DEEP", ordinal=4, owner="codex", effective_route_identity="route-f", lens="runtime")


def test_deep_capacity_is_bounded_and_never_fabricates_quorum(tmp_path):
    ledger = ReleaseReviewLedger(tmp_path / "reviews.db")
    base = {
        "candidate_hash": "a" * 64, "environment_fingerprint": "env-a",
        "failure_fingerprint": "failed:test_one", "configured_concurrency": 4,
    }
    lanes = [
        {"lane": "codex", "effective_route_identity": "route-a", "lens": "runtime", "available": True, "token_cost": 1},
        {"lane": "alias", "effective_route_identity": "route-a", "lens": "runtime", "available": True, "token_cost": 1},
        {"lane": "claude", "effective_route_identity": "route-b", "lens": "security", "available": False, "token_cost": 1},
    ]
    degraded = ledger.admit_deep_capacity(**base, ordinal=1, lanes=lanes, token_budget=3)
    assert degraded["status"] == "DEGRADED_ROUTE_CAPACITY"
    assert [lane["lane"] for lane in degraded["selected"]] == ["codex"]
    admitted = ledger.admit_deep_capacity(
        **base, ordinal=1, token_budget=3,
        lanes=lanes + [{"lane": "grok", "effective_route_identity": "route-c", "lens": "dependency", "available": True, "token_cost": 1}],
    )
    assert admitted["status"] == "ADMITTED"
    deep_three = ledger.admit_deep_capacity(
        **base, ordinal=3, token_budget=2,
        lanes=[
            {"lane": "a", "effective_route_identity": "route-a", "lens": "runtime", "available": True, "token_cost": 1},
            {"lane": "b", "effective_route_identity": "route-b", "lens": "security", "available": True, "token_cost": 1},
            {"lane": "c", "effective_route_identity": "route-c", "lens": "dependency", "available": True, "token_cost": 1},
        ],
    )
    assert deep_three["status"] == "DEGRADED_ROUTE_CAPACITY"


def test_deep_three_terminal_stop_prevents_an_automatic_new_cycle(tmp_path):
    ledger = ReleaseReviewLedger(tmp_path / "reviews.db")
    packet = ledger.record_recovery_packet(_recovery_packet())
    first = _admit_recovery(ledger, packet["packet_hash"])
    for ordinal, prior, owner, route, lens in (
        (1, first, "codex", "route-a", "runtime"),
    ):
        ledger.transition_recovery_attempt(prior["attempt_id"], fence_token=prior["fence_token"], state="ACCEPTED")
        ledger.transition_recovery_attempt(prior["attempt_id"], fence_token=prior["fence_token"], state="RUNNING")
        ledger.transition_recovery_attempt(prior["attempt_id"], fence_token=prior["fence_token"], state="FAILED")
    second = _admit_recovery(ledger, packet["packet_hash"], ordinal=2, owner="claude", effective_route_identity="route-b", lens="contract", predecessor_attempt_id=first["attempt_id"])
    ledger.transition_recovery_attempt(second["attempt_id"], fence_token=second["fence_token"], state="ACCEPTED")
    ledger.transition_recovery_attempt(second["attempt_id"], fence_token=second["fence_token"], state="RUNNING")
    ledger.transition_recovery_attempt(second["attempt_id"], fence_token=second["fence_token"], state="FAILED")
    third = _admit_recovery(ledger, packet["packet_hash"], ordinal=3, owner="grok", effective_route_identity="route-c", lens="security", predecessor_attempt_id=second["attempt_id"])
    ledger.transition_recovery_attempt(third["attempt_id"], fence_token=third["fence_token"], state="ACCEPTED")
    ledger.transition_recovery_attempt(third["attempt_id"], fence_token=third["fence_token"], state="RUNNING")
    ledger.transition_recovery_attempt(third["attempt_id"], fence_token=third["fence_token"], state="FAILED")
    deep_one = _admit_recovery(ledger, packet["packet_hash"], mode="DEEP", ordinal=1, owner="codex", effective_route_identity="route-d", lens="runtime")
    for attempt in (deep_one,):
        ledger.transition_recovery_attempt(attempt["attempt_id"], fence_token=attempt["fence_token"], state="ACCEPTED")
        ledger.transition_recovery_attempt(attempt["attempt_id"], fence_token=attempt["fence_token"], state="RUNNING")
        ledger.transition_recovery_attempt(attempt["attempt_id"], fence_token=attempt["fence_token"], state="FAILED")
    deep_two = _admit_recovery(ledger, packet["packet_hash"], mode="DEEP", ordinal=2, owner="claude", effective_route_identity="route-e", lens="contract", predecessor_attempt_id=deep_one["attempt_id"])
    ledger.transition_recovery_attempt(deep_two["attempt_id"], fence_token=deep_two["fence_token"], state="ACCEPTED")
    ledger.transition_recovery_attempt(deep_two["attempt_id"], fence_token=deep_two["fence_token"], state="RUNNING")
    ledger.transition_recovery_attempt(deep_two["attempt_id"], fence_token=deep_two["fence_token"], state="FAILED")
    deep_three = _admit_recovery(ledger, packet["packet_hash"], mode="DEEP", ordinal=3, owner="grok", effective_route_identity="route-f", lens="security", predecessor_attempt_id=deep_two["attempt_id"])
    ledger.transition_recovery_attempt(deep_three["attempt_id"], fence_token=deep_three["fence_token"], state="ACCEPTED")
    ledger.transition_recovery_attempt(deep_three["attempt_id"], fence_token=deep_three["fence_token"], state="RUNNING")
    ledger.transition_recovery_attempt(deep_three["attempt_id"], fence_token=deep_three["fence_token"], state="FAILED")
    stopped = ledger.stop_and_report_after_deep_three(
        candidate_hash="a" * 64, environment_fingerprint="env-a", normalized_scope="release tests",
        failure_fingerprint="failed:test_one", normalized_task="reproduce test one", generation=0, reason="exhausted",
    )
    assert stopped["status"] == "STOP_AND_REPORT"
    with pytest.raises(RuntimeError, match="STOP_AND_REPORT"):
        _admit_recovery(ledger, packet["packet_hash"], generation=0, owner="other", effective_route_identity="route-z", lens="other")


def test_recovery_packet_mismatch_and_sensitive_fields_fail_closed(tmp_path):
    ledger = ReleaseReviewLedger(tmp_path / "reviews.db")
    with pytest.raises(ValueError, match="forbidden"):
        ledger.record_recovery_packet(_recovery_packet(api_key="secret"))
    packet = ledger.record_recovery_packet(_recovery_packet())
    with pytest.raises(RuntimeError, match="does not match"):
        _admit_recovery(ledger, packet["packet_hash"], environment_fingerprint="env-b")
    with pytest.raises(ValueError, match="forbidden"):
        ledger.record_recovery_packet(_recovery_packet(verified_facts=[{"nested": {"api-key": "secret"}}]))


def test_material_review_requires_current_fence_and_rejects_generic_batch(tmp_path, monkeypatch):
    from tools import async_delegation as ad

    hermes_home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    ad._reset_for_tests()
    try:
        ledger = ReleaseReviewLedger(hermes_home / "release-review-ledger.db")
        packet = ledger.record_recovery_packet(_recovery_packet())
        attempt = _admit_recovery(ledger, packet["packet_hash"])
        request = _request(environment_fingerprint="env-a", scope="release tests", preflight=_preflight())
        blocked = launch_material_async_review(
            ledger, attempt_id=attempt["attempt_id"], fence_token=attempt["fence_token"],
            dispatch_kwargs={"goals": ["same review"]}, **request,
        )
        assert blocked["status"] == "rejected"
        result = launch_material_async_review(
            ledger, attempt_id=attempt["attempt_id"], fence_token=attempt["fence_token"],
            dispatch_kwargs={
                "goal": "read-only material review", "context": "candidate", "toolsets": None,
                "role": "reviewer", "model": "m", "session_key": "test",
                "runner": lambda: {"status": "completed", "summary": "done"},
                "interrupt_fn": lambda: None, "max_async_children": 1,
            },
            **request,
        )
        assert result["status"] == "launched"
        with sqlite3.connect(hermes_home / "state.db") as conn:
            candidate, fence = conn.execute(
                "SELECT candidate_hash, submission_fence FROM async_delegations WHERE delegation_id=?",
                (result["dispatch"]["delegation_id"],),
            ).fetchone()
        assert candidate == "a" * 64
        assert fence == attempt["fence_token"]
        deadline = time.monotonic() + 2
        while ad.active_count() and time.monotonic() < deadline:
            time.sleep(0.01)
        with sqlite3.connect(hermes_home / "release-review-ledger.db") as conn:
            assert conn.execute(
                "SELECT state FROM workflow_recovery_attempts WHERE attempt_id=?", (attempt["attempt_id"],)
            ).fetchone()[0] == "COMMITTED"
    finally:
        deadline = time.monotonic() + 2
        while ad.active_count() and time.monotonic() < deadline:
            time.sleep(0.01)
        ad._reset_for_tests()


def test_fleet_material_plan_is_atomically_bound_before_async_submission(tmp_path, monkeypatch):
    """A material route plan, claim, and outbox share one candidate/fence boundary."""
    from tools import async_delegation as ad

    hermes_home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    ad._reset_for_tests()
    try:
        ledger = ReleaseReviewLedger(hermes_home / "release-review-ledger.db")
        packet = ledger.record_recovery_packet(_recovery_packet())
        attempt = _admit_recovery(ledger, packet["packet_hash"])
        route_plan = {
            "requested": 2,
            "degraded_route_capacity": True,
            "selected": {
                "lane_id": "codex", "effective_execution_identity": "route-a",
                "review_lens": "runtime", "provider_id": "openai", "model_id": "gpt",
            },
            "unavailable": [{"lane_id": "alias", "reason": "duplicate_route"}],
        }
        result = ledger.admit_fleet_material_launch(
            attempt_id=attempt["attempt_id"],
            fence_token=attempt["fence_token"],
            route_plan=route_plan,
            **_request(environment_fingerprint="env-a", scope="release tests", preflight=_preflight()),
        )
        assert result["status"] == "admitted"
        with sqlite3.connect(hermes_home / "release-review-ledger.db") as conn:
            row = conn.execute(
                "SELECT attempt_id, fence_token, route_identity, review_lens, plan_json FROM workflow_material_route_plans"
            ).fetchone()
        assert row[:4] == (attempt["attempt_id"], attempt["fence_token"], "route-a", "runtime")
        assert json.loads(row[4])["unavailable"][0]["reason"] == "duplicate_route"
    finally:
        ad._reset_for_tests()


def test_material_terminal_saga_commits_receipt_plan_and_recovery_attempt_together(tmp_path):
    ledger = ReleaseReviewLedger(tmp_path / "reviews.db")
    packet = ledger.record_recovery_packet(_recovery_packet())
    attempt = _admit_recovery(ledger, packet["packet_hash"])
    route_plan = {
        "requested": 1,
        "degraded_route_capacity": False,
        "selected": {
            "lane_id": "codex", "effective_execution_identity": "route-a",
            "review_lens": "runtime", "provider_id": "openai", "model_id": "gpt",
        },
        "unavailable": [],
    }
    receipt = ledger.admit_fleet_material_launch(
        attempt_id=attempt["attempt_id"], fence_token=attempt["fence_token"], route_plan=route_plan,
        **_request(environment_fingerprint="env-a", scope="release tests", preflight=_preflight()),
    )
    ledger.transition_recovery_attempt(attempt["attempt_id"], fence_token=attempt["fence_token"], state="ACCEPTED")
    with sqlite3.connect(tmp_path / "reviews.db") as conn:
        conn.execute("UPDATE release_review_receipts SET state='running' WHERE receipt_id=?", (receipt["receipt_id"],))
    assert ledger.bind_material_owned_handle(
        receipt["receipt_id"], attempt_id=attempt["attempt_id"], fence_token=attempt["fence_token"],
        handle_id="owned-handle", pid=123, host_start_time=456,
    )
    assert ledger.bind_material_owned_handle(
        receipt["receipt_id"], attempt_id=attempt["attempt_id"], fence_token=attempt["fence_token"],
        handle_id="owned-handle", pid=123, host_start_time=456,
    )
    assert ledger.finalize_material_saga(
        receipt["receipt_id"], attempt_id=attempt["attempt_id"], fence_token=attempt["fence_token"],
        status="completed", evidence={"delegation_id": "deleg_1", "result": {"status": "completed"}},
    ) is True
    with sqlite3.connect(tmp_path / "reviews.db") as conn:
        receipt_state = conn.execute(
            "SELECT state FROM release_review_receipts WHERE receipt_id=?", (receipt["receipt_id"],)
        ).fetchone()[0]
        plan_state = conn.execute(
            "SELECT saga_state FROM workflow_material_route_plans WHERE receipt_id=?", (receipt["receipt_id"],)
        ).fetchone()[0]
        attempt_state = conn.execute(
            "SELECT state FROM workflow_recovery_attempts WHERE attempt_id=?", (attempt["attempt_id"],)
        ).fetchone()[0]
    assert (receipt_state, plan_state, attempt_state) == ("completed", "TERMINAL", "COMMITTED")
    assert ledger.finalize_material_saga(
        receipt["receipt_id"], attempt_id=attempt["attempt_id"], fence_token=attempt["fence_token"],
        status="completed", evidence={"delegation_id": "deleg_1"},
    ) is False


def test_material_terminal_saga_rejects_stale_fence_without_partial_terminal_state(tmp_path):
    ledger = ReleaseReviewLedger(tmp_path / "reviews.db")
    packet = ledger.record_recovery_packet(_recovery_packet())
    attempt = _admit_recovery(ledger, packet["packet_hash"])
    receipt = ledger.admit_fleet_material_launch(
        attempt_id=attempt["attempt_id"], fence_token=attempt["fence_token"],
        route_plan={"requested": 1, "degraded_route_capacity": False,
                    "selected": {"lane_id": "codex", "effective_execution_identity": "route-a", "review_lens": "runtime"},
                    "unavailable": []},
        **_request(environment_fingerprint="env-a", scope="release tests", preflight=_preflight()),
    )
    with pytest.raises(RuntimeError, match="route-plan fence"):
        ledger.finalize_material_saga(
            receipt["receipt_id"], attempt_id=attempt["attempt_id"], fence_token=attempt["fence_token"] + 1,
            status="completed", evidence={},
        )
    with sqlite3.connect(tmp_path / "reviews.db") as conn:
        assert conn.execute(
            "SELECT state FROM release_review_receipts WHERE receipt_id=?", (receipt["receipt_id"],)
        ).fetchone()[0] == "launching"
        assert conn.execute(
            "SELECT saga_state FROM workflow_material_route_plans WHERE receipt_id=?", (receipt["receipt_id"],)
        ).fetchone()[0] == "SEALED"


def test_unowned_material_saga_becomes_interrupted_without_accepting_a_late_result(tmp_path):
    ledger = ReleaseReviewLedger(tmp_path / "reviews.db")
    packet = ledger.record_recovery_packet(_recovery_packet())
    attempt = _admit_recovery(ledger, packet["packet_hash"])
    receipt = ledger.admit_fleet_material_launch(
        attempt_id=attempt["attempt_id"], fence_token=attempt["fence_token"],
        route_plan={"requested": 1, "degraded_route_capacity": False,
                    "selected": {"lane_id": "codex", "effective_execution_identity": "route-a", "review_lens": "runtime"},
                    "unavailable": []},
        **_request(environment_fingerprint="env-a", scope="release tests", preflight=_preflight()),
    )
    ledger.transition_recovery_attempt(attempt["attempt_id"], fence_token=attempt["fence_token"], state="ACCEPTED")
    with sqlite3.connect(tmp_path / "reviews.db") as conn:
        conn.execute("UPDATE release_review_receipts SET state='running' WHERE receipt_id=?", (receipt["receipt_id"],))
    assert ledger.interrupt_unowned_material_saga(
        receipt["receipt_id"], attempt_id=attempt["attempt_id"], fence_token=attempt["fence_token"],
        evidence={"reason": "owner_died_before_external_handle"},
    )
    with sqlite3.connect(tmp_path / "reviews.db") as conn:
        receipt_state, terminal_json = conn.execute(
            "SELECT state, terminal_json FROM release_review_receipts WHERE receipt_id=?", (receipt["receipt_id"],)
        ).fetchone()
        plan_state = conn.execute(
            "SELECT saga_state FROM workflow_material_route_plans WHERE receipt_id=?", (receipt["receipt_id"],)
        ).fetchone()[0]
        attempt_state = conn.execute(
            "SELECT state FROM workflow_recovery_attempts WHERE attempt_id=?", (attempt["attempt_id"],)
        ).fetchone()[0]
    assert (receipt_state, plan_state, attempt_state) == ("unknown", "TERMINAL", "INTERRUPTED")
    assert json.loads(terminal_json)["evidence"]["external_handle_bound"] is False
    assert not ledger.interrupt_unowned_material_saga(
        receipt["receipt_id"], attempt_id=attempt["attempt_id"], fence_token=attempt["fence_token"],
        evidence={"reason": "late_replay"},
    )


def test_fleet_material_plan_rejects_a_caller_supplied_runner(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    ledger = ReleaseReviewLedger(hermes_home / "release-review-ledger.db")
    packet = ledger.record_recovery_packet(_recovery_packet())
    attempt = _admit_recovery(ledger, packet["packet_hash"])
    result = launch_material_async_review(
        ledger,
        attempt_id=attempt["attempt_id"],
        fence_token=attempt["fence_token"],
        fleet_route_plan={
            "selected": {
                "lane_id": "codex", "effective_execution_identity": "route-a",
                "review_lens": "runtime",
            },
            "unavailable": [], "requested": 1,
        },
        **_request(environment_fingerprint="env-a", scope="release tests", preflight=_preflight()),
        dispatch_kwargs={"runner": lambda: {"status": "completed"}, "interrupt_fn": lambda: None},
    )
    assert result["status"] == "rejected"
    assert "construct their runner internally" in result["error"]
    with sqlite3.connect(hermes_home / "release-review-ledger.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM workflow_material_route_plans").fetchone()[0] == 0


def test_generic_async_launcher_rejects_a_material_recovery_attempt(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    ledger = ReleaseReviewLedger(hermes_home / "release-review-ledger.db")
    result = launch_async_review(
        ledger, **_request(preflight=_preflight()), dispatch=lambda **_kwargs: {"status": "dispatched"},
        dispatch_kwargs={"goal": "review", "interrupt_fn": lambda: None, "recovery_attempt_id": "retry-1"},
    )
    assert result["status"] == "rejected"
    assert "material adapter" in result["error"]


def test_public_material_launcher_rejects_callers_without_sealed_ingress(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    ledger = ReleaseReviewLedger(hermes_home / "release-review-ledger.db")
    packet = ledger.record_recovery_packet(_recovery_packet())
    attempt = _admit_recovery(ledger, packet["packet_hash"])
    result = _public_material_launch(
        ledger, attempt_id=attempt["attempt_id"], fence_token=attempt["fence_token"],
        dispatch_kwargs={}, **_request(environment_fingerprint="env-a", scope="release tests", preflight=_preflight()),
    )
    assert result["status"] == "rejected"
    assert "sealed fleet material ingress" in result["error"]


def test_unaccepted_material_launch_keeps_retry_prepared(tmp_path, monkeypatch):
    """A pre-submission rejection closes its receipt but spends no retry."""
    hermes_home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    ledger = ReleaseReviewLedger(hermes_home / "release-review-ledger.db")
    packet = ledger.record_recovery_packet(_recovery_packet())
    attempt = _admit_recovery(ledger, packet["packet_hash"])
    result = launch_material_async_review(
        ledger, attempt_id=attempt["attempt_id"], fence_token=attempt["fence_token"],
        dispatch_kwargs={"goal": "review", "context": "candidate", "toolsets": None, "role": "reviewer", "model": "m", "session_key": "test"},
        **_request(environment_fingerprint="env-a", scope="release tests", preflight=_preflight()),
    )
    assert result["status"] == "rejected"
    with sqlite3.connect(hermes_home / "release-review-ledger.db") as conn:
        assert conn.execute(
            "SELECT state FROM workflow_recovery_attempts WHERE attempt_id=?", (attempt["attempt_id"],)
        ).fetchone()[0] == "PREPARED"


def test_activation_failure_preserves_outbox_and_interrupts_material_retry(tmp_path, monkeypatch):
    """No crash gap may delete the only durable recovery owner."""
    from tools import async_delegation as ad

    hermes_home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    ad._reset_for_tests()
    monkeypatch.setattr(ad, "_activate_durable_dispatch", lambda _delegation_id: False)
    try:
        ledger = ReleaseReviewLedger(hermes_home / "release-review-ledger.db")
        packet = ledger.record_recovery_packet(_recovery_packet())
        attempt = _admit_recovery(ledger, packet["packet_hash"])
        result = launch_material_async_review(
            ledger, attempt_id=attempt["attempt_id"], fence_token=attempt["fence_token"],
            dispatch_kwargs={
                "goal": "review", "context": "candidate", "toolsets": None, "role": "reviewer", "model": "m", "session_key": "test",
                "runner": lambda: {"status": "completed"}, "interrupt_fn": lambda: None, "max_async_children": 1,
            },
            **_request(environment_fingerprint="env-a", scope="release tests", preflight=_preflight()),
        )
        assert result["status"] == "rejected"
        with sqlite3.connect(hermes_home / "state.db") as conn:
            assert conn.execute("SELECT state FROM async_delegations").fetchone()[0] == "unknown"
        with sqlite3.connect(hermes_home / "release-review-ledger.db") as conn:
            assert conn.execute(
                "SELECT state FROM workflow_recovery_attempts WHERE attempt_id=?", (attempt["attempt_id"],)
            ).fetchone()[0] == "INTERRUPTED"
    finally:
        ad._reset_for_tests()


def test_material_review_cancel_is_fenced_and_rejects_late_result(tmp_path, monkeypatch):
    from tools import async_delegation as ad

    hermes_home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    ad._reset_for_tests()
    gate = threading.Event()
    try:
        ledger = ReleaseReviewLedger(hermes_home / "release-review-ledger.db")
        packet = ledger.record_recovery_packet(_recovery_packet())
        attempt = _admit_recovery(ledger, packet["packet_hash"])
        result = launch_material_async_review(
            ledger, attempt_id=attempt["attempt_id"], fence_token=attempt["fence_token"],
            **_request(environment_fingerprint="env-a", scope="release tests", preflight=_preflight()),
            dispatch_kwargs={
                "goal": "review", "context": "candidate", "toolsets": None, "role": "reviewer",
                "model": "m", "session_key": "test",
                "runner": lambda: (gate.wait(2), {"status": "completed", "summary": "late"})[1],
                "interrupt_fn": lambda: None, "max_async_children": 1,
            },
        )
        delegation_id = result["dispatch"]["delegation_id"]
        assert not ad.cancel_async_delegation(delegation_id, fence_token=attempt["fence_token"] + 1)
        assert ad.cancel_async_delegation(delegation_id, fence_token=attempt["fence_token"])
        gate.set()
        deadline = time.monotonic() + 2
        while ad.active_count() and time.monotonic() < deadline:
            time.sleep(0.01)
        with sqlite3.connect(hermes_home / "state.db") as conn:
            assert conn.execute("SELECT state FROM async_delegations WHERE delegation_id=?", (delegation_id,)).fetchone()[0] == "cancelled"
        assert ledger.receipt_state(result["receipt_id"]) == "cancelled"
    finally:
        gate.set()
        ad._reset_for_tests()


def test_material_review_owner_death_interrupts_only_current_attempt(tmp_path, monkeypatch):
    from tools import async_delegation as ad

    hermes_home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    ad._reset_for_tests()
    gate = threading.Event()
    try:
        ledger = ReleaseReviewLedger(hermes_home / "release-review-ledger.db")
        packet = ledger.record_recovery_packet(_recovery_packet())
        attempt = _admit_recovery(ledger, packet["packet_hash"])
        result = launch_material_async_review(
            ledger, attempt_id=attempt["attempt_id"], fence_token=attempt["fence_token"],
            **_request(environment_fingerprint="env-a", scope="release tests", preflight=_preflight()),
            dispatch_kwargs={
                "goal": "review", "context": "candidate", "toolsets": None, "role": "reviewer",
                "model": "m", "session_key": "test",
                "runner": lambda: (gate.wait(2), {"status": "completed"})[1],
                "interrupt_fn": lambda: None, "max_async_children": 1,
            },
        )
        with sqlite3.connect(hermes_home / "state.db") as conn:
            conn.execute("UPDATE async_delegations SET owner_pid=0 WHERE delegation_id=?", (result["dispatch"]["delegation_id"],))
        assert ad.recover_abandoned_delegations() == 1
        with sqlite3.connect(hermes_home / "release-review-ledger.db") as conn:
            assert conn.execute(
                "SELECT state FROM workflow_recovery_attempts WHERE attempt_id=?", (attempt["attempt_id"],)
            ).fetchone()[0] == "INTERRUPTED"
    finally:
        gate.set()
        ad._reset_for_tests()


def test_variant_output_or_deadline_is_conflict_not_silent_reuse(tmp_path):
    ledger = ReleaseReviewLedger(tmp_path / "reviews.db")
    ledger.admit(**_request())
    output_variant = ledger.admit(**_request(output_path="other.json"))
    deadline_variant = ledger.admit(**_request(deadline_seconds=120))
    assert output_variant["status"] == "conflict"
    assert deadline_variant["status"] == "conflict"


@pytest.mark.parametrize("missing_field", ["environment_fingerprint", "evidence_fingerprint"])
def test_review_admission_rejects_missing_immutable_identity(tmp_path, missing_field):
    ledger = ReleaseReviewLedger(tmp_path / "reviews.db")
    request = _request()
    request[missing_field] = ""
    with pytest.raises(ValueError, match=missing_field):
        ledger.admit(**request)


def test_changed_environment_or_evidence_creates_a_fresh_review_identity(tmp_path):
    ledger = ReleaseReviewLedger(tmp_path / "reviews.db")
    first = ledger.admit(**_request(environment_fingerprint="env-a", evidence_fingerprint="evidence-a"))
    changed_environment = ledger.admit(**_request(environment_fingerprint="env-b", evidence_fingerprint="evidence-a"))
    changed_evidence = ledger.admit(**_request(environment_fingerprint="env-a", evidence_fingerprint="evidence-b"))
    assert first["status"] == "admitted"
    assert changed_environment["status"] == "admitted"
    assert changed_evidence["status"] == "admitted"
    assert len({first["receipt_id"], changed_environment["receipt_id"], changed_evidence["receipt_id"]}) == 3


def test_effective_route_identity_collapses_endpoint_account_model_aliases(tmp_path):
    first_route = canonical_effective_route_identity(
        provider="OpenAI", base_url="https://API.example.test/v1/", account_secret="private", model="GPT-5.6 Sol",
    )
    alias_route = canonical_effective_route_identity(
        provider="openai", base_url="https://api.example.test/v1", account_secret="private", model="gpt_5.6-sol",
    )
    assert first_route == alias_route
    assert "private" not in first_route
    ledger = ReleaseReviewLedger(tmp_path / "reviews.db")
    first = ledger.admit(**_request(lane="lane-one", effective_route_identity=first_route))
    alias = ledger.admit(**_request(lane="lane-two", effective_route_identity=alias_route))
    assert first["status"] == alias["status"] == "admitted"


def test_effective_route_identity_preserves_case_sensitive_endpoint_paths():
    lower = canonical_effective_route_identity(
        provider="openai", base_url="https://api.example.test/v1", account_secret="private", model="gpt-5.6-sol",
    )
    upper = canonical_effective_route_identity(
        provider="openai", base_url="https://api.example.test/V1", account_secret="private", model="gpt-5.6-sol",
    )
    assert lower != upper


def test_effective_route_identity_ignores_executable_location():
    first = canonical_effective_route_identity(
        provider="claude", base_url="", account_secret=None, model="opus",
        adapter_kind="external_cli", auth_kind="oauth", auth_source="official-subscription",
        executable="C:/one/claude.exe",
    )
    alias = canonical_effective_route_identity(
        provider="CLAUDE", base_url="", account_secret=None, model="OPUS",
        adapter_kind="external_cli", auth_kind="oauth", auth_source="official-subscription",
        executable="D:/another/claude.exe",
    )
    assert first == alias
    assert "claude.exe" not in first


def test_conflicting_requested_id_is_rejected(tmp_path):
    ledger = ReleaseReviewLedger(tmp_path / "reviews.db")
    ledger.admit(**_request(receipt_id="r"))
    conflict = ledger.admit(**_request(candidate_hash="b" * 64, receipt_id="r"))
    assert conflict["status"] == "conflict"
    assert conflict["reason"] == "receipt_id_reused"


@pytest.mark.parametrize("deadline", [0, -1, float("inf"), 3601])
def test_invalid_deadlines_are_rejected(tmp_path, deadline):
    ledger = ReleaseReviewLedger(tmp_path / "reviews.db")
    with pytest.raises(ValueError):
        ledger.admit(**_request(deadline_seconds=deadline))


def test_preflight_requires_verifiable_controls_and_authenticated_health(tmp_path):
    ledger = ReleaseReviewLedger(tmp_path / "reviews.db")
    receipt = ledger.admit(**_request())
    with pytest.raises(ValueError):
        ledger.capture_preflight(receipt["receipt_id"], {key: "verified" for key in _preflight()})
    incomplete = _preflight()
    incomplete["health"]["authenticated"] = False
    with pytest.raises(ValueError):
        ledger.capture_preflight(receipt["receipt_id"], incomplete)
    ledger.capture_preflight(receipt["receipt_id"], _preflight())


def test_validation_cache_requires_exact_candidate_environment_evidence_and_command(tmp_path):
    ledger = ReleaseReviewLedger(tmp_path / "reviews.db")
    first = ledger.admit_validation(
        candidate_hash="a" * 64, environment_fingerprint="clean-env-a", evidence_fingerprint="lock-a",
        command=["scripts/run_tests.sh", "tests/tools/test_release_review_ledger.py"],
    )
    assert first["status"] == "admitted"
    ledger.finalize_validation(first["validation_id"], passed=True, evidence={"tests": "26 passed"})
    assert ledger.admit_validation(
        candidate_hash="a" * 64, environment_fingerprint="clean-env-a", evidence_fingerprint="lock-a",
        command=["scripts/run_tests.sh", "tests/tools/test_release_review_ledger.py"],
    )["status"] == "cached"
    assert ledger.admit_validation(
        candidate_hash="a" * 64, environment_fingerprint="clean-env-b", evidence_fingerprint="lock-a",
        command=["scripts/run_tests.sh", "tests/tools/test_release_review_ledger.py"],
    )["status"] == "admitted"


def test_early_preflight_timing_and_alerts_are_scoped_and_deduplicated(tmp_path):
    ledger = ReleaseReviewLedger(tmp_path / "reviews.db")
    fingerprint = ledger.record_operation_preflight(
        candidate_hash="a" * 64, operation="local validation",
        controls={"environment": {"status": "verified", "evidence": "fresh temp home"},
                  "dependencies": {"status": "ready", "evidence": "locked input"}},
    )
    assert len(fingerprint) == 64
    ledger.record_timing(receipt_id="build-a", phase="tests", category="active", started_at=10, ended_at=15, evidence="runner")
    ledger.record_timing(receipt_id="build-a", phase="ci", category="external_wait", started_at=15, ended_at=27, evidence="remote")
    assert ledger.timing_summary("build-a") == {"active": 5.0, "external_wait": 12.0}
    alert = {"fingerprint": "same-failure", "candidate_hash": "a" * 64, "terminal_state": "failed",
             "owner": "codex", "evidence": "exit 1", "ttl_seconds": 60}
    assert ledger.record_alert(**alert)["status"] == "recorded"
    assert ledger.record_alert(**alert)["status"] == "suppressed"
    assert ledger.record_alert(**{**alert, "candidate_hash": "b" * 64})["status"] == "recorded"


def test_decision_record_can_gate_launch_and_terminal_receipts_can_only_mark_cleanup(tmp_path):
    ledger = ReleaseReviewLedger(tmp_path / "reviews.db")
    decision = ledger.record_decision(
        decision_id="async-scope", scope="receipt-bound async review", rationale="dedupe work", owner="codex",
        safety_boundary="no live mutation", acceptance_criteria="temp-home regression", classification="post-release",
    )
    assert decision["status"] == "recorded"
    ledger.require_decision("async-scope")
    receipt = ledger.admit(**_request())
    ledger.capture_preflight(receipt["receipt_id"], _preflight())
    assert ledger.claim_launch(receipt["receipt_id"])["status"] == "claimed"
    ledger.attach_processes(receipt["receipt_id"], 1, None, "test")
    assert ledger.finalize_async_receipt(receipt["receipt_id"], "completed", {"result": "ok"}) is True
    assert ledger.record_cleanup_eligibility(
        receipt_id=receipt["receipt_id"], candidate_hash="a" * 64, reason="terminal artifact cache", evidence="completed receipt",
    ) is None


def test_claim_is_atomic_and_second_claim_cannot_launch(tmp_path):
    ledger = ReleaseReviewLedger(tmp_path / "reviews.db")
    receipt = ledger.admit(**_request())
    ledger.capture_preflight(receipt["receipt_id"], _preflight())
    assert ledger.claim_launch(receipt["receipt_id"])["status"] == "claimed"
    assert ledger.claim_launch(receipt["receipt_id"])["state"] == "launching"
    ledger.attach_processes(receipt["receipt_id"], 101, 102, "pid:101")
    with pytest.raises(RuntimeError):
        ledger.attach_processes(receipt["receipt_id"], 201, 202, "pid:201")
    with sqlite3.connect(tmp_path / "reviews.db") as conn:
        assert conn.execute("SELECT root_pid, leaf_pid, state FROM release_review_receipts").fetchone() == (101, 102, "running")


def test_claim_does_not_consume_review_timebox_before_attachment(tmp_path):
    ledger = ReleaseReviewLedger(tmp_path / "reviews.db")
    receipt = ledger.admit(**_request())
    ledger.capture_preflight(receipt["receipt_id"], _preflight())
    assert ledger.expire_due(now=time.time() + 61) == 0
    assert ledger.claim_launch(receipt["receipt_id"])["state"] == "launching"


def test_unknown_receipt_cannot_be_updated(tmp_path):
    ledger = ReleaseReviewLedger(tmp_path / "reviews.db")
    with pytest.raises(KeyError):
        ledger.attach_processes("missing", 1, 2, "pid:1")


def test_findings_are_append_only_and_produce_incremental_scope(tmp_path):
    ledger = ReleaseReviewLedger(tmp_path / "reviews.db")
    receipt = ledger.admit(**_request())
    ledger.capture_preflight(receipt["receipt_id"], _preflight())
    ledger.claim_launch(receipt["receipt_id"])
    ledger.attach_processes(receipt["receipt_id"], 1, 1, "pid:1")
    ledger.append_findings(receipt["receipt_id"], [{"finding_id": "one", "files": ["a.py"], "tests": ["test_a"], "disposition": "open"}])
    ledger.append_findings(receipt["receipt_id"], [{"finding_id": "two", "files": ["b.py", "a.py"], "tests": ["test_b"], "disposition": "fixed"}])
    assert ledger.incremental_scope(receipt["receipt_id"]) == {"files": ["a.py", "b.py"], "tests": ["test_a", "test_b"]}
    with pytest.raises(ValueError):
        ledger.append_findings(receipt["receipt_id"], [{"finding_id": "one", "files": ["a.py"], "tests": ["test_a"]}])


def test_direct_shell_launch_reuses_receipt_without_second_process(tmp_path):
    ledger = ReleaseReviewLedger(tmp_path / "reviews.db")
    calls = []

    class Process:
        pid = 42

        def poll(self):
            return 0

    def popen(command, **kwargs):
        calls.append((command, kwargs))
        return Process()

    args = {**_request(), "preflight": _preflight(), "command": ["reviewer", "--read-only"]}
    first = launch_shell_review(ledger, popen=popen, restart_recovery_mode="current_process_only", **args)
    second = launch_shell_review(ledger, popen=popen, restart_recovery_mode="current_process_only", **args)
    assert first["status"] == "launched"
    assert first["root_pid"] == 42
    assert second["status"] == "existing"
    assert len(calls) == 1
    assert calls[0][1]["shell"] is False


def test_direct_shell_requires_explicit_non_durable_restart_mode(tmp_path):
    ledger = ReleaseReviewLedger(tmp_path / "reviews.db")
    called = False

    def popen(*_args, **_kwargs):
        nonlocal called
        called = True

    for mode in (None, "durable"):
        result = launch_shell_review(
            ledger, **_request(), preflight=_preflight(), command=["reviewer"],
            popen=popen, restart_recovery_mode=mode,
        )
        assert result["status"] == "rejected"
        assert "durable restart recovery is unsupported" in result["error"]
    assert called is False


def test_direct_attachment_restarts_deadline_from_successful_attach(tmp_path):
    ledger = ReleaseReviewLedger(tmp_path / "reviews.db")
    receipt = ledger.admit(**_request(deadline_seconds=60))
    ledger.capture_preflight(receipt["receipt_id"], _preflight())
    ledger.claim_launch(receipt["receipt_id"])
    ledger.attach_processes(receipt["receipt_id"], 91, 91, "pid:91:single-process")
    with sqlite3.connect(tmp_path / "reviews.db") as conn:
        deadline_at, updated_at, deadline_seconds, state = conn.execute(
            "SELECT deadline_at, updated_at, deadline_seconds, state FROM release_review_receipts WHERE receipt_id=?",
            (receipt["receipt_id"],),
        ).fetchone()
    assert state == "running"
    assert deadline_at == pytest.approx(updated_at + deadline_seconds, abs=0.01)


def test_direct_expiry_between_spawn_and_attach_is_terminal_not_an_error(tmp_path):
    class ExpiringLedger(ReleaseReviewLedger):
        def attach_processes(self, receipt_id, root_pid, leaf_pid, launch_handle):
            with sqlite3.connect(self.path) as conn:
                conn.execute(
                    "UPDATE release_review_receipts SET state='timebox_expired' WHERE receipt_id=?", (receipt_id,)
                )
            return super().attach_processes(receipt_id, root_pid, leaf_pid, launch_handle)

    class Process:
        pid = 92
        terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

    ledger = ExpiringLedger(tmp_path / "reviews.db")
    process = Process()
    result = launch_shell_review(
        ledger, **_request(), preflight=_preflight(), command=["reviewer"],
        popen=lambda *_args, **_kwargs: process, restart_recovery_mode="current_process_only",
    )
    assert result["status"] == "timebox_expired"
    assert process.terminated is True
    with sqlite3.connect(tmp_path / "reviews.db") as conn:
        state, terminal = conn.execute(
            "SELECT state, terminal_json FROM release_review_receipts WHERE receipt_id=?", (result["receipt_id"],)
        ).fetchone()
    assert state == "timebox_expired"
    assert "receipt expired before direct attachment" in terminal


def test_direct_launcher_records_timebox_before_terminating_its_own_process(tmp_path, monkeypatch):
    ledger = ReleaseReviewLedger(tmp_path / "reviews.db")

    class Process:
        pid = 77
        terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

    process = Process()
    callbacks = []
    monkeypatch.setattr(ledger, "supervise_deadline", lambda _receipt_id, callback: callbacks.append(callback))
    result = launch_shell_review(
        ledger,
        **_request(deadline_seconds=60),
        preflight=_preflight(),
        command=["reviewer"],
        popen=lambda *_args, **_kwargs: process,
        restart_recovery_mode="current_process_only",
    )
    assert ledger.deadline_watch_state(result["receipt_id"], now=time.time() + 61) == "expired"
    callbacks[0]()
    assert process.terminated is True
    with sqlite3.connect(tmp_path / "reviews.db") as conn:
        state, terminal = conn.execute(
            "SELECT state, terminal_json FROM release_review_receipts WHERE receipt_id=?", (result["receipt_id"],)
        ).fetchone()
    assert state == "timebox_expired"
    assert "direct review deadline" in terminal


def test_direct_shell_normal_exit_records_terminal_evidence_before_deadline(tmp_path):
    ledger = ReleaseReviewLedger(tmp_path / "reviews.db")

    class Process:
        pid = 78

        def poll(self):
            return 0

        def terminate(self):
            raise AssertionError("completed reviewer must not be terminated")

    result = launch_shell_review(
        ledger, **_request(deadline_seconds=1), preflight=_preflight(), command=["reviewer"],
        popen=lambda *_args, **_kwargs: Process(), restart_recovery_mode="current_process_only",
    )
    deadline = time.monotonic() + 1
    terminal = None
    while terminal is None and time.monotonic() < deadline:
        with sqlite3.connect(tmp_path / "reviews.db") as conn:
            terminal = conn.execute("SELECT state, terminal_json FROM release_review_receipts WHERE receipt_id=?", (result["receipt_id"],)).fetchone()
        if terminal[0] != "completed":
            terminal = None
            time.sleep(0.01)
    assert terminal[0] == "completed"
    assert "return_code" in terminal[1]
    assert ledger.deadline_watch_state(result["receipt_id"]) == "terminal"


def test_simultaneous_timeboxes_terminate_each_reviewers_own_process(tmp_path):
    ledger = ReleaseReviewLedger(tmp_path / "reviews.db")

    class Process:
        def __init__(self, pid):
            self.pid = pid
            self.terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

    first, second = Process(81), Process(82)
    processes = iter((first, second))
    for candidate in ("a" * 64, "b" * 64):
        launch_shell_review(
            ledger,
            **_request(candidate_hash=candidate, deadline_seconds=0.1),
            preflight=_preflight(),
            command=["reviewer"],
            popen=lambda *_args, **_kwargs: next(processes),
            restart_recovery_mode="current_process_only",
        )
    deadline = time.monotonic() + 1
    while not (first.terminated and second.terminated) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert first.terminated is True
    assert second.terminated is True


def test_async_launch_uses_one_receipt_and_preserves_rejected_state(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    ledger = ReleaseReviewLedger(tmp_path / "hermes-home" / "release-review-ledger.db")
    calls = []

    def dispatch(**kwargs):
        calls.append(kwargs)
        return {"status": "rejected", "error": "capacity"}

    args = {**_request(), "preflight": _preflight(), "dispatch": dispatch, "dispatch_kwargs": {"goal": "review"}}
    first = launch_async_review(ledger, **args)
    second = launch_async_review(ledger, **args)
    assert first["status"] == "rejected"
    assert second["status"] == "existing"
    assert second["state"] == "launch_rejected"
    assert len(calls) == 0


def test_async_fallback_gets_a_new_route_bound_admission_after_rejection(tmp_path, monkeypatch):
    """A fallback may not reuse or overwrite the primary lane's receipt."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    ledger = ReleaseReviewLedger(tmp_path / "hermes-home" / "release-review-ledger.db")
    calls = []

    def dispatch(**kwargs):
        calls.append(kwargs)
        return {"status": "rejected", "error": "provider unavailable"}

    base = {
        **_request(lane="primary", model="model-primary", effective_route_identity="route-primary"),
        "preflight": _preflight(), "dispatch": dispatch,
        "dispatch_kwargs": {"goal": "review", "interrupt_fn": lambda: None},
    }
    first = launch_async_review(ledger, **base)
    repeated = launch_async_review(ledger, **base)
    fallback = launch_async_review(
        ledger,
        **{**base, "lane": "fallback", "model": "model-fallback", "effective_route_identity": "route-fallback"},
    )
    assert first["status"] == "rejected"
    assert repeated["status"] == "existing"
    assert fallback["status"] == "rejected"
    assert fallback["receipt_id"] != first["receipt_id"]
    assert len(calls) == 2


def test_async_launcher_uses_the_real_async_delegation_rail(tmp_path, monkeypatch):
    """The adapter must protect the same dispatcher Hermes uses in production."""
    from tools import async_delegation as ad

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    ad._reset_for_tests()
    try:
        ledger = ReleaseReviewLedger(tmp_path / "hermes-home" / "release-review-ledger.db")
        result = launch_async_review(
            ledger,
            **_request(),
            preflight=_preflight(),
            dispatch=ad.dispatch_async_delegation,
            dispatch_kwargs={
                "goal": "read-only review",
                "context": "frozen candidate",
                "toolsets": None,
                "role": "reviewer",
                "model": "m",
                "session_key": "test",
                "runner": lambda: {"status": "completed", "summary": "done"},
                "interrupt_fn": lambda: None,
                "max_async_children": 1,
            },
        )
        assert result["status"] == "launched"
        assert result["dispatch"]["status"] == "dispatched"
        assert result["dispatch"]["review_receipt_id"] == result["receipt_id"]
        assert result["root_pid"] > 0
        assert result["leaf_pid"] is None
        deadline = time.monotonic() + 2
        while ad.active_count() and time.monotonic() < deadline:
            time.sleep(0.01)
        with sqlite3.connect(tmp_path / "hermes-home" / "release-review-ledger.db") as conn:
            state, terminal = conn.execute(
                "SELECT state, terminal_json FROM release_review_receipts WHERE receipt_id=?",
                (result["receipt_id"],),
            ).fetchone()
        assert state == "completed"
        assert "delegation_id" in terminal
    finally:
        deadline = time.monotonic() + 2
        while ad.active_count() and time.monotonic() < deadline:
            time.sleep(0.01)
        ad._reset_for_tests()


def test_async_timeout_records_terminal_outcome_when_interrupt_is_noop(tmp_path, monkeypatch):
    """A non-cooperative thread cannot revive a receipt after its timebox."""
    from tools import async_delegation as ad

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    ad._reset_for_tests()
    gate = threading.Event()
    try:
        ledger = ReleaseReviewLedger(tmp_path / "hermes-home" / "release-review-ledger.db")
        result = launch_async_review(
            ledger,
            **_request(),
            preflight=_preflight(),
            dispatch=ad.dispatch_async_delegation,
            dispatch_kwargs={
                "goal": "read-only review",
                "context": "frozen candidate",
                "toolsets": None,
                "role": "reviewer",
                "model": "m",
                "session_key": "test",
                "runner": lambda: (gate.wait(2), {"status": "completed", "summary": "late"})[1],
                "interrupt_fn": lambda: None,
                "max_async_children": 1,
            },
        )
        assert result["status"] == "launched"
        assert ad.force_timeout_review_receipt(result["receipt_id"]) == 1
        with sqlite3.connect(tmp_path / "hermes-home" / "release-review-ledger.db") as conn:
            state, terminal = conn.execute(
                "SELECT state, terminal_json FROM release_review_receipts WHERE receipt_id=?",
                (result["receipt_id"],),
            ).fetchone()
        assert state == "timebox_expired"
        assert "timebox_expired" in terminal
        gate.set()
        deadline = time.monotonic() + 2
        while ad.active_count() and time.monotonic() < deadline:
            time.sleep(0.01)
        with sqlite3.connect(tmp_path / "hermes-home" / "release-review-ledger.db") as conn:
            assert conn.execute(
                "SELECT state FROM release_review_receipts WHERE receipt_id=?", (result["receipt_id"],)
            ).fetchone()[0] == "timebox_expired"
    finally:
        gate.set()
        deadline = time.monotonic() + 2
        while ad.active_count() and time.monotonic() < deadline:
            time.sleep(0.01)
        ad._reset_for_tests()


def test_async_recovery_terminalizes_the_linked_receipt_in_a_fresh_temp_home(tmp_path, monkeypatch):
    from tools import async_delegation as ad

    hermes_home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    ad._reset_for_tests()
    gate = threading.Event()
    try:
        ledger = ReleaseReviewLedger(hermes_home / "release-review-ledger.db")
        result = launch_async_review(
            ledger, **_request(), preflight=_preflight(), dispatch=ad.dispatch_async_delegation,
            dispatch_kwargs={
                "goal": "read-only review", "context": "candidate", "toolsets": None, "role": "reviewer",
                "model": "m", "session_key": "test", "runner": lambda: (gate.wait(2), {"status": "completed"})[1],
                "interrupt_fn": lambda: None, "max_async_children": 1,
            },
        )
        with sqlite3.connect(hermes_home / "state.db") as conn:
            conn.execute("UPDATE async_delegations SET owner_pid=0 WHERE delegation_id=?", (result["dispatch"]["delegation_id"],))
        assert ad.recover_abandoned_delegations() == 1
        assert ledger.receipt_state(result["receipt_id"]) == "unknown"
    finally:
        gate.set()
        deadline = time.monotonic() + 2
        while ad.active_count() and time.monotonic() < deadline:
            time.sleep(0.01)
        ad._reset_for_tests()


def test_recovery_terminalizes_a_crash_between_durable_lease_and_activation(tmp_path, monkeypatch):
    from tools import async_delegation as ad

    hermes_home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    ledger = ReleaseReviewLedger(hermes_home / "release-review-ledger.db")
    receipt = ledger.admit(**_request())
    ledger.capture_preflight(receipt["receipt_id"], _preflight())
    assert ledger.claim_launch(receipt["receipt_id"])["status"] == "claimed"
    ledger.bind_async_dispatch(receipt["receipt_id"], "deleg_crash", 1)
    record = {
        "delegation_id": "deleg_crash", "goal": "review", "context": None, "toolsets": None,
        "role": "reviewer", "model": "m", "session_key": "test", "origin_ui_session_id": "",
        "origin_session_id": "", "parent_session_id": None, "review_receipt_id": receipt["receipt_id"],
        "review_ledger_path": str(hermes_home / "release-review-ledger.db"), "dispatched_at": time.time(),
    }
    assert ad._persist_dispatch(record, max_async_children=1, state="dispatching") is True
    with sqlite3.connect(hermes_home / "state.db") as conn:
        conn.execute("UPDATE async_delegations SET owner_pid=0 WHERE delegation_id='deleg_crash'")
    assert ad.recover_abandoned_delegations() == 1
    assert ledger.receipt_state(receipt["receipt_id"]) == "unknown"


def test_durable_capacity_rejects_a_second_process_view(tmp_path, monkeypatch):
    from tools import async_delegation as ad

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    ad._reset_for_tests()
    gate = threading.Event()
    try:
        first = ad.dispatch_async_delegation(
            goal="first", context=None, toolsets=None, role="builder", model="m", session_key="test",
            runner=lambda: (gate.wait(2), {"status": "completed"})[1], interrupt_fn=lambda: None, max_async_children=1,
        )
        assert first["status"] == "dispatched"
        with ad._records_lock:
            ad._records.clear()
        second = ad.dispatch_async_delegation(
            goal="second", context=None, toolsets=None, role="builder", model="m", session_key="test",
            runner=lambda: {"status": "completed"}, interrupt_fn=lambda: None, max_async_children=1,
        )
        assert second["status"] == "rejected"
        assert "across Hermes processes" in second["error"]
    finally:
        gate.set()
        deadline = time.monotonic() + 2
        while ad.active_count() and time.monotonic() < deadline:
            time.sleep(0.01)
        ad._reset_for_tests()


def test_durable_capacity_is_atomic_across_two_processes(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    ready_one, ready_two, go = tmp_path / "ready-one", tmp_path / "ready-two", tmp_path / "go"
    source = (
        "import os, sys, time\n"
        "from pathlib import Path\n"
        "from tools.async_delegation import _persist_dispatch\n"
        f"os.environ['HERMES_HOME'] = r'{hermes_home}'\n"
        "ready, go, identity = map(Path, sys.argv[1:4])\n"
        "ready.write_text('ready', encoding='utf-8')\n"
        "while not go.exists(): time.sleep(0.01)\n"
        "record={'delegation_id': identity.name, 'goal':'review', 'context':None, 'toolsets':None, 'role':'reviewer', 'model':'m', 'session_key':'test', 'origin_ui_session_id':'', 'origin_session_id':'', 'parent_session_id':None, 'dispatched_at':time.time()}\n"
        "print(_persist_dispatch(record, max_async_children=1, state='dispatching'))\n"
    )
    cwd = Path(__file__).resolve().parents[2]
    one = subprocess.Popen([sys.executable, "-c", source, str(ready_one), str(go), str(tmp_path / "deleg-one")], cwd=cwd, stdout=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    two = subprocess.Popen([sys.executable, "-c", source, str(ready_two), str(go), str(tmp_path / "deleg-two")], cwd=cwd, stdout=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    deadline = time.monotonic() + 5
    while not (ready_one.exists() and ready_two.exists()) and time.monotonic() < deadline:
        time.sleep(0.01)
    go.write_text("go", encoding="utf-8")
    outcomes = {one.communicate(timeout=5)[0].strip(), two.communicate(timeout=5)[0].strip()}
    assert outcomes == {"True", "False"}


def test_pruning_never_deletes_a_dispatching_durable_lease(tmp_path, monkeypatch):
    from tools import async_delegation as ad

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    ad._reset_for_tests()
    try:
        record = {
            "delegation_id": "deleg-prune", "goal": "review", "context": None, "toolsets": None,
            "role": "reviewer", "model": "m", "session_key": "test", "origin_ui_session_id": "",
            "origin_session_id": "", "parent_session_id": None, "dispatched_at": time.time(),
        }
        assert ad._persist_dispatch(record, state="dispatching") is True
        monkeypatch.setattr(ad, "_MAX_RETAINED_COMPLETED", 0)
        monkeypatch.setattr(ad, "_MAX_DURABLE_PENDING", 0)
        ad._prune_durable_records()
        with sqlite3.connect(tmp_path / "hermes-home" / "state.db") as conn:
            assert conn.execute("SELECT state FROM async_delegations WHERE delegation_id='deleg-prune'").fetchone()[0] == "dispatching"
    finally:
        ad._reset_for_tests()


def test_direct_async_dispatch_refuses_unclaimed_review_receipt(tmp_path, monkeypatch):
    from tools import async_delegation as ad

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    rejected = ad.dispatch_async_delegation(
        goal="review", context=None, toolsets=None, role="reviewer", model="m", session_key="test",
        runner=lambda: {"status": "completed"}, interrupt_fn=lambda: None,
        review_receipt_id="not-claimed", review_ledger_path=str(tmp_path / "hermes-home" / "release-review-ledger.db"),
    )
    assert rejected["status"] == "rejected"
    assert "not admitted" in rejected["error"]


def test_async_adapter_never_dispatches_existing_or_conflicting_receipt(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    ledger = ReleaseReviewLedger(tmp_path / "hermes-home" / "release-review-ledger.db")
    calls = []

    def dispatch(**kwargs):
        calls.append(kwargs)
        ledger.activate_async_dispatch(kwargs["review_receipt_id"], kwargs["delegation_id"], __import__("os").getpid())
        return {"status": "dispatched", "delegation_id": kwargs["delegation_id"]}

    args = {**_request(), "preflight": _preflight(), "dispatch": dispatch,
            "dispatch_kwargs": {"goal": "review", "interrupt_fn": lambda: None}}
    first = launch_async_review(ledger, **args)
    existing = launch_async_review(ledger, **args)
    conflict = launch_async_review(ledger, **{**args, "output_path": "other-output"})
    assert first["status"] == "launched"
    assert existing["status"] == "existing"
    assert conflict["status"] == "conflict"
    assert len(calls) == 1


def test_async_adapter_rejects_noncanonical_ledger_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    ledger = ReleaseReviewLedger(tmp_path / "reviews.db")
    result = launch_async_review(
        ledger, **_request(), preflight=_preflight(), dispatch=lambda **_kwargs: {"status": "dispatched"},
        dispatch_kwargs={"goal": "review", "interrupt_fn": lambda: None},
    )
    assert result["status"] == "rejected"
    assert "canonical Hermes ledger path" in result["error"]


def test_separate_process_same_identity_allows_one_claim(tmp_path):
    db = tmp_path / "reviews.db"
    source = (
        "from pathlib import Path\n"
        "from tools.release_review_ledger import ReleaseReviewLedger\n"
        f"l=ReleaseReviewLedger(Path(r'{db}'))\n"
        "r=l.admit(candidate_hash='a'*64,scope='runtime',lane='codex',model='m',prompt='p',deadline_seconds=60,output_path='out',environment_fingerprint='test-environment',evidence_fingerprint='test-evidence')\n"
        "if r['status']=='admitted':\n l.capture_preflight(r['receipt_id'], {'target':{'status':'verified','evidence':'x'},'install':{'status':'verified','evidence':'x'},'restart':{'status':'verified','evidence':'x'},'rollback':{'status':'verified','evidence':'x'},'health':{'status':'verified','evidence':'x','authenticated':True,'method':'probe','endpoint':'/health'}})\n print(l.claim_launch(r['receipt_id'])['status'])\n"
        "else:\n print(r['status'])\n"
    )
    proc = [sys.executable, "-c", source]
    first = subprocess.Popen(proc, cwd=Path(__file__).resolve().parents[2], stdout=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    second = subprocess.Popen(proc, cwd=Path(__file__).resolve().parents[2], stdout=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    outputs = {first.communicate(timeout=10)[0].strip(), second.communicate(timeout=10)[0].strip()}
    assert outputs == {"claimed", "existing"}
