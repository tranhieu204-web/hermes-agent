"""Fleet Scheduler v1 behavior: route receipts, leases, capability peers."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time

import pytest


FAMILIES = {
    "alpha": "grok",
    "beta": "claude",
    "gamma": "sol",
    "delta": "claude",
}


def _contract(**overrides):
    base = {
        "preferred": "alpha",
        "peers": ["beta", "gamma"],
        "profile_families": FAMILIES,
        "profile_models": {
            "alpha": "grok-4.6",
            "beta": "claude-opus",
            "gamma": "sol-flagship",
        },
        "eligible_reviewer_families": ["claude", "sol"],
        "material_bytes": True,
    }
    base.update(overrides)
    return base


@pytest.fixture()
def kb_home(monkeypatch):
    test_home = tempfile.mkdtemp(prefix="kanban_fleet_sched_")
    for prof in ("alpha", "beta", "gamma", "delta", "default"):
        os.makedirs(os.path.join(test_home, "profiles", prof), exist_ok=True)
    cfg_path = os.path.join(test_home, "config.yaml")
    with open(cfg_path, "w", encoding="utf-8") as fh:
        fh.write("kanban:\n  fleet_scheduler:\n    enabled: true\n")
    monkeypatch.setenv("HERMES_HOME", test_home)
    for mod in list(sys.modules.keys()):
        if (
            mod.startswith("hermes_cli")
            or mod.startswith("hermes_state")
            or mod == "hermes_constants"
        ):
            del sys.modules[mod]
    from hermes_cli import kanban_db
    yield kanban_db


def _spawn(*_args, **_kwargs):
    return 12345


def _events(kb, conn, task_id, kind=None):
    events = kb.list_events(conn, task_id)
    out = []
    for ev in events:
        if kind and ev.kind != kind:
            continue
        payload = ev.payload
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                pass
        out.append((ev.kind, payload))
    return out


def _create_board(kb, conn):
    kb.create_board(slug="default", name="Fleet")


def test_legacy_unpooled_behavior_unchanged(kb_home):
    kb = kb_home
    with kb.connect_closing() as conn:
        _create_board(kb, conn)
        tid = kb.create_task(conn, title="legacy", assignee="alpha")
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_spawn, dry_run=False)
    assert len(res.spawned) == 1
    assert res.spawned[0][0] == tid
    assert res.spawned[0][1] == "alpha"
    assert res.skipped_fleet_deferred == []
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "running"
        assert task.assignee == "alpha"
        assert task.fleet_contract is None
        kinds = [k for k, _ in _events(kb, conn, tid)]
        assert "route_considered" in kinds
        receipt = [p for k, p in _events(kb, conn, tid, "route_considered") if p][0]
        assert receipt["kind"] == "legacy"
        assert receipt["selected"] == "alpha"
        assert receipt["pooled"] is False
        assert kb.get_active_lease(conn, task_id=tid) is None


def test_route_receipt_complete_for_pooled_and_legacy(kb_home):
    kb = kb_home
    with kb.connect_closing() as conn:
        _create_board(kb, conn)
        pooled = kb.create_task(
            conn, title="pooled", assignee="alpha", fleet_contract=_contract(),
        )
        kb.create_task(conn, title="legacy", assignee="beta")
    with kb.connect_closing() as conn:
        kb.dispatch_once(conn, spawn_fn=_spawn, dry_run=False)
        receipt = [p for k, p in _events(kb, conn, pooled, "route_considered") if p][0]
        assert receipt["kind"] == "selected"
        assert receipt["preferred"] == "alpha"
        assert receipt["selected"] == "alpha"
        assert {c["profile"] for c in receipt["considered"]} >= {"alpha"}
        for field in (
            "requested_profile",
            "requested_model",
            "requested_family",
            "configured_profile",
            "observed_model",
        ):
            assert field in receipt
        selected = [p for k, p in _events(kb, conn, pooled, "route_selected") if p]
        assert selected


def test_preferred_occupied_falls_back_to_peer(kb_home):
    kb = kb_home
    with kb.connect_closing() as conn:
        _create_board(kb, conn)
        blocker = kb.create_task(conn, title="hold-alpha", assignee="alpha")
        kb.claim_task(conn, blocker)
        tid = kb.create_task(
            conn, title="generic", assignee="alpha",
            fleet_contract=_contract(),
        )
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_spawn, max_in_progress_per_profile=1,
        )
    spawned = [s for s in res.spawned if s[0] == tid]
    assert spawned
    assert spawned[0][1] == "beta"
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task.preferred_assignee == "alpha"
        assert task.executing_profile == "beta"
        assert task.assignee == "beta"


def test_exact_pin_waits_without_substitution(kb_home):
    kb = kb_home
    with kb.connect_closing() as conn:
        _create_board(kb, conn)
        blocker = kb.create_task(conn, title="hold-alpha", assignee="alpha")
        kb.claim_task(conn, blocker)
        tid = kb.create_task(
            conn, title="exact", assignee="alpha",
            fleet_contract=_contract(exact_profile=True),
        )
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_spawn, max_in_progress_per_profile=1,
        )
    assert tid not in [s[0] for s in res.spawned]
    assert any(d[0] == tid for d in res.skipped_fleet_deferred)
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.assignee == "alpha"
        deferred = [p for k, p in _events(kb, conn, tid, "route_deferred") if p]
        assert deferred
        assert "exact" in deferred[0]["reason"]


def test_older_exact_waiter_capacity_protection(kb_home):
    kb = kb_home
    with kb.connect_closing() as conn:
        _create_board(kb, conn)
        blocker = kb.create_task(conn, title="hold-alpha", assignee="alpha")
        kb.claim_task(conn, blocker)
        exact = kb.create_task(
            conn, title="older-exact", assignee="beta",
            fleet_contract=_contract(
                preferred="beta", peers=[], exact_profile=True,
            ),
        )
        generic = kb.create_task(
            conn, title="newer-generic", assignee="alpha",
            fleet_contract=_contract(preferred="alpha", peers=["beta"]),
        )
        # Force older created_at on the exact waiter.
        conn.execute(
            "UPDATE tasks SET created_at = created_at - 10 WHERE id = ?",
            (exact,),
        )
        conn.commit()
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_spawn, max_in_progress_per_profile=1,
        )
    spawned_ids = [s[0] for s in res.spawned]
    assert exact in spawned_ids
    assert generic not in spawned_ids
    assert any(d[0] == generic for d in res.skipped_fleet_deferred)


def test_same_effective_root_collides_atomically(kb_home, tmp_path):
    kb = kb_home
    shared = tmp_path / "same-dir"
    shared.mkdir()
    with kb.connect_closing() as conn:
        _create_board(kb, conn)
        a = kb.create_task(
            conn, title="a", assignee="alpha",
            workspace_kind="dir", workspace_path=str(shared),
            fleet_contract=_contract(),
        )
        b = kb.create_task(
            conn, title="b", assignee="beta",
            workspace_kind="dir", workspace_path=str(shared),
            fleet_contract=_contract(preferred="beta", peers=["gamma"]),
        )
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_spawn)
    assert len(res.spawned) == 1
    assert len(res.lease_conflicts) == 1
    loser = res.lease_conflicts[0]
    with kb.connect_closing() as conn:
        loser_task = kb.get_task(conn, loser)
        assert loser_task.status == "ready"
        assert loser_task.consecutive_failures == 0
        winner = res.spawned[0][0]
        assert kb.get_active_lease(conn, task_id=winner) is not None


def test_distinct_worktree_siblings_can_run(kb_home, tmp_path):
    kb = kb_home
    repo = tmp_path / "repo"
    a_path = repo / ".worktrees" / "task-a"
    b_path = repo / ".worktrees" / "task-b"
    a_path.mkdir(parents=True)
    b_path.mkdir(parents=True)
    with kb.connect_closing() as conn:
        _create_board(kb, conn)
        a = kb.create_task(
            conn, title="wt-a", assignee="alpha",
            workspace_kind="dir",
            workspace_path=str(a_path),
            fleet_contract=_contract(),
        )
        b = kb.create_task(
            conn, title="wt-b", assignee="beta",
            workspace_kind="dir",
            workspace_path=str(b_path),
            fleet_contract=_contract(preferred="beta", peers=["gamma"]),
        )
        assert kb.normalize_mutation_root(a_path) != kb.normalize_mutation_root(b_path)
        assert kb.normalize_mutation_root(repo) != kb.normalize_mutation_root(a_path)
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_spawn)
    assert {s[0] for s in res.spawned} == {a, b}
    assert res.lease_conflicts == []


def test_timer_and_foreign_host_do_not_release_lease(kb_home, tmp_path):
    kb = kb_home
    root = tmp_path / "lease-dir"
    root.mkdir()
    with kb.connect_closing() as conn:
        _create_board(kb, conn)
        tid = kb.create_task(
            conn, title="leased", assignee="alpha",
            workspace_kind="dir", workspace_path=str(root),
            fleet_contract=_contract(),
        )
    with kb.connect_closing() as conn:
        kb.dispatch_once(conn, spawn_fn=_spawn)
        lease = kb.get_active_lease(conn, task_id=tid)
        assert lease is not None
        conn.execute(
            "UPDATE tasks SET claim_expires = ? WHERE id = ?",
            (int(time.time()) - 10, tid),
        )
        conn.commit()
        kb.release_stale_claims(conn)
        still = kb.get_active_lease(conn, task_id=tid)
        assert still is not None
        conn.execute(
            "UPDATE task_leases SET host_id = ? WHERE task_id = ?",
            ("other-host", tid),
        )
        conn.commit()
        ok, reason = kb.release_task_lease(
            conn,
            task_id=tid,
            claim_token=still["claim_token"],
            route_epoch=still["route_epoch"],
        )
        assert ok is False
        assert reason == "foreign_host"
        assert kb.get_active_lease(conn, task_id=tid) is not None


def test_matching_token_release_and_stale_epoch_rejected(kb_home, tmp_path):
    kb = kb_home
    root = tmp_path / "done-dir"
    root.mkdir()
    with kb.connect_closing() as conn:
        _create_board(kb, conn)
        tid = kb.create_task(
            conn, title="done", assignee="alpha",
            workspace_kind="dir", workspace_path=str(root),
            fleet_contract=_contract(),
        )
    with kb.connect_closing() as conn:
        kb.dispatch_once(conn, spawn_fn=_spawn)
        task = kb.get_task(conn, tid)
        assert kb.complete_task(
            conn, tid, expected_route_epoch="not-the-epoch",
        ) is False
        assert kb.get_task(conn, tid).status == "running"
        assert kb.get_active_lease(conn, task_id=tid) is not None
        assert kb.complete_task(
            conn, tid, expected_route_epoch=task.route_epoch,
        ) is True
        assert kb.get_task(conn, tid).status == "done"
        assert kb.get_active_lease(conn, task_id=tid) is None


def test_lease_conflict_does_not_burn_failure_breaker(kb_home, tmp_path):
    kb = kb_home
    shared = tmp_path / "collide"
    shared.mkdir()
    with kb.connect_closing() as conn:
        _create_board(kb, conn)
        kb.create_task(
            conn, title="first", assignee="alpha",
            workspace_kind="dir", workspace_path=str(shared),
            fleet_contract=_contract(),
        )
        second = kb.create_task(
            conn, title="second", assignee="beta",
            workspace_kind="dir", workspace_path=str(shared),
            fleet_contract=_contract(preferred="beta", peers=["gamma"]),
        )
    with kb.connect_closing() as conn:
        kb.dispatch_once(conn, spawn_fn=_spawn)
        task = kb.get_task(conn, second)
        assert task.status == "ready"
        assert task.consecutive_failures == 0


def test_reviewer_family_guard_and_reservation(kb_home):
    kb = kb_home
    with kb.connect_closing() as conn:
        _create_board(kb, conn)
        # Selecting claude as builder would consume the last reviewer family
        # besides itself when only claude is eligible.
        tid = kb.create_task(
            conn, title="needs-reviewer", assignee="delta",
            fleet_contract=_contract(
                preferred="delta",
                peers=["alpha"],
                eligible_reviewer_families=["claude"],
            ),
        )
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_spawn)
        # delta is claude-family; last reviewer family would be consumed.
        # Resolver should skip delta and take alpha (grok), reserving claude.
        spawned = [s for s in res.spawned if s[0] == tid]
        assert spawned
        assert spawned[0][1] == "alpha"
        task = kb.get_task(conn, tid)
        assert task.builder_family == "grok"
        assert task.reserved_reviewer_family == "claude"
        assert kb.request_review(conn, tid, reviewer="alpha", force=True) is False
        assert kb.request_review(conn, tid, reviewer="delta", force=True) is True


def test_selected_peer_not_preferred_gets_cap_accounting(kb_home):
    kb = kb_home
    with kb.connect_closing() as conn:
        _create_board(kb, conn)
        blocker = kb.create_task(conn, title="hold-alpha", assignee="alpha")
        kb.claim_task(conn, blocker)
        first = kb.create_task(
            conn, title="g1", assignee="alpha",
            fleet_contract=_contract(peers=["beta"]),
        )
        second = kb.create_task(
            conn, title="g2", assignee="alpha",
            fleet_contract=_contract(peers=["beta"]),
        )
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_spawn, max_in_progress_per_profile=1, dry_run=True,
        )
    selected = [s for s in res.spawned if s[0] in {first, second}]
    assert len(selected) == 1
    assert selected[0][1] == "beta"
    capped = [c for c in res.skipped_per_profile_capped if c[0] in {first, second}]
    # The second generic task must see beta at cap, not alpha.
    assert any(c[1] == "beta" for c in capped) or any(
        d[0] in {first, second} for d in res.skipped_fleet_deferred
    )


def test_path_aliases_normalize_to_same_lease_key(kb_home, tmp_path):
    kb = kb_home
    d = tmp_path / "AliasDir"
    d.mkdir()
    a = kb.normalize_mutation_root(d)
    b = kb.normalize_mutation_root(str(d) + os.sep)
    assert a == b
    if os.name == "nt":
        assert a == kb.normalize_mutation_root(str(d).upper())


def test_exact_model_override_mismatch_does_not_launch(kb_home):
    """Sol falsifier: EXACT_MODEL grok-4.6 wrong-model alpha"""
    kb = kb_home
    with kb.connect_closing() as conn:
        _create_board(kb, conn)
        tid = kb.create_task(
            conn,
            title="exact-mismatch",
            assignee="alpha",
            model_override="wrong-model",
            fleet_contract=_contract(exact_model="grok-4.6"),
        )
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_spawn)
        task = kb.get_task(conn, tid)
        receipt = [p for k, p in _events(kb, conn, tid, "route_considered") if p]
    launched = [s for s in res.spawned if s[0] == tid]
    observed = (
        "grok-4.6",
        task.model_override,
        launched[0][1] if launched else None,
    )
    assert observed != ("grok-4.6", "wrong-model", "alpha")
    assert launched == []
    assert task.status == "ready"
    assert any(p.get("reason") == "exact_model_override_mismatch" for p in receipt)


def test_atomic_claim_and_lease_have_no_committed_gap(kb_home, tmp_path):
    """Sol falsifier: ATOMIC_GAP selected running alpha True True True"""
    kb = kb_home
    root = tmp_path / "atomic-dir"
    root.mkdir()
    with kb.connect_closing() as conn:
        _create_board(kb, conn)
        tid = kb.create_task(
            conn,
            title="atomic",
            assignee="alpha",
            workspace_kind="dir",
            workspace_path=str(root),
            fleet_contract=_contract(),
        )
    with kb.connect_closing() as conn:
        real_acquire = kb.acquire_task_lease

        def boom(*args, **kwargs):
            raise RuntimeError("injected-lease-failure")

        kb.acquire_task_lease = boom
        try:
            try:
                kb.dispatch_once(conn, spawn_fn=_spawn)
            except RuntimeError:
                pass
        finally:
            kb.acquire_task_lease = real_acquire
        task = kb.get_task(conn, tid)
        lease = kb.get_active_lease(conn, task_id=tid)
        receipt = [p for k, p in _events(kb, conn, tid, "route_selected") if p]
        gap = (
            receipt[0]["kind"] if receipt else None,
            task.status,
            task.assignee,
            bool(receipt),
            task.status == "running",
            task.current_run_id is not None,
        )
        assert gap != ("selected", "running", "alpha", True, True, True)
        assert task.status != "running" or lease is not None

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_spawn)
        task = kb.get_task(conn, tid)
        lease = kb.get_active_lease(conn, task_id=tid)
        receipt = [p for k, p in _events(kb, conn, tid, "route_selected") if p]
        if res.spawned:
            assert task.status == "running"
            assert lease is not None
            assert receipt


def test_foreign_host_stale_claim_is_not_reclaimed(kb_home, tmp_path):
    """Sol falsifier: FOREIGN_RECLAIM 1 ready True"""
    kb = kb_home
    root = tmp_path / "foreign-dir"
    root.mkdir()
    with kb.connect_closing() as conn:
        _create_board(kb, conn)
        tid = kb.create_task(
            conn,
            title="foreign",
            assignee="alpha",
            workspace_kind="dir",
            workspace_path=str(root),
            fleet_contract=_contract(),
        )
    with kb.connect_closing() as conn:
        kb.dispatch_once(conn, spawn_fn=_spawn)
        conn.execute(
            "UPDATE tasks SET claim_lock = ?, claim_expires = ? WHERE id = ?",
            ("other-host:9999", int(time.time()) - 10, tid),
        )
        conn.execute(
            "UPDATE task_leases SET host_id = ? WHERE task_id = ?",
            ("other-host", tid),
        )
        conn.commit()
        reclaimed = kb.release_stale_claims(conn)
        task = kb.get_task(conn, tid)
        lease = kb.get_active_lease(conn, task_id=tid)
        observed = (reclaimed, task.status, lease is None)
        assert observed != (1, "ready", True)
        assert reclaimed == 0
        assert task.status == "running"
        assert lease is not None
        deferred = [p for k, p in _events(kb, conn, tid, "reclaim_deferred") if p]
        assert deferred
        assert deferred[0]["reason"] == "foreign_host"


def test_lease_run_fence_rejects_other_run_release(kb_home, tmp_path):
    """Sol falsifier: LEASE_RUN_FENCE 1 2 False True True"""
    kb = kb_home
    root = tmp_path / "fence-dir"
    root.mkdir()
    with kb.connect_closing() as conn:
        _create_board(kb, conn)
        tid = kb.create_task(
            conn,
            title="fence",
            assignee="alpha",
            workspace_kind="dir",
            workspace_path=str(root),
            fleet_contract=_contract(),
        )
    with kb.connect_closing() as conn:
        kb.dispatch_once(conn, spawn_fn=_spawn)
        task = kb.get_task(conn, tid)
        lease = kb.get_active_lease(conn, task_id=tid)
        run_1 = task.current_run_id
        assert run_1 is not None
        released, reason = kb.release_task_lease(
            conn,
            task_id=tid,
            claim_token=lease["claim_token"],
            route_epoch=lease["route_epoch"],
            run_id=int(run_1) + 1,
        )
        still = kb.get_active_lease(conn, task_id=tid)
        assert released is False
        assert reason == "run_id_mismatch"
        assert still is not None
        assert still["run_id"] == run_1
        ok_complete = kb.complete_task(conn, tid, expected_run_id=int(run_1) + 1)
        assert ok_complete is False
        assert kb.get_active_lease(conn, task_id=tid) is not None


def test_family_case_is_canonical(kb_home):
    """Sol falsifier: FAMILY_CASE (True, 'ok') Grok grok"""
    kb = kb_home
    with kb.connect_closing() as conn:
        _create_board(kb, conn)
        tid = kb.create_task(
            conn,
            title="case",
            assignee="alpha",
            fleet_contract=_contract(
                profile_families={"alpha": "Grok", "beta": "claude", "gamma": "sol"},
            ),
        )
    with kb.connect_closing() as conn:
        kb.dispatch_once(conn, spawn_fn=_spawn)
        allowed = kb.review_family_allowed(conn, tid, "alpha")
        task = kb.get_task(conn, tid)
        observed = (allowed, task.builder_family, "grok")
        assert allowed != (True, "ok")
        assert allowed[0] is False
        assert allowed[1] == "same_family_review"
        assert task.builder_family == "grok"


def test_material_task_requires_reviewer_reservation(kb_home):
    """Sol falsifier: REVIEW_RESERVATION_EMPTY selected alpha grok None"""
    kb = kb_home
    with kb.connect_closing() as conn:
        _create_board(kb, conn)
        tid = kb.create_task(
            conn,
            title="no-reviewer",
            assignee="alpha",
            fleet_contract=_contract(eligible_reviewer_families=[]),
        )
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_spawn)
        task = kb.get_task(conn, tid)
        receipt = [p for k, p in _events(kb, conn, tid, "route_considered") if p]
        selected = receipt[0] if receipt else {}
        observed = (
            selected.get("kind"),
            selected.get("selected"),
            selected.get("builder_family"),
            selected.get("reserved_reviewer_family"),
        )
        assert observed != ("selected", "alpha", "grok", None)
        assert tid not in [s[0] for s in res.spawned]
        assert task.status == "ready"
        assert task.reserved_reviewer_family is None


def test_default_off_keeps_legacy_unpooled(kb_home, monkeypatch):
    """Sol falsifier: DEFAULT_OFF selected beta capability_peer"""
    kb = kb_home
    monkeypatch.setattr(kb, "load_fleet_scheduler_config", lambda: {"enabled": False})
    with kb.connect_closing() as conn:
        _create_board(kb, conn)
        blocker = kb.create_task(conn, title="hold-alpha", assignee="alpha")
        kb.claim_task(conn, blocker)
        tid = kb.create_task(
            conn,
            title="should-stay-legacy",
            assignee="alpha",
            fleet_contract=_contract(),
        )
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_spawn, max_in_progress_per_profile=1,
        )
        task = kb.get_task(conn, tid)
        receipt = [p for k, p in _events(kb, conn, tid, "route_considered") if p]
        selected = receipt[0] if receipt else {}
        spawned = [s for s in res.spawned if s[0] == tid]
        observed = (
            selected.get("kind"),
            spawned[0][1] if spawned else None,
            selected.get("reason"),
        )
        assert observed != ("selected", "beta", "capability_peer")
        assert spawned == []
        assert task.status == "ready"
        assert task.assignee == "alpha"
        assert selected.get("kind") in {None, "legacy"}
        assert selected.get("pooled") is not True


def test_cli_create_fleet_contract_uses_real_resolver(kb_home):
    kb = kb_home
    from hermes_cli import kanban as kb_cli
    import argparse

    with kb.connect_closing() as conn:
        _create_board(kb, conn)
    ns = argparse.Namespace(
        title="cli-contract",
        body=None,
        assignee="alpha",
        created_by="user",
        workspace="scratch",
        tenant=None,
        priority=0,
        parent=None,
        triage=False,
        idempotency_key=None,
        max_runtime=None,
        skills=None,
        json=False,
        fleet_contract=json.dumps(_contract()),
        model_override=None,
        provider_override=None,
        goal_mode=False,
        goal_max_turns=None,
        initial_status="running",
        project=None,
        branch=None,
        max_retries=None,
    )
    rc = kb_cli._cmd_create(ns)
    assert rc == 0
    with kb.connect_closing() as conn:
        tasks = kb.list_tasks(conn)
        created = [t for t in tasks if t.title == "cli-contract"]
        assert created
        task = created[0]
        assert task.fleet_contract is not None
        decision = kb.resolve_fleet_route(
            assignee=task.assignee,
            contract=task.fleet_contract,
            profile_exists_fn=lambda _name: True,
        )
        assert decision.kind == "selected"
        assert decision.selected == "alpha"
        assert decision.reserved_reviewer_family in {"claude", "sol"}
        parsed = kb.parse_fleet_contract_arg(
            json.dumps(task.fleet_contract), assignee=task.assignee,
        )
        assert parsed["preferred"] == "alpha"


def _park(kb, conn, task_id):
    conn.execute(
        "UPDATE tasks SET status = 'done', claim_lock = NULL, "
        "claim_expires = NULL, worker_pid = NULL WHERE id = ?",
        (task_id,),
    )
    conn.commit()


def _leased_task(kb, tmp_path, title="leased"):
    root = tmp_path / f"{title}-dir"
    root.mkdir()
    with kb.connect_closing() as conn:
        _create_board(kb, conn)
        tid = kb.create_task(
            conn,
            title=title,
            assignee="alpha",
            workspace_kind="dir",
            workspace_path=str(root),
            fleet_contract=_contract(),
        )
    return tid, root


def _lease_count(conn, task_id):
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM task_leases WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    return int(row["n"])


def _capture_claim_hooks():
    from hermes_cli.plugins import get_plugin_manager

    events = []
    mgr = get_plugin_manager()
    saved = {k: list(v) for k, v in mgr._hooks.items()}
    mgr._hooks.setdefault("kanban_task_claimed", []).append(
        lambda **kw: events.append(kw)
    )
    return events, mgr, saved


def test_spawn_failure_releases_lease_and_retry_dispatches(kb_home, tmp_path):
    """Sol falsifier: SPAWN_RETRY_LEASE ready True 1 0 ['<task>'] ready 1"""
    kb = kb_home
    tid, _root = _leased_task(kb, tmp_path, "spawn-retry")

    def boom(*_args, **_kwargs):
        raise RuntimeError("injected-spawn-failure")

    with kb.connect_closing() as conn:
        kb.dispatch_once(conn, spawn_fn=boom)
        task = kb.get_task(conn, tid)
        lease = kb.get_active_lease(conn, task_id=tid)
        ready_ids = [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM tasks WHERE status = 'ready'"
            ).fetchall()
        ]
        observed = (
            task.status,
            lease is not None,
            task.consecutive_failures,
            0,
            ready_ids,
            task.status,
            1 if lease is not None else 0,
        )
        assert observed != ("ready", True, 1, 0, [tid], "ready", 1)
        assert task.status == "ready"
        assert lease is None
        assert task.consecutive_failures == 1

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_spawn)
        task = kb.get_task(conn, tid)
        lease = kb.get_active_lease(conn, task_id=tid)
        spawned = [s[0] for s in res.spawned]
        assert spawned == [tid]
        assert task.status == "running"
        assert lease is not None
        assert lease["task_id"] == tid
        assert lease["run_id"] == task.current_run_id
        assert lease["claim_token"] == task.claim_lock
        assert lease["host_id"] == kb.fleet_host_id()
        assert lease["route_epoch"] == task.route_epoch


def test_review_changes_then_builder_rework_dispatches(kb_home, tmp_path):
    """Sol falsifier: REWORK_LEASE (True, 'alpha') 2 2 0 ['<task>'] ready"""
    kb = kb_home
    tid, _root = _leased_task(kb, tmp_path, "rework")
    with kb.connect_closing() as conn:
        first = kb.dispatch_once(conn, spawn_fn=_spawn)
        assert [s[0] for s in first.spawned] == [tid]
        builder = kb.get_task(conn, tid)
        builder_run = builder.current_run_id
        assert kb.request_review(
            conn, tid, reviewer="delta", expected_run_id=builder_run,
        ) is True
        review_lease = kb.get_active_lease(conn, task_id=tid)
        assert review_lease is not None
        assert review_lease["run_id"] is None
        review = kb.dispatch_once(conn, spawn_fn=_spawn)
        assert [s[0] for s in review.spawned] == [tid]
        reviewing = kb.get_task(conn, tid)
        review_run = reviewing.current_run_id
        assert review_run != builder_run
        transferred = kb.get_active_lease(conn, task_id=tid)
        assert transferred is not None
        assert transferred["run_id"] == review_run
        ok, implementer = kb.request_changes(
            conn, tid, reason="need builder rework", expected_run_id=review_run,
        )
        after_changes = kb.get_task(conn, tid)
        leftover = kb.get_active_lease(conn, task_id=tid)
        observed = (
            (ok, implementer),
            review_run,
            leftover["run_id"] if leftover is not None else None,
            after_changes.consecutive_failures,
            [tid],
            after_changes.status,
        )
        assert observed != ((True, "alpha"), 2, 2, 0, [tid], "ready")
        assert ok is True
        assert implementer == "alpha"
        assert leftover is None
        assert after_changes.status == "ready"
        assert after_changes.assignee == "alpha"
        rework = kb.dispatch_once(conn, spawn_fn=_spawn)
        reworked = kb.get_task(conn, tid)
        new_lease = kb.get_active_lease(conn, task_id=tid)
        assert [s[0] for s in rework.spawned] == [tid]
        assert reworked.status == "running"
        assert reworked.assignee == "alpha"
        assert new_lease is not None
        assert new_lease["run_id"] == reworked.current_run_id
        assert new_lease["run_id"] != review_run


def test_review_spawn_failure_keeps_reservation_and_retries(kb_home, tmp_path):
    kb = kb_home
    tid, _root = _leased_task(kb, tmp_path, "review-spawn")
    with kb.connect_closing() as conn:
        kb.dispatch_once(conn, spawn_fn=_spawn)
        builder = kb.get_task(conn, tid)
        assert kb.request_review(
            conn, tid, reviewer="delta", expected_run_id=builder.current_run_id,
        ) is True
        reserved = kb.get_active_lease(conn, task_id=tid)
        assert reserved is not None
        assert reserved["run_id"] is None

        def boom(*_args, **_kwargs):
            raise RuntimeError("review-spawn-failure")

        kb.dispatch_once(conn, spawn_fn=boom)
        after = kb.get_task(conn, tid)
        parked = kb.get_active_lease(conn, task_id=tid)
        assert after.status == "review"
        assert parked is not None
        assert parked["lease_key"] == reserved["lease_key"]
        assert parked["run_id"] is None
        retry = kb.dispatch_once(conn, spawn_fn=_spawn)
        reviewing = kb.get_task(conn, tid)
        live = kb.get_active_lease(conn, task_id=tid)
        assert tid in [s[0] for s in retry.spawned]
        assert reviewing.status == "running"
        assert live is not None
        assert live["run_id"] == reviewing.current_run_id
        assert live["lease_key"] == reserved["lease_key"]


def test_two_connection_capacity_uses_live_sqlite_counts(kb_home):
    """Sol falsifier: CAPACITY_2CONN {'gamma': 1} selected alpha True {'alpha': 2, 'gamma': 1}"""
    kb = kb_home
    with kb.connect_closing() as conn:
        _create_board(kb, conn)
        gamma = kb.create_task(conn, title="hold-gamma", assignee="gamma")
        kb.claim_task(conn, gamma)
        first = kb.create_task(
            conn, title="alpha-one", assignee="alpha",
            fleet_contract=_contract(exact_profile=True, peers=[]),
        )
        second = kb.create_task(
            conn, title="alpha-two", assignee="alpha",
            fleet_contract=_contract(exact_profile=True, peers=[]),
        )
    stale_map = {"gamma": 1}
    exists = lambda _name: True
    conn1 = kb.connect()
    conn2 = kb.connect()
    try:
        task1 = kb.get_task(conn1, first)
        task2 = kb.get_task(conn2, second)
        r1 = kb.commit_fleet_ready_claim(
            conn1,
            ready_task=task1,
            row_assignee="alpha",
            running=stale_map,
            cap=1,
            profile_exists_fn=exists,
            ttl_seconds=None,
        )
        r2 = kb.commit_fleet_ready_claim(
            conn2,
            ready_task=task2,
            row_assignee="alpha",
            running=stale_map,
            cap=1,
            profile_exists_fn=exists,
            ttl_seconds=None,
        )
        live = kb._live_profile_running(conn2)
        observed = (
            stale_map,
            r2.route.kind,
            r2.route.selected,
            r2.task is not None,
            live,
        )
        assert observed != (
            {"gamma": 1},
            "selected",
            "alpha",
            True,
            {"alpha": 2, "gamma": 1},
        )
        assert r1.task is not None
        assert r1.route.selected == "alpha"
        assert r2.task is None
        assert r2.deferred is True
        assert live.get("alpha") == 1
        assert live.get("gamma") == 1
    finally:
        conn1.close()
        conn2.close()


def test_one_task_cannot_own_two_lease_keys(kb_home, tmp_path):
    """Sol falsifier: MULTI_LEASE True acquired 2"""
    kb = kb_home
    tid, root = _leased_task(kb, tmp_path, "one-lease")
    other = tmp_path / "other-root"
    other.mkdir()
    with kb.connect_closing() as conn:
        kb.dispatch_once(conn, spawn_fn=_spawn)
        assert kb._task_leases_has_task_unique(conn) is True
        task = kb.get_task(conn, tid)
        first = kb.get_active_lease(conn, task_id=tid)
        assert first is not None
        ok, reason = kb.acquire_task_lease(
            conn,
            lease_key=kb.normalize_mutation_root(other),
            task_id=tid,
            claim_token=task.claim_lock,
            route_epoch=task.route_epoch,
            run_id=task.current_run_id,
        )
        rows = kb._lease_rows_for_task(conn, tid)
        observed = (ok, reason if not ok else "acquired", len(rows))
        assert observed != (True, "acquired", 2)
        assert ok is False
        assert reason == "task_already_leased"
        assert len(rows) == 1
        assert rows[0]["lease_key"] == first["lease_key"]


def test_duplicate_task_lease_migration_preserves_evidence(tmp_path, monkeypatch):
    import sqlite3
    from hermes_cli import kanban_db as kb

    conn = sqlite3.connect(str(tmp_path / "legacy-leases.db"))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE task_leases (
            lease_key    TEXT PRIMARY KEY,
            task_id      TEXT NOT NULL,
            run_id       INTEGER,
            claim_token  TEXT NOT NULL,
            host_id      TEXT NOT NULL,
            route_epoch  TEXT NOT NULL,
            acquired_at  INTEGER NOT NULL
        )
        """
    )
    conn.executemany(
        """
        INSERT INTO task_leases (
            lease_key, task_id, run_id, claim_token, host_id, route_epoch, acquired_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("root-a", "t_dup", 1, "tok-a", "host", "epoch", 1),
            ("root-b", "t_dup", 2, "tok-b", "host", "epoch", 2),
        ],
    )
    conn.commit()
    kb._migrate_task_leases_one_task(conn)
    live = conn.execute(
        "SELECT lease_key FROM task_leases WHERE task_id = 't_dup' "
        "ORDER BY lease_key"
    ).fetchall()
    snapshot = conn.execute(
        "SELECT lease_key, disposition FROM task_leases_legacy_duplicates "
        "ORDER BY lease_key"
    ).fetchall()
    assert [row["lease_key"] for row in live] == ["root-a", "root-b"]
    assert [row["lease_key"] for row in snapshot] == ["root-a", "root-b"]
    assert all(
        row["disposition"] == "preserved_duplicate_task_lease" for row in snapshot
    )
    assert kb._task_leases_has_task_unique(conn) is False
    ok, reason = kb.acquire_task_lease(
        conn,
        lease_key="root-c",
        task_id="t_dup",
        claim_token="tok-c",
        route_epoch="epoch",
        run_id=3,
        host_id="host",
    )
    assert ok is False
    assert reason == "task_already_leased"
    assert kb.get_active_lease(conn, task_id="t_dup") is None
    released, rel_reason = kb.release_task_lease(
        conn,
        task_id="t_dup",
        claim_token="tok-a",
        route_epoch="epoch",
        run_id=1,
        host_id="host",
    )
    assert released is False
    assert rel_reason == "ambiguous_task_lease"
    still = conn.execute(
        "SELECT COUNT(*) AS n FROM task_leases WHERE task_id = 't_dup'"
    ).fetchone()
    assert int(still["n"]) == 2
    conn.close()


def test_provider_model_receipt_persist_and_spawn_agree(kb_home, tmp_path):
    """Sol falsifier: LAUNCH_PROVIDER UNKNOWN {'provider': 'xai', 'model': 'grok-4.6'}"""
    kb = kb_home
    captured = {}

    def capture(task, workspace, **_kwargs):
        captured["task"] = task
        captured["argv"] = kb.fleet_persisted_launch_args(task)
        captured["workspace"] = workspace
        return 4242

    root = tmp_path / "provider-dir"
    root.mkdir()
    with kb.connect_closing() as conn:
        _create_board(kb, conn)
        tid = kb.create_task(
            conn,
            title="provider-parity",
            assignee="alpha",
            workspace_kind="dir",
            workspace_path=str(root),
            model_override="grok-4.6",
            provider_override="xai",
            fleet_contract=_contract(),
        )
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=capture)
        task = kb.get_task(conn, tid)
        receipt = [p for k, p in _events(kb, conn, tid, "route_selected") if p][0]
        launch = {
            "provider": receipt.get("launch_provider"),
            "model": receipt.get("launch_model"),
        }
        assert launch != {"provider": "UNKNOWN", "model": "UNKNOWN"}
        assert launch == {"provider": "xai", "model": "grok-4.6"}
        assert task.provider_override == "xai"
        assert task.model_override == "grok-4.6"
        assert captured["argv"] == ["-m", "grok-4.6", "--provider", "xai"]
        assert [s[0] for s in res.spawned] == [tid]
        assert (
            receipt["launch_provider"],
            receipt["launch_model"],
            task.provider_override,
            task.model_override,
            captured["argv"],
        ) == (
            "xai",
            "grok-4.6",
            "xai",
            "grok-4.6",
            ["-m", "grok-4.6", "--provider", "xai"],
        )


def test_dispatcher_claim_hook_fires_once_after_commit(kb_home, tmp_path):
    """Sol falsifier: DISPATCH_CLAIM_HOOK []"""
    kb = kb_home
    from hermes_cli.plugins import get_plugin_manager

    events = []
    mgr = get_plugin_manager()
    saved = {k: list(v) for k, v in mgr._hooks.items()}
    mgr._hooks.setdefault("kanban_task_claimed", []).append(
        lambda **kw: events.append(kw)
    )
    try:
        tid, _root = _leased_task(kb, tmp_path, "hook-ok")
        with kb.connect_closing() as conn:
            kb.dispatch_once(conn, spawn_fn=_spawn)
        fired = [e["task_id"] for e in events]
        assert fired != []
        assert fired == [tid]
        assert events[0]["run_id"] is not None
        assert events[0]["assignee"] == "alpha"

        events.clear()
        with kb.connect_closing() as conn:
            blocker = kb.create_task(conn, title="hold-alpha", assignee="alpha")
            kb.claim_task(conn, blocker)
            events.clear()
            waiting = kb.create_task(
                conn,
                title="exact-wait",
                assignee="alpha",
                fleet_contract=_contract(exact_profile=True),
            )
            kb.dispatch_once(
                conn, spawn_fn=_spawn, max_in_progress_per_profile=1,
            )
            waiting_task = kb.get_task(conn, waiting)
        assert waiting_task.status == "ready"
        assert events == []

        events.clear()
        with kb.connect_closing() as conn:
            shared = tmp_path / "hook-collide"
            shared.mkdir()
            kb.create_task(
                conn,
                title="winner",
                assignee="alpha",
                workspace_kind="dir",
                workspace_path=str(shared),
                fleet_contract=_contract(),
            )
            loser = kb.create_task(
                conn,
                title="loser",
                assignee="beta",
                workspace_kind="dir",
                workspace_path=str(shared),
                fleet_contract=_contract(preferred="beta", peers=["gamma"]),
            )
            before = list(events)
            kb.dispatch_once(conn, spawn_fn=_spawn)
            loser_task = kb.get_task(conn, loser)
            loser_hooks = [e for e in events[len(before):] if e["task_id"] == loser]
        assert loser_task.status == "ready"
        assert loser_hooks == []
    finally:
        mgr._hooks = saved


def test_direct_claim_hook_still_fires_once(kb_home):
    kb = kb_home
    from hermes_cli.plugins import get_plugin_manager

    events = []
    mgr = get_plugin_manager()
    saved = {k: list(v) for k, v in mgr._hooks.items()}
    mgr._hooks.setdefault("kanban_task_claimed", []).append(
        lambda **kw: events.append(kw)
    )
    try:
        with kb.connect_closing() as conn:
            _create_board(kb, conn)
            tid = kb.create_task(conn, title="direct", assignee="alpha")
            claimed = kb.claim_task(conn, tid)
            assert claimed is not None
        assert [e["task_id"] for e in events] == [tid]
    finally:
        mgr._hooks = saved


def test_crash_timeout_rate_limit_block_and_stale_release_lease(kb_home, tmp_path, monkeypatch):
    kb = kb_home
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    tid, _root = _leased_task(kb, tmp_path, "endings")
    with kb.connect_closing() as conn:
        kb.dispatch_once(conn, spawn_fn=_spawn)
        task = kb.get_task(conn, tid)
        assert kb.get_active_lease(conn, task_id=tid) is not None
        conn.execute(
            "UPDATE tasks SET worker_pid = ?, started_at = ?, "
            "max_runtime_seconds = 1 WHERE id = ?",
            (71001, int(time.time()) - 30, tid),
        )
        conn.commit()
        kb._record_worker_exit(71001, 1 << 8)
        crashed = kb.detect_crashed_workers(conn)
        assert tid in crashed
        assert kb.get_task(conn, tid).status == "ready"
        assert kb.get_active_lease(conn, task_id=tid) is None
        _park(kb, conn, tid)

    tid2, _root2 = _leased_task(kb, tmp_path, "timeout")
    with kb.connect_closing() as conn:
        kb.dispatch_once(conn, spawn_fn=_spawn)
        run_id = kb.get_task(conn, tid2).current_run_id
        old = int(time.time()) - 30
        conn.execute(
            "UPDATE tasks SET worker_pid = ?, started_at = ?, "
            "max_runtime_seconds = 1 WHERE id = ?",
            (71002, old, tid2),
        )
        if run_id is not None:
            conn.execute(
                "UPDATE task_runs SET started_at = ? WHERE id = ?",
                (old, int(run_id)),
            )
        conn.commit()
        timed = kb.enforce_max_runtime(conn, signal_fn=lambda *_a, **_k: None)
        assert tid2 in timed
        assert kb.get_active_lease(conn, task_id=tid2) is None
        assert kb.get_task(conn, tid2).status == "ready"
        _park(kb, conn, tid2)

    tid3, _root3 = _leased_task(kb, tmp_path, "rate-limit")
    with kb.connect_closing() as conn:
        kb.dispatch_once(conn, spawn_fn=_spawn)
        conn.execute(
            "UPDATE tasks SET worker_pid = ? WHERE id = ?",
            (71003, tid3),
        )
        conn.commit()
        kb._record_worker_exit(71003, kb.KANBAN_RATE_LIMIT_EXIT_CODE << 8)
        kind, _code = kb._classify_worker_exit(71003)
        crashed = kb.detect_crashed_workers(conn)
        assert kb.get_task(conn, tid3).status == "ready"
        assert kb.get_active_lease(conn, task_id=tid3) is None
        if kind == "rate_limited":
            assert tid3 not in crashed
            assert tid3 in getattr(kb.detect_crashed_workers, "_last_rate_limited", [])
            assert kb.get_task(conn, tid3).consecutive_failures == 0
        _park(kb, conn, tid3)

    tid4, _root4 = _leased_task(kb, tmp_path, "blocked")
    with kb.connect_closing() as conn:
        kb.dispatch_once(conn, spawn_fn=_spawn)
        assert kb.block_task(conn, tid4, reason="wait", kind="needs_input") is True
        assert kb.get_task(conn, tid4).status == "blocked"
        assert kb.get_active_lease(conn, task_id=tid4) is None
        assert kb.unblock_task(conn, tid4) is True
        assert kb.get_active_lease(conn, task_id=tid4) is None
        res = kb.dispatch_once(conn, spawn_fn=_spawn)
        assert tid4 in [s[0] for s in res.spawned]
        assert kb.get_active_lease(conn, task_id=tid4) is not None
        _park(kb, conn, tid4)

    tid5, _root5 = _leased_task(kb, tmp_path, "stale")
    with kb.connect_closing() as conn:
        kb.dispatch_once(conn, spawn_fn=_spawn)
        conn.execute(
            "UPDATE tasks SET claim_expires = ?, worker_pid = NULL WHERE id = ?",
            (int(time.time()) - 10, tid5),
        )
        conn.commit()
        reclaimed = kb.release_stale_claims(conn)
        assert reclaimed == 1
        assert kb.get_task(conn, tid5).status == "ready"
        assert kb.get_active_lease(conn, task_id=tid5) is None


def test_cli_subprocess_create_then_real_dispatch(kb_home, tmp_path):
    kb = kb_home
    worktree = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..")
    )
    with kb.connect_closing() as conn:
        _create_board(kb, conn)
    env = dict(os.environ)
    env["HERMES_HOME"] = os.environ["HERMES_HOME"]
    env["PYTHONPATH"] = worktree + os.pathsep + env.get("PYTHONPATH", "")
    contract = json.dumps(_contract())
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "hermes_cli.main",
            "kanban",
            "create",
            "cli-subproc",
            "--assignee",
            "alpha",
            "--fleet-contract",
            contract,
            "--model",
            "grok-4.6",
            "--provider",
            "xai",
            "--json",
        ],
        env=env,
        capture_output=True,
        text=True,
        cwd=worktree,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    created = json.loads(proc.stdout)
    tid = created["id"]
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.fleet_contract is not None
        assert task.model_override == "grok-4.6"
        assert task.provider_override == "xai"
        assert task.status == "ready"
        res = kb.dispatch_once(conn, spawn_fn=_spawn)
        after = kb.get_task(conn, tid)
        assert tid in [s[0] for s in res.spawned]
        assert after.status == "running"
        assert after.executing_profile == "alpha"


def test_archive_active_task_releases_lease(kb_home, tmp_path):
    kb = kb_home
    tid, _root = _leased_task(kb, tmp_path, "archive-active")
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_spawn)
        assert tid in [s[0] for s in res.spawned]
        assert kb.get_active_lease(conn, task_id=tid) is not None
        assert kb.archive_task(conn, tid) is True
        task = kb.get_task(conn, tid)
        assert task.status == "archived"
        assert kb.get_active_lease(conn, task_id=tid) is None
        assert _lease_count(conn, tid) == 0


def test_schedule_active_task_releases_lease(kb_home, tmp_path):
    kb = kb_home
    tid, _root = _leased_task(kb, tmp_path, "schedule-active")
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_spawn)
        assert tid in [s[0] for s in res.spawned]
        assert kb.get_active_lease(conn, task_id=tid) is not None
        assert kb.schedule_task(conn, tid, reason="park-for-later") is True
        task = kb.get_task(conn, tid)
        assert task.status == "scheduled"
        assert kb.get_active_lease(conn, task_id=tid) is None
        assert _lease_count(conn, tid) == 0


def test_local_orphan_reconciliation_releases_lease_and_retries(kb_home, tmp_path):
    kb = kb_home
    tid, _root = _leased_task(kb, tmp_path, "orphan-ready")
    with kb.connect_closing() as conn:
        first = kb.dispatch_once(conn, spawn_fn=_spawn)
        assert tid in [s[0] for s in first.spawned]
        assert kb.get_active_lease(conn, task_id=tid) is not None
        conn.execute(
            "UPDATE tasks SET claim_lock = NULL, claim_expires = NULL, "
            "worker_pid = NULL WHERE id = ?",
            (tid,),
        )
        conn.commit()
        reconciled = kb.reconcile_orphaned_running(conn)
        task = kb.get_task(conn, tid)
        assert tid in reconciled
        assert task.status == "ready"
        assert kb.get_active_lease(conn, task_id=tid) is None
        assert _lease_count(conn, tid) == 0
        retry = kb.dispatch_once(conn, spawn_fn=_spawn)
        after = kb.get_task(conn, tid)
        lease = kb.get_active_lease(conn, task_id=tid)
        assert tid in [s[0] for s in retry.spawned]
        assert after.status == "running"
        assert lease is not None
        assert lease["task_id"] == tid
        assert lease["run_id"] == after.current_run_id


def test_ancestor_reopen_invalidates_running_child_and_releases_lease(
    kb_home, tmp_path,
):
    kb = kb_home
    root = tmp_path / "reopen-child-dir"
    root.mkdir()
    with kb.connect_closing() as conn:
        _create_board(kb, conn)
        parent = kb.create_task(conn, title="ancestor", assignee="alpha")
        assert kb.complete_task(conn, parent) is True
        child = kb.create_task(
            conn,
            title="running-descendant",
            assignee="alpha",
            workspace_kind="dir",
            workspace_path=str(root),
            fleet_contract=_contract(),
            parents=[parent],
        )
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_spawn)
        assert child in [s[0] for s in res.spawned]
        before = kb.get_task(conn, child)
        run_id = before.current_run_id
        assert before.status == "running"
        assert kb.get_active_lease(conn, task_id=child) is not None
        result = kb.invalidate_descendants_for_parent_reopen(
            conn, parent, author="operator",
        )
        after = kb.get_task(conn, child)
        ended = conn.execute(
            "SELECT outcome, status, ended_at FROM task_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        invalidated_ids = [entry["id"] for entry in result["invalidated"]]
        assert child in invalidated_ids
        assert after.status == "todo"
        assert after.current_run_id is None
        assert kb.get_active_lease(conn, task_id=child) is None
        assert _lease_count(conn, child) == 0
        assert ended is not None
        assert ended["ended_at"] is not None
        assert ended["outcome"] == "reclaimed"


def test_delete_archived_and_ordinary_task_leaves_no_lease_row(kb_home, tmp_path):
    kb = kb_home
    archived_root = tmp_path / "delete-archived-dir"
    ordinary_root = tmp_path / "delete-ordinary-dir"
    archived_root.mkdir()
    ordinary_root.mkdir()
    with kb.connect_closing() as conn:
        _create_board(kb, conn)
        archived_id = kb.create_task(
            conn,
            title="delete-archived",
            assignee="alpha",
            workspace_kind="dir",
            workspace_path=str(archived_root),
            fleet_contract=_contract(),
        )
        ordinary_id = kb.create_task(
            conn,
            title="delete-ordinary",
            assignee="beta",
            workspace_kind="dir",
            workspace_path=str(ordinary_root),
            fleet_contract=_contract(preferred="beta", peers=["gamma"]),
        )
    with kb.connect_closing() as conn:
        first = kb.dispatch_once(conn, spawn_fn=_spawn)
        spawned = {item[0] for item in first.spawned}
        assert archived_id in spawned
        assert kb.get_active_lease(conn, task_id=archived_id) is not None
        assert kb.archive_task(conn, archived_id) is True
        assert kb.get_task(conn, archived_id).status == "archived"
        assert kb.delete_archived_task(conn, archived_id) is True
        assert kb.get_task(conn, archived_id) is None
        assert _lease_count(conn, archived_id) == 0

        if ordinary_id not in spawned:
            second = kb.dispatch_once(conn, spawn_fn=_spawn)
            assert ordinary_id in [item[0] for item in second.spawned]
        assert kb.get_task(conn, ordinary_id).status == "running"
        assert kb.delete_task(conn, ordinary_id) is True
        assert kb.get_task(conn, ordinary_id) is None
        assert _lease_count(conn, ordinary_id) == 0


def test_staged_unknown_root_conflict_emits_no_loser_claim_hook(
    kb_home, tmp_path, monkeypatch,
):
    kb = kb_home
    shared = tmp_path / "staged-collide"
    shared.mkdir()
    events, mgr, saved = _capture_claim_hooks()
    monkeypatch.setattr(kb, "resolve_workspace", lambda _task, **_kw: shared)
    try:
        with kb.connect_closing() as conn:
            _create_board(kb, conn)
            first = kb.create_task(
                conn,
                title="staged-a",
                assignee="alpha",
                workspace_kind="dir",
                fleet_contract=_contract(),
            )
            second = kb.create_task(
                conn,
                title="staged-b",
                assignee="beta",
                workspace_kind="dir",
                fleet_contract=_contract(preferred="beta", peers=["gamma"]),
            )
            res = kb.dispatch_once(conn, spawn_fn=_spawn)
            spawned_ids = [item[0] for item in res.spawned]
            conflicted = set(res.lease_conflicts)
            hook_ids = [event["task_id"] for event in events]
            assert conflicted == {first, second} - set(spawned_ids)
            assert len(spawned_ids) == 1
            assert len(conflicted) == 1
            winner = spawned_ids[0]
            loser = next(iter(conflicted))
            winner_task = kb.get_task(conn, winner)
            loser_task = kb.get_task(conn, loser)
            assert {winner, loser} == {first, second}
            assert winner_task.status == "running"
            assert loser_task.status == "ready"
            assert kb.get_active_lease(conn, task_id=winner) is not None
            assert kb.get_active_lease(conn, task_id=loser) is None
            assert hook_ids == [winner]
    finally:
        mgr._hooks = saved


def test_staged_workspace_resolution_failure_emits_no_claim_hook(kb_home):
    kb = kb_home
    events, mgr, saved = _capture_claim_hooks()
    try:
        with kb.connect_closing() as conn:
            _create_board(kb, conn)
            tid = kb.create_task(
                conn,
                title="unresolved-dir",
                assignee="alpha",
                workspace_kind="dir",
                fleet_contract=_contract(),
            )
            res = kb.dispatch_once(conn, spawn_fn=_spawn)
            task = kb.get_task(conn, tid)
            deferred = [item for item in res.skipped_fleet_deferred if item[0] == tid]
            assert task.status == "ready"
            assert tid not in [item[0] for item in res.spawned]
            assert deferred
            assert deferred[0][1] == "workspace_unresolved"
            assert kb.get_active_lease(conn, task_id=tid) is None
            assert _lease_count(conn, tid) == 0
            assert [event["task_id"] for event in events] == []
    finally:
        mgr._hooks = saved


def _set_lease_fields(conn, task_id, **fields):
    assignments = ", ".join(f"{column} = ?" for column in fields)
    conn.execute(
        f"UPDATE task_leases SET {assignments} WHERE task_id = ?",
        (*fields.values(), task_id),
    )
    conn.commit()


def _insert_legacy_duplicate_evidence(conn, task_id, lease_key):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS task_leases_legacy_duplicates (
            lease_key    TEXT NOT NULL,
            task_id      TEXT NOT NULL,
            run_id       INTEGER,
            claim_token  TEXT,
            host_id      TEXT,
            route_epoch  TEXT,
            acquired_at  INTEGER,
            recorded_at  INTEGER NOT NULL,
            disposition  TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO task_leases_legacy_duplicates (
            lease_key, task_id, run_id, claim_token, host_id,
            route_epoch, acquired_at, recorded_at, disposition
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (lease_key, task_id, 1, "tok", "host", "epoch", 1, 1, "preserved_duplicate_task_lease"),
    )
    conn.commit()


def _legacy_evidence_count(conn, task_id):
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM task_leases_legacy_duplicates WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    except Exception:
        return 0
    return int(row["n"])


def test_archive_token_mismatch_and_foreign_host_fail_closed(kb_home, tmp_path):
    kb = kb_home
    mismatch_id, _root = _leased_task(kb, tmp_path, "archive-mismatch")
    foreign_root = tmp_path / "archive-foreign-dir"
    foreign_root.mkdir()
    with kb.connect_closing() as conn:
        foreign_id = kb.create_task(
            conn,
            title="archive-foreign",
            assignee="beta",
            workspace_kind="dir",
            workspace_path=str(foreign_root),
            fleet_contract=_contract(preferred="beta", peers=["gamma"]),
        )
        res = kb.dispatch_once(conn, spawn_fn=_spawn)
        spawned = {item[0] for item in res.spawned}
        assert mismatch_id in spawned
        assert foreign_id in spawned
        _set_lease_fields(conn, mismatch_id, claim_token="stolen-token")
        _set_lease_fields(conn, foreign_id, host_id="foreign-host")
        mismatch_before = kb.get_task(conn, mismatch_id)
        foreign_before = kb.get_task(conn, foreign_id)
        assert kb.archive_task(conn, mismatch_id) is False
        assert kb.archive_task(conn, foreign_id) is False
        mismatch_after = kb.get_task(conn, mismatch_id)
        foreign_after = kb.get_task(conn, foreign_id)
        assert mismatch_after.status == mismatch_before.status == "running"
        assert foreign_after.status == foreign_before.status == "running"
        assert _lease_count(conn, mismatch_id) == 1
        assert _lease_count(conn, foreign_id) == 1
        assert kb.get_active_lease(conn, task_id=mismatch_id)["claim_token"] == "stolen-token"
        assert kb.get_active_lease(conn, task_id=foreign_id)["host_id"] == "foreign-host"


def test_schedule_token_mismatch_and_foreign_host_fail_closed(kb_home, tmp_path):
    kb = kb_home
    mismatch_id, _root = _leased_task(kb, tmp_path, "schedule-mismatch")
    foreign_root = tmp_path / "schedule-foreign-dir"
    foreign_root.mkdir()
    with kb.connect_closing() as conn:
        foreign_id = kb.create_task(
            conn,
            title="schedule-foreign",
            assignee="beta",
            workspace_kind="dir",
            workspace_path=str(foreign_root),
            fleet_contract=_contract(preferred="beta", peers=["gamma"]),
        )
        res = kb.dispatch_once(conn, spawn_fn=_spawn)
        spawned = {item[0] for item in res.spawned}
        assert mismatch_id in spawned
        assert foreign_id in spawned
        _set_lease_fields(conn, mismatch_id, claim_token="stolen-token")
        _set_lease_fields(conn, foreign_id, host_id="foreign-host")
        assert kb.schedule_task(conn, mismatch_id, reason="park") is False
        assert kb.schedule_task(conn, foreign_id, reason="park") is False
        assert kb.get_task(conn, mismatch_id).status == "running"
        assert kb.get_task(conn, foreign_id).status == "running"
        assert _lease_count(conn, mismatch_id) == 1
        assert _lease_count(conn, foreign_id) == 1
        assert kb.get_active_lease(conn, task_id=mismatch_id)["claim_token"] == "stolen-token"
        assert kb.get_active_lease(conn, task_id=foreign_id)["host_id"] == "foreign-host"


def test_delete_mismatch_preserves_task_lease_and_migration_evidence(kb_home, tmp_path):
    kb = kb_home
    ordinary_id, _root = _leased_task(kb, tmp_path, "delete-mismatch")
    archived_root = tmp_path / "delete-archived-mismatch-dir"
    archived_root.mkdir()
    with kb.connect_closing() as conn:
        archived_id = kb.create_task(
            conn,
            title="delete-archived-mismatch",
            assignee="beta",
            workspace_kind="dir",
            workspace_path=str(archived_root),
            fleet_contract=_contract(preferred="beta", peers=["gamma"]),
        )
        res = kb.dispatch_once(conn, spawn_fn=_spawn)
        spawned = {item[0] for item in res.spawned}
        assert ordinary_id in spawned
        if archived_id not in spawned:
            later = kb.dispatch_once(conn, spawn_fn=_spawn)
            assert archived_id in [item[0] for item in later.spawned]
        _insert_legacy_duplicate_evidence(conn, ordinary_id, "ordinary-evidence")
        _insert_legacy_duplicate_evidence(conn, archived_id, "archived-evidence")
        _set_lease_fields(conn, ordinary_id, claim_token="stolen-token", route_epoch="other-epoch")
        assert kb.delete_task(conn, ordinary_id) is False
        assert kb.get_task(conn, ordinary_id) is not None
        assert kb.get_task(conn, ordinary_id).status == "running"
        assert _lease_count(conn, ordinary_id) == 1
        assert _legacy_evidence_count(conn, ordinary_id) == 1

        assert kb.archive_task(conn, archived_id) is True
        assert kb.get_task(conn, archived_id).status == "archived"
        conn.execute(
            """
            INSERT INTO task_leases (
                lease_key, task_id, run_id, claim_token, host_id, route_epoch, acquired_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("leftover-archived", archived_id, 99, "not-ours", "foreign-host", "epoch-x", 1),
        )
        conn.commit()
        assert kb.delete_archived_task(conn, archived_id) is False
        assert kb.get_task(conn, archived_id) is not None
        assert kb.get_task(conn, archived_id).status == "archived"
        assert _lease_count(conn, archived_id) == 1
        assert _legacy_evidence_count(conn, archived_id) == 1


def test_ancestor_reopen_mismatched_lease_does_not_move_descendant(kb_home, tmp_path):
    kb = kb_home
    root = tmp_path / "reopen-mismatch-dir"
    root.mkdir()
    with kb.connect_closing() as conn:
        _create_board(kb, conn)
        parent = kb.create_task(conn, title="ancestor-mismatch", assignee="alpha")
        assert kb.complete_task(conn, parent) is True
        child = kb.create_task(
            conn,
            title="protected-descendant",
            assignee="alpha",
            workspace_kind="dir",
            workspace_path=str(root),
            fleet_contract=_contract(),
            parents=[parent],
        )
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_spawn)
        assert child in [item[0] for item in res.spawned]
        before = kb.get_task(conn, child)
        run_id = before.current_run_id
        _set_lease_fields(conn, child, claim_token="stolen-token", host_id="foreign-host")
        result = kb.invalidate_descendants_for_parent_reopen(
            conn, parent, author="operator",
        )
        after = kb.get_task(conn, child)
        lease = kb.get_active_lease(conn, task_id=child)
        ended = conn.execute(
            "SELECT ended_at FROM task_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        invalidated_ids = [entry["id"] for entry in result["invalidated"]]
        assert child not in invalidated_ids
        assert after.status == "running"
        assert after.current_run_id == run_id
        assert lease is not None
        assert lease["claim_token"] == "stolen-token"
        assert lease["host_id"] == "foreign-host"
        assert ended["ended_at"] is None


def test_orphan_foreign_or_mismatched_lease_does_not_requeue(kb_home, tmp_path):
    kb = kb_home
    tid, _root = _leased_task(kb, tmp_path, "orphan-foreign")
    with kb.connect_closing() as conn:
        first = kb.dispatch_once(conn, spawn_fn=_spawn)
        assert tid in [item[0] for item in first.spawned]
        conn.execute(
            "UPDATE tasks SET claim_lock = NULL, claim_expires = NULL, "
            "worker_pid = NULL WHERE id = ?",
            (tid,),
        )
        conn.commit()
        _set_lease_fields(conn, tid, host_id="foreign-host", claim_token="not-ours")
        reconciled = kb.reconcile_orphaned_running(conn)
        task = kb.get_task(conn, tid)
        lease = kb.get_active_lease(conn, task_id=tid)
        assert tid not in reconciled
        assert task.status == "running"
        assert lease is not None
        assert lease["host_id"] == "foreign-host"
        assert _lease_count(conn, tid) == 1
        retry = kb.dispatch_once(conn, spawn_fn=_spawn)
        after = kb.get_task(conn, tid)
        assert tid not in [item[0] for item in retry.spawned]
        assert after.status == "running"
        assert kb.get_active_lease(conn, task_id=tid)["host_id"] == "foreign-host"


def test_direct_claim_inside_write_txn_emits_one_hook(kb_home):
    kb = kb_home
    events, mgr, saved = _capture_claim_hooks()
    try:
        with kb.connect_closing() as conn:
            _create_board(kb, conn)
            tid = kb.create_task(conn, title="joined-claim", assignee="alpha")
            with kb.write_txn(conn):
                claimed = kb.claim_task(conn, tid)
                assert claimed is not None
                assert [event["task_id"] for event in events] == []
            task = kb.get_task(conn, tid)
            assert task.status == "running"
            assert task.current_run_id is not None
            assert [event["task_id"] for event in events] == [tid]
            assert events[0]["run_id"] == task.current_run_id
    finally:
        mgr._hooks = saved


# ---------------------------------------------------------------------------
# R4 falsifiers: reservation-token authority, atomic lifecycle disposal, and
# exact-once hook delivery across a failing post-COMMIT invariant check.
# ---------------------------------------------------------------------------

def _run_rows(conn, task_id):
    return [
        (
            row["id"], row["status"], row["outcome"],
            row["claim_lock"], row["ended_at"],
        )
        for row in conn.execute(
            "SELECT id, status, outcome, claim_lock, ended_at FROM task_runs "
            "WHERE task_id = ? ORDER BY id",
            (task_id,),
        ).fetchall()
    ]


def _event_kinds(kb, conn, task_id):
    return [ev.kind for ev in kb.list_events(conn, task_id)]


def _reserved_review_task(kb, tmp_path, title):
    """Park a real review reservation (task_leases.run_id IS NULL)."""
    tid, root = _leased_task(kb, tmp_path, title)
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_spawn)
        assert tid in [item[0] for item in res.spawned]
        builder = kb.get_task(conn, tid)
        assert kb.request_review(
            conn, tid, reviewer="delta", expected_run_id=builder.current_run_id,
        ) is True
        lease = kb.get_active_lease(conn, task_id=tid)
        assert lease is not None
        assert lease["run_id"] is None
    return tid, root


