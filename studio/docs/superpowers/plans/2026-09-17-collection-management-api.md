# 采集管理 API Implementation Plan

> 2026-09-17 检查收口：Task 1–9 的 API 与测试已落地；补齐包调整行锁、原子作废和拆包上限。当前进度、验证与限制见 [Plan 1–3 检查记录](../../PLAN_1_3_REVIEW.md)。下方保留原实施步骤，不用历史步骤复选框表示当前完成率。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Plan 1 已落地的采集域模型之上，实现「采集管理」后端 API：数采配置（标签/设备型号）、采集项目、采集任务（含预生成数据包）、数据包分配/作废，以及基于入库有效时长的采集概览。

**Architecture:** 新建 `data/services/collection_*` 承载业务规则，`data/routers/collection_*` 暴露 REST；权限按规格一期三角色——采集管理写操作强制 `admin`，读操作也仅管理员（标注员/审核员不走采集管理页）。工作空间归属一律经 `require_workspace_actor`。不实现上传会话、入库审核、建批与标注（留给 Plan ③④）。

**Tech Stack:** Python 3.11、FastAPI、SQLAlchemy 2.0（`Column()`）、Pydantic v2、pytest、既有 `success()` / `emit_audit_event` / `require_workspace_actor` 模式

**Spec:** `docs/superpowers/specs/2026-09-14-collection-studio-init-design.md`（§2.1–2.4、§6、§7 采集管理导航、§9.1–9.2 中与采集管理相关的资源）

**前置：** Plan ① 已完成。迁移头为 `0006_data_batch`。模型在 `backend/data/models/`。

**本计划是 4 份顺序计划的第 ②份。** ① 迁移+模型 ✓ · ② 采集管理 API（本文件）· ③ 接入与入库审核 · ④ 治理、标注审核、资产与数据集。

## Global Constraints

- 采集模式固定 `offline`；在线分发不实现。
- 数据包一经分配即锁定，不可改派；错误路径=作废 + 新建待分配包。
- 分配必须同时指定 `responsible_collector_id` 与 `operator_collector_id`（均引用 `PersonnelProfile`，且须属于该工作空间）。
- 仅 `pending_assignment` 包可改目标时长 / 删除 / 批量增删拆分。
- 任务创建时按 `target_duration_hours ÷ default_package_duration_hours` 预生成包；余量不足一个标准包时生成余量包。
- `package_uid` 服务端生成、不可变；离线清单按 `package_uid` 归属。
- 看板与任务进度用 `intake_valid_duration_hours`（本阶段多为 `null`，概览返回 0 / 目标对比即可）。
- 浏览器不接收存储 URI；本计划不出 OSS 凭据。
- 测试命令（仓库根目录）：

```bash
TEST_DATABASE_URL=postgresql+psycopg://quicdata:quicdata@127.0.0.1:5432/quicdata_test_studio \
  .venv/bin/python -m pytest backend/tests/<file> -q
```

## File Structure

```text
backend/data/
  services/
    collection_access.py          ← admin + workspace 校验
    collection_labels.py          ← 标签增/停用
    collection_device_models.py   ← 型号列表 + 预置种子
    collection_projects.py        ← 项目 CRUD / 归档
    collection_tasks.py           ← 任务 CRUD + 拆包
    collection_packages.py        ← 包调整 / 分配 / 作废 / 清单
    collection_overview.py        ← 概览聚合
  routers/
    collection_labels.py
    collection_device_models.py
    collection_projects.py
    collection_tasks.py
    collection_packages.py
    collection_overview.py
  main.py                         ← include_router
backend/tests/
  test_collection_labels_api.py
  test_collection_device_models_api.py
  test_collection_projects_api.py
  test_collection_tasks_api.py
  test_collection_packages_api.py
  test_collection_overview_api.py
  collection_api_fixtures.py      ← 共享建项目/任务/数采员辅助
```

路由前缀（均挂在 `settings.api_prefix` = `/api/v1` 下）：

| 资源 | 前缀 |
|---|---|
| 标签 | `/collection-labels` |
| 设备型号 | `/collection-device-models` |
| 采集项目 | `/collection-projects` |
| 采集任务 | `/collection-tasks` |
| 数据包 | `/data-packages` |
| 采集概览 | `/collection-overview` |

---

### Task 1: 采集访问控制与共享测试夹具

**Files:**
- Create: `backend/data/services/collection_access.py`
- Create: `backend/tests/collection_api_fixtures.py`
- Test: `backend/tests/test_collection_access.py`

**Interfaces:**
- Produces: `require_collection_admin(db, actor_id) -> User`；`require_collection_workspace(db, *, actor_id, workspace_id) -> Workspace`
- Consumes: `data.services.workspace_access.require_actor` / `require_workspace_actor`

