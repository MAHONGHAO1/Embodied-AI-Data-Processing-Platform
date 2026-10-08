"""Long lived API tokens bound to a user for external tools."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint

from data.database import Base, JsonDocument


class ApiToken(Base):
    """A named credential that inherits the bound user's role and workspace access."""

    __tablename__ = "api_tokens"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_api_tokens_user_name"),
        Index("ix_api_tokens_user_revoked", "user_id", "revoked_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    name = Column(String(64), nullable=False)
    key_id = Column(String(32), nullable=False, unique=True)
    secret_hash = Column(String(255), nullable=False)
    # Kept only for the rotation grace window so an external tool can switch
    # secrets without a hard cut over.
    previous_secret_hash = Column(String(255), nullable=True)
    scopes = Column(JsonDocument, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    rotated_at = Column(DateTime, nullable=True)
    last_used_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
