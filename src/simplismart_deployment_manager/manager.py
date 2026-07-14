from __future__ import annotations

from typing import Any

from simplismart import CronScalingRule, Simplismart

from .settings import Settings


class DeploymentManager:
    """Small, automation-safe facade over the Simplismart deployment SDK."""

    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self._settings = settings
        self._client = client or Simplismart(
            pg_token=settings.pg_token.get_secret_value(),
            base_url=settings.base_url,
            timeout=settings.timeout,
        )

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

    def get(self, deployment_id: str) -> Any:
        return self._client.get_model_deployment(deployment_id=deployment_id)

    def start(self, deployment_id: str, *, org_id: str | None = None) -> dict[str, Any]:
        detail = self.get(deployment_id)
        status = self._deployment_status(detail)
        if status in {"DEPLOYED", "PENDING"}:
            return self._unchanged(deployment_id, status)

        result = self._client.start_deployment(
            deployment_id=deployment_id,
            org_id=org_id or self._settings.org_id,
        )
        return self._changed(deployment_id, "start", result)

    def stop(self, deployment_id: str, *, org_id: str | None = None) -> dict[str, Any]:
        detail = self.get(deployment_id)
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
        deployment_id: str,
        *,
        namespace: str,
        org_id: str | None = None,
    ) -> dict[str, Any]:
        resolved_org_id = org_id or self._settings.org_id
        if not resolved_org_id:
            raise ValueError("ORG_ID or --org-id is required for restart")
        result = self._client.restart_deployment(
            deployment_id=deployment_id,
            org_id=resolved_org_id,
            namespace=namespace,
        )
        return self._changed(deployment_id, "restart", result)

    def health(self, deployment_id: str) -> Any:
        return self._client.fetch_deployment_health(deployment_id=deployment_id)

    def set_schedule(
        self,
        deployment_id: str,
        *,
        timezone: str,
        start: str,
        end: str,
        desired_replicas: int,
        max_replicas: int,
    ) -> dict[str, Any]:
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
        deployment_id: str,
        *,
        min_replicas: int,
        max_replicas: int,
    ) -> dict[str, Any]:
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
