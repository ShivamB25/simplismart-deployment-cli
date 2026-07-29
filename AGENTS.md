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

## Verification and secrets

- Health alone is insufficient for parser changes. Send an OpenAI-compatible
  forced tool call with a real `tools` schema; verify
  `message.tool_calls[*].function.name` and JSON-parseable arguments.
- Keep benchmark latency, TTFT, throughput, failures, and attempts in readable
  ignored artifacts. Never include API keys, HF tokens, or control-plane tokens.
- Load credentials only from ignored `.env` files or platform secrets. Never
  print or commit them.
