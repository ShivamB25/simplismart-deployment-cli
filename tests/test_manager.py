from __future__ import annotations

from typing import Any

import pytest
from simplismart import SimplismartError

from simplismart_deployment_cli.manager import (
    AmbiguousDeploymentError,
    DeploymentManager,
    DeploymentUpdateError,
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
        offset = kwargs["offset"]
        count = min(kwargs["count"], 20)
        return self.deployments[offset : offset + count]

    def start_deployment(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(("start", kwargs))
        return {"accepted": "start"}

    def stop_deployment(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(("stop", kwargs))
        return {"accepted": "stop"}

    def restart_deployment(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(("restart", kwargs))
        return {"accepted": "restart"}

    def delete_deployment(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(("delete", kwargs))
        return {"accepted": "delete"}

    def update_deployment(self, **kwargs: Any) -> dict[str, bool]:
        self.calls.append(("update", kwargs))
        self.detail["env_variables"] = kwargs["payload"].env_variables
        return {"accepted": True}

    def update_deployment_autoscaling(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(("autoscaling", kwargs))
        return {"accepted": "autoscaling"}


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


def test_list_returns_every_sdk_page_by_default() -> None:
    deployments = [{"deployment_id": f"deployment-{index}"} for index in range(42)]
    client = FakeClient(deployments=deployments)
    manager = DeploymentManager(settings(), client=client)

    assert manager.list() == deployments
    list_calls = [kwargs for action, kwargs in client.calls if action == "list"]
    assert [call["offset"] for call in list_calls] == [0, 20, 40]
    assert [call["count"] for call in list_calls] == [20, 20, 20]


def test_list_count_spans_the_sdk_page_cap() -> None:
    deployments = [{"deployment_id": f"deployment-{index}"} for index in range(30)]
    client = FakeClient(deployments=deployments)
    manager = DeploymentManager(settings(), client=client)

    assert manager.list(count=25) == deployments[:25]
    list_calls = [kwargs for action, kwargs in client.calls if action == "list"]
    assert [call["offset"] for call in list_calls] == [0, 20]
    assert [call["count"] for call in list_calls] == [20, 5]


def test_start_is_idempotent_when_already_deployed() -> None:
    client = FakeClient({"status": "DEPLOYED"})
    manager = DeploymentManager(settings(), client=client)

    result = manager.start("fc9318f7-1cfb-4e40-b14c-93c09b47205c")

    assert result == {
        "deployment_id": "fc9318f7-1cfb-4e40-b14c-93c09b47205c",
        "status": "DEPLOYED",
        "changed": False,
    }
    assert client.calls == [
        ("get", {"deployment_id": "fc9318f7-1cfb-4e40-b14c-93c09b47205c"})
    ]


def test_start_is_idempotent_while_start_is_pending() -> None:
    client = FakeClient({"status": "PENDING"})
    manager = DeploymentManager(settings(), client=client)

    result = manager.start("fc9318f7-1cfb-4e40-b14c-93c09b47205c")

    assert result["status"] == "PENDING"
    assert result["changed"] is False
    assert client.calls == [
        ("get", {"deployment_id": "fc9318f7-1cfb-4e40-b14c-93c09b47205c"})
    ]


def test_stop_is_idempotent_when_already_stopped() -> None:
    client = FakeClient({"deployment_status": "stopped"})
    manager = DeploymentManager(settings(), client=client)

    result = manager.stop("fc9318f7-1cfb-4e40-b14c-93c09b47205c")

    assert result["changed"] is False
    assert client.calls == [
        ("get", {"deployment_id": "fc9318f7-1cfb-4e40-b14c-93c09b47205c"})
    ]


def test_delete_uses_current_org_scoped_sdk_endpoint() -> None:
    deployment_id = "fc9318f7-1cfb-4e40-b14c-93c09b47205c"
    client = FakeClient({"status": "DEPLOYED"})
    manager = DeploymentManager(settings(), client=client)

    result = manager.delete(deployment_id)

    assert result == {
        "deployment_id": deployment_id,
        "action": "delete",
        "changed": True,
        "result": {"accepted": "delete"},
    }
    assert client.calls == [
        ("delete", {"deployment_id": deployment_id, "org_id": "org-1"}),
    ]


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

    manager.clear_schedule(
        "fc9318f7-1cfb-4e40-b14c-93c09b47205c", min_replicas=1, max_replicas=2
    )

    _, payload = client.calls[-1]
    assert payload["cron_scaling"] == []
    assert payload["min_replicas"] == 1
    assert payload["max_replicas"] == 2


def test_restart_requires_an_organization() -> None:
    client = FakeClient()
    manager = DeploymentManager(settings(org_id=None), client=client)

    with pytest.raises(ValueError, match="ORG_ID"):
        manager.restart(
            "fc9318f7-1cfb-4e40-b14c-93c09b47205c", namespace="model-serving"
        )


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


def test_name_resolution_ignores_deleted_duplicates() -> None:
    active_id = "fc9318f7-1cfb-4e40-b14c-93c09b47205c"
    client = FakeClient(
        deployments=[
            {
                "deployment_id": "8ac7142f-8378-49b8-b555-fa29bd7db92b",
                "deployment_name": "shared-name",
                "status": "DELETED",
            },
            {
                "deployment_id": active_id,
                "deployment_name": "shared-name",
                "status": "STOPPED",
            },
            {
                "deployment_id": "d5b09822-1750-4673-80bf-7aef6e872191",
                "deployment_name": "shared-name",
                "status": "DELETED",
            },
        ]
    )
    manager = DeploymentManager(settings(), client=client)

    assert manager.resolve_id("shared-name") == active_id


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


def test_update_env_preserves_existing_variables_and_verifies() -> None:
    deployment_id = "fc9318f7-1cfb-4e40-b14c-93c09b47205c"
    client = FakeClient(
        detail={
            "status": "DEPLOYED",
            "env_variables": {
                "HF_TOKEN": "old-token",
                "KEEP_TOKEN": "other-token",
                "REPLICA_COUNT": 2,
            },
        }
    )
    manager = DeploymentManager(settings(), client=client)

    result = manager.update_env(
        deployment_id,
        values={"HF_TOKEN": "new-token", "NEW_TOKEN": "added-token"},
        unset=[],
    )

    assert result == {
        "deployment_id": deployment_id,
        "action": "update-env",
        "changed": True,
        "updated_keys": ["HF_TOKEN", "NEW_TOKEN"],
        "removed_keys": [],
        "verified": True,
    }
    update = next(kwargs for action, kwargs in client.calls if action == "update")
    assert update["deployment_id"] == deployment_id
    assert update["payload"].env_variables == {
        "HF_TOKEN": "new-token",
        "KEEP_TOKEN": "other-token",
        "REPLICA_COUNT": 2,
        "NEW_TOKEN": "added-token",
    }
    assert [action for action, _ in client.calls] == ["get", "update", "get"]
    assert "new-token" not in repr(result)


def test_update_env_removes_only_requested_existing_variables() -> None:
    deployment_id = "fc9318f7-1cfb-4e40-b14c-93c09b47205c"
    client = FakeClient(
        detail={
            "status": "DEPLOYED",
            "env_variables": {"KEEP": "preserved", "REMOVE": "removed"},
        }
    )
    manager = DeploymentManager(settings(), client=client)

    result = manager.update_env(
        deployment_id,
        values={},
        unset=["REMOVE", "ABSENT"],
    )

    assert result == {
        "deployment_id": deployment_id,
        "action": "update-env",
        "changed": True,
        "updated_keys": [],
        "removed_keys": ["REMOVE"],
        "verified": True,
    }
    update = next(kwargs for action, kwargs in client.calls if action == "update")
    assert update["payload"].env_variables == {"KEEP": "preserved"}


def test_update_env_noop_skips_mutation_during_an_in_flight_edit() -> None:
    deployment_id = "fc9318f7-1cfb-4e40-b14c-93c09b47205c"
    client = FakeClient(
        detail={
            "status": "EDIT_IN_PROGRESS",
            "env_variables": {"HF_TOKEN": "same-token"},
        }
    )
    manager = DeploymentManager(settings(), client=client)

    result = manager.update_env(
        deployment_id,
        values={"HF_TOKEN": "same-token"},
        unset=["ABSENT"],
    )

    assert result == {
        "deployment_id": deployment_id,
        "action": "update-env",
        "changed": False,
        "updated_keys": [],
        "removed_keys": [],
        "verified": True,
    }
    assert client.calls == [("get", {"deployment_id": deployment_id})]


@pytest.mark.parametrize(
    "detail",
    [
        {"status": "DEPLOYED"},
        {"status": "DEPLOYED", "env_variables": None},
        {"status": "DEPLOYED", "env_variables": []},
    ],
)
def test_update_env_refuses_unavailable_or_malformed_environment(
    detail: dict[str, Any],
) -> None:
    client = FakeClient(detail=detail)
    manager = DeploymentManager(settings(), client=client)

    with pytest.raises(DeploymentUpdateError):
        manager.update_env(
            "fc9318f7-1cfb-4e40-b14c-93c09b47205c",
            values={"TOKEN": "secret"},
            unset=[],
        )

    assert [action for action, _ in client.calls] == ["get"]


@pytest.mark.parametrize(
    ("values", "unset", "error_type"),
    [
        ({}, [], ValueError),
        ({"INVALID-NAME": "secret"}, [], ValueError),
        ({"TOKEN": 1}, [], ValueError),
        ({"TOKEN": "secret"}, ["TOKEN"], ValueError),
        ({}, ["TOKEN", "TOKEN"], ValueError),
        ([], [], TypeError),
        ({}, (), TypeError),
    ],
)
def test_update_env_validates_programmatic_operations_before_api_calls(
    values: Any, unset: Any, error_type: type[Exception]
) -> None:
    client = FakeClient()
    manager = DeploymentManager(settings(), client=client)

    with pytest.raises(error_type):
        manager.update_env(
            "fc9318f7-1cfb-4e40-b14c-93c09b47205c",
            values=values,
            unset=unset,
        )

    assert client.calls == []


@pytest.mark.parametrize(
    "status",
    [
        "DELETED",
        "DELETING",
        "DELETE_IN_PROGRESS",
        "FAILED",
        "EDIT_IN_PROGRESS",
        "PENDING",
        "DEPLOYING",
        "PROGRESSING",
        "STOPPED",
        "UNKNOWN",
        None,
    ],
)
def test_update_env_rejects_non_deployed_statuses(status: str | None) -> None:
    client = FakeClient(detail={"status": status, "env_variables": {"TOKEN": "old"}})
    manager = DeploymentManager(settings(), client=client)

    with pytest.raises(ValueError):
        manager.update_env(
            "fc9318f7-1cfb-4e40-b14c-93c09b47205c",
            values={"TOKEN": "new"},
            unset=[],
        )

    assert [action for action, _ in client.calls] == ["get"]


def test_update_env_fails_when_readback_does_not_match() -> None:
    class StaleReadbackClient(FakeClient):
        def update_deployment(self, **kwargs: Any) -> dict[str, bool]:
            self.calls.append(("update", kwargs))
            return {"accepted": True}

    deployment_id = "fc9318f7-1cfb-4e40-b14c-93c09b47205c"
    client = StaleReadbackClient(
        detail={"status": "DEPLOYED", "env_variables": {"TOKEN": "old"}}
    )
    manager = DeploymentManager(settings(), client=client)

    with pytest.raises(DeploymentUpdateError, match="could not be verified"):
        manager.update_env(
            deployment_id,
            values={"TOKEN": "new-secret"},
            unset=[],
        )

    assert [action for action, _ in client.calls] == ["get", "update", "get"]


def test_update_env_api_error_does_not_echo_environment_values() -> None:
    secret = "token-that-must-not-leak"

    class EchoingErrorClient(FakeClient):
        def update_deployment(self, **kwargs: Any) -> dict[str, bool]:
            self.calls.append(("update", kwargs))
            raise SimplismartError(
                f"provider echoed {secret}",
                status_code=403,
                payload={"env_variables": {"TOKEN": secret}},
            )

    client = EchoingErrorClient(
        detail={"status": "DEPLOYED", "env_variables": {"TOKEN": "old"}}
    )
    manager = DeploymentManager(settings(), client=client)

    with pytest.raises(DeploymentUpdateError) as caught:
        manager.update_env(
            "fc9318f7-1cfb-4e40-b14c-93c09b47205c",
            values={"TOKEN": secret},
            unset=[],
        )

    assert caught.value.status_code == 403
    assert caught.value.payload == {}
    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)


def test_update_env_resolves_exact_deployment_name() -> None:
    deployment_id = "fc9318f7-1cfb-4e40-b14c-93c09b47205c"
    client = FakeClient(
        detail={"status": "DEPLOYED", "env_variables": {}},
        deployments=[
            {
                "deployment_id": deployment_id,
                "deployment_name": "nightly-inference",
            }
        ],
    )
    manager = DeploymentManager(settings(), client=client)

    result = manager.update_env(
        "nightly-inference",
        values={"TOKEN": "new"},
        unset=[],
    )

    assert result["deployment_id"] == deployment_id
    update = next(kwargs for action, kwargs in client.calls if action == "update")
    assert update["deployment_id"] == deployment_id


def test_update_env_unexpected_sdk_error_cannot_expose_credentials() -> None:
    secret = "unparsed-provider-credential"

    class InvalidResponseClient(FakeClient):
        def get_model_deployment(self, **kwargs: Any) -> dict[str, Any]:
            raise ValueError(f"invalid provider response containing {secret}")

    manager = DeploymentManager(settings(), client=InvalidResponseClient())

    with pytest.raises(DeploymentUpdateError) as caught:
        manager.update_env(
            "fc9318f7-1cfb-4e40-b14c-93c09b47205c",
            values={"HF_TOKEN": "replacement"},
            unset=[],
        )

    assert caught.value.status_code is None
    assert caught.value.payload == {}
    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)
