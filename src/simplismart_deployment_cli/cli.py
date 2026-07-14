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
from rich.markup import escape
from rich.table import Table
from simplismart import SimplismartError

from .manager import DeploymentManager, DeploymentWaitError
from .settings import Settings

EXIT_CONFIG = 2
EXIT_AUTH = 3
EXIT_NOT_FOUND = 4
EXIT_API = 5
EXIT_UNHEALTHY = 6
EXIT_SOFTWARE = 70
EXIT_WAIT_FAILED = 7
HEALTHY_STATUS = "healthy"
VALID_DEPLOYMENT_STATUSES = {"DEPLOYED", "PENDING", "FAILED", "STOPPED", "DELETED"}
DEFAULT_WAIT_TIMEOUT = 600.0
DEFAULT_POLL_INTERVAL = 5.0

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
    help=(
        "Manage Simplismart deployments interactively or from cron and "
        "Kubernetes CronJobs."
    ),
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)
schedule_app = typer.Typer(help="Manage native Simplismart cron scaling windows.")
app.add_typer(schedule_app, name="schedule")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(version("simplismart-deployment-cli"))
        raise typer.Exit()


@app.callback()
def configure(
    ctx: typer.Context,
    output: Annotated[
        OutputFormat,
        typer.Option(
            "--output",
            "-o",
            envvar="SIMPLISMART_OUTPUT",
            help="Output format. JSON is stable for automation.",
        ),
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
        typer.Option(
            "--version",
            "-V",
            callback=_version_callback,
            is_eager=True,
            help="Show version and exit.",
        ),
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
    """Show the complete deployment record."""
    _execute(ctx, lambda manager: manager.get(deployment_id))


@app.command()
def status(ctx: typer.Context, deployment_id: str) -> None:
    """Show deployment state and health together."""
    _execute(ctx, lambda manager: manager.status(deployment_id))


@app.command()
def start(
    ctx: typer.Context,
    deployment_id: str,
    org_id: Annotated[str | None, typer.Option("--org-id")] = None,
    wait: Annotated[
        bool,
        typer.Option("--wait", help="Wait until the deployment is DEPLOYED and Healthy."),
    ] = False,
    wait_timeout: Annotated[
        float,
        typer.Option("--wait-timeout", min=1, help="Maximum wait in seconds."),
    ] = DEFAULT_WAIT_TIMEOUT,
    poll_interval: Annotated[
        float,
        typer.Option("--poll-interval", min=0.1, help="Status polling interval in seconds."),
    ] = DEFAULT_POLL_INTERVAL,
) -> None:
    """Start a deployment; safely no-op if it is already active."""
    _execute(
        ctx,
        lambda manager: _lifecycle_result(
            ctx,
            manager,
            action=lambda: manager.start(deployment_id, org_id=org_id),
            deployment_id=deployment_id,
            target_status="DEPLOYED",
            require_healthy=True,
            wait=wait,
            wait_timeout=wait_timeout,
            poll_interval=poll_interval,
        ),
    )


@app.command()
def stop(
    ctx: typer.Context,
    deployment_id: str,
    org_id: Annotated[str | None, typer.Option("--org-id")] = None,
    wait: Annotated[
        bool,
        typer.Option("--wait", help="Wait until the deployment is STOPPED."),
    ] = False,
    wait_timeout: Annotated[
        float,
        typer.Option("--wait-timeout", min=1, help="Maximum wait in seconds."),
    ] = DEFAULT_WAIT_TIMEOUT,
    poll_interval: Annotated[
        float,
        typer.Option("--poll-interval", min=0.1, help="Status polling interval in seconds."),
    ] = DEFAULT_POLL_INTERVAL,
) -> None:
    """Stop a deployment; safely no-op if it is already stopped."""
    _execute(
        ctx,
        lambda manager: _lifecycle_result(
            ctx,
            manager,
            action=lambda: manager.stop(deployment_id, org_id=org_id),
            deployment_id=deployment_id,
            target_status="STOPPED",
            require_healthy=False,
            wait=wait,
            wait_timeout=wait_timeout,
            poll_interval=poll_interval,
        ),
    )


@app.command()
def restart(
    ctx: typer.Context,
    deployment_id: str,
    namespace: Annotated[
        str | None,
        typer.Option(
            "--namespace",
            help="Kubernetes namespace; inferred from config or deployment details when omitted.",
        ),
    ] = None,
    org_id: Annotated[str | None, typer.Option("--org-id")] = None,
    wait: Annotated[
        bool,
        typer.Option("--wait", help="Wait until the deployment is DEPLOYED and Healthy."),
    ] = False,
    wait_timeout: Annotated[
        float,
        typer.Option("--wait-timeout", min=1, help="Maximum wait in seconds."),
    ] = DEFAULT_WAIT_TIMEOUT,
    poll_interval: Annotated[
        float,
        typer.Option("--poll-interval", min=0.1, help="Status polling interval in seconds."),
    ] = DEFAULT_POLL_INTERVAL,
) -> None:
    """Restart a deployment. This operation is intentionally not idempotent."""
    _execute(
        ctx,
        lambda manager: _lifecycle_result(
            ctx,
            manager,
            action=lambda: manager.restart(
                deployment_id,
                namespace=namespace,
                org_id=org_id,
            ),
            deployment_id=deployment_id,
            target_status="DEPLOYED",
            require_healthy=True,
            wait=wait,
            wait_timeout=wait_timeout,
            poll_interval=poll_interval,
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
    wait: Annotated[
        bool,
        typer.Option("--wait", help="Wait until the deployment is DEPLOYED and Healthy."),
    ] = False,
    wait_timeout: Annotated[
        float,
        typer.Option("--wait-timeout", min=1, help="Maximum wait in seconds."),
    ] = DEFAULT_WAIT_TIMEOUT,
    poll_interval: Annotated[
        float,
        typer.Option("--poll-interval", min=0.1, help="Status polling interval in seconds."),
    ] = DEFAULT_POLL_INTERVAL,
) -> None:
    """Report deployment health, optionally waiting for readiness."""
    if wait:
        _execute(
            ctx,
            lambda manager: _wait_for_status(
                ctx,
                manager,
                deployment_id=deployment_id,
                target_status="DEPLOYED",
                require_healthy=True,
                wait_timeout=wait_timeout,
                poll_interval=poll_interval,
            ),
        )
        return

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


def _lifecycle_result(
    ctx: typer.Context,
    manager: DeploymentManager,
    *,
    action: Callable[[], dict[str, Any]],
    deployment_id: str,
    target_status: str,
    require_healthy: bool,
    wait: bool,
    wait_timeout: float,
    poll_interval: float,
) -> dict[str, Any]:
    result = action()
    if wait:
        result["final_state"] = _wait_for_status(
            ctx,
            manager,
            deployment_id=deployment_id,
            target_status=target_status,
            require_healthy=require_healthy,
            wait_timeout=wait_timeout,
            poll_interval=poll_interval,
        )
    return result


def _wait_for_status(
    ctx: typer.Context,
    manager: DeploymentManager,
    *,
    deployment_id: str,
    target_status: str,
    require_healthy: bool,
    wait_timeout: float,
    poll_interval: float,
) -> dict[str, Any]:
    show_progress = error_console.is_terminal and not ctx.resilient_parsing
    message = f"Waiting for {target_status}"
    if require_healthy:
        message += " and Healthy"

    if show_progress:
        with error_console.status(message) as progress:
            return manager.wait_for_status(
                deployment_id,
                target_status=target_status,
                require_healthy=require_healthy,
                timeout=wait_timeout,
                poll_interval=poll_interval,
                on_progress=lambda state: progress.update(
                    f"{message} ({escape(state)})"
                ),
            )

    return manager.wait_for_status(
        deployment_id,
        target_status=target_status,
        require_healthy=require_healthy,
        timeout=wait_timeout,
        poll_interval=poll_interval,
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
        _fail(_validation_message(exc), EXIT_CONFIG)
    except SimplismartError as exc:
        status_code = getattr(exc, "status_code", None)
        if status_code in {401, 403}:
            exit_code = EXIT_AUTH
        elif status_code == 404:
            exit_code = EXIT_NOT_FOUND
        else:
            exit_code = EXIT_API
        _fail(str(exc), exit_code, status_code=status_code, payload=getattr(exc, "payload", None))
    except DeploymentWaitError as exc:
        _fail(str(exc), EXIT_WAIT_FAILED)
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


def _validation_message(exc: ValidationError) -> str:
    messages: list[str] = []
    for error in exc.errors(include_url=False, include_input=False):
        location = ".".join(str(part) for part in error["loc"])
        if location in {"pg_token", "SIMPLISMART_PG_TOKEN"}:
            message = (
                "SIMPLISMART_PG_TOKEN is required; set it in the environment or .env"
                if error["type"] == "missing"
                else "SIMPLISMART_PG_TOKEN must not be empty"
            )
            messages.append(message)
        else:
            messages.append(f"{location}: {error['msg']}" if location else error["msg"])
    return "; ".join(messages)


def _render(data: Any, output: OutputFormat) -> None:
    if output is OutputFormat.json:
        console.print_json(json.dumps(data, default=str))
        return

    rows = _collection_rows(data)
    if rows is not None:
        _render_rows(rows)
        return
    _render_record(data)


def _collection_rows(data: Any) -> list[dict[str, Any]] | None:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict) and isinstance(data.get("results"), list):
        return [item for item in data["results"] if isinstance(item, dict)]
    return None


def _render_rows(rows: list[dict[str, Any]]) -> None:
    if not rows:
        console.print("[dim]No deployments found.[/dim]")
        return

    preferred = (
        "deployment_id",
        "uuid",
        "deployment_name",
        "name",
        "status",
        "model_repo_name",
        "accelerator_type",
        "accelerator_count",
    )
    columns = [column for column in preferred if any(column in row for row in rows)]
    if not columns:
        columns = list(dict.fromkeys(key for row in rows for key in row))

    table = Table(show_header=True, header_style="bold")
    for column in columns:
        table.add_column(column.replace("_", " ").title())
    for row in rows:
        table.add_row(*(_format_value(row.get(column)) for column in columns))
    console.print(table)


def _render_record(data: Any) -> None:
    record = data if isinstance(data, dict) else {"value": data}
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column("Field", style="bold cyan", no_wrap=True)
    table.add_column("Value")
    for field, value in _flatten_record(record):
        table.add_row(field.replace("_", " "), _format_value(value))
    console.print(table)


def _flatten_record(
    record: dict[str, Any],
    prefix: str = "",
    depth: int = 0,
) -> list[tuple[str, Any]]:
    flattened: list[tuple[str, Any]] = []
    for key, value in record.items():
        field = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict) and depth < 2:
            flattened.extend(_flatten_record(value, field, depth + 1))
        else:
            flattened.append((field, value))
    return flattened


def _format_value(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str, separators=(",", ":"))
    return str(value)


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
