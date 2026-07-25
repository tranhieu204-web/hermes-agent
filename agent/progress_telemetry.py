"""Pure progress-aware telemetry and attempt classification module.

Provides typed outcome classification (VERIFIED_PROGRESS, VERIFIED_NO_PROGRESS, UNKNOWN),
deterministic normalization, usage tracking, and atomic turn snapshots.
"""

from __future__ import annotations

import enum
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from agent.tool_result_classification import file_mutation_result_landed


class AttemptOutcome(str, enum.Enum):
    """Tri-state classification for a completed attempt."""

    VERIFIED_PROGRESS = "verified_progress"
    VERIFIED_NO_PROGRESS = "verified_no_progress"
    UNKNOWN = "unknown"


class UsageSourceQuality(str, enum.Enum):
    """Quality indicator for usage counters."""

    MEASURED = "measured"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CanonicalUsage:
    """Normalized provider-neutral usage counters."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    cost: float = 0.0
    model_requests: int = 0
    quality: UsageSourceQuality = UsageSourceQuality.UNKNOWN

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read_tokens + self.cache_write_tokens

    def __add__(self, other: "CanonicalUsage") -> "CanonicalUsage":
        if not isinstance(other, CanonicalUsage):
            return NotImplemented
        # Quality degrades to lower confidence
        q_order = {UsageSourceQuality.MEASURED: 2, UsageSourceQuality.ESTIMATED: 1, UsageSourceQuality.UNKNOWN: 0}
        q_val = min(q_order[self.quality], q_order[other.quality])
        q_map = {2: UsageSourceQuality.MEASURED, 1: UsageSourceQuality.ESTIMATED, 0: UsageSourceQuality.UNKNOWN}
        
        return CanonicalUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
            cost=self.cost + other.cost,
            model_requests=self.model_requests + other.model_requests,
            quality=q_map[q_val],
        )


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?")
_TIMESTAMP_RE = re.compile(r"\b\d{8,12}(\.\d+)?\b")
_TIME_LABEL_RE = re.compile(r"\b(time|timestamp|ts|duration|clock|now)\s*[:=]\s*\S+", re.IGNORECASE)
_DURATION_RE = re.compile(r"\b\d+(\.\d+)?\s*(ms|seconds|second|sec|s|m|min|minutes)\b", re.IGNORECASE)
_UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_REQ_ID_RE = re.compile(r"\breq_[0-9a-zA-Z]+\b")
_PID_RE = re.compile(r"\bpid\s*[:=]?\s*\d+\b", re.IGNORECASE)
_TEMP_PATH_RE = re.compile(r"([a-zA-Z]:[\\/][^:\n]*?[\\/](Temp|tmp)[\\/][^\s\n]+|/tmp/[^\s\n]+)", re.IGNORECASE)


def normalize_result(result_text: Optional[str]) -> str:
    """Normalize tool results by stripping volatile markers before hashing."""
    if not result_text:
        return ""
    text = _ANSI_RE.sub("", result_text)
    text = _ISO_DATE_RE.sub("<TIMESTAMP>", text)
    text = _TIME_LABEL_RE.sub("<TIME_LABEL>", text)
    text = _TIMESTAMP_RE.sub("<TIMESTAMP>", text)
    text = _DURATION_RE.sub("<DURATION>", text)
    text = _UUID_RE.sub("<UUID>", text)
    text = _REQ_ID_RE.sub("<REQ_ID>", text)
    text = _PID_RE.sub("<PID>", text)
    text = _TEMP_PATH_RE.sub("<TMP_PATH>", text)
    return text.strip()


def canonical_args_hash(args: Mapping[str, Any] | None) -> str:
    """Compute deterministic sha256 hash of sorted JSON args."""
    if not args:
        return hashlib.sha256(b"{}").hexdigest()
    try:
        compact = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:
        compact = str(args)
    return hashlib.sha256(compact.encode("utf-8")).hexdigest()


class ProgressTelemetry:
    """Turn/Session progress telemetry accumulator."""

    def __init__(self, session_id: str = "", context_id: str = "") -> None:
        self.session_id = session_id
        self.context_id = context_id or session_id
        self.attempt_seq: int = 0
        self.progress_seq: int = 0
        self.last_attempt_key: Optional[str] = None
        self.last_result_hash: Optional[str] = None
        self.last_outcome: AttemptOutcome = AttemptOutcome.UNKNOWN
        self.no_progress_streak: int = 0
        self.failure_seq: int = 0
        self.last_failure_sig: Optional[str] = None
        self.failure_streak: int = 0
        self.is_last_failure_retryable: bool = True
        self.usage: CanonicalUsage = CanonicalUsage()
        self.last_attempt_tool: str = ""

    def record_attempt_completion(
        self,
        tool_name: str,
        args: Optional[Dict[str, Any]],
        result_text: Optional[str],
        is_failure: bool = False,
        file_mutation_landed: bool = False,
        failure_sig: Optional[str] = None,
        is_retryable: bool = True,
    ) -> AttemptOutcome:
        """Record a completed tool attempt and return its tri-state outcome."""
        self.attempt_seq += 1
        self.last_attempt_tool = tool_name
        args_h = canonical_args_hash(args)
        attempt_key = f"{tool_name}:{args_h}"

        normalized = normalize_result(result_text)
        result_h = hashlib.sha256(normalized.encode("utf-8")).hexdigest()

        # Check landed file mutation first
        landed = file_mutation_landed or file_mutation_result_landed(tool_name, result_text or "")
        
        if landed:
            outcome = AttemptOutcome.VERIFIED_PROGRESS
        elif attempt_key == self.last_attempt_key and result_h == self.last_result_hash:
            outcome = AttemptOutcome.VERIFIED_NO_PROGRESS
        else:
            outcome = AttemptOutcome.UNKNOWN

        # Update streaks based on non-negotiable semantics
        if outcome == AttemptOutcome.VERIFIED_PROGRESS:
            self.progress_seq += 1
            self.no_progress_streak = 0
        elif outcome == AttemptOutcome.VERIFIED_NO_PROGRESS:
            self.no_progress_streak += 1
        else:
            # UNKNOWN outcome does NOT erase existing same-attempt/same-result no-progress pattern!
            pass

        # Failure tracking
        if is_failure:
            sig = failure_sig or f"{tool_name}:{result_h[:12]}"
            self.failure_seq += 1
            if sig == self.last_failure_sig:
                self.failure_streak += 1
            else:
                self.failure_streak = 1
                self.last_failure_sig = sig
            self.is_last_failure_retryable = is_retryable
        else:
            self.failure_streak = 0
            self.last_failure_sig = None
            self.is_last_failure_retryable = True

        self.last_attempt_key = attempt_key
        self.last_result_hash = result_h
        self.last_outcome = outcome
        return outcome

    def update_usage(self, new_usage: CanonicalUsage) -> None:
        """Update or accumulate usage counters."""
        self.usage = self.usage + new_usage

    def get_activity_snapshot(self) -> Dict[str, Any]:
        """Return an atomic turn-scoped snapshot separating liveness from progress."""
        return {
            "session_id": self.session_id,
            "context_id": self.context_id,
            "attempt_seq": self.attempt_seq,
            "progress_seq": self.progress_seq,
            "no_progress_streak": self.no_progress_streak,
            "last_outcome": self.last_outcome.value,
            "failure_seq": self.failure_seq,
            "failure_streak": self.failure_streak,
            "last_attempt_key": self.last_attempt_key,
            "last_result_hash": self.last_result_hash,
            "last_attempt_tool": self.last_attempt_tool,
            "usage": {
                "input_tokens": self.usage.input_tokens,
                "output_tokens": self.usage.output_tokens,
                "cache_read_tokens": self.usage.cache_read_tokens,
                "cache_write_tokens": self.usage.cache_write_tokens,
                "reasoning_tokens": self.usage.reasoning_tokens,
                "cost": self.usage.cost,
                "model_requests": self.usage.model_requests,
                "quality": self.usage.quality.value,
            },
        }
