"""Regression coverage for global identities with no legacy workspace_id."""

from uuid import uuid4

import pytest
from sqlalchemy import inspect

from data.database import (
    Batch,
    Episode,
    EpisodeCollectorAttribution,
    PersonnelProfile,
    TaskLabel,
    TaskSet,
    User,
    Workspace,
    WorkspaceMember,
    WorkspacePersonnelProfile,
)
from data.services.collector_profiles import (
    add_workspace_membership,
    create_collector_profile,
    require_collector_profile_admin,
)
from data.services.episode_workbench import (
    CollectorAttributionImmutable,
    WorkbenchError,
    _apply_collector_attribution,
    _validate_collector_choice,
    effective_collector_attribution,
)
from data.services.import_intake import _validated_candidate_attribution_override
from data.services.import_sessions import create_import_session


@pytest.fixture
def context(db_session):
    suffix = uuid4().hex
    workspaces = [Workspace(name=f"collector-{suffix}-{i}") for i in range(3)]
    db_session.add_all(workspaces)
    db_session.flush()
    batches = []
    for workspace in workspaces:
        task_set = TaskSet(workspace_id=workspace.id, name=f"task-{suffix}")
        db_session.add(task_set)
        db_session.flush()
        batch = Batch(
            workspace_id=workspace.id,
            task_set_id=task_set.id,
            name="collector regression",
            batch_type="ego",
        )
        db_session.add(batch)
        batches.append(batch)
    label = TaskLabel(key=f"collector-{suffix}", name="collector regression")
    db_session.add(label)
    profile = create_collector_profile(
        db_session,
        workspace_id=workspaces[0].id,
        name="Same name",
        profile_key=None,
    )
    add_workspace_membership(db_session, workspace_id=workspaces[1].id, profile_id=profile.id)
    db_session.flush()
    episode = Episode(
        workspace_id=workspaces[0].id,
        task_set_id=batches[0].task_set_id,
        batch_id=batches[0].id,
        episode_uid=f"collector-{suffix}",
        kind="source",
        modality="ego",
    )
    db_session.add(episode)
    db_session.commit()
    assert profile.workspace_id is None
    return workspaces, batches, label, profile, episode


def test_default_collector_accepts_memberships_and_rejects_unrelated_workspace(
    db_session,
    context,
):
    actor = db_session.query(User).filter_by(email="admin@quicdata.com").one()
    _, batches, label, profile, _ = context
    for index, batch in enumerate(batches):
        fields = {
            "batch_id": batch.id,
            "import_type": "chunked_upload",
            "actor_id": actor.id,
            "task_label_id": label.id,
            "default_collector_profile_id": profile.id,
            "default_collection_device_id": None,
        }
        if index < 2:
            session = create_import_session(db_session, **fields)
            assert session.default_collector_profile_id == profile.id
        else:
            with pytest.raises(ValueError, match="collector profile is unavailable"):
                create_import_session(db_session, **fields)
            db_session.rollback()


@pytest.mark.parametrize(
    "unavailable", ["inactive", "missing", "removed_membership", "legacy_only"]
)
def test_unavailable_collectors_are_denied_in_all_selection_paths(
    db_session,
    context,
    unavailable,
):
    actor = db_session.query(User).filter_by(email="admin@quicdata.com").one()
    workspaces, batches, label, profile, episode = context
    profile_id = profile.id
    if unavailable == "inactive":
        profile.is_active = False
    elif unavailable == "missing":
        profile_id = 2_000_000_000
    else:
        db_session.query(WorkspacePersonnelProfile).filter_by(
            workspace_id=workspaces[0].id,
            personnel_profile_id=profile.id,
        ).delete()
        if unavailable == "legacy_only":
            profile.workspace_id = workspaces[0].id
    db_session.commit()
    with pytest.raises(ValueError, match="collector profile is unavailable"):
        create_import_session(
            db_session,
            batch_id=batches[0].id,
            import_type="chunked_upload",
            actor_id=actor.id,
            task_label_id=label.id,
            default_collector_profile_id=profile_id,
            default_collection_device_id=None,
        )
    db_session.rollback()
    with pytest.raises(WorkbenchError, match="collector_profile_invalid"):
        _validate_collector_choice(db_session, episode=episode, collector_profile_id=profile_id)
    with pytest.raises(ValueError, match="collector profile is unavailable"):
        _validated_candidate_attribution_override(
            db_session,
            batch=batches[0],
            collector_profile_id=profile_id,
            collection_device_id=None,
        )