def test_tampered_review_reservation_survives_archive_and_delete(kb_home, tmp_path):
    """Sol R4 blocker 1: a run-less reservation must not authorize itself."""
    kb = kb_home
    tid, _root = _reserved_review_task(kb, tmp_path, "reservation-tamper")
    with kb.connect_closing() as conn:
        _insert_legacy_duplicate_evidence(conn, tid, "reservation-evidence")
        before = kb.get_task(conn, tid)
        runs_before = _run_rows(conn, tid)
        kinds_before = _event_kinds(kb, conn, tid)
        genuine_token = kb.get_active_lease(conn, task_id=tid)["claim_token"]
        assert genuine_token
        _set_lease_fields(conn, tid, claim_token="stolen-reservation-token")

        assert kb.archive_task(conn, tid) is False
        assert kb.delete_task(conn, tid) is False

        after = kb.get_task(conn, tid)
        assert after is not None
        assert after.status == before.status == "review"
        assert after.current_run_id == before.current_run_id
        assert _run_rows(conn, tid) == runs_before
        assert _event_kinds(kb, conn, tid) == kinds_before
        assert _legacy_evidence_count(conn, tid) == 1
        lease = kb.get_active_lease(conn, task_id=tid)
        assert lease is not None
        assert lease["run_id"] is None
        assert lease["claim_token"] == "stolen-reservation-token"
        assert _lease_count(conn, tid) == 1


