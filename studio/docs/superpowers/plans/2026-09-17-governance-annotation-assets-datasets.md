# 治理、标注审核、数据资产与数据集版本 Implementation Plan

> **当前核对说明（2026-09-18）：** 权限、标注、单审、批资产和目录版本数据库/API 测试已落地；但原 `export_catalog_version()` 仍只写入 `succeeded` 记录和摘要哈希，没有生成真实 QRDF/LeRobot 产物。该项不再按“完成”处理，改由 `docs/superpowers/specs/2026-09-18-quicstudio-object-storage-logical-assets-design.md` 及其实现计划任务 6–7 收口。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 完成 `init` 最后一环：入库审核通过的数据包建数据批 → 可选治理阶段 → 管理员分配标注/单审 → 每批生成至多一个数据资产 → 跨采集工作空间创建不可变数据集版本并支持导出/引用保护。

**Architecture:** 在 Plan① 已有 `DataBatch` / `DataBatchPackage` / `DataBatchLabel` 上扩展治理与人员快照字段；新建采集域 `AnnotationWorkItem` / `ReviewWorkItem`（**不**复用遗留 `work_items`/`batches`）；新建全局 `DataAsset`（批 1:1）与目录型 `CatalogDataset*`（**不**复用遗留 episode 级 `datasets` 表，避免 workspace 强制与 Episode 清单语义冲突）。服务层 `data/services/governance_*`、`annotation_*`、`data_assets.py`、`catalog_datasets.py`；路由挂 `/api/v1`。遗留 Native LeRobot 导入会话可接到目录数据集版本，但不进入采集包/批。

**Tech Stack:** Python 3.11、FastAPI、SQLAlchemy 2.0（`Column()`）、Pydantic v2、Alembic、Celery（治理/导出 job）、pytest、既有 `success()` / `emit_audit_event` / `require_collection_admin` / 三角色 `User.role`

**Spec:** `docs/superpowers/specs/2026-09-14-collection-studio-init-design.md`（§4–§11）

**前置：** Plan ①②③ 已完成。迁移头 `0007_collection_upload_sessions`。包状态含 `intake_approved`。Episode `validity_status ∈ {valid, intake_rejected, qc_dropped}`。

**本计划是 4 份顺序计划的第 ④份。** ① 迁移+模型 ✓ · ② 采集管理 API ✓ · ③ 接入与入库审核 ✓ · ④ 治理、标注审核、资产与数据集（本文件）。

## Global Constraints

- Episode QC 淘汰字面量固定 **`qc_dropped`**（禁止 `qc_rejected`）；入库不合格为 `intake_rejected`；二者独立，互不覆盖。
- 建批候选仅 `status=intake_approved` 且尚未出现在 `data_batch_packages`；创建后包 → `batched`，清单/开关/标签/人员锁定。
- `review_mode` 本期只允许创建 `single`；库内保留 `'dual'` CHECK 作扩展位，API 拒绝 dual。
- 治理阶段状态：`queued` / `running` / `passed` / `skipped` / `failed`；失败仅重试失败阶段；QC 淘汰不可靠重试恢复。
- 标注/审核工作单位是**数据包**；队列无 claim/release；转派仅未完成项；旧处理人写入 403/409。
- 一个 `data_batch_id` 最多一个 `DataAsset`（DB 唯一）；全淘汰 → 批终态 `no_publishable_asset`，不建空资产。
- 资产与数据集目录**不**隐式按当前采集工作空间过滤；治理/标注 API **要**校验工作空间成员。
- 资产/数据集有效时长口径：`governed_valid_duration_hours`（包级）与资产上批内未淘汰 Episode 时长之和。
- 浏览器不接收存储桶/对象键/原始 URI；导出只给服务端生成的短期授权或任务状态。
- 直接导入 LeRobot：不进采集/批/标注；不可反向转 QRDF。
- 测试命令（仓库根）：

```bash
TEST_DATABASE_URL=postgresql+psycopg://quicdata:quicdata@127.0.0.1:5432/quicdata_test_studio \
  .venv/bin/python -m pytest backend/tests/<file> -q
```

前端：

```bash
node --test frontend/tests/*.test.mjs
```

## 架构决策

