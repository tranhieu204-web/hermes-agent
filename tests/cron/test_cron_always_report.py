"""Regression coverage for the opt-in always-report cron policy."""

from unittest.mock import patch

from hermes_cli.config import save_config


def test_always_report_converts_silent_success_to_outcome(monkeypatch):
    from cron import scheduler

    monkeypatch.setattr(
        scheduler,
        "load_config_readonly",
        lambda: {"cron": {"always_report": True}},
    )

    job = {"id": "job123", "name": "Daily audit"}

    assert scheduler._apply_cron_reporting_policy(
        job, "[SILENT]", success=True
    ) == "Cron task 'Daily audit' ran successfully. Nothing new to report."


def test_always_report_preserves_real_content_and_failures(monkeypatch):
    from cron import scheduler

    monkeypatch.setattr(
        scheduler,
        "load_config_readonly",
        lambda: {"cron": {"always_report": True}},
    )

    job = {"id": "job123", "name": "Daily audit"}

    assert scheduler._apply_cron_reporting_policy(
        job, "2 findings", success=True
    ) == "2 findings"
    assert scheduler._apply_cron_reporting_policy(
        job, "", success=False
    ) == ""


def test_default_policy_preserves_silent_marker(monkeypatch):
    from cron import scheduler

    monkeypatch.setattr(scheduler, "load_config_readonly", lambda: {})

    assert scheduler._apply_cron_reporting_policy(
        {"id": "job123"}, "[SILENT]", success=True
    ) == "[SILENT]"


def test_always_report_reads_real_profile_config():
    from cron import scheduler

    save_config({"cron": {"always_report": True}}, strip_defaults=False)

    assert scheduler._cron_always_report_enabled() is True


def test_always_report_real_loader_isolated_across_profile_homes(tmp_path, monkeypatch):
    from cron import scheduler

    profile_a = tmp_path / "profile-a"
    profile_b = tmp_path / "profile-b"

    monkeypatch.setenv("HERMES_HOME", str(profile_a))
    save_config({"cron": {"always_report": False}}, strip_defaults=False)
    assert scheduler._cron_always_report_enabled() is False

    monkeypatch.setenv("HERMES_HOME", str(profile_b))
    save_config({"cron": {"always_report": True}}, strip_defaults=False)
    assert scheduler._cron_always_report_enabled() is True

    monkeypatch.setenv("HERMES_HOME", str(profile_a))
    assert scheduler._cron_always_report_enabled() is False


def test_always_report_reaches_scheduler_delivery(monkeypatch):
    from cron import scheduler

    monkeypatch.setattr(
        scheduler,
        "load_config_readonly",
        lambda: {"cron": {"always_report": True}},
    )
    job = {
        "id": "job123",
        "name": "Daily audit",
        "deliver": "discord:456",
    }

    with patch("cron.scheduler.get_due_jobs", return_value=[job]), \
         patch("cron.scheduler.claim_job_for_fire", return_value=True), \
         patch("cron.scheduler.run_job", return_value=(True, "# output", "[SILENT]", None)), \
         patch("cron.scheduler.save_job_output", return_value="/tmp/out.md"), \
         patch("cron.scheduler._deliver_result", return_value=None) as deliver_mock, \
         patch("cron.scheduler.mark_job_run"):
        scheduler.tick(verbose=False)

    deliver_mock.assert_called_once()
    assert deliver_mock.call_args.args[1] == (
        "Cron task 'Daily audit' ran successfully. Nothing new to report."
    )


def test_always_report_uses_one_config_snapshot_per_run(monkeypatch):
    from cron import scheduler

    policy_read = patch(
        "cron.scheduler._cron_always_report_enabled",
        side_effect=[True, False],
    )
    job = {
        "id": "job123",
        "name": "Daily audit",
        "deliver": "discord:456",
    }

    with policy_read as read_mock, \
         patch("cron.scheduler.get_due_jobs", return_value=[job]), \
         patch("cron.scheduler.claim_job_for_fire", return_value=True), \
         patch("cron.scheduler.run_job", return_value=(True, "# output", "[SILENT]", None)), \
         patch("cron.scheduler.save_job_output", return_value="/tmp/out.md"), \
         patch("cron.scheduler._deliver_result", return_value=None) as deliver_mock, \
         patch("cron.scheduler.mark_job_run"):
        scheduler.tick(verbose=False)

    read_mock.assert_called_once_with()
    deliver_mock.assert_called_once()
    assert deliver_mock.call_args.args[1] == (
        "Cron task 'Daily audit' ran successfully. Nothing new to report."
    )