def test_untampered_reservation_still_claims_into_review_run(kb_home, tmp_path):
    kb = kb_home
    tid, _root = _reserved_review_task(kb, tmp_path, "reservation-claim")
    with kb.connect_closing() as conn:
        parked = kb.get_active_lease(conn, task_id=tid)
        res = kb.dispatch_once(conn, spawn_fn=_spawn)
        assert tid in [item[0] for item in res.spawned]
        task = kb.get_task(conn, tid)
        live = kb.get_active_lease(conn, task_id=tid)
        assert task.status == "running"
        assert live is not None
        assert live["run_id"] == task.current_run_id
        assert live["lease_key"] == parked["lease_key"]


def test_untampered_reservation_still_reopens_review(kb_home, tmp_path):
    kb = kb_home
    tid, _root = _reserved_review_task(kb, tmp_path, "reservation-reopen")
    with kb.connect_closing() as conn:
        assert kb.reopen_review_task(conn, tid) is True
        assert kb.get_task(conn, tid).status in {"ready", "todo"}
        assert kb.get_active_lease(conn, task_id=tid) is None
        assert _lease_count(conn, tid) == 0


def test_untampered_reservation_still_completes_from_review(kb_home, tmp_path):
    kb = kb_home
    tid, _root = _reserved_review_task(kb, tmp_path, "reservation-complete")
    with kb.connect_closing() as conn:
        assert kb.complete_task(conn, tid, result="approved") is True
        assert kb.get_task(conn, tid).status == "done"
        assert _lease_count(conn, tid) == 0