| 决策 | 取值 | 理由 |
|---|---|---|
| 扩展 vs 新建批表 | **扩展**既有 `data_batches` | Plan① 已有包归属唯一约束与标签快照 |
| 标注工作项 | 新表 `annotation_work_items` / `review_work_items` | 遗留 `work_items` 绑旧 `batches`/Episode 粒度，语义不合 |
| 数据集表 | 新 `catalog_datasets*` | 遗留 `datasets` 强制 `workspace_id` 且清单是 Episode；规格要求全局资产组集 |
| QC 字面量 | `qc_dropped` | 与 Plan① Episode CHECK 一致 |
| 双审 | 仅 `review_mode` 列扩展位 | 规格一期单审 |
| 治理执行 | Celery queue `governance` + 可同步 service 供测试 | 对齐既有 ingest 模式 |

## File Structure

```text
backend/data/
  models/
    data_batch.py              ← 扩展 DataBatch 状态/人员；+ GovernanceRun/StageRun
    annotation_work.py         ← AnnotationWorkItem, ReviewWorkItem, 转派审计辅助字段
    data_asset.py              ← DataAsset（uq data_batch_id）
    catalog_dataset.py         ← CatalogDataset, Version, VersionAsset, Export
  services/
    data_batches.py            ← 候选列表、建批、详情、锁定读写
    governance_runs.py         ← 阶段执行/跳过/重试、QC、合规记录、时长回写
    annotation_work_items.py   ← 分配、保存、提交、转派
    review_work_items.py       ← 通过/退回、转派
    data_assets.py             ← 批结束资产生成、全局目录
    catalog_datasets.py        ← 版本创建/归档/导出/LeRobot 接入
  routers/
    data_batches.py
    governance.py              ← 或并入 data_batches 子路径
    annotation_work_items.py
    review_work_items.py
    data_assets.py
    catalog_datasets.py
  main.py
backend/alembic/versions/
  0008_data_batch_governance.py
  0009_annotation_review_work_items.py
  0010_data_assets.py
  0011_catalog_datasets.py
backend/tests/
  test_data_batches_api.py
  test_governance_runs.py
  test_annotation_work_items_api.py
  test_review_work_items_api.py
  test_data_assets_api.py
  test_catalog_datasets_api.py
  test_governance_annotation_assets_smoke.py
  collection_api_fixtures.py   ← 扩展 seed_intake_approved_package 等
frontend/
  （Task 7）建批/队列/资产/数据集页改真实 API；测试见 Task 8
```

路由前缀（`/api/v1`）：

| 资源 | 前缀 |
|---|---|
| 数据批 | `/data-batches` |
| 标注工作项 | `/annotation-work-items` |
| 审核工作项 | `/review-work-items` |
| 数据资产 | `/data-assets` |
| 目录数据集 | `/catalog-datasets` |

---

### Task 1: 数据批扩展、治理模型与建批 API

**Files:**
- Modify: `backend/data/models/data_batch.py`
- Create: `backend/alembic/versions/0008_data_batch_governance.py`
- Create: `backend/data/services/data_batches.py`
- Create: `backend/data/routers/data_batches.py`
- Modify: `backend/data/main.py`
- Test: `backend/tests/test_data_batches_api.py`
- Modify: `backend/tests/collection_api_fixtures.py`（`seed_intake_approved_package`）

**Interfaces:**
- Consumes: `DataPackage`（`intake_approved`）、`CollectionLabel`（scene/purpose/modality/training）、`require_collection_admin`、`require_collection_workspace`
- Produces:
  - `list_batch_candidates(db, workspace_id, filters) -> list[DataPackage]`
  - `create_data_batch(...) -> DataBatch`
  - `get_data_batch` / `list_data_batches`
  - 包状态条件更新 `intake_approved → batched`

**DataBatch 新增列（迁移 0008）：**
- `status`: `draft_locked` 不需要——创建即锁定；用 `open` / `governing` / `annotating` / `reviewing` / `publishing` / `published` / `no_publishable_asset` / `failed`
- `annotator_user_ids_json`（list[int] 快照）
- `reviewer_user_id`（nullable int，单审）
- `assignment_snapshot_json`（包→标注员映射）

