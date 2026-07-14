# Simplismart Deployment CLI

A community-built, one-shot, automation-safe CLI around the
[Simplismart Python SDK](https://docs.simplismart.ai/sdk/python/overview).
It manages deployment lifecycle operations and native Simplismart cron
scaling without keeping a Python scheduler process alive.

## Why a one-shot CLI

- `start` treats `DEPLOYED` and the in-flight `PENDING` state as successful
  no-ops; `stop` is a no-op when already `STOPPED`. Repeated cron or Kubernetes
  CronJob invocations therefore do not submit duplicate lifecycle actions.
- Tables are automatic on terminals; redirected output is stable JSON.
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
`ORG_ID` and `SIMPLISMART_NAMESPACE` are configuration rather than secrets.
Keeping them beside the token makes local and scheduled invocations consistent,
and lets `restart` run without repetitive flags.

```dotenv
SIMPLISMART_PG_TOKEN=replace-me
ORG_ID=replace-with-org-uuid
SIMPLISMART_NAMESPACE=replace-with-kubernetes-namespace
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
# Interactive terminals get a table; pipes/files get JSON automatically
uv run simplismart-deploy list
uv run simplismart-deploy list --status DEPLOYED --count 50
uv run simplismart-deploy get DEPLOYMENT_NAME_OR_UUID
uv run simplismart-deploy status DEPLOYMENT_NAME_OR_UUID
uv run simplismart-deploy start DEPLOYMENT_NAME_OR_UUID
uv run simplismart-deploy stop DEPLOYMENT_NAME_OR_UUID
uv run simplismart-deploy restart DEPLOYMENT_NAME_OR_UUID
uv run simplismart-deploy health DEPLOYMENT_NAME_OR_UUID

# Force either format; SIMPLISMART_OUTPUT can set the same preference
uv run simplismart-deploy --output table list
uv run simplismart-deploy --output json list

# Wait for the accepted action to reach its real terminal condition
uv run simplismart-deploy start DEPLOYMENT_NAME_OR_UUID --wait
uv run simplismart-deploy stop DEPLOYMENT_NAME_OR_UUID --wait
uv run simplismart-deploy restart DEPLOYMENT_NAME_OR_UUID --wait --wait-timeout 900

# Readiness gates for scripts and Kubernetes probes
uv run simplismart-deploy health DEPLOYMENT_NAME_OR_UUID --require-healthy
uv run simplismart-deploy health DEPLOYMENT_NAME_OR_UUID --wait --wait-timeout 900
```

Every deployment argument accepts either a UUID or an exact deployment name.
Names must be unique; ambiguous names fail without changing either deployment.
`ORG_ID` is used when the API requires it. `restart` also needs the Kubernetes
namespace, resolved from command options, environment configuration, or
deployment details. `--wait` polls read-only status endpoints, prints progress
only on an interactive terminal, and emits one final JSON document.

## Daily schedules

Clock inputs accept both 12-hour and 24-hour forms such as `10am`, `10:00`,
`1:13 am`, and `01:13`. The timezone defaults to the computer's current local
IANA timezone. Use `--timezone Asia/Kolkata` (or another IANA name) to make the
schedule independent of the host configuration.

### Durable native replica window

This is the recommended mode when changing replica capacity is sufficient. It
stores the schedule in Simplismart, so it continues while this computer is
asleep or powered off:

```bash
uv run simplismart-deploy schedule daily nightly-inference \
  --on-at 10am \
  --off-at 1am \
  --desired-replicas 1 \
  --max-replicas 1
```

`10am` → `1am` is treated as an overnight window. The command sets
`min_replicas=0` and native `cronScaling`; it does not invoke the deployment
start/stop lifecycle endpoints. Inspect or clear it with:

```bash
uv run simplismart-deploy schedule show nightly-inference
uv run simplismart-deploy schedule clear nightly-inference
```

Advanced cron expressions remain available through `schedule set`:

```bash
uv run simplismart-deploy schedule set nightly-inference \
  --start '0 9 * * 1-5' \
  --end '0 18 * * 1-5' \
  --desired-replicas 2 \
  --max-replicas 2
```

Simplismart does not allow native cron scaling and traffic-based scale-to-zero
at the same time. Schedule clearing explicitly sends an empty `cronScaling`
list; omitting that field would leave the previous schedule unchanged.

### Foreground lifecycle scheduler

Use this mode when the deployment itself must be started and stopped:

```bash
uv run simplismart-deploy schedule run nightly-inference \
  --on-at 10am \
  --off-at 1am
```

The command immediately reconciles the current local time. Starting it at
11am starts the deployment unless it is already active, then waits until 1am
and stops it. Starting it at 2am ensures the deployment is stopped, then waits
for 10am. Waking late simply causes another current-time reconciliation.
Lifecycle transitions are idempotent. Transient API and wait failures retry
with bounded delay; authentication, configuration, and ambiguous-name errors
stop the process. JSON mode emits one compact JSON object per event (JSONL).

`schedule run` is intentionally a foreground process. For persistence across
logout or reboot, supervise it with launchd, systemd, or a container service;
do not rely on `nohup` or a detached terminal process.

Ready-to-edit service definitions are included:

- macOS: `examples/launchd/com.simplismart.deployment-schedule.plist`
- Linux: `examples/systemd/simplismart-deployment-schedule.service`

Both use `/opt/simplismart-deployment-cli`, the exact deployment name
`nightly-inference`, and the host's local timezone. Change those values first.
On macOS, install the launch agent so it starts at login and restarts if it
exits:

```bash
cp examples/launchd/com.simplismart.deployment-schedule.plist \
  ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) \
  ~/Library/LaunchAgents/com.simplismart.deployment-schedule.plist
launchctl kickstart -k \
  gui/$(id -u)/com.simplismart.deployment-schedule
```

On Linux:

```bash
sudo cp examples/systemd/simplismart-deployment-schedule.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now simplismart-deployment-schedule.service
```

### Finite boot or cron reconciliation

For an external scheduler, run the same current-time decision once:

```bash
uv run simplismart-deploy schedule reconcile nightly-inference \
  --on-at 10am \
  --off-at 1am \
  --wait
```

This is safe at boot, wake, or every few minutes from cron. If a 10am event was
missed and the machine starts at 11am, it starts the deployment. If the 1am
event was missed and the machine starts at 2am, it stops the deployment.
Inject `SIMPLISMART_PG_TOKEN` and `ORG_ID` from the scheduler's secret store;
never place credentials in command arguments.

A periodic Linux cron reconciliation provides the same late-boot correction
without a foreground process. `flock` prevents overlapping invocations:

```cron
*/5 * * * * cd /opt/simplismart-deployment-cli && flock -n /tmp/simplismart-schedule.lock .venv/bin/simplismart-deploy schedule reconcile nightly-inference --on-at 10am --off-at 1am >> /var/log/simplismart-schedule.log 2>&1
```

For a Kubernetes CronJob, set `concurrencyPolicy: Forbid` for the same reason.

## Exit codes

| Code | Meaning |
| ---: | --- |
| `0` | Success, including an idempotent no-op |
| `2` | Invalid CLI input or configuration |
| `3` | Authentication or authorization failure |
| `4` | Deployment not found |
| `5` | Other Simplismart API failure |
| `6` | `health --require-healthy` reported a non-healthy state |
| `7` | A waited lifecycle action failed or timed out |
| `70` | Unexpected local software failure |

## Development

All dependency operations use uv:

```bash
uv add PACKAGE
uv add --dev PACKAGE
uv lock
uv run pytest -q
```