def test_untampered_reservation_still_archives(kb_home, tmp_path):
    kb = kb_home
    tid, _root = _reserved_review_task(kb, tmp_path, "reservation-archive")
    with kb.connect_closing() as conn:
        assert kb.archive_task(conn, tid) is True
        assert kb.get_task(conn, tid).status == "archived"
        assert _lease_count(conn, tid) == 0


def test_untampered_reservation_still_deletes(kb_home, tmp_path):
    kb = kb_home
    tid, _root = _reserved_review_task(kb, tmp_path, "reservation-delete")
    with kb.connect_closing() as conn:
        assert kb.delete_task(conn, tid) is True
        assert kb.get_task(conn, tid) is None
        assert _lease_count(conn, tid) == 0


@pytest.mark.parametrize(
    "tamper",
    [
        {"claim_token": "stolen-token"},
        {"route_epoch": "other-epoch"},
        {"run_id": 987654},
        {"host_id": "foreign-host"},
    ],
    ids=["token", "epoch", "run", "host"],
)
@pytest.mark.parametrize("kind", ["needs_input", "dependency"])
def test_block_task_rolls_back_when_live_lease_disposal_fails(
    kb_home, tmp_path, tamper, kind,
):
    """Sol R4 blocker 1: block must not commit lifecycle state over a bad lease."""
    kb = kb_home
    tid, _root = _leased_task(kb, tmp_path, "block-fence")
    with kb.connect_closing() as conn:
        assert tid in [i[0] for i in kb.dispatch_once(conn, spawn_fn=_spawn).spawned]
        before = kb.get_task(conn, tid)
        runs_before = _run_rows(conn, tid)
        kinds_before = _event_kinds(kb, conn, tid)
        _set_lease_fields(conn, tid, **tamper)

        assert kb.block_task(conn, tid, reason="fence", kind=kind) is False

        after = kb.get_task(conn, tid)
        assert after.status == before.status == "running"
        assert after.current_run_id == before.current_run_id
        assert after.claim_lock == before.claim_lock
        assert after.block_kind == before.block_kind
        assert after.block_recurrences == before.block_recurrences
        assert _run_rows(conn, tid) == runs_before
        assert _event_kinds(kb, conn, tid) == kinds_before
        assert _lease_count(conn, tid) == 1