def test_manual_attribution_uses_membership_and_preserves_unknown_and_verified_lock(
    db_session, context
):
    _, batches, _, profile, episode = context
    actor = db_session.query(User).filter_by(email="admin@quicdata.com").one()
    for batch in batches[:2]:
        assert (
            _validated_candidate_attribution_override(
                db_session,
                batch=batch,
                collector_profile_id=profile.id,
                collection_device_id=None,
            )["collector_profile_id"]
            == profile.id
        )
    with pytest.raises(ValueError, match="collector profile is unavailable"):
        _validated_candidate_attribution_override(
            db_session,
            batch=batches[2],
            collector_profile_id=profile.id,
            collection_device_id=None,
        )
    for selected in (profile.id, None):
        _apply_collector_attribution(
            db_session,
            episode=episode,
            actor=actor,
            payload={},
            override_collector_profile_id=selected,
            annotation_version=1,
            note="global collector regression",
        )
        db_session.flush()
        latest = (
            db_session.query(EpisodeCollectorAttribution)
            .filter_by(
                root_source_episode_id=episode.id,
            )
            .order_by(EpisodeCollectorAttribution.id.desc())
            .first()
        )
        assert latest.collector_profile_id == selected
    foreign_episode = Episode(workspace_id=batches[2].workspace_id)
    # A source with no persisted attribution still must respect workspace membership.
    foreign_episode.id = -1
    foreign_episode.kind = "source"
    with pytest.raises(WorkbenchError, match="collector_profile_invalid"):
        _validate_collector_choice(
            db_session, episode=foreign_episode, collector_profile_id=profile.id
        )
    db_session.add(
        EpisodeCollectorAttribution(
            root_source_episode_id=episode.id,
            collector_profile_id=profile.id,
            source="online_verified",
            match_status="matched",
        )
    )
    db_session.flush()
    assert (
        effective_collector_attribution(db_session, episode=episode)["effective_source"]
        == "online_verified"
    )
    with pytest.raises(CollectorAttributionImmutable):
        _validate_collector_choice(db_session, episode=episode, collector_profile_id=None)
    db_session.rollback()


def test_admin_edit_is_global_allows_duplicate_names_and_reactivation(
    client,
    admin_headers,
    db_session,
    context,
):
    workspaces, _, _, profile, _ = context
    other = create_collector_profile(
        db_session,
        workspace_id=workspaces[0].id,
        name="Another name",
        profile_key=None,
    )
    db_session.commit()
    for data in ({"name": other.name}, {"is_active": False}, {"is_active": True}):
        response = client.patch(
            f"/api/v1/collector-profiles/{profile.id}", headers=admin_headers, json=data
        )
        assert response.status_code == 200, response.text
        for workspace in workspaces[:2]:
            listed = client.get(
                "/api/v1/collector-profiles",
                headers=admin_headers,
                params={
                    "workspace_id": workspace.id,
                    "include_inactive": True,
                },
            )
            row = next(row for row in listed.json()["data"]["items"] if row["id"] == profile.id)
            for key, value in data.items():
                assert row[key] == value
    empty = client.patch(
        f"/api/v1/collector-profiles/{profile.id}", headers=admin_headers, json={"name": "   "}
    )
    assert empty.status_code == 422
    indexes = inspect(db_session.bind).get_indexes("personnel_profiles")
    assert not any(i["name"] == "uq_personnel_profiles_normalized_name" for i in indexes)
    assert not any(
        i.name == "uq_personnel_profiles_normalized_name"
        for i in PersonnelProfile.__table__.indexes
    )


def test_non_admin_and_anonymous_cannot_edit_global_profile(
    client,
    operator_headers,
    db_session,
    context,
    monkeypatch,
):
    workspaces, _, _, profile, _ = context
    actor = db_session.query(User).filter_by(email="operator@quicdata.com").one()
    db_session.add(WorkspaceMember(workspace_id=workspaces[0].id, user_id=actor.id))
    db_session.commit()
    with pytest.raises(PermissionError, match="only an admin"):
        require_collector_profile_admin(db_session, actor_id=actor.id)
    # Even a role granted workspace:write cannot mutate another workspace's identity.
    import data.routers.collector_profiles as router

    monkeypatch.setattr(router, "require_permission", lambda *_args: None)
    for data in ({"name": "Unauthorized rename"}, {"is_active": False}):
        denied = client.patch(
            f"/api/v1/collector-profiles/{profile.id}", headers=operator_headers, json=data
        )
        assert denied.status_code == 403, denied.text
    anonymous = client.patch(f"/api/v1/collector-profiles/{profile.id}", json={"is_active": False})
    assert anonymous.status_code == 401
    db_session.refresh(profile)
    assert profile.name == "Same name" and profile.is_active


def test_unknown_default_remains_usable(db_session, context):
    actor = db_session.query(User).filter_by(email="admin@quicdata.com").one()
    _, batches, label, _, _ = context
    session = create_import_session(
        db_session,
        batch_id=batches[0].id,
        import_type="chunked_upload",
        actor_id=actor.id,
        task_label_id=label.id,
        default_collector_profile_id=None,
        default_collection_device_id=None,
    )
    assert session.default_collector_profile_id is None
