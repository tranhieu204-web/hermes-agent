"""Per-provider spend guard for routing.

Given a provider's *verified* weekly-usage percent (see
:mod:`gateway.fleet_safety.usage_verify`), decide whether new heavy /
max-effort work may still route to it:

  - **>= cap_percent** (default 90%): stop routing new heavy/max-effort work to
    the provider — the caller should fall to the next provider in the chain.
  - **>= downgrade_percent** (default 80%) and < cap: still route, but downgrade
    reasoning effort (``max`` -> ``medium``) so the tail of the budget is spent
    an order of magnitude slower.
  - **< downgrade_percent**: allow unchanged.

The decision is a pure function of ``(request, used_percent)``. It never places
an order, never spends, never mutates the wallet — it only tells the router
where *not* to send expensive work. "Heavy" work is the only thing capped;
light/cheap requests are always allowed so the fleet stays responsive even near
a limit.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Optional


class WalletAction(str, enum.Enum):
    ALLOW = "allow"
    DOWNGRADE_EFFORT = "downgrade_effort"
    FALLBACK_PROVIDER = "fallback_provider"


# Effort ordering, cheapest → most expensive. Downgrade steps one rung down.
_EFFORT_ORDER = ["minimal", "none", "low", "medium", "high", "xhigh", "max"]


def _downgrade_effort(effort: str, floor: str = "medium", provider: str = "") -> str:
    """One rung down, but never below ``floor`` and never below the request."""
    try:
        from gateway.fleet_safety.selector import get_effort_ladder
        ladder = get_effort_ladder(provider)
    except Exception:
        ladder = ["none", "low", "medium", "high", "xhigh", "max"]
    e = (effort or "").lower()
    if e in ladder:
        idx = ladder.index(e)
        floor_idx = ladder.index(floor) if floor in ladder else 0
        return ladder[max(floor_idx, idx - 1)]
    if e in _EFFORT_ORDER:
        e_rank = _EFFORT_ORDER.index(e)
        cheaper = [r for r in ladder if r in _EFFORT_ORDER and _EFFORT_ORDER.index(r) < e_rank]
        if cheaper:
            res = cheaper[-1]
            floor_rank = _EFFORT_ORDER.index(floor) if floor in _EFFORT_ORDER else 0
            if _EFFORT_ORDER.index(res) >= floor_rank:
                return res
    return floor


@dataclass(frozen=True)
class WalletCapConfig:
    cap_percent: float = 90.0          # >= this: stop heavy work, fall to next provider
    downgrade_percent: float = 80.0    # >= this (and < cap): downgrade effort
    downgrade_floor: str = "medium"    # never downgrade below this rung
    # What to do when usage is unknown/unverifiable for a heavy request.
    # "downgrade" is the conservative-but-not-paralyzing default: we do not
    # know we are safe, so we slow expensive work without halting the fleet.
    on_unknown: str = "downgrade"      # "allow" | "downgrade" | "fallback"
    enabled: bool = True

    @classmethod
    def from_config(cls, cfg: Optional[dict]) -> "WalletCapConfig":
        cfg = cfg or {}

        def _f(key, default):
            try:
                v = cfg.get(key, default)
                return float(v) if v is not None else default
            except (TypeError, ValueError):
                return default

        on_unknown = str(cfg.get("on_unknown", cls.on_unknown)).lower()
        if on_unknown not in {"allow", "downgrade", "fallback"}:
            on_unknown = cls.on_unknown
        floor = str(cfg.get("downgrade_floor", cls.downgrade_floor)).lower()
        if floor not in _EFFORT_ORDER:
            floor = cls.downgrade_floor
        return cls(
            cap_percent=_f("cap_percent", cls.cap_percent),
            downgrade_percent=_f("downgrade_percent", cls.downgrade_percent),
            downgrade_floor=floor,
            on_unknown=on_unknown,
            enabled=bool(cfg.get("enabled", cls.enabled)),
        )


@dataclass(frozen=True)
class RoutingRequest:
    provider: str
    effort: str = "medium"
    # Only heavy work is capped. Heavy = expensive per call: max-effort,
    # or a large/agentic job the caller marks heavy.
    is_heavy: bool = False


@dataclass(frozen=True)
class WalletDecision:
    action: WalletAction
    provider: str
    effort: str                 # the effort to actually use (possibly downgraded)
    reason: str
    used_percent: Optional[float]
    fallback_provider: Optional[str] = None
    fallback_model: Optional[str] = None

    @property
    def allowed(self) -> bool:
        return self.action != WalletAction.FALLBACK_PROVIDER


class WalletCap:
    def __init__(self, config: Optional[WalletCapConfig] = None) -> None:
        self.config = config or WalletCapConfig()

    def decide(self, request: RoutingRequest, used_percent: Optional[float]) -> WalletDecision:
        """Decide routing for ``request`` given the provider's verified usage.

        ``used_percent`` is 0–100, or ``None`` when usage could not be verified.
        """
        c = self.config

        def _mk(action: WalletAction, effort: str, reason: str) -> WalletDecision:
            return WalletDecision(
                action=action,
                provider=request.provider,
                effort=effort,
                reason=reason,
                used_percent=used_percent,
            )

        # Guard disabled, or a light request: never cap.
        if not c.enabled or not request.is_heavy:
            return _mk(WalletAction.ALLOW, request.effort, "not capped")

        if used_percent is None:
            if c.on_unknown == "allow":
                return _mk(WalletAction.ALLOW, request.effort, "usage unknown; allowed by policy")
            if c.on_unknown == "fallback":
                return _mk(
                    WalletAction.FALLBACK_PROVIDER, request.effort,
                    "usage unknown; failing to next provider by policy",
                )
            return _mk(
                WalletAction.DOWNGRADE_EFFORT,
                _downgrade_effort(request.effort, c.downgrade_floor, request.provider),
                "usage unknown; downgrading effort by policy",
            )

        if used_percent >= c.cap_percent:
            return _mk(
                WalletAction.FALLBACK_PROVIDER, request.effort,
                f"provider at {used_percent:.0f}% (cap {c.cap_percent:.0f}%); "
                f"no new heavy work — fall to next provider",
            )
        if used_percent >= c.downgrade_percent:
            downgraded = _downgrade_effort(request.effort, c.downgrade_floor, request.provider)
            if downgraded != (request.effort or "").lower():
                return _mk(
                    WalletAction.DOWNGRADE_EFFORT, downgraded,
                    f"provider at {used_percent:.0f}% "
                    f"(>= {c.downgrade_percent:.0f}%); effort "
                    f"{request.effort} -> {downgraded}",
                )
            return _mk(
                WalletAction.ALLOW, request.effort,
                f"provider at {used_percent:.0f}%; effort already at floor",
            )
        return _mk(WalletAction.ALLOW, request.effort, f"provider at {used_percent:.0f}%")