@pytest.mark.parametrize(
    "tamper",
    [
        {"claim_token": "stolen-token"},
        {"route_epoch": "other-epoch"},
        {"run_id": 987654},
        {"host_id": "foreign-host"},
    ],
    ids=["token", "epoch", "run", "host"],
)
def test_request_review_rolls_back_when_live_lease_disposal_fails(
    kb_home, tmp_path, tamper,
):
    kb = kb_home
    tid, _root = _leased_task(kb, tmp_path, "review-fence")
    with kb.connect_closing() as conn:
        assert tid in [i[0] for i in kb.dispatch_once(conn, spawn_fn=_spawn).spawned]
        before = kb.get_task(conn, tid)
        runs_before = _run_rows(conn, tid)
        kinds_before = _event_kinds(kb, conn, tid)
        _set_lease_fields(conn, tid, **tamper)

        ok, reason = kb.request_review(
            conn, tid, reviewer="delta",
            expected_run_id=before.current_run_id,
            with_reason=True,
        )
        assert ok is False
        assert reason

        after = kb.get_task(conn, tid)
        assert after.status == before.status == "running"
        assert after.current_run_id == before.current_run_id
        assert after.claim_lock == before.claim_lock
        assert after.assignee == before.assignee
        assert _run_rows(conn, tid) == runs_before
        assert _event_kinds(kb, conn, tid) == kinds_before
        assert "review_requested" not in _event_kinds(kb, conn, tid)
        assert _lease_count(conn, tid) == 1


