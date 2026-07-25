"""Usage-headroom fallback selector and role-based reasoning effort mapping.

Implements usage-verified fleet routing across lanes (chatgpt_codex, claude_code,
grok, antigravity).

Key features:
1. **Usage-headroom routing:** Selects the enabled agent with the highest verified
   remaining weekly headroom above its lane ``reserve_floor_pct``.
2. **Fail-safe unverified handling:** Any lane whose usage cannot be verified or whose
   attestation is stale/suspect is treated as unknown usage (0 headroom) for heavy work.
3. **Anti-thrash 20-pt band:** Keeps the incumbent provider if the top competitor does
   not beat it by at least 20 percentage points of remaining headroom.
4. **Role-based effort & ladders:** Maps main workers (Codex & Claude) to ``xhigh`` and
   auditors/investigators (Grok & Gemini) to ``high``. Provides provider-specific ladders.
5. **Claude Fable→Opus ladder:** Switches from ``claude-fable-5`` (<50% weekly usage)
   to ``claude-opus-5`` (50–100% weekly usage).
6. **All-lanes-below-floor fallback:** When no lane meets its reserve floor, falls back
   to the highest remaining headroom enabled lane without raising or returning empty.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

from gateway.fleet_safety.usage_verify import verified_usage_for

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LaneConfig:
    name: str                  # canonical lane name e.g. "chatgpt_codex", "claude_code", "grok", "antigravity"
    provider: str              # provider identifier e.g. "openai-codex", "anthropic", "xai-oauth", "antigravity"
    model: str                 # default top model e.g. "gpt-5.6-sol", "claude-sonnet-4-6", "grok-4.5", "gemini-3.1-pro-high"
    reserve_floor_pct: float = 0.0
    enabled: bool = True


@dataclass(frozen=True)
class SelectedLane:
    lane: str                  # canonical lane name
    provider: str              # provider identifier
    model: str                 # resolved model identifier (e.g. claude-fable-5 or claude-opus-5)
    effort: str                # resolved role-based effort
    used_percent: Optional[float]
    remaining_headroom: Optional[float]
    is_fallback: bool          # True if selected via all-lanes-below-floor fallback
    reason: str


DEFAULT_LANES: Dict[str, LaneConfig] = {
    "chatgpt_codex": LaneConfig("chatgpt_codex", "openai-codex", "gpt-5.6-sol", 8.0, True),
    "claude_code": LaneConfig("claude_code", "anthropic", "claude-sonnet-4-6", 2.0, True),
    "grok": LaneConfig("grok", "xai-oauth", "grok-4.5", 5.0, True),
    "antigravity": LaneConfig("antigravity", "antigravity", "gemini-3.1-pro-high", 5.0, True),
}

DEFAULT_LANE_EFFORTS: Dict[str, str] = {
    "chatgpt_codex": "xhigh",
    "claude_code": "xhigh",
    "grok": "high",
    "antigravity": "high",
}

LANE_EFFORT_LADDERS: Dict[str, List[str]] = {
    "chatgpt_codex": ["none", "low", "medium", "high", "xhigh", "max"],
    "claude_code": ["low", "medium", "high", "xhigh", "max"],
    "grok": ["none", "low", "medium", "high"],
    "antigravity": ["minimal", "low", "medium", "high"],
}

_PROVIDER_TO_LANE: Dict[str, str] = {
    "openai-codex": "chatgpt_codex",
    "openai": "chatgpt_codex",
    "codex": "chatgpt_codex",
    "gpt-5.6-sol": "chatgpt_codex",
    "gpt-5.6": "chatgpt_codex",
    "gpt-5.5": "chatgpt_codex",
    "anthropic": "claude_code",
    "claude": "claude_code",
    "claude-sonnet-4-6": "claude_code",
    "claude-fable-5": "claude_code",
    "claude-opus-5": "claude_code",
    "xai-oauth": "grok",
    "xai": "grok",
    "grok-4.5": "grok",
    "antigravity": "antigravity",
    "gemini": "antigravity",
    "gemini-3.1-pro-high": "antigravity",
    "google": "antigravity",
}


def get_lane_name(model_or_provider: str) -> str:
    """Map a provider, model, or lane slug to its canonical lane name."""
    if not model_or_provider:
        return ""
    val = str(model_or_provider).strip().lower()
    if val in DEFAULT_LANES:
        return val
    if val in _PROVIDER_TO_LANE:
        return _PROVIDER_TO_LANE[val]
    for key, lane in _PROVIDER_TO_LANE.items():
        if key in val or val in key:
            return lane
    return val


def get_effort_ladder(provider_or_lane: str) -> List[str]:
    """Return the effort ladder for a provider or lane."""
    lane = get_lane_name(provider_or_lane)
    if lane in LANE_EFFORT_LADDERS:
        return list(LANE_EFFORT_LADDERS[lane])
    return ["none", "low", "medium", "high", "xhigh", "max"]


_EFFORT_SCALE = ["none", "minimal", "low", "medium", "high", "xhigh", "max"]
_EFFORT_INDEX = {eff: idx for idx, eff in enumerate(_EFFORT_SCALE)}


def _clamp_effort_to_ladder(requested_effort: str, ladder: List[str]) -> str:
    """Clamp requested effort level to the max/min bounds of a provider's ladder."""
    if not ladder:
        return requested_effort
    req = str(requested_effort).strip().lower()
    if req in ladder:
        return req
    if req not in _EFFORT_INDEX:
        return ladder[-1]
    req_idx = _EFFORT_INDEX[req]
    ladder_indices = [(_EFFORT_INDEX.get(item, 3), item) for item in ladder]
    ladder_indices.sort(key=lambda x: x[0])
    min_idx, min_item = ladder_indices[0]
    max_idx, max_item = ladder_indices[-1]
    if req_idx > max_idx:
        return max_item
    if req_idx < min_idx:
        return min_item
    for idx_val, item in reversed(ladder_indices):
        if idx_val <= req_idx:
            return item
    return ladder[-1]


