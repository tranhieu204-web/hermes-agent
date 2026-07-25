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
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from utils import atomic_replace

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


def _read_console_attestation(path: Path) -> dict[str, float]:
    """Optional operator/CI file: {"lanes": {"grok": {"weekly_pct_used": 10}}}."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        document = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    lanes = document.get("lanes") if isinstance(document, dict) else None
    if not isinstance(lanes, dict):
        return {}
    out: dict[str, float] = {}
    for lane_id, row in lanes.items():
        if not isinstance(row, dict):
            continue
        pct = _clamp_pct(row.get("weekly_pct_used"))
        if pct is not None:
            out[str(lane_id)] = pct
    return out


def _probe_console_lane_health(lane_id: str) -> tuple[bool, str]:
    """Return (healthy, detail) without inventing usage percentages."""
    if lane_id == "grok":
        try:
            from hermes_cli.auth import get_xai_oauth_auth_status

            status = get_xai_oauth_auth_status() or {}
        except Exception as exc:  # noqa: BLE001 - probe must fail closed
            return False, f"xai-oauth status failed: {type(exc).__name__}"
        if bool(status.get("logged_in")):
            return True, "xai-oauth logged_in"
        return False, "xai-oauth not logged_in"
    if lane_id == "antigravity":
        try:
            from hermes_cli.fleet.live import FleetQualificationDoctor
            from hermes_cli.fleet.profiles import profile_map

            qualification = FleetQualificationDoctor().qualify(
                (profile_map()["antigravity"],)
            )["antigravity"]
        except Exception as exc:  # noqa: BLE001
            return False, f"antigravity doctor failed: {type(exc).__name__}"
        if qualification.qualified:
            return True, "antigravity live-receipt qualified"
        return False, qualification.detail or "antigravity not qualified"
    return False, f"unsupported console lane: {lane_id}"




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
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return max(0.0, min(100.0, round(number)))


def _label_matches(label: object, needles: tuple[str, ...]) -> bool:
    text = " ".join(str(label or "").strip().lower().replace("·", " ").split())
    return any(needle in text for needle in needles)


def _find_plan(plans: list[dict[str, Any]], needles: tuple[str, ...]) -> dict[str, Any] | None:
    matches = [row for row in plans if _label_matches(row.get("label"), needles)]
    if len(matches) > 1:
        raise UsageRefreshError(f"duplicate plan rows for {needles[0]}")
    return matches[0] if matches else None


def _weekly_used_from_snapshot(snapshot: object) -> float | None:
    windows = getattr(snapshot, "windows", None) or ()
    if not windows:
        return None
    weekly = [
        window
        for window in windows
        if "week" in str(getattr(window, "label", "")).lower()
    ]
    chosen = weekly[0] if weekly else windows[-1]
    return _clamp_pct(getattr(chosen, "used_percent", None))


def _fetch_auto_lane_pct(
    provider_id: str,
    *,
    fetch_usage: Callable[[str], object | None] | None = None,
) -> float | None:
    if fetch_usage is None:
        from agent.account_usage import fetch_account_usage

        fetch_usage = fetch_account_usage  # type: ignore[assignment]
    try:
        snapshot = fetch_usage(provider_id)  # type: ignore[misc]
    except Exception as exc:  # pragma: no cover - provider network failures
        raise UsageRefreshError(f"{provider_id} usage fetch failed: {exc}") from exc
    return _weekly_used_from_snapshot(snapshot)


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
    any_console_health_success = False
    stamp = _iso(now)

    for lane_id, provider_id, default_label, needles in AUTO_LANES:
        try:
            pct = _fetch_auto_lane_pct(provider_id, fetch_usage=fetch_usage)
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
        if lane_id in attestation:
            row["weekly_pct_used"] = attestation[lane_id]
            prior_pct = attestation[lane_id]
        healthy, health_detail = _probe_console_lane_health(lane_id)
        row["health_status"] = "UP" if healthy else "DOWN"
        row["health_checked_at"] = stamp
        if healthy:
            any_console_health_success = True
            results.append(
                LaneRefreshResult(
                    lane_id=lane_id,
                    updated=False,
                    weekly_pct_used=prior_pct,
                    checked_at=(
                        str(row["checked_at"])
                        if isinstance(row.get("checked_at"), str)
                        else None
                    ),
                    detail=f"console health probe ok; {health_detail}",
                )
            )
            continue
        prior_checked = row.get("checked_at")
        results.append(
            LaneRefreshResult(
                lane_id=lane_id,
                updated=False,
                weekly_pct_used=prior_pct,
                checked_at=str(prior_checked) if isinstance(prior_checked, str) else None,
                detail=f"console-only; health probe failed: {health_detail}",
            )
        )

    if (
        not any_auto_success
        and not any_console_health_success
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
        ok=any_auto_success or any_console_health_success,
        detail=(
            "refreshed"
            if any_auto_success
            else (
                "console health refreshed; prior usage preserved"
                if any_console_health_success
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
