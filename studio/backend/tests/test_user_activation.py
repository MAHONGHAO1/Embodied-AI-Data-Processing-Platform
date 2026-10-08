from __future__ import annotations

import pytest

from data.database import JOB_STATUS_QUEUED, JOB_STATUS_RETRY_PENDING, JobRun, User
from data.services.user_lifecycle import UserLifecycleConflict, set_user_active
from data.utils.helpers import hash_password


def _create_user(db_session, *, email: str, role: str = "viewer") -> User:
    user = User(email=email, password_hash=hash_password("activation-password"), role=role)
    db_session.add(user)
    db_session.commit()
    return user


def test_admin_can_deactivate_and_reactivate_user(client, admin_headers, db_session):
    target = _create_user(db_session, email="activation-target@example.com")

    disabled = client.patch(
        f"/api/v1/auth/users/{target.id}/status",
        headers=admin_headers,
        json={"is_active": False},
    )
    enabled = client.patch(
        f"/api/v1/auth/users/{target.id}/status",
        headers=admin_headers,
        json={"is_active": True},
    )

    assert disabled.status_code == 200
    assert disabled.json()["data"]["is_active"] is False
    assert enabled.status_code == 200
    assert enabled.json()["data"]["is_active"] is True


def test_deactivation_invalidates_access_refresh_and_login(client, admin_headers, db_session):
    target = _create_user(db_session, email="activation-session@example.com")
    login = client.post(
        "/api/v1/auth/login",
        json={"email": target.email, "password": "activation-password"},
    )
    access = login.json()["data"]["access_token"]

    disabled = client.patch(
        f"/api/v1/auth/users/{target.id}/status",
        headers=admin_headers,
        json={"is_active": False},
    )
    protected = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {access}"},
    )
    refreshed = client.post("/api/v1/auth/refresh")
    relogin = client.post(
        "/api/v1/auth/login",
        json={"email": target.email, "password": "activation-password"},
    )

    assert disabled.status_code == 200
    assert protected.status_code == 401
    assert refreshed.json()["code"] == 401
    assert relogin.json()["code"] == 401


def test_admin_cannot_deactivate_self(client, admin_headers, db_session):
    admin = db_session.query(User).filter(User.email == "admin@quicdata.com").one()

    response = client.patch(
        f"/api/v1/auth/users/{admin.id}/status",
        headers=admin_headers,
        json={"is_active": False},
    )

    assert response.status_code == 409


def test_non_admin_cannot_change_user_status(client, operator_headers, db_session):
    target = _create_user(db_session, email="operator-status-target@example.com")

    response = client.patch(
        f"/api/v1/auth/users/{target.id}/status",
        headers=operator_headers,
        json={"is_active": False},
    )

    assert response.status_code == 403


def test_lifecycle_rejects_deactivating_the_last_active_admin(db_session):
    target = _create_user(db_session, email="last-admin-target@example.com", role="admin")
    actor = _create_user(db_session, email="lifecycle-actor@example.com", role="viewer")
    other_admins = db_session.query(User).filter(User.role == "admin", User.id != target.id).all()
    for admin in other_admins:
        admin.is_active = False
    db_session.commit()

    try:
        with pytest.raises(UserLifecycleConflict, match="最后一个"):
            set_user_active(
                db_session,
                actor_user_id=actor.id,
                target_user_id=target.id,
                is_active=False,
            )
    finally:
        db_session.rollback()
        for admin in other_admins:
            admin.is_active = True
        db_session.commit()


def test_last_active_admin_cannot_be_demoted(client, admin_headers, db_session):
    actor = db_session.query(User).filter(User.email == "admin@quicdata.com").one()
    other_admins = db_session.query(User).filter(User.role == "admin", User.id != actor.id).all()
    for admin in other_admins:
        admin.is_active = False
    db_session.commit()

    try:
        response = client.put(
            f"/api/v1/auth/users/{actor.id}/role",
            headers=admin_headers,
            json={"role": "viewer"},
        )

        assert response.status_code == 409
        db_session.refresh(actor)
        assert actor.role == "admin"
    finally:
        db_session.rollback()
        for admin in other_admins:
            admin.is_active = True
        db_session.commit()


def test_deactivation_cancels_only_unclaimed_jobs(client, admin_headers, db_session):
    target = _create_user(db_session, email="activation-jobs@example.com")
    queued = JobRun(
        id="activationqueuedjob0000000000000001",
        kind="import_scan",
        resource_type="import_session",
        resource_id="activation-session",
        idempotency_key="activation:queued",
        queue="ingest",
        actor_id=target.id,
        status=JOB_STATUS_QUEUED,
        phase=JOB_STATUS_QUEUED,
    )
    retry_pending = JobRun(
        id="activationretryjob00000000000000001",
        kind="dataset_export",
        resource_type="dataset_revision",
        resource_id="1",
        idempotency_key="activation:retry",
        queue="export",
        actor_id=target.id,
        status=JOB_STATUS_RETRY_PENDING,
        phase=JOB_STATUS_RETRY_PENDING,
    )
    db_session.add_all([queued, retry_pending])
    db_session.commit()

    response = client.patch(
        f"/api/v1/auth/users/{target.id}/status",
        headers=admin_headers,
        json={"is_active": False},
    )

    assert response.status_code == 200
    db_session.expire_all()
    assert db_session.get(JobRun, queued.id).status == "cancelled"
    assert db_session.get(JobRun, retry_pending.id).status == "cancelled"


def test_claim_job_rejects_an_inactive_actor(db_session):
    from data.services.job_runs import claim_job

    target = _create_user(db_session, email="inactive-job-claim@example.com")
    target.is_active = False
    job = JobRun(
        id="inactiveactorjob00000000000000001",
        kind="import_scan",
        resource_type="import_session",
        resource_id="inactive-session",
        idempotency_key="inactive:claim",
        queue="ingest",
        actor_id=target.id,
        status=JOB_STATUS_QUEUED,
        phase=JOB_STATUS_QUEUED,
    )
    db_session.add(job)
    db_session.commit()

    result = claim_job(db_session, job.id, worker_id="test-worker")

    assert result.claimed is False
    assert result.reason == "actor_inactive"
    db_session.refresh(job)
    assert job.status == "cancelled"
