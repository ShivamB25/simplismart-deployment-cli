# Source map
- Packaged Python CLI in `src/simplismart_deployment_cli/`.
- `cli.py`: Typer command surface, rendering, error-to-exit-code mapping.
- `manager.py`: automation-safe facade over the official Simplismart SDK.
- `settings.py`: environment-backed Pydantic settings.
- Tests under `tests/`; CI in `.github/workflows/ci.yml`.

# Invariants
- Individual lifecycle commands are one-shot; `schedule reconcile` evaluates the desired state once, while `schedule run` is a foreground reconcile/wait/sleep/retry loop. Native replica cron configuration is a separate provider-managed mechanism.
- State-machine boundaries, status-catalogue versus action-permission rules, fail-closed discovery, and safe orchestration smoke checks: `mem:architecture/state_machines`.
- Predictable schedules use Simplismart native `cronScaling`, with `min_replicas=0` and no traffic scale-to-zero combination.
- Default output contracts and exit codes must remain safe for unattended cron/Kubernetes use.
- Credentials never enter CLI arguments; `SIMPLISMART_PG_TOKEN` comes from environment or `.env`.
- Container env updates use `update-env --from-env NAME` / `--unset NAME`, preserving existing values and verifying read-back. Actual writes require exactly DEPLOYED; pending/in-flight/stopped/unknown states cannot be edited. Unchanged configurations are read-only no-ops. For rotation, rollout/rollback checks, secret-safe output, and concurrency limitations see `mem:ops/environment_updates`.

See `mem:tech_stack` for tooling, `mem:conventions` for implementation patterns, and `mem:task_completion` for required checks.
For the Mac LaunchAgent that stops all non-deleted deployments at 23:00 IST (off-only, with the overnight wake-up safety window), see `mem:ops/local_auto_stop`.