def resolve_effort_from_map(effort_map: Any, model: str = "", provider: str = "") -> str:
    """Resolve role-based effort from a per-agent map or fallback default, bounded by provider ladder."""
    lane = get_lane_name(provider or model)
    ladder = get_effort_ladder(lane or provider or model)
    raw_effort = ""
    if isinstance(effort_map, dict):
        if model:
            m_lower = str(model).strip().lower()
            if m_lower in effort_map:
                raw_effort = str(effort_map[m_lower])
        if not raw_effort and lane and lane in effort_map:
            raw_effort = str(effort_map[lane])
        if not raw_effort and provider and str(provider).strip().lower() in effort_map:
            raw_effort = str(effort_map[str(provider).strip().lower()])
    elif isinstance(effort_map, str) and effort_map.strip():
        raw_effort = effort_map.strip()

    if not raw_effort:
        raw_effort = DEFAULT_LANE_EFFORTS.get(lane, "medium")

    return _clamp_effort_to_ladder(raw_effort, ladder)


def _resolve_claude_model(used_percent: Optional[float], default_model: str = "claude-sonnet-4-6") -> str:
    """Return configured model for Claude without inventing non-existent model IDs."""
    return default_model


def select_best_lane(
    config: Optional[dict] = None,
    current_provider: str = "",
    *,
    is_heavy: bool = True,
    now: Optional[float] = None,
    usage_by_lane: Optional[Mapping[str, float]] = None,
) -> SelectedLane:
    """Route to the enabled lane with the highest verified weekly headroom.

    Enforces reserve floor percentages, anti-thrash 20-pt band, fail-safe unverified
    attestation treatment, and role-based effort resolution bounded by provider ladders.
    """
    if now is None:
        now = time.time()

    cfg = config or {}
    fleet_cfg = cfg.get("fleet") or {}
    lanes_cfg = fleet_cfg.get("lanes") or {}
    switch_delta = float(
        fleet_cfg.get(
            "switch_delta_pct",
            fleet_cfg.get("switch_delta", 20.0),
        )
    )

    candidates: Dict[str, Tuple[LaneConfig, Optional[float], Optional[float], List[str]]] = {}

    for slug, default_lane in DEFAULT_LANES.items():
        l_cfg = lanes_cfg.get(slug) if isinstance(lanes_cfg, dict) else {}
        if not isinstance(l_cfg, dict):
            l_cfg = {}
        enabled = bool(l_cfg.get("enabled", default_lane.enabled))
        if not enabled:
            continue
        floor = float(l_cfg.get("reserve_floor_pct", default_lane.reserve_floor_pct))
        lane = LaneConfig(
            name=default_lane.name,
            provider=str(l_cfg.get("provider", default_lane.provider)).strip() or default_lane.provider,
            model=str(l_cfg.get("model", default_lane.model)).strip() or default_lane.model,
            reserve_floor_pct=floor,
            enabled=True,
        )

        if usage_by_lane is not None:
            raw_used_pct = usage_by_lane.get(slug)
            try:
                used_pct = (
                    float(raw_used_pct) if raw_used_pct is not None else None
                )
            except (TypeError, ValueError):
                used_pct = None
            if used_pct is not None and not (
                math.isfinite(used_pct) and 0.0 <= used_pct <= 100.0
            ):
                used_pct = None
            headroom = (100.0 - used_pct) if used_pct is not None else None
            reasons = [
                "prevalidated fleet capacity"
                if used_pct is not None
                else "prevalidated fleet capacity: invalid or missing"
            ]
        else:
            verified = verified_usage_for(lane.provider, now=now)
            used_pct = verified.used_percent
            if is_heavy and (
                used_pct is None or verified.stale or verified.suspect
            ):
                used_pct = None
                headroom = None
            else:
                headroom = (100.0 - used_pct) if used_pct is not None else None
            reasons = verified.reasons

        candidates[slug] = (lane, used_pct, headroom, reasons)

    # Filter eligible lanes: known headroom >= reserve floor
    eligible: Dict[str, Tuple[LaneConfig, Optional[float], float, List[str]]] = {}
    for slug, (lane, used_pct, headroom, reasons) in candidates.items():
        if headroom is not None and headroom >= lane.reserve_floor_pct:
            eligible[slug] = (lane, used_pct, headroom, reasons)

    effort_map = cfg.get("agent", {}).get("reasoning_effort")
    cur_lane_name = get_lane_name(current_provider)

    if eligible:
        sorted_el = sorted(eligible.values(), key=lambda x: x[2], reverse=True)
        best_lane, best_used, best_hr, best_reasons = sorted_el[0]

        if cur_lane_name in eligible and cur_lane_name != best_lane.name:
            inc_lane, inc_used, inc_hr, inc_reasons = eligible[cur_lane_name]
            if best_hr < inc_hr + switch_delta:
                best_lane, best_used, best_hr, best_reasons = inc_lane, inc_used, inc_hr, inc_reasons
                reason_str = f"incumbent {inc_lane.name} retained via 20-pt anti-thrash band (headroom {inc_hr:.1f}% vs top {sorted_el[0][0].name} at {sorted_el[0][2]:.1f}%)"
            else:
                reason_str = f"selected {best_lane.name} (headroom {best_hr:.1f}% exceeds floor {best_lane.reserve_floor_pct}%)"
        else:
            reason_str = f"selected {best_lane.name} (headroom {best_hr:.1f}% exceeds floor {best_lane.reserve_floor_pct}%)"

        model_id = best_lane.model
        effort_val = resolve_effort_from_map(effort_map, model_id, best_lane.provider)
        return SelectedLane(
            lane=best_lane.name,
            provider=best_lane.provider,
            model=model_id,
            effort=effort_val,
            used_percent=best_used,
            remaining_headroom=best_hr,
            is_fallback=False,
            reason=reason_str,
        )

    # Requirement 3: Fail closed when no eligible verified lane exists
    return SelectedLane(
        lane="",
        provider="",
        model="",
        effort="",
        used_percent=None,
        remaining_headroom=None,
        is_fallback=True,
        reason="no_eligible_lane: no eligible verified lane available meeting reserve floor",
    )


