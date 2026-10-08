"""Duance <-> Studio client-admission contract: documented examples match the schemas."""

import json
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError
from tests.collection_api_fixtures import (
    make_project,
    make_workspace,
    seed_package_pending_intake_review,
)
from tests.test_episode_objects import verified_entries

from data.config import settings
from data.integrations.qrdf.admission import TRAINING_READINESS_ISSUE_CODES
from data.models.collection_upload import CollectionUploadSession, CollectionUploadSessionPackage
from data.schemas.client_admission import (
    ClientAdmissionRequest,
    DeclarationResponse,
    SignedPartResponse,
    SourceAdmissionStatus,
    UploadCapabilitiesResponse,
)
from data.services.client_admission import (
    NON_BLOCKING_ISSUE_CODES,
    client_admission_capabilities,
)
from data.services.collection_upload_parse import refresh_upload_admission_counts
from data.services.episode_admission import record_episode_admission_fact

DOC = Path(__file__).resolve().parents[2] / "docs" / "COLLECTION_UPLOAD_API.md"
_EXAMPLE = re.compile(r"```json ([a-z0-9-]+)\n(.*?)\n```", re.S)


def doc_examples() -> dict[str, Any]:
    text = DOC.read_text(encoding="utf-8")
    return {name: json.loads(body) for name, body in _EXAMPLE.findall(text)}


def test_documented_examples_match_the_contract_schemas():
    examples = doc_examples()
    UploadCapabilitiesResponse.model_validate(examples["capabilities-response"])
    ClientAdmissionRequest.model_validate(examples["declaration-item"]["client_admission"])
    DeclarationResponse.model_validate(examples["declaration-response"])
    SignedPartResponse.model_validate(examples["sign-part-response"])
    for item in examples["package-source-admission"]:
        SourceAdmissionStatus.model_validate(item)


def test_non_blocking_codes_are_the_training_readiness_codes():
    assert TRAINING_READINESS_ISSUE_CODES is NON_BLOCKING_ISSUE_CODES
    assert NON_BLOCKING_ISSUE_CODES == {
        "NO_VALID_TRAINING_FRAMES",
        "NO_VALID_EGO_FRAMES",
        "NO_STATE_TOPIC",
        "NO_ACTION_TOPIC",
    }


def test_capabilities_follow_configuration(monkeypatch):
    monkeypatch.setattr(settings, "accept_client_admission", False)
    assert client_admission_capabilities() == {
        "enabled": False,
        "accepted_qrdf_versions": ["0.2.1"],
        "accepted_policy_versions": ["v1"],
        "non_blocking_issue_codes": sorted(NON_BLOCKING_ISSUE_CODES),
    }


def test_capabilities_endpoint_is_not_shadowed_by_session_lookup(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    response = client.get(
        "/api/v1/upload-sessions/capabilities",
        params={"workspace_id": workspace.id},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    body = UploadCapabilitiesResponse.model_validate(response.json()["data"])
    assert body.client_admission.enabled is True
    assert body.client_admission.non_blocking_issue_codes == sorted(NON_BLOCKING_ISSUE_CODES)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c.pop("report"),
        lambda c: c.pop("qrdf_version"),
        lambda c: c["report"].pop("media_validation"),
        lambda c: c["report"].pop("issues"),
        lambda c: c["files"][0].update(sha256="ABC"),
        lambda c: c["files"][0].update(size_bytes=0),
        lambda c: c["files"][0].update(extra=True),
        lambda c: c["report"]["issues"].append({"severity": "FATAL", "code": "X"}),
        lambda c: c["report"]["issues"].append({"severity": "ERROR"}),
        lambda c: c.update(extra=1),
    ],
)
def test_client_admission_request_rejects_malformed_envelopes(mutate):
    item = doc_examples()["declaration-item"]["client_admission"]
    mutate(item)
    with pytest.raises(ValidationError):
        ClientAdmissionRequest.model_validate(item)


def test_documented_sign_part_request_matches_the_router_model():
    from data.routers.collection_upload_sessions import OssSignPartRequest

    request = OssSignPartRequest.model_validate(doc_examples()["sign-part-request"])
    assert request.file_id and request.content_md5 and request.source_id is None


def _source_state(source_id, package_uid, episode_id, **extra):
    return {
        "source_id": source_id,
        "package_uid": package_uid,
        "episode_id": episode_id,
        "status": "ready",
        "attempt": 2,
        "error_code": "",
        "metadata_text": "{}",
        **extra,
    }


def test_package_detail_exposes_integrity_source_and_fallback(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package, (client_episode, server_episode) = seed_package_pending_intake_review(
        db_session, workspace, project
    )
    record_episode_admission_fact(
        db_session,
        episode_id=client_episode.id,
        attempt=2,
        source_fingerprint=client_episode.source_fingerprint,
        validation_policy_version="v1",
        integrity_status="passed",
        preview_status="ready",
        output_verification_status="verified",
        report_ref={"ok": True},
        objects=verified_entries(),
        integrity_source="client",
    )
    client_source, fallback_source = "a" * 64, "b" * 64
    upload = CollectionUploadSession(
        id=str(uuid4()),
        workspace_id=workspace.id,
        collection_project_id=project.id,
        status="succeeded",
        upload_mode="oss_multipart",
        result_json={
            "admission_sources": {
                client_source: _source_state(
                    client_source, package.package_uid, client_episode.id, integrity_source="client"
                ),
                fallback_source: _source_state(
                    fallback_source,
                    package.package_uid,
                    server_episode.id,
                    integrity_source="server",
                    client_admission_fallback="client_preview_invalid",
                ),
            }
        },
    )
    db_session.add(upload)
    db_session.flush()
    db_session.add(
        CollectionUploadSessionPackage(
            upload_session_id=upload.id,
            data_package_id=package.id,
            package_uid=package.package_uid,
        )
    )
    # SessionLocal is autoflush=False; refresh_upload_admission_counts queries
    # CollectionUploadSessionPackage via db.scalars(select(...)), which will not
    # see the link above without an explicit flush first.
    db_session.flush()
    refresh_upload_admission_counts(db_session, upload)
    db_session.commit()

    response = client.get(
        f"/api/v1/data-packages/{package.id}",
        params={"workspace_id": workspace.id},
        headers=admin_headers,
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    sources = {
        item.source_id: item
        for item in (
            SourceAdmissionStatus.model_validate(raw)
            for raw in data["qrdf_facts"]["source_admission"]["sources"]
        )
    }
    assert sources[client_source].integrity_source == "client"
    assert sources[client_source].client_admission_fallback is None
    assert sources[fallback_source].integrity_source == "server"
    assert sources[fallback_source].client_admission_fallback == "client_preview_invalid"
    assert {item["id"]: item["integrity_source"] for item in data["episodes"]} == {
        client_episode.id: "client",
        server_episode.id: "server",
    }
