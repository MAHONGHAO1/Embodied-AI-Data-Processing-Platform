"""Realtime session revocation helpers owned by the authentication boundary."""

from __future__ import annotations

from data.database import User


def revoke_realtime_session(user: User) -> int:
    """Advance a user's server-authoritative Socket.IO session generation."""
    previous_epoch = int(user.realtime_session_epoch or 0)
    user.realtime_session_epoch = previous_epoch + 1
    return previous_epoch
