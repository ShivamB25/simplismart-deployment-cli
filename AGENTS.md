# Agent Instructions

## Repository workflow

- Deploy only checked-in `deployments/<model>/*.yaml` manifests through:
  ```bash
  uv run simplismart-deploy deploy DEPLOYMENT_MANIFEST --wait-timeout 1800
  ```
  These are repository-local BYOC manifests, not documents to upload directly
  to the Simplismart dashboard.
- `${VARIABLE}` values resolve from the process environment or ignored `.env`.
  Missing variables fail before any API call.
- vLLM manifests pin `vllm/vllm-openai:v0.26.0-cu129`. When changing an image,
  change the versioned `modelRepository.name` and matching
  `deployment.modelRepoName`; never reuse a repository with a different image.
- Keep `HF_TOKEN` as the only Hugging Face credential variable. Never add
  `HUGGING_FACE_HUB_TOKEN` or `HUGGINGFACE_HUB_TOKEN`.
- For Gemma QAT, use the tokenizer's default chat template. Do not pass the
  unavailable `examples/tool_chat_template_gemma4.jinja` override or
  `--default-chat-template-kwargs`.

## Simplismart deployment lifecycle

- Use the current organization-scoped deletion API only:
  ```python
  client.delete_deployment(deployment_id=deployment_id, org_id=org_id)
  ```
- Never call `client.delete_model_deployment(...)`.
- Route destructive operations through `DeploymentManager.delete(...)`.
- Before deleting, inspect the exact ID, name, status, and health. After
  deleting, verify the terminal state and GPU allocation before replacement.
- Do not delete a healthy deployment to establish a baseline. Create a fresh
  deployment only for a configuration change or independent test; record argv,
  image, model, GPU, and deployment ID.
- Do not attribute `FetchFailed` to command flags without startup logs.

## State-machine changes

- Inventory meaningful stateful workflows before changing them: observed state,
  desired state, guards, side effects, completion criteria, and failure exits.
  Do not turn every conditional into an FSM or introduce a generic framework.
- A documented status catalogue is not a per-action permission matrix. SDK
  `0.1.15` documents 17 deployment states, not just the five CLI list filters;
  this includes edit, warm-pool, and failed-deletion states. Preserve provider
  dispatch for recognized states instead of guessing new lifecycle bans.
  Recheck the catalogue when upgrading the SDK.
- Preserve start no-ops for `DEPLOYED`/`PENDING`, stop no-op for `STOPPED`, and
  intentionally non-idempotent restart. Missing/unrecognized states fail closed;
  start/stop/restart refuse `DELETED`, and deleting a deleted UUID is a no-op.
- Keep status separate from health, and saved configuration separate from a
  verified rollout. Native replica windows, finite `schedule reconcile`, and
  foreground `schedule run` are different workflows, not interchangeable.
- Validate manifest input before API calls. Malformed listings or unavailable
  detail for a matched ID are not proof of absence: refuse creation rather than
  silently selecting a create/replacement path.
- Creation, environment edits, and multi-target commands are not transactions.
  A failure may follow applied side effects; inspect before retrying. Do not add
  automatic rollback, delete/recreate, or persistent workflow machinery.
- Bound benchmark workers by concurrency; reject non-positive counts before
  HTTP. Streaming success requires `[DONE]`; truncated, malformed, or in-band
  error streams fail even when usage reaches the token budget.
- Stop-job locking must work on macOS and Linux, span listing through actual
  stop completion, and release on process exit. Do not require a `flock`
  executable or let listing/parsing failures look like an empty successful run.
- Architecture and verification boundaries: Serena `mem:architecture/state_machines`.

## Deployment environment updates

- Rotate container credentials with
  `uv run simplismart-deploy update-env NAME_OR_UUID --from-env HF_TOKEN --wait --wait-timeout 1800`.
  Repeat `--from-env NAME` or `--unset NAME`; never pass secret values in argv.