- [x] **Step 1: 写失败的测试**

`backend/tests/test_collection_access.py`：

```python
"""采集管理访问控制：仅 admin，且必须属于工作空间。"""

from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from data.database import User, Workspace, WorkspaceMember
from data.services.collection_access import require_collection_admin, require_collection_workspace


def _user(db: Session, *, email: str, role: str) -> User:
    user = User(email=email, password_hash="x", role=role, is_active=True)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_require_collection_admin_rejects_annotator(db_session):
    user = _user(db_session, email=f"ann-{uuid4().hex}@t.com", role="annotator")
    with pytest.raises(PermissionError, match="admin"):
        require_collection_admin(db_session, actor_id=user.id)


def test_require_collection_workspace_rejects_outsider(db_session):
    admin = _user(db_session, email=f"adm-{uuid4().hex}@t.com", role="admin")
    workspace = Workspace(name=f"ws-{uuid4().hex}", creator=admin.email)
    db_session.add(workspace)
    db_session.commit()
    with pytest.raises(PermissionError):
        require_collection_workspace(db_session, actor_id=admin.id, workspace_id=workspace.id)


def test_require_collection_workspace_accepts_member_admin(db_session):
    admin = _user(db_session, email=f"adm-{uuid4().hex}@t.com", role="admin")
    workspace = Workspace(name=f"ws-{uuid4().hex}", creator=admin.email)
    db_session.add(workspace)
    db_session.flush()
    db_session.add(WorkspaceMember(workspace_id=workspace.id, user_id=admin.id))
    db_session.commit()
    got = require_collection_workspace(db_session, actor_id=admin.id, workspace_id=workspace.id)
    assert got.id == workspace.id
```

- [x] **Step 2: 运行确认失败**

```bash
TEST_DATABASE_URL=postgresql+psycopg://quicdata:quicdata@127.0.0.1:5432/quicdata_test_studio \
  .venv/bin/python -m pytest backend/tests/test_collection_access.py -q
```

预期：`ImportError` 或收集失败。

- [x] **Step 3: 实现访问控制**

`backend/data/services/collection_access.py`：

```python
"""采集管理 API 的统一鉴权。"""

from __future__ import annotations

from sqlalchemy.orm import Session

from data.database import User, Workspace
from data.services.workspace_access import require_actor, require_workspace_actor


def require_collection_admin(db: Session, *, actor_id: int | None) -> User:
    """规格一期：采集项目管理/分配仅管理员。"""
    actor = require_actor(db, actor_id=actor_id)
    if actor.role != "admin":
        raise PermissionError("only an admin can manage collection resources")
    return actor


def require_collection_workspace(
    db: Session, *, actor_id: int | None, workspace_id: int
) -> Workspace:
    """先验证 admin，再验证工作空间成员身份。"""
    require_collection_admin(db, actor_id=actor_id)
    require_workspace_actor(db, actor_id=actor_id, workspace_id=workspace_id)
    workspace = db.get(Workspace, workspace_id)
    if workspace is None:
        raise ValueError("workspace does not exist")
    return workspace
```

- [x] **Step 4: 写共享夹具模块**

`backend/tests/collection_api_fixtures.py`：

