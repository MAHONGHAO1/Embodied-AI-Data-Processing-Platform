# 采集按日开包（Studio 部分）Implementation Plan

<!-- 代码块为插入片段，保留原始缩进，关闭 ruff 格式化 -->
<!-- fmt:off -->

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把采集包从「建任务时预拆 + 分配」改为「客户端按 任务 × 采集员 × 采集日 × 序号 开包」，并提供封口、任务结束、上传声明校验与 Episode 哈希，供 Duance 与上传 CLI 使用。

**Architecture:** 新模块 `data/services/daily_packages.py` 集中「采集日换算、开包、封口、声明校验」四条规则；数据库用一个一次性 Alembic 版本 `0012_daily_packages` 完成切换（作废空包、回填、删旧列）。先删旧功能（分配、调整、补采、离线清单、预拆），再切表结构，再逐个加新接口，最后改 CLI 与前端。

**Tech Stack:** FastAPI、SQLAlchemy 2、Alembic、PostgreSQL、pytest；标准库 CLI（`scripts/studio_upload.py`）；Vue 3 全局构建 + Element Plus、`node --test`。

**Spec:** `docs/superpowers/specs/2026-09-30-collection-daily-packages-design.md`

**配套计划：** Duance 部分见 `../duance/docs/superpowers/plans/2026-09-30-collection-daily-packages-duance.md`，依赖本计划 Task 4–6 的接口，须在本计划完成后执行。

## Global Constraints

- 采集日时区固定为 `Asia/Shanghai`；采集日 = Episode `start_ns` 换算到该时区的日期。库内时间仍为无时区 UTC。
- 包的唯一键：`(collection_task_id, collector_id, capture_date, sequence_no)`；序号从 1 开始，作废的序号不复用。
- 包状态：`open, pending_upload, uploading, parsing, ingested, pending_intake_review, intake_approved, batched, governing, published, parse_failed, voided`（删除 `pending_assignment`、`assigned`）。可上传状态：`open, parse_failed, pending_intake_review`。
- 任务状态：`active`（进行中）、`closed`（已结束）。
- 「冻结」= 存在 `package_intake_reviews` 记录；「可追加」= 未作废、未冻结、`sealed_at IS NULL`。
- 采集员标识：`metadata.operator.id` 为全数字字符串且数值 > 0，去掉前导零后按 `personnel_profiles.profile_key` 匹配；`unknown`（不区分大小写）视为缺失。
- 错误码放在 HTTP `detail` 字符串中（Duance 读取 `detail`）：`task_not_found`(404)、`task_closed`(409)、`project_archived`(409)、`capture_date_invalid`(422)、`collector_unmatched`(422)、`package_voided`(409)；声明不匹配为 422，`detail` 形如 `episode_capture_date_mismatch: ep1, ep2; episode_collector_mismatch: ep3`。
- 解析不下载、不重算 MCAP；Episode 哈希原样复制声明值。
- 看板时间分桶不改（仍按 `captured_started_at`）。
- 测试命令（在仓库根目录）：
  - 后端：`TEST_DATABASE_URL='postgresql+psycopg://quicdata:quicdata@127.0.0.1:5432/quicdata_test_daily' TEST_REDIS_URL='redis://127.0.0.1:6379/15' PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin <路径> -q`（下文简写为 `$PYTEST <路径>`）
  - 前端：`node --test frontend/tests/<文件>.test.mjs`；全部前端：`node --test frontend/tests/`
  - CLI：`python3 -m unittest discover -s scripts/tests -p test_studio_upload.py -v`
  - Lint：`.venv/bin/ruff check backend scripts`

## Review Focus

- **采集日跨零点**：Asia/Shanghai 23:59:59 与次日 00:00:00 开始的 Episode 必须落在不同采集日；UTC 与本地日期不同的时刻（UTC 16:00 前后）最容易出错。→ Task 4 `test_capture_date_of_uses_shanghai_midnight`、Task 6 `test_declaration_rejects_episode_from_next_capture_day`。
- **补零的采集员 ID**：元数据写 `"0007"`、档案 `profile_key="7"` 时，开包与声明都必须视为同一人。→ Task 1 `test_zero_padded_operator_matches_profile`、Task 6 `test_declaration_accepts_zero_padded_operator`。
- **开包后被冻结**：包在开出后、上传前被审核冻结，再开包必须得到续包而不是旧包。→ Task 4 `test_open_after_intake_review_creates_next_sequence`。
- **采集员在开包后被停用**：已开出的包仍可上传，声明校验只比对人，不要求在职。→ Task 6 `test_declaration_accepts_collector_deactivated_after_open`。
- **同一组合并发开包**：两个客户端同时为同一采集员同一天开包，只能生成一个包。→ Task 4 `test_concurrent_open_creates_one_package`。

---

## 文件结构

| 文件 | 职责 |
|---|---|
| `backend/data/services/capture_provenance.py`（改） | 采集员标识规范化（去前导零）与工作空间内按标识查人 |
| `backend/data/services/daily_packages.py`（新） | 采集日换算、开包、封口、上传声明校验 |
| `backend/data/services/package_views.py`（新） | 包的采集员／采集日／序号等字段的统一序列化 |
| `backend/alembic/versions/0012_daily_packages.py`（新） | 一次性表结构切换与数据回填 |
| `backend/data/models/data_package.py`、`collection_core.py`（改） | 新字段、状态、约束 |
| `backend/data/services/collection_tasks.py`、`routers/collection_tasks.py`（改） | 去预拆；开包、结束／重新开启接口；任务列表字段 |
| `backend/data/services/collection_packages.py`、`routers/collection_packages.py`（改） | 删分配／调整／补采／清单；封口接口；列表筛选 |
| `backend/data/services/collection_upload_intake.py`、`collection_upload_parse.py`（改） | 声明校验；Episode 哈希 |
| `backend/data/services/collection_dashboard.py`、`collection_overview.py`、`data_batches.py`、`fetch_manifests.py`、`package_list_projection.py`、`collection_upload_sessions.py`（改） | 读新字段与新状态 |
| `backend/scripts/seed_dev.py`（改） | 按日开包造数 |
| `backend/tests/collection_api_fixtures.py`（改） | `make_open_package`、上传元数据辅助 |
| `scripts/studio_upload.py`、`scripts/tests/test_studio_upload.py`（改） | `--task` 取代 `--package`，先开包再上传 |
| `frontend/js/{app,api,mining-utils,demo-data}.js`（改） | 删分配等入口；任务状态、进度、结束按钮；包列表新列 |
| `docs/COLLECTION_UPLOAD_API.md`、`docs/STUDIO_UPLOAD_CLI.md`（改） | 接口与 CLI 文档 |

---

### Task 1: 采集员标识规范化

**Files:**
- Modify: `backend/data/services/capture_provenance.py:28`, `:252-272`
- Test: `backend/tests/test_collector_identifier.py`（新）

**Interfaces:**
- Produces:
  - `canonical_collector_identifier(value: object) -> str | None`
  - `declared_collector_identifier(metadata: object) -> str | None`（从 `metadata["operator"]["id"]` 取值后规范化）
  - `resolve_workspace_collector(db: Session, *, workspace_id: int, identifier: str | None, require_active: bool) -> PersonnelProfile | None`

- [ ] **Step 1: 写失败的测试**

`backend/tests/test_collector_identifier.py`：

```python
"""Collector identifiers from QRDF metadata map to workspace profiles."""

import pytest
from collection_api_fixtures import make_collector, make_workspace

from data.services.capture_provenance import (
    canonical_collector_identifier,
    declared_collector_identifier,
    project_capture_provenance,
    resolve_workspace_collector,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("7", "7"),
        ("0007", "7"),
        ("42", "42"),
        ("0", None),
        ("0000", None),
        ("unknown", None),
        ("UNKNOWN", None),
        ("7a", None),
        (" 7", None),
        ("", None),
        (7, None),
        (None, None),
    ],
)
def test_canonical_collector_identifier(value, expected):
    assert canonical_collector_identifier(value) == expected


def test_declared_collector_identifier_reads_operator_id():
    assert declared_collector_identifier({"operator": {"id": "0042"}}) == "42"
    assert declared_collector_identifier({"operator": "42"}) is None
    assert declared_collector_identifier({}) is None
    assert declared_collector_identifier(None) is None


def test_zero_padded_operator_matches_profile(db_session):
    workspace = make_workspace(db_session)
    collector = make_collector(db_session, workspace)
    projection = project_capture_provenance(
        db_session,
        workspace_id=workspace.id,
        raw_metadata={"operator": {"id": f"000{collector.profile_key}"}},
    )
    assert projection.collector_profile_id == collector.id
    assert projection.collector_match_status == "matched"
    assert projection.collector_identifier == collector.profile_key


def test_resolve_workspace_collector_scopes_workspace_and_activity(db_session):
    workspace = make_workspace(db_session)
    other = make_workspace(db_session)
    collector = make_collector(db_session, workspace)
    stranger = make_collector(db_session, other)

    found = resolve_workspace_collector(
        db_session, workspace_id=workspace.id, identifier=collector.profile_key, require_active=True
    )
    assert found is not None and found.id == collector.id
    assert (
        resolve_workspace_collector(
            db_session, workspace_id=workspace.id, identifier=stranger.profile_key, require_active=True
        )
        is None
    )
    assert resolve_workspace_collector(
        db_session, workspace_id=workspace.id, identifier=None, require_active=True
    ) is None

    collector.is_active = False
    db_session.commit()
    assert (
        resolve_workspace_collector(
            db_session, workspace_id=workspace.id, identifier=collector.profile_key, require_active=True
        )
        is None
    )
    inactive = resolve_workspace_collector(
        db_session, workspace_id=workspace.id, identifier=collector.profile_key, require_active=False
    )
    assert inactive is not None and inactive.id == collector.id
```

- [ ] **Step 2: 运行，确认失败**

Run: `$PYTEST backend/tests/test_collector_identifier.py`
Expected: FAIL，`ImportError: cannot import name 'canonical_collector_identifier'`

- [ ] **Step 3: 实现**

`capture_provenance.py` 第 28 行改为：

```python
_COLLECTOR_IDENTIFIER = re.compile(r"^[0-9]+$")
```

在 `is_unknown_collector_identifier` 之后新增：

```python
def canonical_collector_identifier(value: object) -> str | None:
    """Return the positive profile key a producer meant, or ``None``.

    Legacy producers zero-pad keys (``"0007"``); Studio stores ``"7"``.
    """
    if not isinstance(value, str) or is_unknown_collector_identifier(value):
        return None
    if not _COLLECTOR_IDENTIFIER.fullmatch(value) or int(value) == 0:
        return None
    return str(int(value))


def declared_collector_identifier(metadata: object) -> str | None:
    """Canonical collector identifier declared by one QRDF metadata document."""
    if not isinstance(metadata, dict):
        return None
    operator = metadata.get("operator")
    if not isinstance(operator, dict):
        return None
    return canonical_collector_identifier(operator.get("id"))


def resolve_workspace_collector(
    db: Session,
    *,
    workspace_id: int,
    identifier: str | None,
    require_active: bool,
) -> PersonnelProfile | None:
    """Find the workspace collector for a canonical identifier."""
    if identifier is None:
        return None
    query = (
        select(PersonnelProfile)
        .join(
            WorkspacePersonnelProfile,
            WorkspacePersonnelProfile.personnel_profile_id == PersonnelProfile.id,
        )
        .where(
            WorkspacePersonnelProfile.workspace_id == workspace_id,
            PersonnelProfile.profile_key == identifier,
        )
    )
    if require_active:
        query = query.where(PersonnelProfile.is_active.is_(True))
    return db.scalar(query)
```

`_extract_collector_identifier` 中，把

```python
    if (
        not isinstance(value, str)
        or reported_identifier is None
        or not _COLLECTOR_IDENTIFIER.fullmatch(value)
    ):
        return reported_identifier, None, "invalid", "unmatched"
    return reported_identifier, value, "valid", "unmatched"
```

替换为：

```python
    canonical = canonical_collector_identifier(value)
    if reported_identifier is None or canonical is None:
        return reported_identifier, None, "invalid", "unmatched"
    return reported_identifier, canonical, "valid", "unmatched"
```

- [ ] **Step 4: 运行，确认通过，并回归相关测试**

