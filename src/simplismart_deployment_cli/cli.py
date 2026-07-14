from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from importlib.metadata import version
from math import ceil
from time import sleep
from typing import Any, Annotated

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from simplismart import SimplismartError

from .manager import (
    DeploymentManager,
    DeploymentNotFoundError,
    DeploymentWaitError,
)
from .settings import Settings
from .scheduling import DailyWindow, local_timezone_name

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
SCHEDULE_RETRY_SECONDS = 30.0
SCHEDULE_SLEEP_CHUNK_SECONDS = 30.0
RETRYABLE_SCHEDULE_EXIT_CODES = {EXIT_API, EXIT_WAIT_FAILED}
REDACTED = "<redacted>"
SENSITIVE_OUTPUT_KEYS = {
    "api_details",
    "env_variables",
    "environment_variables",
}
SENSITIVE_OUTPUT_FRAGMENTS = (
    "api_key",
    "authorization",
    "credential",
    "password",
    "secret",
    "token",
)

console = Console()
error_console = Console(stderr=True)


class OutputFormat(str, Enum):
    json = "json"
    table = "table"


DeploymentReference = Annotated[
    str,
    typer.Argument(
        metavar="DEPLOYMENT",
        help="Exact deployment name or UUID.",
    ),
]


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
schedule_app = typer.Typer(
    help="Manage deployment schedules and native Simplismart cron scaling."
)
app.add_typer(schedule_app, name="schedule")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(version("simplismart-deployment-cli"))
        raise typer.Exit()


@app.callback()
def configure(
    ctx: typer.Context,
    output: Annotated[
        OutputFormat | None,
        typer.Option(
            "--output",
            "-o",
            envvar="SIMPLISMART_OUTPUT",
            help="Output format. Defaults to table on a terminal and JSON when redirected.",
        ),
    ] = None,
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
    resolved_output = output or (
        OutputFormat.table if console.is_terminal else OutputFormat.json
    )
    ctx.obj = RuntimeOptions(
        output=resolved_output,
        settings_overrides=overrides,
    )


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
def get_deployment(ctx: typer.Context, deployment_id: DeploymentReference) -> None:
    """Show the complete deployment record."""
    _execute(ctx, lambda manager: manager.get(deployment_id))


@app.command()
def status(ctx: typer.Context, deployment_id: DeploymentReference) -> None:
    """Show deployment state and health together."""
    _execute(ctx, lambda manager: manager.status(deployment_id))


@app.command()
def start(
    ctx: typer.Context,
    deployment_id: DeploymentReference,
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
    deployment_id: DeploymentReference,
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
    deployment_id: DeploymentReference,
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
    deployment_id: DeploymentReference,
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
    deployment_id: DeploymentReference,
    start: Annotated[str, typer.Option("--start", help="Cron expression for window start.")],
    end: Annotated[str, typer.Option("--end", help="Cron expression for window end.")],
    timezone: Annotated[
        str | None,
        typer.Option(help="IANA timezone; defaults to the system timezone."),
    ] = None,
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
            timezone=timezone or local_timezone_name(),
            start=start,
            end=end,
            desired_replicas=desired_replicas,
            max_replicas=max_replicas,
        ),
    )


@schedule_app.command("daily")
def daily_schedule(
    ctx: typer.Context,
    deployment_id: DeploymentReference,
    on_at: Annotated[
        str,
        typer.Option("--on-at", help="Daily start time, for example 10:00 or 10am."),
    ],
    off_at: Annotated[
        str,
        typer.Option("--off-at", help="Daily stop time, for example 01:00 or 1am."),
    ],
    timezone: Annotated[
        str | None,
        typer.Option(help="IANA timezone; defaults to the system timezone."),
    ] = None,
    desired_replicas: Annotated[int, typer.Option(min=1)] = 1,
    max_replicas: Annotated[int, typer.Option(min=1)] = 1,
) -> None:
    """Configure a durable native daily window using human clock times."""
    if desired_replicas > max_replicas:
        _fail("--desired-replicas cannot exceed --max-replicas", EXIT_CONFIG)
    _execute(
        ctx,
        lambda manager: _configure_daily_schedule(
            manager,
            deployment_id=deployment_id,
            on_at=on_at,
            off_at=off_at,
            timezone=timezone,
            desired_replicas=desired_replicas,
            max_replicas=max_replicas,
        ),
    )


@schedule_app.command("show")
def show_schedule(
    ctx: typer.Context,
    deployment_id: DeploymentReference,
) -> None:
    """Show the current replica and native schedule configuration."""
    _execute(ctx, lambda manager: _schedule_view(manager, deployment_id))