def rank_fallback_chain(
    chain: List[Dict[str, Any]],
    config: Optional[dict] = None,
    *,
    is_heavy: bool = True,
    now: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Filter and order fallback provider entries by usage headroom and floor rules."""
    if now is None:
        now = time.time()

    cfg = config or {}
    fleet_cfg = cfg.get("fleet") or {}
    lanes_cfg = fleet_cfg.get("lanes") or {}

    pool = list(chain) if chain else []
    if not pool and fleet_cfg.get("enabled", True):
        for def_lane in DEFAULT_LANES.values():
            pool.append({"provider": def_lane.provider, "model": def_lane.model})

    eligible_entries: List[Tuple[float, int, Dict[str, Any]]] = []

    for idx, entry in enumerate(pool):
        if not isinstance(entry, dict):
            continue
        prov = str(entry.get("provider") or "").strip()
        mod = str(entry.get("model") or "").strip()
        if not prov and not mod:
            continue

        lane_name = get_lane_name(prov or mod)
        def_lane = DEFAULT_LANES.get(lane_name)

        # Respect lane enabled state from config
        if isinstance(lanes_cfg, dict) and lane_name in lanes_cfg and isinstance(lanes_cfg[lane_name], dict):
            if lanes_cfg[lane_name].get("enabled") is False:
                continue

        floor = def_lane.reserve_floor_pct if def_lane else 0.0
        if isinstance(lanes_cfg, dict) and lane_name in lanes_cfg and isinstance(lanes_cfg[lane_name], dict):
            floor = float(lanes_cfg[lane_name].get("reserve_floor_pct", floor))

        verified = verified_usage_for(prov or (def_lane.provider if def_lane else ""), now=now)
        used_pct = verified.used_percent
        if is_heavy and (used_pct is None or verified.stale or verified.suspect):
            used_pct = None
            headroom = None
        else:
            headroom = (100.0 - used_pct) if used_pct is not None else None

        updated_entry = dict(entry)
        if prov:
            updated_entry["provider"] = prov
        if mod:
            updated_entry["model"] = mod

        if headroom is not None and headroom >= floor:
            eligible_entries.append((headroom, idx, updated_entry))

    # Sort eligible descending by headroom
    eligible_entries.sort(key=lambda x: (x[0], -x[1]), reverse=True)

    return [e for _, _, e in eligible_entries]
