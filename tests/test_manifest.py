from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from simplismart_deployment_cli.manifest import ManifestError, load_byoc_manifest
from simplismart_deployment_cli.manager import DeploymentManager
from simplismart_deployment_cli.settings import Settings


class ManifestClient:
    def __init__(self) -> None:
        self.model_repos: list[dict[str, Any]] = []
        self.deployments: list[dict[str, Any]] = []
        self.calls: list[tuple[str, Any]] = []

    def list_model_repos(self, params: Any) -> dict[str, list[dict[str, Any]]]:
        self.calls.append(("list_model_repos", params.model_dump()))
        return {"results": self.model_repos}

    def create_model_repo(self, payload: Any) -> dict[str, Any]:
        self.calls.append(("create_model_repo", payload.model_dump()))
        model_repo = {
            "uuid": "4d230b52-5dfb-45a8-a07a-d6f1ab5278bc",
            "name": payload.name,
            "source_url": f"{payload.registry_path}:{payload.docker_tag}",
        }
        self.model_repos.append(model_repo)
        return model_repo

    def list_deployments(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("list_deployments", kwargs))
        return self.deployments

    def create_deployment(self, payload: Any) -> dict[str, Any]:
        self.calls.append(("create_deployment", payload.model_dump()))
        deployment = {
            "deployment_id": "3c5fefc8-8775-49b0-bd9f-6caeb3f19e8e",
            "deployment_name": payload.name,
            "status": "PENDING",
        }
        self.deployments.append(deployment)
        return deployment


def _write_manifest(path: Path, *, model_repo_name: str = "test-repository") -> Path:
    path.write_text(
        f"""\
apiVersion: simplismart.ai/byoc/v1alpha1
metadata:
  name: test-deployment
  model: test/model
modelRepository:
  name: {model_repo_name}
  sourceType: docker_hub
  registryPath: example/image
  dockerTag: v1
  runtimeGpus: 1
  env:
    API_KEY: ${{TEST_API_KEY}}
  healthcheck:
    path: /health
    port: 8000
  ports:
    http:
      port: 8000
  metricsPath:
    - /v1/chat/completions
  deploymentCustomConfiguration:
    command: [python3, -m, server, --api-key, "${{TEST_API_KEY}}"]
deployment:
  name: test-deployment
  modelRepoName: {model_repo_name}
  gpuId: nvidia-h100
  minPodReplicas: 1
  maxPodReplicas: 1
  authEnabled: false
  envVariables:
    API_KEY: ${{TEST_API_KEY}}
  healthcheck:
    path: /health
    port: 8000
  ports:
    http:
      port: 8000
  metricsPath:
    - /v1/chat/completions
  deploymentCustomConfiguration:
    command: [python3, -m, server, --api-key, "${{TEST_API_KEY}}"]
"""
    )
    return path


def test_checked_in_manifests_parse_with_environment_substitution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BENCHMARK_API_KEY", "benchmark-key")
    monkeypatch.setenv("HF_TOKEN", "hf-token")

    manifest_paths = sorted(Path("deployments").glob("**/*.yaml"))
    assert manifest_paths
    for path in manifest_paths:
        manifest = load_byoc_manifest(
            path, org_id="c8d34a37-61f3-4d7a-9208-23a39e6dd645"
        )
        assert manifest.deployment["modelRepoName"] == manifest.model_repo.name


def test_manifest_requires_defined_environment_variables(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEST_API_KEY", raising=False)

    with pytest.raises(ManifestError, match="TEST_API_KEY"):
        load_byoc_manifest(
            _write_manifest(tmp_path / "deployment.yaml"),
            org_id="c8d34a37-61f3-4d7a-9208-23a39e6dd645",
        )


def test_deploy_manifest_creates_repository_and_deployment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_API_KEY", "test-key")
    client = ManifestClient()
    deployment_manager = DeploymentManager(
        Settings(
            pg_token="test-token",
            org_id="c8d34a37-61f3-4d7a-9208-23a39e6dd645",
        ),
        client=client,
    )

    result = deployment_manager.deploy_manifest(
        str(_write_manifest(tmp_path / "deployment.yaml"))
    )

    assert result["deployment_created"] is True
    assert result["model_repo_created"] is True
    model_repo_payload = next(
        payload for name, payload in client.calls if name == "create_model_repo"
    )
    assert model_repo_payload["env"]["API_KEY"] == "test-key"
    deployment_payload = next(
        payload for name, payload in client.calls if name == "create_deployment"
    )
    assert deployment_payload["model_repo"] == "4d230b52-5dfb-45a8-a07a-d6f1ab5278bc"
    assert (
        deployment_payload["deployment_custom_configuration"]["command"][-1]
        == "test-key"
    )


def test_qat_manifest_preserves_vllm_health_and_serving_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BENCHMARK_API_KEY", "benchmark-key")
    monkeypatch.setenv("HF_TOKEN", "hf-token")

    manifest = load_byoc_manifest(
        "deployments/gemma-4-31b-it/vllm-qat.yaml",
        org_id="c8d34a37-61f3-4d7a-9208-23a39e6dd645",
    )
    payload = manifest.deployment_payload(
        org_id="c8d34a37-61f3-4d7a-9208-23a39e6dd645",
        model_repo_id="4d230b52-5dfb-45a8-a07a-d6f1ab5278bc",
    )

    assert manifest.model_repo.healthcheck == {
        "path": "/health",
        "port": 8000,
        "initialDelaySeconds": 900,
        "periodSeconds": 10,
        "timeoutSeconds": 5,
        "failureThreshold": 300,
    }
    assert payload.healthcheck == manifest.model_repo.healthcheck
    assert payload.deployment_custom_configuration == {
        "command": [
            "vllm",
            "serve",
            "google/gemma-4-31B-it-qat-w4a16-ct",
            "--served-model-name",
            "google/gemma-4-31B-it",
            "--host",
            "0.0.0.0",
            "--port",
            "8000",
            "--tensor-parallel-size",
            "1",
            "--max-model-len",
            "32768",
            "--gpu-memory-utilization",
            "0.94",
            "--max-num-seqs",
            "128",
            "--max-num-batched-tokens",
            "32768",
            "--enable-chunked-prefill",
            "--enable-prefix-caching",
            "--async-scheduling",
            "--language-model-only",
            "--mm-processor-cache-gb",
            "0",
            "--renderer-num-workers",
            "4",
            "--performance-mode",
            "balanced",
            "--kv-cache-dtype",
            "auto",
            "--enable-auto-tool-choice",
            "--tool-call-parser",
            "gemma4",
            "--reasoning-parser",
            "gemma4",
        ]
    }
