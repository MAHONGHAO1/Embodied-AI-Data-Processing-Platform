# 数据包接入与入库审核 Implementation Plan

> 2026-09-18 协作基线说明：下文是 2026-09-17 的历史实施及自检记录，不代表最新限制。当前代码已支持多来源 OSS multipart，并在入库审核提交时强制检查完整性与 Preview；此前的「仅单来源」「缺 Preview 不失败」不能作为当前验收标准。本地分片仍是待退场兼容链路。最新接口见 [采集上传接口](../../COLLECTION_UPLOAD_API.md)，开发交接见 [Data/Train 协作基线](../../DEVELOPMENT_HANDOFF.md)，剩余工作见 [三桶与逻辑资产计划](2026-09-18-quicstudio-object-storage-logical-assets.md)。

> 2026-09-17 writing-plans 自检（见文末「Self-Review」）：规格覆盖、占位符、类型一致性已核对；以 [当前接口约定](../../COLLECTION_UPLOAD_API.md) 和 [检查记录](../../PLAN_1_3_REVIEW.md) 为执行真源。历史 Task 步骤保留供追溯，复选框不是进度来源。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Plan ② 采集管理 API 之上，实现离线数据包接入（`upload-sessions`）与管理员入库审核，使已分配包经 SDK/分片/OSS 上传、解析后进入 `pending_intake_review`，再经逐包或批量审核得到不可变的 `intake_valid_duration_hours`。

**Architecture:** 新建采集域 `CollectionUploadSession`（对外资源名 `upload-sessions`），通过关联表绑定多个已预生成的 `DataPackage`；字节传输与 QRDF 校验复用既有 `import_intake` / `duance_imports` / `import_parser` 的底层能力，但不挂靠遗留 `Batch`/`TaskSet`。SDK **逐文件**分片（服务端下发 `source_id`），**不接收 ZIP**。解析成功后自动进入待入库审核；入库审核写 `PackageIntakeReview`、更新 Episode `validity_status`，整包拒绝作废且不自动补包。不实现建批、标注、资产（Plan ④）。

**Tech Stack:** Python 3.11、FastAPI、SQLAlchemy 2.0（`Column()`）、Pydantic v2、Alembic、pytest、既有 `success()` / `emit_audit_event` / `require_collection_admin` / Celery `ingest` 队列

**Spec:** `docs/superpowers/specs/2026-09-14-collection-studio-init-design.md`（§3、§6 入库有效时长、§9.1–9.2 中与 upload-sessions / 入库审核相关的边界）

**前置：** Plan ①② 已完成。迁移头为 `0006_data_batch`（落地后为 `0007_collection_upload_sessions`）。采集管理 API 在 `backend/data/services/collection_*.py`。

**本计划是 4 份顺序计划的第 ③份。** ① 迁移+模型 ✓ · ② 采集管理 API ✓ · ③ 接入与入库审核（本文件）· ④ 治理、标注审核、资产与数据集。

## Global Constraints

- 上传会话**不创建**业务数据包；只关联任务创建时预生成的包，按不可变 `package_uid` 归属。
- 一个上传会话可关联多个数据包；一个数据包同一时刻最多归属一个**活跃**上传会话。
- 归档项目禁止新建上传会话；已归档项目保留历史查询。
- 主路径状态：`assigned → pending_upload → uploading → parsing → ingested → pending_intake_review → intake_approved`；终止：`parse_failed`（可重新上传）、入库拒绝 → `voided`。
- 上传完成只做接入/解析/基础可用性，**不**自动启动完整性检查、QC、合规或标注。
- 入库审核仅管理员；强制环节；支持逐包审核与列表批量通过（`is_bulk` 区分审计）。
- 入库有效时长在审核完成时写入且之后不变；整包拒绝不自动补包。
- QRDF `privacy_sensitive` 原样透传并在包详情可展示；本期不脱敏。
- 浏览器/SDK 响应不得暴露存储桶、对象键、原始 URI、provider upload id、AK/SK。
- 多 Episode/多包：**不是 ZIP**；声明后由服务端分配 `source_id`，客户端对每个 MCAP 独立分片上传。
- `authorized_import` 仅模型枚举预留；创建会话时拒绝未实现模式。OSS 直传本期仅保证**单来源**；多 MCAP 走 SDK 分片。
- 测试命令（仓库根目录）：

```bash
TEST_DATABASE_URL=postgresql+psycopg://quicdata:quicdata@127.0.0.1:5432/quicdata_test_studio \
  .venv/bin/python -m pytest backend/tests/<file> -q
```

## 架构决策

| 决策 | 取值 | 理由 |
|---|---|---|
| 对外资源名 | `/api/v1/upload-sessions` | 规格 §9.2 资源划分 |
| 会话模型 | 新建 `collection_upload_sessions`，**不**把遗留 `ImportSession.batch_id` 改成可空硬挂采集 | 避免破坏 Batch 导入栈；采集会话边界清晰 |
| 字节传输 | 复用 `import_intake` 分片/OSS 限额与签名；staging=`storage_root/collection-uploads/{session_id}`；多文件用 `source_id` 分目录，**禁止 ZIP 解包** | 对齐 Duance SDK（仅 metadata+MCAP）；隔离遗留 `imports/` |
| Episode 归属 | `data_package_id` 必填（采集路径）；`task_set_id`/`batch_id` 改为可空，并加 CHECK：采集路径与遗留路径二选一 | 规格层级是 Package→Episode，不再伪造 Batch |
| 解析触发 | Celery job kind `collection_upload_parse`，队列 `ingest`；测试可同步调用 service | 与现有 ingest worker 一致 |
| 设备 SN | QRDF `devices[].serial_number` 在工作空间内匹配 `CollectionDevice`；未匹配记 `EpisodeDeviceAttribution.match_status=unmatched`，**不**自动建实例 | 与既有 `capture_provenance` 一致；型号已在任务上绑定 |
| 遗留 Duance/Import API | 保持可用，Plan ③ 不删除；采集客户端走新 `upload-sessions` | 平行演进，Plan ④ 后再谈废弃 |
| 已授权来源导入 | 规格 §3.2 有要求；本计划 **API 拒绝** `authorized_import`，不实现 OSS 扫描入口 | 避免半成品；多文件路径已由 SDK 分片覆盖主场景 |

## File Structure

