"""Journaled Hermes native-runtime cutover with private preimage recovery.

The tool has no implicit live paths.  Execution requires an explicit JSON spec
and ``--execute``; tests inject disposable repositories, packages and a
protector.  It never launches Hermes or accesses a provider.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import hmac
import json
import msvcrt
import os
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Protocol
from ctypes import wintypes


FORWARD_PHASES = (
    "PREPARED_DETACHED",
    "HEAD_ATTACHED_BASE",
    "SOURCE_TREE_PREPARED",
    "SOURCE_PROMOTED",
    "OLD_PACKAGE_QUARANTINED",
    "PACKAGE_PROMOTED",
    "BINDINGS_VERIFIED",
    "TUPLE_VERIFIED",
    "ACTIVATION_INTENT",
    "ACTIVATED",
)

ROLLBACK_PHASES = (
    "ROLLBACK_INTENT",
    "ROLLBACK_PACKAGE_RESTORED",
    "ROLLBACK_BINDINGS_RESTORED",
    "ROLLBACK_SOURCE_BASE_STAGED",
    "ROLLBACK_MAIN_BASE",
    "ROLLBACK_HEAD_DETACHED_BASE",
    "ROLLBACK_PROTECTED_STATE_VERIFIED",
    "ROLLED_BACK_DETACHED_BASE",
)

PHASES = FORWARD_PHASES + ROLLBACK_PHASES


class CutoverError(RuntimeError):
    pass


@dataclass(frozen=True)
class GuardAclAce:
    sid: str
    ace_type: int
    mask: int
    flags: int


@dataclass(frozen=True)
class GuardAclSnapshot:
    protected: bool
    aces: tuple[GuardAclAce, ...]
    owner_sid: str | None = None


class GuardAclBackend(Protocol):
    def current_user_sid(self) -> str: ...
    def apply_exact(self, directory: Path, user_sid: str) -> None: ...
    def read(self, directory: Path) -> GuardAclSnapshot: ...


_SYSTEM_SID = "S-1-5-18"
_ADMINISTRATORS_SID = "S-1-5-32-544"
_ACCESS_ALLOWED_ACE_TYPE = 0
_FILE_ALL_ACCESS = 0x001F01FF
_OBJECT_AND_CONTAINER_INHERIT = 0x03


class _AclSizeInformation(ctypes.Structure):
    _fields_ = [
        ("AceCount", wintypes.DWORD),
        ("AclBytesInUse", wintypes.DWORD),
        ("AclBytesFree", wintypes.DWORD),
    ]


class _AceHeader(ctypes.Structure):
    _fields_ = [
        ("AceType", ctypes.c_ubyte),
        ("AceFlags", ctypes.c_ubyte),
        ("AceSize", wintypes.WORD),
    ]


class _AccessAce(ctypes.Structure):
    _fields_ = [("Header", _AceHeader), ("Mask", wintypes.DWORD), ("SidStart", wintypes.DWORD)]


def _assert_exact_acl(snapshot: GuardAclSnapshot, user_sid: str, flags: int) -> None:
    expected = {
        (sid.upper(), _ACCESS_ALLOWED_ACE_TYPE, _FILE_ALL_ACCESS, flags)
        for sid in (user_sid, _SYSTEM_SID, _ADMINISTRATORS_SID)
    }
    observed = {
        (ace.sid.upper(), ace.ace_type, ace.mask, ace.flags)
        for ace in snapshot.aces
    }
    if not snapshot.protected or len(snapshot.aces) != 3 or observed != expected:
        raise CutoverError("private guard effective ACL does not match the exact policy")


def assert_exact_guard_acl(snapshot: GuardAclSnapshot, user_sid: str) -> None:
    """Accept only the protected, three-principal full-control guard DACL."""
    _assert_exact_acl(snapshot, user_sid, _OBJECT_AND_CONTAINER_INHERIT)


class WindowsGuardAclBackend:
    """Construct and read back the guard DACL using SID identities only."""

    def __init__(self) -> None:
        self._current_sid: str | None = None

    @staticmethod
    def _require_windows() -> None:
        if os.name != "nt":
            raise CutoverError("private guard ACL enforcement requires Windows")

    @staticmethod
    def _sid_string(sid: ctypes.c_void_p) -> str:
        advapi32 = ctypes.windll.advapi32
        kernel32 = ctypes.windll.kernel32
        rendered = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(rendered)):
            raise CutoverError("Windows SID conversion failed")
        try:
            return rendered.value
        finally:
            kernel32.LocalFree(rendered)

    def current_user_sid(self) -> str:
        self._require_windows()
        if self._current_sid is not None:
            return self._current_sid
        script = "[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value"
        encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        result = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
            creationflags=0x08000000,
        )
        candidate = result.stdout.strip()
        if result.returncode or not candidate.startswith("S-") or not all(
            part.isdigit() for part in candidate[2:].split("-")
        ):
            raise CutoverError("current Windows SID could not be resolved")
        self._current_sid = candidate
        return candidate

    def apply_exact(self, directory: Path, user_sid: str) -> None:
        self._require_windows()
        script = r"""