```python
"""采集管理 API 测试共享构造器。"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from sqlalchemy.orm import Session

from data.database import (
    PersonnelProfile,
    User,
    Workspace,
    WorkspaceMember,
    WorkspacePersonnelProfile,
)
from data.models.collection_config import CollectionDeviceModel, CollectionLabel
from data.models.collection_core import CollectionProject, CollectionTask
from data.services.collector_profiles import create_collector_profile


def ensure_admin_in_workspace(db: Session, workspace: Workspace) -> User:
    admin = db.query(User).filter(User.email == "admin@quicdata.com").one()
    exists = (
        db.query(WorkspaceMember)
        .filter(
            WorkspaceMember.workspace_id == workspace.id,
            WorkspaceMember.user_id == admin.id,
        )
        .first()
    )
    if exists is None:
        db.add(WorkspaceMember(workspace_id=workspace.id, user_id=admin.id))
        db.commit()
    return admin


def make_workspace(db: Session) -> Workspace:
    admin = db.query(User).filter(User.email == "admin@quicdata.com").one()
    workspace = Workspace(name=f"coll-api-{uuid4().hex}", creator=admin.email)
    db.add(workspace)
    db.flush()
    db.add(WorkspaceMember(workspace_id=workspace.id, user_id=admin.id))
    db.commit()
    db.refresh(workspace)
    return workspace


def make_label(db: Session, *, category: str, name: str | None = None) -> CollectionLabel:
    label = CollectionLabel(
        category=category,
        name=name or f"{category}-{uuid4().hex[:8]}",
        description="",
        is_active=True,
    )
    db.add(label)
    db.commit()
    db.refresh(label)
    return label


def make_device_model(db: Session) -> CollectionDeviceModel:
    model = CollectionDeviceModel(
        vendor=f"Vendor-{uuid4().hex[:6]}",
        model=f"Model-{uuid4().hex[:6]}",
        device_type="ego",
        modalities_json=["rgb"],
        is_active=True,
    )
    db.add(model)
    db.commit()
    db.refresh(model)
    return model


def make_project(
    db: Session, workspace: Workspace, *, name: str | None = None
) -> CollectionProject:
    project = CollectionProject(
        workspace_id=workspace.id,
        name=name or f"project-{uuid4().hex[:8]}",
        description="",
        status="enabled",
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def make_task(
    db: Session,
    project: CollectionProject,
    *,
    target_hours: str = "4.00",
    package_hours: str = "2.00",
    device_model_id: int | None = None,
) -> CollectionTask:
    task = CollectionTask(
        workspace_id=project.workspace_id,
        collection_project_id=project.id,
        name=f"task-{uuid4().hex[:8]}",
        target_duration_hours=Decimal(target_hours),
        default_package_duration_hours=Decimal(package_hours),
        capture_mode="offline",
        device_model_id=device_model_id,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def make_collector(
    db: Session, workspace: Workspace, *, name: str | None = None
) -> PersonnelProfile:
    profile = create_collector_profile(
        db, workspace_id=workspace.id, name=name or f"collector-{uuid4().hex[:6]}"
    )
    db.commit()
    db.refresh(profile)
    return profile
```

- [x] **Step 5: 跑测试通过并提交**

```bash
TEST_DATABASE_URL=postgresql+psycopg://quicdata:quicdata@127.0.0.1:5432/quicdata_test_studio \
  .venv/bin/python -m pytest backend/tests/test_collection_access.py -q
```

```bash
git add backend/data/services/collection_access.py backend/tests/test_collection_access.py backend/tests/collection_api_fixtures.py
git commit -m "feat: add collection admin access helpers for management APIs"
```

---

### Task 2: 标签字典 API

**Files:**
- Create: `backend/data/services/collection_labels.py`
- Create: `backend/data/routers/collection_labels.py`
- Modify: `backend/data/main.py`（注册路由）
- Test: `backend/tests/test_collection_labels_api.py`

**Interfaces:**
- `GET /api/v1/collection-labels?workspace_id=&category=&include_inactive=`
- `POST /api/v1/collection-labels` body `{category, name, description?}`
- `POST /api/v1/collection-labels/{id}/deactivate`
- 无 DELETE。名称在同类内 `lower(btrim(name))` 唯一。

说明：标签本身不挂 `workspace_id`（Plan 1 模型如此，规格「数采配置」为平台级字典）。`workspace_id` 查询参数仅用于鉴权（确认调用者是该空间 admin 成员）。

- [x] **Step 1: 写失败的 API 测试**

```python
"""采集标签字典 API。"""

from collection_api_fixtures import make_label, make_workspace


def test_create_label_and_list_by_category(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    created = client.post(
        "/api/v1/collection-labels",
        headers=admin_headers,
        json={"workspace_id": workspace.id, "category": "scene", "name": "Kitchen"},
    )
    assert created.status_code == 200
    label_id = created.json()["data"]["id"]

    listed = client.get(
        "/api/v1/collection-labels",
        headers=admin_headers,
        params={"workspace_id": workspace.id, "category": "scene"},
    )
    assert listed.status_code == 200
    names = [row["name"] for row in listed.json()["data"]["items"]]
    assert "Kitchen" in names
    assert label_id in {row["id"] for row in listed.json()["data"]["items"]}


def test_duplicate_label_name_conflicts(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    make_label(db_session, category="purpose", name="PickPlace")
    response = client.post(
        "/api/v1/collection-labels",
        headers=admin_headers,
        json={"workspace_id": workspace.id, "category": "purpose", "name": " pickplace "},
    )
    assert response.status_code == 409


def test_deactivate_label_hides_from_default_list(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    label = make_label(db_session, category="training", name="Grip")
    response = client.post(
        f"/api/v1/collection-labels/{label.id}/deactivate",
        headers=admin_headers,
        json={"workspace_id": workspace.id},
    )
    assert response.status_code == 200
    listed = client.get(
        "/api/v1/collection-labels",
        headers=admin_headers,
        params={"workspace_id": workspace.id, "category": "training"},
    )
    assert label.id not in {row["id"] for row in listed.json()["data"]["items"]}


def test_annotator_cannot_create_label(client, db_session, annotator_headers):
    workspace = make_workspace(db_session)
    response = client.post(
        "/api/v1/collection-labels",
        headers=annotator_headers,
        json={"workspace_id": workspace.id, "category": "scene", "name": "Nope"},
    )
    assert response.status_code == 403
```