```text
backend/data/
  models/
    collection_upload.py          ← CollectionUploadSession + 关联表
    data_package.py               ← 可选：补充 capture_mode / qrdf_facts_json 字段
  services/
    collection_upload_sessions.py ← 会话 CRUD、绑包、状态机
    collection_upload_intake.py   ← 分片/OSS 写入、完成上传
    collection_upload_parse.py    ← 解析适配、写 Episode、推进包状态
    collection_intake_review.py   ← 逐包/批量入库审核
    collection_packages.py        ← 追加详情（含 episodes）、parse_failed 重传入口联动
  routers/
    collection_upload_sessions.py
    collection_intake_review.py   ← 或挂在 collection_packages 下的 review 子路径
    collection_packages.py        ← 追加 GET detail
  main.py
backend/alembic/versions/
  0007_collection_upload_sessions.py
backend/tests/
  test_collection_upload_session_models.py
  test_collection_upload_sessions_api.py
  test_collection_upload_intake_api.py
  test_collection_upload_parse.py
  test_collection_intake_review_api.py
  test_collection_upload_intake_smoke.py
```

路由前缀（均在 `settings.api_prefix` = `/api/v1` 下）：

| 资源 | 前缀 / 路径 |
|---|---|
| 上传会话 | `/upload-sessions` |
| 数据包详情 | `GET /data-packages/{id}`（扩展） |
| 入库审核 | `POST /data-packages/{id}/intake-review` |
| 批量通过 | `POST /data-packages/intake-review/bulk-approve` |

---

### Task 1: 采集上传会话模型与 Episode 归属迁移

**Files:**
- Create: `backend/data/models/collection_upload.py`
- Create: `backend/alembic/versions/0007_collection_upload_sessions.py`
- Modify: `backend/data/models/__init__.py`（导出新模型）
- Modify: `backend/data/models/data_package.py`（增加 `capture_mode`、`qrdf_facts_json`、`parse_error_code`、`parse_error_message`）
- Modify: `backend/data/database.py`（`Episode.task_set_id`/`batch_id` 可空 + CHECK；为 `data_package_id` 加索引）
- Test: `backend/tests/test_collection_upload_session_models.py`

**Interfaces:**
- Produces: `CollectionUploadSession`、`CollectionUploadSessionPackage`；Episode 采集路径可不依赖 Batch/TaskSet
- Consumes: 既有 `DataPackage`、`Workspace`、`CollectionProject`

会话状态字面量：

```python
COLLECTION_UPLOAD_SESSION_STATUSES = (
    "init",
    "uploading",
    "uploaded",
    "parsing",
    "succeeded",
    "failed",
    "cancelled",
)
```

关联表唯一约束：`(upload_session_id, data_package_id)`；另建部分唯一/服务层规则保证「活跃会话下同一 package 不被两个会话占用」。

- [x] **Step 1: 写失败的模型测试**

```python
"""采集上传会话模型与 Episode 采集归属。"""

from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from data.database import Episode, SessionLocal, Workspace
from data.models.collection_core import CollectionProject, CollectionTask
from data.models.collection_upload import CollectionUploadSession, CollectionUploadSessionPackage
from data.models.data_package import DataPackage


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _scope(db):
    from data.database import PersonnelProfile

    ws = Workspace(name=f"ws-up-{uuid4().hex}")
    db.add(ws)
    db.commit()
    project = CollectionProject(workspace_id=ws.id, name=f"P-{ws.id}")
    db.add(project)
    db.commit()
    task = CollectionTask(
        workspace_id=ws.id,
        collection_project_id=project.id,
        name=f"T-{ws.id}",
        target_duration_hours=2,
    )
    db.add(task)
    db.commit()
    owner = PersonnelProfile(name="o", profile_key=f"o-{uuid4().hex[:8]}")
    operator = PersonnelProfile(name="p", profile_key=f"p-{uuid4().hex[:8]}")
    db.add_all([owner, operator])
    db.commit()
    return ws, project, task, owner, operator


def test_upload_session_can_link_multiple_packages(db):
    ws, project, task, owner, operator = _scope(db)
    packages = []
    for _ in range(2):
        pkg = DataPackage(
            workspace_id=ws.id,
            collection_project_id=project.id,
            collection_task_id=task.id,
            package_uid=f"pkg_{uuid4().hex}",
            target_duration_hours=1,
            status="assigned",
            responsible_collector_id=owner.id,
            operator_collector_id=operator.id,
        )
        db.add(pkg)
        packages.append(pkg)
    db.commit()
    session = CollectionUploadSession(
        id=str(uuid4()),
        workspace_id=ws.id,
        collection_project_id=project.id,
        status="init",
        upload_mode="duance_sdk",
        created_by_user_id=None,
    )
    db.add(session)
    db.flush()
    for pkg in packages:
        db.add(
            CollectionUploadSessionPackage(
                upload_session_id=session.id,
                data_package_id=pkg.id,
                package_uid=pkg.package_uid,
            )
        )
    db.commit()
    assert len(session.package_links) == 2


def test_collection_episode_does_not_require_legacy_batch(db):
    ws, project, task, owner, operator = _scope(db)
    pkg = DataPackage(
        workspace_id=ws.id,
        collection_project_id=project.id,
        collection_task_id=task.id,
        package_uid=f"pkg_{uuid4().hex}",
        target_duration_hours=1,
        status="assigned",
        responsible_collector_id=owner.id,
        operator_collector_id=operator.id,
    )
    db.add(pkg)
    db.commit()
    episode = Episode(
        episode_uid=f"ep-{uuid4().hex}",
        workspace_id=ws.id,
        task_set_id=None,
        batch_id=None,
        data_package_id=pkg.id,
        kind="source",
        modality="rgb",
        validity_status="valid",
    )
    db.add(episode)
    db.commit()
    assert episode.data_package_id == pkg.id
    assert episode.batch_id is None
```

- [x] **Step 2: 运行确认失败**

```bash
TEST_DATABASE_URL=postgresql+psycopg://quicdata:quicdata@127.0.0.1:5432/quicdata_test_studio \
  .venv/bin/python -m pytest backend/tests/test_collection_upload_session_models.py -q
```

Expected: FAIL（模型/迁移不存在，或 Episode 非空约束仍在）。

- [x] **Step 3: 实现模型**

`backend/data/models/collection_upload.py`：