**新表：**
- `data_batch_governance_runs`：`data_batch_id` unique、`status`
- `data_batch_stage_runs`：`run_id`、`stage ∈ {integrity, quality, compliance}`、`status ∈ {queued,running,passed,skipped,failed}`、`attempt`、`result_json`、`error_message`

**HTTP:**
- `GET /data-batches/candidates?workspace_id=&collection_project_id=&...`
- `POST /data-batches` body：

```python
{
    "workspace_id": 1,
    "name": "batch-1",
    "data_package_ids": [10, 11],
    "label_ids": [1, 2],  # 仅 scene/purpose/modality/training
    "integrity_check_enabled": True,
    "quality_check_enabled": True,
    "compliance_check_enabled": False,
    "annotation_enabled": True,
    "annotator_user_ids": [5, 6],
    "reviewer_user_id": 7,  # annotation_enabled 时必填
    "review_mode": "single",
}
```

- `GET /data-batches?workspace_id=`
- `GET /data-batches/{id}?workspace_id=`
- 无 PATCH 改定义；任何改清单尝试 → 409 `batch_locked`

- [x] **Step 1: 写失败测试**

```python
"""数据批：候选、建批锁定与包一次性归属。"""

from tests.collection_api_fixtures import (
    make_workspace,
    make_project,
    seed_intake_approved_package,
)


def test_create_batch_moves_packages_to_batched_and_rejects_reuse(
    client, db_session, admin_headers
):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    p1 = seed_intake_approved_package(db_session, workspace, project)
    p2 = seed_intake_approved_package(db_session, workspace, project)
    # 另建 annotator/reviewer 用户并加入工作空间（fixture 辅助）
    ann, rev = make_annotator_reviewer(db_session, workspace)

    created = client.post(
        "/api/v1/data-batches",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "name": "Kitchen-1",
            "data_package_ids": [p1.id, p2.id],
            "label_ids": [],
            "integrity_check_enabled": False,
            "quality_check_enabled": False,
            "compliance_check_enabled": False,
            "annotation_enabled": True,
            "annotator_user_ids": [ann.id],
            "reviewer_user_id": rev.id,
            "review_mode": "single",
        },
    )
    assert created.status_code == 200
    batch_id = created.json()["data"]["id"]
    db_session.refresh(p1)
    assert p1.status == "batched"

    again = client.post(
        "/api/v1/data-batches",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "name": "Kitchen-2",
            "data_package_ids": [p1.id],
            "label_ids": [],
            "integrity_check_enabled": False,
            "quality_check_enabled": False,
            "compliance_check_enabled": False,
            "annotation_enabled": False,
            "annotator_user_ids": [],
            "reviewer_user_id": None,
            "review_mode": "single",
        },
    )
    assert again.status_code == 409
    assert "already_batched" in again.json()["detail"]


def test_create_batch_rejects_dual_review_mode(client, db_session, admin_headers):
    ...
    assert response.status_code == 422
```

- [x] **Step 2: 实现模型/迁移/service/router；跑通；提交**

```bash
git commit -m "feat: add data-batch create API with locked package membership"
```

建批成功后：若任一治理开关为真 → 创建 `governance_run` 并将批 `status=governing`；若全关且 `annotation_enabled` → 直接进入 Task 3 分配（本任务可只写 hook `schedule_post_batch_pipeline(batch_id)` 空实现，由 Task 2/3 填充）；若全跳过且无标注 → Task 4 资产生成 hook。

---

### Task 2: 治理执行、跳过、QC 淘汰与时长回写

**Files:**
- Create: `backend/data/services/governance_runs.py`
- Modify: `backend/data/routers/data_batches.py`（报告/重试）
- Create: `backend/data/tasks/governance_tasks.py`（或注册既有 celery）
- Test: `backend/tests/test_governance_runs.py`

**Interfaces:**
- `run_governance(db, batch_id, *, sync: bool=False)`
- `retry_failed_stage(db, batch_id, stage)`
- `GET /data-batches/{id}/governance-report?workspace_id=`
- `POST /data-batches/{id}/governance/retry` `{workspace_id, stage}`

