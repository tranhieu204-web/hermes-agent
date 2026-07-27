"""Native Windows-safe fleet usage refresh (no WSL dependency).

Writes the profile-scoped capacity document under Hermes home with atomic
replace semantics. Auto-queryable lanes (Codex/Claude) fetch weekly used % from
provider APIs. Console-only Grok/Antigravity never invent a usage percentage,
and their live health probes update only separate health fields while
preserving prior usage evidence and its timestamp.
"""

from __future__ import annotations

import copy
import json
import math
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from utils import atomic_replace

from .live_capacity import RELEVANT_WEEKLY_WINDOWS, weekly_used_percents
from .usage_paths import COCKPIT_MIRROR_PATH, default_native_usage_path, resolve_usage_path

SCHEMA_VERSION = "plans-1"
AUTO_LANES = (
    ("chatgpt_codex", "openai-codex", "ChatGPT Pro · Codex", ("chatgpt", "codex")),
    ("claude_code", "anthropic", "Claude Max 20x", ("claude", "max")),
)
CONSOLE_ONLY_LANES = (
    ("grok", "SuperGrok", ("supergrok", "grok")),
    ("antigravity", "Google AI · Antigravity", ("antigravity", "google ai")),
)


def _console_attestation_path(*, home: Path | None = None) -> Path:
    from hermes_constants import get_hermes_home

    base = Path(home) if home is not None else get_hermes_home()
    return (base / "fleet" / "usage-console-attestation.json").resolve()


def _read_console_attestation(path: Path) -> dict[str, tuple[float, str]]:
    """Read timestamped weekly usage evidence for console-only lanes.

    A lane-level or document-level ``checked_at`` wins. For compatibility with
    the original percentage-only schema, the file modification time is the
    evidence timestamp; it ages naturally instead of being re-stamped by each
    refresh.
    """
    try:
        raw = path.read_text(encoding="utf-8")
        file_checked_at = _iso(
            datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        )
    except OSError:
        return {}
    try:
        document = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    lanes = document.get("lanes") if isinstance(document, dict) else None
    if not isinstance(lanes, dict):
        return {}
    document_checked_at = document.get("checked_at")
    out: dict[str, tuple[float, str]] = {}
    for lane_id, row in lanes.items():
        if not isinstance(row, dict):
            continue
        pct = _clamp_pct(row.get("weekly_pct_used"))
        checked_at = row.get("checked_at") or document_checked_at or file_checked_at
        if pct is None or not isinstance(checked_at, str):
            continue
        try:
            parsed = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            continue
        out[str(lane_id)] = (pct, _iso(parsed))
    return out


def _is_attestation_stale(checked_at_str: str | None, *, max_age_hours: int = 24, now: datetime | None = None) -> bool:
    """Check if an attestation timestamp is stale.

    Args:
        checked_at_str: ISO format timestamp string or None
        max_age_hours: Maximum age in hours before considered stale
        now: Reference time (defaults to current UTC time)

    Returns:
        True if stale or unparseable; False if fresh
    """
    if not checked_at_str or not isinstance(checked_at_str, str):
        return True
    try:
        attested_at = datetime.fromisoformat(checked_at_str.replace("Z", "+00:00"))
        age = (now or _utc_now()) - attested_at
        return age > timedelta(hours=max_age_hours)
    except (ValueError, TypeError):
        return True


def no_console_creationflags() -> int:
    """subprocess creationflags that keep a console probe off the screen.

    On Windows a console-subsystem child (``agy.exe``) allocates its own
    console when the parent has none — the gateway runs under ``pythonw.exe``
    — so every probe flashes a terminal on the operator's screen. The
    pre-dispatch usage refresh runs on each ~60s dispatcher tick, which made
    the flash continuous (reported by the operator 2026-07-26).

    ``CREATE_NO_WINDOW`` suppresses it without affecting stdout/stderr capture.
    Returns 0 on non-Windows, where the flag does not exist.
    """
    import subprocess

    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _resolve_agy_executable() -> str | None:
    """Resolve absolute path to agy executable.

    Checks:
    1. Known installation path: C:/Users/HieuKa/AppData/Local/agy/bin/agy.exe
    2. On PATH via shutil.which("agy")

    Returns absolute path or None if not found.
    """
    import shutil
    from pathlib import Path

    # Known installation location (from COO diagnostic)
    known_path = Path("C:/Users/HieuKa/AppData/Local/agy/bin/agy.exe").resolve()
    if known_path.is_file():
        return str(known_path)

    # Fallback to PATH
    agy_bin = shutil.which("agy")
    return agy_bin