```python
"""采集上传会话：技术会话，不创建业务数据包。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from data.database import Base, JsonDocument

COLLECTION_UPLOAD_SESSION_STATUSES = (
    "init",
    "uploading",
    "uploaded",
    "parsing",
    "succeeded",
    "failed",
    "cancelled",
)
COLLECTION_UPLOAD_MODES = ("duance_sdk", "chunked", "oss_multipart", "authorized_import")

_STATUS_SQL = ", ".join(f"'{s}'" for s in COLLECTION_UPLOAD_SESSION_STATUSES)
_MODE_SQL = ", ".join(f"'{s}'" for s in COLLECTION_UPLOAD_MODES)


class CollectionUploadSession(Base):
    __tablename__ = "collection_upload_sessions"
    __table_args__ = (
        CheckConstraint(f"status IN ({_STATUS_SQL})", name="ck_collection_upload_sessions_status"),
        CheckConstraint(f"upload_mode IN ({_MODE_SQL})", name="ck_collection_upload_sessions_mode"),
        Index("ix_collection_upload_sessions_workspace_created", "workspace_id", "created_at"),
        Index("ix_collection_upload_sessions_project_status", "collection_project_id", "status"),
    )

    id = Column(String(36), primary_key=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    collection_project_id = Column(Integer, ForeignKey("collection_projects.id"), nullable=False)
    status = Column(String(32), nullable=False, default="init")
    upload_mode = Column(String(32), nullable=False)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    result_json = Column(JsonDocument, nullable=False, default=dict)
    error_code = Column(String(64), nullable=False, default="")
    error_message = Column(String(512), nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    package_links = relationship(
        "CollectionUploadSessionPackage",
        back_populates="upload_session",
        cascade="all, delete-orphan",
    )


class CollectionUploadSessionPackage(Base):
    __tablename__ = "collection_upload_session_packages"
    __table_args__ = (
        UniqueConstraint(
            "upload_session_id",
            "data_package_id",
            name="uq_collection_upload_session_packages_pair",
        ),
        Index("ix_collection_upload_session_packages_package", "data_package_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    upload_session_id = Column(
        String(36), ForeignKey("collection_upload_sessions.id"), nullable=False
    )
    data_package_id = Column(Integer, ForeignKey("data_packages.id"), nullable=False)
    package_uid = Column(String(64), nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    upload_session = relationship("CollectionUploadSession", back_populates="package_links")
    package = relationship("DataPackage")
```

在 `DataPackage` 上追加（同一文件或本任务内修改）：

```python
capture_mode = Column(String(32), nullable=True)  # ego/umi/teleop/simulation/...
qrdf_facts_json = Column(JsonDocument, nullable=False, default=dict)
parse_error_code = Column(String(64), nullable=False, default="")
parse_error_message = Column(String(512), nullable=False, default="")
```

`Episode` CHECK（迁移 SQL 表达）：

```sql
CHECK (
  (data_package_id IS NOT NULL AND task_set_id IS NULL AND batch_id IS NULL)
  OR
  (data_package_id IS NULL AND task_set_id IS NOT NULL AND batch_id IS NOT NULL)
)
```

名：`ck_episodes_collection_or_legacy_scope`。并：`CREATE INDEX ix_episodes_data_package_id ON episodes (data_package_id)`。

- [x] **Step 4: 写迁移 `0007_collection_upload_sessions.py`**

- `down_revision = "0006_data_batch"`
- 建两张新表
- `data_packages` 加四列
- `episodes.task_set_id` / `batch_id` 改为 nullable；加 CHECK 与索引
- 注意：现有遗留行全部是 legacy 路径（`data_package_id IS NULL`），CHECK 必须兼容

- [x] **Step 5: 测试通过并提交**

```bash
TEST_DATABASE_URL=postgresql+psycopg://quicdata:quicdata@127.0.0.1:5432/quicdata_test_studio \
  .venv/bin/python -m pytest backend/tests/test_collection_upload_session_models.py -q
```

```bash
git add backend/data/models/collection_upload.py backend/data/models/data_package.py \
  backend/data/database.py backend/data/models/__init__.py \
  backend/alembic/versions/0007_collection_upload_sessions.py \
  backend/tests/test_collection_upload_session_models.py
git commit -m "feat: add collection upload session models and episode package scope"
```

---

### Task 2: 上传会话创建、绑包与状态校验 API

**Files:**
- Create: `backend/data/services/collection_upload_sessions.py`
- Create: `backend/data/routers/collection_upload_sessions.py`
- Modify: `backend/data/main.py`
- Modify: `backend/data/security/audit.py`（注册 `collection.upload.*` 事件）
- Test: `backend/tests/test_collection_upload_sessions_api.py`

**Interfaces:**
- Consumes: `require_collection_admin`、`require_collection_workspace`、`DataPackage`、`CollectionProject`
- Produces:
  - `create_upload_session(db, *, workspace_id, collection_project_id, package_uids: list[str], upload_mode, actor_id) -> CollectionUploadSession`
  - `get_upload_session` / `list_upload_sessions` / `cancel_upload_session`
  - 创建成功：关联包从 `assigned` 或 `parse_failed` → `pending_upload`（条件 UPDATE）

**HTTP:**
- `POST /api/v1/upload-sessions`
  body: `{workspace_id, collection_project_id, package_uids: string[], upload_mode}`
- `GET /api/v1/upload-sessions?workspace_id=&collection_project_id=&status=`
- `GET /api/v1/upload-sessions/{id}?workspace_id=`
- `POST /api/v1/upload-sessions/{id}/cancel` `{workspace_id}`

校验规则：
- 管理员 + 工作空间成员
- 项目 `enabled`，否则 409 detail 含 `archived`
- 每个 `package_uid` 必须存在、属于该项目、状态为 `assigned` 或 `parse_failed`
- 包不能已在其他非终态会话中（`init|uploading|uploaded|parsing`）
- 空 `package_uids` → 422
- 取消：会话 → `cancelled`；仍为 `pending_upload` 的包回到 `assigned`（`parse_failed` 重传取消则回到 `parse_failed`）

- [x] **Step 1: 写失败的 API 测试**

