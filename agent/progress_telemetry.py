"""Pure progress-aware telemetry and attempt classification module.

Provides typed outcome classification (VERIFIED_PROGRESS, VERIFIED_NO_PROGRESS, UNKNOWN),
deterministic normalization, usage tracking, and atomic turn snapshots.
"""

from __future__ import annotations

import enum
import hashlib
import json
import re
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, replace
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

    @property
    def is_empty(self) -> bool:
        return not any(
            (
                self.input_tokens,
                self.output_tokens,
                self.cache_read_tokens,
                self.cache_write_tokens,
                self.reasoning_tokens,
                self.cost,
                self.model_requests,
            )
        )

    def __add__(self, other: "CanonicalUsage") -> "CanonicalUsage":
        if not isinstance(other, CanonicalUsage):
            return NotImplemented
        if self.is_empty and not other.is_empty:
            quality = other.quality
        elif other.is_empty and not self.is_empty:
            quality = self.quality
        elif self.is_empty and other.is_empty:
            quality = UsageSourceQuality.UNKNOWN
        elif self.quality == other.quality:
            quality = self.quality
        else:
            # Only two non-empty buckets can degrade one another.
            q_order = {
                UsageSourceQuality.MEASURED: 2,
                UsageSourceQuality.ESTIMATED: 1,
                UsageSourceQuality.UNKNOWN: 0,
            }
            q_map = {
                2: UsageSourceQuality.MEASURED,
                1: UsageSourceQuality.ESTIMATED,
                0: UsageSourceQuality.UNKNOWN,
            }
            quality = q_map[min(q_order[self.quality], q_order[other.quality])]

        return CanonicalUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
            cost=self.cost + other.cost,
            model_requests=self.model_requests + other.model_requests,
            quality=quality,
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
_VOLATILE_ARG_KEYS = frozenset(
    {
        "clock",
        "current_time",
        "duration",
        "duration_ms",
        "elapsed",
        "elapsed_ms",
        "heartbeat",
        "heartbeat_at",
        "now",
        "pid",
        "request_id",
        "req_id",
        "span_id",
        "time",
        "timestamp",
        "trace_id",
        "ts",
    }
)
_VOLATILE_ARG_SUFFIXES = (
    "_duration",
    "_duration_ms",
    "_elapsed",
    "_elapsed_ms",
    "_heartbeat",
    "_heartbeat_at",
    "_request_id",
    "_timestamp",
)
_MAX_FINGERPRINTS = 128


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


def _normalize_arg_value(value: Any, *, key: str = "") -> Any:
    lower_key = key.lower()
    if lower_key in _VOLATILE_ARG_KEYS or lower_key.endswith(_VOLATILE_ARG_SUFFIXES):
        return "<VOLATILE>"
    if isinstance(value, Mapping):
        return {
            str(child_key): _normalize_arg_value(child_value, key=str(child_key))
            for child_key, child_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_normalize_arg_value(item) for item in value]
    if isinstance(value, str):
        return normalize_result(value)
    return value