Run: `$PYTEST backend/tests/test_collector_identifier.py backend/tests/test_capture_source_provenance.py backend/tests/test_historical_ego_import.py`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/data/services/capture_provenance.py backend/tests/test_collector_identifier.py
git commit -m "feat(collection): canonicalize zero-padded collector identifiers"
```

---

### Task 2: 删除分配、调整、补采、离线清单与预拆

本任务只删功能，不改表结构（旧列暂留，Task 3 删）。前端此时仍调用已删接口，Task 8 修复。

**Files:**
- Modify: `backend/data/services/collection_packages.py`（删 `AssignmentLockedError`、`_validate_duration`、`_require_task`、`_require_task_package`、`_require_active_workspace_collector`、`build_offline_manifest`、`assign_data_package`、`get_offline_manifest`、`adjust_pending_packages`、`create_supplement_package`；保留 `_require_enabled_project_for_update`、`_require_workspace_package`、`void_data_package`、`list_data_packages`、`get_data_package_detail`）
- Modify: `backend/data/routers/collection_packages.py`（删 `AddPackageOperation` 至 `AssignPackageRequest`、`SupplementPackageRequest`、`PackageCollectorAssignment`、`BatchAssignRequest` 等模型，删 `/adjust`、`/{id}/assign`、`/{id}/offline-manifest`、`/{id}/supplements`、`/batch-assign` 路由；`_package_item` 去掉 `supplement_for_package_id`、`supplement_reason`）
- Modify: `backend/data/services/collection_tasks.py`（删 `_DURATION_QUANTUM`、`_MAX_DURATION`、`MAX_PACKAGES_PER_TASK`、`split_package_durations`、`OFFLINE_MANIFEST_COLUMNS`、`offline_manifest_rows`；`create_collection_task` 删 `default_package_duration_hours` 参数与建包代码）
- Modify: `backend/data/routers/collection_tasks.py`（删请求字段 `default_package_duration_hours`、`/{task_id}/offline-manifest` 路由、`csv`/`io`/`PlainTextResponse` 导入）
- Modify: `backend/tests/collection_api_fixtures.py`
- Delete: `backend/tests/test_package_supplement_flow.py`、`backend/tests/test_collection_task_manifest_export.py`
- Modify: `backend/tests/test_collection_tasks_api.py`、`test_collection_packages_api.py`、`test_collection_management_smoke.py`、`test_external_tool_e2e.py`，以及 Step 5 列出的其它引用文件

**Interfaces:**
- Produces: `create_collection_task(db, *, workspace_id, collection_project_id, name, target_duration_hours, description="", sop_text="", device_model_id=None, label_ids, created_by_user_id) -> CollectionTask`（不再建包）

- [ ] **Step 1: 改写建任务测试为「不建包」**

`backend/tests/test_collection_tasks_api.py`：删除全部 `test_split_package_durations_*` 与 `split_package_durations` 导入；把 `test_create_task_pregenerates_packages_and_labels` 改名并改写为：

```python
def test_create_task_creates_no_packages_and_keeps_labels(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    label = make_label(db_session, category="purpose")
    response = client.post(
        "/api/v1/collection-tasks",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "collection_project_id": project.id,
            "name": "daily task",
            "target_duration_hours": "5.00",
            "label_ids": [label.id],
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["package_count"] == 0
    assert data["packages"] == []
    assert data["label_ids"] == [label.id]
    assert "default_package_duration_hours" not in data
    assert (
        db_session.query(DataPackage).filter(DataPackage.collection_task_id == data["id"]).count()
        == 0
    )


def test_create_task_rejects_legacy_package_duration_field(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    response = client.post(
        "/api/v1/collection-tasks",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "collection_project_id": project.id,
            "name": "legacy",
            "target_duration_hours": "5.00",
            "default_package_duration_hours": "2.00",
        },
    )
    assert response.status_code == 422
```

同文件其它调用 `create_collection_task(...)` 的地方删除 `default_package_duration_hours=` 实参。

- [ ] **Step 2: 新增「旧接口已删除」测试**

`backend/tests/test_collection_packages_api.py`：删除 `test_adjust_*`、`test_assign_*`、`test_concurrent_assign_*`、`test_offline_manifest_*` 全部用例，新增：

```python
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/api/v1/data-packages/adjust"),
        ("post", "/api/v1/data-packages/1/assign"),
        ("post", "/api/v1/data-packages/batch-assign"),
        ("get", "/api/v1/data-packages/1/offline-manifest"),
        ("post", "/api/v1/data-packages/1/supplements"),
        ("get", "/api/v1/collection-tasks/1/offline-manifest"),
    ],
)
def test_retired_package_endpoints_are_gone(client, admin_headers, method, path):
    response = getattr(client, method)(path, headers=admin_headers, json={})
    assert response.status_code in {404, 405}
```

（路径不再匹配任何路由时返回 404；路径与其它方法的路由重合时（如 `POST /data-packages/adjust` 撞上 `GET /data-packages/{id}`）返回 405，两者都表示已删除。）

- [ ] **Step 3: 运行，确认失败**

Run: `$PYTEST backend/tests/test_collection_tasks_api.py backend/tests/test_collection_packages_api.py`
Expected: FAIL（任务仍建包；旧接口仍存在）

- [ ] **Step 4: 删除服务与路由代码**

按本任务 **Files** 列表删除。`create_collection_task` 改为：

```python
def create_collection_task(
    db: Session,
    *,
    workspace_id: int,
    collection_project_id: int,
    name: str,
    target_duration_hours: Decimal,
    description: str = "",
    sop_text: str = "",
    device_model_id: int | None = None,
    label_ids: list[int],
    created_by_user_id: int | None,
) -> CollectionTask:
    """Create an offline collection task; packages are opened later per collector and day."""
    _require_enabled_project(
        db,
        workspace_id=workspace_id,
        collection_project_id=collection_project_id,
    )
    _validate_device_model(db, device_model_id)
    labels = _active_labels(db, label_ids)
    task = CollectionTask(
        workspace_id=workspace_id,
        collection_project_id=collection_project_id,
        name=normalized_name(name),
        description=description.strip(),
        target_duration_hours=target_duration_hours,
        capture_mode="offline",
        sop_text=sop_text.strip(),
        device_model_id=device_model_id,
        created_by_user_id=created_by_user_id,
    )
    db.add(task)
    db.flush()
    db.add_all(
        [
            CollectionTaskLabel(collection_task_id=task.id, collection_label_id=label.id)
            for label in labels
        ]
    )
    db.flush()
    return task
```

`routers/collection_tasks.py` 的 `create_collection_task` 路由删去 `default_package_duration_hours=body.default_package_duration_hours`；返回的 `_task_item(...)` 保持 `pending_assignment_count=len(packages)`（Task 3 统一改字段）。

- [ ] **Step 5: 修测试夹具与其余引用**

`collection_api_fixtures.py`：删 `assign_data_package` 导入；`make_task` 删 `package_hours` 参数与 `default_package_duration_hours=`；`make_assigned_package` 改为直接建行：

```python
def make_assigned_package(
    db: Session,
    workspace: Workspace,
    project: CollectionProject,
    *,
    hours: str = "2.00",
) -> DataPackage:
    device = make_device_model(db)
    task = create_collection_task(
        db,
        workspace_id=workspace.id,
        collection_project_id=project.id,
        name=f"t-{uuid4().hex[:8]}",
        target_duration_hours=Decimal(hours),
        device_model_id=device.id,
        label_ids=[],
        created_by_user_id=None,
    )
    owner = make_collector(db, workspace, name="Owner")
    package = DataPackage(
        package_uid=f"pkg_{uuid4().hex}",
        workspace_id=workspace.id,
        collection_project_id=project.id,
        collection_task_id=task.id,
        status="assigned",
        target_duration_hours=Decimal(hours),
        responsible_collector_id=owner.id,
        operator_collector_id=owner.id,
        assigned_at=datetime.utcnow(),
    )
    db.add(package)
    db.commit()
    db.refresh(package)
    return package
```

（文件顶部补 `from datetime import datetime`。）

然后找出其余引用并逐个修正：

```bash
grep -rnE "default_package_duration_hours|package_hours=|split_package_durations|assign_data_package|adjust_pending_packages|create_supplement_package|offline_manifest|offline-manifest|/supplements|batch-assign|/adjust" backend/tests backend/data backend/scripts | grep -v __pycache__
```

处理规则：
- 测试里构造任务时传 `default_package_duration_hours` → 删除该实参。
- 依赖「建任务后自动出包」的测试 → 改用 `make_assigned_package`，或在测试内直接建 `DataPackage` 行（同上字段）。
- 只为验证分配／调整／补采／离线清单的测试 → 整个删除。
- `backend/scripts/seed_dev.py` 暂不动（Task 3 重写）。

- [ ] **Step 6: 运行受影响测试**

Run: `$PYTEST backend/tests/test_collection_tasks_api.py backend/tests/test_collection_packages_api.py backend/tests/test_collection_management_smoke.py backend/tests/test_external_tool_e2e.py backend/tests/test_collection_upload_sessions_api.py backend/tests/test_collection_overview_api.py backend/tests/test_fetch_manifests_api.py backend/tests/test_collection_intake_desensitization.py`
Expected: PASS

- [ ] **Step 7: 全量后端测试 + lint**

Run: `$PYTEST backend/tests` 与 `.venv/bin/ruff check backend`
Expected: PASS（`slow` 标记的用例可跳过）

- [ ] **Step 8: 提交**

```bash
git add -A backend
git commit -m "refactor(collection): retire package assignment, adjustment, supplements and manifests"
```

---

### Task 3: 表结构切换（`0012_daily_packages`）与读取方改造

**Files:**
- Create: `backend/alembic/versions/0012_daily_packages.py`
- Create: `backend/data/services/package_views.py`
- Modify: `backend/data/models/data_package.py`、`backend/data/models/collection_core.py`
- Modify: `backend/data/services/collection_tasks.py`、`backend/data/routers/collection_tasks.py`
- Modify: `backend/data/services/collection_packages.py`、`backend/data/routers/collection_packages.py`
- Modify: `backend/data/services/collection_upload_sessions.py:21`, `:290-292`
- Modify: `backend/data/services/collection_dashboard.py`、`collection_overview.py`、`data_batches.py`、`routers/data_batches.py`、`fetch_manifests.py`、`package_list_projection.py`
- Modify: `backend/scripts/seed_dev.py`
- Modify: `backend/tests/collection_api_fixtures.py` 及 Step 8 列出的测试
- Test: `backend/tests/test_daily_packages_migration.py`（新）、`backend/tests/test_daily_package_model.py`（新）

**Interfaces:**
- Produces:
  - `DataPackage.collector_id: int | None`、`DataPackage.collector`（relationship `PersonnelProfile`）、`capture_date: date | None`、`sequence_no: int | None`、`sealed_at: datetime | None`、`opened_at: datetime | None`
  - `CollectionTask.status: str`（`"active"`/`"closed"`）、`closed_at`、`closed_by_user_id`；常量 `COLLECTION_TASK_STATUSES = ("active", "closed")`
  - `DATA_PACKAGE_STATUSES`（见 Global Constraints）、`DATA_PACKAGE_UPLOADABLE_STATUSES = frozenset({"open", "parse_failed", "pending_intake_review"})`（定义在 `data_package.py`）
  - `package_views.daily_package_fields(package: DataPackage) -> dict[str, object]`
  - 测试夹具 `make_open_package(db, workspace, project, *, hours="2.00", capture_date=date(1970, 1, 1), collector=None) -> DataPackage`（取代 `make_assigned_package`）
  - `list_data_packages(..., collector_id: int | None = None, capture_date_from: date | None = None, capture_date_to: date | None = None, ...)`（`operator_collector_id` 参数删除）

- [ ] **Step 1: 写迁移测试**

`backend/tests/test_daily_packages_migration.py`（在独立库上从 `0011` 升级到 `0012`）：

```python
"""0012 converts pre-split packages into daily packages."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url

ROOT = Path(__file__).resolve().parents[2]


def _config(url: URL) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "backend" / "alembic"))
    config.set_main_option("sqlalchemy.url", url.render_as_string(hide_password=False))
    return config


def _database_url() -> URL:
    base = make_url(os.environ["TEST_DATABASE_URL"])
    name = f"{base.database}_daily_migration"
    if not re.fullmatch(r"quicdata_test[a-z0-9_]*", name):
        raise RuntimeError("migration test database name is not approved")
    return base.set(database=name)


def _recreate(url: URL, *, create: bool) -> None:
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)'))
            if create:
                connection.execute(text(f'CREATE DATABASE "{url.database}"'))
    finally:
        admin.dispose()


@pytest.fixture
def legacy_database():
    url = _database_url()
    _recreate(url, create=True)
    command.upgrade(_config(url), "0011_package_dashboard_facts")
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO workspaces (id, name, description, creator, realtime_version, created_at) "
            "VALUES (900, 'mig', '', 'a@b.c', 0, NOW())"
        ))
        connection.execute(text(
            "INSERT INTO personnel_profiles (id, name, profile_key, is_active, created_at) "
            "VALUES (901, 'Zhang', '901', TRUE, NOW())"
        ))
        connection.execute(text(
            "INSERT INTO workspace_personnel_profiles (workspace_id, personnel_profile_id, created_at) "
            "VALUES (900, 901, NOW())"
        ))
        connection.execute(text(
            "INSERT INTO collection_projects (id, workspace_id, name) VALUES (902, 900, 'p')"
        ))
        connection.execute(text(
            "INSERT INTO collection_tasks (id, workspace_id, collection_project_id, name, "
            "target_duration_hours, default_package_duration_hours, capture_mode) "
            "VALUES (903, 900, 902, 't', 10, 2, 'offline')"
        ))
    yield url, engine
    engine.dispose()
    _recreate(url, create=False)


def _package(connection, *, pid, status, captured=None, collector=True):
    connection.execute(
        text(
            "INSERT INTO data_packages (id, package_uid, workspace_id, collection_project_id, "
            "collection_task_id, status, target_duration_hours, responsible_collector_id, "
            "operator_collector_id, captured_started_at, qrdf_facts_json, parse_error_code, "
            "parse_error_message, supplement_reason, created_at, updated_at) "
            "VALUES (:id, :uid, 900, 902, 903, :status, 2, :collector, :collector, :captured, "
            "'{}', '', '', '', NOW(), NOW())"
        ),
        {
            "id": pid,
            "uid": f"pkg_mig_{pid}",
            "status": status,
            "collector": 901 if collector else None,
            "captured": captured,
        },
    )


def test_0012_voids_empty_and_backfills_daily_identity(legacy_database):
    url, engine = legacy_database
    with engine.begin() as connection:
        _package(connection, pid=1, status="pending_assignment", collector=False)
        _package(connection, pid=2, status="assigned")
        # 2026-09-29 15:30 UTC = 2026-09-29 23:30 Shanghai
        _package(connection, pid=3, status="pending_intake_review", captured="2026-09-29 15:30:00")
        # 2026-09-29 16:30 UTC = 2026-09-30 00:30 Shanghai
        _package(connection, pid=4, status="intake_approved", captured="2026-09-29 16:30:00")
        _package(connection, pid=5, status="intake_approved", captured="2026-09-29 18:00:00")

    command.upgrade(_config(url), "0012_daily_packages")

    with engine.connect() as connection:
        rows = {
            row.id: row
            for row in connection.execute(text(
                "SELECT id, status, collector_id, capture_date, sequence_no, opened_at "
                "FROM data_packages ORDER BY id"
            ))
        }
        task = connection.execute(text("SELECT status FROM collection_tasks WHERE id = 903")).one()
        columns = {
            row.column_name
            for row in connection.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'data_packages'"
            ))
        }
    assert rows[1].status == "voided" and rows[2].status == "voided"
    assert str(rows[3].capture_date) == "2026-09-29" and rows[3].sequence_no == 1
    assert str(rows[4].capture_date) == "2026-09-30" and rows[4].sequence_no == 1
    assert str(rows[5].capture_date) == "2026-09-30" and rows[5].sequence_no == 2
    assert {rows[3].collector_id, rows[4].collector_id, rows[5].collector_id} == {901}
    assert task.status == "active"
    assert {"responsible_collector_id", "operator_collector_id", "target_duration_hours",
            "supplement_for_package_id", "supplement_reason", "assigned_at"}.isdisjoint(columns)
    assert {"collector_id", "capture_date", "sequence_no", "sealed_at", "opened_at"} <= columns


def test_0012_aborts_when_a_package_with_data_has_no_capture_time(legacy_database):
    url, engine = legacy_database
    with engine.begin() as connection:
        _package(connection, pid=6, status="pending_intake_review", captured=None)
        connection.execute(text(
            "INSERT INTO package_intake_reviews (data_package_id, verdict) VALUES (6, 'approved')"
        ))
    with pytest.raises(Exception, match="capture time"):
        command.upgrade(_config(url), "0012_daily_packages")
```

`backend/tests/test_daily_package_model.py`（模型层约束）：

```python
"""Daily package identity constraints on the head schema."""

from datetime import date

import pytest
from collection_api_fixtures import make_collector, make_open_package, make_project, make_workspace
from sqlalchemy.exc import IntegrityError

from data.models.data_package import DataPackage


def test_open_package_requires_daily_identity(db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package = make_open_package(db_session, workspace, project)
    package.capture_date = None
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_daily_identity_is_unique(db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    collector = make_collector(db_session, workspace)
    first = make_open_package(
        db_session, workspace, project, collector=collector, capture_date=date(2026, 9, 30)
    )
    duplicate = DataPackage(
        package_uid="pkg_duplicate_identity",
        workspace_id=workspace.id,
        collection_project_id=project.id,
        collection_task_id=first.collection_task_id,
        status="open",
        collector_id=collector.id,
        capture_date=date(2026, 9, 30),
        sequence_no=first.sequence_no,
    )
    db_session.add(duplicate)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
```

- [ ] **Step 2: 运行，确认失败**

Run: `$PYTEST backend/tests/test_daily_packages_migration.py backend/tests/test_daily_package_model.py`
Expected: FAIL（`Can't locate revision 0012_daily_packages`；`make_open_package` 不存在）

- [ ] **Step 3: 写迁移**

`backend/alembic/versions/0012_daily_packages.py`：

```python
"""Collection packages are opened per collector and capture day.