```python
"""采集上传会话：创建、绑包与取消。"""

from uuid import uuid4

from data.models.data_package import DataPackage
from data.services.collection_packages import assign_data_package
from data.services.collection_tasks import create_collection_task
from tests.collection_api_fixtures import (
    make_collector,
    make_device_model,
    make_project,
    make_workspace,
)


def _assigned_package(db_session, workspace, project, *, hours="2.00"):
    from decimal import Decimal

    device = make_device_model(db_session)
    task = create_collection_task(
        db_session,
        workspace_id=workspace.id,
        collection_project_id=project.id,
        name=f"t-{uuid4().hex[:8]}",
        target_duration_hours=Decimal(hours),
        default_package_duration_hours=Decimal(hours),
        device_model_id=device.id,
        label_ids=[],
        created_by_user_id=None,
    )
    db_session.commit()
    package = db_session.query(DataPackage).filter(DataPackage.collection_task_id == task.id).one()
    owner = make_collector(db_session, workspace, name="Owner")
    operator = make_collector(db_session, workspace, name="Op")
    assign_data_package(
        db_session,
        workspace_id=workspace.id,
        data_package_id=package.id,
        responsible_collector_id=owner.id,
        operator_collector_id=operator.id,
    )
    db_session.commit()
    db_session.refresh(package)
    return package


def test_create_upload_session_binds_assigned_packages(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package = _assigned_package(db_session, workspace, project)
    response = client.post(
        "/api/v1/upload-sessions",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "collection_project_id": project.id,
            "package_uids": [package.package_uid],
            "upload_mode": "duance_sdk",
        },
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "init"
    assert package.package_uid in {p["package_uid"] for p in data["packages"]}
    db_session.refresh(package)
    assert package.status == "pending_upload"


def test_create_upload_session_rejects_pending_assignment(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    from decimal import Decimal
    from data.services.collection_tasks import create_collection_task

    device = make_device_model(db_session)
    task = create_collection_task(
        db_session,
        workspace_id=workspace.id,
        collection_project_id=project.id,
        name="unassigned",
        target_duration_hours=Decimal("2.00"),
        default_package_duration_hours=Decimal("2.00"),
        device_model_id=device.id,
        label_ids=[],
        created_by_user_id=None,
    )
    db_session.commit()
    package = db_session.query(DataPackage).filter(DataPackage.collection_task_id == task.id).one()
    response = client.post(
        "/api/v1/upload-sessions",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "collection_project_id": project.id,
            "package_uids": [package.package_uid],
            "upload_mode": "chunked",
        },
    )
    assert response.status_code == 409
    assert "package_not_uploadable" in response.json()["detail"]


def test_create_upload_session_rejects_unknown_package_uid(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    response = client.post(
        "/api/v1/upload-sessions",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "collection_project_id": project.id,
            "package_uids": [f"pkg_{uuid4().hex}"],
            "upload_mode": "duance_sdk",
        },
    )
    assert response.status_code in {404, 409}
```

- [x] **Step 2: 运行确认失败（404）**

```bash
TEST_DATABASE_URL=postgresql+psycopg://quicdata:quicdata@127.0.0.1:5432/quicdata_test_studio \
  .venv/bin/python -m pytest backend/tests/test_collection_upload_sessions_api.py -q
```

- [x] **Step 3: 实现 service**

关键逻辑要点：

```python
UPLOADABLE_PACKAGE_STATUSES = frozenset({"assigned", "parse_failed"})
ACTIVE_SESSION_STATUSES = frozenset({"init", "uploading", "uploaded", "parsing"})


def create_upload_session(...):
    project = lock_enabled_project(...)  # FOR UPDATE，archived → ArchivedCollectionProjectError
    packages = load_packages_by_uids(...)
    for package in packages:
        if package.status not in UPLOADABLE_PACKAGE_STATUSES:
            raise PackageStateConflictError("package_not_uploadable")
        if package.collection_project_id != project.id:
            raise LookupError("package_project_mismatch")
        if _has_active_session(db, package.id):
            raise PackageStateConflictError("package_session_busy")
    session = CollectionUploadSession(id=str(uuid4()), ...)
    db.add(session)
    db.flush()
    for package in packages:
        prior = package.status
        updated = db.execute(
            update(DataPackage)
            .where(
                DataPackage.id == package.id,
                DataPackage.status.in_(tuple(UPLOADABLE_PACKAGE_STATUSES)),
            )
            .values(status="pending_upload", updated_at=datetime.utcnow())
        )
        if updated.rowcount != 1:
            raise PackageStateConflictError("package_not_uploadable")
        db.add(CollectionUploadSessionPackage(
            upload_session_id=session.id,
            data_package_id=package.id,
            package_uid=package.package_uid,
        ))
        # stash prior status in result_json["package_prior_status"][uid] for cancel restore
    ...
```

Router 将 `ArchivedCollectionProjectError` → 409 `archived`；`PackageStateConflictError` → 409；`LookupError` → 404；`PermissionError` → 403。

审计：`collection.upload.create` / `collection.upload.cancel`。

- [x] **Step 4: 注册路由、测试通过、提交**

```bash
git add backend/data/services/collection_upload_sessions.py \
  backend/data/routers/collection_upload_sessions.py backend/data/main.py \
  backend/data/security/audit.py backend/tests/test_collection_upload_sessions_api.py
git commit -m "feat: add collection upload session create and cancel APIs"
```

---

### Task 3: Duance SDK 清单绑定与分片/OSS 上传入口

**Files:**
- Create: `backend/data/services/collection_upload_intake.py`
- Modify: `backend/data/routers/collection_upload_sessions.py`
- Modify: `backend/data/services/collection_upload_sessions.py`（状态 → `uploading`）
- Test: `backend/tests/test_collection_upload_intake_api.py`

**Interfaces:**
- Consumes: Task 2 会话；`duance_imports.build_duance_import_manifest` 思路；`import_intake` 限额常量
- Produces:
  - `declare_package_sources(session, declarations)` — 每个包声明一个或多个 Episode 源（metadata + data file 摘要）
  - `start_chunked_upload` / `write_chunk` / `complete_chunked_upload`
  - `start_oss_multipart` / `sign_part` / `complete_oss_multipart`
  - 完成上传后会话 → `uploaded`，关联包 → `uploading`（开始落盘校验）再进入解析调度前保持可观测

**HTTP（挂在会话下；真源见 `docs/COLLECTION_UPLOAD_API.md`）：**
- `POST /upload-sessions/{id}/declarations`
  `{workspace_id, items: [{package_uid, source, metadata_text, data_file}]}`
  响应含 `sources: [{source_id, package_uid, episode_id}, ...]`（`source_id` 服务端生成）
