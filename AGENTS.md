# Agent Instructions

## Simplismart deployment lifecycle

- Use the current organization-scoped deletion API only:
  ```python
  client.delete_deployment(
      deployment_id=deployment_id,
      org_id=org_id,
  )
  ```
- Never call the legacy `client.delete_model_deployment(...)` method. It targets a different endpoint and can leave resource allocation unclear.
- Route destructive operations through `DeploymentManager.delete(...)`; do not issue ad hoc SDK deletion calls from notebooks, scripts, or one-off sessions.
- Before deleting, list and inspect the exact deployment ID, name, status, and health. Never infer that a failed or paused deployment is gone.
- After deleting, verify the deployment reaches a terminal/deleted state and audit the deployment list and GPU allocation before creating a replacement.
- Do not delete a known-healthy deployment merely to establish a performance baseline. Keep it running when capacity permits; otherwise state the expected downtime and preserve its configuration and benchmark artifacts.
- Create a fresh deployment only when the command/configuration actually changes or when an independent test instance is required. Record the exact argv, image, model, GPU, and deployment ID.
- Do not attribute image-pull or platform `FetchFailed` errors to container command flags unless the container reached startup and emitted command-related logs.

## Verification

- Health is necessary but insufficient for parser changes. For tool-call parser work, send an OpenAI-compatible request with a real `tools` schema and forced or obvious tool choice; verify structured `message.tool_calls[*].function.name` and JSON-parseable arguments.
- Keep latency, TTFT, throughput, failures, and per-request attempts in readable benchmark artifacts. Never include API keys or HF tokens.

## Secrets

- Load credentials from ignored environment files or the platform secret mechanism. Never print or commit raw API keys, HF tokens, or control-plane credentials.
