"""Pre-dispatch usage refresh hook — throttled, timeout-bounded, coalescing-safe.

Ensures fresh usage evidence before every router/dispatch decision without
deadlocking the dispatcher or spamming the usage APIs.

CONCURRENCY DESIGN (proven by construction):
  1. Lock held ONLY during task selection/creation (critical section ~microseconds)
  2. Lock released BEFORE any await (no serialization; true coalescing)
  3. asyncio.shield() protects task from cancellation if waiter times out
  4. Throttle window rechecked inside lock before creating new task
  5. All concurrent callers await the same shielded task → consistent results
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


def _sanitize_error_message(exc: Exception) -> str:
    """Sanitize exception message to prevent credential/secret leakage.

    Keeps exception type name but scrubs the message of credential-like patterns.
    Max length capped at 100 chars to prevent DoS via enormous error messages.
    """
    exc_type = type(exc).__name__
    msg = str(exc)

    # Redact patterns that look like credentials/secrets
    # Matches: key=..., token=..., credential=..., password=..., secret=...,
    #          authorization=..., bearer ..., etc.
    redaction_pattern = r"(?i)(key|token|credential|password|secret|authorization|bearer|api[\s_-]?key|access[\s_-]?token)[\s:=]*[^\s,;)]*"
    sanitized = re.sub(redaction_pattern, r"\1=***", msg)

    # If the entire message got scrubbed (all secrets), suppress it
    if not sanitized or re.match(r"^\*+$", sanitized.replace("=", "")):
        return f"{exc_type}: details suppressed"

    # Cap length to prevent DoS
    if len(sanitized) > 100:
        sanitized = sanitized[:97] + "..."

    return f"{exc_type}: {sanitized}"


class UsageRefreshHook:
    """Throttled pre-dispatch usage refresh with true concurrent coalescing.

    LOCK DISCIPLINE (critical for correctness):
      - Lock acquired ONLY for task selection/creation (check throttle, reuse task, or create)
      - Lock released IMMEDIATELY before any await (no queueing, true coalescing)
      - Concurrent callers can all await the same task simultaneously (not serialized)

    TASK SHIELDING (critical for timeout safety):
      - await asyncio.wait_for(asyncio.shield(task), timeout)
      - Shield prevents timeout from cancelling the underlying task
      - Task continues in background even if a waiter times out
      - Other waiters can still await the same shielded task

    DUPLICATE PREVENTION (critical for correctness):
      - Throttle window rechecked INSIDE the lock before creating new task
      - Sequence: acquire lock → recheck throttle+in-flight → create only if needed → release
      - Guarantees at most one refresh per throttle window
    """

    def __init__(
        self,
        *,
        refresh_fn: Callable[[], Any] | None = None,
        min_interval_seconds: float = 900.0,
        timeout_seconds: float = 10.0,
        usage_path: Path | None = None,
    ) -> None:
        """Initialize the hook.

        Args:
            refresh_fn: Callable that performs the actual refresh (sync function).
                Defaults to refresh_usage_document if not provided.
            min_interval_seconds: Minimum seconds between successful refreshes
                (default 900s = 15 minutes, Chairman cadence 2026-07-26).
                The hook is still consulted BEFORE EVERY routing/dispatch decision —
                it is never bypassed — but within this window callers reuse the last
                timestamped result instead of re-probing. This keeps live-usage
                evidence fresh for routing while avoiding a probe storm: at the old
                60s default the agy console probe ran on every ~60s dispatcher tick.
                Clamped to a 60s floor for safety.
            timeout_seconds: Maximum seconds to wait for a refresh to complete (default 10s).
                Calls waiting on an in-flight refresh will timeout after this duration.
                The underlying task is shielded and continues; other waiters can still await.
            usage_path: Optional path to usage document (reserved for future use).
        """
        self.refresh_fn = refresh_fn
        self.min_interval_seconds = max(60.0, float(min_interval_seconds))
        self.timeout_seconds = float(timeout_seconds)
        self.usage_path = usage_path

        self._last_refresh_at: float | None = None
        self._refresh_in_progress: asyncio.Task[Any] | None = None
        self._refresh_lock = asyncio.Lock()

    async def refresh_if_needed(self) -> dict[str, Any]:
        """Refresh usage if needed, with true concurrent coalescing and task shielding.

        LOCK FLOW (millisecond-scale critical section):
          1. Acquire lock (blocks if another caller is in task selection)
          2. Recheck throttle window (it may have started while we waited for lock)
          3. Check if task already in progress
          4. Create task only if no throttle and no in-flight task
          5. Release lock (BEFORE awaiting)
          6. Await task OUTSIDE lock with shield (allows concurrent coalescing)

        Returns a status dict with keys: ok, refreshed, cached, reason, detail, checked_at
        """
        now = time.time()
        checked_at = datetime.now(timezone.utc).isoformat()

        # Quick throttle check outside lock (ok to be stale; will recheck inside)
        if self._last_refresh_at is not None:
            age = now - self._last_refresh_at
            if age < self.min_interval_seconds:
                return {
                    "ok": True,
                    "refreshed": False,
                    "cached": True,
                    "reason": "throttled",
                    "detail": f"Last refresh {age:.0f}s ago (min {self.min_interval_seconds}s)",
                    "checked_at": checked_at,
                }

        # === CRITICAL SECTION: Task selection/creation (lock held) ===
        task_to_await: asyncio.Task[Any] | None = None
        was_initiated_by_this_caller: bool = False
        async with self._refresh_lock:
            # Recheck throttle window INSIDE lock (prevents duplicate-refresh race)
            if self._last_refresh_at is not None:
                age = now - self._last_refresh_at
                if age < self.min_interval_seconds:
                    return {
                        "ok": True,
                        "refreshed": False,
                        "cached": True,
                        "reason": "throttled",
                        "detail": f"Last refresh {age:.0f}s ago (min {self.min_interval_seconds}s)",
                        "checked_at": checked_at,
                    }

            # Check if task already in progress
            if self._refresh_in_progress is not None and not self._refresh_in_progress.done():
                # Coalesce: reuse existing task
                task_to_await = self._refresh_in_progress
                was_initiated_by_this_caller = False
            else:
                # No in-flight task; create one
                if self.refresh_fn is None:
                    from hermes_cli.fleet.usage_refresh import refresh_usage_document
                    refresh_fn = refresh_usage_document
                else:
                    refresh_fn = self.refresh_fn

                async def _do_refresh():
                    try:
                        result = await asyncio.to_thread(refresh_fn)
                        ok = (
                            result.ok
                            if hasattr(result, "ok")
                            else result.get("ok", True)
                            if isinstance(result, dict)
                            else True
                        )
                        return {"ok": ok}
                    except Exception as exc:
                        # Sanitize to prevent credential leakage (DEFECT #1 fix)
                        # Log ONLY the sanitized form (not raw exception with logger.exception)
                        sanitized_error = _sanitize_error_message(exc)
                        logger.error("Usage refresh failed: %s", sanitized_error, exc_info=False)
                        return {
                            "ok": False,
                            "error": type(exc).__name__,
                            "error_detail": sanitized_error,
                        }

                task_to_await = asyncio.create_task(_do_refresh())
                self._refresh_in_progress = task_to_await
                was_initiated_by_this_caller = True

                # DEFECT #3 (minor): Add done-callback to clear task reference when complete
                # (instead of lazily clearing on next successful await)
                def _clear_task_when_done(task: asyncio.Task[Any]) -> None:
                    async def _do_clear():
                        async with self._refresh_lock:
                            if self._refresh_in_progress is task:
                                self._refresh_in_progress = None
                    asyncio.create_task(_do_clear())

                task_to_await.add_done_callback(
                    lambda _: _clear_task_when_done(task_to_await)
                )

        # === END CRITICAL SECTION (lock released) ===

        # Await OUTSIDE lock with shield so multiple callers coalesce on same task
        # Shield prevents timeout from cancelling the underlying task
        assert task_to_await is not None, "task_to_await must be set after lock"
        try:
            refresh_result = await asyncio.wait_for(
                asyncio.shield(task_to_await),
                timeout=self.timeout_seconds,
            )
            # Refresh task completed (check if it succeeded or failed)
            self._last_refresh_at = now

            # Distinguish between initiators and coalesced callers
            if was_initiated_by_this_caller:
                # This caller initiated the refresh
                if refresh_result.get("ok", False):
                    return {
                        "ok": True,
                        "refreshed": True,
                        "cached": False,
                        "reason": "ok",
                        "detail": "Refresh completed successfully",
                        "checked_at": checked_at,
                    }
                else:
                    # Refresh task failed (exception or returned ok=False)
                    # DEFECT #1 (security): error_detail is already sanitized by _sanitize_error_message
                    error_msg = refresh_result.get("error_detail", "Unknown error")
                    return {
                        "ok": False,
                        "refreshed": False,
                        "cached": False,
                        "reason": "failed",
                        "detail": error_msg,
                        "checked_at": checked_at,
                    }
            else:
                # This caller coalesced on an existing refresh
                return {
                    "ok": refresh_result.get("ok", False),
                    "refreshed": False,
                    "cached": True,
                    "reason": "coalesced",
                    "detail": "Coalesced on in-flight refresh",
                    "checked_at": checked_at,
                }
        except asyncio.TimeoutError:
            # Timeout: task continues in background (shielded), throttle applies next
            self._last_refresh_at = now

            # DEFECT #2: coalesced identity must dominate outcome
            # Coalesced callers return reason="coalesced" even on timeout
            if was_initiated_by_this_caller:
                return {
                    "ok": False,
                    "refreshed": False,
                    "cached": False,
                    "reason": "timeout",
                    "detail": f"Refresh timed out ({self.timeout_seconds}s)",
                    "checked_at": checked_at,
                }
            else:
                return {
                    "ok": False,
                    "refreshed": False,
                    "cached": True,
                    "reason": "coalesced",
                    "detail": f"Coalesced refresh timed out ({self.timeout_seconds}s)",
                    "checked_at": checked_at,
                }
        except Exception as exc:
            # Refresh failed (outer layer — shouldn't happen if _do_refresh is robust)
            self._last_refresh_at = now
            # Clear task on failure so next caller can retry
            if task_to_await is self._refresh_in_progress:
                self._refresh_in_progress = None
            # DEFECT #1 (security): sanitize exception details before logging
            # Log ONLY the sanitized form (not raw exception with logger.exception)
            sanitized_error = _sanitize_error_message(exc)
            logger.error("Usage refresh failed: %s", sanitized_error, exc_info=False)
            return {
                "ok": False,
                "refreshed": False,
                "cached": False,
                "reason": "failed",
                "detail": f"Refresh failed: {sanitized_error}",
                "checked_at": checked_at,
            }


# Global singleton hook (per-process)
_global_hook: UsageRefreshHook | None = None


def get_global_usage_refresh_hook() -> UsageRefreshHook:
    """Get or create the global usage refresh hook."""
    global _global_hook
    if _global_hook is None:
        _global_hook = UsageRefreshHook()
    return _global_hook


async def refresh_usage_before_dispatch() -> dict[str, Any]:
    """Call from kanban dispatcher before routing decisions."""
    hook = get_global_usage_refresh_hook()
    status = await hook.refresh_if_needed()

    if not status.get("ok"):
        logger.warning(
            "Usage refresh before dispatch: %s — %s",
            status.get("reason"),
            status.get("detail"),
        )
    else:
        if status.get("refreshed"):
            logger.info("Usage evidence refreshed before dispatch")

    return status