- `POST /upload-sessions/{id}/chunked/init` `{workspace_id, source_id?, total_chunks}`
  多来源必须带 `source_id`；仅单来源时可省略（兼容旧单文件布局）
- `PUT /upload-sessions/{id}/chunked/{chunk_index}?source_id=` — 原始 MCAP 字节，限制 `MAX_IMPORT_CHUNK_BYTES`
- `POST /upload-sessions/{id}/chunked/complete` `{workspace_id}` — 按来源核对 size/SHA-256，**不是 ZIP**
- `POST /upload-sessions/{id}/oss/init|sign-part|complete` — **仅单来源会话**；多 MCAP 走 SDK 分片

规则：
- 声明中的 `package_uid` 必须属于该会话
- 跨项目/未知 uid / 未知 `source_id` → 明确 404/409，禁止静默归属
- 响应只返回短期签名 URL / 会话进度 / `source_id`，不返回 bucket/key/staging 路径
- staging：`settings.storage_root / "collection-uploads" / session.id`（多文件在 `file-{source_id}/` 下），路径必须 `resolve()` 落在 storage_root 内
- 创建会话 `upload_mode` 仅接受已实现模式；`authorized_import` → 422/409 拒绝

- [x] **Step 1: 写失败测试（声明绑定 + 拒绝错 uid）**

先把 Task 2 的 `_assigned_package` 抽到 `backend/tests/collection_api_fixtures.py`，命名 `make_assigned_package(db, workspace, project)`。

```python
"""采集上传字节入口：声明绑定与分片完成。"""

import json
from uuid import uuid4

from tests.collection_api_fixtures import make_assigned_package, make_project, make_workspace


def _minimal_duance_payload(package_uid: str) -> dict:
    metadata = {
        "qrdf_version": "0.2.0",
        "episode_id": f"ep-{uuid4().hex[:12]}",
        "timing": {"start_timestamp_ns": "0", "end_timestamp_ns": "3600000000000"},
        "capture": {"mode": "ego", "app_version": "test-1"},
        "privacy_sensitive": False,
    }
    metadata_text = json.dumps(metadata, ensure_ascii=False)
    return {
        "package_uid": package_uid,
        "source": {
            "episode_id": metadata["episode_id"],
            "start_ns": "0",
            "end_ns": "3600000000000",
            "metadata_sha256": "0" * 64,
            "data_mcap_sha256": "1" * 64,
        },
        "metadata_text": metadata_text,
        "data_file": {
            "path": "data.mcap",
            "size_bytes": 4,
            "sha256": "1" * 64,
        },
    }


def test_declare_rejects_package_uid_outside_session(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package_a = make_assigned_package(db_session, workspace, project)
    package_b = make_assigned_package(db_session, workspace, project)
    created = client.post(
        "/api/v1/upload-sessions",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "collection_project_id": project.id,
            "package_uids": [package_a.package_uid],
            "upload_mode": "duance_sdk",
        },
    )
    session_id = created.json()["data"]["id"]
    response = client.post(
        f"/api/v1/upload-sessions/{session_id}/declarations",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "items": [_minimal_duance_payload(package_b.package_uid)],
        },
    )
    assert response.status_code == 409
    assert "package_not_in_session" in response.json()["detail"]


def test_chunked_upload_happy_path_marks_session_uploaded(
    client, db_session, admin_headers, tmp_path, monkeypatch
):
    from data.config import settings

    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package = make_assigned_package(db_session, workspace, project)
    created = client.post(
        "/api/v1/upload-sessions",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "collection_project_id": project.id,
            "package_uids": [package.package_uid],
            "upload_mode": "chunked",
        },
    )
    session_id = created.json()["data"]["id"]
    client.post(
        f"/api/v1/upload-sessions/{session_id}/declarations",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "items": [_minimal_duance_payload(package.package_uid)],
        },
    )
    init = client.post(
        f"/api/v1/upload-sessions/{session_id}/chunked/init",
        headers=admin_headers,
        json={"workspace_id": workspace.id, "total_chunks": 1},
    )
    assert init.status_code == 200
    put = client.put(
        f"/api/v1/upload-sessions/{session_id}/chunked/0",
        headers={**admin_headers, "Content-Type": "application/octet-stream"},
        content=b"mcap",
    )
    assert put.status_code == 200
    done = client.post(
        f"/api/v1/upload-sessions/{session_id}/chunked/complete",
        headers=admin_headers,
        json={"workspace_id": workspace.id},
    )
    assert done.status_code == 200
    assert done.json()["data"]["status"] == "uploaded"
```

说明：`metadata_sha256` 在实现里应按 `metadata_text` 真实 sha256 计算并在测试中同步，避免校验失败；上面占位 `"0"*64` 须在落地测试中改成 `hashlib.sha256(metadata_text.encode()).hexdigest()`。

- [x] **Step 2: 实现 intake service（最小可用）**

复用常量：

```python
from data.services.import_intake import (
    MAX_IMPORT_CHUNKS,
    MAX_IMPORT_CHUNK_BYTES,
)
```

OSS 签名调用 `data.infra.oss_client` 现有方法；完成时用 ListParts/CRC 对账（对齐 `complete_direct_multipart_upload` 的校验深度——至少校验 part 数量、大小与 ETag 列表非空，生产路径复用既有 verify 辅助函数，禁止信任浏览器自报清单而不核对）。

会话状态推进用 `SELECT FOR UPDATE`。

- [x] **Step 3: 测试通过并提交**

```bash
git commit -m "feat: add collection upload intake for SDK chunked and OSS paths"
```

---

### Task 4: 解析适配与包状态推进到待入库审核

**Files:**
- Create: `backend/data/services/collection_upload_parse.py`
- Modify: `backend/data/services/collection_upload_intake.py`（complete 后派发 parse job）
- Modify: `backend/data/tasks/` 或既有 job worker 注册 `collection_upload_parse`
- Test: `backend/tests/test_collection_upload_parse.py`