def test_always_report_delivers_empty_response_as_failure(monkeypatch):
    from cron import scheduler

    monkeypatch.setattr(
        scheduler,
        "load_config_readonly",
        lambda: {"cron": {"always_report": True}},
    )
    job = {
        "id": "job123",
        "name": "Daily audit",
        "deliver": "discord:456",
    }

    with patch("cron.scheduler.get_due_jobs", return_value=[job]), \
         patch("cron.scheduler.claim_job_for_fire", return_value=True), \
         patch("cron.scheduler.run_job", return_value=(True, "# output", "", None)), \
         patch("cron.scheduler.save_job_output", return_value="/tmp/out.md"), \
         patch("cron.scheduler._deliver_result", return_value=None) as deliver_mock, \
         patch("cron.scheduler.mark_job_run") as mark_mock:
        scheduler.tick(verbose=False)

    deliver_mock.assert_called_once()
    delivered = deliver_mock.call_args.args[1]
    assert "ran successfully" not in delivered
    assert "empty response" in delivered
    mark_mock.assert_called_once_with(
        "job123",
        False,
        "Agent completed but produced empty response (model error, timeout, or misconfiguration)",
        delivery_error=None,
    )


def test_always_report_delivers_acknowledged_empty_response_failure(monkeypatch):
    from cron import scheduler

    monkeypatch.setattr(
        scheduler,
        "load_config_readonly",
        lambda: {"cron": {"always_report": True}},
    )
    job = {
        "id": "job123",
        "name": "Daily audit",
        "deliver": "discord:456",
    }

    with patch("cron.scheduler.get_due_jobs", return_value=[job]), \
         patch("cron.scheduler.claim_job_for_fire", return_value=True), \
         patch("cron.scheduler.run_job", return_value=(True, "# output", "", None)), \
         patch("cron.scheduler.save_job_output", return_value="/tmp/out.md"), \
         patch("cron.scheduler._upsert_incident_for_failure", return_value=(True, "incident-1")), \
         patch("cron.scheduler._deliver_result", return_value=None) as deliver_mock, \
         patch("cron.scheduler.mark_job_run") as mark_mock:
        scheduler.tick(verbose=False)

    deliver_mock.assert_called_once()
    assert "failed" in deliver_mock.call_args.args[1].lower()
    mark_mock.assert_called_once_with(
        "job123",
        False,
        "Agent completed but produced empty response (model error, timeout, or misconfiguration)",
        delivery_error=None,
    )


def test_always_report_delivers_acknowledged_blocked_config_failure(monkeypatch):
    from cron import scheduler

    monkeypatch.setattr(
        scheduler,
        "load_config_readonly",
        lambda: {"cron": {"always_report": True}},
    )
    job = {
        "id": "job123",
        "name": "Daily audit",
        "deliver": "discord:456",
    }
    error = f"{scheduler.BLOCKED_CONFIG_SILENT_MARKER} provider unavailable"

    with patch("cron.scheduler.get_due_jobs", return_value=[job]), \
         patch("cron.scheduler.claim_job_for_fire", return_value=True), \
         patch("cron.scheduler.run_job", return_value=(False, "# output", "", error)), \
         patch("cron.scheduler.save_job_output", return_value="/tmp/out.md"), \
         patch("cron.scheduler._deliver_result", return_value=None) as deliver_mock, \
         patch("cron.scheduler.mark_job_run"):
        scheduler.tick(verbose=False)

    deliver_mock.assert_called_once()
    assert "provider unavailable" in deliver_mock.call_args.args[1]


def test_always_report_delivers_failure_before_automatic_retry(monkeypatch):
    from cron import scheduler

    monkeypatch.setattr(
        scheduler,
        "load_config_readonly",
        lambda: {"cron": {"always_report": True}},
    )
    job = {
        "id": "job123",
        "name": "Daily audit",
        "deliver": "discord:456",
        "_model_unreachable": True,
    }

    with patch("cron.scheduler.get_due_jobs", return_value=[job]), \
         patch("cron.scheduler.claim_job_for_fire", return_value=True), \
         patch("cron.scheduler.run_job", return_value=(False, "# output", "", "provider unreachable")), \
         patch("cron.scheduler.save_job_output", return_value="/tmp/out.md"), \
         patch("cron.scheduler._deliver_result", return_value=None) as deliver_mock, \
         patch("cron.scheduler.mark_job_run"), \
         patch("cron.unreachable_retry.will_retry", return_value=True):
        scheduler.tick(verbose=False)

    deliver_mock.assert_called_once()
    assert "provider unreachable" in deliver_mock.call_args.args[1]