@schedule_app.command("reconcile")
def reconcile_schedule(
    ctx: typer.Context,
    deployment_id: DeploymentReference,
    on_at: Annotated[
        str,
        typer.Option("--on-at", help="Desired daily start time."),
    ],
    off_at: Annotated[
        str,
        typer.Option("--off-at", help="Desired daily stop time."),
    ],
    timezone: Annotated[
        str | None,
        typer.Option(help="IANA timezone; defaults to the system timezone."),
    ] = None,
    org_id: Annotated[str | None, typer.Option("--org-id")] = None,
    wait: Annotated[
        bool,
        typer.Option("--wait", help="Wait for the desired state to be reached."),
    ] = False,
    wait_timeout: Annotated[
        float,
        typer.Option("--wait-timeout", min=1, help="Maximum wait in seconds."),
    ] = DEFAULT_WAIT_TIMEOUT,
    poll_interval: Annotated[
        float,
        typer.Option("--poll-interval", min=0.1, help="Status polling interval."),
    ] = DEFAULT_POLL_INTERVAL,
) -> None:
    """Apply the correct state now; safe for boot, cron, or wake catch-up."""
    _execute(
        ctx,
        lambda manager: _reconcile_daily_schedule(
            ctx,
            manager,
            deployment_id=deployment_id,
            on_at=on_at,
            off_at=off_at,
            timezone=timezone,
            org_id=org_id,
            wait=wait,
            wait_timeout=wait_timeout,
            poll_interval=poll_interval,
        ),
    )


@schedule_app.command("run")
def run_schedule(
    ctx: typer.Context,
    deployment_id: DeploymentReference,
    on_at: Annotated[
        str,
        typer.Option("--on-at", help="Desired daily start time."),
    ],
    off_at: Annotated[
        str,
        typer.Option("--off-at", help="Desired daily stop time."),
    ],
    timezone: Annotated[
        str | None,
        typer.Option(help="IANA timezone; defaults to the system timezone."),
    ] = None,
    org_id: Annotated[str | None, typer.Option("--org-id")] = None,
    wait: Annotated[
        bool,
        typer.Option(
            "--wait/--no-wait",
            help="Wait for each lifecycle transition before sleeping.",
        ),
    ] = True,
    wait_timeout: Annotated[
        float,
        typer.Option("--wait-timeout", min=1, help="Maximum transition wait."),
    ] = DEFAULT_WAIT_TIMEOUT,
    poll_interval: Annotated[
        float,
        typer.Option("--poll-interval", min=0.1, help="Status polling interval."),
    ] = DEFAULT_POLL_INTERVAL,
) -> None:
    """Run the daily start/stop loop in the foreground."""
    try:
        window = DailyWindow.create(
            on_at=on_at,
            off_at=off_at,
            timezone_name=timezone,
        )
    except ValueError as exc:
        _fail(str(exc), EXIT_CONFIG)

    if error_console.is_terminal:
        error_console.print(
            "[bold]Running daily lifecycle schedule[/bold]\\n"
            f"  Deployment: {escape(deployment_id)}\\n"
            f"  Window: {window.on_at.strftime('%H:%M')} → "
            f"{window.off_at.strftime('%H:%M')} ({window.timezone_name})\\n"
            "  Press Ctrl+C to stop."
        )

    try:
        while True:
            now = datetime.now(window.timezone)
            boundary = window.next_boundary(now)
            try:
                _execute(
                    ctx,
                    lambda manager: _reconcile_window(
                        ctx,
                        manager,
                        deployment_id=deployment_id,
                        window=window,
                        evaluated_at=now,
                        org_id=org_id,
                        wait=wait,
                        wait_timeout=wait_timeout,
                        poll_interval=poll_interval,
                    ),
                    stream=True,
                )
            except typer.Exit as exc:
                if exc.exit_code not in RETRYABLE_SCHEDULE_EXIT_CODES:
                    raise
                until_boundary = (
                    boundary.timestamp()
                    - datetime.now(window.timezone).timestamp()
                )
                retry_in = max(
                    min(SCHEDULE_RETRY_SECONDS, until_boundary),
                    0.1,
                )
                error_console.print(
                    f"[yellow]Retrying reconciliation in {retry_in:g}s.[/yellow]"
                )
                sleep(retry_in)
                continue

            remaining = (
                boundary.timestamp()
                - datetime.now(window.timezone).timestamp()
            )
            if error_console.is_terminal:
                error_console.print(
                    f"[dim]Next reconciliation: {boundary.isoformat()} "
                    f"(in {_format_duration(remaining)})[/dim]"
                )
            _sleep_until(boundary, window)
    except KeyboardInterrupt:
        error_console.print("[dim]Schedule stopped.[/dim]")
        raise typer.Exit(130) from None