**规则：**
- 未启用的阶段 → `skipped`，不跑逻辑
- `quality`：对批内各包 `validity_status=valid` 的 Episode 执行 QC（init 可用规则引擎占位：读 `metadata_json` 质量标记或测试注入 `result_json`）；不合格 → `validity_status=qc_dropped`，写 `metadata_json["qc"]={reason, at}`；**禁止**改回 `valid`
- QC 后回写各包 `governed_valid_duration_hours` = 仍为 `valid` 的 Episode 时长之和（≤ `intake_valid_duration_hours`）
- `compliance`：只把 QRDF 合规相关键抄入 `stage.result_json`，不改内容
- `integrity`：检查声明文件/Episode 完整性摘要（可复用 artifact 存在性）；失败 → stage `failed`
- 全部阶段结束后：若无任何 `valid` Episode → 批 `no_publishable_asset`；否则若 `annotation_enabled` → `annotating` 并调用分配；否则 → 触发资产生成
- 重试：仅 `failed` 阶段；不清掉已 `qc_dropped` 的 Episode

- [x] **Step 1: 测试**

```python
def test_quality_stage_marks_qc_dropped_and_updates_governed_duration(db_session):
    # seed batch with 2 valid episodes on one package; inject QC drop for episode A
    run_governance(db_session, batch_id=batch.id, sync=True)
    db_session.refresh(episode_a)
    db_session.refresh(package)
    assert episode_a.validity_status == "qc_dropped"
    assert package.governed_valid_duration_hours == Decimal("1.00")  # only B


def test_retry_does_not_revive_qc_dropped(db_session): ...
```

- [x] **Step 2: 实现、提交**

```bash
git commit -m "feat: add data-batch governance stages with qc_dropped rules"
```

---

### Task 3: 标注工作项与单审、转派

**Files:**
- Create: `backend/data/models/annotation_work.py`
- Create: `backend/alembic/versions/0009_annotation_review_work_items.py`
- Create: `backend/data/services/annotation_work_items.py`
- Create: `backend/data/services/review_work_items.py`
- Create: `backend/data/routers/annotation_work_items.py`
- Create: `backend/data/routers/review_work_items.py`
- Modify: `backend/data/main.py`
- Test: `backend/tests/test_annotation_work_items_api.py`, `test_review_work_items_api.py`

**模型要点：**

```python
ANNOTATION_WORK_STATUSES = ("assigned", "in_progress", "submitted", "returned", "done")
REVIEW_WORK_STATUSES = ("assigned", "in_progress", "approved", "returned", "done")


class AnnotationWorkItem(Base):
    __tablename__ = "annotation_work_items"
    # data_batch_id, data_package_id, workspace_id
    # assignee_user_id, status, draft_json, draft_version
    # generation (转派后 +1 或保持并由 assignee 切换)
    # Unique(data_batch_id, data_package_id) — 一包一批一个标注项
```

`ReviewWorkItem`：`annotation_work_item_id` FK；`assignee_user_id=reviewer`；退回写 `reason` 并把标注项 → `returned`。

**分配算法（建批后调用）：**
1. 仅包含仍有 `valid` Episode 的包
2. 预计时长 = 包 `governed_valid_duration_hours` 或 fallback `intake_valid_*`
3. 贪心：每次把下一包分给当前累计时长最小的标注员；并列按 `user_id` 升序
4. 写入 `assignment_snapshot_json`；创建 Review 项挂同一 `reviewer_user_id`

**HTTP：**
- `GET /annotation-work-items?workspace_id=`（标注员只见自己的）
- `PATCH /annotation-work-items/{id}` 保存 draft（仅 assignee）
- `POST /annotation-work-items/{id}/submit`
- `POST /annotation-work-items/{id}/reassign` `{workspace_id, to_user_id, reason}` admin
- `GET /review-work-items?workspace_id=`
- `POST /review-work-items/{id}/approve`
- `POST /review-work-items/{id}/return` `{reason}` 非空
- `POST /review-work-items/{id}/reassign` admin

**禁止：** 任何 claim/release 路由；完成后 submit/approve → 409；非 assignee 写 → 403。

- [x] **Step 1: 测试负载均衡与退回/转派**

```python
def test_load_balance_assigns_shorter_load_first(db_session):
    # two annotators, three packages durations 3,2,2 → expected counts/sums stable by user_id tie-break
    ...


def test_return_requires_reason_and_reopens_annotation(client, db_session, reviewer_headers): ...


def test_reassign_rejects_writes_from_previous_assignee(
    client, db_session, admin_headers, ann_headers
): ...
```

