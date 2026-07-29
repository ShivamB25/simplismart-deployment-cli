# Qwen3.6-35B-A3B-FP8 deployment profiles

These YAML files are Simplismart BYOC manifests consumed by this repository's
`deploy` command. They are not sent directly to the Simplismart API.

## How to use them

```bash
uv run simplismart-deploy deploy \
  deployments/qwen3.6-35b-a3b-fp8/sglang-simple.yaml \
  --wait
```

The command:

1. Expands `${VARIABLE}` values from the environment or ignored `.env`.
2. Creates or validates the `modelRepository`.
3. Resolves `deployment.modelRepoName` to the returned model-repository UUID.
4. Creates a missing/deleted deployment or resumes an exact matching one.

It never overwrites a non-matching deployment or model repository. Inspect and
delete it explicitly before applying changed argv, image, or model settings.

## Profiles

| File | Purpose |
|---|---|
| `sglang-throughput.yaml` | Latency/throughput-optimized SGLang with MTP |
| `sglang-simple.yaml` | Minimal SGLang startup control |
| `vllm-throughput.yaml` | Latency/throughput-optimized vLLM with MTP |
| `vllm-simple.yaml` | Minimal vLLM startup control |

## Shared benchmark auth

All four profiles use the same temporary benchmark key via:

- SGLang: `--api-key <value>`
- vLLM: `--api-key <value>` and `VLLM_API_KEY`
- Visible env: `BENCHMARK_API_KEY`

Simplismart gateway auth is disabled (`authEnabled: false`) so clients only need this one key:

```http
Authorization: Bearer <BENCHMARK_API_KEY>
```

Keep the key out of public chat when possible; rotate it after shared testing.