- [x] **Step 2: 运行确认失败（404）**

```bash
TEST_DATABASE_URL=postgresql+psycopg://quicdata:quicdata@127.0.0.1:5432/quicdata_test_studio \
  .venv/bin/python -m pytest backend/tests/test_collection_labels_api.py -q
```

- [x] **Step 3: 实现 service**

`backend/data/services/collection_labels.py` 关键行为：

```python
def create_collection_label(
    db, *, category: str, name: str, description: str = ""
) -> CollectionLabel:
    if category not in COLLECTION_LABEL_CATEGORIES:
        raise ValueError(f"unsupported label category: {category}")
    cleaned = name.strip()
    if not cleaned:
        raise ValueError("label name is required")
    label = CollectionLabel(
        category=category, name=cleaned, description=description.strip(), is_active=True
    )
    db.add(label)
    db.flush()
    return label


def deactivate_collection_label(db, *, label_id: int) -> CollectionLabel:
    label = db.get(CollectionLabel, label_id)
    if label is None:
        raise ValueError("label does not exist")
    label.is_active = False
    db.flush()
    return label
```

唯一冲突：捕获 `IntegrityError` / `is_constraint_conflict(..., "uq_collection_labels_category_normalized_name")` → HTTP 409。

- [x] **Step 4: 实现 router 并注册**

Router 模式对齐 `collector_profiles.py`：`APIRouter(prefix="/collection-labels")`，`extra="forbid"` 的 Pydantic body，`emit_audit_event` 记录 `collection.label.create` / `collection.label.deactivate`。

在 `main.py` 的 router import 区增加 `collection_labels`，并在既有 `include_router` 列表末尾追加：

```python
from data.routers import collection_labels

app.include_router(collection_labels.router, prefix=prefix)
```

- [x] **Step 5: 测试通过并提交**

```bash
TEST_DATABASE_URL=postgresql+psycopg://quicdata:quicdata@127.0.0.1:5432/quicdata_test_studio \
  .venv/bin/python -m pytest backend/tests/test_collection_labels_api.py -q
```

```bash
git add backend/data/services/collection_labels.py backend/data/routers/collection_labels.py backend/data/main.py backend/tests/test_collection_labels_api.py
git commit -m "feat: add collection label dictionary API"
```

---

### Task 3: 设备型号目录 API + 预置种子

**Files:**
- Create: `backend/data/services/collection_device_models.py`
- Create: `backend/data/routers/collection_device_models.py`
- Create: `backend/scripts/seed_collection_device_models.py`（或并入既有 `seed_dev` 调用）
- Modify: `backend/data/main.py`
- Modify: `backend/scripts/seed_dev.py`（开发种子时调用 upsert）
- Test: `backend/tests/test_collection_device_models_api.py`

**Interfaces:**
- `GET /api/v1/collection-device-models?workspace_id=&include_inactive=`
- **无** 前端新建 POST（规格：新建按钮置灰）。种子数据由服务端脚本维护。
- 可选内部：`ensure_default_device_models(db)` 幂等插入若干型号。

- [x] **Step 1: 写失败的测试**

```python
def test_list_device_models_returns_seeded_active_rows(client, db_session, admin_headers):
    from data.services.collection_device_models import ensure_default_device_models

    workspace = make_workspace(db_session)
    ensure_default_device_models(db_session)
    db_session.commit()
    response = client.get(
        "/api/v1/collection-device-models",
        headers=admin_headers,
        params={"workspace_id": workspace.id},
    )
    assert response.status_code == 200
    items = response.json()["data"]["items"]
    assert len(items) >= 1
    assert {"id", "vendor", "model", "device_type", "modalities", "is_active"} <= set(items[0])


def test_no_public_create_endpoint(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    response = client.post(
        "/api/v1/collection-device-models",
        headers=admin_headers,
        json={"workspace_id": workspace.id, "vendor": "X", "model": "Y", "device_type": "ego"},
    )
    assert response.status_code in {404, 405, 422}
```

- [x] **Step 2: 实现 `ensure_default_device_models`**

至少预置 2 条（示例）：

```python
DEFAULT_DEVICE_MODELS = (
    {
        "vendor": "Unitree",
        "model": "G1",
        "device_type": "humanoid",
        "modalities_json": ["rgb", "depth"],
    },
    {"vendor": "Custom", "model": "EGO-Rig", "device_type": "ego", "modalities_json": ["rgb"]},
)
```

按 `lower(btrim(vendor))+lower(btrim(model))` 查重后插入。

