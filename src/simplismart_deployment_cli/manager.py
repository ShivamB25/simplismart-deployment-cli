from __future__ import annotations

import re
from collections.abc import Callable
from time import monotonic, sleep
from typing import Any
from uuid import UUID

from simplismart import CronScalingRule, DeploymentUpdate, Simplismart, SimplismartError

from .settings import Settings

_ENVIRONMENT_VARIABLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class DeploymentNotFoundError(ValueError):
    """No deployment matched an exact name."""


class AmbiguousDeploymentError(ValueError):
    """More than one deployment matched an exact name."""


class DeploymentWaitError(RuntimeError):
    """A deployment did not reach the requested state."""


class DeploymentWaitTimeout(DeploymentWaitError):
    """A deployment wait exceeded its deadline."""


class DeploymentUpdateError(SimplismartError):
    """A deployment update failed without exposing environment contents."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message, status_code=status_code)


def _deployment_update_error(message: str, error: Exception) -> DeploymentUpdateError:
    status_code = getattr(error, "status_code", None)
    if type(status_code) is not int:
        status_code = None
    return DeploymentUpdateError(message, status_code=status_code)


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
        active_matches = {
            deployment_id: row
            for deployment_id, row in self._deployments_named(
                manifest.deployment_name
            ).items()
            if self._deployment_status(row) != "DELETED"
        }
        if len(active_matches) > 1:
            ids = ", ".join(sorted(active_matches))
            raise AmbiguousDeploymentError(
                "more than one non-deleted deployment is named "
                f"{manifest.deployment_name!r}: {ids}"
            )

        deployment_id = next(iter(active_matches), None)
        deployment = (
            self._client.get_model_deployment(deployment_id=deployment_id)
            if deployment_id
            else None
        )
        if deployment_id and deployment is None:
            raise ValueError(
                "existing deployment details are unavailable; refusing to create a replacement"
            )
        if deployment is not None and self._deployment_status(deployment) == "DELETED":
            deployment = None
        existing_model_repo_id = None
        if deployment is not None:
            self._lifecycle_status(deployment, action="resume")
            existing_model_repo_id = self._detail_value(
                deployment, "model_repo", "model_repo_id"
            )
            if not existing_model_repo_id:
                raise ValueError(
                    "existing deployment does not report its model repository; "
                    "refusing to resume an unverified configuration"
                )
            desired_fields = manifest.deployment.model_dump(
                exclude_unset=True, exclude={"model_repo_name", "name"}
            )
            drift = [
                key
                for key, value in desired_fields.items()
                if (key in deployment or key == "deployment_custom_configuration")
                and deployment.get(key) != value
            ]
            if drift:
                raise ValueError(
                    "existing deployment differs from the manifest in: "
                    f"{', '.join(sorted(drift))}; inspect it before updating or replacing it"
                )

        model_repo, model_repo_created = self._manifest_model_repo(
            manifest.model_repo, create=deployment is None
        )
        model_repo_id = self._detail_value(model_repo, "uuid", "id")
        if not model_repo_id:
            raise RuntimeError("Simplismart did not return a model repository ID")
        if existing_model_repo_id and existing_model_repo_id != model_repo_id:
            raise ValueError("existing deployment uses a different model repository")

        deployment_created = deployment is None
        if deployment_created:
            desired_deployment = manifest.deployment_payload(
                org_id=org_id, model_repo_id=model_repo_id
            )
            deployment = self._client.create_deployment(desired_deployment)
            deployment_id = self._detail_value(
                deployment, "deployment_id", "uuid", "id"
            )
            if not deployment_id:
                raise RuntimeError("Simplismart did not return a deployment ID")
        else:
            deployment = self.start(deployment_id, org_id=org_id)

        return {
            "manifest": str(manifest.path),
            "model_repo": model_repo,
            "deployment": deployment,
            "deployment_id": deployment_id,
            "model_repo_created": model_repo_created,
            "deployment_created": deployment_created,
        }

    def _manifest_model_repo(
        self, desired_model_repo: Any, *, create: bool = True
    ) -> tuple[Any, bool]:
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
            if not create:
                raise ValueError(
                    "the manifest model repository was not found; "
                    "refusing to create one for an existing deployment"
                )
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

        active_matches = {
            deployment_id: row
            for deployment_id, row in matches.items()
            if self._deployment_status(row) != "DELETED"
        }
        if not active_matches:
            suffix = " (only deleted deployments matched)" if matches else ""
            raise DeploymentNotFoundError(
                f"no non-deleted deployment named {reference!r}{suffix}; "
                "use `list` to see exact names"
            )
        if len(active_matches) > 1:
            ids = ", ".join(sorted(active_matches))
            raise AmbiguousDeploymentError(
                f"deployment name {reference!r} is ambiguous; "
                f"matching non-deleted IDs: {ids}"
            )
        return next(iter(active_matches))

    def get(self, reference: str) -> Any:
        deployment_id = self.resolve_id(reference)
        return self._client.get_model_deployment(deployment_id=deployment_id)

    def update_env(
        self,
        reference: str,
        *,
        values: dict[str, str],
        unset: list[str],
    ) -> dict[str, Any]:
        """Update only the requested deployment environment variables."""
        if not isinstance(reference, str) or not reference:
            raise ValueError("deployment reference must be a non-empty name or UUID")
        if not isinstance(values, dict):
            raise TypeError("environment values must be a dictionary")
        if not isinstance(unset, list):
            raise TypeError("environment removals must be a list")
        if any(
            not isinstance(name, str)
            or _ENVIRONMENT_VARIABLE_NAME.fullmatch(name) is None
            for name in (*values, *unset)
        ):
            raise ValueError(
                "environment variable names must be valid shell identifiers"
            )
        if any(not isinstance(value, str) for value in values.values()):
            raise ValueError("environment variable values must be strings")
        if len(set(unset)) != len(unset):
            raise ValueError("environment variable names must not be repeated")
        if set(values).intersection(unset):
            raise ValueError("an environment variable cannot be updated and unset")
        if not values and not unset:
            raise ValueError("at least one environment update or removal is required")

        try:
            deployment_id = self.resolve_id(reference)
        except DeploymentNotFoundError:
            raise DeploymentNotFoundError(
                "no deployment was found for the environment update"
            ) from None
        except AmbiguousDeploymentError:
            raise AmbiguousDeploymentError(
                "deployment name is ambiguous for the environment update"
            ) from None
        except Exception as exc:  # noqa: BLE001 - SDK exceptions may echo credentials.
            raise _deployment_update_error(
                "could not resolve deployment for the environment update", exc
            ) from None

        try:
            detail = self._client.get_model_deployment(deployment_id=deployment_id)
        except Exception as exc:  # noqa: BLE001 - SDK exceptions may echo credentials.
            raise _deployment_update_error(
                "could not read deployment environment variables", exc
            ) from None

        existing_env = detail.get("env_variables") if isinstance(detail, dict) else None
        if not isinstance(existing_env, dict):
            raise DeploymentUpdateError(
                "deployment environment variables are unavailable; refusing to replace them"
            )

        status = self._deployment_status(detail)
        merged_env = existing_env.copy()
        updated_keys = sorted(
            key
            for key, value in values.items()
            if key not in existing_env or existing_env[key] != value
        )
        removed_keys = sorted(key for key in unset if key in existing_env)
        for key in updated_keys:
            merged_env[key] = values[key]
        for key in removed_keys:
            del merged_env[key]

        summary = {
            "deployment_id": deployment_id,
            "action": "update-env",
            "changed": bool(updated_keys or removed_keys),
            "updated_keys": updated_keys,
            "removed_keys": removed_keys,
            "verified": True,
        }
        if not updated_keys and not removed_keys:
            return summary

        if status != "DEPLOYED":
            raise ValueError(
                "environment updates require a DEPLOYED deployment; "
                "wait for the current rollout to finish"
            )

        try:
            payload = DeploymentUpdate(env_variables=merged_env)
            self._client.update_deployment(
                deployment_id=deployment_id,
                payload=payload,
            )
        except Exception as exc:  # noqa: BLE001 - SDK exceptions may echo credentials.
            raise _deployment_update_error(
                "deployment environment update failed", exc
            ) from None

        try:
            verified_detail = self._client.get_model_deployment(
                deployment_id=deployment_id
            )
        except Exception as exc:  # noqa: BLE001 - SDK exceptions may echo credentials.
            raise _deployment_update_error(
                "could not verify deployment environment update", exc
            ) from None

        verified_env = (
            verified_detail.get("env_variables")
            if isinstance(verified_detail, dict)
            else None
        )
        if not isinstance(verified_env, dict) or verified_env != merged_env:
            raise DeploymentUpdateError(
                "deployment environment update could not be verified"
            )

        return summary

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
            "health": self._client.fetch_deployment_health(deployment_id=deployment_id),
        }

    def start(self, reference: str, *, org_id: str | None = None) -> dict[str, Any]:
        deployment_id = self.resolve_id(reference)
        detail = self._client.get_model_deployment(deployment_id=deployment_id)
        status = self._lifecycle_status(detail, action="start")
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
        status = self._lifecycle_status(detail, action="stop")
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
            raise ValueError(
                "delete needs an organization; set ORG_ID or pass --org-id"
            )
        detail = self._client.get_model_deployment(deployment_id=deployment_id)
        status = self._lifecycle_status(detail, action="delete", allow_deleted=True)
        if status == "DELETED":
            return self._unchanged(deployment_id, status)
        deployment_name = self._detail_value(detail, "deployment_name", "name")
        reported_id = self._detail_value(detail, "deployment_id", "uuid", "id")
        if not deployment_name or (reported_id and reported_id != deployment_id):
            raise ValueError("could not verify deployment identity before deletion")
        health = self._client.fetch_deployment_health(deployment_id=deployment_id)
        health_status = self._health_status(health)
        if not health_status:
            raise ValueError("deployment health is unavailable; refusing deletion")
        inspected_state = {
            "deployment_id": deployment_id,
            "deployment_name": deployment_name,
            "status": status,
            "health": health_status,
        }

        result = self._client.delete_deployment(
            deployment_id=deployment_id,
            org_id=resolved_org_id,
        )
        return {
            **self._changed(deployment_id, "delete", result),
            "before_delete": inspected_state,
        }

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
        detail = self._client.get_model_deployment(deployment_id=deployment_id)
        self._lifecycle_status(detail, action="restart")

        if not resolved_org_id or not resolved_namespace:
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
            detail = self._client.get_model_deployment(deployment_id=deployment_id)
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

    @classmethod
    def _lifecycle_status(
        cls, detail: Any, *, action: str, allow_deleted: bool = False
    ) -> str:
        status = cls._deployment_status(detail)
        # SDK 0.1.15 documents these statuses, not per-action permissions.
        if status not in {
            "PROGRESSING",
            "PENDING",
            "DEPLOYING",
            "DEPLOYED",
            "DEPLOYED_WARMPOOL",
            "FAILED_DELETING",
            "FAILED_WARMPOOL",
            "FAILED_DELETING_WARMPOOL",
            "FAILED",
            "DELETING",
            "DELETED",
            "STOPPED",
            "EDIT_IN_PROGRESS",
            "EDIT_WARMPOOL_IN_PROGRESS",
            "EDIT_ROLLBACK_IN_PROGRESS",
            "EDIT_ROLLBACK_FAILED",
            "EDIT_SUCCESS",
        }:
            raise ValueError(
                f"cannot {action} deployment with missing or unrecognized status; "
                "inspect its state before retrying"
            )
        if status == "DELETED" and not allow_deleted:
            raise ValueError(f"cannot {action} a DELETED deployment")
        return status

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
        rows = data.get("results") if isinstance(data, dict) else data
        if not isinstance(rows, list) or any(
            not isinstance(row, dict) for row in rows
        ):
            raise ValueError("resource listing is unavailable or malformed")
        return rows

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