def _probe_agy_health() -> tuple[bool | None, str]:
    """Probe agy CLI health by querying installed models.

    Surface real subprocess errors (file-not-found vs exit code).
    Tolerant of catalog evolution (unknown models don't fail the probe).

    Returns:
        (True, detail) if agy is healthy and model list parsed
        (False, detail) if agy runs but models unavailable
        (None, detail) if agy not found or probe failed
    """
    import subprocess

    agy_exe = _resolve_agy_executable()
    if not agy_exe:
        return None, "agy executable not found on PATH or known location"

    try:
        completed = subprocess.run(
            [agy_exe, "models"],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
            creationflags=no_console_creationflags(),
        )
    except FileNotFoundError:
        return None, f"agy executable not found: {agy_exe}"
    except subprocess.TimeoutExpired:
        return None, f"agy models command timeout (5s): {agy_exe}"
    except OSError as exc:
        return None, f"agy executable launch failed: {type(exc).__name__}: {exc}"

    if completed.returncode != 0:
        stderr_detail = (
            completed.stderr[:100].strip() if completed.stderr else ""
        )
        return (
            False,
            f"agy models exit {completed.returncode}: {stderr_detail or '(no stderr)'}",
        )

    # Check that output contains expected model markers (tolerant of catalog changes)
    output_lower = (completed.stdout or "").lower()
    if not output_lower or "gemini" not in output_lower:
        return False, "agy models output missing model list"

    return True, f"agy {agy_exe} healthy; model list available"


def _probe_console_lane_health(lane_id: str) -> tuple[bool | None, str]:
    """Return (healthy, detail) without inventing usage percentages.

    Returns:
        (True, detail) if healthy
        (False, detail) if unhealthy
        (None, detail) if unavailable to probe
    """
    if lane_id == "grok":
        try:
            from hermes_cli.auth import get_xai_oauth_auth_status

            status = get_xai_oauth_auth_status() or {}
        except Exception as exc:  # noqa: BLE001 - probe must fail closed
            return None, f"xai-oauth status failed: {type(exc).__name__}"
        if bool(status.get("logged_in")):
            return True, "xai-oauth logged_in"
        return False, "xai-oauth not logged_in"
    if lane_id == "antigravity":
        try:
            return _probe_agy_health()
        except Exception as exc:  # noqa: BLE001
            return None, f"agy probe failed: {type(exc).__name__}: {exc}"
    return None, f"unsupported console lane: {lane_id}"




class UsageRefreshError(RuntimeError):
    """Bounded refresh failure that leaves prior bytes untouched."""


@dataclass(frozen=True)
class LaneRefreshResult:
    lane_id: str
    updated: bool
    weekly_pct_used: float | None
    checked_at: str | None
    detail: str


