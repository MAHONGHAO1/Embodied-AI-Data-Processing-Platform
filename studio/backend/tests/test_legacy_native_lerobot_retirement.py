"""Retired collection batch and legacy LeRobot creation APIs are gone entirely."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

import pytest

from data.database import Batch, NativeLerobotDataset, TaskSet
from tests.collection_api_fixtures import make_workspace


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        (
            "post",
            "/api/v1/batches",
            {
                "workspace_id": "{workspace_id}",
                "task_set_id": "{task_set_id}",
                "name": "retired native batch",
                "batch_type": "lerobot",
            },
        ),
        (
            "get",
            "/api/v1/batches/lerobot-candidates?workspace_id={workspace_id}&task_set_id={task_set_id}",
            None,
        ),
        (
            "post",
            "/api/v1/batches/lerobot",
            {
                "workspace_id": "{workspace_id}",
                "task_set_id": "{task_set_id}",
                "name": "retired native batch",
                "candidate_token": "retired-candidate",
            },
        ),
        (
            "post",
            "/api/v1/batches/native-lerobot-scan-snapshots",
            {
                "workspace_id": "{workspace_id}",
                "task_set_id": "{task_set_id}",
            },
        ),
        (
            "post",
            "/api/v1/batches/{batch_id}/native-lerobot-sessions",
            None,
        ),
        (
            "post",
            "/api/v1/batches/native-lerobot-sessions/{session_id}/submit",
            {"candidate_ids": [str(uuid4())], "request_id": str(uuid4())},
        ),
    ],
)
def test_legacy_native_lerobot_batch_entry_points_are_gone(
    client,
    admin_headers,
    db_session,
    method,
    path,
    payload,
):
    """The collection batch router is deleted; stale clients cannot resurrect it."""
    workspace = make_workspace(db_session)
    task_set = TaskSet(workspace_id=workspace.id, name=f"retired-{uuid4().hex[:8]}")
    db_session.add(task_set)
    db_session.flush()
    batch = Batch(
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        name=f"historical-{uuid4().hex[:8]}",
        batch_type="lerobot",
    )
    db_session.add(batch)
    db_session.commit()

    formatted_path = path.format(
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        batch_id=batch.id,
        session_id=uuid4(),
    )
    formatted_payload = {
        key: (
            value.format(workspace_id=workspace.id, task_set_id=task_set.id)
            if isinstance(value, str)
            else value
        )
        for key, value in (payload or {}).items()
    }
    request = getattr(client, method)
    if method == "get":
        response = request(formatted_path, headers=admin_headers)
    else:
        response = request(
            formatted_path,
            headers=admin_headers,
            json=formatted_payload or None,
        )

    # The path no longer exists. FastAPI answers 404 for unknown GETs and 405 for
    # other methods because the SPA fallback only serves GET.
    assert response.status_code in {404, 405}, response.text
    assert "batch_type" not in response.text
    assert "candidate" not in response.text


def test_legacy_native_lerobot_mutations_are_gone_but_history_is_readable(
    client, admin_headers, db_session
):
    """Historical rows remain auditable but cannot create copy/bundle work."""
    workspace = make_workspace(db_session)
    task_set = TaskSet(workspace_id=workspace.id, name=f"history-{uuid4().hex[:8]}")
    db_session.add(task_set)
    db_session.flush()
    batch = Batch(
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        name=f"historical-{uuid4().hex[:8]}",
        batch_type="lerobot",
    )
    db_session.add(batch)
    db_session.flush()
    dataset = NativeLerobotDataset(
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        batch_id=batch.id,
        name="historical native dataset",
        source_oss_uri="oss://legacy/raw/source/",
        oss_uri="oss://legacy/export/dataset/",
        robot_type="so101",
        dataset_id=f"history-{uuid4().hex[:8]}",
        file_count=1,
        total_size=1,
        completed_at=datetime(2026, 9, 20, 0, 0, 0),
        manifest_sha256="a" * 64,
        marker_sha256="b" * 64,
        copy_status="succeeded",
    )
    db_session.add(dataset)
    db_session.commit()

    readable = client.get(f"/api/v1/native-lerobot-datasets/{dataset.id}", headers=admin_headers)
    assert readable.status_code == 200, readable.text

    for method, path, payload in (
        ("post", f"/api/v1/native-lerobot-datasets/{dataset.id}/bundles", None),
        ("post", f"/api/v1/native-lerobot-datasets/{dataset.id}/copy/retry", None),
        (
            "post",
            f"/api/v1/native-lerobot-datasets/{dataset.id}/source-reauthorization",
            {"candidate_token": "retired-candidate"},
        ),
        ("patch", f"/api/v1/native-lerobot-datasets/{dataset.id}", {"status": "archived"}),
        (
            "post",
            "/api/v1/catalog-datasets/lerobot-imports",
            {"name": "retired catalog import", "native_lerobot_dataset_id": dataset.id},
        ),
    ):
        response = getattr(client, method)(path, headers=admin_headers, json=payload)
        assert response.status_code == 410, response.text
        assert "direct" in str(response.json()["detail"]).lower()
