from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from typer.testing import CliRunner

from simplismart_deployment_cli import cli

runner = CliRunner()


class FakeManager:
    def __init__(self, settings: Any) -> None:
        self.settings = settings

    def list(self, **kwargs: Any) -> list[dict[str, str]]:
        return [{"deployment_id": "deployment-1", "status": "DEPLOYED"}]

    def get(self, deployment_id: str) -> dict[str, Any]:
        return {
            "deployment_id": deployment_id,
            "env_variables": {"PRIVATE_API_KEY": "super-secret-value"},
            "nested": {"access_token": "another-secret"},
            "status": "DEPLOYED",
        }

    def start(self, deployment_id: str, **kwargs: Any) -> dict[str, Any]:
        return {
            "deployment_id": deployment_id,
            "action": "start",
            "changed": True,
            "result": {"accepted": True},
        }

    def stop(self, deployment_id: str, **kwargs: Any) -> dict[str, Any]:
        return {
            "deployment_id": deployment_id,
            "action": "stop",
            "changed": True,
            "result": {"accepted": True},
        }

    def health(self, deployment_id: str) -> dict[str, str]:
        return {"data": "Progressing", "deployment_id": deployment_id}

    def wait_for_status(self, deployment_id: str, **kwargs: Any) -> dict[str, Any]:
        return {
            "deployment": {"deployment_id": deployment_id, "status": "DEPLOYED"},
            "health": {"data": "Healthy"},
        }

    def set_schedule(self, deployment_id: str, **kwargs: Any) -> dict[str, Any]:
        return {
            "deployment_id": deployment_id,
            "action": "schedule_set",
            "changed": True,
            "result": kwargs,
        }


def test_list_emits_machine_readable_json(monkeypatch: Any) -> None:
    monkeypatch.setattr(cli, "DeploymentManager", FakeManager)

    result = runner.invoke(
        cli.app,
        ["list"],
        env={"SIMPLISMART_PG_TOKEN": "test-token"},
    )

    assert result.exit_code == 0
    assert '"deployment_id": "deployment-1"' in result.stdout
    assert '"status": "DEPLOYED"' in result.stdout


def test_health_gate_has_distinct_unhealthy_exit_code(monkeypatch: Any) -> None:
    monkeypatch.setattr(cli, "DeploymentManager", FakeManager)

    result = runner.invoke(
        cli.app,
        ["health", "deployment-1", "--require-healthy"],
        env={"SIMPLISMART_PG_TOKEN": "test-token"},
    )

    assert result.exit_code == cli.EXIT_UNHEALTHY
    assert '"data": "Progressing"' in result.stdout


def test_invalid_status_is_a_usage_failure() -> None:
    result = runner.invoke(
        cli.app,
        ["list", "--status", "running"],
        env={"SIMPLISMART_PG_TOKEN": "test-token"},
    )

    assert result.exit_code == cli.EXIT_CONFIG
    assert '"exit_code": 2' in result.stderr


def test_start_wait_emits_one_final_json_document(monkeypatch: Any) -> None:
    monkeypatch.setattr(cli, "DeploymentManager", FakeManager)

    result = runner.invoke(
        cli.app,
        [
            "start",
            "deployment-1",
            "--wait",
            "--wait-timeout",
            "30",
            "--poll-interval",
            "1",
        ],
        env={"SIMPLISMART_PG_TOKEN": "test-token"},
    )

    assert result.exit_code == 0
    assert result.stderr == ""
    assert '"action": "start"' in result.stdout
    assert '"final_state"' in result.stdout
    assert '"data": "Healthy"' in result.stdout


def test_wait_failure_has_distinct_exit_code(monkeypatch: Any) -> None:
    class FailingManager(FakeManager):
        def wait_for_status(self, deployment_id: str, **kwargs: Any) -> dict[str, Any]:
            raise cli.DeploymentWaitError("deployment entered terminal state FAILED")

    monkeypatch.setattr(cli, "DeploymentManager", FailingManager)

    result = runner.invoke(
        cli.app,
        ["start", "deployment-1", "--wait"],
        env={"SIMPLISMART_PG_TOKEN": "test-token"},
    )

    assert result.exit_code == cli.EXIT_WAIT_FAILED
    assert '"exit_code": 7' in result.stderr
    assert "terminal state FAILED" in result.stderr