@dataclass(frozen=True)
class UsageRefreshReport:
    path: Path
    mirrored_to: Path | None
    source: str
    lanes: tuple[LaneRefreshResult, ...]
    ok: bool
    detail: str = ""
    document: Mapping[str, Any] = field(default_factory=dict)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(now: datetime | None = None) -> str:
    stamp = (now or _utc_now()).astimezone(timezone.utc)
    # Keep millisecond precision for stable JSON readers.
    return stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _clamp_pct(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or not 0.0 <= number <= 100.0:
        return None
    return float(round(number))


def _label_matches(label: object, needles: tuple[str, ...]) -> bool:
    text = " ".join(str(label or "").strip().lower().replace("·", " ").split())
    return any(needle in text for needle in needles)


def _find_plan(plans: list[dict[str, Any]], needles: tuple[str, ...]) -> dict[str, Any] | None:
    matches = [row for row in plans if _label_matches(row.get("label"), needles)]
    if len(matches) > 1:
        raise UsageRefreshError(f"duplicate plan rows for {needles[0]}")
    return matches[0] if matches else None


def _weekly_used_from_snapshot(
    snapshot: object, *, lane_id: str
) -> float | None:
    # Single shared implementation with LiveUsageAdapter — the two paths drifting
    # apart is how a lane ends up measured in one place and blind in the other.
    values = [
        pct
        for pct in (
            _clamp_pct(raw) for raw in weekly_used_percents(snapshot, lane_id=lane_id)
        )
        if pct is not None
    ]
    return max(values) if values else None


def _fetch_auto_lane_pct(
    provider_id: str,
    *,
    lane_id: str,
    fetch_usage: Callable[[str], object | None] | None = None,
) -> float | None:
    if fetch_usage is None:
        from agent.account_usage import fetch_account_usage

        fetch_usage = fetch_account_usage  # type: ignore[assignment]
    try:
        snapshot = fetch_usage(provider_id)  # type: ignore[misc]
    except Exception as exc:  # pragma: no cover - provider network failures
        raise UsageRefreshError(f"{provider_id} usage fetch failed: {exc}") from exc

    # Anthropic usage goes dark the moment the OAuth token expires: the usage
    # resolver is STRICTLY READ-ONLY by design (no refresh, no credential writes,
    # no pool persistence) and honestly returns None rather than refreshing. The
    # router then scores the lane at ZERO headroom and stops selecting it — which
    # is how an 84%-headroom lane silently dropped out of rotation.
    #
    # The fix must NOT make Hermes refresh credentials; that is the exact risk the
    # read-only hardening removed. Instead ask Claude Code to refresh its OWN
    # token, which it does as a side effect of any invocation, then re-read.
    if snapshot is None and str(provider_id) == "anthropic":
        if _poke_claude_code_to_refresh_token():
            try:
                snapshot = fetch_usage(provider_id)  # type: ignore[misc]
            except Exception:
                snapshot = None
    return _weekly_used_from_snapshot(snapshot, lane_id=lane_id)


def _poke_claude_code_to_refresh_token(timeout_seconds: float = 90.0) -> bool:
    """Invoke Claude Code minimally so IT refreshes its own OAuth token.

    Hermes never writes the credential: Claude Code owns that file and refreshes
    it on any invocation. This is a metering-support action, not inference work —
    the prompt is a single token and the result is discarded.

    Returns True if the CLI ran. Never raises: a failed poke must degrade to
    "usage unknown", never break the refresh sweep.
    """
    import shutil
    import subprocess

    try:
        exe = shutil.which("claude") or str(
            Path.home() / ".local" / "bin" / "claude.exe"
        )
        if not exe or not os.path.isfile(exe):
            return False
        subprocess.run(
            [exe, "-p", "ok"],
            capture_output=True,
            timeout=timeout_seconds,
            creationflags=no_console_creationflags(),
        )
        return True
    except Exception:
        return False


def _empty_document(*, now: datetime | None = None) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "checked_at": _iso(now),
        "source": "hermes native fleet usage refresh",
        "plans": [
            {
                "label": "ChatGPT Pro · Codex",
                "agents": ["Hermes", "ChatGPT"],
                "weekly_pct_used": 0,
                "resets": "weekly",
            },
            {
                "label": "Claude Max 20x",
                "agents": ["Claude Code"],
                "weekly_pct_used": 0,
                "resets": "weekly",
            },
            {
                "label": "SuperGrok",
                "agents": ["Grok"],
                "weekly_pct_used": 0,
                "resets": "weekly",
            },
            {
                "label": "Google AI · Antigravity",
                "agents": ["Gemini (agy)"],
                "weekly_pct_used": 0,
                "resets": "weekly",
            },
        ],
    }


def _validate_document(document: object) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise UsageRefreshError("usage document must be an object")
    plans = document.get("plans")
    if not isinstance(plans, list) or not plans:
        raise UsageRefreshError("usage document missing plans[]")
    for row in plans:
        if not isinstance(row, dict):
            raise UsageRefreshError("plan row must be an object")
        if not str(row.get("label") or "").strip():
            raise UsageRefreshError("plan row missing label")
        pct = row.get("weekly_pct_used")
        if _clamp_pct(pct) is None and pct is not None:
            raise UsageRefreshError("plan row weekly_pct_used invalid")
    return document


