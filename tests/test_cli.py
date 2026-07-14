from __future__ import annotations

from typing import Any

from typer.testing import CliRunner

from simplismart_deployment_cli import cli

runner = CliRunner()


class FakeManager:
    def __init__(self, settings: Any) -> None:
        self.settings = settings

    def list(self, **kwargs: Any) -> list[dict[str, str]]:
        return [{"deployment_id": "deployment-1", "status": "DEPLOYED"}]

    def start(self, deployment_id: str, **kwargs: Any) -> dict[str, Any]:
        return {
            "deployment_id": deployment_id,
            "action": "start",
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
