"""External tool path end to end: token, offline manifest, upload, review, annotate.

The test drives the public HTTP API with a self-service bearer token and moves real
bytes through the MinIO test bucket.  Only the asynchronous parse worker is invoked
directly, because Celery is not running inside the test process.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from urllib.request import urlopen
from uuid import uuid4

from tests.collection_api_fixtures import (
    make_assigned_package,
    make_project,
    make_workspace,
)

from data.services.duance_imports import build_duance_import_manifest


def _issue_token(client, headers, name: str) -> str:
    response = client.post("/api/v1/tokens", json={"name": name}, headers=headers)
    assert response.status_code == 200, response.text
    secret = response.json()["data"]["secret"]
    assert secret.startswith("qs_")
    return secret


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _declaration(package_uid: str, episode_dir) -> tuple[dict[str, object], bytes]:
    """One episode declaration exactly as duance builds it from a local episode."""

    from pathlib import Path

    episode_dir = Path(episode_dir)
    metadata_text = (episode_dir / "metadata.json").read_text(encoding="utf-8")
    metadata = json.loads(metadata_text)
    payload = (episode_dir / "data.mcap").read_bytes()
    # duance declares the metadata's own data_file path, not the local layout.
    data_path = str(metadata.get("data_file") or "data.mcap")
    timing = metadata.get("timing") or {}
    data_sha256 = hashlib.sha256(payload).hexdigest()
    manifest = build_duance_import_manifest(
        source={
            "episode_id": str(metadata.get("episode_id") or episode_dir.name),
            "start_ns": str(timing.get("start_timestamp_ns") or 0),
            "end_ns": str(timing.get("end_timestamp_ns") or 1),
            "metadata_sha256": hashlib.sha256(metadata_text.encode("utf-8")).hexdigest(),
            "data_mcap_sha256": data_sha256,
        },
        metadata_text=metadata_text,
        data_file={
            "path": data_path,
            "size_bytes": len(payload),
            "sha256": data_sha256,
        },
    )
    stored = manifest.to_storage()
    return (
        {
            "package_uid": package_uid,
            "source": stored["source"],
            "metadata_text": stored["metadata_text"],
            "data_file": stored["data_file"],
        },
        payload,
    )


def _upload_declared_part(upload_session_id: str, payload: bytes, part_number: int = 1) -> str:
    """Upload one part into the multipart upload the platform just created.

    The product only signs browser upload URLs that point at a public HTTPS
    endpoint, which a loopback MinIO cannot provide, so the test talks to the
    same provider with the same credentials.  The multipart identity, declared
    object identity and completion still go through the platform API.
    """

    import boto3
    from botocore.config import Config

    from data.config import settings
    from data.database import SessionLocal
    from data.models.collection_upload import CollectionUploadSession

    with SessionLocal() as db:
        session = db.get(CollectionUploadSession, upload_session_id)
        assert session is not None, "upload session must exist"
        result = session.result_json or {}
        sources = result.get("oss_multipart_sources") or {}
        declaration = next(iter(sources.values())) if sources else result.get("oss_multipart")
        assert isinstance(declaration, dict), "multipart declaration must exist"

    client = boto3.client(
        "s3",
        endpoint_url=str(settings.storage_endpoint),
        aws_access_key_id=str(settings.storage_access_key_id),
        aws_secret_access_key=str(settings.storage_secret_access_key),
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
    response = client.upload_part(
        Bucket=str(declaration["bucket"]),
        Key=str(declaration["object_key"]),
        UploadId=str(declaration["upload_id"]),
        PartNumber=part_number,
        Body=payload,
    )
    etag = str(response.get("ETag") or "").strip().strip('"')
    assert etag, "MinIO did not return an ETag for the uploaded part"
    return etag


def _seed_annotation_item(db_session, *, workspace, actor_id: int, package_id: int):
    from data.database import WorkspaceMember
    from data.models.annotation_work import AnnotationWorkItem
    from data.models.data_batch import DataBatch

    batch = DataBatch(
        workspace_id=workspace.id,
        name=f"e2e-batch-{uuid4().hex[:8]}",
        integrity_check_enabled=True,
        quality_check_enabled=True,
        compliance_check_enabled=False,
        annotation_enabled=True,
    )
    db_session.add(batch)
    db_session.flush()
    item = AnnotationWorkItem(
        data_batch_id=batch.id,
        data_package_id=package_id,
        workspace_id=workspace.id,
        assignee_user_id=actor_id,
        status="assigned",
    )
    db_session.add(item)
    exists = (
        db_session.query(WorkspaceMember)
        .filter(
            WorkspaceMember.workspace_id == workspace.id,
            WorkspaceMember.user_id == actor_id,
        )
        .first()
    )
    if exists is None:
        db_session.add(WorkspaceMember(workspace_id=workspace.id, user_id=actor_id))
    db_session.commit()
    return item


def test_token_upload_review_fetch_and_batch_annotation(
    client, db_session, admin_headers, annotator_headers, tmp_path, monkeypatch
):
    from tests.test_qrdf_admission import _episode

    from data.config import settings
    from data.database import Episode, JobRun
    from data.integrations.qrdf.admission import run_qrdf_admission_worker
    from data.models.data_package import DataPackage
    from data.services.collection_upload_parse import parse_collection_upload_session

    # Admission scratch must not sit under a symlinked path (macOS /tmp).
    monkeypatch.setattr(settings, "storage_root", str(tmp_path / "storage"))

    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package = make_assigned_package(db_session, workspace, project)
    episode_dir = _episode(tmp_path)
    declaration, payload = _declaration(package.package_uid, episode_dir)

    # 1) A bound user issues a long lived token; every later call is token-only.
    token = _issue_token(client, admin_headers, f"duance-e2e-{uuid4().hex[:8]}")
    headers = _bearer(token)
    me = client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200, me.text
    actor_id = int(me.json()["data"]["id"])

    # 2) Offline manifest: one package row per data package, JSON and CSV.
    manifest = client.get(
        f"/api/v1/collection-tasks/{package.collection_task_id}/offline-manifest",
        params={"workspace_id": workspace.id},
        headers=headers,
    )
    assert manifest.status_code == 200, manifest.text
    rows = manifest.json()["data"]["items"]
    assert [row["package_uid"] for row in rows] == [package.package_uid]
    assert manifest.json()["data"]["manifest_revision"]
    csv_manifest = client.get(
        f"/api/v1/collection-tasks/{package.collection_task_id}/offline-manifest",
        params={"workspace_id": workspace.id, "format": "csv"},
        headers=headers,
    )
    assert csv_manifest.status_code == 200
    assert package.package_uid in csv_manifest.text

    # 3) Upload session + declaration + real MinIO multipart round trip.
    session = client.post(
        "/api/v1/upload-sessions",
        json={
            "workspace_id": workspace.id,
            "collection_project_id": project.id,
            "package_uids": [package.package_uid],
            "upload_mode": "oss_multipart",
        },
        headers=headers,
    )
    assert session.status_code == 200, session.text
    session_id = session.json()["data"]["id"]

    declared = client.post(
        f"/api/v1/upload-sessions/{session_id}/declarations",
        json={"workspace_id": workspace.id, "items": [declaration]},
        headers=headers,
    )
    assert declared.status_code == 200, declared.text
    assert declared.json()["data"]["declared_sources"] == 1
    # Byte uploads must reuse the opaque source id the platform just issued.
    declared_sources = declared.json()["data"]["sources"]
    assert len(declared_sources) == 1
    source_id = declared_sources[0]["source_id"]
    assert declared_sources[0]["package_uid"] == package.package_uid

    initiated = client.post(
        f"/api/v1/upload-sessions/{session_id}/oss/init",
        json={
            "workspace_id": workspace.id,
            "total_size_bytes": len(payload),
            "source_id": source_id,
        },
        headers=headers,
    )
    assert initiated.status_code == 200, initiated.text
    assert initiated.json()["data"]["total_parts"] == 1

    signed = client.post(
        f"/api/v1/upload-sessions/{session_id}/oss/sign-part",
        json={
            "workspace_id": workspace.id,
            "part_number": 1,
            "source_id": source_id,
        },
        headers=headers,
    )
    # A loopback MinIO cannot be exposed as a public browser endpoint, so the
    # product API refuses to sign a browser URL; the test uploads the part with
    # the provider client instead of a signed browser PUT.
    assert signed.status_code == 422, signed.text
    assert "invalid browser upload URL" in signed.json()["detail"]
    etag = _upload_declared_part(session_id, payload)
    completed = client.post(
        f"/api/v1/upload-sessions/{session_id}/oss/complete",
        json={
            "workspace_id": workspace.id,
            "parts": [{"part_number": 1, "etag": etag}],
            "source_id": source_id,
        },
        headers=headers,
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["data"]["status"] in {"uploaded", "parsing"}

    # 4) Parse (the worker body) turns the uploaded object into a reviewable package.
    # The API committed in its own session, so drop the test session's snapshot first.
    db_session.expire_all()
    parse_collection_upload_session(db_session, session_id=session_id)
    db_session.expire_all()
    reviewed_package = db_session.get(DataPackage, package.id)
    assert reviewed_package.status == "pending_intake_review", reviewed_package.status
    episode = (
        db_session.query(Episode)
        .filter(Episode.data_package_id == package.id)
        .order_by(Episode.id.desc())
        .first()
    )
    assert episode is not None, "parse must create the package Episode"

    # The package detail exposes the declared external episode id so Duance can
    # correlate each local episode to its remote Episode before submitting cuts.
    detail = client.get(
        f"/api/v1/data-packages/{package.id}",
        params={"workspace_id": workspace.id},
        headers=headers,
    )
    assert detail.status_code == 200, detail.text
    detail_episodes = detail.json()["data"]["episodes"]
    assert detail_episodes
    assert {item["external_episode_id"] for item in detail_episodes} == {
        declaration["source"]["episode_id"]
    }
    assert {item["id"] for item in detail_episodes} == {episode.id}

    # 4b) The QRDF admission worker (ingest queue body) validates the uploaded bytes
    #     and publishes the raw/process objects the fetch manifest points at.
    admission_job = next(
        (
            job
            for job in db_session.query(JobRun)
            .filter(JobRun.kind == "collection_upload_admission")
            .order_by(JobRun.id.desc())
            .all()
            if (job.detail_json or {}).get("upload_session_id") == session_id
        ),
        None,
    )
    assert admission_job is not None, "parse must queue a QRDF admission job"
    admission_job.status = "running"
    admission_job.lease_token = uuid4().hex
    admission_job.lease_worker_id = "e2e-worker"
    admission_job.lease_expires_at = datetime.utcnow() + timedelta(minutes=10)
    admission_job.attempt_count = max(1, int(admission_job.attempt_count or 0))
    db_session.commit()
    run_qrdf_admission_worker(db_session, admission_job)
    db_session.expire_all()
    admitted = db_session.get(DataPackage, package.id)
    assert admitted.status == "pending_intake_review", admitted.status

    # 5) Intake review approves the package for building.
    approved = client.post(
        f"/api/v1/data-packages/{package.id}/intake-review",
        json={"workspace_id": workspace.id, "verdict": "approved"},
        headers=headers,
    )
    assert approved.status_code == 200, approved.text
    db_session.expire_all()
    assert db_session.get(DataPackage, package.id).status == "intake_approved"

    # 6) Fetch manifest: object identity always, signed download when the deployment
    #    can expose a browser-reachable endpoint (a loopback test bucket cannot).
    fetch = client.post(
        "/api/v1/fetch-manifests",
        json={"workspace_id": workspace.id, "scope": "data_packages", "ids": [package.id]},
        headers=headers,
    )
    assert fetch.status_code == 200, fetch.text
    objects = fetch.json()["data"]["objects"]
    assert objects, "fetch manifest must describe the uploaded object"
    entry = objects[0]
    assert entry["episode_uid"] == episode.episode_uid
    assert entry["key"].startswith("oss://")
    assert entry["size"] == len(payload)
    assert entry["sha256"] == hashlib.sha256(payload).hexdigest()
    if entry["available"]:
        with urlopen(entry["url"]) as download:  # noqa: S310 - signed test bucket URL
            assert hashlib.sha256(download.read()).hexdigest() == entry["sha256"]
        assert entry["expires_at"]
    else:
        assert entry["url"] == ""

    # 7) Algorithm batch submission is idempotent for one client request id.
    item = _seed_annotation_item(
        db_session, workspace=workspace, actor_id=actor_id, package_id=package.id
    )
    body = {
        "workspace_id": workspace.id,
        "client_request_id": f"run-{uuid4().hex[:8]}",
        "items": [
            {
                "work_item_id": item.id,
                # Status-only draft: the package-scoped annotation payload contract
                # is covered by the dedicated annotation tests.
                "payload": {},
                "source": {
                    "kind": "algorithm",
                    "name": "ego-vl",
                    "version": "1.3.0",
                    "run_id": "e2e-run",
                    "confidence": 0.9,
                },
                "review_required": True,
            }
        ],
    }
    first = client.post("/api/v1/annotation-work-items/batch-submit", json=body, headers=headers)
    replay = client.post("/api/v1/annotation-work-items/batch-submit", json=body, headers=headers)
    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    submitted = first.json()["data"]["items"][0]
    assert submitted["status"] == "submitted"
    assert submitted["source"]["kind"] == "algorithm"
    assert replay.json()["data"]["items"][0]["work_item_id"] == submitted["work_item_id"]
    db_session.expire_all()
    assert db_session.query(type(item)).filter(type(item).id == item.id).one().status == "submitted"

    # 8) An annotator without approval rights cannot skip review: it is downgraded
    #    and audited instead of failing the submission.
    annotator_token = _issue_token(client, annotator_headers, f"algo-{uuid4().hex[:8]}")
    annotator_id = int(
        client.get("/api/v1/auth/me", headers=_bearer(annotator_token)).json()["data"]["id"]
    )
    downgraded_item = _seed_annotation_item(
        db_session,
        workspace=workspace,
        actor_id=annotator_id,
        package_id=package.id,
    )
    skip_body = {
        "workspace_id": workspace.id,
        "client_request_id": f"run-{uuid4().hex[:8]}",
        "items": [
            {
                "work_item_id": downgraded_item.id,
                # Status-only draft: the package-scoped annotation payload contract
                # is covered by the dedicated annotation tests.
                "payload": {},
                "source": {"kind": "algorithm", "name": "ego-vl", "version": "1.3.0"},
                "review_required": False,
            }
        ],
    }
    downgraded = client.post(
        "/api/v1/annotation-work-items/batch-submit",
        json=skip_body,
        headers=_bearer(annotator_token),
    )
    assert downgraded.status_code == 200, downgraded.text
    assert downgraded.json()["data"]["items"][0]["review_required"] is True

    from data.database import SecurityAuditEvent

    db_session.expire_all()
    audit_actions = [
        row.action
        for row in db_session.query(SecurityAuditEvent)
        .order_by(SecurityAuditEvent.id.desc())
        .limit(20)
        .all()
    ]
    assert "annotation.review_skip_downgraded" in audit_actions
    db_session.expire_all()
    assert db_session.get(type(downgraded_item), downgraded_item.id).status == "submitted"
