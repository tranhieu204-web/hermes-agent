from __future__ import annotations

import json
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli.fleet.adapters.live_routes import (
    AntigravityAdapter,
    inspect_agy_subscription_receipt,
)
from hermes_cli.fleet.profiles import profile_map
from hermes_cli.fleet.types import (
    AdapterRequest,
    OverageState,
    Qualification,
    ReasonCode,
)


NOW = datetime(2026, 7, 24, tzinfo=timezone.utc)
CANONICAL_MODEL_ID = "gemini-3.1-pro-high"
DISPLAY_MODEL_LABEL = "Gemini 3.1 Pro (High)"
RECEIPT_LINE = (
    "I0724 11:02:16.509256 40296 model_config_manager.go:272] "
    f'Propagating selected model override to backend: label="{DISPLAY_MODEL_LABEL}"'
    "\napplyAuthResult: authMethod=consumer, quotaProject="
    "\nURL: https://daily-cloudcode-pa.googleapis.com/"
    "v1internal:streamGenerateContent?alt=sse"
)


def _request(tmp_path: Path, *, model: str = CANONICAL_MODEL_ID) -> AdapterRequest:
    profile = profile_map()["antigravity"]
    if model != CANONICAL_MODEL_ID:
        profile = replace(profile, ordered_models=(model,))
    return AdapterRequest(
        task_id="agy-model-label",
        cwd=tmp_path,
        prompt="bounded test task",
        profile=profile,
        model=model,
        effort="medium",
    )


def _qualification(request: AdapterRequest) -> Qualification:
    executable = str(Path(sys.executable).resolve())
    return Qualification(
        qualified=True,
        captured_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        auth_kind="cli_subscription",
        auth_source="antigravity:agy-models-policy",
        overage_disabled=True,
        provider_id=request.profile.provider_id,
        models=request.profile.ordered_models,
        efforts=request.profile.supported_efforts,
        fast_off_supported=True,
        capabilities=request.profile.capabilities,
        executable=executable,
        version="agy 1.2.3",
        evidence_id="qualification:agy",
        subscription_only_proven=True,
        paid_fallback_absent=True,
        overage_state=OverageState.OFF,
    )


def _write_receipt(argv: list[str], content: str | bytes = RECEIPT_LINE) -> Path:
    log_path = Path(argv[argv.index("--log-file") + 1])
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        log_path.write_bytes(content)
    else:
        log_path.write_text(content, encoding="utf-8")
    return log_path


def test_agy_uses_display_label_positional_prompt_and_unique_profile_logs(
    tmp_path, monkeypatch
):
    home = tmp_path / "profile-home"
    secret = "AGY_SECRET_CANARY_6fce17"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("GEMINI_API_KEY", secret)
    monkeypatch.setenv("GOOGLE_API_KEY", secret)
    calls: list[tuple[list[str], dict[str, object], Path]] = []

    def process(argv, **kwargs):
        log_path = _write_receipt(argv)
        calls.append((argv, kwargs, log_path))
        return SimpleNamespace(
            returncode=0,
            stdout="antigravity complete",
            stderr="",
        )

    request = _request(tmp_path)
    qualification = _qualification(request)
    adapter = AntigravityAdapter(sys.executable, run_process=process)

    first = adapter.execute(request, qualification)
    second = adapter.execute(request, qualification)

    assert first.ok and second.ok
    assert first.model_id == second.model_id == CANONICAL_MODEL_ID
    assert first.metadata["route_proof"]["requested_model_id"] == CANONICAL_MODEL_ID
    assert first.metadata["route_proof"]["served_model_id"] == CANONICAL_MODEL_ID
    assert first.metadata["route_proof"]["served_model_label"] == DISPLAY_MODEL_LABEL
    assert len(calls) == 2
    log_paths = []
    for argv, kwargs, log_path in calls:
        assert argv[argv.index("-p") + 1] == request.prompt
        assert argv[argv.index("--model") + 1] == DISPLAY_MODEL_LABEL
        assert "--effort" not in argv
        assert "--log-file" in argv
        assert "input" not in kwargs
        assert secret not in json.dumps(argv)
        assert secret not in json.dumps(kwargs["env"], sort_keys=True)
        assert log_path.is_relative_to(home)
        assert not log_path.exists()
        log_paths.append(log_path)
    assert log_paths[0] != log_paths[1]
    assert secret not in json.dumps(first.metadata, sort_keys=True)