- Route env changes through `DeploymentManager.update_env(...)`: read current
  env, preserve unrelated variables, edit through SDK `DeploymentUpdate`, and
  verify exact read-back. Missing/malformed current env must fail closed.
- Actual environment mutations require status `DEPLOYED`; refuse creation,
  in-progress, stopped, terminal, unknown, or missing states. Identical values
  remain read-only no-ops. Never overlap an initial rollout with an env edit.
- `verified` without `--wait` means saved configuration only. Waiting must check
  DEPLOYED/HEALTHY and recheck desired keys/removals to detect a healthy rollback.
- Return key-only change summaries and minimal final status/health; never raw
  deployment detail, env values, provider mutation responses, or echoed secrets.
- Updates are not atomic; do not race another edit or delete/recreate for a token
  change. A failed verification may follow an applied edit: inspect before retry.
- Keep manifest env references consistent; rotating `.env`'s `HF_TOKEN` needs no
  YAML edit when `${HF_TOKEN}` is already used. SDK minimum: `0.1.15`.
- Environment-update verification scope and operations: Serena
  `mem:ops/environment_updates`.

## Local off-only auto-stop

- Native Simplismart schedules always require both `--on-at` and `--off-at`.
  There is no off-only native window.
- Nightly stop-only uses `scripts/stop-all-deployments.sh` via LaunchAgent
  `com.simplismart.stop-all-deployments` at local 23:00.
- The script must refuse stops outside **23:00–08:00 Asia/Kolkata**. launchd
  can fire a missed 11pm job on later wake; without this guard, an 11am wake
  would stop daytime GPUs. Override only with `FORCE_STOP=1`.
- Keep the LaunchAgent WorkingDirectory on this project root so `.env` and
  `.venv` resolve. Details/commands: Serena `mem:ops/local_auto_stop`.

## Verification and secrets

- Health alone is insufficient for parser changes. Send an OpenAI-compatible
  forced tool call with a real `tools` schema; verify
  `message.tool_calls[*].function.name` and JSON-parseable arguments.
- Keep benchmark latency, TTFT, throughput, failures, and attempts in readable
  ignored artifacts. Never include API keys, HF tokens, or control-plane tokens.
- Load credentials only from ignored `.env` files or platform secrets. Never
  print or commit them.
- Redact HF token-shaped strings in scalar output/error messages and nested
  command arrays, not only token-named fields. Treat model repository `env` and
  `environment` dictionaries as sensitive alongside deployment env variables.
- Incident audits must not print secret matches, prefixes, hashes, raw histories,
  or unredacted scanner reports. Revocation is containment, not proof of cause;
  distinguish public Git refs, local stash/reflog objects, and ignored artifacts.

## Tests

- Do not reintroduce fake SDK clients/managers, mock-echo tests, call-forwarding
  assertions, or help/display-wording snapshots.
- Keep permanent tests on actual parsing, input validation, and scheduling logic.
  Report CLI smoke evidence separately; a local HTTP fixture is not a live
  Simplismart integration check.
- Keep the smallest set covering distinct consumer-visible failure modes. Do
  not add private-helper matrices, repeated invalid SDK-field/type variants,
  or tests that merely pin dependency validators, copies, or incidental defaults.
  Retain meaningful boundaries such as manifest fail-fast validation and DST.
- No test-count targets: more cases are not evidence of better coverage.
  Remove redundant/obsolete cases instead of re-pinning implementation details.
- Use throwaway localhost HTTP or isolated shell smoke scenarios for orchestration;
  never mutate live deployments merely to test a guard. Report fixture/tokenizer
  substitutions and other unexercised boundaries explicitly.
- Exercise the changed behavior, not just startup: `--help` is only a startup
  check. Run the relevant suite after final edits and remove throwaway scripts
  before committing. Guidance-only changes need no new tests or cloud calls.
