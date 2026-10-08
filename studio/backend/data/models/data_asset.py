"""Data asset: one data batch corresponds to at most one immutable asset snapshot."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from data.database import Base, JsonDocument


class DataAsset(Base):
    """Global data asset published when a batch ends (one-to-one with ``data_batch_id``).

    Not implicitly filtered by the current collection workspace; ``workspace_id`` is solely for origin tracking.
    Does not reuse the legacy episode-level ``datasets`` table.
    """

    __tablename__ = "data_assets"
    __table_args__ = (UniqueConstraint("data_batch_id", name="uq_data_assets_batch"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    data_batch_id = Column(Integer, ForeignKey("data_batches.id"), nullable=False)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    governed_valid_duration_hours = Column(Numeric(10, 2), nullable=False)
    stage_snapshot_json = Column(JsonDocument, nullable=False, default=dict)
    episode_ids_json = Column(JsonDocument, nullable=False, default=list)
    source_json = Column(JsonDocument, nullable=False, default=dict)
    source_snapshot_json = Column(JsonDocument, nullable=False, default=dict)
    source_snapshot_id = Column(String(64), nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    batch = relationship("DataBatch")