- [x] **Step 3: Router + 注册 + 种子挂钩 + 测试 + commit**

```bash
git commit -m "feat: add collection device model catalog API and seed presets"
```

---

### Task 4: 采集项目 API

**Files:**
- Create: `backend/data/services/collection_projects.py`
- Create: `backend/data/routers/collection_projects.py`
- Modify: `backend/data/main.py`
- Test: `backend/tests/test_collection_projects_api.py`

**Interfaces:**
- `GET /api/v1/collection-projects?workspace_id=&status=`
- `POST /api/v1/collection-projects` `{workspace_id, name, description?, owner_user_id?}`
- `PATCH /api/v1/collection-projects/{id}` 可改 name/description/owner（仅 `enabled`）
- `POST /api/v1/collection-projects/{id}/archive`
- 归档后：禁止在该项目下新建任务（后续 Task 5 校验）

- [x] **Step 1: 写失败的测试**

```python
def test_create_list_and_archive_project(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    created = client.post(
        "/api/v1/collection-projects",
        headers=admin_headers,
        json={"workspace_id": workspace.id, "name": "Alpha", "description": "demo"},
    )
    assert created.status_code == 200
    project_id = created.json()["data"]["id"]
    assert created.json()["data"]["status"] == "enabled"

    listed = client.get(
        "/api/v1/collection-projects",
        headers=admin_headers,
        params={"workspace_id": workspace.id},
    )
    assert project_id in {row["id"] for row in listed.json()["data"]["items"]}

    archived = client.post(
        f"/api/v1/collection-projects/{project_id}/archive",
        headers=admin_headers,
        json={"workspace_id": workspace.id},
    )
    assert archived.status_code == 200
    assert archived.json()["data"]["status"] == "archived"


def test_duplicate_project_name_in_workspace_conflicts(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    make_project(db_session, workspace, name="Same")
    response = client.post(
        "/api/v1/collection-projects",
        headers=admin_headers,
        json={"workspace_id": workspace.id, "name": " same "},
    )
    assert response.status_code == 409
```

- [x] **Step 2: 实现 service/router**

归档：`status = "archived"`。已归档项目再次 archive 返回 200（幂等）或 409——选 **200 幂等**。

审计事件：`collection.project.create` / `collection.project.archive`。

- [x] **Step 3: 测试通过并提交**

```bash
git commit -m "feat: add collection project management API"
```

---

### Task 5: 采集任务 API + 预生成数据包

**Files:**
- Create: `backend/data/services/collection_tasks.py`（含 `split_packages_for_duration`）
- Create: `backend/data/routers/collection_tasks.py`
- Modify: `backend/data/main.py`
- Test: `backend/tests/test_collection_tasks_api.py`

**Interfaces:**
- `GET /api/v1/collection-tasks?workspace_id=&collection_project_id=`
- `POST /api/v1/collection-tasks`
  body：`{workspace_id, collection_project_id, name, description?, target_duration_hours, default_package_duration_hours?, sop_text?, device_model_id?, label_ids: int[]}`
- `GET /api/v1/collection-tasks/{id}?workspace_id=`
- 创建成功后自动插入 `DataPackage` 行，`status=pending_assignment`，生成 `package_uid`

**拆包算法（必须单测）：**

```python
from decimal import Decimal, ROUND_DOWN


def split_package_durations(target: Decimal, default_pkg: Decimal) -> list[Decimal]:
    if target <= 0 or default_pkg <= 0:
        raise ValueError("durations must be positive")
    full, remainder = divmod(target, default_pkg)
    durations = [default_pkg] * int(full)
    if remainder > 0:
        durations.append(remainder)
    if not durations:
        raise ValueError("no packages generated")
    return durations
```

例：`target=5, default=2` → `[2, 2, 1]`；`target=4, default=2` → `[2, 2]`。

`package_uid`：`f"pkg_{uuid4().hex}"`。

标签：只允许 `is_active=True` 且 category 合法的 id；写入 `collection_task_labels`。

归档项目拒绝建任务 → 409/`422`，detail 含 `archived`。

- [x] **Step 1: 纯函数单测 + API 测试**

```python
from decimal import Decimal
from data.services.collection_tasks import split_package_durations


def test_split_package_durations_with_remainder():
    assert split_package_durations(Decimal("5.00"), Decimal("2.00")) == [
        Decimal("2.00"),
        Decimal("2.00"),
        Decimal("1.00"),
    ]


def test_create_task_pregenerates_packages(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    device = make_device_model(db_session)
    scene = make_label(db_session, category="scene")
    purpose = make_label(db_session, category="purpose")

    response = client.post(
        "/api/v1/collection-tasks",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "collection_project_id": project.id,
            "name": "Pick cups",
            "target_duration_hours": "5.00",
            "default_package_duration_hours": "2.00",
            "device_model_id": device.id,
            "label_ids": [scene.id, purpose.id],
            "sop_text": "wash hands first",
        },
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["package_count"] == 3
    assert len(data["packages"]) == 3
    assert {p["status"] for p in data["packages"]} == {"pending_assignment"}
    assert all(p["package_uid"].startswith("pkg_") for p in data["packages"])
```

