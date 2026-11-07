from __future__ import annotations

from typer.testing import CliRunner

from simplismart_deployment_cli import cli

runner = CliRunner()


def test_invalid_status_is_a_usage_failure() -> None:
    result = runner.invoke(
        cli.app,
        ["list", "--status", "running"],
        env={"SIMPLISMART_PG_TOKEN": "test-token"},
    )

    assert result.exit_code == cli.EXIT_CONFIG


def test_empty_token_is_a_configuration_error() -> None:
    result = runner.invoke(
        cli.app,
        ["list"],
        env={"SIMPLISMART_PG_TOKEN": ""},
    )

    assert result.exit_code == cli.EXIT_CONFIG
