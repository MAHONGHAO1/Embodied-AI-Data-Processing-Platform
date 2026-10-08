"""Package workbench API: real persistence, QRDF validation and frozen exports."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from tests.collection_api_fixtures import make_project, make_workspace, seed_intake_approved_package
from tests.test_annotation_work_items_api import _headers_for, _make_annotator, _make_reviewer
from tests.test_episode_objects import verified_entries

from data.database import Episode, EpisodeAnnotation
from data.models.annotation_work import (
    AnnotationSubmission,
    AnnotationSubmissionReview,
    AnnotationWorkItem,
)
from data.models.data_asset import DataAsset
from data.models.episode_admission import EpisodeAdmissionFact
from data.services.annotation_work_items import assign_work_items_for_batch
from data.services.data_assets import DataAssetSnapshotError, publish_data_asset
from data.services.data_batches import create_data_batch
from data.services.package_annotation_workbench import DRAFT_SCHEMA

START = 9_007_199_254_740_993_123
END = START + 5_000_000_000


def _fact_data_sha256(fact) -> str:
    from data.services.episode_objects import data_object, fact_objects

    return data_object(fact_objects(fact)).ref["sha256"]


class MappingStore:
    def __init__(self):
        self.objects = {}
        self.signs = []
        self.downloads = []

    def sign_get(self, ref, *, expires):
        self.signs.append(ref.object_key)
        return f"https://preview.invalid/{ref.object_key}?expires={expires}"

    def download_file(self, ref, destination):
        self.downloads.append(ref.object_key)
        Path(destination).write_bytes(self.objects[ref.object_key])
        return ref


def object_ref(role, key, payload=b"object"):
    return {
        "bucket_role": role,
        "object_key": key,
        "version_id": "pinned-v1",
        "etag": hashlib.md5(payload).hexdigest(),
        "size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


@pytest.fixture
def package_work(client, db_session, monkeypatch):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package = seed_intake_approved_package(
        db_session, workspace, project, episode_hours=(0.01, 0.01)
    )
    annotator = _make_annotator(db_session, workspace)
    other = _make_annotator(db_session, workspace)
    reviewer = _make_reviewer(db_session, workspace)
    batch, _ = create_data_batch(
        db_session,
        workspace_id=workspace.id,
        name=f"WB-{uuid4().hex}",
        data_package_ids=[package.id],
        label_ids=[],
        integrity_check_enabled=False,
        quality_check_enabled=False,
        compliance_check_enabled=False,
        annotation_enabled=True,
        annotator_user_ids=[annotator.id],
        reviewer_user_id=reviewer.id,
        review_mode="single",
        created_by_user_id=None,
    )
    batch.status = "annotating"
    store = MappingStore()
    episodes = (
        db_session.query(Episode).filter_by(data_package_id=package.id).order_by(Episode.id).all()
    )
    for episode in episodes:
        episode.metadata_json = {
            "collection_upload": {"external_episode_id": f"qrdf-{episode.id}"},
            "timing": {
                "start_timestamp_ns": str(START),
                "end_timestamp_ns": str(END),
                "duration_s": 5,
            },
        }
        fact = (
            db_session.query(EpisodeAdmissionFact).filter_by(episode_id=episode.id, attempt=1).one()
        )
        raw = object_ref("raw", f"raw/{episode.id}/data.mcap", f"mcap-{episode.id}".encode())
        timeline = json.dumps(
            {
                "topic": "/camera",
                "entries": [
                    {
                        "kind": "frame",
                        "frame_index": index,
                        "timestamp_ns": str(START + index * 1_000_000_000),
                        "video_pts_us": str(index * 1_000_000),
                    }
                    for index in range(5)
                ],
            }
        ).encode()
        timeline_key = f"process/{episode.id}/timeline.json"
        store.objects[timeline_key] = timeline
        objects = verified_entries()
        objects[0]["ref"] = raw
        objects[4]["topic"] = "/camera"
        objects[4]["ref"] = object_ref("process", f"process/{episode.id}/video.mp4")
        objects[5]["topic"] = "/camera"
        objects[5]["ref"] = object_ref("process", timeline_key, timeline)
        fact.objects_json = objects
    item = assign_work_items_for_batch(db_session, batch_id=batch.id)[0]
    db_session.commit()
    monkeypatch.setattr(
        "data.services.package_annotation_workbench.get_storage_provider", lambda: store
    )
    monkeypatch.setattr(
        "data.routers.review_work_items.schedule_asset_creation", lambda _batch: None
    )
    return {
        "workspace": workspace,
        "package": package,
        "batch": batch,
        "item": item,
        "annotator": annotator,
        "other": other,
        "reviewer": reviewer,
        "episodes": episodes,
        "store": store,
        "db": db_session,
        "client": client,
    }


def request(work, method, suffix, body=None, *, review=False, actor=None):
    item_id = work["item"].review_item.id if review else work["item"].id
    domain = "review" if review else "annotation"
    path = f"/api/v1/{domain}-work-items/{item_id}{suffix}"
    headers = _headers_for(actor or work["reviewer" if review else "annotator"])
    return getattr(work["client"], method)(
        path,
        headers=headers,
        **(
            {"json": {"workspace_id": work["workspace"].id, **body}}
            if body is not None
            else {"params": {"workspace_id": work["workspace"].id}}
        ),
    )


def segment(index=0, *, start=START, end=START + 1_000_000_000, description="Pick up the cup"):
    return {
        "id": f"local-{index}",
        "start_ns": str(start),
        "end_ns": str(end),
        "description": description,
    }


def draft(work, *, all_invalid=False):
    return {
        "schema": DRAFT_SCHEMA,
        "episodes": {
            str(episode.id): (
                {"conclusion": "no_valid_segments", "reason": "Camera was covered", "segments": []}
                if all_invalid
                else {"conclusion": "segments", "segments": [segment()]}
            )
            for episode in work["episodes"]
        },
    }


def save(work, payload=None):
    work["db"].refresh(work["item"])
    item = work["item"]
    response = request(
        work,
        "patch",
        "",
        {
            "draft_json": payload or draft(work),
            "base_version": item.draft_version,
            "expected_generation": item.generation,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def submit(work):
    work["db"].refresh(work["item"])
    return request(
        work,
        "post",
        "/submit",
        {
            "base_version": work["item"].draft_version,
            "expected_generation": work["item"].generation,
        },
    )


def review_body(work, **extra):
    work["db"].refresh(work["item"].review_item)
    review = work["item"].review_item
    return {
        "expected_submission_id": review.submission_id,
        "expected_generation": review.generation,
        **extra,
    }


def test_projection_authorizes_frozen_members_and_only_signs_current_episode(package_work):
    work = package_work
    response = request(work, "get", "/workbench")
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert result["capabilities"]["edit"] is True
    assert len(result["episodes"]) == 2
    assert work["store"].signs == []
    ep = work["episodes"][0]
    response = request(work, "get", f"/episodes/{ep.id}/workbench")
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert result["timeline"]["start_ns"] == str(START)
    assert result["source_binding"]["qrdf_episode_id"] == f"qrdf-{ep.id}"
    assert result["media"]["preview"]["playback_timeline"]["entries"][1]["timestamp_ns"] == str(
        START + 1_000_000_000
    )
    assert all(f"/{ep.id}/" in key for key in work["store"].signs)
    assert request(work, "get", "/workbench", actor=work["other"]).status_code == 403
    assert request(work, "get", "/episodes/2147483647/workbench").status_code == 404
    assert len(work["store"].signs) == 2

    fact = work["db"].query(EpisodeAdmissionFact).filter_by(episode_id=ep.id, attempt=1).one()
    fact.is_current = False
    work["db"].flush()
    work["db"].add(
        EpisodeAdmissionFact(
            episode_id=ep.id,
            attempt=2,
            is_current=True,
            source_fingerprint=ep.source_fingerprint,
            integrity_status="failed",
            preview_status="failed",
            output_verification_status="failed",
        )
    )
    work["db"].commit()
    response = request(work, "get", f"/episodes/{ep.id}/workbench")
    assert response.status_code == 200
    assert response.json()["data"]["source_binding"]["admission_attempt"] == 1


def test_draft_cas_partial_descriptions_and_empty_submit(package_work):
    work = package_work
    assert submit(work).status_code == 422
    missing = request(work, "patch", "", {"draft_json": draft(work)})
    assert missing.status_code == 422
    partial = draft(work)
    partial["episodes"] = {
        str(work["episodes"][0].id): {
            "conclusion": "segments",
            "segments": [segment(description="")],
        }
    }
    saved = save(work, partial)
    assert saved["draft_version"] == 1
    stale = request(
        work, "patch", "", {"draft_json": draft(work), "base_version": 0, "expected_generation": 1}
    )
    assert stale.status_code == 409
    assert submit(work).status_code == 422
    complete = draft(work)
    complete["episodes"][str(work["episodes"][0].id)]["segments"][0]["description"] = " "
    save(work, complete)
    assert "annotation_description_required" in submit(work).text
    no_reason = draft(work, all_invalid=True)
    no_reason["episodes"][str(work["episodes"][0].id)]["reason"] = ""
    save(work, no_reason)
    assert "annotation_no_valid_reason_required" in submit(work).text
    assert (
        work["db"]
        .query(AnnotationSubmission)
        .filter_by(annotation_work_item_id=work["item"].id)
        .count()
        == 0
    )


def test_ranges_reject_overlap_extra_members_and_rounded_numbers(package_work):
    work = package_work
    payload = draft(work)
    first = payload["episodes"][str(work["episodes"][0].id)]
    first["segments"].append(segment(1, start=START + 1, end=START + 2_000_000_000))
    response = request(
        work, "patch", "", {"draft_json": payload, "base_version": 0, "expected_generation": 1}
    )
    assert response.status_code == 422 and "overlap" in response.text
    payload = draft(work)
    payload["episodes"]["999999"] = deepcopy(first)
    response = request(
        work, "patch", "", {"draft_json": payload, "base_version": 0, "expected_generation": 1}
    )
    assert response.status_code == 422
    payload = draft(work)
    payload["episodes"][str(work["episodes"][0].id)]["segments"][0]["start_ns"] = START
    response = request(
        work, "patch", "", {"draft_json": payload, "base_version": 0, "expected_generation": 1}
    )
    assert response.status_code == 422
    payload = draft(work)
    payload["episodes"][str(work["episodes"][0].id)]["segments"][0]["end_ns"] = str(END + 1)
    save(work, payload)
    assert "out_of_bounds" in submit(work).text


def test_submit_is_fixed_idempotent_and_stale_review_cannot_approve_resubmission(package_work):
    work = package_work
    payload = draft(work)
    payload["episodes"][str(work["episodes"][0].id)]["segments"] = [
        segment(),
        segment(1, start=START + 1_000_000_000, end=START + 2_000_000_000),
        segment(2, start=START + 3_000_000_000, end=END),
    ]
    save(work, payload)
    work["db"].refresh(work["item"])
    versions = {
        "base_version": work["item"].draft_version,
        "expected_generation": work["item"].generation,
    }
    first = request(work, "post", "/submit", versions)
    assert first.status_code == 200, first.text
    submission_id = first.json()["data"]["submission_id"]
    retry = request(work, "post", "/submit", versions)
    assert retry.status_code == 200 and retry.json()["data"]["submission_id"] == submission_id
    stale_save = request(work, "patch", "", {**versions, "draft_json": payload})
    assert stale_save.status_code == 409
    old_review = review_body(work)
    assert request(work, "post", "/approve", {}, review=True).status_code == 422
    response = request(work, "get", f"/episodes/{work['episodes'][0].id}/workbench", review=True)
    assert response.status_code == 200
    assert response.json()["data"]["capabilities"]["edit"] is False
    assert response.json()["data"]["entry"]["effective_duration_ns"] == "4000000000"
    assert (
        request(
            work, "post", "/return", {**old_review, "reason": "Describe the grasp"}, review=True
        ).status_code
        == 200
    )
    changed = draft(work)
    changed["episodes"][str(work["episodes"][0].id)]["segments"][0]["description"] = "Grasp the cup"
    save(work, changed)
    # Returned result remains the fixed old submission, even after new draft saves.
    old_result = request(
        work, "get", f"/episodes/{work['episodes'][0].id}/workbench", review=True
    ).json()["data"]
    assert old_result["entry"]["segments"][0]["description"] == "Pick up the cup"
    assert submit(work).status_code == 200
    assert request(work, "post", "/approve", old_review, review=True).status_code == 409
    approved = request(work, "post", "/approve", review_body(work), review=True)
    assert approved.status_code == 200, approved.text
    assert request(work, "post", "/approve", review_body(work), review=True).status_code == 409
    submissions = (
        work["db"]
        .query(AnnotationSubmission)
        .filter_by(annotation_work_item_id=work["item"].id)
        .all()
    )
    assert len(submissions) == 2
    assert len({row.id for row in submissions}) == 2
    decisions = (
        work["db"]
        .query(AnnotationSubmissionReview)
        .filter(AnnotationSubmissionReview.submission_id.in_([row.id for row in submissions]))
        .all()
    )
    assert {row.decision for row in decisions} == {"returned", "approved"}


def test_assets_use_approved_revision_and_exclude_unselected_and_invalid_episodes(package_work):
    work = package_work
    payload = draft(work)
    invalid = work["episodes"][1]
    payload["episodes"][str(invalid.id)] = {
        "conclusion": "no_valid_segments",
        "reason": "No task action",
        "segments": [],
    }
    save(work, payload)
    assert submit(work).status_code == 200
    assert request(work, "post", "/approve", review_body(work), review=True).status_code == 200
    ep = work["episodes"][0]
    db = work["db"]
    db.add(
        EpisodeAnnotation(
            episode_id=ep.id, version=999, payload_json={"unrelated": "newer global version"}
        )
    )
    db.commit()
    asset = publish_data_asset(db, batch_id=work["batch"].id)
    db.commit()
    assert asset.episode_ids_json == [ep.id]
    frozen = asset.source_snapshot_json["episodes"][0]
    assert frozen["annotation_revision"]["version"] == 1
    assert frozen["annotation_submission_id"] == work["item"].current_submission_id
    assert frozen["effective_duration_ns"] == "1000000000"
    assert len(frozen["effective_segments"]) == 1
    assert frozen["effective_segments"][0]["start_ns"] == str(START)
    assert frozen["effective_segments"][0]["description"] == "Pick up the cup"
    assert Decimal(frozen["valid_duration_hours"]) * Decimal(3_600_000_000_000) == Decimal(
        1_000_000_000
    )
    assert asset.source_snapshot_json["no_valid_episode_conclusions"][0]["episode_id"] == invalid.id
    assert db.query(EpisodeAnnotation).filter_by(episode_id=invalid.id).count() == 0


def test_all_invalid_completes_with_zero_summary_and_no_asset(package_work):
    work = package_work
    save(work, draft(work, all_invalid=True))
    assert submit(work).status_code == 200
    assert request(work, "post", "/approve", review_body(work), review=True).status_code == 200
    assert publish_data_asset(work["db"], batch_id=work["batch"].id) is None
    work["db"].commit()
    work["db"].refresh(work["batch"])
    assert work["batch"].status == "no_publishable_asset"
    assert (
        work["batch"].assignment_snapshot_json["annotation_outcome_summary"][
            "effective_duration_ns"
        ]
        == "0"
    )
    assert work["db"].query(DataAsset).filter_by(data_batch_id=work["batch"].id).count() == 0


def test_missing_or_changed_mapping_disables_edit_and_blocks_submission(package_work):
    work = package_work
    first = work["episodes"][0]
    key = f"process/{first.id}/timeline.json"
    original = work["store"].objects.pop(key)
    response = request(work, "get", f"/episodes/{first.id}/workbench")
    assert response.status_code == 200
    assert response.json()["data"]["media"]["preview"]["available"] is True
    assert response.json()["data"]["capabilities"]["edit"] is False
    save(work)
    assert submit(work).status_code == 422
    work["store"].objects[key] = original.replace(b'"frame_index": 0', b'"frame_index": 1')
    assert submit(work).status_code == 422
    work["store"].objects[key] = original
    assert submit(work).status_code == 200


def test_submission_evidence_is_database_immutable_and_historical_assets_fail_closed(package_work):
    work = package_work
    save(work)
    assert submit(work).status_code == 200
    work["db"].refresh(work["item"])
    submission_id = work["item"].current_submission_id
    with pytest.raises(DBAPIError, match="immutable"):
        work["db"].execute(
            text(
                "UPDATE annotation_submissions SET draft_version = draft_version + 1 WHERE id = :id"
            ),
            {"id": submission_id},
        )
    work["db"].rollback()
    assert request(work, "post", "/approve", review_body(work), review=True).status_code == 200
    work["db"].refresh(work["item"])
    work["item"].current_submission_id = None
    work["db"].commit()
    with pytest.raises(DataAssetSnapshotError, match="approved_annotation_submission_unavailable"):
        publish_data_asset(work["db"], batch_id=work["batch"].id)
    work["db"].rollback()


def test_reassign_generation_blocks_late_save_even_when_assignee_returns(package_work):
    from data.database import User

    work = package_work
    admin = work["db"].query(User).filter_by(email="admin@quicdata.com").one()
    payload = draft(work)
    for actor in (work["other"], work["annotator"]):
        response = request(
            work, "post", "/reassign", {"to_user_id": actor.id, "reason": "coverage"}, actor=admin
        )
        assert response.status_code == 200, response.text
    late = request(
        work, "patch", "", {"base_version": 0, "expected_generation": 1, "draft_json": payload}
    )
    assert late.status_code == 409
    assert "generation_conflict" in late.text


def test_qrdf_adapter_checks_actual_source_and_every_track_range(package_work):
    from tests.test_annotation_work_items_api import _qrdf_annotation_payload

    work = package_work
    entries = {}
    for episode in work["episodes"]:
        fact = (
            work["db"].query(EpisodeAdmissionFact).filter_by(episode_id=episode.id, attempt=1).one()
        )
        payload = _qrdf_annotation_payload(episode.id)
        payload["source"] = {
            "episode_id": f"qrdf-{episode.id}",
            "data_sha256": _fact_data_sha256(fact),
        }
        payload["tracks"][0]["items"][0]["target"].update(
            start_ns=str(START), end_ns=str(START + 1_000_000_000)
        )
        entries[str(episode.id)] = payload
    first_id = str(work["episodes"][0].id)
    entries[first_id]["source"]["data_sha256"] = "f" * 64
    body = {
        "workspace_id": work["workspace"].id,
        "client_request_id": str(uuid4()),
        "items": [{"work_item_id": work["item"].id, "payload": {"episodes": entries}}],
    }
    response = work["client"].post(
        "/api/v1/annotation-work-items/batch-submit",
        headers=_headers_for(work["annotator"]),
        json=body,
    )
    assert response.status_code == 422 and "source_mismatch" in response.text
    fact = (
        work["db"]
        .query(EpisodeAdmissionFact)
        .filter_by(episode_id=work["episodes"][0].id, attempt=1)
        .one()
    )
    entries[first_id]["source"]["data_sha256"] = _fact_data_sha256(fact)
    entries[first_id]["tracks"].append(
        {
            "name": "event",
            "items": [
                {
                    "id": "01998211-1234-7000-8000-123456789abd",
                    "target": {"type": "time_point", "timestamp_ns": str(END + 1)},
                    "event_type": "grasp",
                }
            ],
        }
    )
    response = work["client"].post(
        "/api/v1/annotation-work-items/batch-submit",
        headers=_headers_for(work["annotator"]),
        json=body,
    )
    assert response.status_code == 422 and "out_of_bounds" in response.text
    entries[first_id]["tracks"].pop()
    response = work["client"].post(
        "/api/v1/annotation-work-items/batch-submit",
        headers=_headers_for(work["annotator"]),
        json=body,
    )
    assert response.status_code == 200, response.text


def test_typed_qrdf_and_effective_segments_require_the_same_ids(package_work):
    from tests.test_annotation_work_items_api import _qrdf_annotation_payload

    work = package_work
    episode = work["episodes"][0]
    fact = work["db"].query(EpisodeAdmissionFact).filter_by(episode_id=episode.id, attempt=1).one()
    payload = draft(work, all_invalid=True)
    selected = segment()
    qrdf = _qrdf_annotation_payload(episode.id)
    qrdf["source"] = {
        "episode_id": f"qrdf-{episode.id}",
        "data_sha256": _fact_data_sha256(fact),
    }
    qrdf_segment = qrdf["tracks"][0]["items"][0]
    qrdf_segment["target"].update(start_ns=selected["start_ns"], end_ns=selected["end_ns"])
    qrdf_segment["high_level_subtask"] = selected["description"]
    payload["episodes"][str(episode.id)] = {
        "conclusion": "segments",
        "segments": [selected],
        "qrdf_payload": qrdf,
    }
    save(work, payload)
    response = submit(work)
    assert response.status_code == 422 and "effective_ranges_mismatch" in response.text
    selected["id"] = qrdf_segment["id"]
    save(work, payload)
    response = submit(work)
    assert response.status_code == 200, response.text


def test_concurrent_save_and_submit_apply_once_under_same_batch_lock(package_work):
    from concurrent.futures import ThreadPoolExecutor

    from data.database import SessionLocal, User
    from data.services.annotation_work_items import (
        AnnotationWorkConflict,
        save_annotation_draft,
        submit_annotation_work_item,
    )

    work = package_work
    saved = save(work)
    params = {
        "workspace_id": work["workspace"].id,
        "item_id": work["item"].id,
        "base_version": saved["draft_version"],
        "expected_generation": saved["generation"],
    }
    actor_id = work["annotator"].id
    payload = draft(work)

    def execute(action):
        with SessionLocal() as db:
            try:
                if action == "save":
                    save_annotation_draft(
                        db, actor=db.get(User, actor_id), draft_json=payload, **params
                    )
                else:
                    submit_annotation_work_item(db, actor=db.get(User, actor_id), **params)
                db.commit()
                return action
            except AnnotationWorkConflict:
                db.rollback()
                return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(execute, ("save", "submit")))
    assert results.count("conflict") == 1
    work["db"].expire_all()
    item = work["db"].get(AnnotationWorkItem, params["item_id"])
    count = (
        work["db"].query(AnnotationSubmission).filter_by(annotation_work_item_id=item.id).count()
    )
    assert count == (1 if "submit" in results else 0)
    assert item.draft_version == saved["draft_version"] + (1 if "save" in results else 0)
