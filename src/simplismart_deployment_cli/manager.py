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

    def list(
        self,
        *,
        model_repo_id: str | None = None,
        status: str | None = None,
        offset: int = 0,
        count: int = 20,
    ) -> Any:
        return self._client.list_deployments(
            model_repo_id=model_repo_id,
            status=status,
            offset=offset,
            count=count,
        )

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
