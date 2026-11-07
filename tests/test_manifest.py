from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from simplismart_deployment_cli.manifest import ManifestError, load_byoc_manifest


def _write_manifest(path: Path) -> Path:
    path.write_text(
        """\
apiVersion: simplismart.ai/byoc/v1alpha1
metadata:
  name: test-deployment
  model: test/model
modelRepository:
  name: test-repository
  sourceType: docker_hub
  registryPath: example/image
  dockerTag: v1
  runtimeGpus: 1
  env:
    API_KEY: ${TEST_API_KEY}
  healthcheck:
    path: /health
    port: 8000
  ports:
    http:
      port: 8000
  metricsPath:
    - /v1/chat/completions
  deploymentCustomConfiguration:
    command: [python3, -m, server, --api-key, "${TEST_API_KEY}"]
deployment:
  name: test-deployment
  modelRepoName: test-repository
  gpuId: nvidia-h100
  minPodReplicas: 1
  maxPodReplicas: 1
  authEnabled: false
  envVariables:
    API_KEY: ${TEST_API_KEY}
  healthcheck:
    path: /health
    port: 8000
  ports:
    http:
      port: 8000
  metricsPath:
    - /v1/chat/completions
  deploymentCustomConfiguration:
    command: [python3, -m, server, --api-key, "${TEST_API_KEY}"]
"""
    )
    return path


def test_manifest_requires_defined_environment_variables(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TEST_API_KEY", raising=False)

    with pytest.raises(ManifestError):
        load_byoc_manifest(
            _write_manifest(tmp_path / "deployment.yaml"),
            org_id="c8d34a37-61f3-4d7a-9208-23a39e6dd645",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("minPodReplicas", 0),
        ("envVariables", ["not", "a", "mapping"]),
    ],
)
def test_manifest_rejects_invalid_deployment_before_repository_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TEST_API_KEY", "local-test-value")
    path = _write_manifest(tmp_path / "deployment.yaml")
    document = yaml.safe_load(path.read_text())
    document["deployment"][field] = value
    path.write_text(yaml.safe_dump(document))

    with pytest.raises(ValueError):
        load_byoc_manifest(
            path,
            org_id="c8d34a37-61f3-4d7a-9208-23a39e6dd645",
        )
