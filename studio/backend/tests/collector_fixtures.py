"""Create real global collector identities and explicit workspace membership."""

from data.services.collector_profiles import create_collector_profile


def make_collector(db_session, *, workspace_id, name, is_active=True):
    profile = create_collector_profile(
        db_session,
        workspace_id=workspace_id,
        name=name,
        profile_key=None,
    )
    profile.is_active = is_active
    db_session.flush()
    return profile
