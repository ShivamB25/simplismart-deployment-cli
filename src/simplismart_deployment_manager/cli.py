from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from importlib.metadata import version
from typing import Any, Annotated

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table
from simplismart import SimplismartError

from .manager import DeploymentManager
from .settings import Settings

EXIT_CONFIG = 2
EXIT_AUTH = 3
EXIT_NOT_FOUND = 4
EXIT_API = 5
EXIT_UNHEALTHY = 6
EXIT_SOFTWARE = 70
HEALTHY_STATUS = "healthy"
VALID_DEPLOYMENT_STATUSES = {"DEPLOYED", "PENDING", "FAILED", "STOPPED", "DELETED"}

console = Console()
error_console = Console(stderr=True)


class OutputFormat(str, Enum):
    json = "json"
    table = "table"


@dataclass(frozen=True)
class RuntimeOptions:
    output: OutputFormat
    settings_overrides: dict[str, Any]


app = typer.Typer(
    help="Manage Simplismart deployments from shells, cron, and Kubernetes CronJobs.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)
schedule_app = typer.Typer(help="Manage native Simplismart cron scaling windows.")
app.add_typer(schedule_app, name="schedule")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(version("simplismart-deployment-manager"))
        raise typer.Exit()


@app.callback()
def configure(
    ctx: typer.Context,
    output: Annotated[
        OutputFormat,
        typer.Option("--output", "-o", help="Output format. JSON is stable for automation."),
    ] = OutputFormat.json,
    org_id: Annotated[
        str | None,
        typer.Option("--org-id", help="Override ORG_ID for this invocation."),
    ] = None,
    base_url: Annotated[
        str | None,
        typer.Option("--base-url", help="Override SIMPLISMART_BASE_URL."),
    ] = None,
    timeout: Annotated[
        float | None,
        typer.Option("--timeout", min=0.1, help="Override SIMPLISMART_TIMEOUT in seconds."),
    ] = None,
    version_flag: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Show version and exit."),
    ] = False,
) -> None:
    """Load non-secret CLI overrides; the token is accepted only through the environment."""
    del version_flag
    overrides = {
        key: value
        for key, value in {
            "org_id": org_id,
            "base_url": base_url,
            "timeout": timeout,
        }.items()
        if value is not None
    }
    ctx.obj = RuntimeOptions(output=output, settings_overrides=overrides)


@app.command("list")
def list_deployments(
    ctx: typer.Context,
    model_repo_id: Annotated[
        str | None,
        typer.Option("--model-repo-id", help="Filter by model repository UUID."),
    ] = None,
    status: Annotated[
        str | None,
        typer.Option(help="Filter by DEPLOYED, PENDING, FAILED, STOPPED, or DELETED."),
    ] = None,
    offset: Annotated[int, typer.Option(min=0)] = 0,
    count: Annotated[int, typer.Option(min=1, max=100)] = 20,
) -> None:
    """List deployments with optional filters."""
    normalized_status = status.upper() if status else None
    if normalized_status and normalized_status not in VALID_DEPLOYMENT_STATUSES:
        _fail(
            f"invalid status {status!r}; expected one of {', '.join(sorted(VALID_DEPLOYMENT_STATUSES))}",
            EXIT_CONFIG,
        )
    _execute(
        ctx,
        lambda manager: manager.list(
            model_repo_id=model_repo_id,
            status=normalized_status,
            offset=offset,
            count=count,
        ),
    )


@app.command("get")
def get_deployment(ctx: typer.Context, deployment_id: str) -> None:
    """Get one deployment by UUID."""
    _execute(ctx, lambda manager: manager.get(deployment_id))


@app.command()
def start(
    ctx: typer.Context,
    deployment_id: str,
    org_id: Annotated[str | None, typer.Option("--org-id")] = None,
) -> None:
    """Start a deployment; no-op successfully if already deployed."""
    _execute(ctx, lambda manager: manager.start(deployment_id, org_id=org_id))


@app.command()
def stop(
    ctx: typer.Context,
    deployment_id: str,
    org_id: Annotated[str | None, typer.Option("--org-id")] = None,
) -> None:
    """Stop a deployment; no-op successfully if already stopped."""
    _execute(ctx, lambda manager: manager.stop(deployment_id, org_id=org_id))


@app.command()
def restart(
    ctx: typer.Context,
    deployment_id: str,
    namespace: Annotated[str, typer.Option("--namespace", help="Deployment Kubernetes namespace.")],
    org_id: Annotated[str | None, typer.Option("--org-id")] = None,
) -> None:
    """Restart a deployment."""
    _execute(
        ctx,
        lambda manager: manager.restart(
            deployment_id,
            namespace=namespace,
            org_id=org_id,
        ),
    )