@schedule_app.command("clear")
def clear_schedule(
    ctx: typer.Context,
    deployment_id: DeploymentReference,
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


def _configure_daily_schedule(
    manager: DeploymentManager,
    *,
    deployment_id: str,
    on_at: str,
    off_at: str,
    timezone: str | None,
    desired_replicas: int,
    max_replicas: int,
) -> dict[str, Any]:
    window = DailyWindow.create(
        on_at=on_at,
        off_at=off_at,
        timezone_name=timezone,
    )
    result = manager.set_schedule(
        deployment_id,
        timezone=window.timezone_name,
        start=window.on_cron,
        end=window.off_cron,
        desired_replicas=desired_replicas,
        max_replicas=max_replicas,
    )
    result["schedule"] = {
        **window.as_dict(),
        "mode": "native_cron_scaling",
        "desired_replicas": desired_replicas,
        "max_replicas": max_replicas,
    }
    return result


def _schedule_view(
    manager: DeploymentManager,
    deployment_id: str,
) -> dict[str, Any]:
    resolved_id = manager.resolve_id(deployment_id)
    detail = manager.get(resolved_id)
    if not isinstance(detail, dict):
        return {"deployment_id": resolved_id, "deployment": detail}
    return {
        "deployment_id": resolved_id,
        "deployment_name": detail.get("deployment_name") or detail.get("name"),
        "status": detail.get("status") or detail.get("deployment_status"),
        "min_pod_replicas": detail.get("min_pod_replicas"),
        "max_pod_replicas": detail.get("max_pod_replicas"),
        "autoscale_config": (
            detail.get("autoscale_config")
            or detail.get("autoscaling_config")
            or {}
        ),
    }


def _reconcile_daily_schedule(
    ctx: typer.Context,
    manager: DeploymentManager,
    *,
    deployment_id: str,
    on_at: str,
    off_at: str,
    timezone: str | None,
    org_id: str | None,
    wait: bool,
    wait_timeout: float,
    poll_interval: float,
) -> dict[str, Any]:
    window = DailyWindow.create(
        on_at=on_at,
        off_at=off_at,
        timezone_name=timezone,
    )
    return _reconcile_window(
        ctx,
        manager,
        deployment_id=deployment_id,
        window=window,
        evaluated_at=datetime.now(window.timezone),
        org_id=org_id,
        wait=wait,
        wait_timeout=wait_timeout,
        poll_interval=poll_interval,
    )


def _reconcile_window(
    ctx: typer.Context,
    manager: DeploymentManager,
    *,
    deployment_id: str,
    window: DailyWindow,
    evaluated_at: datetime,
    org_id: str | None,
    wait: bool,
    wait_timeout: float,
    poll_interval: float,
) -> dict[str, Any]:
    active = window.is_active(evaluated_at)
    action = (
        manager.start(deployment_id, org_id=org_id)
        if active
        else manager.stop(deployment_id, org_id=org_id)
    )
    target_status = "DEPLOYED" if active else "STOPPED"
    result: dict[str, Any] = {
        "deployment_id": action["deployment_id"],
        "evaluated_at": evaluated_at.astimezone(window.timezone).isoformat(),
        "desired_state": target_status,
        "schedule": window.as_dict(),
        "action": action,
    }
    if wait:
        result["final_state"] = _wait_for_status(
            ctx,
            manager,
            deployment_id=action["deployment_id"],
            target_status=target_status,
            require_healthy=active,
            wait_timeout=wait_timeout,
            poll_interval=poll_interval,
        )
    return result


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
    *,
    stream: bool = False,
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
    except DeploymentNotFoundError as exc:
        _fail(str(exc), EXIT_NOT_FOUND)
    except DeploymentWaitError as exc:
        _fail(str(exc), EXIT_WAIT_FAILED)
    except ValueError as exc:
        _fail(str(exc), EXIT_CONFIG)
    except Exception as exc:  # Last-resort stable failure contract for unattended jobs.
        _fail(str(exc), EXIT_SOFTWARE)

    if stream and runtime.output is OutputFormat.json:
        print(
            json.dumps(_redact(result), default=str, separators=(",", ":")),
            flush=True,
        )
    else:
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


def _redact(value: Any) -> Any:
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if not isinstance(value, dict):
        return value

    redacted: dict[str, Any] = {}
    for key, item in value.items():
        normalized = str(key).lower()
        if normalized in SENSITIVE_OUTPUT_KEYS:
            if isinstance(item, dict):
                redacted[key] = {name: REDACTED for name in item}
            else:
                redacted[key] = REDACTED
        elif any(fragment in normalized for fragment in SENSITIVE_OUTPUT_FRAGMENTS):
            redacted[key] = None if item is None else REDACTED
        else:
            redacted[key] = _redact(item)
    return redacted


def _render(data: Any, output: OutputFormat) -> None:
    safe_data = _redact(data)
    if output is OutputFormat.json:
        console.print_json(json.dumps(safe_data, default=str))
        return

    rows = _collection_rows(safe_data)
    if rows is not None:
        _render_rows(rows)
        return
    _render_record(safe_data)


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


def _sleep_until(
    boundary: datetime,
    window: DailyWindow,
    *,
    clock: Callable[[], datetime] | None = None,
    sleeper: Callable[[float], None] | None = None,
) -> None:
    current_time = clock or (lambda: datetime.now(window.timezone))
    pause = sleeper or sleep
    while True:
        remaining = boundary.timestamp() - current_time().timestamp()
        if remaining <= 0:
            return
        pause(min(remaining, SCHEDULE_SLEEP_CHUNK_SECONDS))


def _format_duration(seconds: float) -> str:
    total_minutes = ceil(max(seconds, 0) / 60)
    if total_minutes == 0:
        return "<1m"
    days, remaining_minutes = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(remaining_minutes, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)


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
    error_console.print_json(json.dumps(_redact(body), default=str))
    raise typer.Exit(exit_code)
