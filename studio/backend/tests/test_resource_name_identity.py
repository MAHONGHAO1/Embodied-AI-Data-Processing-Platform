from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy.exc import IntegrityError

from data.services.resource_names import allocate_legacy_names, is_constraint_conflict


def _create_workspace(client, headers, name: str) -> dict[str, object]:
    response = client.post(
        "/api/v1/workspace/create",
        headers=headers,
        json={"workspace_name": name, "desc": "identity test"},
    )
    assert response.status_code == 200
    return response.json()["data"]


def _assert_conflict(response, *, code: str, message: str) -> None:
    assert response.status_code == 409
    assert response.json()["detail"] == {"code": code, "message": message}


def test_historical_duplicate_names_use_reserved_suffixes_and_length_limit():
    names = allocate_legacy_names(
        [(1, "box"), (2, "Box"), (3, "box_2"), (4, " box ")],
        maximum=128,
    )

    assert names == {1: "box", 2: "box_3", 3: "box_2", 4: "box_4"}

    long_name = "x" * 128
    truncated = allocate_legacy_names([(10, long_name), (11, long_name)], maximum=128)
    assert truncated[10] == long_name
    assert truncated[11] == f"{'x' * 126}_2"
    assert len(truncated[11]) == 128


def test_constraint_conflicts_do_not_mask_unrelated_integrity_errors():
    expected = IntegrityError(
        "insert",
        {},
        SimpleNamespace(diag=SimpleNamespace(constraint_name="uq_expected")),
    )
    unrelated = IntegrityError(
        "insert",
        {},
        SimpleNamespace(diag=SimpleNamespace(constraint_name="uq_unrelated")),
    )

    assert is_constraint_conflict(expected, "uq_expected") is True
    assert is_constraint_conflict(unrelated, "uq_expected") is False


def test_workspace_name_is_globally_unique_after_trim_and_casefold(client, admin_headers):
    suffix = uuid4().hex[:10]
    _create_workspace(client, admin_headers, f"Identity {suffix}")

    duplicate = client.post(
        "/api/v1/workspace/create",
        headers=admin_headers,
        json={"workspace_name": f"  identity {suffix}  ", "desc": "duplicate"},
    )

    _assert_conflict(
        duplicate,
        code="workspace_name_exists",
        message="数采工作空间名称不可重复",
    )


def test_workspace_scoped_names_and_device_serial_use_structured_conflicts(client, admin_headers):
    suffix = uuid4().hex[:10]
    workspace = _create_workspace(client, admin_headers, f"Scoped identity {suffix}")
    workspace_id = int(workspace["id"])

    first_task_set = client.post(
        "/api/v1/workspace/task-set/create",
        headers=admin_headers,
        json={"workspace_id": workspace_id, "name": f"Task {suffix}"},
    )
    assert first_task_set.status_code == 200
    _assert_conflict(
        client.post(
            "/api/v1/workspace/task-set/create",
            headers=admin_headers,
            json={"workspace_id": workspace_id, "name": f"  task {suffix}  "},
        ),
        code="task_set_name_exists",
        message="相同数采工作空间下任务集名称不可重复",
    )

    first_dataset = client.post(
        "/api/v1/datasets",
        headers=admin_headers,
        json={"workspace_id": workspace_id, "name": f"Dataset {suffix}", "description": ""},
    )
    assert first_dataset.status_code == 200
    _assert_conflict(
        client.post(
            "/api/v1/datasets",
            headers=admin_headers,
            json={"workspace_id": workspace_id, "name": f" dataset {suffix} ", "description": ""},
        ),
        code="dataset_name_exists",
        message="相同数采工作空间下数据集名称不可重复",
    )

    first_collector = client.post(
        "/api/v1/collector-profiles",
        headers=admin_headers,
        json={"workspace_id": workspace_id, "name": f"Collector {suffix}"},
    )
    assert first_collector.status_code == 200
    same_name = client.post(
        "/api/v1/collector-profiles",
        headers=admin_headers,
        json={"workspace_id": workspace_id, "name": f" collector {suffix} "},
    )
    assert same_name.status_code == 200
    assert same_name.json()["data"]["profile_key"] != first_collector.json()["data"]["profile_key"]

    first_device = client.post(
        "/api/v1/collection-devices",
        headers=admin_headers,
        json={
            "workspace_id": workspace_id,
            "name": "Phone A",
            "device_type": "phone",
            "serial_number": f"sn-{suffix}",
        },
    )
    assert first_device.status_code == 200
    _assert_conflict(
        client.post(
            "/api/v1/collection-devices",
            headers=admin_headers,
            json={
                "workspace_id": workspace_id,
                "name": "Phone B",
                "device_type": "phone",
                "serial_number": f" SN-{suffix} ",
            },
        ),
        code="device_serial_exists",
        message="设备SN号重复请重新输入",
    )


def test_task_set_name_can_be_reused_in_another_workspace(client, admin_headers):
    suffix = uuid4().hex[:10]
    first = _create_workspace(client, admin_headers, f"First scope {suffix}")
    second = _create_workspace(client, admin_headers, f"Second scope {suffix}")

    for workspace in (first, second):
        response = client.post(
            "/api/v1/workspace/task-set/create",
            headers=admin_headers,
            json={"workspace_id": workspace["id"], "name": f"Shared {suffix}"},
        )
        assert response.status_code == 200
