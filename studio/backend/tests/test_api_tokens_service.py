"""Long lived API tokens: issuance, verification, rotation and revocation."""

from datetime import datetime, timedelta

import pytest

from data.models.api_token import ApiToken
from data.services.api_tokens import (
    TokenError,
    issue_token,
    revoke_token,
    rotate_token,
    verify_token,
)


@pytest.fixture
def admin_user(db_session):
    from data.database import User

    return db_session.query(User).filter(User.email == "admin@quicdata.com").one()


def test_issue_token_stores_only_the_hash(db_session, admin_user):
    issued = issue_token(
        db_session,
        user_id=admin_user.id,
        name="duance-运维机",
        expires_in_days=90,
        created_by=admin_user.id,
    )

    assert issued["secret"].startswith("qs_")
    row = db_session.query(ApiToken).filter_by(key_id=issued["key_id"]).one()
    secret_part = issued["secret"].split("_", 2)[2]
    assert secret_part not in row.secret_hash
    assert row.revoked_at is None
    assert row.expires_at is not None


def test_verify_token_returns_the_bound_principal(db_session, admin_user):
    issued = issue_token(
        db_session,
        user_id=admin_user.id,
        name="算法批-A",
        expires_in_days=30,
        created_by=admin_user.id,
    )

    principal = verify_token(db_session, issued["secret"])

    assert principal["sub"] == str(admin_user.id)
    assert principal["role"] == admin_user.role


def test_verify_token_rejects_unknown_revoked_and_expired(db_session, admin_user):
    with pytest.raises(TokenError) as unknown:
        verify_token(db_session, "qs_deadbeef_secret")
    assert unknown.value.code == "invalid_token"

    revoked = issue_token(
        db_session,
        user_id=admin_user.id,
        name="revoked-token",
        expires_in_days=30,
        created_by=admin_user.id,
    )
    revoke_token(db_session, token_id=revoked["id"])
    with pytest.raises(TokenError) as revoked_error:
        verify_token(db_session, revoked["secret"])
    assert revoked_error.value.code == "revoked"

    expired = issue_token(
        db_session,
        user_id=admin_user.id,
        name="expired-token",
        expires_in_days=1,
        created_by=admin_user.id,
    )
    row = db_session.query(ApiToken).filter_by(key_id=expired["key_id"]).one()
    row.expires_at = datetime.utcnow() - timedelta(minutes=1)
    db_session.flush()
    with pytest.raises(TokenError) as expired_error:
        verify_token(db_session, expired["secret"])
    assert expired_error.value.code == "expired"


def test_rotate_token_accepts_the_previous_secret_for_one_hour(db_session, admin_user):
    issued = issue_token(
        db_session,
        user_id=admin_user.id,
        name="rotate-me",
        expires_in_days=30,
        created_by=admin_user.id,
    )

    rotated = rotate_token(db_session, token_id=issued["id"])

    assert rotated["secret"] != issued["secret"]
    assert verify_token(db_session, rotated["secret"])["sub"] == str(admin_user.id)
    assert verify_token(db_session, issued["secret"])["sub"] == str(admin_user.id)

    row = db_session.query(ApiToken).filter_by(key_id=issued["key_id"]).one()
    row.rotated_at = datetime.utcnow() - timedelta(hours=2)
    db_session.flush()
    with pytest.raises(TokenError) as expired_rotation:
        verify_token(db_session, issued["secret"])
    assert expired_rotation.value.code in {"invalid_token", "revoked"}
