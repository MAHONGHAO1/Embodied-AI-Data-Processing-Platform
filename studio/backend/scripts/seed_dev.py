#!/usr/bin/env python3
"""Create explicit development-only demo users and workspace membership."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data.bootstrap  # noqa: F401
from data.config import DEFAULT_USERS, settings
from data.database import (
    CollectionDevice,
    PersonnelProfile,
    SessionLocal,
    User,
    Workspace,
    WorkspaceMember,
)
from data.models.collection_core import CollectionProject, CollectionTask
from data.models.data_package import DataPackage
from data.services.collection_device_models import ensure_default_device_models
from data.utils.helpers import hash_password


def seed_development_data() -> None:
    if settings.is_production:
        raise RuntimeError("development seed is disabled in production")

    db = SessionLocal()
    try:
        existing_users = {user.email for user in db.query(User.email).all()}
        for user in DEFAULT_USERS:
            if user["email"] not in existing_users:
                db.add(
                    User(
                        email=user["email"],
                        password_hash=hash_password(user["password"]),
                        role=user["role"],
                    )
                )

        workspace = (
            db.query(Workspace)
            .filter(Workspace.name == "默认工作空间", Workspace.creator == "system")
            .one_or_none()
        )
        if workspace is None and db.query(Workspace).count() == 0:
            workspace = Workspace(
                name="默认工作空间", description="QuicData MVP 默认空间", creator="system"
            )
            db.add(workspace)
            db.flush()

        if workspace is not None:
            db.flush()
            demo_users = (
                db.query(User)
                .filter(User.email.in_([user["email"] for user in DEFAULT_USERS]))
                .all()
            )
            existing_member_ids = {
                user_id
                for (user_id,) in db.query(WorkspaceMember.user_id)
                .filter(WorkspaceMember.workspace_id == workspace.id)
                .all()
            }
            db.add_all(
                WorkspaceMember(workspace_id=workspace.id, user_id=user.id)
                for user in demo_users
                if user.id not in existing_member_ids
            )
        ensure_default_device_models(db)

        from data.models.collection_config import CollectionLabel

        if workspace is not None:
            default_project = (
                db.query(CollectionProject)
                .filter(
                    CollectionProject.workspace_id == workspace.id,
                    CollectionProject.name == "默认采集项目",
                )
                .one_or_none()
            )
            if default_project is None:
                admin_user = db.query(User).filter(User.email == "admin@quicdata.com").one_or_none()
                db.add(
                    CollectionProject(
                        workspace_id=workspace.id,
                        name="默认采集项目",
                        description="QuicStudio 默认具身智能数采项目",
                        owner_user_id=admin_user.id if admin_user else None,
                        status="enabled",
                    )
                )

        if not settings.test_mode:
            default_labels = [
                ("scene", "家居 · 厨房", "厨房场景操作采集"),
                ("scene", "家居 · 客厅", "客厅桌面整理采集"),
                ("scene", "商超 · 货架", "商超货架商品拣选"),
                ("scene", "仓储 · 分拣台", "仓储流水线工业分拣"),
                ("purpose", "正式任务", "生产级基线任务"),
                ("purpose", "测试任务", "验证与标定任务"),
                ("training", "预训练", "通用预训练数据集"),
                ("training", "后训练", "下游微调与强化学习对齐"),
                ("modality", "双目RGBD", "双目立体深度视觉"),
                ("modality", "单目RGB", "单目全局视角"),
                ("modality", "腕部相机", "末端夹爪手腕相机"),
            ]
            existing_label_names = {
                (lbl[0], lbl[1])
                for lbl in db.query(CollectionLabel.category, CollectionLabel.name).all()
            }
            for category, name, desc in default_labels:
                if (category, name) not in existing_label_names:
                    db.add(
                        CollectionLabel(
                            category=category, name=name, description=desc, is_active=True
                        )
                    )

        if workspace is not None:
            _seed_collection_overview_demo(db, workspace)

        db.commit()
    finally:
        db.close()


def _seed_collection_overview_demo(db, workspace: Workspace) -> None:
    """Create deterministic package facts so the local collection overview is populated."""
    project = (
        db.query(CollectionProject)
        .filter(
            CollectionProject.workspace_id == workspace.id,
            CollectionProject.name == "采集概览演示",
        )
        .one_or_none()
    )
    if project is None:
        project = CollectionProject(
            workspace_id=workspace.id,
            name="采集概览演示",
            description="本地开发环境采集概览演示数据",
            status="enabled",
        )
        db.add(project)
        db.flush()

    task = (
        db.query(CollectionTask)
        .filter(
            CollectionTask.workspace_id == workspace.id,
            CollectionTask.collection_project_id == project.id,
            CollectionTask.name == "采集概览演示任务",
        )
        .one_or_none()
    )
    if task is None:
        task = CollectionTask(
            workspace_id=workspace.id,
            collection_project_id=project.id,
            name="采集概览演示任务",
            description="用于本地预览采集概览卡片、趋势和状态分布",
            target_duration_hours=Decimal("13.00"),
            default_package_duration_hours=Decimal("3.00"),
            capture_mode="offline",
        )
        db.add(task)
        db.flush()
    else:
        task.target_duration_hours = Decimal("13.00")

    collector_id = db.query(User.id).filter(User.email == "admin@quicdata.com").scalar()
    profile_id = db.query(PersonnelProfile.id).order_by(PersonnelProfile.id).first()
    device_id = (
        db.query(CollectionDevice.id)
        .filter(CollectionDevice.workspace_id == workspace.id)
        .order_by(CollectionDevice.id)
        .first()
    )
    profile_id = profile_id[0] if profile_id else None
    device_id = device_id[0] if device_id else None
    if profile_id is None:
        return

    now = datetime.utcnow().replace(microsecond=0)
    packages = [
        {
            "uid": "seed-overview-pending",
            "status": "pending_assignment",
            "target": "2.00",
            "days": 0,
            "captured": None,
            "valid": None,
            "size": None,
        },
        {
            "uid": "seed-overview-assigned",
            "status": "assigned",
            "target": "4.00",
            "days": 0,
            "captured": "3600.000",
            "valid": "3000.000",
            "size": 8_000_000_000,
        },
        {
            "uid": "seed-overview-review",
            "status": "pending_intake_review",
            "target": "3.00",
            "days": 7,
            "captured": "7200.000",
            "valid": None,
            "size": 10_000_000_000,
        },
        {
            "uid": "seed-overview-ingested",
            "status": "ingested",
            "target": "3.00",
            "days": 14,
            "captured": "5400.000",
            "valid": "4800.000",
            "size": 6_000_000_000,
        },
        {
            "uid": "seed-overview-voided",
            "status": "voided",
            "target": "1.00",
            "days": 21,
            "captured": None,
            "valid": None,
            "size": None,
        },
    ]
    existing = {
        row.package_uid: row
        for row in db.query(DataPackage)
        .filter(DataPackage.package_uid.in_([p["uid"] for p in packages]))
        .all()
    }
    for item in packages:
        started = now - timedelta(days=item["days"])
        captured = Decimal(item["captured"]) if item["captured"] is not None else None
        valid = Decimal(item["valid"]) if item["valid"] is not None else None
        assigned = item["status"] != "pending_assignment"
        row = existing.get(item["uid"])
        if row is not None:
            # Keep reruns deterministic while preserving any package review relations.
            row.created_at = started
            row.updated_at = started
            row.assigned_at = started if assigned else None
            row.captured_started_at = started if captured is not None else None
            row.upload_completed_at = started + timedelta(hours=1) if captured is not None else None
            continue
        db.add(
            DataPackage(
                package_uid=item["uid"],
                workspace_id=workspace.id,
                collection_project_id=project.id,
                collection_task_id=task.id,
                status=item["status"],
                target_duration_hours=Decimal(item["target"]),
                captured_duration_hours=(
                    captured / Decimal("3600") if captured is not None else None
                ),
                intake_valid_duration_hours=(
                    valid / Decimal("3600") if valid is not None else None
                ),
                governed_valid_duration_hours=(
                    valid / Decimal("3600") if valid is not None else None
                ),
                captured_started_at=(started if captured is not None else None),
                captured_duration_s=captured,
                captured_size_bytes=item["size"],
                intake_valid_duration_s=valid,
                intake_valid_size_bytes=(
                    int(item["size"] * 0.8)
                    if item["size"] is not None and valid is not None
                    else None
                ),
                responsible_collector_id=(profile_id if assigned else None),
                operator_collector_id=(profile_id if assigned else None),
                collection_device_id=(device_id if assigned else None),
                assigned_at=(started if assigned else None),
                upload_completed_at=(
                    started + timedelta(hours=1) if captured is not None else None
                ),
                created_at=started,
                updated_at=started,
                created_by_user_id=collector_id,
            )
        )


def main() -> int:
    seed_development_data()
    print("development seed completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