$ErrorActionPreference = 'Stop'
$item = Get-Item -LiteralPath $env:SAKAAN_GUARD_ACL_PATH -Force
$security = if ($item.PSIsContainer) {
  [System.Security.AccessControl.DirectorySecurity]::new()
} else {
  [System.Security.AccessControl.FileSecurity]::new()
}
$security.SetAccessRuleProtection($true, $false)
$inheritance = if ($item.PSIsContainer) {
  [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
} else {
  [System.Security.AccessControl.InheritanceFlags]::None
}
foreach ($sidText in @($env:SAKAAN_GUARD_USER_SID, 'S-1-5-18', 'S-1-5-32-544')) {
  $sid = [System.Security.Principal.SecurityIdentifier]::new($sidText)
  $rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
    $sid,
    [System.Security.AccessControl.FileSystemRights]::FullControl,
    $inheritance,
    [System.Security.AccessControl.PropagationFlags]::None,
    [System.Security.AccessControl.AccessControlType]::Allow
  )
  [void]$security.AddAccessRule($rule)
}
$item.SetAccessControl($security)
"""
        encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        environment = os.environ.copy()
        environment["SAKAAN_GUARD_ACL_PATH"] = str(directory)
        environment["SAKAAN_GUARD_USER_SID"] = user_sid
        result = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
            creationflags=0x08000000,
        )
        if result.returncode:
            raise CutoverError("private guard ACL could not be restricted")

    def read(self, directory: Path) -> GuardAclSnapshot:
        self._require_windows()
        advapi32 = ctypes.windll.advapi32
        kernel32 = ctypes.windll.kernel32
        dacl = ctypes.c_void_p()
        owner = ctypes.c_void_p()
        descriptor = ctypes.c_void_p()
        result = advapi32.GetNamedSecurityInfoW(
            str(directory), 1, 0x00000005, ctypes.byref(owner), None,
            ctypes.byref(dacl), None, ctypes.byref(descriptor),
        )
        if result or not descriptor.value or not dacl.value or not owner.value:
            if descriptor.value:
                kernel32.LocalFree(descriptor)
            raise CutoverError("private guard effective ACL could not be read")
        try:
            control = wintypes.WORD()
            revision = wintypes.DWORD()
            if not advapi32.GetSecurityDescriptorControl(
                descriptor, ctypes.byref(control), ctypes.byref(revision),
            ):
                raise CutoverError("private guard ACL control could not be read")
            size = _AclSizeInformation()
            if not advapi32.GetAclInformation(
                dacl, ctypes.byref(size), ctypes.sizeof(size), 2,
            ):
                raise CutoverError("private guard ACL entries could not be read")
            aces = []
            for index in range(size.AceCount):
                pointer = ctypes.c_void_p()
                if not advapi32.GetAce(dacl, index, ctypes.byref(pointer)):
                    raise CutoverError("private guard ACL entry could not be read")
                header = ctypes.cast(pointer, ctypes.POINTER(_AceHeader)).contents
                if header.AceType not in (0, 1):
                    # Object/callback/audit ACE layouts have different SID
                    # offsets.  They are forbidden here, so reject without
                    # interpreting attacker-controlled layout as AccessAce.
                    aces.append(GuardAclAce(
                        sid="UNSUPPORTED", ace_type=int(header.AceType),
                        mask=0, flags=int(header.AceFlags),
                    ))
                    continue
                ace = ctypes.cast(pointer, ctypes.POINTER(_AccessAce)).contents
                sid_pointer = ctypes.c_void_p(pointer.value + _AccessAce.SidStart.offset)
                aces.append(GuardAclAce(
                    sid=self._sid_string(sid_pointer), ace_type=int(header.AceType),
                    mask=int(ace.Mask), flags=int(header.AceFlags),
                ))
            return GuardAclSnapshot(
                protected=bool(control.value & 0x1000), aces=tuple(aces),
                owner_sid=self._sid_string(owner),
            )
        finally:
            kernel32.LocalFree(descriptor)


class Protector(Protocol):
    def protect(self, raw: bytes) -> bytes: ...
    def unprotect(self, protected: bytes) -> bytes: ...


class WindowsDpapiProtector:
    """Current-user DPAPI without third-party dependencies."""

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    @classmethod
    def _crypt(cls, raw: bytes, *, decrypt: bool) -> bytes:
        if os.name != "nt":
            raise CutoverError("DPAPI is available only on Windows")
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        in_buffer = ctypes.create_string_buffer(raw)
        in_blob = cls._Blob(len(raw), ctypes.cast(in_buffer, ctypes.POINTER(ctypes.c_byte)))
        out_blob = cls._Blob()
        if decrypt:
            ok = crypt32.CryptUnprotectData(
                ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)
            )
        else:
            ok = crypt32.CryptProtectData(
                ctypes.byref(in_blob), "Hermes native runtime guard", None, None, None, 0,
                ctypes.byref(out_blob),
            )
        if not ok:
            raise CutoverError(f"DPAPI operation failed: {ctypes.GetLastError()}")
        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            kernel32.LocalFree(out_blob.pbData)

    def protect(self, raw: bytes) -> bytes:
        return self._crypt(raw, decrypt=False)

    def unprotect(self, protected: bytes) -> bytes:
        return self._crypt(protected, decrypt=True)


@dataclass(frozen=True)
class CutoverSpec:
    transaction_id: str
    source_repo: str
    base_commit: str
    release_commit: str
    live_package: str
    stage_package: str
    rollback_package: str
    live_cmd: str
    candidate_cmd: str
    auth_file: str
    guard_dir: str
    journal: str
    hold: str
    manifest: str
    manifest_sha256: str
    package_hash_list: str
    package_hash_list_sha256: str
    package_tree_sha256: str
    package_file_count: int
    package_bytes: int
    live_package_tree_sha256: str
    live_package_file_count: int
    live_package_bytes: int
    candidate_cmd_sha256: str
    launcher_ps1_blob_sha256: str
    launcher_vbs_blob_sha256: str
    launcher_ps1_sha256: str
    launcher_vbs_sha256: str
    cli_launcher_blob_sha256: str
    cli_launcher_sha256: str
    branch_plan_sha256: str
    branch_plan_audit_sha256: str
    required_branch: str = "main"
    detached_head_policy: str = "attach-under-hold"
    predecessor_spec: str | None = None
    predecessor_spec_sha256: str | None = None
    predecessor_receipt: str | None = None
    predecessor_receipt_sha256: str | None = None
    predecessor_journal_sha256: str | None = None
    handover_receipt: str | None = None
    handover_lock: str | None = None

    @classmethod
    def load(cls, path: Path) -> "CutoverSpec":
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise CutoverError("spec must be an object")
        spec = cls(**{name: data[name] for name in cls.__dataclass_fields__ if name in data})
        for name in (
            "source_repo", "live_package", "stage_package", "rollback_package", "live_cmd",
            "candidate_cmd", "auth_file", "guard_dir", "journal", "hold", "manifest",
            "package_hash_list",
        ):
            if not Path(getattr(spec, name)).is_absolute():
                raise CutoverError(f"{name} must be absolute")
        predecessor_values = (
            spec.predecessor_spec, spec.predecessor_spec_sha256,
            spec.predecessor_receipt, spec.predecessor_receipt_sha256,
            spec.predecessor_journal_sha256, spec.handover_receipt, spec.handover_lock,
        )
        if any(value is not None for value in predecessor_values):
            if any(value is None for value in predecessor_values):
                raise CutoverError("predecessor handover fields must be complete")
            for name in ("predecessor_spec", "predecessor_receipt", "handover_receipt", "handover_lock"):
                if not Path(str(getattr(spec, name))).is_absolute():
                    raise CutoverError(f"{name} must be absolute")
        if not spec.transaction_id or spec.base_commit == spec.release_commit:
            raise CutoverError("invalid transaction identity")
        if spec.detached_head_policy != "attach-under-hold" or spec.required_branch != "main":
            raise CutoverError("unsupported canonical branch policy")
        for name in (
            "manifest_sha256", "package_hash_list_sha256", "package_tree_sha256",
            "live_package_tree_sha256",
            "candidate_cmd_sha256", "launcher_ps1_sha256", "launcher_vbs_sha256",
            "launcher_ps1_blob_sha256", "launcher_vbs_blob_sha256",
            "cli_launcher_blob_sha256", "cli_launcher_sha256",
            "branch_plan_sha256", "branch_plan_audit_sha256",
        ):
            value = str(getattr(spec, name))
            if len(value) != 64 or any(char not in "0123456789abcdefABCDEF" for char in value):
                raise CutoverError(f"{name} must be a SHA-256 digest")
        for name in (
            "predecessor_spec_sha256", "predecessor_receipt_sha256", "predecessor_journal_sha256",
        ):
            value = getattr(spec, name)
            if value is not None and (
                len(str(value)) != 64
                or any(char not in "0123456789abcdefABCDEF" for char in str(value))
            ):
                raise CutoverError(f"{name} must be a SHA-256 digest")
        for name in ("base_commit", "release_commit"):
            value = str(getattr(spec, name))
            if len(value) != 40 or any(char not in "0123456789abcdefABCDEF" for char in value):
                raise CutoverError(f"{name} must be an exact 40-character commit")
        if (
            spec.package_file_count < 1 or spec.package_bytes < 1
            or spec.live_package_file_count < 1 or spec.live_package_bytes < 1
        ):
            raise CutoverError("package counts must be positive")
        return spec


def _atomic_write(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], text=True, encoding="utf-8",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if result.returncode:
        raise CutoverError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _git_optional(repo: Path, *args: str) -> tuple[int, str, str]:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], text=True, encoding="utf-8",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _git_stdin(repo: Path, payload: str, *, message: str) -> None:
    result = subprocess.run(
        ["git", "-C", str(repo), "update-ref", "--stdin", "-m", message],
        input=payload.encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        raise CutoverError(
            f"git update-ref transaction failed: {result.stderr.decode('utf-8', errors='replace').strip()}"
        )


def _git_with_env(repo: Path, environment: dict[str, str], *args: str) -> str:
    process_env = os.environ.copy()
    process_env.update(environment)
    result = subprocess.run(
        ["git", "-C", str(repo), *args], env=process_env, text=True, encoding="utf-8",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if result.returncode:
        raise CutoverError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


_ROLLBACK_DETACH_REFLOG_ACTION = "sakaan R7 cutover rollback: detach HEAD at BASE"


def _git_blob(repo: Path, commit: str, path: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), "show", f"{commit}:{path}"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if result.returncode:
        raise CutoverError(f"release blob is unavailable: {path}")
    return result.stdout


def file_sha256(path: Path) -> str:
    if not path.is_file():
        raise CutoverError(f"required file is missing: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _expected_package_rows(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CutoverError("package hash list is unreadable") from exc
    rows: dict[str, str] = {}
    for line in text.splitlines():
        if not line:
            continue
        digest, separator, relative = line.partition("  ")
        relative_path = Path(relative)
        if (
            not separator or len(digest) != 64
            or any(char not in "0123456789abcdefABCDEF" for char in digest)
            or not relative or relative_path.is_absolute() or ".." in relative_path.parts
            or relative in rows
        ):
            raise CutoverError("package hash list is invalid")
        rows[relative.replace("\\", "/")] = digest.lower()
    if not rows:
        raise CutoverError("package hash list is empty")
    return rows


def verify_package_inventory(root: Path, hash_list: Path) -> tuple[int, int, str]:
    expected = _expected_package_rows(hash_list)
    actual_paths = {
        item.relative_to(root).as_posix(): item
        for item in root.rglob("*") if item.is_file()
    } if root.is_dir() else {}
    if set(actual_paths) != set(expected):
        raise CutoverError("package inventory has missing or extra files")
    total = 0
    for relative, path in actual_paths.items():
        raw = path.read_bytes()
        total += len(raw)
        if not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), expected[relative]):
            raise CutoverError(f"package inventory digest mismatch: {relative}")
    return len(actual_paths), total, tree_digest(root)


def tree_digest(root: Path) -> str:
    if not root.is_dir():
        raise CutoverError(f"package directory is missing: {root}")
    digest = hashlib.sha256()
    for path in sorted((item for item in root.rglob("*") if item.is_file()), key=lambda p: p.as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        raw = path.read_bytes()
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(hashlib.sha256(raw).digest())
    return digest.hexdigest()


def _is_reparse_path(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError as exc:
        raise CutoverError(f"package path is unreadable: {path}") from exc
    if stat.S_ISLNK(info.st_mode):
        return True
    if bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(callable(is_junction) and is_junction())


def _path_lexists(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


def ordinary_directory_identity(root: Path) -> tuple[int, int, str]:
    """Return a stable identity while rejecting links/reparse points and non-regular entries."""
    if not root.exists() or _is_reparse_path(root) or not stat.S_ISDIR(root.lstat().st_mode):
        raise CutoverError("live package must be an existing ordinary non-reparse directory")
    files: list[Path] = []
    for current, directories, names in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in (*directories, *names):
            item = current_path / name
            if _is_reparse_path(item):
                raise CutoverError("live package must not contain reparse paths")
        for name in names:
            item = current_path / name
            if not stat.S_ISREG(item.lstat().st_mode):
                raise CutoverError("live package contains a non-regular file")
            files.append(item)
    digest = hashlib.sha256()
    total = 0
    for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        raw = path.read_bytes()
        total += len(raw)
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(hashlib.sha256(raw).digest())
    return len(files), total, digest.hexdigest()


class NativeRuntimeCutover:
    def __init__(
        self, spec: CutoverSpec, *, protector: Protector, enforce_acl: bool = True,
        guard_fail_after: str | None = None, acl_backend: GuardAclBackend | None = None,
        handover_fail_after: str | None = None,
    ):
        self.spec = spec
        self.protector = protector
        self.enforce_acl = enforce_acl
        self.acl_backend = acl_backend or WindowsGuardAclBackend()
        self.guard_fail_after = guard_fail_after
        self.handover_fail_after = handover_fail_after
        self.repo = Path(spec.source_repo)
        self.live_package = Path(spec.live_package)
        self.stage_package = Path(spec.stage_package)
        self.rollback_package = Path(spec.rollback_package)
        self.live_cmd = Path(spec.live_cmd)
        self.candidate_cmd = Path(spec.candidate_cmd)
        self.auth_file = Path(spec.auth_file)
        self.guard_dir = Path(spec.guard_dir)
        self.journal_path = Path(spec.journal)
        self.hold = Path(spec.hold)
        self.manifest_path = Path(spec.manifest)
        self.package_hash_list = Path(spec.package_hash_list)
        self.guard_staging_receipt = self.guard_dir.with_name(
            f".{self.guard_dir.name}.{self.spec.transaction_id}.staging.json"
        )
        self.legacy_guard_staging_dir = self.guard_dir.with_name(
            f".{self.guard_dir.name}.{self.spec.transaction_id}.preparing"
        )
        self.guard_staging_dir = self.guard_dir.with_name(
            f".{self.guard_dir.name}.{self.spec.transaction_id}.unassigned"
        )

    @staticmethod
    def _hold_bytes(transaction_id: str, base_commit: str, release_commit: str) -> bytes:
        return _json_bytes({
            "version": 1, "transaction_id": transaction_id,
            "base_commit": base_commit, "release_commit": release_commit,
        })

    def _handover_interruption(self, boundary: str) -> None:
        if self.handover_fail_after == boundary:
            raise CutoverError(f"synthetic handover interruption after {boundary}")

    def _owned_file_identity(self, path: Path, label: str) -> tuple[int, int]:
        """Validate a handover file before it is opened, read, written or promoted."""
        if not _path_lexists(path) or _is_reparse_path(path):
            raise CutoverError(f"{label} must be an ordinary non-reparse file")
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise CutoverError(f"{label} must be a single-link regular file")
        if self.enforce_acl:
            user_sid = self.acl_backend.current_user_sid()
            snapshot = self.acl_backend.read(path)
            _assert_exact_acl(snapshot, user_sid, 0)
            if snapshot.owner_sid is None or snapshot.owner_sid.upper() != user_sid.upper():
                raise CutoverError(f"{label} owner SID does not match the current user")
        return int(info.st_dev), int(info.st_ino)

    def _open_owned_file(self, path: Path, label: str, expected: bytes | None = None):
        before = self._owned_file_identity(path, label)
        flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise CutoverError(f"{label} could not be opened safely") from exc
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or (int(opened.st_dev), int(opened.st_ino)) != before
                or self._owned_file_identity(path, label) != before
            ):
                raise CutoverError(f"{label} path identity changed while opening")
            handle = os.fdopen(descriptor, "r+b", closefd=True)
            descriptor = -1
            if expected is not None:
                handle.seek(0)
                observed = handle.read()
                if not hmac.compare_digest(observed, expected):
                    handle.close()
                    raise CutoverError(f"{label} content does not match this transaction")
                handle.seek(0)
            if self._owned_file_identity(path, label) != before:
                handle.close()
                raise CutoverError(f"{label} path identity changed after opening")
            return handle, before
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _publish_owned_file(self, path: Path, raw: bytes, label: str) -> None:
        """Publish exact protected bytes without overwriting an unknown filesystem object."""
        staging = path.with_name(f".{path.name}.{self.spec.transaction_id}.owned")
        if _path_lexists(staging):
            handle, _ = self._open_owned_file(staging, f"{label} staging file", raw)
            handle.close()
        else:
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
            try:
                descriptor = os.open(staging, flags, 0o600)
            except FileExistsError as exc:
                raise CutoverError(f"{label} staging ownership changed during creation") from exc
            try:
                with os.fdopen(descriptor, "wb", closefd=True) as handle:
                    descriptor = -1
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
            if self.enforce_acl:
                user_sid = self.acl_backend.current_user_sid()
                self.acl_backend.apply_exact(staging, user_sid)
            handle, _ = self._open_owned_file(staging, f"{label} staging file", raw)
            handle.close()
        if _path_lexists(path):
            raise CutoverError(f"{label} appeared during exclusive publication")
        try:
            os.rename(staging, path)
        except OSError as exc:
            raise CutoverError(f"{label} could not be published exclusively") from exc
        handle, _ = self._open_owned_file(path, label, raw)
        handle.close()

    def _replace_owned_file(self, path: Path, raw: bytes, label: str) -> None:
        """Atomically replace an already trusted owned file with equally trusted bytes."""
        prior_identity = None
        if _path_lexists(path):
            handle, prior_identity = self._open_owned_file(path, label)
            handle.close()
        staging = path.with_name(f".{path.name}.{self.spec.transaction_id}.replacement")
        if _path_lexists(staging):
            handle, _ = self._open_owned_file(staging, f"{label} replacement", raw)
            handle.close()
        else:
            self._publish_owned_file(staging, raw, f"{label} replacement")
        if prior_identity is not None and self._owned_file_identity(path, label) != prior_identity:
            raise CutoverError(f"{label} identity changed before replacement")
        if prior_identity is None and _path_lexists(path):
            raise CutoverError(f"{label} appeared before replacement")
        os.replace(staging, path)
        handle, _ = self._open_owned_file(path, label, raw)
        handle.close()

    def _restore_handover_hold(self, raw: bytes) -> None:
        """Replace an untrusted HOLD entry without dereferencing or mutating its target."""
        replacement = self.hold.with_name(
            f".{self.hold.name}.{self.spec.transaction_id}.restore"
        )
        if _path_lexists(replacement):
            handle, _ = self._open_owned_file(replacement, "HOLD restoration", raw)
            handle.close()
        else:
            self._publish_owned_file(replacement, raw, "HOLD restoration")
        os.replace(replacement, self.hold)
        handle, _ = self._open_owned_file(self.hold, "restored predecessor HOLD", raw)
        handle.close()

    def _verify_predecessor_terminal(self) -> tuple[SimpleNamespace, bytes]:
        assert self.spec.predecessor_spec is not None
        predecessor_spec_path = Path(self.spec.predecessor_spec)
        predecessor_receipt_path = Path(str(self.spec.predecessor_receipt))
        for label, path in (
            ("predecessor spec", predecessor_spec_path),
            ("predecessor receipt", predecessor_receipt_path),
        ):
            if not path.is_file() or _is_reparse_path(path):
                raise CutoverError(f"{label} must be an ordinary non-reparse file")
        if not hmac.compare_digest(
            file_sha256(predecessor_spec_path), str(self.spec.predecessor_spec_sha256).lower(),
        ):
            raise CutoverError("predecessor spec digest mismatch")
        if not hmac.compare_digest(
            file_sha256(predecessor_receipt_path), str(self.spec.predecessor_receipt_sha256).lower(),
        ):
            raise CutoverError("predecessor receipt digest mismatch")
        try:
            predecessor_data = json.loads(predecessor_spec_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CutoverError("predecessor spec is unreadable") from exc
        required = (
            "transaction_id", "source_repo", "base_commit", "release_commit", "live_package",
            "rollback_package", "live_cmd", "auth_file", "guard_dir", "journal", "hold",
            "live_package_tree_sha256", "live_package_file_count", "live_package_bytes",
        )
        if any(name not in predecessor_data for name in required):
            raise CutoverError("predecessor spec identity is incomplete")
        predecessor = SimpleNamespace(**predecessor_data)
        if (
            predecessor.transaction_id == self.spec.transaction_id
            or predecessor.source_repo != self.spec.source_repo
            or predecessor.base_commit != self.spec.base_commit
            or predecessor.live_package != self.spec.live_package
            or predecessor.live_cmd != self.spec.live_cmd
            or predecessor.auth_file != self.spec.auth_file
            or predecessor.hold != self.spec.hold
            or predecessor.guard_dir == self.spec.guard_dir
        ):
            raise CutoverError("predecessor spec does not bind this successor")
        journal_path = Path(predecessor.journal)
        if not journal_path.is_file() or _is_reparse_path(journal_path):
            raise CutoverError("predecessor journal must be an ordinary non-reparse file")
        if not hmac.compare_digest(
            file_sha256(journal_path), str(self.spec.predecessor_journal_sha256).lower(),
        ):
            raise CutoverError("predecessor terminal journal digest mismatch")
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        if (
            journal.get("transaction_id") != predecessor.transaction_id
            or journal.get("phase") != "ROLLED_BACK_DETACHED_BASE"
            or journal.get("release_commit") != predecessor.release_commit
            or journal.get("auth_equal") is not True
            or journal.get("guard_acl_pass") is not True
        ):
            raise CutoverError("predecessor journal is not an exact terminal rollback")
        predecessor_tx = NativeRuntimeCutover(
            predecessor, protector=self.protector, enforce_acl=self.enforce_acl,
            acl_backend=self.acl_backend,
        )
        predecessor_tx._require_source_state("F0")
        predecessor_tx._verify_live_package_preimage()
        _, auth_preimage, cmd_preimage = predecessor_tx._guard()
        if not hmac.compare_digest(auth_preimage, self.auth_file.read_bytes()):
            raise CutoverError("predecessor auth preimage does not match live auth")
        if not hmac.compare_digest(cmd_preimage, self.live_cmd.read_bytes()):
            raise CutoverError("predecessor CMD preimage does not match live CMD")
        if Path(predecessor.rollback_package).exists():
            raise CutoverError("predecessor rollback package must be absent")
        return predecessor, self._hold_bytes(
            predecessor.transaction_id, predecessor.base_commit, predecessor.release_commit,
        )

    def _handover_predecessor_hold(self) -> None:
        if self.spec.predecessor_spec is None:
            return
        assert self.spec.handover_receipt is not None and self.spec.handover_lock is not None
        receipt_path = Path(self.spec.handover_receipt)
        lock_path = Path(self.spec.handover_lock)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        if not _path_lexists(lock_path):
            self._publish_owned_file(lock_path, b"0", "handover lock")
        try:
            lock_handle, lock_identity = self._open_owned_file(
                lock_path, "handover lock", b"0",
            )
        except (CutoverError, OSError) as exc:
            if isinstance(exc, PermissionError) or isinstance(exc.__cause__, PermissionError):
                raise CutoverError("predecessor handover is already in progress") from exc
            raise
        with lock_handle:
            try:
                if self._owned_file_identity(lock_path, "handover lock") != lock_identity:
                    raise CutoverError("handover lock identity changed before locking")
            except PermissionError as exc:
                raise CutoverError("predecessor handover is already in progress") from exc
            try:
                msvcrt.locking(lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise CutoverError("predecessor handover is already in progress") from exc
            try:
                predecessor, predecessor_hold = self._verify_predecessor_terminal()
                successor_hold = self._hold_bytes(
                    self.spec.transaction_id, self.spec.base_commit, self.spec.release_commit,
                )
                hold_handle, hold_identity = self._open_owned_file(
                    self.hold, "predecessor HOLD",
                )
                with hold_handle:
                    observed_hold = hold_handle.read()
                if observed_hold not in (predecessor_hold, successor_hold):
                    raise CutoverError("predecessor HOLD identity mismatch")
                if observed_hold == predecessor_hold and (
                    self.journal_path.exists() or self.guard_dir.exists() or self.rollback_package.exists()
                ):
                    raise CutoverError("successor state exists before HOLD handover")
                self._handover_interruption("verified")
                receipt = {
                    "version": 1,
                    "phase": "PREPARED" if observed_hold == predecessor_hold else "ADOPTED",
                    "predecessor_transaction_id": predecessor.transaction_id,
                    "successor_transaction_id": self.spec.transaction_id,
                    "predecessor_spec_sha256": str(self.spec.predecessor_spec_sha256).lower(),
                    "predecessor_receipt_sha256": str(self.spec.predecessor_receipt_sha256).lower(),
                    "predecessor_journal_sha256": str(self.spec.predecessor_journal_sha256).lower(),
                    "successor_hold_sha256": hashlib.sha256(successor_hold).hexdigest(),
                }
                if _path_lexists(receipt_path):
                    receipt_handle, _ = self._open_owned_file(receipt_path, "handover receipt")
                    with receipt_handle:
                        existing = json.loads(receipt_handle.read().decode("utf-8"))
                    if any(existing.get(key) != value for key, value in receipt.items() if key != "phase"):
                        raise CutoverError("handover receipt identity mismatch")
                if observed_hold == predecessor_hold:
                    candidate = self.hold.with_name(f".{self.hold.name}.{self.spec.transaction_id}.next")
                    if _path_lexists(candidate):
                        candidate_handle, candidate_identity = self._open_owned_file(
                            candidate, "handover HOLD candidate", successor_hold,
                        )
                        candidate_handle.close()
                    else:
                        self._publish_owned_file(
                            candidate, successor_hold, "handover HOLD candidate",
                        )
                        candidate_identity = self._owned_file_identity(
                            candidate, "handover HOLD candidate",
                        )
                    self._handover_interruption("candidate")
                    self._replace_owned_file(
                        receipt_path, _json_bytes(receipt), "handover receipt",
                    )
                    self._handover_interruption("receipt")
                    hold_handle, current_hold_identity = self._open_owned_file(
                        self.hold, "predecessor HOLD", predecessor_hold,
                    )
                    hold_handle.close()
                    if current_hold_identity != hold_identity:
                        raise CutoverError("predecessor HOLD changed before atomic handover")
                    candidate_handle, current_candidate_identity = self._open_owned_file(
                        candidate, "handover HOLD candidate", successor_hold,
                    )
                    candidate_handle.close()
                    if current_candidate_identity != candidate_identity:
                        raise CutoverError("handover HOLD candidate identity changed before promotion")
                    os.replace(candidate, self.hold)
                    try:
                        promoted_handle, promoted_identity = self._open_owned_file(
                            self.hold, "successor HOLD", successor_hold,
                        )
                        promoted_handle.close()
                        if promoted_identity != candidate_identity:
                            raise CutoverError("promoted HOLD is not the verified candidate")
                    except (CutoverError, OSError):
                        self._restore_handover_hold(predecessor_hold)
                        raise
                    self._handover_interruption("replace")
                self._ensure_hold()
                self._handover_interruption("readback")
                receipt["phase"] = "ADOPTED"
                self._replace_owned_file(
                    receipt_path, _json_bytes(receipt), "handover receipt",
                )
            finally:
                lock_handle.seek(0)
                msvcrt.locking(lock_handle.fileno(), msvcrt.LK_UNLCK, 1)

    def _verify_source_preconditions(self) -> None:
        if not self.repo.is_dir():
            raise CutoverError("canonical source repository is missing")
        if _git(self.repo, "rev-parse", "HEAD") != self.spec.base_commit:
            raise CutoverError("source is not at BASE")
        branch_rc, _, _ = _git_optional(self.repo, "symbolic-ref", "-q", "HEAD")
        if branch_rc == 0:
            raise CutoverError("canonical HEAD must be detached at BASE before preparation")
        if _git(self.repo, "rev-parse", "refs/heads/main") != self.spec.base_commit:
            raise CutoverError("main is not at BASE")
        if _git(self.repo, "status", "--porcelain=v1", "--untracked-files=all"):
            raise CutoverError("canonical source is dirty")
        if _git(self.repo, "ls-files", "--unmerged"):
            raise CutoverError("canonical source has unmerged entries")
        if _git(self.repo, "write-tree") != _git(self.repo, "rev-parse", f"{self.spec.base_commit}^{{tree}}"):
            raise CutoverError("canonical index is not the BASE tree")
        self._verify_no_git_locks()
        self._verify_no_other_main_worktree()
        if _git_optional(self.repo, "cat-file", "-e", f"{self.spec.release_commit}^{{commit}}")[0]:
            raise CutoverError("release commit does not exist")
        if _git_optional(
            self.repo, "merge-base", "--is-ancestor", self.spec.base_commit, self.spec.release_commit,
        )[0]:
            raise CutoverError("release is not a descendant of BASE")
        if _git(self.repo, "rev-list", "--count", f"{self.spec.base_commit}..{self.spec.release_commit}") != "1":
            raise CutoverError("release must be exactly one commit over BASE")

    def _git_path(self, name: str) -> Path:
        return Path(_git(self.repo, "rev-parse", "--path-format=absolute", "--git-path", name))

    def _verify_no_git_locks(self) -> None:
        for name in ("HEAD.lock", "index.lock", "packed-refs.lock", "refs/heads/main.lock"):
            if _path_lexists(self._git_path(name)):
                raise CutoverError("canonical Git lock exists")

    def _verify_no_other_main_worktree(self) -> None:
        records = _git(self.repo, "worktree", "list", "--porcelain").split("\n\n")
        here = os.path.normcase(os.path.abspath(self.repo))
        for record in records:
            fields = dict(
                line.split(" ", 1) if " " in line else (line, "")
                for line in record.splitlines() if line
            )
            path = fields.get("worktree")
            if (
                fields.get("branch") == "refs/heads/main" and path
                and os.path.normcase(os.path.abspath(path)) != here
            ):
                raise CutoverError("another worktree has main checked out")

    def _head_file(self) -> Path:
        path = self._git_path("HEAD")
        if _is_reparse_path(path) or not stat.S_ISREG(path.lstat().st_mode):
            raise CutoverError("canonical HEAD must be an ordinary non-reparse file")
        return path

    def _direct_head_preimage(self) -> bytes:
        raw = self._head_file().read_bytes()
        if raw not in {self.spec.base_commit.encode("ascii"), (self.spec.base_commit + "\n").encode("ascii")}:
            raise CutoverError("canonical raw HEAD is not the exact detached BASE preimage")
        return raw

    def _source_state(self) -> str:
        self._verify_no_git_locks()
        head = _git(self.repo, "rev-parse", "HEAD")
        main = _git(self.repo, "rev-parse", "refs/heads/main")
        branch_rc, branch, _ = _git_optional(self.repo, "symbolic-ref", "-q", "HEAD")
        index = _git(self.repo, "write-tree")
        base_tree = _git(self.repo, "rev-parse", f"{self.spec.base_commit}^{{tree}}")
        release_tree = _git(self.repo, "rev-parse", f"{self.spec.release_commit}^{{tree}}")
        dirty = bool(_git(self.repo, "status", "--porcelain=v1", "--untracked-files=all"))
        untracked = bool(_git(self.repo, "ls-files", "--others", "--exclude-standard"))
        worktree_rc, _, _ = _git_optional(self.repo, "diff", "--quiet")
        if worktree_rc not in (0, 1):
            raise CutoverError("canonical worktree comparison failed")
        worktree_dirty = worktree_rc == 1
        unmerged = bool(_git(self.repo, "ls-files", "--unmerged"))
        if unmerged:
            return "INVALID"
        if branch_rc and head == main == self.spec.base_commit and index == base_tree and not dirty:
            return "F0"
        if not branch_rc and branch == "refs/heads/main" and head == main == self.spec.base_commit:
            if index == base_tree and not dirty:
                return "F1"
            if index == release_tree and not worktree_dirty and not untracked:
                return "F2"
        if not branch_rc and branch == "refs/heads/main" and head == main == self.spec.release_commit:
            if index == release_tree and not dirty:
                return "F3"
            if index == base_tree and not worktree_dirty and not untracked:
                return "R1"
        if not branch_rc and branch == "refs/heads/main" and head == main == self.spec.base_commit and index == base_tree and not dirty:
            return "R2"
        return "INVALID"

    def _require_source_state(self, expected: str, *, late: bool = False) -> None:
        try:
            self._verify_no_git_locks()
        except CutoverError as exc:
            if late:
                raise CutoverError(f"LATE_SOURCE_DIRTINESS_HOLD: {exc}") from exc
            raise
        observed = self._source_state()
        if observed != expected:
            prefix = "LATE_SOURCE_DIRTINESS_HOLD" if late else "SOURCE_STATE_HOLD"
            raise CutoverError(f"{prefix}: expected {expected}, observed {observed}")

    def _verify_manifest(self) -> None:
        if not hmac.compare_digest(
            file_sha256(self.manifest_path), self.spec.manifest_sha256.lower(),
        ):
            raise CutoverError("audited manifest digest mismatch")
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CutoverError("audited manifest is unreadable") from exc
        expected = {
            "base_commit": self.spec.base_commit,
            "release_commit": self.spec.release_commit,
            "commit_count": 1,
            "stage": self.spec.stage_package,
            "package_file_count": self.spec.package_file_count,
            "package_bytes": self.spec.package_bytes,
            "live_package_file_count": self.spec.live_package_file_count,
            "live_package_bytes": self.spec.live_package_bytes,
            "live_package_tree_sha256": self.spec.live_package_tree_sha256.upper(),
            "package_hash_list_sha256": self.spec.package_hash_list_sha256.upper(),
            "package_tree_sha256": self.spec.package_tree_sha256.upper(),
            "candidate_cmd_sha256": self.spec.candidate_cmd_sha256.upper(),
            "launcher_ps1_sha256": self.spec.launcher_ps1_sha256.upper(),
            "launcher_vbs_sha256": self.spec.launcher_vbs_sha256.upper(),
            "launcher_ps1_blob_sha256": self.spec.launcher_ps1_blob_sha256.upper(),
            "launcher_vbs_blob_sha256": self.spec.launcher_vbs_blob_sha256.upper(),
            "cli_launcher_blob_sha256": self.spec.cli_launcher_blob_sha256.upper(),
            "cli_launcher_sha256": self.spec.cli_launcher_sha256.upper(),
            "branch_plan_sha256": self.spec.branch_plan_sha256.upper(),
            "branch_plan_audit_sha256": self.spec.branch_plan_audit_sha256.upper(),
        }
        for key, value in expected.items():
            observed = manifest.get(key)
            if isinstance(value, str) and key.endswith("sha256"):
                observed = str(observed or "").upper()
            if observed != value:
                raise CutoverError(f"manifest identity mismatch: {key}")

    def _verify_audited_inputs(self, package: Path, *, launchers_from_commit: bool) -> None:
        self._verify_manifest()
        if not hmac.compare_digest(
            file_sha256(self.package_hash_list), self.spec.package_hash_list_sha256.lower(),
        ):
            raise CutoverError("package hash-list digest mismatch")
        count, total, digest = verify_package_inventory(package, self.package_hash_list)
        if count != self.spec.package_file_count or total != self.spec.package_bytes:
            raise CutoverError("package count/byte identity mismatch")
        if not hmac.compare_digest(digest, self.spec.package_tree_sha256.lower()):
            raise CutoverError("package tree digest mismatch")
        if not hmac.compare_digest(
            file_sha256(self.candidate_cmd), self.spec.candidate_cmd_sha256.lower(),
        ):
            raise CutoverError("candidate CMD digest mismatch")
        launcher_expectations = (
            ("launch-hermes.ps1", self.spec.launcher_ps1_blob_sha256, self.spec.launcher_ps1_sha256),
            ("launch-hermes.vbs", self.spec.launcher_vbs_blob_sha256, self.spec.launcher_vbs_sha256),
            (
                "scripts/native_runtime_cli_launcher.py",
                self.spec.cli_launcher_blob_sha256,
                self.spec.cli_launcher_sha256,
            ),
        )
        for relative, blob_expected, checkout_expected in launcher_expectations:
            expected = (blob_expected if launchers_from_commit else checkout_expected).lower()
            observed = (
                hashlib.sha256(_git_blob(self.repo, self.spec.release_commit, relative)).hexdigest()
                if launchers_from_commit else file_sha256(self.repo / relative)
            )
            if not hmac.compare_digest(observed, expected):
                raise CutoverError(f"launcher digest mismatch: {relative}")
        try:
            stamp = json.loads((package / "resources" / "install-stamp.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CutoverError("package install stamp is unreadable") from exc
        if stamp.get("commit") != self.spec.release_commit or stamp.get("dirty") is not False:
            raise CutoverError("package install stamp does not bind exact clean release")

    def validate_readonly(self) -> None:
        """Validate every audited input without creating a guard, journal or Git mutation."""
        if self.rollback_package.exists() or not self.stage_package.is_dir() or not self.candidate_cmd.is_file():
            raise CutoverError("package/CMD preconditions are invalid")
        if len({self.live_package, self.stage_package, self.rollback_package}) != 3:
            raise CutoverError("package paths must be distinct")
        if os.path.splitdrive(str(self.live_package))[0].lower() != os.path.splitdrive(str(self.stage_package))[0].lower():
            raise CutoverError("live and stage packages must share a volume")
        self._verify_live_package_preimage()
        self._verify_source_preconditions()
        self._verify_audited_inputs(self.stage_package, launchers_from_commit=True)

    def _ensure_hold(self) -> None:
        expected = self._hold_bytes(
            self.spec.transaction_id, self.spec.base_commit, self.spec.release_commit,
        )
        if _path_lexists(self.hold):
            if _is_reparse_path(self.hold) or not stat.S_ISREG(self.hold.lstat().st_mode):
                raise CutoverError("CUTOVER hold must be an ordinary non-reparse file")
            if self.hold.read_bytes() != expected:
                raise CutoverError("unknown CUTOVER hold exists")
            if self.enforce_acl:
                _assert_exact_acl(
                    self.acl_backend.read(self.hold), self.acl_backend.current_user_sid(), 0,
                )
            return
        self.hold.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.hold.open("xb") as handle:
                handle.write(expected)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            raise CutoverError("CUTOVER hold appeared concurrently")
        if self.enforce_acl:
            user_sid = self.acl_backend.current_user_sid()
            self.acl_backend.apply_exact(self.hold, user_sid)
            _assert_exact_acl(self.acl_backend.read(self.hold), user_sid, 0)
        if self.hold.read_bytes() != expected or _is_reparse_path(self.hold):
            raise CutoverError("CUTOVER hold readback failed")

    def _verify_live_package_preimage(self) -> None:
        count, total, digest = ordinary_directory_identity(self.live_package)
        if count != self.spec.live_package_file_count or total != self.spec.live_package_bytes:
            raise CutoverError("live package count/byte identity mismatch")
        if not hmac.compare_digest(digest, self.spec.live_package_tree_sha256.lower()):
            raise CutoverError("live package tree identity mismatch")

    def _journal(self) -> dict:
        if not self.journal_path.exists():
            return {"version": 1, "transaction_id": self.spec.transaction_id, "phase": None}
        data = json.loads(self.journal_path.read_text(encoding="utf-8"))
        if data.get("transaction_id") != self.spec.transaction_id or data.get("phase") not in (None, *PHASES):
            raise CutoverError("journal identity/state is invalid")
        return data

    def _write_journal(self, phase: str, **extra: object) -> None:
        data = self._journal()
        current = data.get("phase")
        allowed: dict[str | None, set[str]] = {
            None: {"PREPARED_DETACHED"},
            **{
                FORWARD_PHASES[index]: {FORWARD_PHASES[index + 1], "ROLLBACK_INTENT"}
                for index in range(len(FORWARD_PHASES) - 1)
            },
            "ACTIVATED": {"ROLLBACK_INTENT"},
            **{
                ROLLBACK_PHASES[index]: {ROLLBACK_PHASES[index + 1]}
                for index in range(len(ROLLBACK_PHASES) - 1)
            },
            "ROLLED_BACK_DETACHED_BASE": set(),
        }
        allowed["ROLLBACK_BINDINGS_RESTORED"].add("ROLLBACK_MAIN_BASE")
        if phase != current and phase not in allowed.get(current, set()):
            raise CutoverError(f"invalid journal transition: {current!r} -> {phase!r}")
        data.update(phase=phase, **extra)
        _atomic_write(self.journal_path, _json_bytes(data))

    def _lock_guard_acl(self, directory: Path) -> None:
        if not self.enforce_acl:
            return
        user_sid = self.acl_backend.current_user_sid()
        self.acl_backend.apply_exact(directory, user_sid)
        self._verify_guard_acl(directory)

    def _verify_guard_acl(self, directory: Path) -> bool:
        if not self.enforce_acl:
            return True
        user_sid = self.acl_backend.current_user_sid()
        assert_exact_guard_acl(self.acl_backend.read(directory), user_sid)
        return True

    def _guard_interruption(self, boundary: str) -> None:
        if self.guard_fail_after == boundary:
            raise CutoverError(f"synthetic guard interruption after {boundary}")

    def _guard_owner(self, directory: Path) -> dict:
        try:
            owner = json.loads((directory / "ownership.json").read_text(encoding="utf-8"))
            protected = base64.b64decode(owner["protected_token"], validate=True)
            token = self.protector.unprotect(protected)
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CutoverError("guard ownership receipt is invalid") from exc
        if (
            owner.get("version") != 1
            or owner.get("transaction_id") != self.spec.transaction_id
            or owner.get("guard_name") != self.guard_dir.name
            or directory.name not in {owner.get("staging_name"), owner.get("guard_name")}
            or not hmac.compare_digest(hashlib.sha256(token).hexdigest(), str(owner.get("token_sha256", "")))
        ):
            raise CutoverError("guard ownership receipt does not match this transaction")
        return owner

    def _validate_guard_shape(self, directory: Path, *, staging: bool) -> None:
        if not directory.exists() or _is_reparse_path(directory) or not stat.S_ISDIR(directory.lstat().st_mode):
            raise CutoverError("guard root must be an ordinary non-reparse directory")
        self._verify_guard_acl(directory)
        entries = list(directory.iterdir())
        names = {entry.name for entry in entries}
        published = {"ownership.json", "auth.dpapi", "cmd.dpapi", "hmac-key.dpapi", "guard.json"}
        prefixes = (
            {"ownership.json"},
            {"ownership.json", "auth.dpapi"},
            {"ownership.json", "auth.dpapi", "cmd.dpapi"},
            {"ownership.json", "auth.dpapi", "cmd.dpapi", "hmac-key.dpapi"},
            published,
        )
        allowed_shapes = prefixes if staging else (published,)
        if names not in allowed_shapes:
            raise CutoverError("private guard is incomplete or has an unexpected shape")
        for entry in entries:
            if _is_reparse_path(entry) or not stat.S_ISREG(entry.lstat().st_mode):
                raise CutoverError("guard child must be a regular non-reparse file")
        self._guard_owner(directory)

    def _owned_staging_from_receipt(self) -> Path | None:
        if not _path_lexists(self.guard_staging_receipt):
            return None
        if _is_reparse_path(self.guard_staging_receipt) or not self.guard_staging_receipt.is_file():
            raise CutoverError("guard staging receipt must be an ordinary file")
        try:
            receipt = json.loads(self.guard_staging_receipt.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CutoverError("guard staging receipt is unreadable") from exc
        name = receipt.get("staging_name")
        if (
            receipt.get("version") != 1
            or receipt.get("transaction_id") != self.spec.transaction_id
            or not isinstance(name, str)
            or Path(name).name != name
            or not name.startswith(f".{self.guard_dir.name}.{self.spec.transaction_id}.")
            or not name.endswith(".preparing")
        ):
            raise CutoverError("guard staging receipt identity is invalid")
        directory = self.guard_dir.parent / name
        if not _path_lexists(directory) and _path_lexists(self.guard_dir):
            self._validate_guard_shape(self.guard_dir, staging=False)
            owner = self._guard_owner(self.guard_dir)
            if not hmac.compare_digest(
                str(owner.get("token_sha256", "")), str(receipt.get("token_sha256", ""))
            ):
                raise CutoverError("published guard staging receipt token mismatch")
            self.guard_staging_receipt.unlink()
            return None
        self._validate_guard_shape(directory, staging=True)
        owner = self._guard_owner(directory)
        if not hmac.compare_digest(str(owner.get("token_sha256", "")), str(receipt.get("token_sha256", ""))):
            raise CutoverError("guard staging receipt token mismatch")
        self.guard_staging_dir = directory
        return directory

    def _remove_transaction_staging_guard(self) -> None:
        directory = self._owned_staging_from_receipt()
        if directory is None:
            return
        if directory.parent != self.guard_dir.parent:
            raise CutoverError("guard staging path escaped its parent")
        shutil.rmtree(directory)
        self.guard_staging_receipt.unlink()

    def _clear_published_staging_receipt(self) -> None:
        if not _path_lexists(self.guard_staging_receipt):
            return
        try:
            receipt = json.loads(self.guard_staging_receipt.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CutoverError("guard staging receipt is unreadable") from exc
        owner = self._guard_owner(self.guard_dir)
        if (
            receipt.get("transaction_id") != self.spec.transaction_id
            or not hmac.compare_digest(str(receipt.get("token_sha256", "")), str(owner.get("token_sha256", "")))
        ):
            raise CutoverError("published guard staging receipt mismatch")
        self.guard_staging_receipt.unlink()

    def _remove_published_guard_if_owned(self) -> None:
        if not _path_lexists(self.guard_dir):
            return
        if _is_reparse_path(self.guard_dir):
            raise CutoverError("published guard path is a reparse point")
        _, auth, cmd = self._guard()
        if not hmac.compare_digest(auth, self.auth_file.read_bytes()):
            raise CutoverError("published guard auth preimage does not match live auth")
        if not hmac.compare_digest(cmd, self.live_cmd.read_bytes()):
            raise CutoverError("published guard CMD preimage does not match live CMD")
        shutil.rmtree(self.guard_dir)

    def _create_private_guard(self) -> dict:
        if not self.auth_file.is_file() or not self.live_cmd.is_file():
            raise CutoverError("protected auth or CMD preimage is missing")
        if _path_lexists(self.legacy_guard_staging_dir):
            raise CutoverError(
                "unowned deterministic guard staging path exists; retain HOLD for manual inspection"
            )
        if _path_lexists(self.guard_dir):
            metadata, auth, cmd = self._guard_at(self.guard_dir)
            if not hmac.compare_digest(auth, self.auth_file.read_bytes()):
                raise CutoverError("published guard auth preimage does not match live auth")
            if not hmac.compare_digest(cmd, self.live_cmd.read_bytes()):
                raise CutoverError("published guard CMD preimage does not match live CMD")
            self._clear_published_staging_receipt()
            return metadata

        # Clean only the random staging leaf positively named by a durable,
        # DPAPI-verifiable ownership receipt.  Unreferenced/unknown directories
        # are never touched and cannot block a fresh random attempt.
        self._remove_transaction_staging_guard()
        self.guard_dir.parent.mkdir(parents=True, exist_ok=True)
        while True:
            candidate = self.guard_dir.with_name(
                f".{self.guard_dir.name}.{self.spec.transaction_id}.{secrets.token_hex(16)}.preparing"
            )
            try:
                candidate.mkdir(exist_ok=False)
                break
            except FileExistsError:
                continue
        self.guard_staging_dir = candidate
        self._guard_interruption("directory")
        self._lock_guard_acl(self.guard_staging_dir)
        self._guard_interruption("acl")
        auth = self.auth_file.read_bytes()
        cmd = self.live_cmd.read_bytes()
        key = secrets.token_bytes(32)
        token = secrets.token_bytes(32)
        owner = {
            "version": 1,
            "transaction_id": self.spec.transaction_id,
            "guard_name": self.guard_dir.name,
            "staging_name": self.guard_staging_dir.name,
            "token_sha256": hashlib.sha256(token).hexdigest(),
            "protected_token": base64.b64encode(self.protector.protect(token)).decode("ascii"),
        }
        _atomic_write(self.guard_staging_dir / "ownership.json", _json_bytes(owner))
        self._guard_interruption("ownership")
        _atomic_write(self.guard_staging_receipt, _json_bytes({
            "version": 1,
            "transaction_id": self.spec.transaction_id,
            "staging_name": self.guard_staging_dir.name,
            "token_sha256": owner["token_sha256"],
        }))
        self._guard_interruption("receipt")
        _atomic_write(self.guard_staging_dir / "auth.dpapi", self.protector.protect(auth))
        self._guard_interruption("auth")
        _atomic_write(self.guard_staging_dir / "cmd.dpapi", self.protector.protect(cmd))
        self._guard_interruption("cmd")
        _atomic_write(self.guard_staging_dir / "hmac-key.dpapi", self.protector.protect(key))
        self._guard_interruption("key")
        metadata = {
            "version": 1,
            "transaction_id": self.spec.transaction_id,
            "auth_length": len(auth),
            "cmd_length": len(cmd),
            "auth_hmac": hmac.new(key, auth, hashlib.sha256).hexdigest(),
            "cmd_hmac": hmac.new(key, cmd, hashlib.sha256).hexdigest(),
        }
        _atomic_write(self.guard_staging_dir / "guard.json", _json_bytes(metadata))
        self._guard_interruption("metadata")
        self._guard_at(self.guard_staging_dir)
        os.replace(self.guard_staging_dir, self.guard_dir)
        self._guard_interruption("publication")
        self._guard_at(self.guard_dir)
        self._clear_published_staging_receipt()
        return metadata

    def _guard_at(self, directory: Path) -> tuple[dict, bytes, bytes]:
        self._validate_guard_shape(directory, staging=False)
        try:
            metadata = json.loads((directory / "guard.json").read_text(encoding="utf-8"))
            key = self.protector.unprotect((directory / "hmac-key.dpapi").read_bytes())
            auth = self.protector.unprotect((directory / "auth.dpapi").read_bytes())
            cmd = self.protector.unprotect((directory / "cmd.dpapi").read_bytes())
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CutoverError("private guard is incomplete or unreadable") from exc
        if metadata.get("transaction_id") != self.spec.transaction_id:
            raise CutoverError("private guard transaction identity mismatch")
        if len(auth) != metadata["auth_length"] or not hmac.compare_digest(
            hmac.new(key, auth, hashlib.sha256).hexdigest(), metadata["auth_hmac"]
        ):
            raise CutoverError("private auth guard integrity failed")
        if len(cmd) != metadata["cmd_length"] or not hmac.compare_digest(
            hmac.new(key, cmd, hashlib.sha256).hexdigest(), metadata["cmd_hmac"]
        ):
            raise CutoverError("private CMD guard integrity failed")
        return metadata, auth, cmd

    def _guard(self) -> tuple[dict, bytes, bytes]:
        return self._guard_at(self.guard_dir)

    def _verify_auth_equal(self) -> None:
        _, expected, _ = self._guard()
        if not hmac.compare_digest(expected, self.auth_file.read_bytes()):
            raise CutoverError("protected auth bytes changed")

    def _prepare(self) -> None:
        self.validate_readonly()
        self._handover_predecessor_hold()
        self._ensure_hold()
        self.validate_readonly()
        self._create_private_guard()
        head_preimage = self._direct_head_preimage()
        self._write_journal(
            "PREPARED_DETACHED", stage_digest=self.spec.package_tree_sha256.lower(), auth_equal=True,
            manifest_sha256=self.spec.manifest_sha256.lower(), release_commit=self.spec.release_commit,
            guard_acl_pass=True, raw_head_sha256=hashlib.sha256(head_preimage).hexdigest(),
        )

    def _head_attached_base(self) -> None:
        if self._source_state() == "F1":
            if "sakaan R7 cutover: attach canonical BASE to main" not in _git(
                self.repo, "reflog", "-1", "--format=%gs", "HEAD"
            ):
                raise CutoverError("attach reflog receipt is missing")
            self._write_journal("HEAD_ATTACHED_BASE")
            return
        self._require_source_state("F0")
        raw = self._direct_head_preimage()
        if hashlib.sha256(raw).hexdigest() != self._journal().get("raw_head_sha256"):
            raise CutoverError("canonical raw HEAD changed after preparation")
        if self._direct_head_preimage() != raw:
            raise CutoverError("canonical raw HEAD changed before attach")
        _git_stdin(self.repo, """start