- [x] **Step 2: 实现并注册路由、跑通、提交**

进度字段（列表用）：`target_duration_hours`、`package_count`、`assigned_count`、`intake_valid_duration_hours`（对任务下包 `coalesce(sum(intake_valid_duration_hours),0)`）。

```bash
git commit -m "feat: add collection task API with offline package pre-generation"
```

---

### Task 6: 未分配数据包调整 API

**Files:**
- Create: `backend/data/services/collection_packages.py`（本任务先实现 list/adjust）
- Create: `backend/data/routers/collection_packages.py`
- Modify: `backend/data/main.py`
- Test: `backend/tests/test_collection_packages_api.py`（本任务部分用例）

**Interfaces:**
- `GET /api/v1/data-packages?workspace_id=&collection_project_id=&collection_task_id=&status=`
- `POST /api/v1/data-packages/adjust`
  body：`{workspace_id, collection_task_id, operations: [...]}`
  operations 之一：
  - `{op: "add", target_duration_hours}` — 仅未开始采集语义下给任务追加待分配包
  - `{op: "delete", data_package_id}` — 仅 `pending_assignment`
  - `{op: "resize", data_package_id, target_duration_hours}` — 仅 `pending_assignment`

对已分配包的任何 adjust → 409，detail 含 `assignment_locked`。

- [x] **Step 1: 写测试**

```python
from decimal import Decimal

from data.models.data_package import DataPackage
from data.services.collection_tasks import create_collection_task


def test_adjust_can_resize_and_add_pending_packages(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    device = make_device_model(db_session)
    task = create_collection_task(
        db_session,
        workspace_id=workspace.id,
        collection_project_id=project.id,
        name="adjust-me",
        target_duration_hours=Decimal("4.00"),
        default_package_duration_hours=Decimal("2.00"),
        device_model_id=device.id,
        label_ids=[],
        created_by_user_id=None,
    )
    db_session.commit()
    pending = (
        db_session.query(DataPackage)
        .filter(DataPackage.collection_task_id == task.id)
        .order_by(DataPackage.id.asc())
        .first()
    )
    response = client.post(
        "/api/v1/data-packages/adjust",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "collection_task_id": task.id,
            "operations": [
                {
                    "op": "resize",
                    "data_package_id": pending.id,
                    "target_duration_hours": "1.50",
                },
                {"op": "add", "target_duration_hours": "2.00"},
            ],
        },
    )
    assert response.status_code == 200
    packages = response.json()["data"]["packages"]
    assert any(str(row["target_duration_hours"]) in {"1.50", "1.5"} for row in packages)
    assert len(packages) == 3


def test_adjust_rejects_assigned_package(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    device = make_device_model(db_session)
    task = create_collection_task(
        db_session,
        workspace_id=workspace.id,
        collection_project_id=project.id,
        name="locked",
        target_duration_hours=Decimal("2.00"),
        default_package_duration_hours=Decimal("2.00"),
        device_model_id=device.id,
        label_ids=[],
        created_by_user_id=None,
    )
    package = db_session.query(DataPackage).filter(DataPackage.collection_task_id == task.id).one()
    owner = make_collector(db_session, workspace, name="Owner")
    operator = make_collector(db_session, workspace, name="Op")
    package.status = "assigned"
    package.responsible_collector_id = owner.id
    package.operator_collector_id = operator.id
    db_session.commit()

    response = client.post(
        "/api/v1/data-packages/adjust",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "collection_task_id": task.id,
            "operations": [{"op": "delete", "data_package_id": package.id}],
        },
    )
    assert response.status_code == 409
    assert "assignment_locked" in response.json()["detail"]
```

- [x] **Step 2: 实现、注册、提交**

```bash
git commit -m "feat: add pending data-package adjust API"
```

---

### Task 7: 数据包分配、离线清单与作废

**Files:**
- Modify: `backend/data/services/collection_packages.py`
- Modify: `backend/data/routers/collection_packages.py`
- Test: `backend/tests/test_collection_packages_api.py`（追加用例）

**Interfaces:**
- `POST /api/v1/data-packages/{id}/assign`
  `{workspace_id, responsible_collector_id, operator_collector_id}`
  成功：`status=assigned`，写 `assigned_at`，返回 `offline_manifest`
