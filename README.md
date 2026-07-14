# Simplismart Deployment CLI

A community-built, one-shot, automation-safe CLI around the
[Simplismart Python SDK](https://docs.simplismart.ai/sdk/python/overview).
It manages deployment lifecycle operations and native Simplismart cron
scaling without keeping a Python scheduler process alive.

## Why a one-shot CLI

- `start` treats `DEPLOYED` and the in-flight `PENDING` state as successful
  no-ops; `stop` is a no-op when already `STOPPED`. Repeated cron or Kubernetes
  CronJob invocations therefore do not submit duplicate lifecycle actions.
- JSON is the default output for logs and downstream automation.
- Failures use stable, meaningful exit codes.
- Scheduled capacity uses Simplismart's native `cronScaling` API. The schedule
  survives this process, uses an explicit IANA timezone, and scales to zero
  outside the configured window.

`restart` intentionally always requests a restart and is not idempotent.

## Setup

Requirements: [uv](https://docs.astral.sh/uv/) and Python 3.11 or newer.

```bash
uv sync
cp .env.example .env
```

Set the Playground token from Simplismart **Settings → API Key** in `.env`.
`ORG_ID` is configuration, not a secret, but keeping it beside the token makes
local and scheduled invocations consistent.

```dotenv
SIMPLISMART_PG_TOKEN=replace-me
ORG_ID=replace-with-org-uuid
```

The Simplismart API URL (`https://api.app.simplismart.ai`) and 300-second
timeout are defaults in the software, so they do not need to be copied into
`.env` or a scheduler manifest. Root options can override them for testing.
The token is accepted only through `SIMPLISMART_PG_TOKEN`, not a command-line
flag, so it is not exposed in process listings.

Run commands through uv during development:

```bash
uv run simplismart-deploy --help
```

After installing the package, use `simplismart-deploy` or its short alias,
`ssdeploy`.

## Deployment commands

```bash
# JSON output is the default
uv run simplismart-deploy list
uv run simplismart-deploy list --status DEPLOYED --count 50
uv run simplismart-deploy get DEPLOYMENT_UUID
uv run simplismart-deploy start DEPLOYMENT_UUID
uv run simplismart-deploy stop DEPLOYMENT_UUID
uv run simplismart-deploy restart DEPLOYMENT_UUID --namespace MODEL_NAMESPACE
uv run simplismart-deploy health DEPLOYMENT_UUID

# Human-readable output
uv run simplismart-deploy --output table list

# Readiness gate for scripts and Kubernetes probes
uv run simplismart-deploy health DEPLOYMENT_UUID --require-healthy
```

`ORG_ID` is used by lifecycle operations when the API requires it. Override it
for one invocation with the root `--org-id` option or the command-level option
on `start`, `stop`, and `restart`.

## Native schedule-based scaling

Keep two replicas running from 09:00 to 18:00 on weekdays in Asia/Kolkata,
with zero replicas outside that window:

```bash
uv run simplismart-deploy schedule set DEPLOYMENT_UUID \
  --timezone Asia/Kolkata \
  --start '0 9 * * 1-5' \
  --end '0 18 * * 1-5' \
  --desired-replicas 2 \
  --max-replicas 2
```

This sets `min_replicas=0` and sends Simplismart a native `cronScaling` rule.
Simplismart does not allow native cron scaling and traffic-based scale-to-zero
at the same time.

Remove the window and leave a normal non-zero replica range:

```bash
uv run simplismart-deploy schedule clear DEPLOYMENT_UUID \
  --min-replicas 1 \
  --max-replicas 2
```

The clear operation explicitly sends an empty `cronScaling` list; omitting that
field would leave an existing schedule unchanged.

## External cron and Kubernetes CronJobs

Prefer native schedule-based scaling for predictable start/end windows. Use an
external scheduler only when orchestration must depend on systems outside
Simplismart. Each invocation is finite and returns a process exit code, so the
same command works in crontab, a CI runner, or a Kubernetes CronJob:

```cron
0 9 * * 1-5 cd /opt/simplismart-deployment-cli && /usr/local/bin/uv run simplismart-deploy start DEPLOYMENT_UUID
0 18 * * 1-5 cd /opt/simplismart-deployment-cli && /usr/local/bin/uv run simplismart-deploy stop DEPLOYMENT_UUID
```

Inject `SIMPLISMART_PG_TOKEN` and `ORG_ID` from the scheduler's secret store.
Do not bake them into a crontab, container image, or command arguments. Set a
failed-job policy in the external scheduler; this CLI does not hide API errors
or retry destructive lifecycle actions.

## Exit codes

| Code | Meaning |
| ---: | --- |
| `0` | Success, including an idempotent no-op |
| `2` | Invalid CLI input or configuration |
| `3` | Authentication or authorization failure |
| `4` | Deployment not found |
| `5` | Other Simplismart API failure |
| `6` | `health --require-healthy` reported a non-healthy state |
| `70` | Unexpected local software failure |

## Development

All dependency operations use uv:

```bash
uv add PACKAGE
uv add --dev PACKAGE
uv lock
uv run pytest -q
```