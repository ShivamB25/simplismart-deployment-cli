from __future__ import annotations

from typing import Any

import pytest

from simplismart_deployment_cli.manager import (
    AmbiguousDeploymentError,
    DeploymentManager,
    DeploymentWaitTimeout,
)
from simplismart_deployment_cli.settings import Settings


class FakeClient:
    def __init__(
        self,
        detail: dict[str, Any] | None = None,
        deployments: list[dict[str, Any]] | None = None,
    ) -> None:
        self.detail = detail or {"status": "PENDING"}
        self.deployments = deployments or []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_model_deployment(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get", kwargs))
        return self.detail

    def list_deployments(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("list", kwargs))
        return self.deployments

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


class SequenceClient(FakeClient):
    def __init__(
        self,
        details: list[dict[str, Any]],
        health: list[dict[str, Any]],
    ) -> None:
        super().__init__()
        self._details = details
        self._health = health

    def get_model_deployment(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get", kwargs))
        if len(self._details) > 1:
            return self._details.pop(0)
        return self._details[0]

    def fetch_deployment_health(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("health", kwargs))
        if len(self._health) > 1:
            return self._health.pop(0)
        return self._health[0]


class FakeTime:
    def __init__(self) -> None:
        self.value = 0.0

    def now(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


def settings(org_id: str | None = "org-1") -> Settings:
    return Settings(
        pg_token="test-token",
        org_id=org_id,
        deployment_namespace=None,
    )


def test_start_is_idempotent_when_already_deployed() -> None:
    client = FakeClient({"status": "DEPLOYED"})
    manager = DeploymentManager(settings(), client=client)

    result = manager.start("fc9318f7-1cfb-4e40-b14c-93c09b47205c")

    assert result == {
        "deployment_id": "fc9318f7-1cfb-4e40-b14c-93c09b47205c",
        "status": "DEPLOYED",
        "changed": False,
    }
    assert client.calls == [("get", {"deployment_id": "fc9318f7-1cfb-4e40-b14c-93c09b47205c"})]


def test_start_is_idempotent_while_start_is_pending() -> None:
    client = FakeClient({"status": "PENDING"})
    manager = DeploymentManager(settings(), client=client)

    result = manager.start("fc9318f7-1cfb-4e40-b14c-93c09b47205c")

    assert result["status"] == "PENDING"
    assert result["changed"] is False
    assert client.calls == [("get", {"deployment_id": "fc9318f7-1cfb-4e40-b14c-93c09b47205c"})]


def test_stop_is_idempotent_when_already_stopped() -> None:
    client = FakeClient({"deployment_status": "stopped"})
    manager = DeploymentManager(settings(), client=client)

    result = manager.stop("fc9318f7-1cfb-4e40-b14c-93c09b47205c")

    assert result["changed"] is False
    assert client.calls == [("get", {"deployment_id": "fc9318f7-1cfb-4e40-b14c-93c09b47205c"})]


def test_schedule_uses_native_cron_scaling_and_zero_minimum() -> None:
    client = FakeClient()
    manager = DeploymentManager(settings(), client=client)

    manager.set_schedule(
        "fc9318f7-1cfb-4e40-b14c-93c09b47205c",
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

    manager.clear_schedule("fc9318f7-1cfb-4e40-b14c-93c09b47205c", min_replicas=1, max_replicas=2)

    _, payload = client.calls[-1]
    assert payload["cron_scaling"] == []
    assert payload["min_replicas"] == 1
    assert payload["max_replicas"] == 2


def test_restart_requires_an_organization() -> None:
    client = FakeClient()
    manager = DeploymentManager(settings(org_id=None), client=client)

    with pytest.raises(ValueError, match="ORG_ID"):
        manager.restart("fc9318f7-1cfb-4e40-b14c-93c09b47205c", namespace="model-serving")


def test_restart_infers_org_and_namespace_from_deployment() -> None:
    client = FakeClient(
        {
            "status": "DEPLOYED",
            "org": {"uuid": "org-inferred"},
            "namespace": "model-serving",
        }
    )
    manager = DeploymentManager(settings(org_id=None), client=client)

    result = manager.restart("fc9318f7-1cfb-4e40-b14c-93c09b47205c")

    assert result["changed"] is True
    assert client.calls[-1] == (
        "restart",
        {
            "deployment_id": "fc9318f7-1cfb-4e40-b14c-93c09b47205c",
            "org_id": "org-inferred",
            "namespace": "model-serving",
        },
    )


def test_wait_reaches_deployed_and_healthy_without_real_sleep() -> None:
    client = SequenceClient(
        details=[
            {"status": "PENDING"},
            {"status": "DEPLOYED"},
            {"status": "DEPLOYED"},
        ],
        health=[
            {"data": "Progressing"},
            {"data": "Healthy", "pods": {"ready": 1, "not_ready": 0}},
        ],
    )
    fake_time = FakeTime()
    manager = DeploymentManager(
        settings(),
        client=client,
        clock=fake_time.now,
        sleeper=fake_time.sleep,
    )
    progress: list[str] = []

    result = manager.wait_for_status(
        "fc9318f7-1cfb-4e40-b14c-93c09b47205c",
        target_status="DEPLOYED",
        require_healthy=True,
        timeout=10,
        poll_interval=1,
        on_progress=progress.append,
    )

    assert result["deployment"]["status"] == "DEPLOYED"
    assert result["health"]["data"] == "Healthy"
    assert fake_time.value == 2
    assert progress == ["status=PENDING", "status=DEPLOYED, health=PROGRESSING"]


def test_wait_timeout_reports_last_observed_state() -> None:
    client = SequenceClient(
        details=[{"status": "PENDING"}],
        health=[{"data": "Progressing"}],
    )
    fake_time = FakeTime()
    manager = DeploymentManager(
        settings(),
        client=client,
        clock=fake_time.now,
        sleeper=fake_time.sleep,
    )

    with pytest.raises(DeploymentWaitTimeout, match="last status=PENDING"):
        manager.wait_for_status(
            "fc9318f7-1cfb-4e40-b14c-93c09b47205c",
            target_status="DEPLOYED",
            require_healthy=True,
            timeout=2,
            poll_interval=1,
        )


def test_exact_deployment_name_resolves_for_lifecycle_action() -> None:
    deployment_id = "fc9318f7-1cfb-4e40-b14c-93c09b47205c"
    client = FakeClient(
        detail={"status": "STOPPED"},
        deployments=[
            {
                "deployment_id": deployment_id,
                "deployment_name": "nightly-inference",
            }
        ],
    )
    manager = DeploymentManager(settings(), client=client)

    result = manager.start("nightly-inference")

    assert result["deployment_id"] == deployment_id
    assert client.calls[-1] == (
        "start",
        {"deployment_id": deployment_id, "org_id": "org-1"},
    )


def test_duplicate_deployment_name_fails_without_action() -> None:
    client = FakeClient(
        deployments=[
            {
                "deployment_id": "fc9318f7-1cfb-4e40-b14c-93c09b47205c",
                "deployment_name": "shared-name",
            },
            {
                "deployment_id": "d3960cc8-f939-40e7-86d4-84005d080d93",
                "deployment_name": "shared-name",
            },
        ]
    )
    manager = DeploymentManager(settings(), client=client)

    with pytest.raises(AmbiguousDeploymentError, match="ambiguous"):
        manager.stop("shared-name")

    assert all(call[0] == "list" for call in client.calls)