def test_request_changes_rolls_back_when_lease_disposal_fails(kb_home, tmp_path):
    kb = kb_home
    tid, _root = _reserved_review_task(kb, tmp_path, "changes-fence")
    with kb.connect_closing() as conn:
        assert tid in [i[0] for i in kb.dispatch_once(conn, spawn_fn=_spawn).spawned]
        before = kb.get_task(conn, tid)
        assert before.status == "running"
        runs_before = _run_rows(conn, tid)
        kinds_before = _event_kinds(kb, conn, tid)
        _set_lease_fields(conn, tid, claim_token="stolen-token")

        ok, reason = kb.request_changes(
            conn, tid, reason="please rework",
            expected_run_id=before.current_run_id,
        )
        assert ok is False
        assert reason

        after = kb.get_task(conn, tid)
        assert after.status == "running"
        assert after.current_run_id == before.current_run_id
        assert after.assignee == before.assignee
        assert _run_rows(conn, tid) == runs_before
        assert _event_kinds(kb, conn, tid) == kinds_before
        assert "changes_requested" not in _event_kinds(kb, conn, tid)
        assert _lease_count(conn, tid) == 1


def test_reopen_review_rolls_back_when_reservation_disposal_fails(kb_home, tmp_path):
    kb = kb_home
    tid, _root = _reserved_review_task(kb, tmp_path, "reopen-fence")
    with kb.connect_closing() as conn:
        before = kb.get_task(conn, tid)
        runs_before = _run_rows(conn, tid)
        kinds_before = _event_kinds(kb, conn, tid)
        _set_lease_fields(conn, tid, claim_token="stolen-reservation-token")

        assert kb.reopen_review_task(conn, tid) is False

        after = kb.get_task(conn, tid)
        assert after.status == before.status == "review"
        assert after.assignee == before.assignee
        assert after.current_run_id == before.current_run_id
        assert _run_rows(conn, tid) == runs_before
        assert _event_kinds(kb, conn, tid) == kinds_before
        assert "review_reopened" not in _event_kinds(kb, conn, tid)
        assert _lease_count(conn, tid) == 1


def test_unblock_rolls_back_when_foreign_reservation_disposal_fails(kb_home, tmp_path):
    """A foreign reservation parked after a clean block must fence unblock."""
    kb = kb_home
    tid, root = _leased_task(kb, tmp_path, "unblock-fence")
    with kb.connect_closing() as conn:
        assert tid in [i[0] for i in kb.dispatch_once(conn, spawn_fn=_spawn).spawned]
        running = kb.get_task(conn, tid)
        assert kb.block_task(conn, tid, reason="hold", kind="needs_input") is True
        assert _lease_count(conn, tid) == 0

        # A different mutation owner parks a run-less reservation through the
        # real lease primitive. Its token is attested by no run of this task.
        ok, _detail = kb.acquire_task_lease(
            conn,
            lease_key=kb.normalize_mutation_root(root),
            task_id=tid,
            claim_token="foreign-reservation-token",
            route_epoch=running.route_epoch,
            run_id=None,
        )
        assert ok is True
        conn.commit()

        before = kb.get_task(conn, tid)
        assert before.status == "blocked"
        runs_before = _run_rows(conn, tid)
        kinds_before = _event_kinds(kb, conn, tid)

        assert kb.unblock_task(conn, tid) is False

        after = kb.get_task(conn, tid)
        assert after.status == "blocked"
        assert after.consecutive_failures == before.consecutive_failures
        assert _run_rows(conn, tid) == runs_before
        assert _event_kinds(kb, conn, tid) == kinds_before
        assert "unblocked" not in _event_kinds(kb, conn, tid)
        lease = kb.get_active_lease(conn, task_id=tid)
        assert lease is not None
        assert lease["claim_token"] == "foreign-reservation-token"
        assert _lease_count(conn, tid) == 1


def test_unblock_still_succeeds_without_a_lingering_lease(kb_home, tmp_path):
    kb = kb_home
    tid, _root = _leased_task(kb, tmp_path, "unblock-ok")
    with kb.connect_closing() as conn:
        assert tid in [i[0] for i in kb.dispatch_once(conn, spawn_fn=_spawn).spawned]
        assert kb.block_task(conn, tid, reason="hold", kind="needs_input") is True
        assert _lease_count(conn, tid) == 0
        assert kb.unblock_task(conn, tid) is True
        assert kb.get_task(conn, tid).status in {"ready", "todo"}
        assert _lease_count(conn, tid) == 0


def test_lease_conflict_restore_does_not_commit_over_failed_disposal(kb_home, tmp_path):
    kb = kb_home
    tid, _root = _leased_task(kb, tmp_path, "restore-fence")
    with kb.connect_closing() as conn:
        assert tid in [i[0] for i in kb.dispatch_once(conn, spawn_fn=_spawn).spawned]
        before = kb.get_task(conn, tid)
        runs_before = _run_rows(conn, tid)
        kinds_before = _event_kinds(kb, conn, tid)
        _set_lease_fields(conn, tid, claim_token="stolen-token")

        kb.restore_ready_after_lease_conflict(conn, tid, reason="fence-test")

        after = kb.get_task(conn, tid)
        assert after.status == "running"
        assert after.claim_lock == before.claim_lock
        assert after.current_run_id == before.current_run_id
        assert _run_rows(conn, tid) == runs_before
        assert _event_kinds(kb, conn, tid) == kinds_before
        assert "lease_conflict" not in _event_kinds(kb, conn, tid)
        assert _lease_count(conn, tid) == 1


