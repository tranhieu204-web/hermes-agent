import json
import hashlib
import os
import subprocess
import msvcrt
from dataclasses import asdict
from pathlib import Path

import pytest

from scripts.native_runtime_cutover import (
    FORWARD_PHASES,
    PHASES,
    ROLLBACK_PHASES,
    CutoverError,
    CutoverSpec,
    GuardAclAce,
    GuardAclSnapshot,
    NativeRuntimeCutover,
    WindowsGuardAclBackend,
    assert_exact_guard_acl,
    tree_digest,
    main,
)


class TestProtector:
    def protect(self, raw: bytes) -> bytes:
        return bytes(value ^ 0xA5 for value in raw)

    def unprotect(self, protected: bytes) -> bytes:
        return self.protect(protected)


class FakeAclBackend:
    user_sid = "S-1-5-21-1000"

    def __init__(self):
        self.snapshot = None
        self.applied_paths = []

    def current_user_sid(self):
        return self.user_sid

    def apply_exact(self, directory, user_sid):
        assert user_sid == self.user_sid
        self.applied_paths.append(str(directory))
        self.snapshot = _exact_acl_snapshot(self.user_sid, flags=0x03 if Path(directory).is_dir() else 0)

    def read(self, directory):
        assert self.snapshot is not None
        return self.snapshot


class PathAclBackend(FakeAclBackend):
    def __init__(self):
        super().__init__()
        self.snapshots = {}

    def apply_exact(self, directory, user_sid):
        assert user_sid == self.user_sid
        self.applied_paths.append(str(directory))
        snapshot = _exact_acl_snapshot(
            self.user_sid, flags=0x03 if Path(directory).is_dir() else 0,
        )
        self.snapshots[str(Path(directory))] = snapshot
        self.snapshot = snapshot

    def read(self, directory):
        return self.snapshots.get(str(Path(directory)), _exact_acl_snapshot(self.user_sid, flags=0))


def _exact_acl_snapshot(user_sid="S-1-5-21-1000", flags=0x03):
    return GuardAclSnapshot(
        protected=True,
        aces=tuple(
            GuardAclAce(sid=sid, ace_type=0, mask=0x1F01FF, flags=flags)
            for sid in (user_sid, "S-1-5-18", "S-1-5-32-544")
        ),
        owner_sid=user_sid,
    )


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_hash_list(root: Path, target: Path) -> None:
    lines = []
    for path in sorted((item for item in root.rglob("*") if item.is_file())):
        lines.append(f"{_sha(path)}  {path.relative_to(root).as_posix()}")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _fixture(tmp_path: Path):
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "user.email", "fixture@example.invalid")
    (repo / "runtime.txt").write_text("BASE", encoding="utf-8")
    _git(repo, "add", "runtime.txt")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-b", "candidate")
    (repo / "runtime.txt").write_text("RELEASE", encoding="utf-8")
    (repo / "launch-hermes.ps1").write_text("Write-Output release\n", encoding="utf-8")
    (repo / "launch-hermes.vbs").write_text("WScript.Quit 0\n", encoding="utf-8")
    (repo / "scripts").mkdir()
    (repo / "scripts" / "native_runtime_cli_launcher.py").write_text(
        "raise SystemExit(0)\n", encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "release")
    release = _git(repo, "rev-parse", "HEAD")
    launcher_ps1_sha = _sha(repo / "launch-hermes.ps1")
    launcher_vbs_sha = _sha(repo / "launch-hermes.vbs")
    cli_launcher_sha = _sha(repo / "scripts" / "native_runtime_cli_launcher.py")
    _git(repo, "checkout", "main")
    _git(repo, "switch", "--detach", base)

    live_package = tmp_path / "packages" / "live"
    stage_package = tmp_path / "packages" / "stage"
    rollback_package = tmp_path / "packages" / "rollback"
    live_package.mkdir(parents=True)
    stage_package.mkdir(parents=True)
    (live_package / "identity.txt").write_text("BASE", encoding="utf-8")
    (stage_package / "identity.txt").write_text("RELEASE", encoding="utf-8")
    stamp = stage_package / "resources" / "install-stamp.json"
    stamp.parent.mkdir()
    stamp.write_text(json.dumps({"commit": release, "dirty": False}), encoding="utf-8")
    live_cmd = tmp_path / "bindings" / "hermes.cmd"
    candidate_cmd = tmp_path / "bindings" / "candidate.cmd"
    live_cmd.parent.mkdir()
    live_cmd.write_bytes(b"OLD CMD\r\n")
    candidate_cmd.write_bytes(b"NEW CMD\r\n")
    auth_file = tmp_path / "profile" / "auth.json"
    auth_file.parent.mkdir()
    auth_bytes = json.dumps({
        "pool": [
            {"provider": "copilot", "secret": "fixture-one"},
            {"provider": "openai-codex", "secret": "fixture-two"},
            {"provider": "xai-oauth", "secret": "fixture-three"},
        ]
    }, separators=(",", ":")).encode()
    auth_file.write_bytes(auth_bytes)
    hold = tmp_path / "CUTOVER.lock"
    package_hash_list = tmp_path / "PACKAGE-HASHES.sha256"
    _write_hash_list(stage_package, package_hash_list)
    package_files = [item for item in stage_package.rglob("*") if item.is_file()]
    package_bytes = sum(item.stat().st_size for item in package_files)
    package_tree_sha = tree_digest(stage_package)
    live_files = [item for item in live_package.rglob("*") if item.is_file()]
    live_bytes = sum(item.stat().st_size for item in live_files)
    live_tree_sha = tree_digest(live_package)
    manifest = tmp_path / "RELEASE-MANIFEST.json"
    manifest.write_text(json.dumps({
        "base_commit": base, "release_commit": release, "commit_count": 1,
        "stage": str(stage_package), "package_file_count": len(package_files),
        "package_bytes": package_bytes, "package_hash_list_sha256": _sha(package_hash_list).upper(),
        "package_tree_sha256": package_tree_sha.upper(),
        "live_package_file_count": len(live_files), "live_package_bytes": live_bytes,
        "live_package_tree_sha256": live_tree_sha.upper(),
        "candidate_cmd_sha256": _sha(candidate_cmd).upper(),
        "launcher_ps1_blob_sha256": launcher_ps1_sha.upper(),
        "launcher_vbs_blob_sha256": launcher_vbs_sha.upper(),
        "launcher_ps1_sha256": launcher_ps1_sha.upper(),
        "launcher_vbs_sha256": launcher_vbs_sha.upper(),
        "cli_launcher_blob_sha256": cli_launcher_sha.upper(),
        "cli_launcher_sha256": cli_launcher_sha.upper(),
        "branch_plan_sha256": ("1" * 64),
        "branch_plan_audit_sha256": ("2" * 64),
    }, sort_keys=True), encoding="utf-8")
    spec = CutoverSpec(
        transaction_id="fixture-transaction",
        source_repo=str(repo), base_commit=base, release_commit=release,
        live_package=str(live_package), stage_package=str(stage_package),
        rollback_package=str(rollback_package), live_cmd=str(live_cmd),
        candidate_cmd=str(candidate_cmd), auth_file=str(auth_file),
        guard_dir=str(tmp_path / "private-guard"), journal=str(tmp_path / "journal.json"),
        hold=str(hold),
        manifest=str(manifest), manifest_sha256=_sha(manifest),
        package_hash_list=str(package_hash_list), package_hash_list_sha256=_sha(package_hash_list),
        package_tree_sha256=package_tree_sha, package_file_count=len(package_files),
        package_bytes=package_bytes, candidate_cmd_sha256=_sha(candidate_cmd),
        live_package_tree_sha256=live_tree_sha, live_package_file_count=len(live_files),
        live_package_bytes=live_bytes,
        launcher_ps1_blob_sha256=launcher_ps1_sha, launcher_vbs_blob_sha256=launcher_vbs_sha,
        launcher_ps1_sha256=launcher_ps1_sha, launcher_vbs_sha256=launcher_vbs_sha,
        cli_launcher_blob_sha256=cli_launcher_sha, cli_launcher_sha256=cli_launcher_sha,
        branch_plan_sha256="1" * 64, branch_plan_audit_sha256="2" * 64,
    )
    return spec, auth_bytes


