"""Redacted subscription-safety status without inference or credential access."""

from __future__ import annotations

import argparse
import json
from typing import Any


_STATUS = {
    "openai-codex": (
        "codex_responses", "NOT_VERIFIED: AUTHORITATIVE_OPENAI_CHARGE_SOURCE_UNAVAILABLE"
    ),
    "xai-oauth": (
        "codex_responses", "NOT_VERIFIED: AUTHORITATIVE_XAI_CHARGE_SOURCE_UNAVAILABLE"
    ),
}


def probe_subscription_route(provider: str) -> dict[str, Any]:
    normalized = str(provider or "").strip().lower()
    if normalized not in _STATUS:
        return {
            "status": "NOT_VERIFIED", "provider": normalized or "unknown",
            "reason": "UNSUPPORTED_SUBSCRIPTION_ROUTE",
        }
    transport, reason = _STATUS[normalized]
    return {
        "status": "NOT_VERIFIED", "provider": normalized,
        "transport": transport, "reason": reason,
        "credential_accessed": False, "network_accessed": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=tuple(_STATUS), required=True)
    args = parser.parse_args(argv)
    print(json.dumps(probe_subscription_route(args.provider), sort_keys=True, separators=(",", ":")))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