- `GET /api/v1/data-packages/{id}/offline-manifest?workspace_id=` — 仅已分配及之后非 voided 状态可取
- `POST /api/v1/data-packages/{id}/void`
  `{workspace_id, reason}` → `status=voided`；**不**自动补包

**离线清单最小字段：**

```python
{
    "schema_version": 1,
    "package_uid": package.package_uid,
    "workspace_id": package.workspace_id,
    "collection_project_id": package.collection_project_id,
    "collection_task_id": package.collection_task_id,
    "target_duration_hours": str(package.target_duration_hours),
    "responsible_collector_id": package.responsible_collector_id,
    "operator_collector_id": package.operator_collector_id,
    "device_model_id": task.device_model_id,
    "assigned_at": format_api_datetime(package.assigned_at),
}
```

**校验：**
- 两名数采员必须存在、`is_active`，且在 `workspace_personnel_profiles` 中属于该 `workspace_id`
- 仅 `pending_assignment` 可 assign；已分配再 assign → 409 `assignment_locked`
- void 仅允许 `assigned` / `pending_upload`（尚未开始上传的包）；已进入 `uploading` 及之后 → 409（上传中的作废留给 Plan ③ 如需扩展）
- 审计：`collection.package.assign` / `collection.package.void`

- [x] **Step 1: 写测试**

```python
from decimal import Decimal

from data.models.data_package import DataPackage
from data.services.collection_tasks import create_collection_task


def test_assign_requires_both_collectors_and_locks(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    device = make_device_model(db_session)
    task = create_collection_task(
        db_session,
        workspace_id=workspace.id,
        collection_project_id=project.id,
        name="assign-me",
        target_duration_hours=Decimal("2.00"),
        default_package_duration_hours=Decimal("2.00"),
        device_model_id=device.id,
        label_ids=[],
        created_by_user_id=None,
    )
    db_session.commit()
    package = db_session.query(DataPackage).filter(DataPackage.collection_task_id == task.id).one()
    owner = make_collector(db_session, workspace, name="Owner")
    operator = make_collector(db_session, workspace, name="Operator")

    assigned = client.post(
        f"/api/v1/data-packages/{package.id}/assign",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "responsible_collector_id": owner.id,
            "operator_collector_id": operator.id,
        },
    )
    assert assigned.status_code == 200
    body = assigned.json()["data"]
    assert body["status"] == "assigned"
    assert body["offline_manifest"]["package_uid"] == body["package_uid"]

    again = client.post(
        f"/api/v1/data-packages/{package.id}/assign",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "responsible_collector_id": owner.id,
            "operator_collector_id": operator.id,
        },
    )
    assert again.status_code == 409


def test_void_assigned_package_does_not_autotop_up(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    device = make_device_model(db_session)
    task = create_collection_task(
        db_session,
        workspace_id=workspace.id,
        collection_project_id=project.id,
        name="void-me",
        target_duration_hours=Decimal("2.00"),
        default_package_duration_hours=Decimal("2.00"),
        device_model_id=device.id,
        label_ids=[],
        created_by_user_id=None,
    )
    package = db_session.query(DataPackage).filter(DataPackage.collection_task_id == task.id).one()
    owner = make_collector(db_session, workspace, name="Owner")
    operator = make_collector(db_session, workspace, name="Operator")
    client.post(
        f"/api/v1/data-packages/{package.id}/assign",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "responsible_collector_id": owner.id,
            "operator_collector_id": operator.id,
        },
    )
    before = db_session.query(DataPackage).filter(DataPackage.collection_task_id == task.id).count()
    voided = client.post(
        f"/api/v1/data-packages/{package.id}/void",
        headers=admin_headers,
        json={"workspace_id": workspace.id, "reason": "wrong collector"},
    )
    assert voided.status_code == 200
    assert voided.json()["data"]["status"] == "voided"
    after = db_session.query(DataPackage).filter(DataPackage.collection_task_id == task.id).count()
    assert after == before
```

- [x] **Step 2: 实现、跑通、提交**

```bash
git commit -m "feat: add data-package assign, offline manifest, and void APIs"
```

---

### Task 8: 采集概览 API

**Files:**
- Create: `backend/data/services/collection_overview.py`
- Create: `backend/data/routers/collection_overview.py`
- Modify: `backend/data/main.py`
- Test: `backend/tests/test_collection_overview_api.py`

**Interfaces:**
- `GET /api/v1/collection-overview?workspace_id=&collection_project_id=`

**返回（口径写进字段名，避免歧义）：**

```python
{
    "workspace_id": ...,
    "collection_project_id": ... | null,
    "target_duration_hours": "12.00",  # 范围内任务 target 之和
    "intake_valid_duration_hours": "0.00",  # sum(coalesce(intake_valid_*,0))
    "package_counts": {"pending_assignment": 2, "assigned": 1, "voided": 0, "other": 0},
    "duration_basis": "intake_valid_duration_hours",
}
```