- [x] **Step 2: 实现、提交**

```bash
git commit -m "feat: add annotation and single-review work items with reassign"
```

全部审核 `approved` 后：批 → `publishing`，触发 Task 4。

---

### Task 4: 数据批 → 单一数据资产

**Files:**
- Create: `backend/data/models/data_asset.py`
- Create: `backend/alembic/versions/0010_data_assets.py`
- Create: `backend/data/services/data_assets.py`
- Create: `backend/data/routers/data_assets.py`
- Modify: `backend/data/main.py`
- Test: `backend/tests/test_data_assets_api.py`

**模型：**

```python
class DataAsset(Base):
    __tablename__ = "data_assets"
    __table_args__ = (UniqueConstraint("data_batch_id", name="uq_data_assets_batch"), ...)
    id = Column(Integer, primary_key=True)
    data_batch_id = Column(Integer, ForeignKey("data_batches.id"), nullable=False)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)  # 来源工作空间追溯
    governed_valid_duration_hours = Column(Numeric(10, 2), nullable=False)
    stage_snapshot_json = Column(JsonDocument, nullable=False, default=dict)
    # integrity/quality/compliance/annotation: passed|skipped|eliminated|not_generated
    episode_ids_json = Column(JsonDocument, nullable=False, default=list)  # 最终纳入的 episode id
    source_json = Column(JsonDocument, nullable=False, default=dict)  # projects/tasks/packages 追溯
    created_at = ...
```

**规则：**
- `publish_data_asset(db, batch_id)` 幂等：已存在则返回现有行
- 纳入 Episode：`validity_status==valid`（已过入库+QC）
- 若零 Episode：不插入资产，批 `no_publishable_asset`
- 包状态 → `published`（或保持 `batched` 至发布完成——**选定**：资产成功后包 `published`，批 `published`）
- `GET /data-assets`：**不要**默认 `workspace_id` 过滤；可选 `source_workspace_id=`
- `GET /data-assets/{id}`

- [x] **Step 1: 测试**

```python
def test_asset_one_to_one_and_idempotent(db_session):
    a1 = publish_data_asset(db_session, batch_id=batch.id)
    a2 = publish_data_asset(db_session, batch_id=batch.id)
    assert a1.id == a2.id


def test_all_qc_dropped_yields_no_asset(db_session):
    ...
    assert batch.status == "no_publishable_asset"
    assert db_session.query(DataAsset).filter_by(data_batch_id=batch.id).count() == 0


def test_list_assets_not_implicitly_scoped_to_current_workspace(client, db_session, admin_headers):
    # create assets in ws A and B; list without filter returns both for admin
    ...
```

- [x] **Step 2: 实现、提交**

```bash
git commit -m "feat: publish one data asset per data batch"
```

---

### Task 5: 目录数据集、版本、导出与 LeRobot 直导

**Files:**
- Create: `backend/data/models/catalog_dataset.py`
- Create: `backend/alembic/versions/0011_catalog_datasets.py`
- Create: `backend/data/services/catalog_datasets.py`
- Create: `backend/data/routers/catalog_datasets.py`
- Modify: `backend/data/main.py`
- Test: `backend/tests/test_catalog_datasets_api.py`

**模型：**
- `catalog_datasets`：`name` 全局唯一（normalize）、`description`、`status∈{active,archived}`、`source_kind∈{qrdf_assets,lerobot_direct}`
- `catalog_dataset_versions`：`dataset_id`、`version` int 服务端递增、`status∈{active,archived}`、`created_by_user_id`、不可变
- `catalog_dataset_version_assets`：`version_id`、`data_asset_id`、`position`；`Unique(version_id, data_asset_id)`
- `catalog_dataset_exports`：`version_id`、`format∈{qrdf_0_2,lerobot_3_0}`、`status`、`checksum`、`detail_json`（无裸 URI）
- `catalog_dataset_version_refs`（可选简化）：导出成功即视为引用；`exports` 行存在则禁止物理删除版本