One-way cutover: empty pre-split packages are voided, packages with data get
their collector, capture day (Asia/Shanghai) and per-day sequence backfilled,
and the assignment, target and supplement columns are dropped.
"""

import sqlalchemy as sa
from alembic import op

revision = "0012_daily_packages"
down_revision = "0011_package_dashboard_facts"
branch_labels = None
depends_on = None

_STATUSES = (
    "open",
    "pending_upload",
    "uploading",
    "parsing",
    "ingested",
    "pending_intake_review",
    "intake_approved",
    "batched",
    "governing",
    "published",
    "parse_failed",
    "voided",
)


def upgrade():
    op.add_column(
        "collection_tasks",
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
    )
    op.add_column("collection_tasks", sa.Column("closed_at", sa.DateTime(), nullable=True))
    op.add_column(
        "collection_tasks",
        sa.Column("closed_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
    )
    op.create_check_constraint(
        "ck_collection_tasks_status", "collection_tasks", "status IN ('active', 'closed')"
    )
    op.drop_constraint(
        "ck_collection_tasks_default_package_duration", "collection_tasks", type_="check"
    )
    op.drop_column("collection_tasks", "default_package_duration_hours")

    op.add_column(
        "data_packages",
        sa.Column(
            "collector_id", sa.Integer(), sa.ForeignKey("personnel_profiles.id"), nullable=True
        ),
    )
    op.add_column("data_packages", sa.Column("capture_date", sa.Date(), nullable=True))
    op.add_column("data_packages", sa.Column("sequence_no", sa.Integer(), nullable=True))
    op.add_column("data_packages", sa.Column("sealed_at", sa.DateTime(), nullable=True))
    for name in (
        "ck_data_packages_status",
        "ck_data_packages_assigned_requires_collectors",
        "ck_data_packages_target_duration",
    ):
        op.drop_constraint(name, "data_packages", type_="check")

    # Empty packages: no capture facts, no source Episode, never reviewed.
    op.execute(
        """
        UPDATE data_packages p SET status = 'voided', updated_at = NOW()
        WHERE p.status <> 'voided'
          AND p.captured_started_at IS NULL
          AND NOT EXISTS (
            SELECT 1 FROM episodes e WHERE e.data_package_id = p.id AND e.kind = 'source'
          )
          AND NOT EXISTS (
            SELECT 1 FROM package_intake_reviews r WHERE r.data_package_id = p.id
          )
        """
    )
    op.execute(
        "UPDATE data_packages "
        "SET collector_id = COALESCE(operator_collector_id, responsible_collector_id)"
    )
    op.execute(
        """
        UPDATE data_packages
        SET capture_date = (captured_started_at AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Shanghai')::date
        WHERE status <> 'voided' AND captured_started_at IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE data_packages p SET capture_date = earliest.day
        FROM (
            SELECT e.data_package_id,
                   (to_timestamp(min((e.metadata_json -> 'timing' ->> 'start_timestamp_ns')::numeric)
                                 / 1000000000) AT TIME ZONE 'Asia/Shanghai')::date AS day
            FROM episodes e
            WHERE e.kind = 'source'
              AND e.data_package_id IS NOT NULL
              AND (e.metadata_json -> 'timing' ->> 'start_timestamp_ns') ~ '^[0-9]+$'
            GROUP BY e.data_package_id
        ) earliest
        WHERE p.id = earliest.data_package_id AND p.status <> 'voided' AND p.capture_date IS NULL
        """
    )
    missing = op.get_bind().execute(
        sa.text(
            "SELECT id FROM data_packages WHERE status <> 'voided' "
            "AND (collector_id IS NULL OR capture_date IS NULL) ORDER BY id"
        )
    ).scalars().all()
    if missing:
        raise RuntimeError(
            "packages with data have no collector or capture time; fix them manually: "
            + ", ".join(str(package_id) for package_id in missing)
        )
    op.execute(
        """
        UPDATE data_packages p SET sequence_no = ranked.seq
        FROM (
            SELECT id, row_number() OVER (
                PARTITION BY collection_task_id, collector_id, capture_date ORDER BY id
            ) AS seq
            FROM data_packages WHERE status <> 'voided'
        ) ranked
        WHERE p.id = ranked.id
        """
    )

    op.alter_column("data_packages", "assigned_at", new_column_name="opened_at")
    for column in (
        "responsible_collector_id",
        "operator_collector_id",
        "target_duration_hours",
        "supplement_for_package_id",
        "supplement_reason",
    ):
        op.drop_column("data_packages", column)

    statuses = ", ".join(f"'{status}'" for status in _STATUSES)
    op.create_check_constraint("ck_data_packages_status", "data_packages", f"status IN ({statuses})")
    op.create_check_constraint(
        "ck_data_packages_daily_identity",
        "data_packages",
        "status = 'voided' OR (collector_id IS NOT NULL AND capture_date IS NOT NULL "
        "AND sequence_no IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_data_packages_sequence_positive",
        "data_packages",
        "sequence_no IS NULL OR sequence_no >= 1",
    )
    op.create_unique_constraint(
        "uq_data_packages_daily_identity",
        "data_packages",
        ["collection_task_id", "collector_id", "capture_date", "sequence_no"],
    )
    op.create_index(
        "ix_data_packages_workspace_capture_date", "data_packages", ["workspace_id", "capture_date"]
    )


def downgrade():
    raise NotImplementedError("0012_daily_packages is a one-way cutover")
```

说明：规格 §9 第 3 步「没有任何数据的包」在此按「没有采集时间、没有源 Episode、没有审核结论」判定，比「待分配/已分配」更宽，也覆盖了 `pending_upload`、`uploading` 中尚无数据的包。

- [ ] **Step 4: 改模型**

`backend/data/models/data_package.py`：

```python
DATA_PACKAGE_STATUSES = (
    "open",
    "pending_upload",
    "uploading",
    "parsing",
    "ingested",
    "pending_intake_review",
    "intake_approved",
    "batched",
    "governing",
    "published",
    "parse_failed",
    "voided",
)
DATA_PACKAGE_TERMINAL_STATUSES = frozenset({"parse_failed", "voided"})
DATA_PACKAGE_UPLOADABLE_STATUSES = frozenset({"open", "parse_failed", "pending_intake_review"})
```

`DataPackage` 类文档改为「按 任务 × 采集员 × 采集日 × 序号 开出的采集交付单元」。`__table_args__` 中：
- 删除 `ck_data_packages_target_duration` 与 `ck_data_packages_assigned_requires_collectors`；
- 新增：

```python
        CheckConstraint(
            "status = 'voided' OR (collector_id IS NOT NULL AND capture_date IS NOT NULL "
            "AND sequence_no IS NOT NULL)",
            name="ck_data_packages_daily_identity",
        ),
        CheckConstraint(
            "sequence_no IS NULL OR sequence_no >= 1", name="ck_data_packages_sequence_positive"
        ),
        UniqueConstraint(
            "collection_task_id",
            "collector_id",
            "capture_date",
            "sequence_no",
            name="uq_data_packages_daily_identity",
        ),
        Index("ix_data_packages_workspace_capture_date", "workspace_id", "capture_date"),
```

列：`status` 默认改为 `"open"`；删 `target_duration_hours`、`responsible_collector_id`、`operator_collector_id`、`supplement_for_package_id`、`supplement_reason`；`assigned_at` 改名 `opened_at`；新增：

```python
    collector_id = Column(Integer, ForeignKey("personnel_profiles.id"), nullable=True)
    capture_date = Column(Date, nullable=True)
    sequence_no = Column(Integer, nullable=True)
    sealed_at = Column(DateTime, nullable=True)
```

关系：删 `responsible_collector`、`operator_collector`，新增 `collector = relationship("PersonnelProfile", foreign_keys=[collector_id])`。导入补 `Date`。

`backend/data/models/collection_core.py` 的 `CollectionTask`：删 `default_package_duration_hours` 列与 `ck_collection_tasks_default_package_duration`；新增

```python
COLLECTION_TASK_STATUSES = ("active", "closed")
```

（模块级）与

```python
        CheckConstraint("status IN ('active', 'closed')", name="ck_collection_tasks_status"),
```

以及列

```python
    status = Column(String(16), nullable=False, default="active", server_default="active")
    closed_at = Column(DateTime, nullable=True)
    closed_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
```

类文档改为「目标时长是唯一目标；包在采集时按采集员和采集日开出」。

- [ ] **Step 5: 统一包序列化**

新建 `backend/data/services/package_views.py`：

```python
"""Serialized daily-package identity shared by every package-returning API."""

from __future__ import annotations

from data.models.data_package import DataPackage
from data.utils.formatting import format_api_datetime


def daily_package_fields(package: DataPackage) -> dict[str, object]:
    collector = package.collector
    return {
        "collector_id": package.collector_id,
        "collector": (
            {"id": collector.id, "name": collector.name, "profile_key": collector.profile_key}
            if collector is not None
            else None
        ),
        "capture_date": package.capture_date.isoformat() if package.capture_date else None,
        "sequence_no": package.sequence_no,
        "sealed_at": format_api_datetime(package.sealed_at),
        "opened_at": format_api_datetime(package.opened_at),
    }
```

`routers/collection_packages.py` 的 `_package_item`：删 `target_duration_hours`、`collector_id`、`created_by_*` 之外的旧采集员字段（`responsible_collector_id`、`operator_collector_id`）、`assigned_at`，改为在字典里展开 `**daily_package_fields(package)`（保留 `created_by_user_id`、`created_by_name`）。`routers/collection_tasks.py` 的 `_package_item` 同样处理并删 `target_duration_hours`；`list_collection_task_packages` 路由里额外叠加的 `responsible_collector_id`/`operator_collector_id`/`collector_id`/`assigned_at` 一并删除，直接返回 `_package_item(package)`。

- [ ] **Step 6: 改任务列表与其它读取方**

`services/collection_tasks.py` 的 `list_collection_tasks_with_progress` 改为返回 `list[tuple[CollectionTask, int, Decimal]]`（任务、包数、有效时长），删除 `assigned_count` 与 `pending_assignment_count` 两个聚合；`routers/collection_tasks.py` 的 `_task_item` 删参数 `assigned_count`、`pending_assignment_count` 与字段 `default_package_duration_hours`、`assigned_count`、`pending_assignment_count`，`_progress_for_task` 与三个调用处同步改为三元组。

其余读取方：

| 文件 | 改法 |
|---|---|
| `services/collection_packages.py` `list_data_packages` | 参数 `operator_collector_id` → `collector_id`（`DataPackage.collector_id == collector_id`）；新增 `capture_date_from`/`capture_date_to`（`>=` / `<=`）；`void_data_package` 可作废状态改为 `("open", "pending_upload")` |
| `routers/collection_packages.py` `list_data_packages` | 查询参数同上改名与新增（`capture_date_from: date | None = Query(default=None)`） |
| `services/collection_upload_sessions.py` | `UPLOADABLE_PACKAGE_STATUSES = DATA_PACKAGE_UPLOADABLE_STATUSES`；第 290、292 行默认值 `"assigned"` → `"open"` |
| `services/collection_dashboard.py` | `status_counts` 改为 `{"open": open_count, "other": total - open_count - voided, "voided": voided}`（聚合里 `pending_assignment`/`assigned` 两个 `case` 改为一个 `status == "open"`）；`operator_collector_id` 全部改 `collector_id` |
| `services/collection_overview.py` | `package_counts` 改为 `{"open", "voided", "other"}`，`other` 为非 `open`、非 `voided` |
| `services/data_batches.py` | `responsible_collector_id`/`operator_collector_id` 两个筛选合并为 `collector_id` |
| `routers/data_batches.py` | 查询参数合并为 `collector_id`；包序列化删 `target_duration_hours`、两个旧采集员字段，展开 `**daily_package_fields(package)` |
| `services/fetch_manifests.py:207` | `package.operator_collector_id` → `package.collector_id` |
| `services/package_list_projection.py:107`, `:221` | `DataPackage.operator_collector` → `DataPackage.collector`，`package.operator_collector` → `package.collector` |

- [ ] **Step 7: 重写演示数据**

`backend/scripts/seed_dev.py` 的采集概览段：任务删 `default_package_duration_hours`；`packages` 列表去掉 `"target"`，状态 `pending_assignment` 的条目删除，`assigned` 改为 `open`；建行改为

```python
        opened = item["status"] != "voided"
        db.add(
            DataPackage(
                package_uid=item["uid"],
                workspace_id=workspace.id,
                collection_project_id=project.id,
                collection_task_id=task.id,
                status=item["status"],
                collector_id=(profile_id if opened else None),
                capture_date=(started + timedelta(hours=8)).date() if opened else None,
                sequence_no=1 if opened else None,
                opened_at=(started if opened else None),
                captured_duration_hours=(captured / Decimal("3600") if captured is not None else None),
                intake_valid_duration_hours=(valid / Decimal("3600") if valid is not None else None),
                governed_valid_duration_hours=(valid / Decimal("3600") if valid is not None else None),
                captured_started_at=(started if captured is not None else None),
                captured_duration_s=captured,
                captured_size_bytes=item["size"],
                intake_valid_duration_s=valid,
                intake_valid_size_bytes=(
                    int(item["size"] * 0.8) if item["size"] is not None and valid is not None else None
                ),
                collection_device_id=(device_id if opened else None),
                upload_completed_at=(started + timedelta(hours=1) if captured is not None else None),
                created_at=started,
                updated_at=started,
                created_by_user_id=collector_id,
            )
        )
```

各条目 `days` 互不相同，因此 `(task, collector, capture_date, 1)` 不冲突。重跑分支里 `row.assigned_at` 改为 `row.opened_at`。

- [ ] **Step 8: 夹具与既有测试**

`collection_api_fixtures.py`：把 `make_assigned_package` 改名为 `make_open_package` 并改为：

```python
def make_open_package(
    db: Session,
    workspace: Workspace,
    project: CollectionProject,
    *,
    hours: str = "2.00",
    capture_date: date = date(1970, 1, 1),
    collector: PersonnelProfile | None = None,
) -> DataPackage:
    """An open daily package. The default capture day matches ``start_ns=0`` in Asia/Shanghai."""
    device = make_device_model(db)
    task = create_collection_task(
        db,
        workspace_id=workspace.id,
        collection_project_id=project.id,
        name=f"t-{uuid4().hex[:8]}",
        target_duration_hours=Decimal(hours),
        device_model_id=device.id,
        label_ids=[],
        created_by_user_id=None,
    )
    owner = collector or make_collector(db, workspace, name="Owner")
    package = DataPackage(
        package_uid=f"pkg_{uuid4().hex}",
        workspace_id=workspace.id,
        collection_project_id=project.id,
        collection_task_id=task.id,
        status="open",
        collector_id=owner.id,
        capture_date=capture_date,
        sequence_no=1,
        opened_at=datetime.utcnow(),
    )
    db.add(package)
    db.commit()
    db.refresh(package)
    return package
```

（导入补 `from datetime import date, datetime`。`seed_package_pending_intake_review` 内部调用改为 `make_open_package`。）

全仓替换夹具名，再逐个修字段引用：

```bash
grep -rl "make_assigned_package" backend/tests | xargs sed -i '' 's/make_assigned_package/make_open_package/g'
grep -rnE "responsible_collector|operator_collector|pending_assignment|\"assigned\"|assigned_at|target_duration_hours=|default_package_duration|supplement_" backend/tests | grep -v __pycache__
```

处理规则：
- 断言或构造里的 `responsible_collector_id`/`operator_collector_id` → `collector_id`；`assigned_at` → `opened_at`。
- 包状态 `"assigned"`、`"pending_assignment"` → `"open"`（只改数据包状态；标注工单等其它模型的 `"assigned"` 不动——看清所在上下文）。
- 直接构造 `DataPackage(...)` 且缺 `collector_id/capture_date/sequence_no` 的非作废包 → 补齐（同一任务同一人同一天的多个包用递增 `sequence_no`）。
- 包上的 `target_duration_hours=` 实参 → 删除；任务上的保留。
- 看板、概览测试中 `status_counts`/`package_counts` 的键 → 按 Step 6 新键。

- [ ] **Step 9: 运行新测试与全量**

Run: `$PYTEST backend/tests/test_daily_packages_migration.py backend/tests/test_daily_package_model.py`
Expected: PASS

Run: `$PYTEST backend/tests` 与 `.venv/bin/ruff check backend`
Expected: PASS

- [ ] **Step 10: 提交**

```bash
git add -A backend
git commit -m "feat(collection): switch packages to per-collector daily identity"
```

---

### Task 4: 开包接口

**Files:**
- Create: `backend/data/services/daily_packages.py`
- Modify: `backend/data/routers/collection_tasks.py`
- Test: `backend/tests/test_daily_package_open_api.py`（新）

**Interfaces:**
- Consumes: `resolve_workspace_collector`（Task 1）；`DataPackage.collector_id/capture_date/sequence_no/sealed_at/opened_at`、`CollectionTask.status`、`daily_package_fields`（Task 3）
- Produces（`data/services/daily_packages.py`）：
  - `CAPTURE_TIMEZONE: ZoneInfo`
  - `capture_date_of(start_ns: int) -> date`
  - `today_in_capture_zone(now: datetime | None = None) -> date`
  - `class DailyPackageError(ValueError)`，属性 `code: str`、`status_code: int`
  - `@dataclass(frozen=True) class OpenedPackage: package: DataPackage; created: bool`
  - `open_collection_package(db, *, workspace_id: int, task_id: int, collector_identifier: str, capture_date: date, actor_id: int | None, now: datetime | None = None) -> OpenedPackage`
  - `appendable_package_conditions() -> tuple`（SQL 条件：未作废、未封口、未冻结）
- Produces（HTTP）：`POST /api/v1/collection-tasks/{task_id}/packages/open`，请求模型 `OpenPackageRequest{workspace_id:int, collector_identifier:str, capture_date:str}`，响应 `data` = 包字段 + `data_package_id` + `created`

- [ ] **Step 1: 写失败的测试**

`backend/tests/test_daily_package_open_api.py`：

```python
"""Open (or reuse) the daily package of one collector."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

import pytest
from collection_api_fixtures import make_collector, make_project, make_task, make_workspace

from data.database import SessionLocal
from data.models.data_package import DataPackage, PackageIntakeReview
from data.services.daily_packages import capture_date_of, open_collection_package

DAY = "2026-09-30"


def _open(client, headers, task, identifier, day=DAY):
    return client.post(
        f"/api/v1/collection-tasks/{task.id}/packages/open",
        headers=headers,
        json={
            "workspace_id": task.workspace_id,
            "collector_identifier": identifier,
            "capture_date": day,
        },
    )


@pytest.fixture
def scene(db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    task = make_task(db_session, project)
    collector = make_collector(db_session, workspace)
    return workspace, project, task, collector


def test_capture_date_of_uses_shanghai_midnight():
    before = int(datetime(2026, 9, 29, 15, 59, 59, tzinfo=timezone.utc).timestamp()) * 10**9
    after = int(datetime(2026, 9, 29, 16, 0, 0, tzinfo=timezone.utc).timestamp()) * 10**9
    assert capture_date_of(before) == date(2026, 9, 29)
    assert capture_date_of(after) == date(2026, 9, 30)
    assert capture_date_of(0) == date(1970, 1, 1)


def test_open_creates_first_package(client, admin_headers, db_session, scene):
    _workspace, _project, task, collector = scene
    response = _open(client, admin_headers, task, collector.profile_key)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["created"] is True
    assert data["status"] == "open"
    assert data["sequence_no"] == 1
    assert data["capture_date"] == DAY
    assert data["collector"]["id"] == collector.id
    assert data["data_package_id"] == data["id"]
    assert data["package_uid"].startswith("pkg_")


def test_open_reuses_appendable_package(client, admin_headers, scene):
    _workspace, _project, task, collector = scene
    first = _open(client, admin_headers, task, collector.profile_key).json()["data"]
    again = _open(client, admin_headers, task, f"00{collector.profile_key}").json()["data"]
    assert again["created"] is False
    assert again["package_uid"] == first["package_uid"]


def test_open_after_intake_review_creates_next_sequence(client, admin_headers, db_session, scene):
    _workspace, _project, task, collector = scene
    first = _open(client, admin_headers, task, collector.profile_key).json()["data"]
    db_session.add(PackageIntakeReview(data_package_id=first["id"], verdict="approved"))
    db_session.commit()
    second = _open(client, admin_headers, task, collector.profile_key).json()["data"]
    assert second["created"] is True
    assert second["sequence_no"] == 2


def test_open_after_seal_creates_next_sequence(client, admin_headers, db_session, scene):
    _workspace, _project, task, collector = scene
    first = _open(client, admin_headers, task, collector.profile_key).json()["data"]
    package = db_session.get(DataPackage, first["id"])
    package.sealed_at = datetime.utcnow()
    db_session.commit()
    second = _open(client, admin_headers, task, collector.profile_key).json()["data"]
    assert second["sequence_no"] == 2


def test_voided_sequence_is_not_reused(client, admin_headers, db_session, scene):
    _workspace, _project, task, collector = scene
    first = _open(client, admin_headers, task, collector.profile_key).json()["data"]
    package = db_session.get(DataPackage, first["id"])
    package.status = "voided"
    db_session.commit()
    second = _open(client, admin_headers, task, collector.profile_key).json()["data"]
    assert second["sequence_no"] == 2


def test_other_day_and_other_collector_get_their_own_packages(
    client, admin_headers, db_session, scene
):
    workspace, _project, task, collector = scene
    other = make_collector(db_session, workspace)
    a = _open(client, admin_headers, task, collector.profile_key).json()["data"]
    b = _open(client, admin_headers, task, collector.profile_key, "2026-09-29").json()["data"]
    c = _open(client, admin_headers, task, other.profile_key).json()["data"]
    assert len({a["package_uid"], b["package_uid"], c["package_uid"]}) == 3
    assert {a["sequence_no"], b["sequence_no"], c["sequence_no"]} == {1}


@pytest.mark.parametrize("identifier", ["999999999", "unknown", "0", "abc"])
def test_open_rejects_unmatched_collector(client, admin_headers, scene, identifier):
    _workspace, _project, task, _collector = scene
    response = _open(client, admin_headers, task, identifier)
    assert response.status_code == 422
    assert response.json()["detail"] == "collector_unmatched"


def test_open_rejects_inactive_or_foreign_collector(client, admin_headers, db_session, scene):
    _workspace, _project, task, collector = scene
    foreign = make_collector(db_session, make_workspace(db_session))
    assert _open(client, admin_headers, task, foreign.profile_key).json()["detail"] == (
        "collector_unmatched"
    )
    collector.is_active = False
    db_session.commit()
    assert _open(client, admin_headers, task, collector.profile_key).json()["detail"] == (
        "collector_unmatched"
    )


@pytest.mark.parametrize("day", ["2999-01-01", "2026-9-30", "not-a-date", "2026-02-30"])
def test_open_rejects_invalid_capture_date(client, admin_headers, scene, day):
    _workspace, _project, task, collector = scene
    response = _open(client, admin_headers, task, collector.profile_key, day)
    assert response.status_code == 422
    assert response.json()["detail"] == "capture_date_invalid"


def test_open_rejects_closed_task_and_archived_project(client, admin_headers, db_session, scene):
    _workspace, project, task, collector = scene
    task.status = "closed"
    db_session.commit()
    closed = _open(client, admin_headers, task, collector.profile_key)
    assert (closed.status_code, closed.json()["detail"]) == (409, "task_closed")
    task.status = "active"
    project.status = "archived"
    db_session.commit()
    archived = _open(client, admin_headers, task, collector.profile_key)
    assert (archived.status_code, archived.json()["detail"]) == (409, "project_archived")


def test_open_rejects_task_of_other_workspace(client, admin_headers, db_session, scene):
    workspace, _project, _task, collector = scene
    foreign_task = make_task(db_session, make_project(db_session, make_workspace(db_session)))
    response = client.post(
        f"/api/v1/collection-tasks/{foreign_task.id}/packages/open",
        headers=admin_headers,
        json={"workspace_id": workspace.id, "collector_identifier": collector.profile_key,
              "capture_date": DAY},
    )
    assert (response.status_code, response.json()["detail"]) == (404, "task_not_found")


def test_concurrent_open_creates_one_package(db_session, scene):
    workspace, _project, task, collector = scene

    def attempt(_):
        db = SessionLocal()
        try:
            opened = open_collection_package(
                db,
                workspace_id=workspace.id,
                task_id=task.id,
                collector_identifier=collector.profile_key,
                capture_date=date(2026, 9, 30),
                actor_id=None,
            )
            db.commit()
            return opened.package.package_uid
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        uids = list(pool.map(attempt, range(4)))
    assert len(set(uids)) == 1
    assert (
        db_session.query(DataPackage)
        .filter(DataPackage.collection_task_id == task.id, DataPackage.status == "open")
        .count()
        == 1
    )
```

- [ ] **Step 2: 运行，确认失败**

Run: `$PYTEST backend/tests/test_daily_package_open_api.py`
Expected: FAIL（`ModuleNotFoundError: data.services.daily_packages`）

- [ ] **Step 3: 实现服务**

`backend/data/services/daily_packages.py`：

```python
"""Daily collection packages: capture day, opening, sealing and declaration checks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from data.models.collection_core import CollectionProject, CollectionTask
from data.models.data_package import DataPackage, PackageIntakeReview
from data.services.capture_provenance import (
    canonical_collector_identifier,
    resolve_workspace_collector,
)

CAPTURE_TIMEZONE = ZoneInfo("Asia/Shanghai")


class DailyPackageError(ValueError):
    """A client-facing rule violation carrying its HTTP status and error code."""

    def __init__(self, code: str, status_code: int) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class OpenedPackage:
    package: DataPackage
    created: bool


def capture_date_of(start_ns: int) -> date:
    """The Asia/Shanghai calendar day an Episode starting at ``start_ns`` belongs to."""
    seconds = int(start_ns) // 1_000_000_000
    return datetime.fromtimestamp(seconds, tz=CAPTURE_TIMEZONE).date()


def today_in_capture_zone(now: datetime | None = None) -> date:
    return (now or datetime.now(timezone.utc)).astimezone(CAPTURE_TIMEZONE).date()


def appendable_package_conditions() -> tuple:
    frozen = exists().where(PackageIntakeReview.data_package_id == DataPackage.id)
    return (DataPackage.status != "voided", DataPackage.sealed_at.is_(None), ~frozen)


def open_collection_package(
    db: Session,
    *,
    workspace_id: int,
    task_id: int,
    collector_identifier: str,
    capture_date: date,
    actor_id: int | None,
    now: datetime | None = None,
) -> OpenedPackage:
    """Return the appendable package of this collector and day, or open the next one.

    The task row lock serialises concurrent opens of the same task, so the
    per-day sequence is allocated without gaps racing each other.
    """
    task = (
        db.query(CollectionTask)
        .filter(CollectionTask.id == task_id, CollectionTask.workspace_id == workspace_id)
        .with_for_update()
        .one_or_none()
    )
    if task is None:
        raise DailyPackageError("task_not_found", 404)
    project = db.get(CollectionProject, task.collection_project_id)
    if project is None or project.status != "enabled":
        raise DailyPackageError("project_archived", 409)
    if task.status != "active":
        raise DailyPackageError("task_closed", 409)
    if capture_date > today_in_capture_zone(now):
        raise DailyPackageError("capture_date_invalid", 422)
    collector = resolve_workspace_collector(
        db,
        workspace_id=workspace_id,
        identifier=canonical_collector_identifier(collector_identifier),
        require_active=True,
    )
    if collector is None:
        raise DailyPackageError("collector_unmatched", 422)

    same_day = (
        DataPackage.collection_task_id == task.id,
        DataPackage.collector_id == collector.id,
        DataPackage.capture_date == capture_date,
    )
    existing = db.scalar(
        select(DataPackage)
        .where(*same_day, *appendable_package_conditions())
        .order_by(DataPackage.sequence_no.desc())
        .limit(1)
    )
    if existing is not None:
        return OpenedPackage(existing, created=False)
    last = db.scalar(select(func.max(DataPackage.sequence_no)).where(*same_day)) or 0
    package = DataPackage(
        package_uid=f"pkg_{uuid4().hex}",
        workspace_id=workspace_id,
        collection_project_id=task.collection_project_id,
        collection_task_id=task.id,
        status="open",
        collector_id=collector.id,
        capture_date=capture_date,
        sequence_no=last + 1,
        opened_at=datetime.utcnow(),
        created_by_user_id=actor_id,
    )
    db.add(package)
    db.flush()
    return OpenedPackage(package, created=True)
```

- [ ] **Step 4: 实现路由**

`backend/data/routers/collection_tasks.py`：导入 `from datetime import date`、`from data.services.daily_packages import DailyPackageError, open_collection_package`、`from data.services.package_views import daily_package_fields`（若 `_package_item` 已展开则不需重复）。新增：

```python
class OpenPackageRequest(BaseModel):
    """Open or reuse the daily package of one collector."""

    model_config = ConfigDict(extra="forbid")

    workspace_id: int = Field(gt=0)
    collector_identifier: str = Field(min_length=1, max_length=128)
    capture_date: str = Field(min_length=1, max_length=32)


def _parse_capture_date(value: str) -> date:
    try:
        if len(value) != 10:
            raise ValueError(value)
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="capture_date_invalid") from exc


@router.post("/{task_id}/packages/open")
def open_task_package(
    task_id: int,
    body: OpenPackageRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Return the appendable daily package of a collector, opening the next one when needed."""
    require_permission(user, "workspace:write")
    _require_workspace(db, user=user, workspace_id=body.workspace_id)
    capture_date = _parse_capture_date(body.capture_date)
    try:
        opened = open_collection_package(
            db,
            workspace_id=body.workspace_id,
            task_id=task_id,
            collector_identifier=body.collector_identifier,
            capture_date=capture_date,
            actor_id=_actor_id(user),
        )
        db.commit()
    except DailyPackageError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc
    package = opened.package
    db.refresh(package)
    if opened.created:
        emit_audit_event(
            "collection.package.open",
            actor=str(user.get("email") or ""),
            resource=f"data_package:{package.id}",
            detail={
                "workspace_id": body.workspace_id,
                "collection_task_id": task_id,
                "collector_id": package.collector_id,
                "capture_date": package.capture_date.isoformat(),
                "sequence_no": package.sequence_no,
            },
        )
    return success({**_package_item(package), "data_package_id": package.id, "created": opened.created})
```

注意：该路由必须写在 `@router.get("/{task_id}")` 之前或之后都可（方法不同），但不要与其它 `POST /{task_id}/...` 冲突。

- [ ] **Step 5: 运行，确认通过**

Run: `$PYTEST backend/tests/test_daily_package_open_api.py`
Expected: PASS

- [ ] **Step 6: 文档**

`docs/COLLECTION_UPLOAD_API.md` 开头「采集包必须已经分配，不能由上传会话自动创建」一句改为：

```markdown
上传前先为「任务 × 采集员 × 采集日」开包：`POST /collection-tasks/{task_id}/packages/open`，
请求 `{"workspace_id", "collector_identifier"（metadata.operator.id 原值）, "capture_date"（YYYY-MM-DD，Asia/Shanghai）}`，
返回 `package_uid`、`sequence_no`、`created` 等；同一组合有未冻结、未封口的包时返回该包。
错误码见 `detail`：`task_not_found`、`task_closed`、`project_archived`、`capture_date_invalid`、`collector_unmatched`。
上传会话不创建业务包；归档项目不接受新上传会话。
```

- [ ] **Step 7: 提交**

```bash
git add backend/data/services/daily_packages.py backend/data/routers/collection_tasks.py backend/tests/test_daily_package_open_api.py docs/COLLECTION_UPLOAD_API.md
git commit -m "feat(collection): open daily packages per collector and capture day"
```

---

### Task 5: 封口、任务结束与重新开启、任务列表字段

**Files:**
- Modify: `backend/data/services/daily_packages.py`（加 `seal_data_package`）
- Modify: `backend/data/services/collection_tasks.py`（加 `set_collection_task_status`；列表加 `status` 筛选）
- Modify: `backend/data/routers/collection_packages.py`（`POST /{id}/seal`）
- Modify: `backend/data/routers/collection_tasks.py`（`POST /{id}/close`、`/{id}/reopen`；列表 `status` 参数与字段）
- Test: `backend/tests/test_daily_package_lifecycle_api.py`（新）

**Interfaces:**
- Consumes: `DailyPackageError`（Task 4）
- Produces:
  - `seal_data_package(db, *, workspace_id: int, data_package_id: int, now: datetime | None = None) -> DataPackage`
  - `set_collection_task_status(db, *, workspace_id: int, task_id: int, status: Literal["active", "closed"], actor_id: int | None) -> CollectionTask`
  - `list_collection_tasks_with_progress(db, *, workspace_id, collection_project_id=None, status: str | None = None)`
  - HTTP：`POST /api/v1/data-packages/{id}/seal`（body `{workspace_id}`）；`POST /api/v1/collection-tasks/{id}/close`、`/reopen`（body `{workspace_id}`）；`GET /api/v1/collection-tasks?status=active|closed`；任务项新增 `status`、`closed_at`、`target_reached`

- [ ] **Step 1: 写失败的测试**

`backend/tests/test_daily_package_lifecycle_api.py`：

```python
"""Sealing packages and closing tasks."""

from decimal import Decimal

from collection_api_fixtures import (
    make_collector,
    make_open_package,
    make_project,
    make_task,
    make_workspace,
)

from data.models.data_package import DataPackage, PackageIntakeReview


def _seal(client, headers, package):
    return client.post(
        f"/api/v1/data-packages/{package.id}/seal",
        headers=headers,
        json={"workspace_id": package.workspace_id},
    )


def test_seal_is_idempotent(client, admin_headers, db_session):
    workspace = make_workspace(db_session)
    package = make_open_package(db_session, workspace, make_project(db_session, workspace))
    first = _seal(client, admin_headers, package)
    assert first.status_code == 200, first.text
    sealed_at = first.json()["data"]["sealed_at"]
    assert sealed_at is not None
    assert _seal(client, admin_headers, package).json()["data"]["sealed_at"] == sealed_at


def test_seal_frozen_package_succeeds_without_change(client, admin_headers, db_session):
    workspace = make_workspace(db_session)
    package = make_open_package(db_session, workspace, make_project(db_session, workspace))
    db_session.add(PackageIntakeReview(data_package_id=package.id, verdict="approved"))
    db_session.commit()
    response = _seal(client, admin_headers, package)
    assert response.status_code == 200
    assert response.json()["data"]["sealed_at"] is None


def test_seal_voided_package_is_rejected(client, admin_headers, db_session):
    workspace = make_workspace(db_session)
    package = make_open_package(db_session, workspace, make_project(db_session, workspace))
    package.status = "voided"
    db_session.commit()
    response = _seal(client, admin_headers, package)
    assert (response.status_code, response.json()["detail"]) == (409, "package_voided")


def test_sealed_package_still_accepts_upload_sessions(client, admin_headers, db_session):
    workspace = make_workspace(db_session)
    package = make_open_package(db_session, workspace, make_project(db_session, workspace))
    assert _seal(client, admin_headers, package).status_code == 200
    response = client.post(
        "/api/v1/upload-sessions",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "collection_project_id": package.collection_project_id,
            "package_uids": [package.package_uid],
            "upload_mode": "oss_multipart",
        },
    )
    assert response.status_code == 200, response.text


def test_close_and_reopen_task(client, admin_headers, db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    task = make_task(db_session, project)
    collector = make_collector(db_session, workspace)
    closed = client.post(
        f"/api/v1/collection-tasks/{task.id}/close",
        headers=admin_headers,
        json={"workspace_id": workspace.id},
    )
    assert closed.status_code == 200, closed.text
    assert closed.json()["data"]["status"] == "closed"
    assert closed.json()["data"]["closed_at"] is not None
    blocked = client.post(
        f"/api/v1/collection-tasks/{task.id}/packages/open",
        headers=admin_headers,
        json={"workspace_id": workspace.id, "collector_identifier": collector.profile_key,
              "capture_date": "2026-09-30"},
    )
    assert blocked.json()["detail"] == "task_closed"
    reopened = client.post(
        f"/api/v1/collection-tasks/{task.id}/reopen",
        headers=admin_headers,
        json={"workspace_id": workspace.id},
    )
    assert reopened.json()["data"]["status"] == "active"
    assert reopened.json()["data"]["closed_at"] is None


def test_closed_task_packages_still_upload(client, admin_headers, db_session):
    workspace = make_workspace(db_session)
    package = make_open_package(db_session, workspace, make_project(db_session, workspace))
    client.post(
        f"/api/v1/collection-tasks/{package.collection_task_id}/close",
        headers=admin_headers,
        json={"workspace_id": workspace.id},
    )
    response = client.post(
        "/api/v1/upload-sessions",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "collection_project_id": package.collection_project_id,
            "package_uids": [package.package_uid],
            "upload_mode": "oss_multipart",
        },
    )
    assert response.status_code == 200, response.text


def test_task_list_reports_status_and_target_reached(client, admin_headers, db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package = make_open_package(db_session, workspace, project, hours="1.00")
    package.intake_valid_duration_hours = Decimal("1.00")
    package.intake_valid_duration_s = Decimal("3600.000")
    db_session.commit()
    closed_task = make_task(db_session, project)
    closed_task.status = "closed"
    db_session.commit()

    everything = client.get(
        "/api/v1/collection-tasks", headers=admin_headers, params={"workspace_id": workspace.id}
    ).json()["data"]["items"]
    by_id = {item["id"]: item for item in everything}
    assert by_id[package.collection_task_id]["target_reached"] is True
    assert by_id[package.collection_task_id]["status"] == "active"
    assert by_id[closed_task.id]["target_reached"] is False

    active = client.get(
        "/api/v1/collection-tasks",
        headers=admin_headers,
        params={"workspace_id": workspace.id, "status": "active"},
    ).json()["data"]["items"]
    assert {item["id"] for item in active} == {package.collection_task_id}
    assert db_session.get(DataPackage, package.id).status == "open"
```

- [ ] **Step 2: 运行，确认失败**

Run: `$PYTEST backend/tests/test_daily_package_lifecycle_api.py`
Expected: FAIL（404 / 缺字段）

- [ ] **Step 3: 实现服务**

`daily_packages.py` 追加：

```python
def seal_data_package(
    db: Session,
    *,
    workspace_id: int,
    data_package_id: int,
    now: datetime | None = None,
) -> DataPackage:
    """Stop the open-package rule from returning this package again. Idempotent."""
    package = (
        db.query(DataPackage)
        .filter(DataPackage.id == data_package_id, DataPackage.workspace_id == workspace_id)
        .with_for_update()
        .one_or_none()
    )
    if package is None:
        raise DailyPackageError("package_not_found", 404)
    if package.status == "voided":
        raise DailyPackageError("package_voided", 409)
    frozen = db.scalar(
        select(PackageIntakeReview.id).where(PackageIntakeReview.data_package_id == package.id).limit(1)
    )
    if frozen is None and package.sealed_at is None:
        package.sealed_at = (now or datetime.now(timezone.utc)).replace(tzinfo=None)
        db.flush()
    return package
```

`collection_tasks.py` 追加（导入 `Literal`）：

```python
def set_collection_task_status(
    db: Session,
    *,
    workspace_id: int,
    task_id: int,
    status: Literal["active", "closed"],
    actor_id: int | None,
) -> CollectionTask:
    """Close a task to stop new packages, or reopen it; existing packages are untouched."""
    task = (
        db.query(CollectionTask)
        .filter(CollectionTask.id == task_id, CollectionTask.workspace_id == workspace_id)
        .with_for_update()
        .one_or_none()
    )
    if task is None:
        raise LookupError("collection task does not exist")
    task.status = status
    task.closed_at = datetime.utcnow() if status == "closed" else None
    task.closed_by_user_id = actor_id if status == "closed" else None
    db.flush()
    return task
```

`list_collection_tasks_with_progress` 增加参数 `status: str | None = None`，非空时 `query = query.filter(CollectionTask.status == status)`。

- [ ] **Step 4: 实现路由**

`routers/collection_packages.py`：

```python
class SealPackageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_id: int = Field(gt=0)


@router.post("/{data_package_id}/seal")
def seal_package(
    data_package_id: int,
    body: SealPackageRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Tell Studio that no more Episodes will be appended to this package."""
    require_permission(user, "workspace:write")
    _require_workspace(db, user=user, workspace_id=body.workspace_id)
    try:
        package = seal_data_package(
            db, workspace_id=body.workspace_id, data_package_id=data_package_id
        )
        db.commit()
    except DailyPackageError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc
    db.refresh(package)
    emit_audit_event(
        "collection.package.seal",
        actor=str(user.get("email") or ""),
        resource=f"data_package:{data_package_id}",
        detail={"workspace_id": body.workspace_id},
    )
    return success(_package_item(package))
```

`routers/collection_tasks.py`：

```python
class TaskStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_id: int = Field(gt=0)


def _change_task_status(task_id, body, db, user, status):
    require_permission(user, "workspace:write")
    _require_workspace(db, user=user, workspace_id=body.workspace_id)
    try:
        task = set_collection_task_status(
            db,
            workspace_id=body.workspace_id,
            task_id=task_id,
            status=status,
            actor_id=_actor_id(user),
        )
        db.commit()
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail="task_not_found") from exc
    db.refresh(task)
    emit_audit_event(
        "collection.task.close" if status == "closed" else "collection.task.reopen",
        actor=str(user.get("email") or ""),
        resource=f"collection_task:{task_id}",
        detail={"workspace_id": body.workspace_id},
    )
    package_count, intake_duration = _progress_for_task(db, task=task)
    return success(
        _task_item(task, package_count=package_count, intake_valid_duration_hours=intake_duration)
    )


@router.post("/{task_id}/close")
def close_collection_task(task_id: int, body: TaskStatusRequest,
                          db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """Stop opening new packages under this task."""
    return _change_task_status(task_id, body, db, user, "closed")


@router.post("/{task_id}/reopen")
def reopen_collection_task(task_id: int, body: TaskStatusRequest,
                           db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """Allow opening packages under this task again."""
    return _change_task_status(task_id, body, db, user, "active")
```

`_task_item` 新增字段：

```python
        "status": task.status,
        "closed_at": format_api_datetime(task.closed_at),
        "target_reached": Decimal(intake_valid_duration_hours) >= Decimal(task.target_duration_hours),
```

`list_collection_tasks` 路由增加 `status: Literal["active", "closed"] | None = Query(default=None)` 并传入服务。

- [ ] **Step 5: 运行，确认通过**

Run: `$PYTEST backend/tests/test_daily_package_lifecycle_api.py backend/tests/test_collection_tasks_api.py`
Expected: PASS

- [ ] **Step 6: 文档与提交**

`docs/COLLECTION_UPLOAD_API.md` 在开包段落后追加：

```markdown
本地审核锁定后调用 `POST /data-packages/{id}/seal`（`{"workspace_id"}`）封口：此后开包不再返回该包，
但已封口的包仍可创建上传会话。已冻结的包封口直接成功；已作废的包返回 409 `package_voided`。
任务结束（`POST /collection-tasks/{id}/close`）后开包返回 409 `task_closed`，已开出的包照常上传、审核。
```

```bash
git add backend/data backend/tests/test_daily_package_lifecycle_api.py docs/COLLECTION_UPLOAD_API.md
git commit -m "feat(collection): seal packages and close collection tasks"
```

---

### Task 6: 上传声明校验与 Episode 哈希

**Files:**
- Modify: `backend/data/services/daily_packages.py`（加 `DeclarationMismatchError`、`check_declared_episodes`）
- Modify: `backend/data/services/collection_upload_intake.py:117-180`（`declare_package_sources`）
- Modify: `backend/data/services/collection_upload_parse.py`（循环内与 `_get_or_create_episode`）
- Modify: `backend/data/routers/collection_packages.py`（`_episode_item`）
- Modify: `backend/tests/collection_api_fixtures.py`（`operator_metadata`）与所有经声明接口上传的测试
- Test: `backend/tests/test_daily_package_declarations.py`（新）

**Interfaces:**
- Consumes: `capture_date_of`（Task 4）、`declared_collector_identifier`（Task 1）
- Produces:
  - `class DeclarationMismatchError(ValueError)`，属性 `mismatches: dict[str, list[str]]`；`str(exc)` 形如 `episode_capture_date_mismatch: ep2; episode_collector_mismatch: ep1`
  - `check_declared_episodes(db, *, workspace_id: int, items: list[tuple[DataPackage, str, str, int]]) -> None`（每项：包、episode_id、metadata_text、start_ns）
  - 包详情 `episodes[]` 新增 `metadata_sha256`、`data_mcap_sha256`（无则 `null`）
  - 夹具 `operator_metadata(db, package_uid: str) -> dict[str, object]`，返回 `{"operator": {"id": <profile_key>}}`

- [ ] **Step 1: 写失败的测试**

`backend/tests/test_daily_package_declarations.py`：

```python
"""Declarations must match the package collector and capture day."""

import hashlib
import json
from datetime import date, datetime, timezone

import pytest
from collection_api_fixtures import (
    make_collector,
    make_open_package,
    make_project,
    make_workspace,
)

from data.database import Episode

SHANGHAI_DAY = date(2026, 9, 30)
# 2026-09-30 10:00 Shanghai = 02:00 UTC
START_NS = int(datetime(2026, 9, 30, 2, 0, tzinfo=timezone.utc).timestamp()) * 10**9
# 2026-10-01 00:00:01 Shanghai = 2026-09-30 16:00:01 UTC
NEXT_DAY_NS = int(datetime(2026, 9, 30, 16, 0, 1, tzinfo=timezone.utc).timestamp()) * 10**9


def _item(package_uid, episode_id, *, operator, start_ns):
    metadata = {
        "qrdf_version": "0.2.0",
        "episode_id": episode_id,
        "timing": {"start_timestamp_ns": str(start_ns),
                   "end_timestamp_ns": str(start_ns + 60 * 10**9)},
        "operator": {"id": operator},
        "data_file": "data.mcap",
    }
    text = json.dumps(metadata)
    return {
        "package_uid": package_uid,
        "source": {
            "episode_id": episode_id,
            "start_ns": str(start_ns),
            "end_ns": str(start_ns + 60 * 10**9),
            "metadata_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "data_mcap_sha256": "1" * 64,
        },
        "metadata_text": text,
        "data_file": {"path": "data.mcap", "size_bytes": 4, "sha256": "1" * 64},
    }


@pytest.fixture
def session_for(client, admin_headers, db_session):
    def build(package):
        response = client.post(
            "/api/v1/upload-sessions",
            headers=admin_headers,
            json={
                "workspace_id": package.workspace_id,
                "collection_project_id": package.collection_project_id,
                "package_uids": [package.package_uid],
                "upload_mode": "oss_multipart",
            },
        )
        assert response.status_code == 200, response.text
        return response.json()["data"]["id"]

    return build


def _declare(client, headers, package, session_id, items):
    return client.post(
        f"/api/v1/upload-sessions/{session_id}/declarations",
        headers=headers,
        json={"workspace_id": package.workspace_id, "items": items},
    )


def _package(db_session):
    workspace = make_workspace(db_session)
    collector = make_collector(db_session, workspace)
    package = make_open_package(
        db_session, workspace, make_project(db_session, workspace),
        collector=collector, capture_date=SHANGHAI_DAY,
    )
    return package, collector


def test_declaration_accepts_matching_episodes(client, admin_headers, db_session, session_for):
    package, collector = _package(db_session)
    response = _declare(client, admin_headers, package, session_for(package), [
        _item(package.package_uid, "ep1", operator=collector.profile_key, start_ns=START_NS),
    ])
    assert response.status_code == 200, response.text


def test_declaration_accepts_zero_padded_operator(client, admin_headers, db_session, session_for):
    package, collector = _package(db_session)
    response = _declare(client, admin_headers, package, session_for(package), [
        _item(package.package_uid, "ep1", operator=f"000{collector.profile_key}", start_ns=START_NS),
    ])
    assert response.status_code == 200, response.text


def test_declaration_accepts_collector_deactivated_after_open(
    client, admin_headers, db_session, session_for
):
    package, collector = _package(db_session)
    collector.is_active = False
    db_session.commit()
    response = _declare(client, admin_headers, package, session_for(package), [
        _item(package.package_uid, "ep1", operator=collector.profile_key, start_ns=START_NS),
    ])
    assert response.status_code == 200, response.text


def test_declaration_rejects_episode_from_next_capture_day(
    client, admin_headers, db_session, session_for
):
    package, collector = _package(db_session)
    response = _declare(client, admin_headers, package, session_for(package), [
        _item(package.package_uid, "ep1", operator=collector.profile_key, start_ns=START_NS),
        _item(package.package_uid, "ep2", operator=collector.profile_key, start_ns=NEXT_DAY_NS),
    ])
    assert response.status_code == 422
    assert response.json()["detail"] == "episode_capture_date_mismatch: ep2"


def test_declaration_lists_every_mismatch(client, admin_headers, db_session, session_for):
    package, _collector = _package(db_session)
    other = make_collector(db_session, make_workspace(db_session))
    response = _declare(client, admin_headers, package, session_for(package), [
        _item(package.package_uid, "ep1", operator=other.profile_key, start_ns=START_NS),
        _item(package.package_uid, "ep2", operator="unknown", start_ns=NEXT_DAY_NS),
    ])
    assert response.status_code == 422
    assert response.json()["detail"] == (
        "episode_capture_date_mismatch: ep2; episode_collector_mismatch: ep1, ep2"
    )


def test_package_detail_exposes_declared_hashes(client, admin_headers, db_session):
    package, _collector = _package(db_session)
    episode = Episode(
        episode_uid="ep-hash",
        workspace_id=package.workspace_id,
        data_package_id=package.id,
        kind="source",
        modality="rgb",
        source_fingerprint="fp-hash",
        validity_status="valid",
        metadata_json={"collection_upload": {"external_episode_id": "ep1",
                                             "metadata_sha256": "a" * 64,
                                             "data_mcap_sha256": "b" * 64}},
    )
    legacy = Episode(
        episode_uid="ep-legacy",
        workspace_id=package.workspace_id,
        data_package_id=package.id,
        kind="source",
        modality="rgb",
        source_fingerprint="fp-legacy",
        validity_status="valid",
        metadata_json={"collection_upload": {"external_episode_id": "ep0"}},
    )
    db_session.add_all([episode, legacy])
    db_session.commit()
    episodes = client.get(
        f"/api/v1/data-packages/{package.id}",
        headers=admin_headers,
        params={"workspace_id": package.workspace_id},
    ).json()["data"]["episodes"]
    by_external = {item["external_episode_id"]: item for item in episodes}
    assert by_external["ep1"]["metadata_sha256"] == "a" * 64
    assert by_external["ep1"]["data_mcap_sha256"] == "b" * 64
    assert by_external["ep0"]["metadata_sha256"] is None
```

在 `backend/tests/test_collection_upload_parse.py` 的 `test_nonfixture_multi_package_uses_per_declaration_staged_files` 末尾循环里（`episode = ...` 之后）追加断言：

```python
        declared = declarations[package.package_uid][0]["source"]
        upload = episode.metadata_json["collection_upload"]
        assert upload["metadata_sha256"] == declared["metadata_sha256"]
        assert upload["data_mcap_sha256"] == declared["data_mcap_sha256"]
```

- [ ] **Step 2: 运行，确认失败**

Run: `$PYTEST backend/tests/test_daily_package_declarations.py`
Expected: FAIL（错误声明被接受；详情缺哈希字段）

- [ ] **Step 3: 实现声明校验**

`daily_packages.py` 追加（导入 `json`、`PersonnelProfile`、`WorkspacePersonnelProfile`、`declared_collector_identifier`）：

```python
class DeclarationMismatchError(ValueError):
    """Declared Episodes that do not belong to their package's collector or capture day."""

    def __init__(self, mismatches: dict[str, list[str]]) -> None:
        self.mismatches = mismatches
        super().__init__(
            "; ".join(f"{code}: {', '.join(ids)}" for code, ids in sorted(mismatches.items()))
        )


def check_declared_episodes(
    db: Session,
    *,
    workspace_id: int,
    items: list[tuple[DataPackage, str, str, int]],
) -> None:
    """Reject Episodes whose operator or capture day differs from their package.

    Activity is not required: a collector deactivated after opening keeps uploading.
    """
    profile_ids: dict[str, int | None] = {}
    mismatches: dict[str, list[str]] = {}
    for package, episode_id, metadata_text, start_ns in items:
        identifier = declared_collector_identifier(json.loads(metadata_text))
        if identifier not in profile_ids:
            profile = resolve_workspace_collector(
                db, workspace_id=workspace_id, identifier=identifier, require_active=False
            )
            profile_ids[identifier] = profile.id if profile is not None else None
        if profile_ids[identifier] is None or profile_ids[identifier] != package.collector_id:
            mismatches.setdefault("episode_collector_mismatch", []).append(episode_id)
        if capture_date_of(start_ns) != package.capture_date:
            mismatches.setdefault("episode_capture_date_mismatch", []).append(episode_id)
    if mismatches:
        raise DeclarationMismatchError(mismatches)
```

`collection_upload_intake.py` 的 `declare_package_sources`：循环前加 `checks: list[tuple[str, object]] = []`；在 `manifest = build_duance_import_manifest(...)` 之后加 `checks.append((package_uid, manifest))`；在 `if set(stored) != package_uids:` 检查之后加：

```python
    packages_by_uid = {
        package.package_uid: package
        for package in db.scalars(
            select(DataPackage).where(DataPackage.package_uid.in_(package_uids))
        )
    }
    check_declared_episodes(
        db,
        workspace_id=workspace_id,
        items=[
            (packages_by_uid[uid], manifest.episode_id, manifest.metadata_text, manifest.start_ns)
            for uid, manifest in checks
        ],
    )
```

（导入 `DataPackage` 与 `check_declared_episodes`。`DeclarationMismatchError` 是 `ValueError`，路由已把 `ValueError` 映射为 422 并以 `str(exc)` 作 `detail`。）

- [ ] **Step 4: 复制哈希到 Episode**

`collection_upload_parse.py`：在调用 `_get_or_create_episode(...)` 之前（`parsed["source_fingerprint"] = ...` 之后）加：

```python
                declared_source = source.get("source") or {}
                parsed["declared_hashes"] = {
                    key: declared_source[key]
                    for key in ("metadata_sha256", "data_mcap_sha256")
                    if isinstance(declared_source.get(key), str)
                }
```

`_get_or_create_episode` 里 `metadata_json["collection_upload"]` 改为：

```python
            "collection_upload": {
                "upload_session_id": upload_session.id,
                "external_episode_id": parsed["episode_id"],
                **parsed.get("declared_hashes", {}),
            },
```

`routers/collection_packages.py` 的 `_episode_item`：取 `upload = metadata.get("collection_upload") if isinstance(metadata.get("collection_upload"), dict) else {}`，字典中加入：

```python
        "metadata_sha256": upload.get("metadata_sha256"),
        "data_mcap_sha256": upload.get("data_mcap_sha256"),
```

- [ ] **Step 5: 运行新测试，确认通过**

Run: `$PYTEST backend/tests/test_daily_package_declarations.py backend/tests/test_collection_upload_parse.py`
Expected: PASS（除下一步要修的既有用例外）

- [ ] **Step 6: 修既有上传测试的元数据**

`collection_api_fixtures.py` 追加：

```python
def operator_metadata(db: Session, package_uid: str) -> dict[str, object]:
    """Metadata fields a declaration needs to match its package collector."""
    package = db.query(DataPackage).filter(DataPackage.package_uid == package_uid).one()
    return {"operator": {"id": package.collector.profile_key}}
```

找出所有经 `/declarations` 声明的测试：

```bash
grep -rlnE "/declarations" backend/tests | grep -v test_daily_package_declarations
```

逐个文件处理（当前为 `test_client_admission_declaration.py`、`test_client_admission_uploads.py`、`test_client_admission_worker.py`、`test_collection_admission_worker.py`、`test_collection_upload_bytes.py`、`test_collection_upload_intake_api.py`、`test_collection_upload_intake_smoke.py`、`test_collection_upload_parse.py`、`test_data_backend_e2e.py`、`test_external_tool_e2e.py`）：
- 构造 metadata 的辅助函数（如 `_declared_source`、`_declare`、`declared`、`_declaration`）在 metadata 字典里合并 `**operator_metadata(db, package_uid)`；辅助函数拿不到 `db` 时增加 `db` 参数，调用处传入 `db_session`。
- 时间戳：`start_ns=0` 与 `make_open_package` 默认采集日 `1970-01-01` 一致，无需改；使用真实时间戳的用例（例如 `test_external_tool_e2e.py` 读取 fixture 目录里的 metadata），开包时传 `capture_date=capture_date_of(start_ns)`（从 `data.services.daily_packages` 导入）。
- 包不是经 `make_open_package` 建的（测试里直接 `DataPackage(...)`），确保其 `collector_id` 指向一个真实 `make_collector` 返回的档案，并用该档案的 `profile_key` 写入 metadata。

- [ ] **Step 7: 全量后端测试**

Run: `$PYTEST backend/tests` 与 `.venv/bin/ruff check backend`
Expected: PASS

- [ ] **Step 8: 文档与提交**

`docs/COLLECTION_UPLOAD_API.md` 在「声明」一节追加：

```markdown
声明时逐个 Episode 校验：`metadata.operator.id`（去前导零后）必须是包的采集员；`start_ns` 换算到 Asia/Shanghai
的日期必须等于包的采集日。不满足时整个请求返回 422，`detail` 形如
`episode_capture_date_mismatch: ep2; episode_collector_mismatch: ep1, ep2`。
解析建 Episode 时把声明的 `metadata_sha256`、`data_mcap_sha256` 原样记录（不下载、不重算），
包详情 `episodes[]` 返回这两个值，供客户端清理本地文件前核对；本功能上线前入库的 Episode 返回 `null`。
```

```bash
git add -A backend docs/COLLECTION_UPLOAD_API.md
git commit -m "feat(collection): validate declared episodes against daily packages"
```

---

### Task 7: 上传 CLI 改为按任务开包

**Files:**
- Modify: `scripts/studio_upload.py`
- Modify: `scripts/tests/test_studio_upload.py`
- Modify: `docs/STUDIO_UPLOAD_CLI.md`

**Interfaces:**
- Consumes: `POST /collection-tasks/{task_id}/packages/open`（Task 4）
- Produces:
  - `episode_capture_group(directory: Path) -> tuple[str, str]`（规范化采集员标识、`YYYY-MM-DD` 采集日）
  - `run_upload(client, args, *, progress=print)`：`args.task: int` 取代 `args.package`
  - 状态文件新增 `package_uid`

- [ ] **Step 1: 写失败的测试**

在 `scripts/tests/test_studio_upload.py`：
1. 所有构造 `argparse.Namespace(...)` 的地方把 `package="package"` 改为 `task=5`。
2. 写测试 metadata 的辅助函数给 metadata 加 `"operator": {"id": "7"}`，并保证 `timing.start_timestamp_ns` 是固定值（例如 `"1790000000000000000"`）。
3. `FakeServer.request` 增加开包分支（放在 `if path == "/upload-sessions":` 之前）：

```python
        if path.endswith("/packages/open"):
            self.opened.append(body)
            return {"package_uid": "package", "data_package_id": 11, "sequence_no": 1,
                    "capture_date": body["capture_date"], "created": True}
```

并在 `__init__` 加 `self.opened = []` 与 `self.mode = "chunked"`。现有 `GET` 分支用 `self.calls[0][2]["body"]` 取上传方式，开包后第一条调用不再是建会话：改为在 `path == "/upload-sessions"` 分支记录 `self.mode = body.get("upload_mode", "chunked")`，`GET` 分支返回 `self._session(self.mode)`。
4. 新增用例：

```python
class DailyPackageTests(unittest.TestCase):
    def _episode(self, root: Path, name: str, *, operator="7", start="1790000000000000000"):
        directory = root / name
        directory.mkdir()
        (directory / "data.mcap").write_bytes(b"mcap")
        (directory / "metadata.json").write_text(json.dumps({
            "episode_id": name,
            "data_file": "data.mcap",
            "operator": {"id": operator},
            "timing": {"start_timestamp_ns": start, "end_timestamp_ns": str(int(start) + 10**9)},
        }))
        return directory

    def test_capture_group_is_canonical_collector_and_shanghai_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 1790000000 s = 2026-09-21 14:13:20 UTC = 22:13:20 Shanghai
            episode = self._episode(Path(tmp), "ep1", operator="0007")
            self.assertEqual(upload.episode_capture_group(episode), ("7", "2026-09-21"))

    def test_mixed_collectors_fail_before_any_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = FakeServer()
            args = argparse.Namespace(
                task=5, workspace=1, project=2, transport="chunked", chunk_mib=1,
                episodes=[self._episode(root, "ep1"), self._episode(root, "ep2", operator="8")],
                state=root / "state.json", session_id=None, wait_seconds=0, poll_seconds=0,
            )
            with self.assertRaisesRegex(upload.UploadError, "one collector and capture date"):
                upload.run_upload(server, args, progress=lambda _m: None)
            self.assertEqual(server.calls, [])

    def test_opens_package_once_and_reuses_it_on_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = FakeServer()
            args = argparse.Namespace(
                task=5, workspace=1, project=2, transport="chunked", chunk_mib=1,
                episodes=[self._episode(root, "ep1")],
                state=root / "state.json", session_id=None, wait_seconds=0, poll_seconds=0,
            )
            upload.run_upload(server, args, progress=lambda _m: None)
            self.assertEqual(server.opened, [
                {"workspace_id": 1, "collector_identifier": "7", "capture_date": "2026-09-21"}
            ])
            self.assertEqual(json.loads(args.state.read_text())["package_uid"], "package")
            upload.run_upload(server, args, progress=lambda _m: None)
            self.assertEqual(len(server.opened), 1)
```


- [ ] **Step 2: 运行，确认失败**

Run: `python3 -m unittest discover -s scripts/tests -p test_studio_upload.py -v`
Expected: FAIL（`AttributeError: episode_capture_group` / `args.package`）

- [ ] **Step 3: 实现**

`scripts/studio_upload.py`：
- 导入 `from datetime import datetime, timezone` 与 `from zoneinfo import ZoneInfo`；新增：

```python
CAPTURE_TIMEZONE = ZoneInfo("Asia/Shanghai")


def episode_capture_group(directory: Path) -> tuple[str, str]:
    """Collector identifier and Asia/Shanghai capture day declared by one Episode."""
    metadata = json.loads((Path(directory) / "metadata.json").read_text(encoding="utf-8"))
    operator = metadata.get("operator")
    raw = operator.get("id") if isinstance(operator, dict) else None
    if not isinstance(raw, str) or not raw.isdigit() or int(raw) == 0:
        raise UploadError("metadata.operator.id must be the collector number")
    start = (metadata.get("timing") or {}).get("start_timestamp_ns")
    if isinstance(start, bool) or not str(start).isdigit():
        raise UploadError("Episode timestamps must be exact integer nanoseconds")
    day = datetime.fromtimestamp(int(start) // 1_000_000_000, tz=timezone.utc)
    return str(int(raw)), day.astimezone(CAPTURE_TIMEZONE).date().isoformat()
```

- `_source_entries(args)` 改为 `_source_entries(args, package_uid: str | None)`，内部 `episode_declaration(directory, package_uid)`。
- `run_upload` 开头改为：

```python
    groups = {episode_capture_group(directory) for directory in _episode_directories(args)}
    if len(groups) != 1:
        raise UploadError("All --episode directories must share one collector and capture date")
    collector_identifier, capture_date = groups.pop()
    entries = _source_entries(args, None)
    chunk_bytes = args.chunk_mib * 1024 * 1024 if transport == "chunked" else None
    identity = {
        "base": client.base,
        "workspace_id": args.workspace,
        "collection_project_id": args.project,
        "collection_task_id": args.task,
        "collector_identifier": collector_identifier,
        "capture_date": capture_date,
        "transport": transport,
        "chunk_bytes": chunk_bytes,
        "episodes": [...],  # 与现有一致
    }
    state = _load_state(args.state, identity)
    if not state.get("package_uid"):
        opened = client.request(
            "POST",
            f"/collection-tasks/{int(args.task)}/packages/open",
            body={
                "workspace_id": args.workspace,
                "collector_identifier": collector_identifier,
                "capture_date": capture_date,
            },
            retry=True,
        )
        state["package_uid"] = opened["package_uid"]
        save_state(args.state, state)
    package_uid = state["package_uid"]
    for entry in entries:
        entry["declaration"]["package_uid"] = package_uid
```

- 创建会话处 `"package_uids": [args.package]` → `[package_uid]`；`check_session(session, identity)` → `check_session(session, identity, package_uid)`，函数签名改为 `check_session(session, identity, package_uid)`，`expected = {package_uid}`。
- `main()`：删除 `--package`，新增 `parser.add_argument("--task", required=True, type=int, help="Collection task ID; the package is opened per collector and capture day")`。

- [ ] **Step 4: 运行，确认通过**

Run: `python3 -m unittest discover -s scripts/tests -p test_studio_upload.py -v`
Expected: PASS

- [ ] **Step 5: 文档**

`docs/STUDIO_UPLOAD_CLI.md`：
- 「使用」第 1 步改为「在 Studio 创建采集任务，取得工作空间 ID、采集项目 ID 和任务 ID。」
- 示例命令中 `--package pkg_your_assigned_package` 改为 `--task 12`，并在示例后加一句：「CLI 从 metadata 读取 `operator.id` 与开始时间，为该采集员该采集日（Asia/Shanghai）开包；一次调用内的 Episode 必须属于同一采集员、同一天。开包结果记录在状态文件中，重跑复用。」
- 「中断恢复」一段中「状态身份绑定 … 包 …」改为「… 任务、采集员、采集日 …」。

- [ ] **Step 6: 提交**

```bash
git add scripts/studio_upload.py scripts/tests/test_studio_upload.py docs/STUDIO_UPLOAD_CLI.md
git commit -m "feat(cli): open the daily package before uploading"
```

---

### Task 8: 前端数采任务页

**Files:**
- Modify: `frontend/js/mining-utils.js`、`frontend/js/api.js`、`frontend/js/app.js`、`frontend/js/demo-data.js`、`frontend/js/data-overview.js`、`frontend/js/collection-data-board.js`
- Modify: `frontend/tests/mining-utils.test.mjs`、`frontend/tests/demo-mode-flows.test.mjs`、`frontend/tests/data-package-intake-review.test.mjs`、`frontend/tests/data-overview.test.mjs`
- Delete: `frontend/tests/mining-assign-entry.test.mjs`
- Test: `frontend/tests/mining-daily-packages.test.mjs`（新）

**Interfaces:**
- Consumes: 任务项 `status`、`closed_at`、`target_reached`、`intake_valid_duration_hours`；包项 `collector{id,name,profile_key}`、`capture_date`、`sequence_no`、`sealed_at`、`opened_at`（Task 3–5）；看板 `status_counts.open`、概览 `package_counts.open`
- Produces:
  - `QuicDataMiningUtils.taskTargetReached(task) -> boolean`、`QuicDataMiningUtils.packageDayLabel(pkg) -> string`
  - `QuicDataAPI.closeCollectionTask(taskId, workspaceId)`、`QuicDataAPI.reopenCollectionTask(taskId, workspaceId)`

- [ ] **Step 1: 写失败的测试**

删除 `frontend/tests/mining-assign-entry.test.mjs`，新建 `frontend/tests/mining-daily-packages.test.mjs`：

```javascript
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const utils = require('../js/mining-utils.js');
const app = readFileSync(new URL('../js/app.js', import.meta.url), 'utf8');
const api = readFileSync(new URL('../js/api.js', import.meta.url), 'utf8');

test('assignment, split, adjustment, manifest and supplement entry points are gone', () => {
  for (const pattern of [
    /openMiningAssignModeDialog/, /openMiningAssignDialog/, /submitMiningAssign/, /canAssignPackage/,
    /miningTaskAssignDone/, /miningPackageCountPreview/, /default_package_duration_hours/,
    /splitMiningTask/, /openMiningTaskSplit/, /adjustDataPackages/, /createSupplementPackage/,
    /getOfflineManifest/, /getTaskOfflineManifest/, /downloadTaskManifest/, /isManifestUnavailable/,
    /batchAssignDataPackages/, /assignDataPackage/, /pendingAssignTasks/,
  ]) {
    assert.doesNotMatch(app, pattern, `app.js still contains ${pattern}`);
  }
  for (const name of ['adjustDataPackages', 'createSupplementPackage', 'batchAssignDataPackages',
    'assignDataPackage', 'getTaskOfflineManifest', 'getOfflineManifest']) {
    assert.doesNotMatch(api, new RegExp(`${name}\\(`), `api.js still defines ${name}`);
  }
});

test('task status, progress and close/reopen are exposed', () => {
  assert.match(api, /closeCollectionTask\(taskId, workspaceId\)/);
  assert.match(api, /reopenCollectionTask\(taskId, workspaceId\)/);
  assert.match(app, /closeMiningTask\(scope\.row\)/);
  assert.match(app, /reopenMiningTask\(scope\.row\)/);
  assert.match(app, /QuicDataMiningUtils\.taskTargetReached/);
  assert.match(app, /t\('taskTargetReachedHint'\)/);
  assert.match(app, /activeTasks/);
});

test('package list shows collector, capture day and sequence', () => {
  assert.match(app, /QuicDataMiningUtils\.packageDayLabel/);
  assert.match(app, /t\('packageCaptureDate'\)/);
  assert.match(app, /t\('packageCollector'\)/);
  assert.match(app, /t\('packageSealed'\)/);
});

test('taskTargetReached and packageDayLabel', () => {
  assert.equal(utils.taskTargetReached({ target_reached: true }), true);
  assert.equal(utils.taskTargetReached({ target_duration_hours: '2.00', intake_valid_duration_hours: '2.00' }), true);
  assert.equal(utils.taskTargetReached({ target_duration_hours: '2.00', intake_valid_duration_hours: '1.99' }), false);
  assert.equal(utils.taskTargetReached({}), false);
  assert.equal(utils.packageDayLabel({ capture_date: '2026-09-30', sequence_no: 2 }), '2026-09-30 #2');
  assert.equal(utils.packageDayLabel({}), '—');
});
```

`frontend/tests/mining-utils.test.mjs`：删除 `canAssignPackage`、`taskAssignDone`、`packageCountPreview` 的断言（第 69–74 行）。

- [ ] **Step 2: 运行，确认失败**

Run: `node --test frontend/tests/mining-daily-packages.test.mjs`
Expected: FAIL

- [ ] **Step 3: `mining-utils.js`**

删除 `canAssignPackage`、`taskAssignDone`、`packageCountPreview` 三个函数及导出；新增并导出：

```javascript
  function taskTargetReached(task) {
    if (task?.target_reached === true) return true;
    const target = Number(task?.target_duration_hours);
    const valid = Number(task?.intake_valid_duration_hours);
    return Number.isFinite(target) && target > 0 && Number.isFinite(valid)
      && Math.round(valid * 100) >= Math.round(target * 100);
  }

  function packageDayLabel(pkg) {
    if (!pkg?.capture_date) return '—';
    return pkg.sequence_no ? `${pkg.capture_date} #${pkg.sequence_no}` : String(pkg.capture_date);
  }
```

- [ ] **Step 4: `api.js`**

删除 `adjustDataPackages`、`createSupplementPackage`、`batchAssignDataPackages`、`assignDataPackage`、`getTaskOfflineManifest`、`getOfflineManifest`；在 `createCollectionTask` 后新增：

```javascript
    closeCollectionTask(taskId, workspaceId) {
      return request('POST', `/collection-tasks/${encodeURIComponent(String(taskId))}/close`, {
        body: { workspace_id: workspaceId },
      });
    },
    reopenCollectionTask(taskId, workspaceId) {
      return request('POST', `/collection-tasks/${encodeURIComponent(String(taskId))}/reopen`, {
        body: { workspace_id: workspaceId },
      });
    },
```

- [ ] **Step 5: `app.js` —— 删除**

删除以下函数、响应式状态、导出名（`return { ... }` 中）与模板片段，删后用 Step 1 的测试确认无残留：
- 分配：`openMiningAssignDialog`、`openMiningAssignModeDialog`、`submitMiningAssign`、`resetMiningAssignDialogState`、`onMiningAssignDialogClosed`、`showMiningAssignDialog`、`miningAssignForm`、`canAssignPackage`、`miningTaskAssignDone`；分配对话框模板（含 `@click="submitMiningAssign"` 的整个 `<el-dialog>`）；任务行与抽屉顶部的「分配任务」按钮（`openMiningAssignModeDialog(...)`）；包行的「分配」按钮与其后的分隔线（`v-if="canAssignPackage(scope.row)"`）；筛选里的 `assignStatus`（`miningTaskFilter`、`resetMiningTaskFilter`、`filteredMiningTaskRows` 中相关分支、`<el-select v-model="miningTaskFilter.assignStatus" ...>`）；任务表「分配状态」列；KPI 中 `pendingAssignTasks`、`pendingAssignBatches`。
- 拆分／调整：`splitMiningTask`、`openMiningTaskSplit`、`openMiningSplitDialog`、`resetMiningSplitDialogState`、`onMiningSplitDialogClosed`、`showMiningSplitDialog`、`miningSplitForm`、`miningSplitTotalHours`、`miningSplitPerBatchHours`、`miningTaskSplitDone` 及拆分对话框模板、抽屉顶部「拆分」按钮、`taskSplit*` 列。
- 新建任务：`miningTaskForm.package_hours`、`miningPackageCountPreview`、对话框中「单包目标时长」`<label>`、`createMiningTask` 中 `packageHours` 校验与 `default_package_duration_hours` 字段。
- 离线清单：`isManifestUnavailable`、`handlePackageRowCommand` 与 `handleTaskRowCommand` 中的清单下载分支（若函数只剩清单用途则整体删除，并删除模板里对应 `<el-dropdown>`）、抽屉顶部「下载任务清单」下拉、`getOfflineManifest`/`getTaskOfflineManifest` 调用。
- 补采：`createSupplementPackage` 调用及其所在的对话框／按钮。
- i18n 两套词条中只被上述代码使用的键（`taskAssign*`、`taskSplit*`、`downloadManifest*`、`downloadTaskManifest`、`manifest*`、`createSupplementPackage`、`packageDurationHours`、`packageCountPreview`、`assignTaskAction`、`assignPackageAction`、`assignBatch`、`splitBatches`），删除前用 `grep -n "t('键名')" frontend/js/app.js` 确认没有其它引用。

- [ ] **Step 6: `app.js` —— 新增与改写**

1. 两套 i18n 词条新增：

```javascript
      // zh-CN
      taskStatusLabel: '任务状态', taskStatusActive: '进行中', taskStatusClosed: '已结束',
      taskProgressLabel: '进度', taskTargetReachedHint: '已达标，可结束',
      closeTaskAction: '结束', reopenTaskAction: '重新开启',
      closeTaskConfirm: '结束后不能再在该任务下开新包，已开出的包照常上传和审核。确认结束？',
      taskClosed: '任务已结束', taskReopened: '任务已重新开启', activeTasks: '进行中任务数',
      packageCollector: '采集员', packageCaptureDate: '采集日', packageSealed: '已封口',
```

```javascript
      // en-US
      taskStatusLabel: 'Status', taskStatusActive: 'Active', taskStatusClosed: 'Closed',
      taskProgressLabel: 'Progress', taskTargetReachedHint: 'Target reached; ready to close',
      closeTaskAction: 'Close', reopenTaskAction: 'Reopen',
      closeTaskConfirm: 'No new packages can be opened after closing; opened packages still upload and get reviewed. Close this task?',
      taskClosed: 'Task closed', taskReopened: 'Task reopened', activeTasks: 'Active tasks',
      packageCollector: 'Collector', packageCaptureDate: 'Capture day', packageSealed: 'Sealed',
```

2. `loadMiningData` 中包映射（`const batches = packages.map(...)`）：

```javascript
              const collectorId = pkg.collector?.id ?? pkg.collector_id ?? null;
              const isDone = ['intake_approved', 'batched', 'governing', 'published'].includes(pkg.status);
              return {
                ...pkg,
                id: pkg.id,
                seq: pkg.sequence_no || idx + 1,
                name: QuicDataMiningUtils.packageDayLabel(pkg),
                batch_type: task.modality || 'ego',
                target: null,
                raw: Number(pkg.captured_duration_hours ?? pkg.intake_valid_duration_hours) || 0,
                checked: Number(pkg.intake_valid_duration_hours) || 0,
                valid: Number(pkg.intake_valid_duration_hours) || 0,
                captured_duration_hours: pkg.captured_duration_hours,
                valid_duration_hours: pkg.intake_valid_duration_hours || 0,
                intake_valid_duration_hours: pkg.intake_valid_duration_hours || 0,
                raw_status: pkg.status,
                status: pkg.status === 'open' ? 'collecting' : (isDone ? 'done' : 'processing'),
                assignees: collectorId ? [{ collector_id: collectorId, device_id: pkg.collection_device_id }] : [],
                collector_name: pkg.collector?.name || '—',
                package_uid: pkg.package_uid,
```

（该对象后续原有字段保留，删除 `target_duration_hours: pkg.target_duration_hours` 一行。）任务对象保留 `status`（任务状态）、`closed_at`、`target_reached` 原样透传：在构造任务行处加 `task_status: task.status, closed_at: task.closed_at, target_reached: task.target_reached`（注意任务行已有 `status` 字段用于其它用途时，用 `task_status` 名称；模板与函数均读 `task_status`）。

3. KPI：`miningKpiStats` 删除 `pendingAssignTasks`/`pendingAssignBatches`，新增 `activeTasks: tasks.filter((task) => task.task_status !== 'closed').length`；顶部卡片把「待分配任务数」改为 `{{ t('activeTasks') }}` / `miningKpiStats.activeTasks`。

4. 任务表：原「分配状态」列位置改为两列：

```html
<el-table-column :label="t('taskStatusLabel')" width="100"><template #default="scope"><el-tag size="small" effect="plain" :type="scope.row.task_status === 'closed' ? 'info' : 'success'">{{ scope.row.task_status === 'closed' ? t('taskStatusClosed') : t('taskStatusActive') }}</el-tag></template></el-table-column>
<el-table-column :label="t('taskProgressLabel')" min-width="170"><template #default="scope"><span>{{ Number(scope.row.intake_valid_duration_hours || 0).toFixed(2) }} / {{ Number(scope.row.target_duration_hours || 0).toFixed(2) }} h</span><el-tag v-if="QuicDataMiningUtils.taskTargetReached(scope.row) && scope.row.task_status !== 'closed'" size="small" type="success" style="margin-left: 6px;">{{ t('taskTargetReachedHint') }}</el-tag></template></el-table-column>
```

（若模板作用域中不能直接访问 `QuicDataMiningUtils`，在 setup 中定义 `const miningTaskTargetReached = (row) => QuicDataMiningUtils.taskTargetReached(row);` 并导出，模板改用它；测试断言的 `QuicDataMiningUtils.taskTargetReached` 出现在该函数体内即满足。）

操作列：「查看数据包」后面加

```html
<el-divider direction="vertical" style="margin: 0 8px; height: 12px;" /><el-button v-if="scope.row.task_status !== 'closed'" link type="warning" @click.stop="closeMiningTask(scope.row)">{{ t('closeTaskAction') }}</el-button><el-button v-else link type="primary" @click.stop="reopenMiningTask(scope.row)">{{ t('reopenTaskAction') }}</el-button>
```

5. 新增函数（与 `createMiningTask` 同区域），并加入 `return { ... }`：

```javascript
      async function closeMiningTask(task) {
        if (!task?.id || demoMode.value) return;
        try {
          await ElMessageBox.confirm(t('closeTaskConfirm'), t('closeTaskAction'), { type: 'warning' });
        } catch (_cancelled) {
          return;
        }
        try {
          await QuicDataAPI.closeCollectionTask(task.id, selectedWorkspaceId.value);
          ElMessage.success(t('taskClosed'));
          await loadMiningData();
        } catch (err) {
          errorMessage(err);
        }
      }

      async function reopenMiningTask(task) {
        if (!task?.id || demoMode.value) return;
        try {
          await QuicDataAPI.reopenCollectionTask(task.id, selectedWorkspaceId.value);
          ElMessage.success(t('taskReopened'));
          await loadMiningData();
        } catch (err) {
          errorMessage(err);
        }
      }
```

6. 包列表（数据包抽屉内表格）：在包 UID 列之后加三列：

```html
<el-table-column :label="t('packageCollector')" min-width="110"><template #default="scope">{{ scope.row.collector_name || scope.row.collector?.name || '—' }}</template></el-table-column>
<el-table-column :label="t('packageCaptureDate')" min-width="130"><template #default="scope">{{ QuicDataMiningUtils.packageDayLabel(scope.row) }}</template></el-table-column>
<el-table-column :label="t('packageSealed')" width="90"><template #default="scope"><el-tag v-if="scope.row.sealed_at" size="small" effect="plain">{{ t('packageSealed') }}</el-tag><span v-else>—</span></template></el-table-column>
```

（同上，若模板不能直接访问 `QuicDataMiningUtils`，用导出的包装函数 `miningPackageDayLabel`，函数体内调用 `QuicDataMiningUtils.packageDayLabel`。）删除包表中「目标时长」列（引用 `target_duration_hours` 的列）。

7. 数据批次页（`app.js` 中批次筛选）若有 `responsible_collector_id`/`operator_collector_id` 查询参数，改为 `collector_id`。

- [ ] **Step 7: 其余前端文件**

- `frontend/js/data-overview.js`、`frontend/js/collection-data-board.js`：`status_counts`/`package_counts` 的 `pending_assignment`、`assigned` 两项合并为 `open`（标签「已开包」/`Open`）。
- `frontend/js/demo-data.js`：演示任务与包改为新字段（包：`status: 'open'`、`collector: { id, name, profile_key }`、`capture_date`、`sequence_no`，去掉 `target_duration_hours`、`responsible_collector_id`、`operator_collector_id`、`assigned_at`、补采字段；任务：`status: 'active'`、`target_reached`），删除演示里的分配、调整、补采、离线清单操作。
- `frontend/tests/demo-mode-flows.test.mjs`、`data-package-intake-review.test.mjs`、`data-overview.test.mjs`：删除针对已移除操作的用例，字段断言改为新字段；运行下一步确认。

- [ ] **Step 8: 运行全部前端测试**

Run: `node --test frontend/tests/` 与 `node --test frontend/tests/demo-mode-flows.test.mjs && node scripts/demo-smoke.mjs`
Expected: PASS

- [ ] **Step 9: 浏览器冒烟**

启动开发环境（`make dev-up` 或项目 `.claude/launch.json` 中的前端配置），在「采集管理 → 数采任务」确认：新建任务对话框无单包时长；任务列表有状态、进度、结束按钮，结束后变为「重新开启」；数据包抽屉显示采集员、采集日 `#序号`、封口列；无任何分配、拆分、清单、补采入口；浏览器控制台无报错。

- [ ] **Step 10: 提交**

```bash
git add -A frontend
git commit -m "feat(frontend): show daily packages and task close controls"
```

---

## 完成检查

- [ ] `$PYTEST backend/tests` 全部通过；`.venv/bin/ruff check backend scripts` 无告警
- [ ] `python3 -m unittest discover -s scripts/tests -p test_studio_upload.py -v` 通过
- [ ] `node --test frontend/tests/` 通过
- [ ] `grep -rnE "responsible_collector|operator_collector|pending_assignment|default_package_duration|supplement_for_package|offline.manifest" backend/data backend/scripts frontend/js scripts docs/COLLECTION_UPLOAD_API.md docs/STUDIO_UPLOAD_CLI.md | grep -v __pycache__` 无结果（`alembic/versions` 历史迁移除外）
- [ ] 通知 Duance 计划可以开始执行
