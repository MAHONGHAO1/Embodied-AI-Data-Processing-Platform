"""Collection domain core: collection projects and collection tasks."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import relationship

from data.database import Base


class CollectionProject(Base):
    """Collection project. Historical query capability is retained after archiving, but adding new tasks, data packages, and uploads is prohibited."""

    __tablename__ = "collection_projects"
    __table_args__ = (
        CheckConstraint("status IN ('enabled', 'archived')", name="ck_collection_projects_status"),
        Index("ix_collection_projects_workspace_status", "workspace_id", "status"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    name = Column(String(128), nullable=False)
    description = Column(Text, nullable=False, default="")
    owner_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    status = Column(String(16), nullable=False, default="enabled")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    tasks = relationship("CollectionTask", back_populates="project")


Index(
    "uq_collection_projects_workspace_normalized_name",
    CollectionProject.workspace_id,
    func.lower(func.btrim(CollectionProject.name)),
    unique=True,
)


class CollectionTask(Base):
    """Collection task.

    Target duration is the sole task objective; the number of data packages is derived by dividing target duration by single-package duration,
    and no task-level quantity input is provided. The task does not carry region or personnel fields: personnel are specified during
    the package allocation phase, and ``created_by_user_id`` is for audit only.
    """

    __tablename__ = "collection_tasks"
    __table_args__ = (
        CheckConstraint("capture_mode = 'offline'", name="ck_collection_tasks_capture_mode"),
        CheckConstraint("target_duration_hours > 0", name="ck_collection_tasks_target_duration"),
        CheckConstraint(
            "default_package_duration_hours > 0",
            name="ck_collection_tasks_default_package_duration",
        ),
        Index("ix_collection_tasks_project_created", "collection_project_id", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    collection_project_id = Column(Integer, ForeignKey("collection_projects.id"), nullable=False)
    name = Column(String(128), nullable=False)
    description = Column(Text, nullable=False, default="")
    target_duration_hours = Column(Numeric(10, 2), nullable=False)
    default_package_duration_hours = Column(Numeric(10, 2), nullable=False, default=2)
    capture_mode = Column(String(16), nullable=False, default="offline")
    sop_text = Column(Text, nullable=False, default="")
    device_model_id = Column(Integer, ForeignKey("collection_device_models.id"), nullable=True)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    project = relationship("CollectionProject", back_populates="tasks")
    device_model = relationship("CollectionDeviceModel")
    labels = relationship("CollectionTaskLabel", back_populates="task")
    created_by_user = relationship("User", foreign_keys=[created_by_user_id])


class CollectionTaskLabel(Base):
    """Many-to-many relationship between tasks and business labels.

    All five label categories allow multiple selections and are handled uniformly through this table rather than five array columns,
    allowing direct lookup of referrers when a label is deactivated.
    """

    __tablename__ = "collection_task_labels"

    collection_task_id = Column(Integer, ForeignKey("collection_tasks.id"), primary_key=True)
    collection_label_id = Column(Integer, ForeignKey("collection_labels.id"), primary_key=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    task = relationship("CollectionTask", back_populates="labels")
    label = relationship("CollectionLabel")