本阶段尚未做入库审核，`intake_valid_duration_hours` 多为 0——测试用手工把某包的 `intake_valid_duration_hours` 写成 `1.5` 断言求和。

- [x] **Step 1: 写测试并实现**

```python
from decimal import Decimal

from data.models.data_package import DataPackage
from data.services.collection_tasks import create_collection_task


def test_overview_sums_intake_valid_duration(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    device = make_device_model(db_session)
    task = create_collection_task(
        db_session,
        workspace_id=workspace.id,
        collection_project_id=project.id,
        name="overview",
        target_duration_hours=Decimal("2.00"),
        default_package_duration_hours=Decimal("2.00"),
        device_model_id=device.id,
        label_ids=[],
        created_by_user_id=None,
    )
    package = db_session.query(DataPackage).filter(DataPackage.collection_task_id == task.id).one()
    package.intake_valid_duration_hours = Decimal("1.50")
    db_session.commit()

    response = client.get(
        "/api/v1/collection-overview",
        headers=admin_headers,
        params={"workspace_id": workspace.id, "collection_project_id": project.id},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["intake_valid_duration_hours"] == "1.50"
    assert data["duration_basis"] == "intake_valid_duration_hours"
```

- [x] **Step 2: 提交**

```bash
git commit -m "feat: add collection overview API using intake valid duration"
```

---

### Task 9: 端到端冒烟与 Plan 自检回归

**Files:**
- Test: `backend/tests/test_collection_management_smoke.py`
- 不改生产代码（除非冒烟暴露缺口）

- [x] **Step 1: 冒烟测试（单文件串起主路径）**

路径：admin 登录（fixture）→ 建项目 → 建任务（5h/2h → 3 包）→ 分配 1 包 → 调整剩余 pending → 作废已分配包 → 概览可读。

不调用上传/入库审核。

- [x] **Step 2: 跑齐本计划测试**

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
  backend/tests/test_collection_*_models.py \
  -q
```

预期：全部 passed（模型测试仍绿）。

- [x] **Step 3: 提交冒烟测试**

```bash
git commit -m "test: add collection management API end-to-end smoke path"
```

---

## 明确不在本计划范围

- `upload-sessions`、SDK/分片上传、解析流水线（Plan ③）
- 入库审核 API（Plan ③）
- 数据批 / 治理 / 标注 / 审核工作项（Plan ④）
- 数据资产、数据集、格式转换、LeRobot 导入（Plan ④）
- 前端页面改造（可用现有 demo；对接另开）
- 在线分发、App 登录、设备前端新建
- 把遗留 `task_set` API 切到 `collection_project`（并行共存；迁移脚本未来另做）

## 计划自检

**规格覆盖：**

| 规格要点 | 任务 |
|---|---|
| §2.1 项目启用/归档 | Task 4 |
| §2.2 任务字段、无人员/地区、按时长拆包 | Task 5 |
| §2.3 型号目录只选不建 | Task 3 |
| §2.4 双人员分配、锁定、作废不改派 | Task 7 |
| §2.4 未分配包可调整 | Task 6 |
| §6 概览用入库有效时长 | Task 8 |
| §7 数采配置标签五类 | Task 2 |
| §9.1 管理员边界 | Task 1 |
| §9.2 `collection-projects` / `collection-tasks` / `data-packages` | Tasks 4–7 |
| 离线清单 `package_uid` | Task 7 |

**未覆盖且正确外置：** §3 接入与入库审核、§4–5 建批标注、§8 资产数据集、upload-sessions。

**类型一致性：** 状态字符串与 Plan 1 `DATA_PACKAGE_STATUSES` 一致（`pending_assignment` / `assigned` / `voided`）；时长用 `Decimal` 经 API 输出为字符串；标签类别元组复用 `COLLECTION_LABEL_CATEGORIES`。

**占位符扫描：** 无 TODO/待定；各任务含可运行测试命令与提交说明。

---

## 审计结果（2026-09-22 收尾核对）

- 证据：`test_collection_access.py`、`test_collection_labels_api.py`、`test_collection_device_models_api.py`、`test_collection_projects_api.py`、`test_collection_tasks_api.py`、`test_collection_packages_api.py`、`test_collection_overview_api.py`、`test_collection_management_smoke.py`、`test_collection_{config,core}_models.py` 一次性运行 **72 passed**；上述文件与 `backend/data/services/collection_*.py`、`collection_api_fixtures.py` 均存在。
- 命名修订：正文里的 task set 语义已由「采集项目（CollectionProject）」承担，正文未回填；以后续规格为准。
- 结论：全部步骤按现有制品与测试勾选。