symref-update HEAD refs/heads/main oid {base}
prepare
commit
""".format(base=self.spec.base_commit), message="sakaan R7 cutover: attach canonical BASE to main")
        self._require_source_state("F1")
        if "sakaan R7 cutover: attach canonical BASE to main" not in _git(
            self.repo, "reflog", "-1", "--format=%gs", "HEAD"
        ):
            # Some Git versions ignore -m for stdin transactions unless option is supplied.
            # The topology postcondition remains safe, but the audit contract requires the receipt.
            raise CutoverError("attach reflog receipt is missing")
        self._write_journal("HEAD_ATTACHED_BASE")

    def _source_tree_prepared(self) -> None:
        try:
            state = self._source_state()
        except CutoverError as exc:
            raise CutoverError(f"LATE_SOURCE_DIRTINESS_HOLD: {exc}") from exc
        if state == "F2":
            self._write_journal("SOURCE_TREE_PREPARED")
            return
        self._verify_live_package_preimage()
        self._require_source_state("F1", late=True)
        _git(self.repo, "read-tree", "--reset", "-u", self.spec.release_commit)
        self._require_source_state("F2")
        self._write_journal("SOURCE_TREE_PREPARED")

    def _source_promoted(self) -> None:
        if self._source_state() == "F3":
            if "sakaan R7 cutover: CAS main BASE to R7" not in _git(
                self.repo, "reflog", "-1", "--format=%gs", "refs/heads/main"
            ):
                raise CutoverError("promotion reflog receipt is missing")
            self._write_journal("SOURCE_PROMOTED")
            return
        self._require_source_state("F2", late=True)
        _git_stdin(self.repo, """start