**Interfaces:**
- Consumes: 已 `uploaded` 会话、声明中的 metadata/MCAP（或 staging 组装件）
- Produces: `parse_collection_upload_session(db, session_id) -> None`
  - 成功：为每个声明创建 `Episode(data_package_id=..., task_set_id=None, batch_id=None)`；写 `qrdf_facts_json`（含 `capture.mode`、`privacy_sensitive`、`capture.app_version`）；包状态 `parsing → ingested → pending_intake_review`；设 `upload_completed_at`、`captured_duration_hours`；会话 `succeeded`
  - 失败：包 → `parse_failed`，写 `parse_error_*`；会话 `failed`；**保留** staging/审计，允许 Task 2 以 `parse_failed` 再开会话

解析要点：
- 调用既有 QRDF metadata 校验（`import_parser` / integrations.qrdf）；不要启动 quality/cut/annotation job
- 缺 `media/preview/manifest.json`：**不**算解析失败；在 `qrdf_facts_json["preview"] = {"available": false}` 记录
- LeRobot 来源若误入采集上传：拒绝并 `parse_failed`（明确 error_code `lerobot_not_supported_on_collection_upload`）
- Episode `validity_status` 默认 `valid`
- SN 匹配：复用 `capture_provenance` 工作空间匹配；未匹配记 attribution `unmatched`
- 幂等：同一 `source_fingerprint` + `data_package_id` 已存在则复用，不复制 Episode

状态推进必须原子：

```python
def _advance_package(db, package_id, from_statuses, to_status, **fields):
    result = db.execute(
        update(DataPackage)
        .where(DataPackage.id == package_id, DataPackage.status.in_(from_statuses))
        .values(status=to_status, updated_at=datetime.utcnow(), **fields)
    )
    if result.rowcount != 1:
        raise PackageStateConflictError("package_status_conflict")
```

建议序列：`pending_upload|uploading → parsing → pending_intake_review`（`ingested` 可在同一事务内瞬间经过并写审计 detail，或作为 `result_json` 阶段标记；对外列表以 `pending_intake_review` 为准）。若保留 `ingested` 对外可见，须在解析末尾**同一请求/job 内**继续转到 `pending_intake_review`，禁止停在 `ingested` 无人值守。

- [x] **Step 1: 纯 service 测试（可用最小 metadata fixture）**

```python
"""采集上传解析：写 Episode 并推进到 pending_intake_review。"""

from decimal import Decimal
from uuid import uuid4

from data.models.collection_upload import CollectionUploadSession, CollectionUploadSessionPackage
from data.models.data_package import DataPackage
from data.database import Episode
from data.services.collection_upload_parse import parse_collection_upload_session
from tests.collection_api_fixtures import make_assigned_package, make_project, make_workspace


def test_parse_creates_episode_under_package_and_moves_to_pending_intake_review(
    db_session, monkeypatch
):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package = make_assigned_package(db_session, workspace, project)
    session = CollectionUploadSession(
        id=str(uuid4()),
        workspace_id=workspace.id,
        collection_project_id=project.id,
        status="uploaded",
        upload_mode="duance_sdk",
        result_json={
            "parse_fixture_mode": True,
            "declarations": [
                {
                    "package_uid": package.package_uid,
                    "episode_id": f"ep-{uuid4().hex[:12]}",
                    "start_ns": 0,
                    "end_ns": 3_600_000_000_000,
                    "capture_mode": "ego",
                    "privacy_sensitive": False,
                    "modality": "rgb",
                }
            ],
        },
    )
    db_session.add(session)
    db_session.flush()
    db_session.add(
        CollectionUploadSessionPackage(
            upload_session_id=session.id,
            data_package_id=package.id,
            package_uid=package.package_uid,
        )
    )
    package.status = "uploading"
    db_session.commit()

    parse_collection_upload_session(db_session, session_id=session.id)
    db_session.refresh(package)
    episode = db_session.query(Episode).filter(Episode.data_package_id == package.id).one()
    assert episode.batch_id is None
    assert episode.validity_status == "valid"
    assert package.status == "pending_intake_review"
    assert package.upload_completed_at is not None
    assert package.capture_mode == "ego"
    assert package.captured_duration_hours == Decimal("1.00")
```

约束：`parse_fixture_mode` **仅**在 `settings` 测试环境或显式 `result_json` 标记下可用；生产 `parse_collection_upload_session` 必须校验 data file / checksum，忽略或拒绝外部传入的 fixture 标记。

- [x] **Step 2: 实现并注册 job；测试通过；提交**

```bash
git commit -m "feat: parse collection uploads into package-scoped episodes"
```

---

### Task 5: 数据包详情（含 Episode 与安全元数据）

**Files:**
- Modify: `backend/data/services/collection_packages.py`
- Modify: `backend/data/routers/collection_packages.py`
- Test: `backend/tests/test_collection_packages_api.py`（追加）

**Interfaces:**
- `GET /api/v1/data-packages/{id}?workspace_id=`
- 返回：包字段 + `episodes: [{id, episode_uid, validity_status, modality, duration_hours?, privacy_sensitive?, preview_available}]` + `qrdf_facts`（已消毒）+ `intake_review`（若有）
- 禁止字段：任何 path、uri、bucket、key、fingerprint 盐值明文若规格未要求则可不暴露 `source_fingerprint`

- [x] **Step 1: 测试**

在 `collection_api_fixtures.py` 增加 `seed_package_pending_intake_review(db, workspace, project, *, episode_hours=(1.0, 1.0))`：创建已分配包，插入 2 条 `Episode(data_package_id=..., batch_id=None, task_set_id=None)`，包状态直接置为 `pending_intake_review`（绕过真实上传，专供审核/详情测试）。

```python
def test_get_package_detail_lists_episodes_without_storage_uris(client, db_session, admin_headers):
    from tests.collection_api_fixtures import (
        make_project,
        make_workspace,
        seed_package_pending_intake_review,
    )

    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package, episodes = seed_package_pending_intake_review(
        db_session, workspace, project, episode_hours=(1.0, 1.0)
    )
    package.qrdf_facts_json = {
        "capture": {"mode": "ego"},
        "preview": {"available": False},
        "internal_path": "/collection-uploads/should-not-leak",
    }
    db_session.commit()

    detail = client.get(
        f"/api/v1/data-packages/{package.id}",
        headers=admin_headers,
        params={"workspace_id": workspace.id},
    )
    assert detail.status_code == 200
    payload = detail.json()["data"]
    assert len(payload["episodes"]) == 2
    assert {row["episode_uid"] for row in payload["episodes"]} == {
        episodes[0].episode_uid,
        episodes[1].episode_uid,
    }
    blob = str(payload)
    assert "s3://" not in blob
    assert "oss://" not in blob
    assert "/collection-uploads/" not in blob
    assert "internal_path" not in blob
```

