"""Shared fixtures and builders for collection management API tests."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from sqlalchemy.orm import Session
from tests.test_episode_objects import verified_entries

from data.database import (
    Episode,
    PersonnelProfile,
    User,
    Workspace,
    WorkspaceMember,
)
from data.models.collection_config import CollectionDeviceModel, CollectionLabel
from data.models.collection_core import CollectionProject, CollectionTask
from data.models.data_package import DataPackage
from data.services.collection_intake_review import review_data_package_intake
from data.services.collection_packages import assign_data_package
from data.services.collection_tasks import create_collection_task
from data.services.collector_profiles import create_collector_profile
from data.services.episode_admission import record_episode_admission_fact


def ensure_admin_in_workspace(db: Session, workspace: Workspace) -> User:
    admin = db.query(User).filter(User.email == "admin@quicdata.com").one()
    exists = (
        db.query(WorkspaceMember)
        .filter(
            WorkspaceMember.workspace_id == workspace.id,
            WorkspaceMember.user_id == admin.id,
        )
        .first()
    )
    if exists is None:
        db.add(WorkspaceMember(workspace_id=workspace.id, user_id=admin.id))
        db.commit()
    return admin


def make_workspace(db: Session) -> Workspace:
    admin = db.query(User).filter(User.email == "admin@quicdata.com").one()
    workspace = Workspace(name=f"coll-api-{uuid4().hex}", creator=admin.email)
    db.add(workspace)
    db.flush()
    db.add(WorkspaceMember(workspace_id=workspace.id, user_id=admin.id))
    db.commit()
    db.refresh(workspace)
    return workspace


def make_label(db: Session, *, category: str, name: str | None = None) -> CollectionLabel:
    label = CollectionLabel(
        category=category,
        name=name or f"{category}-{uuid4().hex[:8]}",
        description="",
        is_active=True,
    )
    db.add(label)
    db.commit()
    db.refresh(label)
    return label


def make_device_model(db: Session) -> CollectionDeviceModel:
    model = CollectionDeviceModel(
        vendor=f"Vendor-{uuid4().hex[:6]}",
        model=f"Model-{uuid4().hex[:6]}",
        device_type="ego",
        modalities_json=["rgb"],
        is_active=True,
    )
    db.add(model)
    db.commit()
    db.refresh(model)
    return model


def make_project(
    db: Session, workspace: Workspace, *, name: str | None = None
) -> CollectionProject:
    project = CollectionProject(
        workspace_id=workspace.id,
        name=name or f"project-{uuid4().hex[:8]}",
        description="",
        status="enabled",
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def make_task(
    db: Session,
    project: CollectionProject,
    *,
    target_hours: str = "4.00",
    package_hours: str = "2.00",
    device_model_id: int | None = None,
) -> CollectionTask:
    task = CollectionTask(
        workspace_id=project.workspace_id,
        collection_project_id=project.id,
        name=f"task-{uuid4().hex[:8]}",
        target_duration_hours=Decimal(target_hours),
        default_package_duration_hours=Decimal(package_hours),
        capture_mode="offline",
        device_model_id=device_model_id,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def make_collector(
    db: Session, workspace: Workspace, *, name: str | None = None
) -> PersonnelProfile:
    profile = create_collector_profile(
        db,
        workspace_id=workspace.id,
        name=name or f"collector-{uuid4().hex[:6]}",
        profile_key=None,
    )
    db.commit()
    db.refresh(profile)
    return profile


def make_assigned_package(
    db: Session,
    workspace: Workspace,
    project: CollectionProject,
    *,
    hours: str = "2.00",
) -> DataPackage:
    device = make_device_model(db)
    task = create_collection_task(
        db,
        workspace_id=workspace.id,
        collection_project_id=project.id,
        name=f"t-{uuid4().hex[:8]}",
        target_duration_hours=Decimal(hours),
        default_package_duration_hours=Decimal(hours),
        device_model_id=device.id,
        label_ids=[],
        created_by_user_id=None,
    )
    db.commit()
    package = db.query(DataPackage).filter(DataPackage.collection_task_id == task.id).one()
    owner = make_collector(db, workspace, name="Owner")
    assign_data_package(
        db,
        workspace_id=workspace.id,
        data_package_id=package.id,
        responsible_collector_id=owner.id,
        operator_collector_id=owner.id,
    )
    db.commit()
    db.refresh(package)
    return package


def seed_package_pending_intake_review(
    db: Session,
    workspace: Workspace,
    project: CollectionProject,
    *,
    episode_hours: tuple[float, ...] = (1.0, 1.0),
) -> tuple[DataPackage, list[Episode]]:
    package = make_assigned_package(
        db,
        workspace,
        project,
        hours=f"{sum(episode_hours):.2f}",
    )
    episodes = [
        Episode(
            episode_uid=f"episode_{uuid4().hex}",
            workspace_id=workspace.id,
            task_set_id=None,
            batch_id=None,
            data_package_id=package.id,
            kind="source",
            modality="rgb",
            source_fingerprint=f"internal-{uuid4().hex}",
            validity_status="valid",
            metadata_json={
                "timing": {"duration_s": hours * 3600},
                "privacy_sensitive": index % 2 == 1,
            },
        )
        for index, hours in enumerate(episode_hours)
    ]
    db.add_all(episodes)
    package.status = "pending_intake_review"
    package.captured_duration_hours = Decimal(f"{sum(episode_hours):.2f}")
    package.qrdf_facts_json = {
        "integrity": {"status": "passed", "algorithm": "sha256"},
        "preview": {"available": True},
    }
    db.commit()
    db.refresh(package)
    for episode in episodes:
        db.refresh(episode)
        record_episode_admission_fact(
            db,
            episode_id=episode.id,
            attempt=1,
            source_fingerprint=episode.source_fingerprint,
            validation_policy_version="v1",
            integrity_status="passed",
            preview_status="ready",
            output_verification_status="verified",
            qrdf_profile="ego",
            report_ref={"uri": f"db://episode/{episode.id}/report"},
            objects=verified_entries(),
        )
    db.commit()
    return package, episodes


def seed_intake_approved_package(
    db: Session,
    workspace: Workspace,
    project: CollectionProject,
    *,
    episode_hours: tuple[float, ...] = (1.0, 1.0),
) -> DataPackage:
    package, _episodes = seed_package_pending_intake_review(
        db,
        workspace,
        project,
        episode_hours=episode_hours,
    )
    review_data_package_intake(
        db,
        workspace_id=workspace.id,
        data_package_id=package.id,
        reviewer_user_id=None,
        verdict="approved",
        rejected_episode_ids=[],
        reason="",
    )
    db.commit()
    db.refresh(package)
    return package


def make_annotator_reviewer(db: Session, workspace: Workspace) -> tuple[User, User]:
    """Create annotator + auditor members for batch assignment tests."""
    annotator = User(
        email=f"ann-{uuid4().hex}@t.com",
        password_hash="x",
        role="annotator",
        is_active=True,
    )
    reviewer = User(
        email=f"rev-{uuid4().hex}@t.com",
        password_hash="x",
        role="auditor",
        is_active=True,
    )
    db.add_all([annotator, reviewer])
    db.flush()
    db.add_all(
        [
            WorkspaceMember(workspace_id=workspace.id, user_id=annotator.id),
            WorkspaceMember(workspace_id=workspace.id, user_id=reviewer.id),
        ]
    )
    db.commit()
    db.refresh(annotator)
    db.refresh(reviewer)
    return annotator, reviewer