@app.command()
def health(
    ctx: typer.Context,
    deployment_id: str,
    require_healthy: Annotated[
        bool,
        typer.Option(help=f"Exit {EXIT_UNHEALTHY} unless Simplismart reports Healthy."),
    ] = False,
) -> None:
    """Report deployment health."""
    result = _execute(ctx, lambda manager: manager.health(deployment_id))
    if require_healthy and _health_status(result) != HEALTHY_STATUS:
        raise typer.Exit(EXIT_UNHEALTHY)


@schedule_app.command("set")
def set_schedule(
    ctx: typer.Context,
    deployment_id: str,
    start: Annotated[str, typer.Option("--start", help="Cron expression for window start.")],
    end: Annotated[str, typer.Option("--end", help="Cron expression for window end.")],
    timezone: Annotated[str, typer.Option(help="IANA timezone, for example UTC.")] = "UTC",
    desired_replicas: Annotated[int, typer.Option(min=1)] = 1,
    max_replicas: Annotated[int, typer.Option(min=1)] = 1,
) -> None:
    """Set one native cron window and scale to zero outside it."""
    if desired_replicas > max_replicas:
        _fail("--desired-replicas cannot exceed --max-replicas", EXIT_CONFIG)
    _execute(
        ctx,
        lambda manager: manager.set_schedule(
            deployment_id,
            timezone=timezone,
            start=start,
            end=end,
            desired_replicas=desired_replicas,
            max_replicas=max_replicas,
        ),
    )


@schedule_app.command("clear")
def clear_schedule(
    ctx: typer.Context,
    deployment_id: str,
    min_replicas: Annotated[int, typer.Option(min=1)] = 1,
    max_replicas: Annotated[int, typer.Option(min=1)] = 1,
) -> None:
    """Remove native cron scaling and leave a non-zero replica range."""
    if min_replicas > max_replicas:
        _fail("--min-replicas cannot exceed --max-replicas", EXIT_CONFIG)
    _execute(
        ctx,
        lambda manager: manager.clear_schedule(
            deployment_id,
            min_replicas=min_replicas,
            max_replicas=max_replicas,
        ),
    )


def _execute(
    ctx: typer.Context,
    operation: Callable[[DeploymentManager], Any],
) -> Any:
    runtime = _runtime(ctx)
    try:
        settings = Settings(**runtime.settings_overrides)
        result = operation(DeploymentManager(settings))
    except ValidationError as exc:
        messages = "; ".join(error["msg"] for error in exc.errors(include_url=False, include_input=False))
        _fail(messages, EXIT_CONFIG)
    except SimplismartError as exc:
        status_code = getattr(exc, "status_code", None)
        if status_code in {401, 403}:
            exit_code = EXIT_AUTH
        elif status_code == 404:
            exit_code = EXIT_NOT_FOUND
        else:
            exit_code = EXIT_API
        _fail(str(exc), exit_code, status_code=status_code, payload=getattr(exc, "payload", None))
    except ValueError as exc:
        _fail(str(exc), EXIT_CONFIG)
    except Exception as exc:  # Last-resort stable failure contract for unattended jobs.
        _fail(str(exc), EXIT_SOFTWARE)

    _render(result, runtime.output)
    return result


def _runtime(ctx: typer.Context) -> RuntimeOptions:
    runtime = ctx.find_root().obj
    if not isinstance(runtime, RuntimeOptions):
        raise RuntimeError("CLI runtime was not configured")
    return runtime


def _render(data: Any, output: OutputFormat) -> None:
    if output is OutputFormat.json:
        console.print_json(json.dumps(data, default=str))
        return

    rows = _rows(data)
    if not rows:
        console.print("No deployments found.")
        return
    columns = list(dict.fromkeys(key for row in rows for key in row))
    table = Table(show_header=True)
    for column in columns:
        table.add_column(column)
    for row in rows:
        table.add_row(*(str(row.get(column, "")) for column in columns))
    console.print(table)


def _rows(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        results = data.get("results")
        if isinstance(results, list):
            return [item for item in results if isinstance(item, dict)]
        return [{key: value for key, value in data.items() if not isinstance(value, (dict, list))}]
    return [{"value": data}]


def _health_status(result: Any) -> str | None:
    if not isinstance(result, dict):
        return None
    status = result.get("data") or result.get("status")
    return str(status).lower() if status is not None else None


def _fail(
    message: str,
    exit_code: int,
    *,
    status_code: int | None = None,
    payload: Any = None,
) -> None:
    body: dict[str, Any] = {"error": message, "exit_code": exit_code}
    if status_code is not None:
        body["status_code"] = status_code
    if payload:
        body["payload"] = payload
    error_console.print_json(json.dumps(body, default=str))
    raise typer.Exit(exit_code)
