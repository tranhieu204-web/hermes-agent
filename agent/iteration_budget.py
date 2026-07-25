"""Per-agent iteration budget — thread-safe consume/refund/extend counter."""

from __future__ import annotations

import threading


class IterationBudget:
    """Thread-safe iteration counter for an agent."""

    def __init__(self, max_total: int):
        self.max_total = max_total
        self._used = 0
        self.extensions_count = 0
        self._lock = threading.Lock()

    def consume(self) -> bool:
        """Try to consume one iteration. Returns True if allowed."""
        with self._lock:
            if self._used >= self.max_total:
                return False
            self._used += 1
            return True

    def refund(self) -> None:
        """Give back one iteration (e.g. for execute_code turns)."""
        with self._lock:
            if self._used > 0:
                self._used -= 1

    def extend_grant(self, grant_size: int) -> int:
        """Thread-safely extend the iteration budget by grant_size."""
        with self._lock:
            amt = max(1, int(grant_size))
            self.max_total += amt
            self.extensions_count += 1
            return self.max_total

    def extend(self, amount: int) -> int:
        """Alias for extend_grant."""
        return self.extend_grant(amount)

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    @property
    def remaining(self) -> int:
        with self._lock:
            return max(0, self.max_total - self._used)


__all__ = ["IterationBudget"]