- [x] **Step 2: 实现、跑通、提交**

```bash
git commit -m "feat: add data-package detail with safe episode metadata"
```

---

### Task 6: 入库审核（逐包通过/拒绝）

**Files:**
- Create: `backend/data/services/collection_intake_review.py`
- Create: `backend/data/routers/collection_intake_review.py`（或并入 packages router）
- Modify: `backend/data/main.py`
- Modify: `backend/data/security/audit.py`
- Test: `backend/tests/test_collection_intake_review_api.py`

**Interfaces:**
- `POST /api/v1/data-packages/{id}/intake-review`
  body:
  - 通过：`{workspace_id, verdict: "approved", rejected_episode_ids?: int[], reason?: ""}`
  - 拒绝：`{workspace_id, verdict: "rejected", reason: str}`（reason 必填非空）

行为：
- 仅 `pending_intake_review`
- 仅 admin
- `approved`：
  - 将 `rejected_episode_ids` 内且属于该包的 Episode → `validity_status=intake_rejected`
  - 其余保持 `valid`
  - 计算 `intake_valid_duration_hours` = sum(valid episodes 时长)；写入包字段后不可再改
  - 包状态 → `intake_approved`
  - 写 `PackageIntakeReview(is_bulk=False, rejected_episode_ids_json=...)`
- `rejected`：
  - 包内**全部** Episode → `intake_rejected`
  - 包状态 → `voided`
  - `intake_valid_duration_hours = 0.00`（选定 0 而非 NULL，便于概览求和）
  - **不**自动新建待分配包
  - 写 review `verdict=rejected`
- 重复审核 → 409 `already_reviewed`
- 审计：`collection.intake.review` detail 含 `verdict`、`is_bulk=false`、`rejected_episode_ids`

时长来源：优先 Episode `metadata_json` timing；若无则用 seed 时写入的小时数字段（fixture 在 `metadata_json["timing"]["duration_s"]` 放置秒数）。

- [x] **Step 1: 写测试**

```python
from data.models.data_package import DataPackage, PackageIntakeReview
from tests.collection_api_fixtures import (
    make_project,
    make_workspace,
    seed_package_pending_intake_review,
)


def test_intake_approve_marks_rejected_episodes_and_sets_immutable_duration(
    client, db_session, admin_headers
):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package, episodes = seed_package_pending_intake_review(
        db_session, workspace, project, episode_hours=(1.0, 1.0)
    )
    bad_episode, good_episode = episodes

    response = client.post(
        f"/api/v1/data-packages/{package.id}/intake-review",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "verdict": "approved",
            "rejected_episode_ids": [bad_episode.id],
        },
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "intake_approved"
    assert data["intake_valid_duration_hours"] == "1.00"

    db_session.refresh(bad_episode)
    db_session.refresh(good_episode)
    assert bad_episode.validity_status == "intake_rejected"
    assert good_episode.validity_status == "valid"

    again = client.post(
        f"/api/v1/data-packages/{package.id}/intake-review",
        headers=admin_headers,
        json={"workspace_id": workspace.id, "verdict": "approved"},
    )
    assert again.status_code == 409
    assert "already_reviewed" in again.json()["detail"]


def test_intake_reject_voids_package_without_autotop_up(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package, episodes = seed_package_pending_intake_review(
        db_session, workspace, project, episode_hours=(1.0,)
    )
    before = (
        db_session.query(DataPackage)
        .filter(DataPackage.collection_task_id == package.collection_task_id)
        .count()
    )
    response = client.post(
        f"/api/v1/data-packages/{package.id}/intake-review",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "verdict": "rejected",
            "reason": "bad capture",
        },
    )
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "voided"
    assert response.json()["data"]["intake_valid_duration_hours"] == "0.00"
    after = (
        db_session.query(DataPackage)
        .filter(DataPackage.collection_task_id == package.collection_task_id)
        .count()
    )
    assert after == before
    assert all(
        db_session.get(type(episodes[0]), ep.id).validity_status == "intake_rejected"
        for ep in episodes
    )
```

- [x] **Step 2: 实现、提交**

```bash
git commit -m "feat: add per-package intake review approve and reject"
```

---

### Task 7: 批量入库通过

**Files:**
- Modify: `backend/data/services/collection_intake_review.py`
- Modify: `backend/data/routers/collection_intake_review.py`
- Test: `backend/tests/test_collection_intake_review_api.py`（追加）

**Interfaces:**
- `POST /api/v1/data-packages/intake-review/bulk-approve`
  `{workspace_id, data_package_ids: int[]}`
- 每个包等价于 `verdict=approved` 且 `rejected_episode_ids=[]`，`is_bulk=True`
- 部分失败：事务策略选 **all-or-nothing**（任一非 `pending_intake_review` 则整批 409，detail 列出问题包 id）——实现简单且审计清晰
- 审计每条 review 仍单独落库，`is_bulk=true`；另可写一条汇总 `collection.intake.bulk_approve`

- [x] **Step 1: 测试**

```python
def test_bulk_approve_sets_is_bulk_and_approves_all(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    p1, _ = seed_package_pending_intake_review(db_session, workspace, project)
    p2, _ = seed_package_pending_intake_review(db_session, workspace, project)
    response = client.post(
        "/api/v1/data-packages/intake-review/bulk-approve",
        headers=admin_headers,
        json={"workspace_id": workspace.id, "data_package_ids": [p1.id, p2.id]},
    )
    assert response.status_code == 200
    assert response.json()["data"]["approved_count"] == 2
    reviews = (
        db_session.query(PackageIntakeReview)
        .filter(PackageIntakeReview.data_package_id.in_([p1.id, p2.id]))
        .all()
    )
    assert len(reviews) == 2
    assert all(r.is_bulk for r in reviews)
    assert all(r.verdict == "approved" for r in reviews)


def test_bulk_approve_is_atomic_when_one_not_pending(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    p1, _ = seed_package_pending_intake_review(db_session, workspace, project)
    p2, _ = seed_package_pending_intake_review(db_session, workspace, project)
    p2.status = "intake_approved"
    db_session.commit()
    response = client.post(
        "/api/v1/data-packages/intake-review/bulk-approve",
        headers=admin_headers,
        json={"workspace_id": workspace.id, "data_package_ids": [p1.id, p2.id]},
    )
    assert response.status_code == 409
    db_session.refresh(p1)
    assert p1.status == "pending_intake_review"
    assert (
        db_session.query(PackageIntakeReview)
        .filter(PackageIntakeReview.data_package_id == p1.id)
        .count()
        == 0
    )
```