def test_post_commit_invariant_failure_delivers_claim_hook_exactly_once(kb_home):
    """Sol R4 blocker 2: a durable commit must not leak its queued hook."""
    kb = kb_home
    events, mgr, saved = _capture_claim_hooks()
    original = kb._check_file_length_invariant

    def boom(_conn):
        raise sqlite3.DatabaseError("torn-extend detected (injected)")

    try:
        with kb.connect_closing() as conn:
            _create_board(kb, conn)
            tid = kb.create_task(conn, title="invariant-boom", assignee="alpha")
            unrelated = kb.create_task(conn, title="unrelated", assignee="alpha")

            kb._check_file_length_invariant = boom
            try:
                with pytest.raises(sqlite3.DatabaseError):
                    kb.claim_task(conn, tid)
            finally:
                kb._check_file_length_invariant = original

            claimed = kb.get_task(conn, tid)
            assert claimed.status == "running"
            assert claimed.current_run_id is not None
            assert [event["task_id"] for event in events] == [tid]
            assert events[0]["run_id"] == claimed.current_run_id
            assert getattr(conn, kb._AFTER_COMMIT_HOOKS_ATTR, []) == []

            events.clear()
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET priority = 5 WHERE id = ?", (unrelated,),
                )
            assert events == []
            assert kb.get_task(conn, unrelated).status == "ready"
    finally:
        kb._check_file_length_invariant = original
        mgr._hooks = saved


def test_claim_hook_queue_controls_are_preserved(kb_home):
    kb = kb_home
    events, mgr, saved = _capture_claim_hooks()
    original_boundary = kb._execute_boundary_with_retry
    try:
        with kb.connect_closing() as conn:
            _create_board(kb, conn)

            # Control 1 - ordinary direct commit fires exactly once.
            plain = kb.create_task(conn, title="plain", assignee="alpha")
            assert kb.claim_task(conn, plain) is not None
            assert [e["task_id"] for e in events] == [plain]

            # Control 2 - an outer rollback fires nothing.
            events.clear()
            rolled = kb.create_task(conn, title="rolled", assignee="alpha")
            with pytest.raises(RuntimeError):
                with kb.write_txn(conn):
                    assert kb.claim_task(conn, rolled) is not None
                    raise RuntimeError("outer boom")
            assert events == []
            assert kb.get_task(conn, rolled).status == "ready"
            assert getattr(conn, kb._AFTER_COMMIT_HOOKS_ATTR, []) == []

            # Control 3 - a nested savepoint rollback drops only inner hooks.
            events.clear()
            outer = kb.create_task(conn, title="outer", assignee="alpha")
            inner = kb.create_task(conn, title="inner", assignee="alpha")
            with kb.write_txn(conn):
                assert kb.claim_task(conn, outer) is not None
                with pytest.raises(RuntimeError):
                    with kb.write_txn(conn, allow_nested=True):
                        assert kb.claim_task(conn, inner) is not None
                        raise RuntimeError("inner boom")
                assert events == []
            assert [e["task_id"] for e in events] == [outer]
            assert kb.get_task(conn, outer).status == "running"
            assert kb.get_task(conn, inner).status == "ready"

            # Control 4 - a failing COMMIT boundary fires nothing.
            events.clear()
            doomed = kb.create_task(conn, title="doomed", assignee="alpha")

            def fail_commit(conn_, sql):
                if sql == "COMMIT":
                    raise sqlite3.OperationalError("commit refused (injected)")
                return original_boundary(conn_, sql)

            kb._execute_boundary_with_retry = fail_commit
            try:
                with pytest.raises(sqlite3.OperationalError):
                    with kb.write_txn(conn):
                        assert kb.claim_task(conn, doomed) is not None
            finally:
                kb._execute_boundary_with_retry = original_boundary
            assert events == []
            assert kb.get_task(conn, doomed).status == "ready"
            assert getattr(conn, kb._AFTER_COMMIT_HOOKS_ATTR, []) == []
    finally:
        kb._execute_boundary_with_retry = original_boundary
        mgr._hooks = saved


# ---------------------------------------------------------------------------
# R5 falsifiers: a run-less completion may never self-authorize lease disposal,
# and every shared lifecycle caller must roll back a failed ended-run disposal.
# ---------------------------------------------------------------------------

_DROP_CLAIMED_EVENT = object()


def _lease_rows(conn, task_id):
    return [
        (
            row["lease_key"], row["run_id"], row["claim_token"],
            row["host_id"], row["route_epoch"],
        )
        for row in conn.execute(
            "SELECT lease_key, run_id, claim_token, host_id, route_epoch "
            "FROM task_leases WHERE task_id = ? ORDER BY lease_key",
            (task_id,),
        ).fetchall()
    ]


def _lifecycle_state(kb, conn, task_id):
    """Externally observable task/run/event/lease state used for rollback proof."""
    task = kb.get_task(conn, task_id)
    return {
        "status": task.status,
        "result": task.result,
        "completed_at": task.completed_at,
        "claim_lock": task.claim_lock,
        "current_run_id": task.current_run_id,
        "consecutive_failures": task.consecutive_failures,
        "last_failure_error": task.last_failure_error,
        "block_kind": task.block_kind,
        "block_recurrences": task.block_recurrences,
        "runs": _run_rows(conn, task_id),
        "events": _event_kinds(kb, conn, task_id),
        "leases": _lease_rows(conn, task_id),
    }


def _running_leased_task(kb, tmp_path, title):
    """Dispatch a pooled task into a live run that holds its mutation lease."""
    tid, root = _leased_task(kb, tmp_path, title)
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_spawn)
        assert tid in [item[0] for item in res.spawned]
        task = kb.get_task(conn, tid)
        lease = kb.get_active_lease(conn, task_id=tid)
        assert task.status == "running"
        assert lease is not None
        assert lease["run_id"] == task.current_run_id
    return tid, root