update refs/heads/main {release} {base}
prepare
commit
""".format(release=self.spec.release_commit, base=self.spec.base_commit), message="sakaan R7 cutover: CAS main BASE to R7")
        self._require_source_state("F3")
        if "sakaan R7 cutover: CAS main BASE to R7" not in _git(
            self.repo, "reflog", "-1", "--format=%gs", "refs/heads/main"
        ):
            raise CutoverError("promotion reflog receipt is missing")
        self._write_journal("SOURCE_PROMOTED")

    def _old_package_quarantined(self) -> None:
        if self.live_package.exists() and not self.rollback_package.exists():
            self.rollback_package.parent.mkdir(parents=True, exist_ok=True)
            os.replace(self.live_package, self.rollback_package)
        if self.live_package.exists() or not self.rollback_package.is_dir():
            raise CutoverError("old package quarantine is ambiguous")
        self._write_journal("OLD_PACKAGE_QUARANTINED")

    def _package_promoted(self) -> None:
        expected = self._journal()["stage_digest"]
        if self.stage_package.exists() and not self.live_package.exists():
            self.live_package.parent.mkdir(parents=True, exist_ok=True)
            os.replace(self.stage_package, self.live_package)
        if not self.live_package.is_dir() or tree_digest(self.live_package) != expected:
            raise CutoverError("promoted package identity is invalid")
        self._write_journal("PACKAGE_PROMOTED")

    def _bindings_verified(self) -> None:
        # Re-bind every audited input immediately before the first binding write.
        self._verify_audited_inputs(self.live_package, launchers_from_commit=False)
        desired = self.candidate_cmd.read_bytes()
        if self.live_cmd.read_bytes() != desired:
            _atomic_write(self.live_cmd, desired)
        if self.live_cmd.read_bytes() != desired:
            raise CutoverError("CMD promotion readback failed")
        self._write_journal("BINDINGS_VERIFIED")

    def _tuple_verified(self) -> None:
        self._verify_release_tuple()
        self._write_journal("TUPLE_VERIFIED", auth_equal=True)

    def _verify_release_tuple(self) -> None:
        data = self._journal()
        self._verify_audited_inputs(self.live_package, launchers_from_commit=False)
        if data.get("manifest_sha256") != self.spec.manifest_sha256.lower():
            raise CutoverError("journal manifest identity mismatch")
        if data.get("release_commit") != self.spec.release_commit:
            raise CutoverError("journal release identity mismatch")
        if _git(self.repo, "rev-parse", "HEAD") != self.spec.release_commit:
            raise CutoverError("source tuple does not equal R")
        if _git(self.repo, "status", "--porcelain"):
            raise CutoverError("source tuple is dirty")
        if tree_digest(self.live_package) != data.get("stage_digest"):
            raise CutoverError("package tuple does not equal R")
        if self.live_cmd.read_bytes() != self.candidate_cmd.read_bytes():
            raise CutoverError("binding tuple does not equal R")
        self._verify_auth_equal()

    def _activation_intent(self) -> None:
        if self._journal()["phase"] != "TUPLE_VERIFIED":
            raise CutoverError("activation intent requires TUPLE_VERIFIED")
        self._verify_release_tuple()
        # This durable state is written before the launch hold can disappear.
        self._write_journal("ACTIVATION_INTENT", auth_equal=True)

    def _activate(self) -> None:
        if self._journal()["phase"] != "ACTIVATION_INTENT":
            raise CutoverError("activation requires durable ACTIVATION_INTENT")
        # If process loss happened after unlink, atomically restore HOLD before
        # any operation that can fail.  The tuple is always revalidated held.
        if not self.hold.exists():
            self._ensure_hold()
        self._verify_release_tuple()
        self.hold.unlink()
        try:
            self._write_journal("ACTIVATED", auth_equal=True)
        except Exception:
            # A journal failure after unlink must never return launchable.  If
            # the journal actually committed despite the exception, absence of
            # HOLD is already paired with durable ACTIVATED and is safe.
            try:
                activated = self._journal().get("phase") == "ACTIVATED"
            except Exception:
                activated = False
            if not activated and not self.hold.exists():
                self._ensure_hold()
            raise

    def run(self, *, fail_after: str | None = None) -> str:
        actions = {
            "PREPARED_DETACHED": self._prepare,
            "HEAD_ATTACHED_BASE": self._head_attached_base,
            "SOURCE_TREE_PREPARED": self._source_tree_prepared,
            "SOURCE_PROMOTED": self._source_promoted,
            "OLD_PACKAGE_QUARANTINED": self._old_package_quarantined,
            "PACKAGE_PROMOTED": self._package_promoted,
            "BINDINGS_VERIFIED": self._bindings_verified,
            "TUPLE_VERIFIED": self._tuple_verified,
            "ACTIVATION_INTENT": self._activation_intent,
            "ACTIVATED": self._activate,
        }
        current = self._journal()["phase"]
        if current in ROLLBACK_PHASES:
            raise CutoverError("rollback intent forbids forward execution")
        if current == "ACTIVATED":
            return "ACTIVATED"
        if current is not None:
            # A resumed transaction must re-establish the private-guard
            # boundary before any source/package/binding mutation.
            self._validate_guard_shape(self.guard_dir, staging=False)
            self._ensure_hold()
        start = 0 if current is None else FORWARD_PHASES.index(current) + 1
        for phase in FORWARD_PHASES[start:]:
            actions[phase]()
            if fail_after == phase:
                raise CutoverError(f"synthetic interruption after {phase}")
        return self._journal()["phase"]

    def rollback(self, *, fail_after: str | None = None) -> str:
        def record(next_phase: str, **extra: object) -> None:
            self._write_journal(next_phase, **extra)
            if fail_after == next_phase:
                raise CutoverError(f"synthetic rollback interruption after {next_phase}")

        data = self._journal()
        phase = data["phase"]
        if phase is None:
            self._ensure_hold()
            if _path_lexists(self.legacy_guard_staging_dir):
                raise CutoverError(
                    "unowned deterministic guard staging path exists; retain HOLD for manual inspection"
                )
            self._remove_transaction_staging_guard()
            self._remove_published_guard_if_owned()
            return "BASE"
        self._ensure_hold()
        self._validate_guard_shape(self.guard_dir, staging=False)
        if phase not in ROLLBACK_PHASES:
            source_state = self._source_state()
            if source_state not in {"F0", "F1", "F2", "F3"}:
                raise CutoverError("rollback source origin is ambiguous")
            record(
                "ROLLBACK_INTENT", rollback_origin_phase=phase,
                rollback_origin_state=source_state, rollback_target="detached-base",
            )
            phase = "ROLLBACK_INTENT"

        if phase == "ROLLED_BACK_DETACHED_BASE":
            return "BASE"

        # Package-first inverse. Keep R staged for diagnosis.
        if self.rollback_package.exists():
            if self.live_package.exists():
                if self.stage_package.exists():
                    raise CutoverError("cannot preserve promoted package: stage path occupied")
                os.replace(self.live_package, self.stage_package)
            os.replace(self.rollback_package, self.live_package)
        self._verify_live_package_preimage()
        if phase == "ROLLBACK_INTENT":
            record("ROLLBACK_PACKAGE_RESTORED")
            phase = "ROLLBACK_PACKAGE_RESTORED"

        _, auth_preimage, cmd_preimage = self._guard()
        if self.live_cmd.read_bytes() != cmd_preimage:
            _atomic_write(self.live_cmd, cmd_preimage)
        # This transaction never writes auth.  Restore only if an explicitly
        # recorded transaction-owned auth mutation exists.
        if data.get("auth_changed_by_transaction") is True and self.auth_file.read_bytes() != auth_preimage:
            _atomic_write(self.auth_file, auth_preimage)
        if phase == "ROLLBACK_PACKAGE_RESTORED":
            if self.live_cmd.read_bytes() != cmd_preimage:
                raise CutoverError("rollback CMD readback failed")
            record("ROLLBACK_BINDINGS_RESTORED")
            phase = "ROLLBACK_BINDINGS_RESTORED"

        state = self._source_state()
        if phase == "ROLLBACK_BINDINGS_RESTORED":
            if state == "F3":
                self._require_source_state("F3", late=True)
                _git(self.repo, "read-tree", "--reset", "-u", self.spec.base_commit)
                self._require_source_state("R1")
                record("ROLLBACK_SOURCE_BASE_STAGED")
                phase = "ROLLBACK_SOURCE_BASE_STAGED"
            elif state == "R1":
                record("ROLLBACK_SOURCE_BASE_STAGED")
                phase = "ROLLBACK_SOURCE_BASE_STAGED"
            elif state == "F2":
                self._require_source_state("F2", late=True)
                _git(self.repo, "read-tree", "--reset", "-u", self.spec.base_commit)
                self._require_source_state("F1")
                record("ROLLBACK_MAIN_BASE")
                phase = "ROLLBACK_MAIN_BASE"
            elif state == "F1":
                record("ROLLBACK_MAIN_BASE")
                phase = "ROLLBACK_MAIN_BASE"
            elif state == "F0":
                record("ROLLBACK_MAIN_BASE")
                phase = "ROLLBACK_MAIN_BASE"
            else:
                raise CutoverError("rollback source state is ambiguous")

        if phase == "ROLLBACK_SOURCE_BASE_STAGED":
            state = self._source_state()
            if state == "R1":
                self._require_source_state("R1", late=True)
                _git_stdin(self.repo, """start
