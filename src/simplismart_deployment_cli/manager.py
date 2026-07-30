from __future__ import annotations

from collections.abc import Callable
from time import monotonic, sleep
from typing import Any
from uuid import UUID

from simplismart import CronScalingRule, Simplismart

from .settings import Settings


class DeploymentNotFoundError(ValueError):
    """No deployment matched an exact name."""


class AmbiguousDeploymentError(ValueError):
    """More than one deployment matched an exact name."""


class DeploymentWaitError(RuntimeError):
    """A deployment did not reach the requested state."""


class DeploymentWaitTimeout(DeploymentWaitError):
    """A deployment wait exceeded its deadline."""


class DeploymentManager:
    """Small, automation-safe facade over the Simplismart deployment SDK."""

    def __init__(
        self,
        settings: Settings,
        client: Any | None = None,
        *,
        clock: Callable[[], float] = monotonic,
        sleeper: Callable[[float], None] = sleep,
    ) -> None:
        self._settings = settings
        self._client = client or Simplismart(
            pg_token=settings.pg_token.get_secret_value(),
            base_url=settings.base_url,
            timeout=settings.timeout,
        )
        self._clock = clock
        self._sleeper = sleeper

    def deploy_manifest(self, path: str) -> dict[str, Any]:
        """Create or resume the exact BYOC deployment declared in a YAML manifest."""
        from .manifest import load_byoc_manifest

        org_id = self._settings.org_id
        if not org_id:
            raise ValueError("ORG_ID is required to deploy a manifest")
        manifest = load_byoc_manifest(path, org_id=org_id)
        model_repo, model_repo_created = self._manifest_model_repo(manifest.model_repo)
        model_repo_id = self._detail_value(model_repo, "uuid", "id")
        if not model_repo_id:
            raise RuntimeError("Simplismart did not return a model repository ID")
        desired_deployment = manifest.deployment_payload(
            org_id=org_id, model_repo_id=model_repo_id
        )

        active_matches = {
            deployment_id: row
            for deployment_id, row in self._deployments_named(manifest.deployment_name).items()
            if self._deployment_status(row) != "DELETED"
        }
        if len(active_matches) > 1:
            ids = ", ".join(sorted(active_matches))
            raise AmbiguousDeploymentError(
                "more than one non-deleted deployment is named "
                f"{manifest.deployment_name!r}: {ids}"
            )
        if not active_matches:
            deployment = self._client.create_deployment(desired_deployment)
            deployment_id = self._detail_value(deployment, "deployment_id", "uuid", "id")
            if not deployment_id:
                raise RuntimeError("Simplismart did not return a deployment ID")
            deployment_created = True
        else:
            deployment_id = next(iter(active_matches))
            deployment = self._client.get_model_deployment(deployment_id=deployment_id)
            if self._deployment_status(deployment) == "DELETED":
                deployment = self._client.create_deployment(desired_deployment)
                deployment_id = self._detail_value(
                    deployment, "deployment_id", "uuid", "id"
                )
                if not deployment_id:
                    raise RuntimeError("Simplismart did not return a deployment ID")
                deployment_created = True
            else:
                existing_model_repo_id = self._detail_value(
                    deployment, "model_repo", "model_repo_id"
                )
                if existing_model_repo_id and existing_model_repo_id != model_repo_id:
                    raise ValueError(
                        f"existing deployment {manifest.deployment_name!r} uses a different model repository"
                    )
                existing_command = deployment.get("deployment_custom_configuration")
                if (
                    existing_command is not None
                    and existing_command
                    != desired_deployment.deployment_custom_configuration
                ):
                    raise ValueError(
                        f"existing deployment {manifest.deployment_name!r} has a different command; "
                        "inspect its logs, then delete and recreate it explicitly"
                    )
                deployment = self.start(deployment_id, org_id=org_id)
                deployment_created = False

        return {
            "manifest": str(manifest.path),
            "model_repo": model_repo,
            "deployment": deployment,
            "deployment_id": deployment_id,
            "model_repo_created": model_repo_created,
            "deployment_created": deployment_created,
        }

    def _manifest_model_repo(self, desired_model_repo: Any) -> tuple[Any, bool]:
        from simplismart import ModelRepoListParams

        response = self._client.list_model_repos(
            ModelRepoListParams(
                org_id=desired_model_repo.org_id,
                name=desired_model_repo.name,
                count=20,
            )
        )
        rows = self._result_rows(response)
        matches = [
            row
            for row in rows
            if isinstance(row, dict) and row.get("name") == desired_model_repo.name
        ]
        if len(matches) > 1:
            raise AmbiguousDeploymentError(
                f"multiple model repositories are named {desired_model_repo.name!r}"
            )
        if not matches:
            return self._client.create_model_repo(desired_model_repo), True

        model_repo = matches[0]
        expected_image = (
            f"{desired_model_repo.registry_path}:{desired_model_repo.docker_tag}"
        )
        byom = model_repo.get("byom")
        actual_image = model_repo.get("source_url") or (
            byom.get("image") if isinstance(byom, dict) else None
        )
        if actual_image != expected_image:
            raise ValueError(
                f"existing model repository {desired_model_repo.name!r} uses "
                f"{actual_image!r}, expected {expected_image!r}"
            )
        return model_repo, False


    def list(
        self,
        *,
        model_repo_id: str | None = None,
        status: str | None = None,
        offset: int = 0,
        count: int | None = None,
    ) -> list[dict[str, Any]]:
        """List the requested deployments across the SDK's 20-row pages."""
        rows: list[dict[str, Any]] = []
        next_offset = offset
        remaining = count
        sdk_page_size = 20

        while remaining is None or remaining > 0:
            page_count = (
                sdk_page_size if remaining is None else min(sdk_page_size, remaining)
            )
            page = self._client.list_deployments(
                model_repo_id=model_repo_id,
                status=status,
                offset=next_offset,
                count=page_count,
            )
            page_rows = self._result_rows(page)
            rows.extend(page_rows)
            if len(page_rows) < page_count:
                break

            next_offset += len(page_rows)
            if remaining is not None:
                remaining -= len(page_rows)

        return rows

    def _deployments_named(self, name: str) -> dict[str, dict[str, Any]]:
        """Return every deployment with an exact name, indexed by deployment ID."""
        matches: dict[str, dict[str, Any]] = {}
        offset = 0
        page_size = 100
        while True:
            page = self.list(offset=offset, count=page_size)
            rows = self._result_rows(page)
            for row in rows:
                row_name = row.get("deployment_name") or row.get("name")
                deployment_id = self._detail_value(row, "deployment_id", "uuid", "id")
                if row_name == name and deployment_id:
                    matches[deployment_id] = row
            if len(rows) < page_size:
                return matches
            offset += page_size

    def resolve_id(self, reference: str) -> str:
        """Resolve a deployment UUID or an exact, unique deployment name."""
        try:
            UUID(reference)
        except ValueError:
            pass
        else:
            return reference

        matches: dict[str, dict[str, Any]] = {}
        offset = 0
        page_size = 100
        while True:
            page = self.list(offset=offset, count=page_size)
            rows = self._result_rows(page)
            for row in rows:
                name = row.get("deployment_name") or row.get("name")
                deployment_id = self._detail_value(row, "deployment_id", "uuid", "id")
                if name == reference and deployment_id:
                    matches[deployment_id] = row
            if len(rows) < page_size:
                break
            offset += page_size

        if not matches:
            raise DeploymentNotFoundError(
                f"no deployment named {reference!r}; use `list` to see exact names"
            )
        if len(matches) > 1:
            ids = ", ".join(sorted(matches))
            raise AmbiguousDeploymentError(
                f"deployment name {reference!r} is ambiguous; matching IDs: {ids}"
            )
        return next(iter(matches))

    def get(self, reference: str) -> Any:
        deployment_id = self.resolve_id(reference)
        return self._client.get_model_deployment(deployment_id=deployment_id)

    def status(self, reference: str) -> dict[str, Any]:
        deployment_id = self.resolve_id(reference)
        detail = self._client.get_model_deployment(deployment_id=deployment_id)
        deployment = (
            {
                "deployment_id": deployment_id,
                "deployment_name": detail.get("deployment_name") or detail.get("name"),
                "status": detail.get("status") or detail.get("deployment_status"),
                "model_repo": detail.get("model_repo"),
                "accelerator_type": detail.get("accelerator_type"),
                "min_pod_replicas": detail.get("min_pod_replicas"),
                "max_pod_replicas": detail.get("max_pod_replicas"),
                "updated_at": detail.get("updated_at"),
            }
            if isinstance(detail, dict)
            else {"deployment_id": deployment_id}
        )
        return {
            "deployment": deployment,
            "health": self._client.fetch_deployment_health(
                deployment_id=deployment_id
            ),
        }

    def start(self, reference: str, *, org_id: str | None = None) -> dict[str, Any]:
        deployment_id = self.resolve_id(reference)
        detail = self._client.get_model_deployment(deployment_id=deployment_id)
        status = self._deployment_status(detail)
        if status in {"DEPLOYED", "PENDING"}:
            return self._unchanged(deployment_id, status)

        result = self._client.start_deployment(
            deployment_id=deployment_id,
            org_id=org_id or self._settings.org_id,
        )
        return self._changed(deployment_id, "start", result)

    def stop(self, reference: str, *, org_id: str | None = None) -> dict[str, Any]:
        deployment_id = self.resolve_id(reference)
        detail = self._client.get_model_deployment(deployment_id=deployment_id)
        status = self._deployment_status(detail)
        if status == "STOPPED":
            return self._unchanged(deployment_id, status)

        result = self._client.stop_deployment(
            deployment_id=deployment_id,
            org_id=org_id or self._settings.org_id,
        )
        return self._changed(deployment_id, "stop", result)
    def delete(self, reference: str, *, org_id: str | None = None) -> dict[str, Any]:
        """Delete a deployment through the current organization-scoped SDK API."""
        deployment_id = self.resolve_id(reference)
        resolved_org_id = org_id or self._settings.org_id
        if not resolved_org_id:
            raise ValueError("delete needs an organization; set ORG_ID or pass --org-id")

        result = self._client.delete_deployment(
            deployment_id=deployment_id,
            org_id=resolved_org_id,
        )
        return self._changed(deployment_id, "delete", result)


    def restart(
        self,
        reference: str,
        *,
        namespace: str | None = None,
        org_id: str | None = None,
    ) -> dict[str, Any]:
        deployment_id = self.resolve_id(reference)
        resolved_org_id = org_id or self._settings.org_id
        resolved_namespace = namespace or self._settings.deployment_namespace

        if not resolved_org_id or not resolved_namespace:
            detail = self._client.get_model_deployment(
                deployment_id=deployment_id
            )
            resolved_org_id = resolved_org_id or self._org_id(detail)
            resolved_namespace = resolved_namespace or self._namespace(detail)

        if not resolved_org_id:
            raise ValueError(
                "restart needs an organization; set ORG_ID or pass --org-id"
            )
        if not resolved_namespace:
            raise ValueError(
                "restart needs a namespace; set SIMPLISMART_NAMESPACE or pass --namespace"
            )

        result = self._client.restart_deployment(
            deployment_id=deployment_id,
            org_id=resolved_org_id,
            namespace=resolved_namespace,
        )
        return self._changed(deployment_id, "restart", result)

    def health(self, reference: str) -> Any:
        deployment_id = self.resolve_id(reference)
        return self._client.fetch_deployment_health(deployment_id=deployment_id)

    def wait_for_status(
        self,
        reference: str,
        *,
        target_status: str,
        require_healthy: bool,
        timeout: float,
        poll_interval: float,
        on_progress: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        if timeout <= 0:
            raise ValueError("wait timeout must be greater than zero")
        if poll_interval <= 0:
            raise ValueError("poll interval must be greater than zero")
        deployment_id = self.resolve_id(reference)
        target_status = target_status.upper()
        deadline = self._clock() + timeout
        last_status: str | None = None
        last_health: str | None = None

        while True:
            detail = self._client.get_model_deployment(
                deployment_id=deployment_id
            )
            last_status = self._deployment_status(detail)
            health: Any = None

            if last_status in {"FAILED", "DELETED"}:
                raise DeploymentWaitError(
                    f"deployment entered terminal state {last_status} while waiting for {target_status}"
                )

            if last_status == target_status:
                if not require_healthy:
                    return {"deployment": detail, "health": None}
                health = self._client.fetch_deployment_health(
                    deployment_id=deployment_id
                )
                last_health = self._health_status(health)
                if last_health == "HEALTHY":
                    return {"deployment": detail, "health": health}

            if on_progress:
                summary = f"status={last_status or 'UNKNOWN'}"
                if require_healthy and last_health:
                    summary += f", health={last_health}"
                on_progress(summary)

            remaining = deadline - self._clock()
            if remaining <= 0:
                detail = f"last status={last_status or 'UNKNOWN'}"
                if require_healthy:
                    detail += f", health={last_health or 'UNKNOWN'}"
                raise DeploymentWaitTimeout(
                    f"timed out after {timeout:g}s waiting for {target_status} ({detail})"
                )
            self._sleeper(min(poll_interval, remaining))

    def set_schedule(
        self,
        reference: str,
        *,
        timezone: str,
        start: str,
        end: str,
        desired_replicas: int,
        max_replicas: int,
    ) -> dict[str, Any]:
        deployment_id = self.resolve_id(reference)
        rule = CronScalingRule(
            timezone=timezone,
            start=start,
            end=end,
            desiredReplicas=desired_replicas,
        )
        result = self._client.update_deployment_autoscaling(
            deployment_id=deployment_id,
            min_replicas=0,
            max_replicas=max_replicas,
            scale_to_zero=False,
            cron_scaling=[rule],
        )
        return self._changed(deployment_id, "schedule_set", result)

    def clear_schedule(
        self,
        reference: str,
        *,
        min_replicas: int,
        max_replicas: int,
    ) -> dict[str, Any]:
        deployment_id = self.resolve_id(reference)
        result = self._client.update_deployment_autoscaling(
            deployment_id=deployment_id,
            min_replicas=min_replicas,
            max_replicas=max_replicas,
            scale_to_zero=False,
            cron_scaling=[],
        )
        return self._changed(deployment_id, "schedule_cleared", result)

    @staticmethod
    def _deployment_status(detail: Any) -> str | None:
        if not isinstance(detail, dict):
            return None
        status = detail.get("status") or detail.get("deployment_status")
        return str(status).upper() if status is not None else None

    @staticmethod
    def _health_status(health: Any) -> str | None:
        if not isinstance(health, dict):
            return None
        status = health.get("data") or health.get("status")
        return str(status).upper() if status is not None else None

    @staticmethod
    def _result_rows(data: Any) -> list[dict[str, Any]]:
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
        if isinstance(data, dict) and isinstance(data.get("results"), list):
            return [row for row in data["results"] if isinstance(row, dict)]
        return []

    @classmethod
    def _org_id(cls, detail: Any) -> str | None:
        return cls._detail_value(detail, "org_id", "org")

    @classmethod
    def _namespace(cls, detail: Any) -> str | None:
        return cls._detail_value(
            detail,
            "namespace",
            "deployment_namespace",
            "kubernetes_namespace",
        )

    @staticmethod
    def _detail_value(detail: Any, *keys: str) -> str | None:
        if not isinstance(detail, dict):
            return None
        for key in keys:
            value = detail.get(key)
            if isinstance(value, str) and value:
                return value
            if isinstance(value, dict):
                nested = value.get("uuid") or value.get("id") or value.get("name")
                if nested:
                    return str(nested)
        return None

    @staticmethod
    def _unchanged(deployment_id: str, status: str) -> dict[str, Any]:
        return {
            "deployment_id": deployment_id,
            "status": status,
            "changed": False,
        }

    @staticmethod
    def _changed(deployment_id: str, action: str, result: Any) -> dict[str, Any]:
        return {
            "deployment_id": deployment_id,
            "action": action,
            "changed": True,
            "result": result,
        }