def _read_document(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise UsageRefreshError(f"cannot read {path}: {exc}") from exc
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise UsageRefreshError(f"invalid JSON in {path}: {exc}") from exc
    return _validate_document(document)


def _atomic_write_json(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.stem}_",
        suffix=".tmp",
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=1, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        atomic_replace(tmp_path, path)
        # Best-effort directory fsync for durability on POSIX.
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
        except OSError:
            dir_fd = -1
        if dir_fd >= 0:
            try:
                os.fsync(dir_fd)
            except OSError:
                pass
            finally:
                os.close(dir_fd)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def refresh_usage_document(
    *,
    path: str | Path | None = None,
    home: Path | None = None,
    mirror_path: str | Path | None = COCKPIT_MIRROR_PATH,
    fetch_usage: Callable[[str], object | None] | None = None,
    now: datetime | None = None,
    create_if_missing: bool = True,
) -> UsageRefreshReport:
    """Refresh auto-queryable lanes and atomically rewrite the native file.

    Failure modes leave the previous native file bytes untouched. Console-only
    lanes are never given a fabricated ``checked_at``.
    """

    target = resolve_usage_path(path, home=home) if path is not None else default_native_usage_path(home=home)
    previous = _read_document(target)
    if previous is None:
        if not create_if_missing:
            raise UsageRefreshError(f"usage file absent: {target}")
        document = _empty_document(now=now)
    else:
        document = copy.deepcopy(previous)

    plans = document["plans"]
    assert isinstance(plans, list)
    results: list[LaneRefreshResult] = []
    any_auto_success = False
    any_console_health_evidence = False
    stamp = _iso(now)

    for lane_id, provider_id, default_label, needles in AUTO_LANES:
        try:
            pct = _fetch_auto_lane_pct(
                provider_id,
                lane_id=lane_id,
                fetch_usage=fetch_usage,
            )
        except UsageRefreshError as exc:
            results.append(
                LaneRefreshResult(
                    lane_id=lane_id,
                    updated=False,
                    weekly_pct_used=None,
                    checked_at=None,
                    detail=str(exc),
                )
            )
            continue
        if pct is None:
            results.append(
                LaneRefreshResult(
                    lane_id=lane_id,
                    updated=False,
                    weekly_pct_used=None,
                    checked_at=None,
                    detail=f"{provider_id}: no weekly window evidence",
                )
            )
            continue
        row = _find_plan(plans, needles)  # type: ignore[arg-type]
        if row is None:
            row = {
                "label": default_label,
                "agents": [],
                "weekly_pct_used": pct,
                "resets": "weekly",
            }
            plans.append(row)
        row["weekly_pct_used"] = pct
        row["checked_at"] = stamp
        # Attributeable capacity metadata when available.
        row.setdefault("comparability_group", "subscription-weekly")
        row.setdefault("measurement_kind", "measured")
        any_auto_success = True
        results.append(
            LaneRefreshResult(
                lane_id=lane_id,
                updated=True,
                weekly_pct_used=pct,
                checked_at=stamp,
                detail="ok",
            )
        )

    # Console-only lanes: usage and health have independent clocks. A health
    # probe may update only health fields; it must never refresh a prior usage
    # percentage or make an unattested percentage measured/comparable.
    attestation = _read_console_attestation(_console_attestation_path(home=home))
    for lane_id, default_label, needles in CONSOLE_ONLY_LANES:
        row = _find_plan(plans, needles)  # type: ignore[arg-type]
        if row is None:
            row = {
                "label": default_label,
                "agents": [],
                "weekly_pct_used": 0,
                "resets": "weekly",
            }
            plans.append(row)
        prior_pct = _clamp_pct(row.get("weekly_pct_used"))
        attested = lane_id in attestation
        attested_fresh = False
        if attested:
            attested_pct, attested_checked_at = attestation[lane_id]
            # Check for stale attestation (>24h old) — grok/antigravity only,
            # never use stale evidence for capacity routing.
            if _is_attestation_stale(attested_checked_at, max_age_hours=24, now=now or _utc_now()):
                # Attestation is stale; keep prior values but mark as stale
                prior_checked = row.get("checked_at")
                health_state, health_detail = _probe_console_lane_health(lane_id)
                health_observed = health_state is not None
                if health_observed:
                    any_console_health_evidence = True
                    row["health_status"] = "UP" if health_state else "DOWN"
                    row["health_checked_at"] = stamp
                    health_summary = (
                        f"console health probe {'ok' if health_state else 'down'}; "
                        f"{health_detail}; attestation stale (>{24}h)"
                    )
                else:
                    health_summary = f"console health probe unavailable; attestation stale (>24h)"
                results.append(
                    LaneRefreshResult(
                        lane_id=lane_id,
                        updated=health_observed,
                        weekly_pct_used=prior_pct,
                        checked_at=(
                            str(prior_checked) if isinstance(prior_checked, str) else None
                        ),
                        detail=health_summary,
                    )
                )
                continue

            attested_fresh = True
            row["weekly_pct_used"] = attested_pct
            row["checked_at"] = attested_checked_at
            row["measurement_kind"] = "measured"
            row["comparability_group"] = "subscription-weekly"
            row["quota_window_id"] = "subscription-weekly"
            row.setdefault("overage_disabled", True)
            row.setdefault("resets", "weekly")
            prior_pct = attested_pct

        health_state, health_detail = _probe_console_lane_health(lane_id)
        health_observed = health_state is not None
        if health_observed:
            any_console_health_evidence = True
            row["health_status"] = "UP" if health_state else "DOWN"
            row["health_checked_at"] = stamp

        prior_checked = row.get("checked_at")
        if health_state is True:
            health_summary = f"console health probe ok; {health_detail}"
        elif health_state is False:
            health_summary = f"console health probe down; {health_detail}"
        else:
            health_summary = f"console health probe unavailable; {health_detail}"
        results.append(
            LaneRefreshResult(
                lane_id=lane_id,
                updated=attested_fresh or health_observed,
                weekly_pct_used=prior_pct,
                checked_at=(
                    str(prior_checked) if isinstance(prior_checked, str) else None
                ),
                detail=health_summary,
            )
        )

    if (
        not any_auto_success
        and not any_console_health_evidence
        and previous is not None
    ):
        # Preserve prior file entirely when every auto lane failed.
        return UsageRefreshReport(
            path=target,
            mirrored_to=None,
            source=str(document.get("source") or ""),
            lanes=tuple(results),
            ok=False,
            detail="no auto-queryable lane produced fresh evidence; prior file preserved",
            document=previous,
        )

    # Root checked_at tracks last successful auto refresh only.
    if any_auto_success:
        document["source"] = (
            "hermes native fleet usage refresh "
            "(openai-codex + anthropic headless; "
            "grok/antigravity console health probe)"
        )
        document["checked_at"] = stamp
    document["schema_version"] = document.get("schema_version") or SCHEMA_VERSION
    validated = _validate_document(document)

    try:
        _atomic_write_json(target, validated)
    except OSError as exc:
        raise UsageRefreshError(f"atomic write failed for {target}: {exc}") from exc

    mirrored: Path | None = None
    if mirror_path is not None and str(mirror_path).strip():
        mirror = Path(mirror_path)
        try:
            _atomic_write_json(mirror, validated)
            mirrored = mirror
        except OSError:
            # Mirror is best-effort compatibility only; native path is SoT.
            mirrored = None

    return UsageRefreshReport(
        path=target,
        mirrored_to=mirrored,
        source=str(validated.get("source") or ""),
        lanes=tuple(results),
        ok=any_auto_success or any_console_health_evidence,
        detail=(
            "refreshed"
            if any_auto_success
            else (
                "console health refreshed; prior usage preserved"
                if any_console_health_evidence
                else "created empty shell"
            )
        ),
        document=validated,
    )


def serialize_refresh_report(report: UsageRefreshReport) -> dict[str, Any]:
    return {
        "ok": report.ok,
        "path": str(report.path),
        "mirrored_to": str(report.mirrored_to) if report.mirrored_to else None,
        "source": report.source,
        "detail": report.detail,
        "lanes": [
            {
                "lane_id": lane.lane_id,
                "updated": lane.updated,
                "weekly_pct_used": lane.weekly_pct_used,
                "checked_at": lane.checked_at,
                "detail": lane.detail,
            }
            for lane in report.lanes
        ],
    }