def test_table_output_uses_human_readable_headers(monkeypatch: Any) -> None:
    monkeypatch.setattr(cli, "DeploymentManager", FakeManager)

    result = runner.invoke(
        cli.app,
        ["--output", "table", "list"],
        env={"SIMPLISMART_PG_TOKEN": "test-token"},
    )

    assert result.exit_code == 0
    assert "Deployment Id" in result.stdout
    assert "Status" in result.stdout


def test_empty_token_has_actionable_configuration_error() -> None:
    result = runner.invoke(
        cli.app,
        ["list"],
        env={"SIMPLISMART_PG_TOKEN": ""},
    )

    assert result.exit_code == cli.EXIT_CONFIG
    assert "SIMPLISMART_PG_TOKEN must not be empty" in result.stderr


def test_daily_schedule_accepts_name_and_human_local_times(monkeypatch: Any) -> None:
    monkeypatch.setattr(cli, "DeploymentManager", FakeManager)

    result = runner.invoke(
        cli.app,
        [
            "schedule",
            "daily",
            "nightly-inference",
            "--on-at",
            "10am",
            "--off-at",
            "1am",
            "--timezone",
            "Asia/Kolkata",
        ],
        env={"SIMPLISMART_PG_TOKEN": "test-token"},
    )

    assert result.exit_code == 0
    assert '\"deployment_id\": \"nightly-inference\"' in result.stdout
    assert '\"start\": \"0 10 * * *\"' in result.stdout
    assert '\"end\": \"0 1 * * *\"' in result.stdout
    assert '\"crosses_midnight\": true' in result.stdout


def test_foreground_schedule_retries_transient_failure(monkeypatch: Any) -> None:
    attempts = 0

    def fake_execute(ctx: Any, operation: Any, **kwargs: Any) -> Any:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise cli.typer.Exit(cli.EXIT_API)
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_execute", fake_execute)
    monkeypatch.setattr(cli, "sleep", lambda seconds: None)

    result = runner.invoke(
        cli.app,
        [
            "schedule",
            "run",
            "nightly-inference",
            "--on-at",
            "10am",
            "--off-at",
            "1am",
            "--timezone",
            "UTC",
        ],
    )

    assert result.exit_code == 130
    assert attempts == 2
    assert "Retrying reconciliation" in result.stderr


def test_foreground_schedule_streams_compact_jsonl(monkeypatch: Any) -> None:
    monkeypatch.setattr(cli, "DeploymentManager", FakeManager)

    def stop_after_first_event(seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "sleep", stop_after_first_event)

    result = runner.invoke(
        cli.app,
        [
            "schedule",
            "run",
            "nightly-inference",
            "--on-at",
            "10am",
            "--off-at",
            "1am",
            "--timezone",
            "UTC",
            "--no-wait",
        ],
        env={"SIMPLISMART_PG_TOKEN": "test-token"},
    )

    lines = result.stdout.splitlines()
    assert result.exit_code == 130
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["deployment_id"] == "nightly-inference"
    assert event["desired_state"] in {"DEPLOYED", "STOPPED"}


def test_scheduler_sleep_rechecks_wall_clock_after_wake() -> None:
    timezone = ZoneInfo("UTC")
    window = cli.DailyWindow.create(
        on_at="10:00",
        off_at="11:00",
        timezone_name="UTC",
    )
    observed_times = iter(
        (
            datetime(2026, 7, 14, 10, 0, tzinfo=timezone),
            datetime(2026, 7, 14, 11, 30, tzinfo=timezone),
        )
    )
    sleeps: list[float] = []

    cli._sleep_until(
        datetime(2026, 7, 14, 11, 0, tzinfo=timezone),
        window,
        clock=lambda: next(observed_times),
        sleeper=sleeps.append,
    )

    assert sleeps == [cli.SCHEDULE_SLEEP_CHUNK_SECONDS]


def test_sensitive_sdk_fields_are_redacted_from_output(monkeypatch: Any) -> None:
    monkeypatch.setattr(cli, "DeploymentManager", FakeManager)

    result = runner.invoke(
        cli.app,
        ["get", "nightly-inference"],
        env={"SIMPLISMART_PG_TOKEN": "test-token"},
    )

    assert result.exit_code == 0
    assert "super-secret-value" not in result.stdout
    assert "another-secret" not in result.stdout
    assert result.stdout.count("<redacted>") == 2
