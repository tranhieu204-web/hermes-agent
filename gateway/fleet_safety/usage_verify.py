"""Verify a cached usage figure against the authoritative provider source.

The 2026-07-25 incident had a second, quieter bug: the local usage tracker
(``usage-weekly.json``) reported Grok at ~10% used while the real xAI console
showed 100%. Usage-based routing and any wallet cap that trust that cached
number are then blind — they never throttle because they think the wallet is
almost full.

This module makes the cached number *earn trust*. It cross-checks it against
the authoritative source (the live provider usage endpoint, surfaced by
``agent.account_usage.fetch_account_usage``) and returns a
:class:`VerifiedUsage` that says which number to trust and flags the cache as
**stale** (too old) or **suspect** (diverges from authoritative, or could not
be verified at all). Downstream — the wallet cap — consumes the verified
percent, not the raw cache.

Fail-closed posture: when the two disagree, the authoritative source wins. When
authoritative is unavailable, the cache is used but marked ``suspect`` so the
wallet cap can apply its unknown-usage policy rather than blindly trusting a
possibly-stale 10%.

Pure and deterministic: :func:`verify_usage` takes ``now`` and both figures
explicitly and reads no clock and no file. :func:`extract_authoritative` and
:func:`load_cached_percent` are the thin, side-effecting adapters the live
wiring uses to produce those inputs.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VerifiedUsage:
    provider: str
    used_percent: Optional[float]      # the number downstream should trust (0-100), or None
    source: str                        # "authoritative" | "cache" | "none"
    stale: bool                        # cache older than max_age
    suspect: bool                      # diverges from authoritative, or unverifiable
    authoritative_percent: Optional[float] = None
    cached_percent: Optional[float] = None
    reasons: List[str] = field(default_factory=list)

    @property
    def trustworthy(self) -> bool:
        return self.used_percent is not None and not self.suspect and not self.stale


def verify_usage(
    provider: str,
    *,
    cached_percent: Optional[float],
    cached_fetched_at: Optional[float],
    authoritative_percent: Optional[float],
    authoritative_available: bool,
    now: float,
    max_age_seconds: float = 900.0,
    divergence_points: float = 15.0,
) -> VerifiedUsage:
    """Reconcile a cached usage percent with the authoritative one.

    ``cached_fetched_at`` / ``now`` are epoch seconds. ``divergence_points`` is
    an absolute percentage-point gap (e.g. 15 means a 10% cache vs a 100%
    authoritative — a 90-point gap — is flagged suspect).
    """
    reasons: List[str] = []
    stale = False
    if cached_fetched_at is not None and (now - cached_fetched_at) > max_age_seconds:
        stale = True
        age_min = (now - cached_fetched_at) / 60.0
        reasons.append(f"cache is {age_min:.0f} min old (> {max_age_seconds / 60:.0f} min)")

    # Authoritative available → it is the source of truth.
    if authoritative_available and authoritative_percent is not None:
        suspect = False
        if cached_percent is not None:
            gap = abs(cached_percent - authoritative_percent)
            if gap > divergence_points:
                suspect = True
                reasons.append(
                    f"cache {cached_percent:.0f}% diverges from authoritative "
                    f"{authoritative_percent:.0f}% by {gap:.0f} pts — trusting authoritative"
                )
        return VerifiedUsage(
            provider=provider,
            used_percent=authoritative_percent,
            source="authoritative",
            stale=False,               # authoritative is fresh by definition
            suspect=suspect,
            authoritative_percent=authoritative_percent,
            cached_percent=cached_percent,
            reasons=reasons,
        )

    # Authoritative unavailable → fall back to cache, but never fully trust it.
    reasons.append("authoritative usage source unavailable — cannot verify cache")
    if cached_percent is not None:
        return VerifiedUsage(
            provider=provider,
            used_percent=cached_percent,
            source="cache",
            stale=stale,
            suspect=True,              # unverifiable → suspect by policy
            authoritative_percent=None,
            cached_percent=cached_percent,
            reasons=reasons,
        )

    # Nothing usable at all.
    reasons.append("no cached usage figure either")
    return VerifiedUsage(
        provider=provider,
        used_percent=None,
        source="none",
        stale=True,
        suspect=True,
        authoritative_percent=None,
        cached_percent=None,
        reasons=reasons,
    )


# --------------------------------------------------------------------------
# Side-effecting adapters (thin; kept out of the pure decision above).
# --------------------------------------------------------------------------


def worst_used_percent(windows: Any) -> Optional[float]:
    """Max ``used_percent`` across usage windows (dicts or objects).

    The weekly ceiling is the binding one for a wallet cap, so we take the
    worst (highest) used percent across whatever windows the provider reports.
    Returns ``None`` when no window carries a numeric percent.
    """
    best: Optional[float] = None
    for w in windows or ():
        pct = getattr(w, "used_percent", None)
        if pct is None and isinstance(w, dict):
            pct = w.get("used_percent")
        if pct is None:
            continue
        try:
            val = float(pct)
        except (TypeError, ValueError):
            continue
        best = val if best is None else max(best, val)
    return best


def extract_authoritative(snapshot: Any) -> tuple:
    """Reduce an ``AccountUsageSnapshot`` to ``(available, percent, fetched_at)``.

    Duck-typed so this module never imports ``agent.account_usage`` (keeps the
    verifier importable and testable in isolation). ``fetched_at`` is returned
    as epoch seconds when derivable.
    """
    if snapshot is None:
        return False, None, None
    available = bool(getattr(snapshot, "available", False))
    percent = worst_used_percent(getattr(snapshot, "windows", ()) or ())
    fetched_at = getattr(snapshot, "fetched_at", None)
    epoch: Optional[float] = None
    if isinstance(fetched_at, datetime):
        dt = fetched_at if fetched_at.tzinfo else fetched_at.replace(tzinfo=timezone.utc)
        epoch = dt.timestamp()
    elif isinstance(fetched_at, (int, float)):
        epoch = float(fetched_at)
    if percent is None:
        available = False
    return available, percent, epoch


def load_cached_percent(path: Path, provider: str) -> tuple:
    """Read ``(percent, fetched_at_epoch)`` for ``provider`` from a cache file.

    Best-effort and forgiving of schema drift: understands a top-level
    ``used_percent`` / ``percent`` and a per-provider ``{provider: {...}}`` map,
    and a ``fetched_at`` / ``updated_at`` timestamp (epoch or ISO-8601).
    Returns ``(None, None)`` on any problem — a missing/garbled cache must not
    crash verification.
    """
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None
    if not isinstance(raw, dict):
        return None, None

    node: Any = raw
    prov_map = raw.get("providers")
    if isinstance(prov_map, dict) and provider in prov_map and isinstance(prov_map[provider], dict):
        node = prov_map[provider]
    elif provider in raw and isinstance(raw[provider], dict):
        node = raw[provider]

    percent = node.get("used_percent", node.get("percent"))
    try:
        percent = float(percent) if percent is not None else None
    except (TypeError, ValueError):
        percent = None

    ts = node.get("fetched_at", node.get("updated_at", raw.get("fetched_at")))
    epoch: Optional[float] = None
    if isinstance(ts, (int, float)):
        epoch = float(ts)
    elif isinstance(ts, str) and ts.strip():
        text = ts.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
            epoch = (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
        except ValueError:
            epoch = None
    return percent, epoch


def _usage_cache_path() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home()) / "usage-weekly.json"
    except Exception:
        return Path.home() / ".hermes" / "usage-weekly.json"


def verified_usage_for(
    provider: str,
    *,
    now: Optional[float] = None,
    max_age_seconds: float = 900.0,
    divergence_points: float = 15.0,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> VerifiedUsage:
    """Reconcile the cached usage figure for ``provider`` against the live
    authoritative source. Best-effort — never raises; returns an unknown/suspect
    result if either side can't be read."""
    if now is None:
        now = time.time()

    cached_percent, cached_ts = load_cached_percent(_usage_cache_path(), provider)

    available, auth_percent, _auth_ts = False, None, None
    try:
        from agent.account_usage import fetch_account_usage
        snapshot = fetch_account_usage(provider, base_url=base_url, api_key=api_key)
        available, auth_percent, _auth_ts = extract_authoritative(snapshot)
    except Exception as e:
        logger.debug("authoritative usage fetch failed for %s: %s", provider, e)

    return verify_usage(
        provider,
        cached_percent=cached_percent,
        cached_fetched_at=cached_ts,
        authoritative_percent=auth_percent,
        authoritative_available=available,
        now=now,
        max_age_seconds=max_age_seconds,
        divergence_points=divergence_points,
    )
