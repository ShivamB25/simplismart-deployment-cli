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

    def status(self, deployment_id: str) -> dict[str, Any]:
        return {
            "deployment": {
                "deployment_id": deployment_id,
                "status": "DEPLOYED",
            },
            "health": {"data": "Healthy"},
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

    def delete(self, deployment_id: str, **kwargs: Any) -> dict[str, Any]:
        return {
            "deployment_id": deployment_id,
            "action": "delete",
            "changed": True,
            "result": {"accepted": True, **kwargs},
        }

    def restart(self, deployment_id: str, **kwargs: Any) -> dict[str, Any]:
        return {
            "deployment_id": deployment_id,
            "action": "restart",
            "changed": True,
            "result": {"accepted": True, **kwargs},
        }

    def deploy_manifest(self, manifest: str) -> dict[str, Any]:
        return {
            "manifest": manifest,
            "deployment_id": "manifest-deployment-1",
            "model_repo_created": True,
            "deployment_created": True,
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


def test_delete_command_uses_manager_delete(monkeypatch: Any) -> None:
    monkeypatch.setattr(cli, "DeploymentManager", FakeManager)

    result = runner.invoke(
        cli.app,
        ["delete", "deployment-1", "--org-id", "org-1"],
        env={"SIMPLISMART_PG_TOKEN": "test-token"},
    )

    assert result.exit_code == 0
    assert '"action": "delete"' in result.stdout
    assert '"org_id": "org-1"' in result.stdout


def test_direct_deployment_commands_accept_multiple_targets(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(cli, "DeploymentManager", FakeManager)

    for command in ("get", "status", "start", "stop", "restart", "health", "delete"):
        result = runner.invoke(
            cli.app,
            [command, "deployment-1", "deployment-2"],
            env={"SIMPLISMART_PG_TOKEN": "test-token"},
        )

        assert result.exit_code == 0, (command, result.output)
        payload = json.loads(result.stdout)
        assert isinstance(payload, list)
        assert len(payload) == 2
        assert "deployment-1" in result.stdout
        assert "deployment-2" in result.stdout


def test_manage_infers_actions_from_refreshed_deployment_state(
    monkeypatch: Any,
) -> None:
    class InteractiveManager(FakeManager):
        status = "DEPLOYED"
        actions: list[str] = []

        def list(self, **kwargs: Any) -> list[dict[str, str]]:
            return [
                {
                    "deployment_id": "deployment-1",
                    "deployment_name": "nightly-inference",
                    "status": self.status,
                }
            ]

        def start(self, deployment_id: str, **kwargs: Any) -> dict[str, Any]:
            self.status = "DEPLOYED"
            self.actions.append("start")
            return {"deployment_id": deployment_id, "action": "start"}

        def stop(self, deployment_id: str, **kwargs: Any) -> dict[str, Any]:
            self.status = "STOPPED"
            self.actions.append("stop")
            return {"deployment_id": deployment_id, "action": "stop"}

    monkeypatch.setattr(cli, "DeploymentManager", InteractiveManager)

    result = runner.invoke(
        cli.app,
        ["manage"],
        input="1\ny\n1\ny\nq\n",
        env={"SIMPLISMART_PG_TOKEN": "test-token"},
    )

    assert result.exit_code == 0
    assert InteractiveManager.actions == ["stop", "start"]
    assert "nightly-inference" in result.stdout
    assert "DEPLOYED" in result.stdout
    assert "STOPPED" in result.stdout
    assert "Select deployments [1-1, all, q]" in result.stdout
    assert "Stopped 1 deployment." in result.stdout
    assert "Started 1 deployment." in result.stdout


def test_manage_reports_plural_batch_summary(monkeypatch: Any) -> None:
    deployment_ids = (
        "9bd51135-f42f-49bd-89c0-974172e7d818",
        "fc9318f7-1cfb-4e40-b14c-93c09b47205c",
    )
    actions: list[str] = []

    class BatchManager(FakeManager):
        def list(self, **kwargs: Any) -> list[dict[str, str]]:
            return [
                {
                    "deployment_id": deployment_id,
                    "deployment_name": f"inference-{index}",
                    "status": "STOPPED" if not actions else "DEPLOYED",
                }
                for index, deployment_id in enumerate(deployment_ids, start=1)
            ]

        def start(self, deployment_id: str, **kwargs: Any) -> dict[str, Any]:
            actions.append(deployment_id)
            return {"deployment_id": deployment_id}

    monkeypatch.setattr(cli, "DeploymentManager", BatchManager)

    result = runner.invoke(
        cli.app,
        ["manage"],
        input="all\ny\nq\n",
        env={"SIMPLISMART_PG_TOKEN": "test-token"},
    )

    assert result.exit_code == 0
    assert actions == list(deployment_ids)
    assert "Select deployments [1-2, all, q]" in result.stdout
    assert "Started 2 deployments." in result.stdout
    assert "ID (short)" in result.stdout
    assert "9bd51135…" in result.stdout
    assert deployment_ids[0] not in result.stdout
    assert "Deployment Id" not in result.stdout


def test_health_gate_has_distinct_unhealthy_exit_code(monkeypatch: Any) -> None:
    monkeypatch.setattr(cli, "DeploymentManager", FakeManager)

    result = runner.invoke(
        cli.app,
        ["health", "deployment-1", "--require-healthy"],
        env={"SIMPLISMART_PG_TOKEN": "test-token"},
    )

    assert result.exit_code == cli.EXIT_UNHEALTHY
    assert '"data": "Progressing"' in result.stdout


def test_deploy_manifest_waits_for_a_healthy_deployment(monkeypatch: Any) -> None:
    monkeypatch.setattr(cli, "DeploymentManager", FakeManager)

    result = runner.invoke(
        cli.app,
        ["deploy", "deployments/qwen3.6-35b-a3b-fp8/sglang-simple.yaml"],
        env={"SIMPLISMART_PG_TOKEN": "test-token", "ORG_ID": "org-1"},
    )

    assert result.exit_code == 0
    assert '"deployment_id": "manifest-deployment-1"' in result.stdout
    assert '"final_state"' in result.stdout


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
    assert '"deployment_id": "nightly-inference"' in result.stdout
    assert '"start": "0 10 * * *"' in result.stdout
    assert '"end": "0 1 * * *"' in result.stdout
    assert '"crosses_midnight": true' in result.stdout


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