def canonical_args_hash(args: Mapping[str, Any] | None) -> str:
    """Compute a deterministic hash after removing volatile argument values."""
    if not args:
        return hashlib.sha256(b"{}").hexdigest()
    try:
        normalized = _normalize_arg_value(args)
        compact = json.dumps(
            normalized,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    except Exception:
        compact = normalize_result(str(args))
    return hashlib.sha256(compact.encode("utf-8")).hexdigest()


def _mechanically_verified_file_mutation(tool_name: str, result_text: Optional[str]) -> bool:
    """Require the existing landed-result contract plus a non-empty effect list."""
    if not file_mutation_result_landed(tool_name, result_text or ""):
        return False
    try:
        payload = json.loads(result_text or "")
    except (TypeError, ValueError):
        return False
    files_modified = payload.get("files_modified") if isinstance(payload, Mapping) else None
    return isinstance(files_modified, list) and bool(files_modified)


class ProgressTelemetry:
    """Turn/Session progress telemetry accumulator."""

    def __init__(self, session_id: str = "", context_id: str = "") -> None:
        self.session_id = session_id
        self.context_id = context_id or session_id
        self._usage_baseline = CanonicalUsage()
        self.reset_for_turn()

    def reset_for_turn(self, cumulative_usage: Optional[CanonicalUsage] = None) -> None:
        """Reset event evidence and snapshot the cumulative usage baseline."""
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
        self._fingerprint_counts: OrderedDict[str, int] = OrderedDict()
        self._usage_baseline = cumulative_usage or CanonicalUsage()

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

        # The bool is retained for call-site compatibility, but result evidence
        # remains authoritative: a generic success or no-op is never progress.
        _ = file_mutation_landed
        landed = _mechanically_verified_file_mutation(tool_name, result_text)
        fingerprint = f"{attempt_key}:{result_h}"
        prior_count = self._fingerprint_counts.get(fingerprint, 0)

        if is_failure:
            outcome = AttemptOutcome.UNKNOWN
        elif landed:
            outcome = AttemptOutcome.VERIFIED_PROGRESS
        elif prior_count > 0:
            outcome = AttemptOutcome.VERIFIED_NO_PROGRESS
        else:
            outcome = AttemptOutcome.UNKNOWN

        if outcome == AttemptOutcome.VERIFIED_PROGRESS:
            self.progress_seq += 1
            self.no_progress_streak = 0
            self._fingerprint_counts.clear()
        elif outcome == AttemptOutcome.VERIFIED_NO_PROGRESS:
            repeat_count = prior_count
            self.no_progress_streak = max(self.no_progress_streak, repeat_count)

        if not is_failure and not landed:
            self._fingerprint_counts[fingerprint] = prior_count + 1
            self._fingerprint_counts.move_to_end(fingerprint)
            while len(self._fingerprint_counts) > _MAX_FINGERPRINTS:
                self._fingerprint_counts.popitem(last=False)

        if is_failure:
            sig = failure_sig or f"{tool_name}:{result_h[:12]}"
            self.failure_seq += 1
            if is_retryable:
                self.failure_streak = 0
                self.last_failure_sig = None
                self.is_last_failure_retryable = True
            else:
                if sig == self.last_failure_sig:
                    self.failure_streak += 1
                else:
                    self.failure_streak = 1
                    self.last_failure_sig = sig
                self.is_last_failure_retryable = False
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

    def _usage_delta(
        self,
        current_usage: CanonicalUsage,
        *,
        model_requests: int,
    ) -> CanonicalUsage:
        baseline = self._usage_baseline
        numeric_fields = (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "reasoning_tokens",
            "cost",
        )
        if any(
            getattr(current_usage, field_name) < getattr(baseline, field_name)
            for field_name in numeric_fields
        ):
            self._usage_baseline = current_usage
            return CanonicalUsage(
                model_requests=max(0, int(model_requests)),
                quality=UsageSourceQuality.UNKNOWN,
            )

        delta = CanonicalUsage(
            input_tokens=max(0, current_usage.input_tokens - baseline.input_tokens),
            output_tokens=max(0, current_usage.output_tokens - baseline.output_tokens),
            cache_read_tokens=max(
                0,
                current_usage.cache_read_tokens - baseline.cache_read_tokens,
            ),
            cache_write_tokens=max(
                0,
                current_usage.cache_write_tokens - baseline.cache_write_tokens,
            ),
            reasoning_tokens=max(
                0,
                current_usage.reasoning_tokens - baseline.reasoning_tokens,
            ),
            cost=max(0.0, current_usage.cost - baseline.cost),
            model_requests=max(0, int(model_requests)),
        )
        provider_tokens_supplied = any(
            (
                delta.input_tokens,
                delta.output_tokens,
                delta.cache_read_tokens,
                delta.cache_write_tokens,
                delta.reasoning_tokens,
            )
        )
        return replace(
            delta,
            quality=(
                UsageSourceQuality.MEASURED
                if provider_tokens_supplied
                else UsageSourceQuality.UNKNOWN
            ),
        )

    def get_activity_snapshot(
        self,
        *,
        current_usage: Optional[CanonicalUsage] = None,
        model_requests: Optional[int] = None,
        cost_status: str = "unknown",
        cost_source: str = "none",
        usage_quality: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Return an atomic turn-scoped snapshot separating liveness from progress."""
        if current_usage is None:
            usage = self.usage
            if model_requests is not None:
                usage = replace(usage, model_requests=max(0, int(model_requests)))
        else:
            usage = self._usage_delta(
                current_usage,
                model_requests=model_requests or 0,
            )
        if usage_quality is not None and not usage.is_empty:
            try:
                explicit_quality = UsageSourceQuality(str(usage_quality).lower())
            except ValueError:
                explicit_quality = UsageSourceQuality.UNKNOWN
            usage = replace(usage, quality=explicit_quality)
        return {
            "session_id": self.session_id,
            "context_id": self.context_id,
            "attempt_seq": self.attempt_seq,
            "progress_seq": self.progress_seq,
            "no_progress_streak": self.no_progress_streak,
            "last_outcome": self.last_outcome.value,
            "failure_seq": self.failure_seq,
            "failure_streak": self.failure_streak,
            "is_non_retryable_failure": (
                self.failure_streak > 0 and not self.is_last_failure_retryable
            ),
            "last_attempt_key": self.last_attempt_key,
            "last_result_hash": self.last_result_hash,
            "last_attempt_tool": self.last_attempt_tool,
            "usage": {
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cache_read_tokens": usage.cache_read_tokens,
                "cache_write_tokens": usage.cache_write_tokens,
                "reasoning_tokens": usage.reasoning_tokens,
                "cost": usage.cost,
                "model_requests": usage.model_requests,
                "quality": usage.quality.value,
                "cost_status": cost_status,
                "cost_source": cost_source,
            },
        }