def _successor_after_terminal_rollback(tmp_path: Path):
    predecessor, auth_bytes = _fixture(tmp_path)
    predecessor_spec = tmp_path / "PREDECESSOR-SPEC.json"
    predecessor_spec.write_text(
        json.dumps({key: value for key, value in asdict(predecessor).items() if value is not None}, sort_keys=True),
        encoding="utf-8",
    )
    predecessor_receipt = tmp_path / "PREDECESSOR-RECEIPT.md"
    predecessor_receipt.write_text("audited terminal rollback receipt\n", encoding="utf-8")
    transaction = NativeRuntimeCutover(predecessor, protector=TestProtector(), enforce_acl=False)
    assert transaction.run() == "ACTIVATED"
    assert transaction.rollback() == "BASE"
    predecessor_guard_hashes = {
        path.name: _sha(path) for path in Path(predecessor.guard_dir).iterdir() if path.is_file()
    }
    predecessor_journal_hash = _sha(Path(predecessor.journal))
    successor = CutoverSpec(**{
        **asdict(predecessor),
        "transaction_id": "fixture-successor",
        "rollback_package": str(tmp_path / "packages" / "rollback-successor"),
        "guard_dir": str(tmp_path / "private-guard-successor"),
        "journal": str(tmp_path / "journal-successor.json"),
        "predecessor_spec": str(predecessor_spec),
        "predecessor_spec_sha256": _sha(predecessor_spec),
        "predecessor_receipt": str(predecessor_receipt),
        "predecessor_receipt_sha256": _sha(predecessor_receipt),
        "predecessor_journal_sha256": predecessor_journal_hash,
        "handover_receipt": str(tmp_path / "handover-successor.json"),
        "handover_lock": str(tmp_path / "handover-successor.lock"),
    })
    return successor, predecessor, auth_bytes, predecessor_guard_hashes, predecessor_journal_hash


@pytest.mark.parametrize("boundary", FORWARD_PHASES)
def test_restart_after_every_journal_boundary_converges_to_release(tmp_path, boundary):
    spec, auth_bytes = _fixture(tmp_path)
    transaction = NativeRuntimeCutover(spec, protector=TestProtector(), enforce_acl=False)
    with pytest.raises(CutoverError, match="synthetic interruption"):
        transaction.run(fail_after=boundary)

    resumed = NativeRuntimeCutover(spec, protector=TestProtector(), enforce_acl=False)
    assert resumed.run() == "ACTIVATED"
    assert _git(Path(spec.source_repo), "rev-parse", "HEAD") == spec.release_commit
    assert (Path(spec.live_package) / "identity.txt").read_text() == "RELEASE"
    assert Path(spec.live_cmd).read_bytes() == Path(spec.candidate_cmd).read_bytes()
    assert Path(spec.auth_file).read_bytes() == auth_bytes
    assert not Path(spec.hold).exists()


@pytest.mark.parametrize("boundary", FORWARD_PHASES)
def test_rollback_after_every_journal_boundary_converges_to_base(tmp_path, boundary):
    spec, auth_bytes = _fixture(tmp_path)
    transaction = NativeRuntimeCutover(spec, protector=TestProtector(), enforce_acl=False)
    with pytest.raises(CutoverError, match="synthetic interruption"):
        transaction.run(fail_after=boundary)

    resumed = NativeRuntimeCutover(spec, protector=TestProtector(), enforce_acl=False)
    assert resumed.rollback() == "BASE"
    repo = Path(spec.source_repo)
    assert _git(repo, "rev-parse", "HEAD^{tree}") == _git(repo, "rev-parse", f"{spec.base_commit}^{{tree}}")
    assert (Path(spec.live_package) / "identity.txt").read_text() == "BASE"
    assert Path(spec.live_cmd).read_bytes() == b"OLD CMD\r\n"
    assert Path(spec.auth_file).read_bytes() == auth_bytes
    assert Path(spec.hold).exists()
    assert _git(repo, "rev-parse", "HEAD") == spec.base_commit
    assert subprocess.run(
        ["git", "-C", str(repo), "symbolic-ref", "-q", "HEAD"], check=False,
    ).returncode != 0


@pytest.mark.parametrize("boundary", ROLLBACK_PHASES)
def test_restart_after_every_rollback_journal_boundary_converges_to_detached_base(tmp_path, boundary):
    spec, auth_bytes = _fixture(tmp_path)
    transaction = NativeRuntimeCutover(spec, protector=TestProtector(), enforce_acl=False)
    assert transaction.run() == "ACTIVATED"
    with pytest.raises(CutoverError, match="synthetic rollback interruption"):
        transaction.rollback(fail_after=boundary)

    resumed = NativeRuntimeCutover(spec, protector=TestProtector(), enforce_acl=False)
    assert resumed.rollback() == "BASE"
    repo = Path(spec.source_repo)
    assert _git(repo, "rev-parse", "HEAD") == spec.base_commit
    assert _git(repo, "rev-parse", "refs/heads/main") == spec.base_commit
    assert subprocess.run(
        ["git", "-C", str(repo), "symbolic-ref", "-q", "HEAD"], check=False,
    ).returncode != 0
    assert Path(spec.auth_file).read_bytes() == auth_bytes
    assert Path(spec.hold).is_file()


def test_private_guard_detects_auth_change_without_disclosing_bytes(tmp_path):
    spec, _ = _fixture(tmp_path)
    transaction = NativeRuntimeCutover(spec, protector=TestProtector(), enforce_acl=False)
    with pytest.raises(CutoverError, match="synthetic interruption"):
        transaction.run(fail_after="PREPARED_DETACHED")
    Path(spec.auth_file).write_bytes(b"changed")
    with pytest.raises(CutoverError, match="protected auth bytes changed"):
        transaction._verify_auth_equal()


