from __future__ import annotations

from typing import Any

import pytest

from simplismart_deployment_manager.manager import DeploymentManager
from simplismart_deployment_manager.settings import Settings


class FakeClient:
    def __init__(self, detail: dict[str, Any] | None = None) -> None:
        self.detail = detail or {"status": "PENDING"}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_model_deployment(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get", kwargs))
        return self.detail

    def start_deployment(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(("start", kwargs))
        return {"accepted": "start"}

    def stop_deployment(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(("stop", kwargs))
        return {"accepted": "stop"}

    def restart_deployment(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(("restart", kwargs))
        return {"accepted": "restart"}

    def update_deployment_autoscaling(self, **kwargs: Any) -> dict[str, bool]:
        self.calls.append(("autoscaling", kwargs))
        return {"accepted": True}


def settings(org_id: str | None = "org-1") -> Settings:
    return Settings(pg_token="test-token", org_id=org_id)


def test_start_is_idempotent_when_already_deployed() -> None:
    client = FakeClient({"status": "DEPLOYED"})
    manager = DeploymentManager(settings(), client=client)

    result = manager.start("deployment-1")

    assert result == {
        "deployment_id": "deployment-1",
        "status": "DEPLOYED",
        "changed": False,
    }
    assert client.calls == [("get", {"deployment_id": "deployment-1"})]


def test_start_is_idempotent_while_start_is_pending() -> None:
    client = FakeClient({"status": "PENDING"})
    manager = DeploymentManager(settings(), client=client)

    result = manager.start("deployment-1")

    assert result["status"] == "PENDING"
    assert result["changed"] is False
    assert client.calls == [("get", {"deployment_id": "deployment-1"})]


def test_stop_is_idempotent_when_already_stopped() -> None:
    client = FakeClient({"deployment_status": "stopped"})
    manager = DeploymentManager(settings(), client=client)

    result = manager.stop("deployment-1")

    assert result["changed"] is False
    assert client.calls == [("get", {"deployment_id": "deployment-1"})]


def test_schedule_uses_native_cron_scaling_and_zero_minimum() -> None:
    client = FakeClient()
    manager = DeploymentManager(settings(), client=client)

    manager.set_schedule(
        "deployment-1",
        timezone="Asia/Kolkata",
        start="0 9 * * 1-5",
        end="0 18 * * 1-5",
        desired_replicas=2,
        max_replicas=3,
    )

    name, payload = client.calls[-1]
    assert name == "autoscaling"
    assert payload["min_replicas"] == 0
    assert payload["max_replicas"] == 3
    assert payload["scale_to_zero"] is False
    assert payload["cron_scaling"][0].model_dump() == {
        "timezone": "Asia/Kolkata",
        "start": "0 9 * * 1-5",
        "end": "0 18 * * 1-5",
        "desiredReplicas": 2,
    }


def test_clear_schedule_sends_explicit_empty_cron_list() -> None:
    client = FakeClient()
    manager = DeploymentManager(settings(), client=client)

    manager.clear_schedule("deployment-1", min_replicas=1, max_replicas=2)

    _, payload = client.calls[-1]
    assert payload["cron_scaling"] == []
    assert payload["min_replicas"] == 1
    assert payload["max_replicas"] == 2


def test_restart_requires_an_organization() -> None:
    client = FakeClient()
    manager = DeploymentManager(settings(org_id=None), client=client)

    with pytest.raises(ValueError, match="ORG_ID"):
        manager.restart("deployment-1", namespace="model-serving")
