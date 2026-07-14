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

    def health(self, deployment_id: str) -> dict[str, str]:
        return {"data": "Progressing", "deployment_id": deployment_id}


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