def _parking_run_id(conn, task_id):
    row = conn.execute(
        "SELECT id FROM task_runs WHERE task_id = ? AND ended_at IS NOT NULL "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    assert row is not None
    return int(row["id"])


def _break_claimed_evidence(conn, task_id, payload):
    """Drop or rewrite the parking run's independent ``claimed`` evidence."""
    run_id = _parking_run_id(conn, task_id)
    row = conn.execute(
        "SELECT id FROM task_events WHERE task_id = ? AND run_id = ? "
        "AND kind = 'claimed' ORDER BY id DESC LIMIT 1",
        (task_id, run_id),
    ).fetchone()
    assert row is not None, "a reservation must start with independent evidence"
    if payload is _DROP_CLAIMED_EVENT:
        conn.execute("DELETE FROM task_events WHERE id = ?", (int(row["id"]),))
    else:
        conn.execute(
            "UPDATE task_events SET payload = ? WHERE id = ?",
            (payload, int(row["id"])),
        )
    conn.commit()


def _tamper_claimed_event_removed(conn, task_id):
    _break_claimed_evidence(conn, task_id, _DROP_CLAIMED_EVENT)


def _tamper_payload_not_json(conn, task_id):
    _break_claimed_evidence(conn, task_id, "{not json")


def _tamper_payload_missing_lock(conn, task_id):
    _break_claimed_evidence(conn, task_id, json.dumps({"assignee": "alpha"}))


def _tamper_payload_lock_wrong_type(conn, task_id):
    _break_claimed_evidence(conn, task_id, json.dumps({"lock": 1234}))


def _tamper_payload_lock_empty(conn, task_id):
    _break_claimed_evidence(conn, task_id, json.dumps({"lock": ""}))


def _tamper_payload_not_object(conn, task_id):
    _break_claimed_evidence(conn, task_id, json.dumps("claimed"))


def _tamper_payload_lock_mismatch(conn, task_id):
    _break_claimed_evidence(
        conn, task_id, json.dumps({"lock": "someone-else:deadbeefcafe"}),
    )


def _tamper_lease_run_id(conn, task_id):
    """Attach the parked reservation to a run that never existed."""
    _set_lease_fields(conn, task_id, run_id=987654)


def _tamper_task_route_epoch_cleared(conn, task_id):
    """Erase the task's own epoch so only the lease row still names one."""
    conn.execute(
        "UPDATE tasks SET route_epoch = NULL WHERE id = ?", (task_id,),
    )
    conn.commit()


_RESERVATION_TAMPERS = {
    "claimed_event_removed": _tamper_claimed_event_removed,
    "payload_not_json": _tamper_payload_not_json,
    "payload_missing_lock": _tamper_payload_missing_lock,
    "payload_lock_wrong_type": _tamper_payload_lock_wrong_type,
    "payload_lock_empty": _tamper_payload_lock_empty,
    "payload_not_object": _tamper_payload_not_object,
    "payload_lock_mismatch": _tamper_payload_lock_mismatch,
    "lease_run_id": _tamper_lease_run_id,
    "task_route_epoch_cleared": _tamper_task_route_epoch_cleared,
}


@pytest.mark.parametrize("tamper_id", sorted(_RESERVATION_TAMPERS))
def test_runless_completion_without_independent_authority_fails_closed(
    kb_home, tmp_path, tamper_id,
):
    """Sol R5 blocker 1: a lease row may never vouch for its own disposal.

    A parked review reservation still holds the mutation root. When the
    independently derived authority for that reservation is missing,
    malformed, ambiguous or mismatched, ``complete_task`` must refuse *and*
    leave task status, completion fields, events, run rows and the
    reservation exactly as they were — never commit ``done`` over a lease it
    could not prove it owns, and never fall back to the lease row's own
    token, epoch or run id.
    """
    kb = kb_home
    tid, _root = _reserved_review_task(kb, tmp_path, f"r5runless{len(tamper_id)}")
    with kb.connect_closing() as conn:
        _RESERVATION_TAMPERS[tamper_id](conn, tid)
        before = _lifecycle_state(kb, conn, tid)
        assert before["status"] == "review"
        assert len(before["leases"]) == 1

        assert kb.complete_task(conn, tid, result="approved") is False

        after = _lifecycle_state(kb, conn, tid)
        assert after == before
        assert after["status"] == "review"
        assert after["result"] is None
        assert after["completed_at"] is None
        assert "completed" not in after["events"]
        assert _lease_count(conn, tid) == 1


def test_untampered_reservation_completion_control(kb_home, tmp_path):
    """Control: a provable reservation still completes and releases its lease."""
    kb = kb_home
    tid, _root = _reserved_review_task(kb, tmp_path, "r5-runless-control")
    with kb.connect_closing() as conn:
        assert kb.complete_task(conn, tid, result="approved") is True
        task = kb.get_task(conn, tid)
        assert task.status == "done"
        assert task.result == "approved"
        assert task.completed_at is not None
        assert "completed" in _event_kinds(kb, conn, tid)
        assert kb.get_active_lease(conn, task_id=tid) is None
        assert _lease_count(conn, tid) == 0


def test_live_run_completion_control(kb_home, tmp_path):
    """Control: an ordinary running task with a live lease still completes."""
    kb = kb_home
    tid, _root = _running_leased_task(kb, tmp_path, "r5-live-control")
    with kb.connect_closing() as conn:
        run_id = kb.get_task(conn, tid).current_run_id
        assert kb.complete_task(
            conn, tid, result="shipped", expected_run_id=run_id,
        ) is True
        task = kb.get_task(conn, tid)
        assert task.status == "done"
        assert task.result == "shipped"
        assert "completed" in _event_kinds(kb, conn, tid)
        assert _lease_count(conn, tid) == 0


def test_live_run_completion_over_stolen_lease_preserves_state(kb_home, tmp_path):
    """A live-run completion still refuses a lease it cannot prove it owns."""
    kb = kb_home
    tid, _root = _running_leased_task(kb, tmp_path, "r5-live-fence")
    with kb.connect_closing() as conn:
        run_id = kb.get_task(conn, tid).current_run_id
        _set_lease_fields(conn, tid, claim_token="stolen-live-token")
        before = _lifecycle_state(kb, conn, tid)

        assert kb.complete_task(
            conn, tid, result="shipped", expected_run_id=run_id,
        ) is False

        after = _lifecycle_state(kb, conn, tid)
        assert after["status"] == before["status"] == "running"
        assert after["result"] is None
        assert after["completed_at"] is None
        assert after["current_run_id"] == before["current_run_id"]
        assert after["runs"] == before["runs"]
        assert after["leases"] == before["leases"]
        assert "completed" not in after["events"]
        assert _lease_count(conn, tid) == 1


# --- Sol R5 blocker 2: every shared ended-run disposal caller ---------------

def _noop_signal(*_args, **_kwargs):
    return None


def _drive_release_stale_claims(kb, conn, tid):
    conn.execute(
        "UPDATE tasks SET claim_expires = ?, worker_pid = NULL WHERE id = ?",
        (int(time.time()) - 10, tid),
    )
    conn.commit()
    return kb.release_stale_claims(conn, signal_fn=_noop_signal)


def _drive_reclaim_task(kb, conn, tid):
    conn.execute("UPDATE tasks SET worker_pid = NULL WHERE id = ?", (tid,))
    conn.commit()
    return kb.reclaim_task(conn, tid, reason="operator", signal_fn=_noop_signal)


def _drive_enforce_max_runtime(kb, conn, tid):
    old = int(time.time()) - 300
    run_id = kb.get_task(conn, tid).current_run_id
    conn.execute(
        "UPDATE tasks SET worker_pid = ?, started_at = ?, "
        "max_runtime_seconds = 1 WHERE id = ?",
        (71501, old, tid),
    )
    conn.execute(
        "UPDATE task_runs SET started_at = ? WHERE id = ?", (old, int(run_id)),
    )
    conn.commit()
    return kb.enforce_max_runtime(conn, signal_fn=_noop_signal)


def _drive_detect_stale_running(kb, conn, tid):
    old = int(time.time()) - 7200
    run_id = kb.get_task(conn, tid).current_run_id
    conn.execute(
        "UPDATE tasks SET worker_pid = NULL, started_at = ?, "
        "last_heartbeat_at = NULL WHERE id = ?",
        (old, tid),
    )
    conn.execute(
        "UPDATE task_runs SET started_at = ? WHERE id = ?", (old, int(run_id)),
    )
    conn.commit()
    return kb.detect_stale_running(
        conn, stale_timeout_seconds=1, signal_fn=_noop_signal,
    )


def _drive_detect_crashed_workers(kb, conn, tid):
    conn.execute("UPDATE tasks SET worker_pid = ? WHERE id = ?", (71502, tid))
    conn.commit()
    kb._record_worker_exit(71502, 1 << 8)
    return kb.detect_crashed_workers(conn)


_LIVE_RUN_DRIVERS = {
    "release_stale_claims": (_drive_release_stale_claims, 0),
    "reclaim_task": (_drive_reclaim_task, False),
    "enforce_max_runtime": (_drive_enforce_max_runtime, []),
    "detect_stale_running": (_drive_detect_stale_running, []),
    "detect_crashed_workers": (_drive_detect_crashed_workers, []),
}


@pytest.mark.parametrize("driver_id", sorted(_LIVE_RUN_DRIVERS))
def test_lifecycle_caller_rolls_back_failed_ended_run_disposal(
    kb_home, tmp_path, monkeypatch, driver_id,
):
    """Sol R5 blocker 2: no recovery path may commit over a failed disposal.

    Each of these paths ends the run and restores the task's source phase.
    When the mutation-root lease cannot be proven, the whole transition must
    roll back: status, claim identity, failure counters, current-run identity,
    run rows, events and the lease row all stay exactly as they were.
    """
    kb = kb_home
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    driver, expected = _LIVE_RUN_DRIVERS[driver_id]
    tid, _root = _running_leased_task(kb, tmp_path, f"r5caller{len(driver_id)}")
    with kb.connect_closing() as conn:
        _set_lease_fields(conn, tid, claim_token="stolen-lifecycle-token")
        before = _lifecycle_state(kb, conn, tid)
        assert before["status"] == "running"
        assert before["current_run_id"] is not None

        outcome = driver(kb, conn, tid)
        assert outcome == expected
        assert outcome is not True

        after = _lifecycle_state(kb, conn, tid)
        # The driver legitimately edits scheduling inputs (pid, expiry,
        # heartbeat, runtime bound) before the transition; only the
        # lifecycle-owned state below must survive untouched.
        for field in (
            "status", "result", "completed_at", "claim_lock",
            "current_run_id", "consecutive_failures", "last_failure_error",
            "block_kind", "block_recurrences", "runs", "events", "leases",
        ):
            assert after[field] == before[field], field
        assert after["status"] == "running"
        assert _lease_count(conn, tid) == 1
        lease = kb.get_active_lease(conn, task_id=tid)
        assert lease["run_id"] == before["current_run_id"]
        assert lease["claim_token"] == "stolen-lifecycle-token"


@pytest.mark.parametrize(
    "failure_limit", [2, 1], ids=["below_threshold", "breaker_trip"],
)
def test_spawn_failure_rolls_back_when_lease_disposal_fails(
    kb_home, tmp_path, failure_limit,
):
    """Sol R5 blocker 2: both ``_record_task_failure`` spawn-failure branches.

    The below-threshold requeue and the breaker trip both end the run, clear
    the claim and move the failure counters. Neither may commit while the
    ended run's lease is still attached and cannot be proven.
    """
    kb = kb_home
    tid, _root = _leased_task(kb, tmp_path, f"r5spawn{failure_limit}")
    holder = {}

    def tamper_then_boom(claimed, _workspace):
        assert claimed.id == tid
        holder["conn"].execute(
            "UPDATE task_leases SET claim_token = ? WHERE task_id = ?",
            ("stolen-spawn-token", tid),
        )
        holder["conn"].commit()
        raise RuntimeError("injected-spawn-failure")

    with kb.connect_closing() as conn:
        holder["conn"] = conn
        result = kb.dispatch_once(
            conn, spawn_fn=tamper_then_boom, failure_limit=failure_limit,
        )
        assert result.spawned == []

        after = _lifecycle_state(kb, conn, tid)
        assert after["status"] == "running", (
            "a spawn failure whose lease cannot be disposed must leave the "
            "claimed run intact rather than committing a requeue or a block"
        )
        assert after["current_run_id"] is not None
        assert after["claim_lock"] is not None
        assert after["consecutive_failures"] == 0
        assert after["last_failure_error"] is None
        assert tid not in result.auto_blocked
        assert len(after["runs"]) == 1
        assert after["runs"][0][4] is None, "the claimed run must still be open"
        assert "spawn_failed" not in after["events"]
        assert "gave_up" not in after["events"]
        assert _lease_count(conn, tid) == 1
        assert kb.get_active_lease(conn, task_id=tid)["claim_token"] == (
            "stolen-spawn-token"
        )


# ---------------------------------------------------------------------------
# R6 falsifier: an active mutation-root lease forces the completion fence even
# when every independently derived identity field AND the lease row's own
# identity values are empty or missing.
# ---------------------------------------------------------------------------


def _blank_every_reservation_identity(conn, task_id, task_epoch):
    """Strip a parked reservation down to unprovable, empty-string identity.

    Leaves the mutation-root lease *active* and run-less while removing every
    independently derived authority for it: the parking run's append-only
    ``claimed`` evidence, the task's own ``route_epoch``, and the lease row's
    ``claim_token`` / ``route_epoch`` (the schema's ``TEXT NOT NULL`` columns
    still accept an empty string).
    """
    _tamper_claimed_event_removed(conn, task_id)
    conn.execute(
        "UPDATE tasks SET route_epoch = ? WHERE id = ?", (task_epoch, task_id),
    )
    conn.commit()
    _set_lease_fields(conn, task_id, claim_token="", route_epoch="")


@pytest.mark.parametrize(
    "task_epoch", [None, ""], ids=["task_epoch_null", "task_epoch_blank"],
)
def test_runless_completion_with_empty_lease_identity_fails_closed(
    kb_home, tmp_path, task_epoch,
):
    """Sol R5 delta blocker: empty identity may not skip the completion fence.

    A parked review reservation still owns the mutation root. When the lease
    row's ``claim_token``/``route_epoch`` are empty strings and the task's own
    epoch and the parking run's independent ``claimed`` evidence are gone,
    there is no provable owner at all — the strictest case, not the weakest.
    Terminal acceptance must therefore refuse, and it must refuse atomically:
    status, result, ``completed_at``, event history, run/audit rows and the
    active reservation all stay exactly as they were. Deciding disposal from
    the truthiness of those identity fields skips the fence entirely and
    publishes ``done`` over a stranded mutation root.
    """
    kb = kb_home
    suffix = "null" if task_epoch is None else "blank"
    tid, _root = _reserved_review_task(kb, tmp_path, f"r6empty-{suffix}")
    with kb.connect_closing() as conn:
        genuine = kb.get_active_lease(conn, task_id=tid)
        assert genuine is not None
        assert genuine["run_id"] is None, "the reservation must be run-less"
        assert genuine["claim_token"] and genuine["route_epoch"]

        _blank_every_reservation_identity(conn, tid, task_epoch)

        before = _lifecycle_state(kb, conn, tid)
        assert before["status"] == "review"
        assert before["claim_lock"] is None
        assert conn.execute(
            "SELECT route_epoch FROM tasks WHERE id = ?", (tid,),
        ).fetchone()["route_epoch"] == task_epoch
        assert kb.get_task(conn, tid).route_epoch is None
        assert before["leases"] == [
            (genuine["lease_key"], None, "", genuine["host_id"], ""),
        ], "the falsifier needs an active lease whose identity is all-empty"

        assert kb.complete_task(conn, tid, result="must-not-land") is False

        after = _lifecycle_state(kb, conn, tid)
        assert after == before, (
            "a completion that could not prove it owns the mutation root must "
            "roll the whole transition back"
        )
        assert after["status"] == "review"
        assert after["result"] is None
        assert after["completed_at"] is None
        assert "completed" not in after["events"]
        assert "done" not in {row[1] for row in after["runs"]}
        assert _lease_count(conn, tid) == 1
        assert kb.get_active_lease(conn, task_id=tid)["claim_token"] == ""


def test_no_lease_completion_still_lands_control(kb_home, tmp_path):
    """Control: a task that never held a mutation lease still completes."""
    kb = kb_home
    with kb.connect_closing() as conn:
        _create_board(kb, conn)
        tid = kb.create_task(conn, title="r6-no-lease", assignee="alpha")
        assert _lease_count(conn, tid) == 0

        assert kb.complete_task(conn, tid, result="landed") is True

        task = kb.get_task(conn, tid)
        assert task.status == "done"
        assert task.result == "landed"
        assert task.completed_at is not None
        assert "completed" in _event_kinds(kb, conn, tid)
        assert _lease_count(conn, tid) == 0


def test_empty_identity_cannot_transfer_runless_lease(kb_home, tmp_path):
    """Empty strings are not transfer authority for a run-less lease.

    An active reservation whose claim_token and route_epoch are blank must
    not CAS-transfer to a successor. Both dispose_matching_lease and
    transfer_task_lease have to reject before mutation.
    """
    kb = kb_home
    tid, _root = _reserved_review_task(kb, tmp_path, "xfer-empty")
    with kb.connect_closing() as conn:
        genuine = kb.get_active_lease(conn, task_id=tid)
        assert genuine is not None
        assert genuine["run_id"] is None
        assert genuine["claim_token"] and genuine["route_epoch"]

        _blank_every_reservation_identity(conn, tid, "")
        before = kb.get_active_lease(conn, task_id=tid)
        assert before["run_id"] is None
        assert before["claim_token"] == ""
        assert before["route_epoch"] == ""
        before_state = _lifecycle_state(kb, conn, tid)

        ok, detail = kb.dispose_matching_lease(
            conn,
            task_id=tid,
            reason="successor",
            claim_token="",
            route_epoch="",
            successor={"run_id": 42, "claim_token": "", "route_epoch": ""},
        )
        assert ok is False
        assert detail
        after_dispose = kb.get_active_lease(conn, task_id=tid)
        assert after_dispose["run_id"] is None
        assert after_dispose["claim_token"] == ""
        assert after_dispose["route_epoch"] == ""
        assert _lifecycle_state(kb, conn, tid) == before_state

        ok_direct, detail_direct = kb.transfer_task_lease(
            conn,
            task_id=tid,
            from_run_id=None,
            to_run_id=42,
            from_token="",
            to_token="",
            route_epoch="",
        )
        assert ok_direct is False
        assert detail_direct
        after_direct = kb.get_active_lease(conn, task_id=tid)
        assert after_direct["run_id"] is None
        assert after_direct["claim_token"] == ""
        assert after_direct["route_epoch"] == ""
        assert _lifecycle_state(kb, conn, tid) == before_state


def test_nonempty_same_host_epoch_transfer_still_works(kb_home, tmp_path):
    """Valid non-empty same-host/same-epoch CAS transfer remains working."""
    kb = kb_home
    tid, _root = _reserved_review_task(kb, tmp_path, "xfer-ok")
    with kb.connect_closing() as conn:
        lease = kb.get_active_lease(conn, task_id=tid)
        assert lease is not None
        assert lease["run_id"] is None
        token = lease["claim_token"]
        epoch = lease["route_epoch"]
        assert token and epoch

        ok, detail = kb.dispose_matching_lease(
            conn,
            task_id=tid,
            reason="successor",
            claim_token=token,
            route_epoch=epoch,
            successor={"run_id": 99, "claim_token": token, "route_epoch": epoch},
        )
        assert ok is True
        assert detail == "transferred"
        after = kb.get_active_lease(conn, task_id=tid)
        assert after is not None
        assert after["run_id"] == 99
        assert after["claim_token"] == token
        assert after["route_epoch"] == epoch