- [x] **Step 2: 实现、提交**

```bash
git commit -m "feat: add bulk intake approve for data packages"
```

---

### Task 8: 端到端冒烟与 Plan 自检回归

**Files:**
- Test: `backend/tests/test_collection_upload_intake_smoke.py`
- 不改生产代码（除非冒烟暴露缺口）

- [x] **Step 1: 冒烟路径**

路径：admin → 建项目/任务/分配包 → 创建 upload-session → 声明源 →（测试用）同步 parse fixture → 包详情可见 episode → 入库审核标记 1 条不合格并通过 → 概览 `intake_valid_duration_hours` 增加 → 另一包 bulk-approve → 拒绝第三包并确认未自动补包。

不调用建批/标注。

- [x] **Step 2: 跑齐本计划 + Plan② 相关测试**

```bash
TEST_DATABASE_URL=postgresql+psycopg://quicdata:quicdata@127.0.0.1:5432/quicdata_test_studio \
  .venv/bin/python -m pytest \
  backend/tests/test_collection_access.py \
  backend/tests/test_collection_labels_api.py \
  backend/tests/test_collection_device_models_api.py \
  backend/tests/test_collection_projects_api.py \
  backend/tests/test_collection_tasks_api.py \
  backend/tests/test_collection_packages_api.py \
  backend/tests/test_collection_overview_api.py \
  backend/tests/test_collection_management_smoke.py \
  backend/tests/test_collection_upload_session_models.py \
  backend/tests/test_collection_upload_sessions_api.py \
  backend/tests/test_collection_upload_intake_api.py \
  backend/tests/test_collection_upload_parse.py \
  backend/tests/test_collection_intake_review_api.py \
  backend/tests/test_collection_upload_intake_smoke.py \
  -q
```

- [x] **Step 3: 提交**

```bash
git add backend/tests/test_collection_upload_intake_smoke.py
git commit -m "test: add collection upload and intake review end-to-end smoke"
```

---

## Spec 覆盖对照

| 规格条目 | 任务 | 自检状态 |
|---|---|---|
| §3.1 状态机（分配后至入库通过/解析失败/作废） | Task 2–4, 6 | ✅ 已落地 |
| §3.2 上传会话 SDK/分片；不自动质检 | Task 3–4 | ✅ SDK 逐文件 `source_id`；无 ZIP |
| §3.2 OSS 直传 | Task 3 | ⚠️ 仅单来源；多文件走 SDK |
| §3.2 已授权来源导入 | — | ⚠️ 模型预留 `authorized_import`，API 拒绝创建（有意延期） |
| §3.2 capture.mode / preview 缺失策略 / QRDF vs 平台标签 | Task 4–5 | ✅ 缺 Preview 不失败；facts 消毒 |
| §3.2 privacy_sensitive 透传 | Task 4–5 | ✅ |
| §3.3 管理员入库审核、逐 Episode 不合格、整包拒绝 | Task 6 | ✅ |
| §3.3 批量通过与 is_bulk 审计 | Task 7 | ✅（汇总审计事件可选，非阻断） |
| §3.3 强制审核、不自动补包 | Task 6–8 | ✅ |
| §6 入库有效时长写入后不变 | Task 6–8 | ✅ |
| §9.2 upload-sessions、package_uid 归属、明确业务错误 | Task 2–3 | ✅ |
| §2.1 归档禁止新上传 | Task 2 | ✅ |
| §7/§10 前端入库抽屉与预览播放 | — | 外置：本计划后端 API；UI/预览联调未验收 |

**未覆盖且正确外置（Plan ④）：** §4 建批与治理、§5 标注审核、§8 资产与数据集、LeRobot 数据集直导。

**类型一致性：** 包状态字符串与 Plan ① `DATA_PACKAGE_STATUSES` 一致；审核 `verdict` 与 `PackageIntakeReview` 一致；Episode `validity_status` 用 `intake_rejected`（对应规格「人工不合格」）；对外身份字段为 `package_uid`（规格口语 `package_id` 的同一概念）。分片目标键：`source_id`（64 hex）与声明一一对应。

## Self-Review（writing-plans · 2026-09-17）

**1. Spec coverage：** 见上表。相对完整 §3.2，「已授权来源导入」与「OSS 多来源」未做满，已在 Global Constraints / 架构决策中显式收窄，避免计划假装覆盖。前端抽屉/预览属 §7，本计划自始为后端范围，与 Plan ② 一致。

**2. Placeholder scan：** 正文无 `TODO`/`TBD`。Task 2 伪代码仍有 `...` 省略（实现已完成，步骤仅作历史）；Task 3 示例里 `"0"*64` 旁注要求落地用真实 sha256——实现与 `test_collection_upload_bytes.py` 已按真值校验。历史步骤复选框全未勾选：进度以 `docs/PLAN_1_3_REVIEW.md` 为准，不把空复选框当未完成。

**3. Type consistency：** `package_uid` / `source_id` / `COLLECTION_UPLOAD_MODES` / 包状态字面量与代码、`COLLECTION_UPLOAD_API.md` 对齐。计划正文 Task 3 旧 HTTP 片段曾省略 `source_id`——已在本自检中改写为与接口约定一致。

**结论：** Plan ③ 作为「接入与入库审核」后端计划可闭合；残留为有意延期（authorized_import、OSS 多来源、云 OSS/worker/SDK 客户端/预览联调）与 Plan ④ 外置范围，不是未写清的占位需求。

---

## 审计结果（2026-09-22 收尾核对）

- 证据：本计划引用的 15 个测试文件全部存在并一起运行 **90 passed**（含上传会话、字节上传、解析、入库审核、冒烟）。
- 命名修订：正文中的 task set/Batch 入口已退役（见 2026-09-21 外部工具接入计划任务 10），本计划的上传与会话语义未变。
- 结论：全部步骤按现有制品与测试勾选。