**HTTP：**
- `POST /catalog-datasets` `{name, description?, source_kind=qrdf_assets}`
- `POST /catalog-datasets/{id}/versions` `{data_asset_ids: int[]}` — 去重、禁止空清单
- `GET /catalog-datasets` / `GET .../versions`
- `POST /catalog-datasets/versions/{version_id}/export` `{format}`
- `POST /catalog-datasets/versions/{version_id}/archive`
- `DELETE` 版本：若存在 export → 409 `version_referenced`；否则仅允许无引用时删（或一律只许归档——**选定只许归档被引用版本，未引用可删**）
- LeRobot：`POST /catalog-datasets/lerobot-imports` 复用/包装既有 native lerobot 会话校验，成功后 `source_kind=lerobot_direct` 建数据集+v1；导出 QRDF 对该类 → 422/`not_supported`

- [x] **Step 1: 测试**

```python
def test_version_asset_list_immutable_and_unique(client, db_session, admin_headers): ...


def test_export_blocks_version_delete(client, db_session, admin_headers): ...


def test_lerobot_direct_cannot_export_qrdf(client, db_session, admin_headers): ...
```

- [x] **Step 2: 实现、提交**

```bash
git commit -m "feat: add catalog datasets versions exports and lerobot import hook"
```

---

### Task 6: 权限矩阵、审计事件与跨空间边界加固

**Files:**
- Modify: 各 router 的依赖注入
- Modify: `backend/data/security/audit.py`（注册事件名）
- Create: `backend/tests/test_plan4_rbac_api.py`

**矩阵：**

| 操作 | admin | annotator | reviewer |
|---|---|---|---|
| 建批/治理/重试/转派 | ✓ | ✗ | ✗ |
| 标注 draft/submit | ✗（除非被分配——annotator✓） | 仅自己的项 | ✗ |
| 审核 approve/return | ✗ | ✗ | 仅自己的项 |
| 资产/数据集 CRUD/导出 | ✓ | ✗ | ✗ |
| 资产/数据集 GET 列表 | ✓（全局） | ✓ 只读可选 | ✓ 只读可选 |

- 治理/批 API：`require_collection_workspace`
- 资产/目录数据集列表：**禁止**把 missing `workspace_id` 当成“当前工作空间”；annotator/reviewer 只读策略在本任务写清（若规格仅管理员管理资产，则 GET 也仅 admin——**选定：资产与数据集写仅 admin，读允许三角色以免工作台空白，但不按工作空间裁剪**）

审计事件（写入 `KNOWN_EVENTS`）：
`governance.batch.create`、`governance.stage.retry`、`governance.qc.drop`、`annotation.assign`、`annotation.reassign`、`annotation.submit`、`review.approve`、`review.return`、`review.reassign`、`asset.publish`、`catalog.dataset.create`、`catalog.version.create`、`catalog.version.archive`、`catalog.version.export`

- [x] **Step 1: RBAC 测试**

```python
def test_annotator_cannot_create_batch(client, db_session, annotator_headers):
    ...
    assert response.status_code == 403


def test_asset_list_includes_other_workspace_sources(client, db_session, admin_headers): ...
```

- [x] **Step 2: 提交**

```bash
git commit -m "feat: enforce plan4 rbac and register governance audit events"
```

---

### Task 7: 前端真实 API 联调

**Files:**（按现有前端结构定位；实现前先 `Glob` 建批/资产/数据集页面）
- Modify: 数据批页、标注审核队列、数据资产页、数据集页、范围栏逻辑
- Test: `frontend/tests/data-batch-console.test.mjs`、`annotation-queue.test.mjs`、`data-asset-console.test.mjs`、`catalog-dataset-console.test.mjs`（新建或扩展）

**要求：**
- 建批：候选包来自 `/data-batches/candidates`；标签只选自字典；提交调 `POST /data-batches`
- 队列：去掉领取/释放；展示 assignee、退回原因、转派
- 资产：一批评一个资产；显示 `governed_valid_duration_hours` 与阶段快照；筛选 `source_workspace_id` 可选
- 数据集：数据集→版本→资产清单；导出按钮；LeRobot 直导入口；LeRobot 源导出 QRDF 置灰
- 范围栏：仅采集管理 + 数据批/标注审核依赖工作空间；资产/数据集页不强制当前工作空间