@pytest.mark.parametrize(
    ("log_content", "expected_reason", "expected_status"),
    [
        (
            (
                "I0724 11:02:16.509256 40296 model_config_manager.go:272] "
                'Propagating selected model override to backend: '
                'label="Gemini 3.6 Flash (High)"'
            ),
            ReasonCode.MODEL_MISMATCH,
            "served_model_mismatch",
        ),
        ("unrelated log line", ReasonCode.MALFORMED_OUTPUT, "missing_receipt"),
        (b"\xff\xfe", ReasonCode.MALFORMED_OUTPUT, "malformed_log"),
        (None, ReasonCode.MALFORMED_OUTPUT, "missing_log"),
    ],
)
def test_agy_receipt_failures_are_closed_and_sanitized(
    tmp_path,
    monkeypatch,
    log_content,
    expected_reason,
    expected_status,
):
    home = tmp_path / "profile-home"
    secret = "AGY_SECRET_CANARY_0f2a8b"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("GEMINI_API_KEY", secret)

    def process(argv, **_kwargs):
        if log_content is not None:
            _write_receipt(argv, log_content)
        return SimpleNamespace(
            returncode=0,
            stdout="must not be reported complete",
            stderr=secret,
        )

    request = _request(tmp_path)
    result = AntigravityAdapter(
        sys.executable, run_process=process
    ).execute(request, _qualification(request))

    assert not result.ok
    assert result.reason is expected_reason
    assert result.output == ""
    assert result.metadata["receipt_check"]["status"] == expected_status
    assert secret not in json.dumps(result.metadata, sort_keys=True)
    assert not list(home.rglob("*.log"))
    evidence_files = list((home / "fleet" / "evidence" / "agy").glob("*.json"))
    assert len(evidence_files) == 1
    assert (
        result.metadata["receipt_check"]["evidence_id"]
        == f"agy:{evidence_files[0].name}"
    )
    evidence = evidence_files[0].read_text(encoding="utf-8")
    assert expected_status in evidence
    assert secret not in evidence


def test_agy_unknown_backend_label_is_not_echoed_to_failure_evidence(
    tmp_path, monkeypatch
):
    home = tmp_path / "profile-home"
    unknown_label = "AGY_UNKNOWN_LABEL_SECRET_CANARY_8f14e45fceea"
    monkeypatch.setenv("HERMES_HOME", str(home))

    def process(argv, **_kwargs):
        _write_receipt(
            argv,
            (
                "applyAuthResult: authMethod=consumer, quotaProject=\n"
                "URL: https://daily-cloudcode-pa.googleapis.com/"
                "v1internal:streamGenerateContent?alt=sse\n"
                "Propagating selected model override to backend: "
                f'label="{unknown_label}"'
            ),
        )
        return SimpleNamespace(returncode=0, stdout="must fail closed", stderr="")

    request = _request(tmp_path)
    result = AntigravityAdapter(
        sys.executable, run_process=process
    ).execute(request, _qualification(request))

    assert result.ok is False
    assert result.reason is ReasonCode.MODEL_MISMATCH
    assert result.output == ""
    assert result.metadata["receipt_check"]["status"] == "unsupported_served_model"
    assert not list(home.rglob("*.log"))
    evidence_files = list((home / "fleet" / "evidence" / "agy").glob("*.json"))
    assert len(evidence_files) == 1
    surfaces = {
        "returned metadata": json.dumps(result.metadata, sort_keys=True),
        "persisted receipt": evidence_files[0].read_text(encoding="utf-8"),
    }
    leaks = [name for name, content in surfaces.items() if unknown_label in content]
    assert leaks == []


def test_agy_receipt_records_observed_backend_model_before_comparison(tmp_path):
    """A mismatch receipt preserves what AGY selected, never the requested identity."""

    log_path = tmp_path / "agy.log"
    log_path.write_text(
        "\n".join(
            (
                "applyAuthResult: authMethod=consumer, quotaProject=",
                "URL: https://daily-cloudcode-pa.googleapis.com/"
                "v1internal:streamGenerateContent?alt=sse",
                "Propagating selected model override to backend: "
                'label="Gemini 3.6 Flash (High)"',
            )
        ),
        encoding="utf-8",
    )

    receipt = inspect_agy_subscription_receipt(
        log_path,
        canonical_model_id=CANONICAL_MODEL_ID,
        expected_display_label=DISPLAY_MODEL_LABEL,
    )

    assert receipt["status"] == "served_model_mismatch"
    assert receipt["served_model_id"] == "gemini-3.6-flash-high"
    assert receipt["served_model_label"] == "Gemini 3.6 Flash (High)"
    assert receipt["canonical_model_id"] == CANONICAL_MODEL_ID


def test_agy_unknown_canonical_model_fails_before_process_launch(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile-home"))
    calls = []
    request = _request(tmp_path, model="gemini-3.6-flash")

    def process(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("unsupported model must not launch AGY")

    result = AntigravityAdapter(
        sys.executable, run_process=process
    ).execute(request, _qualification(request))

    assert not result.ok
    assert result.reason is ReasonCode.MODEL_MISMATCH
    assert result.metadata["receipt_check"]["status"] == "unsupported_model"
    assert calls == []


def test_agy_nonzero_exit_cannot_report_completion(tmp_path, monkeypatch):
    home = tmp_path / "profile-home"
    monkeypatch.setenv("HERMES_HOME", str(home))

    def process(argv, **_kwargs):
        _write_receipt(argv)
        return SimpleNamespace(returncode=7, stdout="not complete", stderr="")

    request = _request(tmp_path)
    result = AntigravityAdapter(
        sys.executable, run_process=process
    ).execute(request, _qualification(request))

    assert not result.ok
    assert result.reason is ReasonCode.EXECUTION_FAILED
    assert result.output == ""
    assert result.metadata["receipt_check"]["status"] == "process_exit_nonzero"
    assert not list(home.rglob("*.log"))