update refs/heads/main {base} {release}
prepare
commit
""".format(base=self.spec.base_commit, release=self.spec.release_commit), message="sakaan R7 cutover rollback: CAS main R7 to BASE")
            elif state != "F1":
                raise CutoverError(f"LATE_SOURCE_DIRTINESS_HOLD: expected R1 or F1, got {state}")
            self._require_source_state("F1")
            if "sakaan R7 cutover rollback: CAS main R7 to BASE" not in _git(
                self.repo, "reflog", "-1", "--format=%gs", "refs/heads/main"
            ):
                raise CutoverError("rollback main reflog receipt is missing")
            record("ROLLBACK_MAIN_BASE")
            phase = "ROLLBACK_MAIN_BASE"

        if phase == "ROLLBACK_MAIN_BASE":
            state = self._source_state()
            if state == "F1":
                journal = self._journal()
                if "rollback_detach_reflog_preimage_sha256" not in journal:
                    rows = _git(self.repo, "reflog", "show", "--format=%H%x00%gs", "HEAD").splitlines()
                    self._write_journal(
                        "ROLLBACK_MAIN_BASE",
                        rollback_detach_reflog_preimage_sha256=hashlib.sha256(_json_bytes(rows)).hexdigest(),
                        rollback_detach_reflog_preimage_count=len(rows),
                    )
                _git_with_env(
                    self.repo,
                    {"GIT_REFLOG_ACTION": _ROLLBACK_DETACH_REFLOG_ACTION},
                    "switch", "--detach", "--no-guess", self.spec.base_commit,
                )
            elif state != "F0":
                raise CutoverError("rollback detach precondition is ambiguous")
            self._require_source_state("F0")
            journal = self._journal()
            receipt_keys = {
                "rollback_detach_reflog_preimage_sha256", "rollback_detach_reflog_preimage_count",
            }
            present_receipt_keys = receipt_keys.intersection(journal)
            if present_receipt_keys and present_receipt_keys != receipt_keys:
                raise CutoverError("rollback detach reflog preimage receipt is incomplete")
            receipt_present = present_receipt_keys == receipt_keys
            if not receipt_present:
                # The rollback origin was already detached BASE, so there was no
                # detach action and therefore no required reflog append.
                record("ROLLBACK_HEAD_DETACHED_BASE")
                phase = "ROLLBACK_HEAD_DETACHED_BASE"
            else:
                try:
                    expected_count = int(journal["rollback_detach_reflog_preimage_count"])
                    expected_preimage = str(journal["rollback_detach_reflog_preimage_sha256"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise CutoverError("rollback detach reflog preimage receipt is missing") from exc
                rows = _git(self.repo, "reflog", "show", "--format=%H%x00%gs", "HEAD").splitlines()
                expected_subject = _ROLLBACK_DETACH_REFLOG_ACTION
                if (
                    len(rows) != expected_count + 1
                    or rows[0] != f"{self.spec.base_commit}\x00{expected_subject}"
                    or hashlib.sha256(_json_bytes(rows[1:])).hexdigest() != expected_preimage
                ):
                    raise CutoverError(
                        "rollback detach HEAD reflog append receipt is invalid: "
                        f"top={rows[0] if rows else '<missing>'!r}"
                    )
                record("ROLLBACK_HEAD_DETACHED_BASE")
                phase = "ROLLBACK_HEAD_DETACHED_BASE"

        if phase == "ROLLBACK_HEAD_DETACHED_BASE":
            self._verify_auth_equal()
            self._verify_live_package_preimage()
            if self.live_cmd.read_bytes() != cmd_preimage:
                raise CutoverError("rollback protected CMD differs")
            self._require_source_state("F0")
            record("ROLLBACK_PROTECTED_STATE_VERIFIED", auth_equal=True)
            phase = "ROLLBACK_PROTECTED_STATE_VERIFIED"

        if phase == "ROLLBACK_PROTECTED_STATE_VERIFIED":
            self._require_source_state("F0")
            record("ROLLED_BACK_DETACHED_BASE", auth_equal=True)

        self._verify_auth_equal()
        return "BASE"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--rollback", action="store_true")
    args = parser.parse_args()
    try:
        spec = CutoverSpec.load(args.spec)
        transaction = NativeRuntimeCutover(spec, protector=WindowsDpapiProtector())
        if not args.execute:
            transaction.validate_readonly()
            print(json.dumps({"status": "VALIDATED_ONLY", "transaction_id": spec.transaction_id}))
            return 0
        result = transaction.rollback() if args.rollback else transaction.run()
        print(json.dumps({"status": result, "transaction_id": spec.transaction_id}))
        return 0
    except (CutoverError, KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "INVALID", "error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