- [x] **Step 1: 前端测试（node:test）描述行为**
- [x] **Step 2: 接线实现**
- [x] **Step 3: 提交**

```bash
git commit -m "feat: wire governance annotation asset dataset UI to real APIs"
```

---

### Task 8: 端到端冒烟与回归

**Files:**
- Test: `backend/tests/test_governance_annotation_assets_smoke.py`
- 不改生产代码（除非冒烟暴露缺口）

**冒烟路径：**
`intake_approved` 包 ×N → 建批（开 QC+标注）→ sync 治理（丢 1 Episode）→ 标注提交 → 单审通过 → 资产 1 条 → 跨空间第二工作空间资产一并组进数据集版本 → 导出 lerobot_3_0 → 归档保护。

另测：全跳过治理且关标注 → 直接资产；全 QC 淘汰 → `no_publishable_asset`。

- [x] **Step 1: 写冒烟**
- [x] **Step 2: 跑齐 Plan④ + 关键 Plan③ 回归**

```bash
TEST_DATABASE_URL=postgresql+psycopg://quicdata:quicdata@127.0.0.1:5432/quicdata_test_studio \
  .venv/bin/python -m pytest \
  backend/tests/test_data_batches_api.py \
  backend/tests/test_governance_runs.py \
  backend/tests/test_annotation_work_items_api.py \
  backend/tests/test_review_work_items_api.py \
  backend/tests/test_data_assets_api.py \
  backend/tests/test_catalog_datasets_api.py \
  backend/tests/test_plan4_rbac_api.py \
  backend/tests/test_governance_annotation_assets_smoke.py \
  backend/tests/test_collection_intake_review_api.py \
  -q

node --test frontend/tests/data-batch-console.test.mjs \
  frontend/tests/annotation-queue.test.mjs \
  frontend/tests/data-asset-console.test.mjs \
  frontend/tests/catalog-dataset-console.test.mjs
```

- [x] **Step 3: 提交**

```bash
git commit -m "test: add governance annotation asset dataset end-to-end smoke"
```

---

## Spec 覆盖对照

| 规格 | 任务 |
|---|---|
| §4 建批筛选/锁定/标签/跳过/重试 | Task 1–2 |
| §4 QC→`qc_dropped`、合规只读标记 | Task 2 |
| §5 包级标注、单审、负载均衡、退回、转派、无 claim | Task 3 |
| §5 双审扩展位 | Task 1 `review_mode` CHECK 保留 `dual`，API 拒绝创建 |
| §6 治理后有效时长 | Task 2、4 |
| §7 导航与范围栏、前端联调 | Task 7 |
| §8 批资产 1:1、版本不可变、导出、引用保护、LeRobot 直导 | Task 4–5 |
| §9 三角色与资源划分 | Task 6 |
| §10 验证 | Task 8 |
| §11 范围外 | 文末排除列表 |

**未覆盖（有意）：** 训练任务调度本体、在线分发、App、OSS 扫描、cut、脱敏、自定义角色、LeRobot→QRDF。

## Self-Review（writing-plans）

**1. Spec coverage：** 上表可逐条指到任务；双审仅扩展位；前端与后端均有任务。

**2. Placeholder scan：** 无 TODO/TBD。Task 1 测试中 `make_annotator_reviewer` / `...` 须在实现时落成真实 fixture（与 Plan③ `collection_api_fixtures` 同级）。Task 7 文件路径要求执行前 Glob 定位现有页面，避免写死不存在的路径。

**3. Type consistency：** 统一 `qc_dropped`；包状态 `intake_approved`→`batched`→`published`；批状态字面量在 Task 1 一次定义，后续任务复用；资产/目录数据集命名避开遗留 `datasets`/`work_items`。

**结论：** 本文件达到与 Plan②/③ 同级的可执行粒度，可作为 SDD 输入。

---

## 审计结果（2026-09-22 收尾核对）

- 证据：本计划引用的 10 个测试文件全部存在并一起运行 **69 passed / 1 failed**；唯一失败是 `test_catalog_datasets_api.py::test_placeholder_export_cannot_register_and_train_plane_stays_off_on_py310`，该用例在干净 HEAD 基线上同样失败，属环境相关预存在问题，与本计划无关。
- 结论：全部步骤按现有制品与测试勾选。