def test_package_tree_digest_is_path_and_byte_sensitive(tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    (package / "a").write_bytes(b"one")
    first = tree_digest(package)
    (package / "a").write_bytes(b"two")
    assert tree_digest(package) != first


@pytest.mark.parametrize("target", ["package", "extra", "remove", "cmd", "manifest", "hash-list"])
def test_readonly_validation_rejects_each_audited_tuple_mutation_before_guard(tmp_path, target):
    spec, _ = _fixture(tmp_path)
    if target == "package":
        (Path(spec.stage_package) / "identity.txt").write_text("TAMPER", encoding="utf-8")
    elif target == "extra":
        (Path(spec.stage_package) / "extra.txt").write_text("TAMPER", encoding="utf-8")
    elif target == "remove":
        (Path(spec.stage_package) / "identity.txt").unlink()
    elif target == "cmd":
        Path(spec.candidate_cmd).write_text("TAMPER", encoding="utf-8")
    elif target == "manifest":
        Path(spec.manifest).write_text("{}", encoding="utf-8")
    else:
        Path(spec.package_hash_list).write_text("bad", encoding="utf-8")
    transaction = NativeRuntimeCutover(spec, protector=TestProtector(), enforce_acl=False)
    with pytest.raises(CutoverError):
        transaction.validate_readonly()
    assert not Path(spec.guard_dir).exists()
    assert not Path(spec.journal).exists()


def test_validation_accepts_exact_detached_canonical_without_creating_hold(tmp_path):
    spec, _ = _fixture(tmp_path)
    transaction = NativeRuntimeCutover(spec, protector=TestProtector(), enforce_acl=False)
    transaction.validate_readonly()
    assert not Path(spec.hold).exists()


def test_phase_contract_is_exact_and_forbids_direct_activation(tmp_path):
    assert PHASES == (
        "PREPARED_DETACHED", "HEAD_ATTACHED_BASE", "SOURCE_TREE_PREPARED", "SOURCE_PROMOTED",
        "OLD_PACKAGE_QUARANTINED", "PACKAGE_PROMOTED", "BINDINGS_VERIFIED", "TUPLE_VERIFIED",
        "ACTIVATION_INTENT", "ACTIVATED", "ROLLBACK_INTENT", "ROLLBACK_PACKAGE_RESTORED",
        "ROLLBACK_BINDINGS_RESTORED", "ROLLBACK_SOURCE_BASE_STAGED", "ROLLBACK_MAIN_BASE",
        "ROLLBACK_HEAD_DETACHED_BASE", "ROLLBACK_PROTECTED_STATE_VERIFIED",
        "ROLLED_BACK_DETACHED_BASE",
    )
    spec, _ = _fixture(tmp_path)
    transaction = NativeRuntimeCutover(spec, protector=TestProtector(), enforce_acl=False)
    with pytest.raises(CutoverError, match="synthetic interruption"):
        transaction.run(fail_after="TUPLE_VERIFIED")
    with pytest.raises(CutoverError, match="invalid journal transition"):
        transaction._write_journal("ACTIVATED")


def test_hold_is_created_and_read_back_before_private_guard(tmp_path, monkeypatch):
    spec, _ = _fixture(tmp_path)
    transaction = NativeRuntimeCutover(spec, protector=TestProtector(), enforce_acl=False)
    original = transaction._create_private_guard

    def assert_held_then_create():
        assert Path(spec.hold).is_file()
        return original()

    monkeypatch.setattr(transaction, "_create_private_guard", assert_held_then_create)
    with pytest.raises(CutoverError, match="synthetic interruption"):
        transaction.run(fail_after="PREPARED_DETACHED")


def test_unknown_preexisting_hold_is_preserved_and_rejected(tmp_path):
    spec, _ = _fixture(tmp_path)
    hold = Path(spec.hold)
    hold.write_bytes(b"foreign hold")
    transaction = NativeRuntimeCutover(spec, protector=TestProtector(), enforce_acl=False)
    with pytest.raises(CutoverError, match="unknown CUTOVER hold"):
        transaction.run()
    assert hold.read_bytes() == b"foreign hold"
    assert transaction._journal()["phase"] is None


def test_terminal_predecessor_hold_handover_is_atomic_and_preserves_evidence(tmp_path, monkeypatch):
    successor, predecessor, _, guard_hashes, journal_hash = _successor_after_terminal_rollback(tmp_path)
    transaction = NativeRuntimeCutover(successor, protector=TestProtector(), enforce_acl=False)
    predecessor_hold = Path(successor.hold).read_bytes()
    original_replace = os.replace
    observed_continuity = False

    def assert_continuous(source, destination):
        nonlocal observed_continuity
        if Path(destination) == Path(successor.hold):
            assert Path(successor.hold).is_file()
            assert Path(successor.hold).read_bytes() == predecessor_hold
            observed_continuity = True
        return original_replace(source, destination)

    monkeypatch.setattr(os, "replace", assert_continuous)
    transaction._handover_predecessor_hold()
    assert observed_continuity
    assert Path(successor.hold).read_bytes() == transaction._hold_bytes(
        successor.transaction_id, successor.base_commit, successor.release_commit,
    )
    assert json.loads(Path(successor.handover_receipt).read_text(encoding="utf-8"))["phase"] == "ADOPTED"
    assert _sha(Path(predecessor.journal)) == journal_hash
    assert {path.name: _sha(path) for path in Path(predecessor.guard_dir).iterdir()} == guard_hashes


@pytest.mark.parametrize("boundary", ["verified", "candidate", "receipt", "replace", "readback"])
def test_terminal_predecessor_handover_restart_converges_at_every_boundary(tmp_path, boundary):
    successor, predecessor, _, guard_hashes, journal_hash = _successor_after_terminal_rollback(tmp_path)
    interrupted = NativeRuntimeCutover(
        successor, protector=TestProtector(), enforce_acl=False, handover_fail_after=boundary,
    )
    with pytest.raises(CutoverError, match="synthetic handover interruption"):
        interrupted._handover_predecessor_hold()
    assert Path(successor.hold).is_file()
    resumed = NativeRuntimeCutover(successor, protector=TestProtector(), enforce_acl=False)
    resumed._handover_predecessor_hold()
    assert Path(successor.hold).read_bytes() == resumed._hold_bytes(
        successor.transaction_id, successor.base_commit, successor.release_commit,
    )
    assert json.loads(Path(successor.handover_receipt).read_text(encoding="utf-8"))["phase"] == "ADOPTED"
    assert _sha(Path(predecessor.journal)) == journal_hash
    assert {path.name: _sha(path) for path in Path(predecessor.guard_dir).iterdir()} == guard_hashes


@pytest.mark.parametrize("target", ["journal", "receipt", "hold", "guard", "base", "live-package"])
def test_terminal_predecessor_handover_mismatch_fails_held(tmp_path, target):
    successor, predecessor, _, _, _ = _successor_after_terminal_rollback(tmp_path)
    repo = Path(successor.source_repo)
    if target == "journal":
        Path(predecessor.journal).write_text("{}", encoding="utf-8")
    elif target == "receipt":
        Path(successor.predecessor_receipt).write_text("altered", encoding="utf-8")
    elif target == "hold":
        Path(successor.hold).write_text("foreign", encoding="utf-8")
    elif target == "guard":
        (Path(predecessor.guard_dir) / "guard.json").write_text("{}", encoding="utf-8")
    elif target == "base":
        _git(repo, "switch", "main")
    else:
        (Path(successor.live_package) / "identity.txt").write_text("altered", encoding="utf-8")

    transaction = NativeRuntimeCutover(successor, protector=TestProtector(), enforce_acl=False)
    with pytest.raises((CutoverError, KeyError, TypeError, json.JSONDecodeError)):
        transaction._handover_predecessor_hold()
    assert Path(successor.hold).exists()
    assert not Path(successor.journal).exists()
    assert not Path(successor.guard_dir).exists()


def test_terminal_predecessor_handover_rejects_concurrent_owner(tmp_path):
    successor, _, _, _, _ = _successor_after_terminal_rollback(tmp_path)
    lock_path = Path(successor.handover_lock)
    lock_path.write_bytes(b"0")
    with lock_path.open("r+b") as held:
        msvcrt.locking(held.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            transaction = NativeRuntimeCutover(successor, protector=TestProtector(), enforce_acl=False)
            with pytest.raises(CutoverError, match="already in progress"):
                transaction._handover_predecessor_hold()
        finally:
            held.seek(0)
            msvcrt.locking(held.fileno(), msvcrt.LK_UNLCK, 1)
    assert Path(successor.hold).is_file()


@pytest.mark.parametrize("artifact", ["lock", "candidate"])
def test_handover_hardlink_alias_is_rejected_without_victim_mutation(tmp_path, artifact):
    successor, _, _, _, _ = _successor_after_terminal_rollback(tmp_path)
    transaction = NativeRuntimeCutover(successor, protector=TestProtector(), enforce_acl=False)
    predecessor_hold = Path(successor.hold).read_bytes()
    candidate = Path(successor.hold).with_name(
        f".{Path(successor.hold).name}.{successor.transaction_id}.next"
    )
    target = Path(successor.handover_lock) if artifact == "lock" else candidate
    victim = tmp_path / f"{artifact}-victim"
    victim_bytes = b"" if artifact == "lock" else transaction._hold_bytes(
        successor.transaction_id, successor.base_commit, successor.release_commit,
    )
    victim.write_bytes(victim_bytes)
    os.link(victim, target)

    with pytest.raises(CutoverError, match="single-link"):
        transaction._handover_predecessor_hold()

    assert victim.read_bytes() == victim_bytes
    assert Path(successor.hold).read_bytes() == predecessor_hold
    assert not Path(successor.journal).exists()
    assert not Path(successor.guard_dir).exists()


@pytest.mark.parametrize("artifact", ["lock", "candidate"])
def test_handover_reparse_artifact_is_rejected_held(tmp_path, artifact, monkeypatch):
    import scripts.native_runtime_cutover as module

    successor, _, _, _, _ = _successor_after_terminal_rollback(tmp_path)
    transaction = NativeRuntimeCutover(successor, protector=TestProtector(), enforce_acl=False)
    predecessor_hold = Path(successor.hold).read_bytes()
    target = Path(successor.handover_lock)
    target.write_bytes(b"0")
    if artifact == "candidate":
        target = Path(successor.hold).with_name(
            f".{Path(successor.hold).name}.{successor.transaction_id}.next"
        )
        target.write_bytes(transaction._hold_bytes(
            successor.transaction_id, successor.base_commit, successor.release_commit,
        ))
    original = module._is_reparse_path
    monkeypatch.setattr(module, "_is_reparse_path", lambda path: Path(path) == target or original(path))

    with pytest.raises(CutoverError, match="non-reparse"):
        transaction._handover_predecessor_hold()

    assert Path(successor.hold).read_bytes() == predecessor_hold
    assert not Path(successor.journal).exists()
    assert not Path(successor.guard_dir).exists()


@pytest.mark.parametrize("artifact,drift", [
    ("lock", "acl"), ("lock", "owner"), ("candidate", "acl"), ("candidate", "owner"),
])
def test_handover_artifact_acl_or_owner_drift_is_rejected_held(tmp_path, artifact, drift):
    successor, _, _, _, _ = _successor_after_terminal_rollback(tmp_path)
    backend = PathAclBackend()
    transaction = NativeRuntimeCutover(
        successor, protector=TestProtector(), acl_backend=backend,
    )
    predecessor_hold = Path(successor.hold).read_bytes()
    lock = Path(successor.handover_lock)
    lock.write_bytes(b"0")
    candidate = Path(successor.hold).with_name(
        f".{Path(successor.hold).name}.{successor.transaction_id}.next"
    )
    candidate.write_bytes(transaction._hold_bytes(
        successor.transaction_id, successor.base_commit, successor.release_commit,
    ))
    target = lock if artifact == "lock" else candidate
    exact = _exact_acl_snapshot(backend.user_sid, flags=0)
    if drift == "acl":
        backend.snapshots[str(target)] = GuardAclSnapshot(False, exact.aces, backend.user_sid)
    else:
        backend.snapshots[str(target)] = GuardAclSnapshot(True, exact.aces, "S-1-5-21-WRONG")

    with pytest.raises(CutoverError, match="ACL|owner SID"):
        transaction._handover_predecessor_hold()

    assert Path(successor.hold).read_bytes() == predecessor_hold
    assert not Path(successor.journal).exists()
    assert not Path(successor.guard_dir).exists()


def test_handover_lock_path_swap_is_rejected_before_write(tmp_path, monkeypatch):
    successor, _, _, _, _ = _successor_after_terminal_rollback(tmp_path)
    transaction = NativeRuntimeCutover(successor, protector=TestProtector(), enforce_acl=False)
    predecessor_hold = Path(successor.hold).read_bytes()
    lock = Path(successor.handover_lock)
    lock.write_bytes(b"0")
    victim = tmp_path / "lock-swap-victim"
    victim.write_bytes(b"victim")
    original_open = os.open
    swapped = False

    def swap_before_open(path, flags, *args):
        nonlocal swapped
        if not swapped and Path(path) == lock:
            swapped = True
            lock.unlink()
            os.link(victim, lock)
        return original_open(path, flags, *args)

    monkeypatch.setattr(os, "open", swap_before_open)
    with pytest.raises(CutoverError, match="single-link|identity changed"):
        transaction._handover_predecessor_hold()

    assert victim.read_bytes() == b"victim"
    assert Path(successor.hold).read_bytes() == predecessor_hold


def test_handover_candidate_replace_swap_restores_predecessor_hold(tmp_path, monkeypatch):
    successor, _, _, _, _ = _successor_after_terminal_rollback(tmp_path)
    transaction = NativeRuntimeCutover(successor, protector=TestProtector(), enforce_acl=False)
    predecessor_hold = Path(successor.hold).read_bytes()
    candidate = Path(successor.hold).with_name(
        f".{Path(successor.hold).name}.{successor.transaction_id}.next"
    )
    victim = tmp_path / "candidate-swap-victim"
    victim.write_bytes(b"victim")
    original_replace = os.replace
    swapped = False

    def swap_at_promotion(source, destination):
        nonlocal swapped
        if not swapped and Path(source) == candidate and Path(destination) == Path(successor.hold):
            swapped = True
            candidate.unlink()
            os.link(victim, candidate)
        return original_replace(source, destination)

    monkeypatch.setattr(os, "replace", swap_at_promotion)
    with pytest.raises(CutoverError, match="single-link|verified candidate"):
        transaction._handover_predecessor_hold()

    assert victim.read_bytes() == b"victim"
    assert Path(successor.hold).read_bytes() == predecessor_hold
    assert not Path(successor.journal).exists()
    assert not Path(successor.guard_dir).exists()


def test_successor_rollback_converges_and_preserves_predecessor_evidence(tmp_path):
    successor, predecessor, auth_bytes, guard_hashes, journal_hash = _successor_after_terminal_rollback(tmp_path)
    transaction = NativeRuntimeCutover(successor, protector=TestProtector(), enforce_acl=False)
    assert transaction.run() == "ACTIVATED"
    assert transaction.rollback() == "BASE"
    assert transaction._source_state() == "F0"
    assert Path(successor.auth_file).read_bytes() == auth_bytes
    assert Path(successor.hold).is_file()
    assert _sha(Path(predecessor.journal)) == journal_hash
    assert {path.name: _sha(path) for path in Path(predecessor.guard_dir).iterdir()} == guard_hashes


@pytest.mark.parametrize("kind", ["symbolic-head", "main-moved", "tracked", "index", "untracked", "lock"])
def test_late_source_dirtiness_never_invokes_read_tree(tmp_path, kind, monkeypatch):
    spec, _ = _fixture(tmp_path)
    repo = Path(spec.source_repo)
    transaction = NativeRuntimeCutover(spec, protector=TestProtector(), enforce_acl=False)
    transaction._prepare()
    transaction._head_attached_base()
    if kind == "symbolic-head":
        _git(repo, "branch", "alternate", spec.base_commit)
        _git(repo, "symbolic-ref", "HEAD", "refs/heads/alternate")
    elif kind == "main-moved":
        _git(repo, "update-ref", "refs/heads/main", spec.release_commit, spec.base_commit)
    elif kind == "tracked":
        (repo / "runtime.txt").write_text("changed", encoding="utf-8")
    elif kind == "index":
        (repo / "runtime.txt").write_text("index-only", encoding="utf-8")
        _git(repo, "add", "runtime.txt")
        _git(repo, "checkout", "--", "runtime.txt")
    elif kind == "untracked":
        (repo / "untracked.txt").write_text("unexpected", encoding="utf-8")
    else:
        Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-path", "index.lock")).write_text(
            "lock", encoding="utf-8",
        )

    invoked = False
    original = subprocess.run

    def sentinel(argv, *args, **kwargs):
        nonlocal invoked
        if "read-tree" in argv:
            invoked = True
        return original(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", sentinel)
    with pytest.raises(CutoverError, match="LATE_SOURCE_DIRTINESS_HOLD"):
        transaction._source_tree_prepared()
    assert not invoked
    assert transaction._journal()["phase"] == "HEAD_ATTACHED_BASE"


def test_raw_head_second_read_race_blocks_attach(tmp_path, monkeypatch):
    spec, _ = _fixture(tmp_path)
    repo = Path(spec.source_repo)
    transaction = NativeRuntimeCutover(spec, protector=TestProtector(), enforce_acl=False)
    transaction._prepare()
    original = transaction._direct_head_preimage
    reads = 0

    def race():
        nonlocal reads
        reads += 1
        if reads == 2:
            transaction._head_file().write_text("ref: refs/heads/main\n", encoding="ascii")
        return original()

    monkeypatch.setattr(transaction, "_direct_head_preimage", race)
    with pytest.raises(CutoverError, match="raw HEAD"):
        transaction._head_attached_base()
    assert _git(repo, "rev-parse", "refs/heads/main") == spec.base_commit
    assert transaction._journal()["phase"] == "PREPARED_DETACHED"


@pytest.mark.parametrize("phase", ["HEAD_ATTACHED_BASE", "SOURCE_TREE_PREPARED", "SOURCE_PROMOTED"])
def test_forward_action_completed_before_journal_restart_advances_only_after_readback(tmp_path, phase, monkeypatch):
    spec, _ = _fixture(tmp_path)
    transaction = NativeRuntimeCutover(spec, protector=TestProtector(), enforce_acl=False)
    original = transaction._write_journal
    failed = False

    def fail_once(next_phase, **extra):
        nonlocal failed
        if next_phase == phase and not failed:
            failed = True
            raise OSError("synthetic journal lag")
        return original(next_phase, **extra)

    monkeypatch.setattr(transaction, "_write_journal", fail_once)
    with pytest.raises(OSError, match="synthetic journal lag"):
        transaction.run()
    resumed = NativeRuntimeCutover(spec, protector=TestProtector(), enforce_acl=False)
    assert resumed.run() == "ACTIVATED"


@pytest.mark.parametrize("kind", ["worktree", "index", "untracked"])
def test_dirtiness_race_after_source_tree_prepared_never_invokes_main_cas(tmp_path, monkeypatch, kind):
    from scripts import native_runtime_cutover as module

    spec, _ = _fixture(tmp_path)
    repo = Path(spec.source_repo)
    transaction = NativeRuntimeCutover(spec, protector=TestProtector(), enforce_acl=False)
    with pytest.raises(CutoverError, match="synthetic interruption"):
        transaction.run(fail_after="SOURCE_TREE_PREPARED")
    if kind == "worktree":
        (repo / "runtime.txt").write_text("late worktree", encoding="utf-8")
    elif kind == "index":
        (repo / "runtime.txt").write_text("late index", encoding="utf-8")
        _git(repo, "add", "runtime.txt")
    else:
        (repo / "late-untracked.txt").write_text("preserve me", encoding="utf-8")

    invoked = False

    def forbidden_cas(*args, **kwargs):
        nonlocal invoked
        invoked = True
        raise AssertionError("main CAS must not run after late dirtiness")

    monkeypatch.setattr(module, "_git_stdin", forbidden_cas)
    with pytest.raises(CutoverError, match="LATE_SOURCE_DIRTINESS_HOLD"):
        transaction.run()
    assert not invoked
    assert _git(repo, "rev-parse", "refs/heads/main") == spec.base_commit
    if kind == "untracked":
        assert (repo / "late-untracked.txt").read_text(encoding="utf-8") == "preserve me"
    assert Path(spec.hold).is_file()


@pytest.mark.parametrize("kind", ["worktree", "index", "untracked"])
def test_dirtiness_race_after_rollback_tree_staged_never_invokes_main_cas(tmp_path, monkeypatch, kind):
    from scripts import native_runtime_cutover as module

    spec, _ = _fixture(tmp_path)
    repo = Path(spec.source_repo)
    transaction = NativeRuntimeCutover(spec, protector=TestProtector(), enforce_acl=False)
    assert transaction.run() == "ACTIVATED"
    with pytest.raises(CutoverError, match="synthetic rollback interruption"):
        transaction.rollback(fail_after="ROLLBACK_SOURCE_BASE_STAGED")
    if kind == "worktree":
        (repo / "runtime.txt").write_text("late worktree", encoding="utf-8")
    elif kind == "index":
        (repo / "runtime.txt").write_text("late index", encoding="utf-8")
        _git(repo, "add", "runtime.txt")
    else:
        (repo / "late-untracked.txt").write_text("preserve me", encoding="utf-8")

    invoked = False

    def forbidden_cas(*args, **kwargs):
        nonlocal invoked
        invoked = True
        raise AssertionError("rollback main CAS must not run after late dirtiness")

    monkeypatch.setattr(module, "_git_stdin", forbidden_cas)
    with pytest.raises(CutoverError, match="LATE_SOURCE_DIRTINESS_HOLD"):
        transaction.rollback()
    assert not invoked
    assert _git(repo, "rev-parse", "refs/heads/main") == spec.release_commit
    if kind == "untracked":
        assert (repo / "late-untracked.txt").read_text(encoding="utf-8") == "preserve me"
    assert Path(spec.hold).is_file()


@pytest.mark.parametrize("kind", ["worktree", "index", "untracked"])
def test_dirtiness_after_main_cas_stays_held_then_clean_rollback_reaches_base(tmp_path, kind):
    spec, _ = _fixture(tmp_path)
    repo = Path(spec.source_repo)
    transaction = NativeRuntimeCutover(spec, protector=TestProtector(), enforce_acl=False)
    with pytest.raises(CutoverError, match="synthetic interruption"):
        transaction.run(fail_after="SOURCE_PROMOTED")
    if kind == "worktree":
        (repo / "runtime.txt").write_text("late worktree", encoding="utf-8")
    elif kind == "index":
        (repo / "runtime.txt").write_text("late index", encoding="utf-8")
        _git(repo, "add", "runtime.txt")
    else:
        (repo / "late-untracked.txt").write_text("preserve me", encoding="utf-8")

    with pytest.raises(CutoverError, match="rollback source origin is ambiguous"):
        transaction.rollback()
    assert Path(spec.hold).is_file()
    assert _git(repo, "rev-parse", "refs/heads/main") == spec.release_commit

    if kind == "untracked":
        (repo / "late-untracked.txt").unlink()
    else:
        _git(repo, "read-tree", "--reset", "-u", spec.release_commit)
    assert transaction.rollback() == "BASE"
    assert transaction._source_state() == "F0"


@pytest.mark.parametrize(
    "phase", ["ROLLBACK_SOURCE_BASE_STAGED", "ROLLBACK_MAIN_BASE", "ROLLBACK_HEAD_DETACHED_BASE"],
)
def test_rollback_action_completed_before_journal_restart_converges(tmp_path, phase, monkeypatch):
    spec, _ = _fixture(tmp_path)
    transaction = NativeRuntimeCutover(spec, protector=TestProtector(), enforce_acl=False)
    assert transaction.run() == "ACTIVATED"
    original = transaction._write_journal
    failed = False

    def fail_once(next_phase, **extra):
        nonlocal failed
        if next_phase == phase and not failed:
            failed = True
            raise OSError("synthetic rollback journal lag")
        return original(next_phase, **extra)

    monkeypatch.setattr(transaction, "_write_journal", fail_once)
    with pytest.raises(OSError, match="synthetic rollback journal lag"):
        transaction.rollback()
    resumed = NativeRuntimeCutover(spec, protector=TestProtector(), enforce_acl=False)
    assert resumed.rollback() == "BASE"
    assert resumed._source_state() == "F0"


def test_rollback_detach_completed_before_journal_uses_verified_receipt_on_restart(tmp_path, monkeypatch):
    from scripts import native_runtime_cutover as module

    spec, _ = _fixture(tmp_path)
    transaction = NativeRuntimeCutover(spec, protector=TestProtector(), enforce_acl=False)
    assert transaction.run() == "ACTIVATED"
    original = transaction._write_journal
    failed = False

    def fail_once(next_phase, **extra):
        nonlocal failed
        if next_phase == "ROLLBACK_HEAD_DETACHED_BASE" and not failed:
            failed = True
            raise OSError("synthetic detach journal lag")
        return original(next_phase, **extra)

    monkeypatch.setattr(transaction, "_write_journal", fail_once)
    with pytest.raises(OSError, match="synthetic detach journal lag"):
        transaction.rollback()
    assert transaction._journal()["phase"] == "ROLLBACK_MAIN_BASE"
    assert transaction._source_state() == "F0"

    def forbidden_second_detach(*args, **kwargs):
        raise AssertionError("verified restart must not execute a second detach")

    monkeypatch.setattr(module, "_git_with_env", forbidden_second_detach)
    resumed = NativeRuntimeCutover(spec, protector=TestProtector(), enforce_acl=False)
    assert resumed.rollback() == "BASE"


def test_rollback_detach_missing_reflog_append_fails_held_on_restart(tmp_path, monkeypatch):
    spec, _ = _fixture(tmp_path)
    repo = Path(spec.source_repo)
    transaction = NativeRuntimeCutover(spec, protector=TestProtector(), enforce_acl=False)
    assert transaction.run() == "ACTIVATED"
    original = transaction._write_journal
    failed = False

    def fail_once(next_phase, **extra):
        nonlocal failed
        if next_phase == "ROLLBACK_HEAD_DETACHED_BASE" and not failed:
            failed = True
            raise OSError("synthetic detach journal lag")
        return original(next_phase, **extra)

    monkeypatch.setattr(transaction, "_write_journal", fail_once)
    with pytest.raises(OSError, match="synthetic detach journal lag"):
        transaction.rollback()

    reflog = repo / ".git" / "logs" / "HEAD"
    lines = reflog.read_bytes().splitlines(keepends=True)
    assert len(lines) > 1
    reflog.write_bytes(b"".join(lines[:-1]))
    resumed = NativeRuntimeCutover(spec, protector=TestProtector(), enforce_acl=False)
    with pytest.raises(CutoverError, match="reflog append receipt is invalid"):
        resumed.rollback()
    assert resumed._journal()["phase"] == "ROLLBACK_MAIN_BASE"
    assert Path(spec.hold).is_file()


def test_validation_rejects_nonexistent_release_and_two_commit_topology(tmp_path):
    spec, _ = _fixture(tmp_path)
    bad = CutoverSpec(**{**asdict(spec), "release_commit": "f" * 40})
    with pytest.raises(CutoverError, match="does not exist"):
        NativeRuntimeCutover(bad, protector=TestProtector(), enforce_acl=False).validate_readonly()

    repo = Path(spec.source_repo)
    _git(repo, "checkout", "candidate")
    (repo / "second.txt").write_text("second", encoding="utf-8")
    _git(repo, "add", "second.txt")
    _git(repo, "commit", "-m", "second release commit")
    second = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "main")
    _git(repo, "switch", "--detach", spec.base_commit)
    two_commit = CutoverSpec(**{**asdict(spec), "release_commit": second})
    with pytest.raises(CutoverError, match="exactly one commit"):
        NativeRuntimeCutover(two_commit, protector=TestProtector(), enforce_acl=False).validate_readonly()


def test_validation_cli_returns_nonzero_for_exact_identifier_mismatch(tmp_path, monkeypatch, capsys):
    spec, _ = _fixture(tmp_path)
    spec_path = tmp_path / "spec.json"
    bad = {**asdict(spec), "release_commit": "e" * 40}
    spec_path.write_text(json.dumps(bad), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["native_runtime_cutover.py", "--spec", str(spec_path)])
    assert main() == 2
    assert json.loads(capsys.readouterr().err)["status"] == "INVALID"
    assert not Path(spec.guard_dir).exists()
    assert not Path(spec.journal).exists()


def test_launcher_tamper_is_rejected_before_binding_write(tmp_path):
    spec, _ = _fixture(tmp_path)
    transaction = NativeRuntimeCutover(spec, protector=TestProtector(), enforce_acl=False)
    with pytest.raises(CutoverError, match="synthetic interruption"):
        transaction.run(fail_after="PACKAGE_PROMOTED")
    old_cmd = Path(spec.live_cmd).read_bytes()
    (Path(spec.source_repo) / "launch-hermes.ps1").write_text("tamper", encoding="utf-8")
    with pytest.raises(CutoverError, match="launcher digest mismatch"):
        transaction.run()
    assert Path(spec.live_cmd).read_bytes() == old_cmd


@pytest.mark.parametrize("kind", ["missing", "file", "reparse"])
def test_live_package_preflight_rejects_wrong_type_before_source_mutation(tmp_path, kind, monkeypatch):
    spec, _ = _fixture(tmp_path)
    live = Path(spec.live_package)
    if kind == "missing":
        (live / "identity.txt").unlink()
        live.rmdir()
    elif kind == "file":
        (live / "identity.txt").unlink()
        live.rmdir()
        live.write_text("not a directory", encoding="utf-8")
    else:
        from scripts import native_runtime_cutover as module
        original = module._is_reparse_path
        monkeypatch.setattr(module, "_is_reparse_path", lambda path: path == live or original(path))

    repo = Path(spec.source_repo)
    transaction = NativeRuntimeCutover(spec, protector=TestProtector(), enforce_acl=False)
    with pytest.raises(CutoverError, match="ordinary non-reparse directory"):
        transaction.validate_readonly()
    assert _git(repo, "rev-parse", "HEAD") == spec.base_commit
    assert not Path(spec.guard_dir).exists()
    assert not Path(spec.journal).exists()


def test_live_package_identity_is_rechecked_immediately_before_source_promotion(tmp_path):
    spec, _ = _fixture(tmp_path)
    transaction = NativeRuntimeCutover(spec, protector=TestProtector(), enforce_acl=False)
    transaction._prepare()
    transaction._head_attached_base()
    (Path(spec.live_package) / "identity.txt").write_text("changed", encoding="utf-8")
    with pytest.raises(CutoverError, match="live package .* identity mismatch"):
        transaction._source_tree_prepared()
    assert _git(Path(spec.source_repo), "rev-parse", "HEAD") == spec.base_commit
    assert transaction._journal()["phase"] == "HEAD_ATTACHED_BASE"


def test_activation_intent_is_durable_before_hold_removal_and_restart_converges(tmp_path):
    spec, _ = _fixture(tmp_path)
    transaction = NativeRuntimeCutover(spec, protector=TestProtector(), enforce_acl=False)
    with pytest.raises(CutoverError, match="synthetic interruption"):
        transaction.run(fail_after="ACTIVATION_INTENT")
    assert transaction._journal()["phase"] == "ACTIVATION_INTENT"
    assert Path(spec.hold).exists()

    # Model process loss in the only side-effect window: unlink completed,
    # final journal persistence did not.  Durable intent still records it.
    Path(spec.hold).unlink()
    resumed = NativeRuntimeCutover(spec, protector=TestProtector(), enforce_acl=False)
    assert resumed._journal()["phase"] == "ACTIVATION_INTENT"
    assert resumed.run() == "ACTIVATED"
    assert not Path(spec.hold).exists()


def test_activation_restart_fails_closed_when_tuple_changed_after_intent(tmp_path):
    spec, _ = _fixture(tmp_path)
    transaction = NativeRuntimeCutover(spec, protector=TestProtector(), enforce_acl=False)
    with pytest.raises(CutoverError, match="synthetic interruption"):
        transaction.run(fail_after="ACTIVATION_INTENT")
    Path(spec.candidate_cmd).write_bytes(b"tampered")
    with pytest.raises(CutoverError):
        transaction.run()
    assert Path(spec.hold).exists()
    assert transaction._journal()["phase"] == "ACTIVATION_INTENT"


@pytest.mark.parametrize(
    "target", ["stage-list", "manifest", "cmd", "launcher", "package", "auth", "source"],
)
def test_post_unlink_tuple_failure_atomically_restores_hold(tmp_path, target):
    spec, _ = _fixture(tmp_path)
    transaction = NativeRuntimeCutover(spec, protector=TestProtector(), enforce_acl=False)
    with pytest.raises(CutoverError, match="synthetic interruption"):
        transaction.run(fail_after="ACTIVATION_INTENT")
    Path(spec.hold).unlink()

    if target == "stage-list":
        Path(spec.package_hash_list).write_text("bad", encoding="utf-8")
    elif target == "manifest":
        Path(spec.manifest).write_text("{}", encoding="utf-8")
    elif target == "cmd":
        Path(spec.candidate_cmd).write_bytes(b"bad")
    elif target == "launcher":
        (Path(spec.source_repo) / "launch-hermes.ps1").write_text("bad", encoding="utf-8")
    elif target == "package":
        (Path(spec.live_package) / "identity.txt").write_text("bad", encoding="utf-8")
    elif target == "auth":
        Path(spec.auth_file).write_bytes(b"bad")
    else:
        (Path(spec.source_repo) / "runtime.txt").write_text("bad", encoding="utf-8")

    with pytest.raises((CutoverError, OSError)):
        transaction.run()
    assert Path(spec.hold).is_file()
    assert transaction._journal()["phase"] == "ACTIVATION_INTENT"


def test_activation_journal_failure_after_unlink_restores_hold_and_restart_converges(tmp_path, monkeypatch):
    spec, _ = _fixture(tmp_path)
    transaction = NativeRuntimeCutover(spec, protector=TestProtector(), enforce_acl=False)
    original = transaction._write_journal

    def fail_activated(phase, **extra):
        if phase == "ACTIVATED":
            raise OSError("synthetic activated journal failure")
        return original(phase, **extra)

    monkeypatch.setattr(transaction, "_write_journal", fail_activated)
    with pytest.raises(OSError, match="synthetic activated journal failure"):
        transaction.run()
    assert transaction._journal()["phase"] == "ACTIVATION_INTENT"
    assert Path(spec.hold).is_file()

    resumed = NativeRuntimeCutover(spec, protector=TestProtector(), enforce_acl=False)
    assert resumed.run() == "ACTIVATED"
    assert not Path(spec.hold).exists()


GUARD_BOUNDARIES = (
    "directory", "acl", "ownership", "receipt", "auth", "cmd", "key", "metadata", "publication",
)


@pytest.mark.parametrize("boundary", GUARD_BOUNDARIES)
def test_private_guard_interruption_before_prepared_restart_converges(tmp_path, boundary):
    spec, auth_bytes = _fixture(tmp_path)
    interrupted = NativeRuntimeCutover(
        spec, protector=TestProtector(), enforce_acl=False, guard_fail_after=boundary,
    )
    with pytest.raises(CutoverError, match=f"synthetic guard interruption after {boundary}"):
        interrupted.run()
    assert interrupted._journal()["phase"] is None
    assert Path(spec.hold).is_file()
    assert Path(spec.auth_file).read_bytes() == auth_bytes
    assert Path(spec.live_cmd).read_bytes() == b"OLD CMD\r\n"

    resumed = NativeRuntimeCutover(spec, protector=TestProtector(), enforce_acl=False)
    assert resumed.run() == "ACTIVATED"
    assert resumed._journal()["phase"] == "ACTIVATED"
    assert Path(spec.auth_file).read_bytes() == auth_bytes
    assert not resumed.guard_staging_dir.exists()
    assert not resumed.guard_staging_receipt.exists()
    assert Path(spec.guard_dir).is_dir()


@pytest.mark.parametrize("boundary", GUARD_BOUNDARIES)
def test_private_guard_interruption_before_prepared_rollback_cleans_owned_artifacts(tmp_path, boundary):
    spec, auth_bytes = _fixture(tmp_path)
    interrupted = NativeRuntimeCutover(
        spec, protector=TestProtector(), enforce_acl=False, guard_fail_after=boundary,
    )
    with pytest.raises(CutoverError, match=f"synthetic guard interruption after {boundary}"):
        interrupted.run()

    rollback = NativeRuntimeCutover(spec, protector=TestProtector(), enforce_acl=False)
    assert rollback.rollback() == "BASE"
    assert not Path(spec.guard_dir).exists()
    assert not rollback.guard_staging_dir.exists()
    assert not rollback.guard_staging_receipt.exists()
    assert Path(spec.hold).is_file()
    assert Path(spec.auth_file).read_bytes() == auth_bytes
    assert Path(spec.live_cmd).read_bytes() == b"OLD CMD\r\n"
    assert _git(Path(spec.source_repo), "rev-parse", "HEAD") == spec.base_commit


def test_valid_published_guard_without_prepared_is_adopted_exactly(tmp_path):
    spec, auth_bytes = _fixture(tmp_path)
    interrupted = NativeRuntimeCutover(
        spec, protector=TestProtector(), enforce_acl=False, guard_fail_after="publication",
    )
    with pytest.raises(CutoverError, match="synthetic guard interruption after publication"):
        interrupted.run()
    before = {path.name: path.read_bytes() for path in Path(spec.guard_dir).iterdir()}

    resumed = NativeRuntimeCutover(spec, protector=TestProtector(), enforce_acl=False)
    assert resumed.run() == "ACTIVATED"
    after = {path.name: path.read_bytes() for path in Path(spec.guard_dir).iterdir()}
    assert after == before
    assert Path(spec.auth_file).read_bytes() == auth_bytes


def test_invalid_published_guard_remains_held_and_is_not_deleted(tmp_path):
    spec, _ = _fixture(tmp_path)
    guard = Path(spec.guard_dir)
    guard.mkdir(parents=True)
    marker = guard / "ambiguous"
    marker.write_bytes(b"keep")
    transaction = NativeRuntimeCutover(spec, protector=TestProtector(), enforce_acl=False)
    with pytest.raises(CutoverError, match="private guard is incomplete"):
        transaction.run()
    assert marker.read_bytes() == b"keep"
    assert Path(spec.hold).is_file()
    assert transaction._journal()["phase"] is None


@pytest.mark.parametrize("location", ["root", "auth", "owner"])
def test_published_guard_root_or_child_reparse_is_rejected_held(tmp_path, location, monkeypatch):
    spec, _ = _fixture(tmp_path)
    creator = NativeRuntimeCutover(
        spec, protector=TestProtector(), enforce_acl=False, guard_fail_after="publication",
    )
    with pytest.raises(CutoverError, match="synthetic guard interruption after publication"):
        creator.run()
    guard = Path(spec.guard_dir)
    before = {path.name: path.read_bytes() for path in guard.iterdir()}
    target = guard if location == "root" else guard / ("auth.dpapi" if location == "auth" else "ownership.json")
    from scripts import native_runtime_cutover as module
    original = module._is_reparse_path
    monkeypatch.setattr(module, "_is_reparse_path", lambda path: path == target or original(path))

    resumed = NativeRuntimeCutover(spec, protector=TestProtector(), enforce_acl=False)
    with pytest.raises(CutoverError, match="non-reparse"):
        resumed.run()
    assert Path(spec.hold).is_file()
    assert resumed._journal()["phase"] is None
    assert {path.name: path.read_bytes() for path in guard.iterdir()} == before


@pytest.mark.parametrize("extra_kind", ["file", "directory"])
def test_published_guard_unexpected_content_is_preserved_and_rejected_held(tmp_path, extra_kind):
    spec, _ = _fixture(tmp_path)
    creator = NativeRuntimeCutover(
        spec, protector=TestProtector(), enforce_acl=False, guard_fail_after="publication",
    )
    with pytest.raises(CutoverError, match="synthetic guard interruption after publication"):
        creator.run()
    guard = Path(spec.guard_dir)
    extra = guard / "unexpected"
    extra.write_bytes(b"preserve") if extra_kind == "file" else extra.mkdir()

    resumed = NativeRuntimeCutover(spec, protector=TestProtector(), enforce_acl=False)
    with pytest.raises(CutoverError, match="unexpected shape"):
        resumed.run()
    assert Path(spec.hold).is_file()
    assert extra.exists()
    assert resumed._journal()["phase"] is None


@pytest.mark.parametrize("shape", ["empty", "unknown", "foreign-owner", "child-directory"])
def test_unknown_deterministic_staging_collision_is_preserved_and_fails_held(tmp_path, shape):
    spec, _ = _fixture(tmp_path)
    transaction = NativeRuntimeCutover(spec, protector=TestProtector(), enforce_acl=False)
    legacy = transaction.guard_dir.with_name(
        f".{transaction.guard_dir.name}.{transaction.spec.transaction_id}.preparing"
    )
    legacy.mkdir(parents=True)
    if shape == "unknown":
        (legacy / "unowned-marker.txt").write_bytes(b"preserve")
    elif shape == "foreign-owner":
        (legacy / "ownership.json").write_text('{"transaction_id":"foreign"}', encoding="utf-8")
    elif shape == "child-directory":
        (legacy / "nested").mkdir()
    before = {
        path.relative_to(legacy).as_posix(): (None if path.is_dir() else path.read_bytes())
        for path in legacy.rglob("*")
    }

    with pytest.raises(CutoverError, match="unowned deterministic guard staging path"):
        transaction.run()
    after = {
        path.relative_to(legacy).as_posix(): (None if path.is_dir() else path.read_bytes())
        for path in legacy.rglob("*")
    }
    assert legacy.is_dir()
    assert after == before
    assert Path(spec.hold).is_file()
    assert transaction._journal()["phase"] is None


def test_unowned_random_staging_with_unknown_marker_is_never_deleted(tmp_path):
    spec, _ = _fixture(tmp_path)
    transaction = NativeRuntimeCutover(spec, protector=TestProtector(), enforce_acl=False)
    unknown = transaction.guard_dir.with_name(
        f".{transaction.guard_dir.name}.{transaction.spec.transaction_id}.unknown.preparing"
    )
    unknown.mkdir(parents=True)
    marker = unknown / "unowned-marker.txt"
    marker.write_bytes(b"preserve-exactly")
    assert transaction.run() == "ACTIVATED"
    assert marker.read_bytes() == b"preserve-exactly"


@pytest.mark.parametrize(
    "snapshot",
    [
        GuardAclSnapshot(False, _exact_acl_snapshot().aces),
        GuardAclSnapshot(True, _exact_acl_snapshot("S-1-5-21-WRONG").aces),
        GuardAclSnapshot(
            True,
            (_exact_acl_snapshot().aces[0].__class__("S-1-5-21-1000", 0, 0x1F01FF, 0x13),)
            + _exact_acl_snapshot().aces[1:],
        ),
        GuardAclSnapshot(True, _exact_acl_snapshot().aces[:-1]),
        GuardAclSnapshot(
            True,
            _exact_acl_snapshot().aces
            + (GuardAclAce("S-1-1-0", 0, 0x120089, 0x03),),
        ),
        GuardAclSnapshot(
            True,
            _exact_acl_snapshot().aces
            + (GuardAclAce("S-1-1-0", 1, 0x120089, 0x03),),
        ),
    ],
    ids=["inheritance-enabled", "wrong-user-sid", "inherited-ace", "missing-admins", "extra-allow", "deny"],
)
def test_effective_guard_acl_rejects_drift(snapshot):
    with pytest.raises(CutoverError, match="effective ACL"):
        assert_exact_guard_acl(snapshot, "S-1-5-21-1000")


def test_guard_acl_is_applied_verified_and_journaled_boolean_only(tmp_path):
    spec, _ = _fixture(tmp_path)
    backend = FakeAclBackend()
    transaction = NativeRuntimeCutover(
        spec, protector=TestProtector(), acl_backend=backend,
    )
    with pytest.raises(CutoverError, match="synthetic interruption after PREPARED_DETACHED"):
        transaction.run(fail_after="PREPARED_DETACHED")
    journal = transaction._journal()
    assert journal["guard_acl_pass"] is True
    assert [key for key in journal if "acl" in key] == ["guard_acl_pass"]
    assert backend.applied_paths


def test_guard_acl_command_success_readback_mismatch_fails_held(tmp_path):
    class MismatchBackend(FakeAclBackend):
        def apply_exact(self, directory, user_sid):
            super().apply_exact(directory, user_sid)
            self.snapshot = GuardAclSnapshot(False, self.snapshot.aces)

    spec, _ = _fixture(tmp_path)
    transaction = NativeRuntimeCutover(
        spec, protector=TestProtector(), acl_backend=MismatchBackend(),
    )
    with pytest.raises(CutoverError, match="effective ACL"):
        transaction.run()
    assert Path(spec.hold).is_file()
    assert transaction._journal()["phase"] is None
    assert _git(Path(spec.source_repo), "rev-parse", "HEAD") == spec.base_commit


@pytest.mark.parametrize("drift", ["wrong-sid", "inheritance", "extra-allow", "deny"])
def test_restart_acl_drift_fails_held_before_source_mutation(tmp_path, drift):
    spec, _ = _fixture(tmp_path)
    backend = FakeAclBackend()
    transaction = NativeRuntimeCutover(
        spec, protector=TestProtector(), acl_backend=backend,
    )
    with pytest.raises(CutoverError, match="synthetic interruption after PREPARED_DETACHED"):
        transaction.run(fail_after="PREPARED_DETACHED")
    exact = _exact_acl_snapshot()
    if drift == "wrong-sid":
        backend.snapshot = _exact_acl_snapshot("S-1-5-21-WRONG")
    elif drift == "inheritance":
        backend.snapshot = GuardAclSnapshot(False, exact.aces)
    elif drift == "extra-allow":
        backend.snapshot = GuardAclSnapshot(
            True, exact.aces + (GuardAclAce("S-1-1-0", 0, 0x120089, 0x03),),
        )
    else:
        backend.snapshot = GuardAclSnapshot(
            True, exact.aces + (GuardAclAce("S-1-1-0", 1, 0x120089, 0x03),),
        )

    resumed = NativeRuntimeCutover(
        spec, protector=TestProtector(), acl_backend=backend,
    )
    with pytest.raises(CutoverError, match="effective ACL"):
        resumed.run()
    assert Path(spec.hold).is_file()
    assert _git(Path(spec.source_repo), "rev-parse", "HEAD") == spec.base_commit
    assert resumed._journal()["phase"] == "PREPARED_DETACHED"


def test_acl_drift_blocks_published_guard_deletion_held(tmp_path):
    spec, _ = _fixture(tmp_path)
    backend = FakeAclBackend()
    transaction = NativeRuntimeCutover(
        spec, protector=TestProtector(), acl_backend=backend,
        guard_fail_after="publication",
    )
    with pytest.raises(CutoverError, match="synthetic guard interruption after publication"):
        transaction.run()
    before = {path.name: path.read_bytes() for path in Path(spec.guard_dir).iterdir()}
    backend.snapshot = GuardAclSnapshot(False, _exact_acl_snapshot().aces)

    rollback = NativeRuntimeCutover(
        spec, protector=TestProtector(), acl_backend=backend,
    )
    with pytest.raises(CutoverError, match="effective ACL"):
        rollback.rollback()
    assert Path(spec.hold).is_file()
    assert {path.name: path.read_bytes() for path in Path(spec.guard_dir).iterdir()} == before


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL integration")
def test_windows_acl_backend_detects_real_widening(tmp_path):
    directory = tmp_path / "acl-guard"
    directory.mkdir()
    backend = WindowsGuardAclBackend()
    user_sid = backend.current_user_sid()
    try:
        backend.apply_exact(directory, user_sid)
        assert_exact_guard_acl(backend.read(directory), user_sid)
        widened = subprocess.run(
            ["icacls", str(directory), "/grant", "*S-1-1-0:(OI)(CI)RX"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
        )
        assert widened.returncode == 0, widened.stderr
        with pytest.raises(CutoverError, match="effective ACL"):
            assert_exact_guard_acl(backend.read(directory), user_sid)
    finally:
        backend.apply_exact(directory, user_sid)
