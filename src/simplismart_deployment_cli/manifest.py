from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, model_validator
from simplismart import DeploymentCreate, ModelRepoCreate

_API_VERSION = "simplismart.ai/byoc/v1alpha1"
_ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_MODEL_REPOSITORY_KEYS = {
    "name",
    "sourceType",
    "registryPath",
    "dockerTag",
    "runtimeGpus",
    "env",
    "healthcheck",
    "ports",
    "metricsPath",
    "deploymentCustomConfiguration",
}


class ManifestError(ValueError):
    """Raised when a BYOC manifest cannot be safely applied."""


class _DeploymentSpec(BaseModel):
    """Validate manifest fields before a repository UUID is available."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=3, max_length=255)
    model_repo_name: str = Field(alias="modelRepoName", min_length=1)
    gpu_id: str = Field(alias="gpuId", min_length=1)
    min_pod_replicas: int = Field(alias="minPodReplicas", ge=0)
    max_pod_replicas: int = Field(alias="maxPodReplicas", ge=1)
    auth_enabled: bool | None = Field(default=False, alias="authEnabled")
    env_variables: dict[str, Any] | None = Field(default=None, alias="envVariables")
    healthcheck: dict[str, Any] | None
    ports: dict[str, Any] | None
    metrics_path: list[str] | None = Field(alias="metricsPath")
    deployment_custom_configuration: dict[str, Any] | None = Field(
        alias="deploymentCustomConfiguration"
    )
    deployment_tag: str | None = Field(default=None, alias="deploymentTag", min_length=1)

    @model_validator(mode="after")
    def _replica_configuration(self) -> _DeploymentSpec:
        if self.min_pod_replicas != 1 or self.max_pod_replicas != 1:
            raise ValueError(
                "BYOC manifests require minPodReplicas=maxPodReplicas=1; "
                "configure autoscaling with the schedule commands after deployment"
            )
        return self


@dataclass(frozen=True)
class ByocManifest:
    """Validated manifest data ready for Simplismart repository/deployment creation."""

    path: Path
    model_repo: ModelRepoCreate
    deployment: _DeploymentSpec

    @property
    def deployment_name(self) -> str:
        return self.deployment.name

    def deployment_payload(
        self, *, org_id: str, model_repo_id: str
    ) -> DeploymentCreate:
        """Build the deployment after resolving the repository UUID."""
        return DeploymentCreate(
            model_repo=model_repo_id,
            org=org_id,
            **self.deployment.model_dump(exclude={"model_repo_name"}),
        )


def load_byoc_manifest(path: str | Path, *, org_id: str) -> ByocManifest:
    """Load, substitute environment values, and validate a checked-in BYOC manifest."""
    manifest_path = Path(path)
    if manifest_path.suffix not in {".yaml", ".yml"}:
        raise ManifestError("manifest must use a .yaml or .yml extension")
    try:
        document = yaml.safe_load(manifest_path.read_text())
    except OSError as exc:
        raise ManifestError(f"could not read manifest {manifest_path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ManifestError(f"invalid YAML in {manifest_path}: {exc}") from exc

    root = _mapping(document, "manifest")
    _required_keys(
        root, {"apiVersion", "metadata", "modelRepository", "deployment"}, "manifest"
    )
    _reject_unknown_keys(
        root, {"apiVersion", "metadata", "modelRepository", "deployment"}, "manifest"
    )
    if root["apiVersion"] != _API_VERSION:
        raise ManifestError(
            f"manifest.apiVersion must be {_API_VERSION!r}, got {root['apiVersion']!r}"
        )

    metadata = _mapping(root["metadata"], "metadata")
    _required_keys(metadata, {"name", "model"}, "metadata")
    _string(metadata["name"], "metadata.name")
    _string(metadata["model"], "metadata.model")

    environment = _environment_values()
    model_repository = _expand_environment(
        _mapping(root["modelRepository"], "modelRepository"), environment
    )
    deployment = _expand_environment(
        _mapping(root["deployment"], "deployment"), environment
    )
    _required_keys(
        model_repository,
        {
            "name",
            "sourceType",
            "registryPath",
            "dockerTag",
            "runtimeGpus",
            "healthcheck",
            "ports",
            "metricsPath",
            "deploymentCustomConfiguration",
        },
        "modelRepository",
    )
    _reject_unknown_keys(model_repository, _MODEL_REPOSITORY_KEYS, "modelRepository")
    deployment_spec = _DeploymentSpec.model_validate(deployment)
    model_repo_name = _string(model_repository["name"], "modelRepository.name")
    if deployment_spec.model_repo_name != model_repo_name:
        raise ManifestError(
            "deployment.modelRepoName must exactly match modelRepository.name"
        )

    model_repo = ModelRepoCreate(
        name=model_repo_name,
        org_id=org_id,
        source_type=_string(
            model_repository["sourceType"], "modelRepository.sourceType"
        ),
        registry_path=_string(
            model_repository["registryPath"], "modelRepository.registryPath"
        ),
        docker_tag=_string(model_repository["dockerTag"], "modelRepository.dockerTag"),
        runtime_gpus=model_repository["runtimeGpus"],
        **_optional_model_repo_values(model_repository),
    )
    return ByocManifest(
        path=manifest_path, model_repo=model_repo, deployment=deployment_spec
    )


def _optional_model_repo_values(values: dict[str, Any]) -> dict[str, Any]:
    aliases = {
        "env": "env",
        "healthcheck": "healthcheck",
        "ports": "ports",
        "metricsPath": "metrics_path",
        "deploymentCustomConfiguration": "deployment_custom_configuration",
    }
    return {
        payload_key: values[manifest_key]
        for manifest_key, payload_key in aliases.items()
        if manifest_key in values
    }


def _environment_values() -> dict[str, str]:
    values = {
        key: value
        for key, value in dotenv_values(".env").items()
        if isinstance(key, str) and value is not None
    }
    values.update(os.environ)
    return values


def _expand_environment(value: Any, environment: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {
            key: _expand_environment(item, environment) for key, item in value.items()
        }
    if isinstance(value, list):
        return [_expand_environment(item, environment) for item in value]
    if not isinstance(value, str):
        return value

    missing = {match.group(1) for match in _ENV_REFERENCE.finditer(value)} - set(
        environment
    )
    if missing:
        names = ", ".join(sorted(missing))
        raise ManifestError(f"undefined environment variable(s): {names}")
    return _ENV_REFERENCE.sub(lambda match: environment[match.group(1)], value)


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManifestError(f"{label} must be a mapping")
    return value


def _required_keys(values: dict[str, Any], required: set[str], label: str) -> None:
    missing = sorted(required - set(values))
    if missing:
        raise ManifestError(
            f"{label} is missing required field(s): {', '.join(missing)}"
        )


def _reject_unknown_keys(values: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise ManifestError(f"{label} has unsupported field(s): {', '.join(unknown)}")


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{label} must be a non-empty string")
    return value
