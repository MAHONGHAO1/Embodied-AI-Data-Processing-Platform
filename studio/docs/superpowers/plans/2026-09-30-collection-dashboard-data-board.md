# 采集概览第一期（共用地基 · 数采看板）与数采任务分配修复 Implementation Plan

<!-- 代码块为插入片段，保留原始缩进，关闭 ruff 格式化 -->
<!-- fmt:off -->

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在数据包上落统计明细，提供按看板切分的实时聚合接口与「数采看板」页面；并修复数采任务的分配入口、分配状态跳变与新建任务拆包时长。

**Architecture:** 解析完成与入库审核时把精确秒数、字节数和采集开始时间写到 `data_packages`（DWD 明细），看板只查 `data_packages` / `collection_projects` / `collection_tasks` / `collection_task_labels`，不读 Episode。后端一个看板一个接口（本期 `GET /api/v1/collection-dashboard/data`），前端采集概览页为外壳 + 全局筛选 + 数采看板组件，图表工具独立成文件。分配修复集中在 `mining-utils.js` 的纯函数与 `app.js` 模板入口，后端任务列表只加一个只读计数。

**Tech Stack:** FastAPI、SQLAlchemy 2、Alembic、PostgreSQL、pytest；Vue 3（全局构建）、Element Plus、ECharts 5.5.1（懒加载）、`node --test`。

**Spec:** `docs/superpowers/specs/2026-09-29-collection-dashboard-data-board-design.md`

## Global Constraints

- 有效时长 = 入库审核有效时长；统计一律用精确秒数（`Numeric(14, 3)`）与字节数（`BigInteger`），展示时才换算。
- 空值表示「尚无此事实」，不得当作 0 参与计算或展示；前端空值显示 `—`。
- 时长、大小的趋势与「今日」按 `captured_started_at`；项目、任务、数据包总量按 `created_at`；数据包今日新增与趋势只统计已冻结包（`intake_valid_duration_s IS NOT NULL`），不随 `basis` 变化。
- 大小只统计准入事实 `objects_json` 中 `ref.bucket_role == "raw"` 的对象；有效大小取审核冻结的那次准入事实。
- 日期范围跨度 ≤ 366 天；`hour` 粒度 ≤ 7 天；默认最近 30 天、`granularity=day`、`basis=valid`、`tz=Asia/Shanghai`。
- 库内时间为无时区 UTC；分桶用 `date_trunc(g, timezone(tz, timezone('UTC', col)))`。
- 看板查询不得读取 `episodes` 表。
- 旧 dashboard（`app.js` 中 `overview` 视图、`/dashboard/*`、`dwd_/dws_/ads_` 表）与旧 `/collection-overview` 接口不改动。
- 目录标签不改。
- 「待分配」唯一定义：包状态为 `pending_assignment`；任务「已完成」= `package_count > 0 && pending_assignment_count === 0`。
- 测试命令（在仓库根目录）：
  - 后端：`TEST_DATABASE_URL='postgresql+psycopg://quicdata:quicdata@127.0.0.1:5432/quicdata_test_dashboard' TEST_REDIS_URL='redis://127.0.0.1:6379/15' PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin <路径> -q`（下文简写为 `$PYTEST <路径>`）
  - 前端：`node --test frontend/tests/<文件>.test.mjs`
  - 基线已知失败：`frontend/tests/admin-console.test.mjs` 中「workspace membership UI models scope without a workspace-level role」在本计划开始前即失败，与本计划无关，不修。
- 提交信息沿用仓库风格：`feat(dashboard): …`、`fix(mining): …`、`test(dashboard): …`，中文描述。

## Review Focus

1. **北京时间零点附近的数据**：UTC 16:00–24:00 采集的包必须落在北京时间次日的桶里，「今日」也按北京时间判定。→ Task 4 `test_trend_buckets_use_shanghai_day_boundary`。
2. **回填前明细为空的已上传包**：不能被算成 0 时长，也不能让接口报错，应计入 `incomplete_packages`。→ Task 4 `test_incomplete_packages_are_counted_not_zeroed`。
3. **审核前多次补传**：再次解析后明细按全部 Episode 重算，不累加。→ Task 2 `test_reparse_recomputes_capture_facts_without_accumulating`。
4. **跨工作空间的项目/任务 ID 与错分类标签**：必须 422，不能静默忽略后返回全量数据。→ Task 5 `test_rejects_foreign_project_and_wrong_label_category`。
5. **进出「查看数据包」抽屉后任务分配状态不变**：状态只依赖列表计数。→ Task 10 `taskAssignDone ignores batches` 与源码断言。

---

## File Structure

后端

| 文件 | 职责 |
|---|---|
| `backend/data/models/data_package.py`（改） | 新增 5 列、约束、索引 |
| `backend/alembic/versions/0011_package_dashboard_facts.py`（新） | 对应迁移 |
| `backend/data/services/package_dashboard_facts.py`（新） | 明细计算：采集事实、入库有效事实、回填重算 |
| `backend/data/services/collection_upload_parse.py`（改） | 两处解析完成写入采集事实 |
| `backend/data/services/collection_intake_review.py`（改） | 审核时写入采集事实与有效事实 |
| `backend/scripts/backfill_package_dashboard_facts.py`（新） | 幂等回填脚本 |
| `backend/data/services/collection_dashboard.py`（新） | 筛选解析、数采看板聚合、CSV |
| `backend/data/routers/collection_dashboard.py`（新） | 路由：参数、权限、格式 |
| `backend/data/main.py`（改） | 注册路由 |
| `backend/data/services/collection_tasks.py`、`backend/data/routers/collection_tasks.py`（改） | 任务列表新增 `pending_assignment_count` |
| `backend/pytest.ini`（改） | 注册 `slow` 标记 |
| 测试：`backend/tests/test_package_dashboard_facts.py`、`test_collection_dashboard_service.py`、`test_collection_dashboard_api.py`、`test_collection_dashboard_perf.py`（新）；`test_collection_upload_parse.py`、`test_collection_tasks_api.py`（改） | |

前端

| 文件 | 职责 |
|---|---|
| `frontend/js/dashboard-charts.js`（新） | 格式化、ECharts 懒加载、实例注册表、折线图配置 |
| `frontend/js/collection-dashboard-filters.js`（新） | 全局筛选栏组件与纯函数 |
| `frontend/js/collection-data-board.js`（新） | 数采看板组件、loader、卡片与 CSV |
| `frontend/js/collection-overview.js`（改写） | 外壳：标签页、筛选状态、选项加载 |
| `frontend/js/page-components.js`（改） | 支持 `deps`，采集概览依赖三个新文件 |
| `frontend/js/api.js`（改） | `getCollectionDashboardData`、`downloadCollectionDashboardCsv` |
| `frontend/js/demo-data.js`（改） | 演示模式的 `/collection-dashboard/data` 与任务列表计数 |
| `frontend/js/mining-utils.js`（改） | `canAssignPackage`、`taskAssignDone`、`packageCountPreview` |
| `frontend/js/app.js`（改） | 分配入口、对话框、状态判定、新建任务单包时长 |
| `frontend/index.html`（改） | `mining-utils.js` 版本号 |
| 测试：`dashboard-charts.test.mjs`、`collection-dashboard-filters.test.mjs`、`collection-data-board.test.mjs`、`mining-assign-entry.test.mjs`（新）；`collection-overview.test.mjs`、`mining-utils.test.mjs`、`demo-mode-flows.test.mjs`（改） | |

---

### Task 1: 数据包统计明细列与明细计算模块

**Files:**
- Modify: `backend/data/models/data_package.py`（`__table_args__` 与列定义，约第 44–110 行）
- Create: `backend/alembic/versions/0011_package_dashboard_facts.py`
- Create: `backend/data/services/package_dashboard_facts.py`
- Test: `backend/tests/test_package_dashboard_facts.py`

**Interfaces:**
- Produces:
  - 列：`captured_started_at: DateTime | None`、`captured_duration_s: Numeric(14,3) | None`、`captured_size_bytes: BigInteger | None`、`intake_valid_duration_s: Numeric(14,3) | None`、`intake_valid_size_bytes: BigInteger | None`
  - `raw_size_bytes(objects: object) -> int`
  - `episode_start_ns(metadata: object) -> int | None`
  - `capture_facts(db: Session, package_id: int) -> dict[str, Any]`（键：三个 `captured_*` 列名）
  - `intake_valid_facts(accepted: Iterable[tuple[Episode, EpisodeAdmissionFact | None]]) -> dict[str, Any]`（键：两个 `intake_valid_*` 列名）
  - `REJECTED_INTAKE_FACTS: dict[str, Any]`（`{"intake_valid_duration_s": Decimal("0.000"), "intake_valid_size_bytes": 0}`）
  - `apply_facts(package: DataPackage, facts: dict[str, Any]) -> None`
  - `recompute_package_dashboard_facts(db: Session, package: DataPackage) -> None`

- [ ] **Step 1: 写失败测试**

`backend/tests/test_package_dashboard_facts.py`：

```python
"""Package-level dashboard facts: exact seconds, raw bytes, capture start."""

from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from tests.collection_api_fixtures import (
    make_project,
    make_workspace,
    seed_package_pending_intake_review,
)
from tests.test_episode_objects import ref, verified_entries

from data.database import Episode
from data.services.collection_intake_review import review_data_package_intake
from data.services.episode_admission import record_episode_admission_fact
from data.services.episode_objects import object_entry
from data.services.package_dashboard_facts import (
    capture_facts,
    episode_start_ns,
    raw_size_bytes,
    recompute_package_dashboard_facts,
)

START_NS = 1_790_000_000_000_000_000  # 2026-09-21T14:13:20Z


def _objects(raw_size: int):
    """Full verified manifest (keeps admission eligible) with a chosen raw size."""
    entries = verified_entries()
    entries[0] = object_entry(
        path="data.mcap", kind="data", ref=ref("raw", f"raw/{uuid4().hex}", size=raw_size)
    )
    return entries


def _timed(db, episode, *, start_ns, seconds, raw_size, attempt=2):
    episode.metadata_json = {
        **episode.metadata_json,
        "timing": {
            "start_timestamp_ns": str(start_ns),
            "end_timestamp_ns": str(start_ns + seconds * 1_000_000_000),
            "duration_s": float(seconds),
        },
    }
    db.flush()
    record_episode_admission_fact(
        db,
        episode_id=episode.id,
        attempt=attempt,
        source_fingerprint=episode.source_fingerprint,
        validation_policy_version="v1",
        integrity_status="passed",
        preview_status="ready",
        output_verification_status="verified",
        qrdf_profile="ego",
        report_ref={"uri": f"db://episode/{episode.id}/report"},
        objects=_objects(raw_size),
    )


def test_raw_size_bytes_counts_only_raw_objects():
    assert raw_size_bytes(_objects(100)) == 100  # process objects (10 bytes each) are ignored
    assert raw_size_bytes([]) == 0
    assert raw_size_bytes(None) == 0
    assert raw_size_bytes([{"ref": {"bucket_role": "raw", "size_bytes": True}}]) == 0


def test_episode_start_ns_reads_timing():
    assert episode_start_ns({"timing": {"start_timestamp_ns": "12"}}) == 12
    assert episode_start_ns({"timing": {"duration_s": 3}}) is None
    assert episode_start_ns(None) is None


def test_capture_facts_use_exact_seconds_raw_bytes_and_earliest_start(db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package, episodes = seed_package_pending_intake_review(db_session, workspace, project)
    _timed(db_session, episodes[0], start_ns=START_NS + 5_000_000_000, seconds=1800, raw_size=100)
    _timed(db_session, episodes[1], start_ns=START_NS, seconds=1201, raw_size=50)
    db_session.commit()

    facts = capture_facts(db_session, package.id)

    assert facts["captured_duration_s"] == Decimal("3001.000")
    assert facts["captured_size_bytes"] == 150
    assert facts["captured_started_at"] == datetime(2026, 9, 21, 14, 13, 20)


def test_capture_facts_are_unknown_without_episodes(db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package, episodes = seed_package_pending_intake_review(db_session, workspace, project)
    for episode in episodes:
        episode.data_package_id = None
    db_session.commit()

    assert capture_facts(db_session, package.id) == {
        "captured_started_at": None,
        "captured_duration_s": None,
        "captured_size_bytes": None,
    }


def test_intake_review_writes_capture_and_valid_facts(db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package, episodes = seed_package_pending_intake_review(db_session, workspace, project)
    _timed(db_session, episodes[0], start_ns=START_NS, seconds=1800, raw_size=100)
    _timed(db_session, episodes[1], start_ns=START_NS, seconds=600, raw_size=40)
    db_session.commit()

    review_data_package_intake(
        db_session,
        workspace_id=workspace.id,
        data_package_id=package.id,
        reviewer_user_id=None,
        verdict="approved",
        rejected_episode_ids=[episodes[1].id],
        reason="",
    )
    db_session.commit()
    db_session.refresh(package)

    assert package.captured_duration_s == Decimal("2400.000")
    assert package.captured_size_bytes == 140
    assert package.intake_valid_duration_s == Decimal("1800.000")
    assert package.intake_valid_size_bytes == 100


def test_rejected_package_has_zero_valid_facts(db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package, episodes = seed_package_pending_intake_review(db_session, workspace, project)
    _timed(db_session, episodes[0], start_ns=START_NS, seconds=1800, raw_size=100)
    db_session.commit()

    review_data_package_intake(
        db_session,
        workspace_id=workspace.id,
        data_package_id=package.id,
        reviewer_user_id=None,
        verdict="rejected",
        rejected_episode_ids=[],
        reason="整包不合格",
    )
    db_session.commit()
    db_session.refresh(package)

    assert package.intake_valid_duration_s == Decimal("0.000")
    assert package.intake_valid_size_bytes == 0
    assert package.captured_duration_s is not None


def test_recompute_uses_frozen_attempt_and_is_idempotent(db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package, episodes = seed_package_pending_intake_review(db_session, workspace, project)
    _timed(db_session, episodes[0], start_ns=START_NS, seconds=1800, raw_size=100)
    _timed(db_session, episodes[1], start_ns=START_NS, seconds=600, raw_size=40)
    db_session.commit()
    review_data_package_intake(
        db_session,
        workspace_id=workspace.id,
        data_package_id=package.id,
        reviewer_user_id=None,
        verdict="approved",
        rejected_episode_ids=[],
        reason="",
    )
    db_session.commit()
    # Simulate legacy rows: clear facts, then a later admission attempt changes the current object size.
    for column in (
        "captured_started_at",
        "captured_duration_s",
        "captured_size_bytes",
        "intake_valid_duration_s",
        "intake_valid_size_bytes",
    ):
        setattr(package, column, None)
    _timed(db_session, episodes[0], start_ns=START_NS, seconds=1800, raw_size=999, attempt=3)
    db_session.commit()

    recompute_package_dashboard_facts(db_session, package)
    db_session.commit()
    first = (package.intake_valid_duration_s, package.intake_valid_size_bytes)
    recompute_package_dashboard_facts(db_session, package)
    db_session.commit()

    assert first == (Decimal("2400.000"), 140)
    assert (package.intake_valid_duration_s, package.intake_valid_size_bytes) == first
    assert package.captured_size_bytes == 999 + 40
    assert db_session.query(Episode).filter(Episode.data_package_id == package.id).count() == 2
```

- [ ] **Step 2: 运行测试确认失败**

Run: `$PYTEST backend/tests/test_package_dashboard_facts.py`
Expected: FAIL，`ModuleNotFoundError: No module named 'data.services.package_dashboard_facts'`

- [ ] **Step 3: 加列、约束、索引与迁移**

在 `backend/data/models/data_package.py` 的 `DataPackage` 中，`governed_valid_duration_hours` 之后加：

```python
    captured_started_at = Column(DateTime, nullable=True)
    captured_duration_s = Column(Numeric(14, 3), nullable=True)
    captured_size_bytes = Column(BigInteger, nullable=True)
    intake_valid_duration_s = Column(Numeric(14, 3), nullable=True)
    intake_valid_size_bytes = Column(BigInteger, nullable=True)
```

（确认文件顶部 `from sqlalchemy import` 含 `BigInteger`，没有则补上。）

在 `__table_args__` 的两个 `Index(...)` 之前加：

```python
        CheckConstraint(
            "(captured_duration_s IS NULL OR captured_duration_s >= 0) "
            "AND (captured_size_bytes IS NULL OR captured_size_bytes >= 0) "
            "AND (intake_valid_duration_s IS NULL OR intake_valid_duration_s >= 0) "
            "AND (intake_valid_size_bytes IS NULL OR intake_valid_size_bytes >= 0)",
            name="ck_data_packages_dashboard_facts_non_negative",
        ),
        CheckConstraint(
            "(intake_valid_duration_s IS NULL OR captured_duration_s IS NULL "
            "OR intake_valid_duration_s <= captured_duration_s) "
            "AND (intake_valid_size_bytes IS NULL OR captured_size_bytes IS NULL "
            "OR intake_valid_size_bytes <= captured_size_bytes)",
            name="ck_data_packages_dashboard_valid_within_captured",
        ),
        Index("ix_data_packages_workspace_captured", "workspace_id", "captured_started_at"),
        Index("ix_data_packages_task_captured", "collection_task_id", "captured_started_at"),
```

`backend/alembic/versions/0011_package_dashboard_facts.py`：

```python
"""Data packages carry exact dashboard facts written at parse and intake review."""

import sqlalchemy as sa
from alembic import op

revision = "0011_package_dashboard_facts"
down_revision = "0010_admission_integrity_source"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("data_packages", sa.Column("captured_started_at", sa.DateTime(), nullable=True))
    op.add_column("data_packages", sa.Column("captured_duration_s", sa.Numeric(14, 3), nullable=True))
    op.add_column("data_packages", sa.Column("captured_size_bytes", sa.BigInteger(), nullable=True))
    op.add_column("data_packages", sa.Column("intake_valid_duration_s", sa.Numeric(14, 3), nullable=True))
    op.add_column("data_packages", sa.Column("intake_valid_size_bytes", sa.BigInteger(), nullable=True))
    op.create_check_constraint(
        "ck_data_packages_dashboard_facts_non_negative",
        "data_packages",
        "(captured_duration_s IS NULL OR captured_duration_s >= 0) "
        "AND (captured_size_bytes IS NULL OR captured_size_bytes >= 0) "
        "AND (intake_valid_duration_s IS NULL OR intake_valid_duration_s >= 0) "
        "AND (intake_valid_size_bytes IS NULL OR intake_valid_size_bytes >= 0)",
    )
    op.create_check_constraint(
        "ck_data_packages_dashboard_valid_within_captured",
        "data_packages",
        "(intake_valid_duration_s IS NULL OR captured_duration_s IS NULL "
        "OR intake_valid_duration_s <= captured_duration_s) "
        "AND (intake_valid_size_bytes IS NULL OR captured_size_bytes IS NULL "
        "OR intake_valid_size_bytes <= captured_size_bytes)",
    )
    op.create_index(
        "ix_data_packages_workspace_captured", "data_packages", ["workspace_id", "captured_started_at"]
    )
    op.create_index(
        "ix_data_packages_task_captured", "data_packages", ["collection_task_id", "captured_started_at"]
    )


def downgrade():
    op.drop_index("ix_data_packages_task_captured", table_name="data_packages")
    op.drop_index("ix_data_packages_workspace_captured", table_name="data_packages")
    op.drop_constraint("ck_data_packages_dashboard_valid_within_captured", "data_packages", type_="check")
    op.drop_constraint("ck_data_packages_dashboard_facts_non_negative", "data_packages", type_="check")
    for column in (
        "intake_valid_size_bytes",
        "intake_valid_duration_s",
        "captured_size_bytes",
        "captured_duration_s",
        "captured_started_at",
    ):
        op.drop_column("data_packages", column)
```

- [ ] **Step 4: 写明细计算模块**

`backend/data/services/package_dashboard_facts.py`：

```python
"""Package-level dashboard facts derived once at parse and intake review.

Dashboards read these columns instead of Episode JSON. Every value is exact
(seconds with millisecond precision, raw bytes) and NULL means "not known yet".
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy.orm import Session

from data.database import Episode
from data.models.data_package import DataPackage, PackageIntakeReview
from data.models.episode_admission import EpisodeAdmissionFact
from data.services.collection_duration import episode_duration_seconds
from data.services.episode_visibility import IMPORT_PLACEHOLDER_WORKFLOW_STATUSES

_SECONDS_QUANTUM = Decimal("0.001")
_EPOCH = datetime(1970, 1, 1)

CAPTURE_FACT_COLUMNS = ("captured_started_at", "captured_duration_s", "captured_size_bytes")
REJECTED_INTAKE_FACTS: dict[str, Any] = {
    "intake_valid_duration_s": Decimal("0.000"),
    "intake_valid_size_bytes": 0,
}


def _seconds(value: Decimal) -> Decimal:
    return value.quantize(_SECONDS_QUANTUM, rounding=ROUND_HALF_UP)


def raw_size_bytes(objects: object) -> int:
    """Sum raw-bucket object sizes of one admission manifest; malformed entries count 0."""
    if not isinstance(objects, list):
        return 0
    total = 0
    for entry in objects:
        ref = entry.get("ref") if isinstance(entry, dict) else None
        if not isinstance(ref, dict) or ref.get("bucket_role") != "raw":
            continue
        size = ref.get("size_bytes")
        if isinstance(size, int) and not isinstance(size, bool) and size >= 0:
            total += size
    return total


def episode_start_ns(metadata: object) -> int | None:
    timing = metadata.get("timing") if isinstance(metadata, dict) else None
    value = timing.get("start_timestamp_ns") if isinstance(timing, dict) else None
    text = str(value) if value is not None else ""
    return int(text) if text.isdigit() else None


def _source_episodes(db: Session, package_id: int) -> list[Episode]:
    return (
        db.query(Episode)
        .filter(
            Episode.data_package_id == package_id,
            Episode.kind == "source",
            Episode.workflow_status.notin_(IMPORT_PLACEHOLDER_WORKFLOW_STATUSES),
        )
        .order_by(Episode.id.asc())
        .all()
    )


def capture_facts(db: Session, package_id: int) -> dict[str, Any]:
    """Recompute capture facts from every current source Episode (never incremental)."""
    episodes = _source_episodes(db, package_id)
    if not episodes:
        return dict.fromkeys(CAPTURE_FACT_COLUMNS)
    durations = [episode_duration_seconds(episode.metadata_json) for episode in episodes]
    starts = [
        start
        for start in (episode_start_ns(episode.metadata_json) for episode in episodes)
        if start is not None
    ]
    facts = (
        db.query(EpisodeAdmissionFact)
        .filter(
            EpisodeAdmissionFact.episode_id.in_([episode.id for episode in episodes]),
            EpisodeAdmissionFact.is_current.is_(True),
        )
        .all()
    )
    return {
        "captured_started_at": (
            _EPOCH + timedelta(microseconds=min(starts) // 1000) if starts else None
        ),
        "captured_duration_s": (
            None
            if any(duration is None for duration in durations)
            else _seconds(sum(durations, Decimal(0)))
        ),
        "captured_size_bytes": sum(raw_size_bytes(fact.objects_json) for fact in facts),
    }


def intake_valid_facts(
    accepted: Iterable[tuple[Episode, EpisodeAdmissionFact | None]],
) -> dict[str, Any]:
    """Valid facts from accepted Episodes and the admission attempt frozen by the review."""
    duration = Decimal(0)
    size = 0
    for episode, fact in accepted:
        duration += episode_duration_seconds(episode.metadata_json) or Decimal(0)
        size += raw_size_bytes(fact.objects_json if fact is not None else None)
    return {"intake_valid_duration_s": _seconds(duration), "intake_valid_size_bytes": size}


def apply_facts(package: DataPackage, facts: dict[str, Any]) -> None:
    for column, value in facts.items():
        setattr(package, column, value)


def recompute_package_dashboard_facts(db: Session, package: DataPackage) -> None:
    """Rebuild all five facts from durable rows; used by the backfill script."""
    apply_facts(package, capture_facts(db, package.id))
    review = (
        db.query(PackageIntakeReview)
        .filter(PackageIntakeReview.data_package_id == package.id)
        .order_by(PackageIntakeReview.id.desc())
        .first()
    )
    if review is None:
        apply_facts(package, {"intake_valid_duration_s": None, "intake_valid_size_bytes": None})
        return
    if review.verdict != "approved":
        apply_facts(package, REJECTED_INTAKE_FACTS)
        return
    accepted_ids = {int(value) for value in review.accepted_episode_ids_json or []}
    attempts = review.fact_attempts_json if isinstance(review.fact_attempts_json, dict) else {}
    rows: list[tuple[Episode, EpisodeAdmissionFact | None]] = []
    for episode in _source_episodes(db, package.id):
        if episode.id not in accepted_ids:
            continue
        attempt = attempts.get(str(episode.id))
        fact = (
            db.query(EpisodeAdmissionFact)
            .filter(
                EpisodeAdmissionFact.episode_id == episode.id,
                EpisodeAdmissionFact.attempt == int(attempt),
            )
            .one_or_none()
            if attempt is not None
            else None
        )
        rows.append((episode, fact))
    apply_facts(package, intake_valid_facts(rows))
```

- [ ] **Step 5: 审核时写入明细**

在 `backend/data/services/collection_intake_review.py` 顶部导入：

```python
from data.services.package_dashboard_facts import (
    REJECTED_INTAKE_FACTS,
    apply_facts,
    capture_facts,
    intake_valid_facts,
)
```

驳回分支（`package.intake_valid_duration_hours = Decimal("0.00")` 之后）加：

```python
        apply_facts(package, capture_facts(db, package.id))
        apply_facts(package, REJECTED_INTAKE_FACTS)
```

通过分支（`package.intake_valid_duration_hours = valid_duration.quantize(...)` 之后）加：

```python
        apply_facts(package, capture_facts(db, package.id))
        apply_facts(
            package,
            intake_valid_facts(
                (episode, fact)
                for episode, fact, _eligible, _reason in admission_rows
                if episode.id in accepted_ids
            ),
        )
```

（审核时重算采集事实：审核后包只读，此时的 Episode 与准入事实是最终版本，也保证 `有效 ≤ 采集` 约束成立。）

- [ ] **Step 6: 运行测试确认通过**

Run: `$PYTEST backend/tests/test_package_dashboard_facts.py backend/tests/test_collection_intake_review_api.py backend/tests/test_baseline_migration.py`
Expected: PASS

- [ ] **Step 7: 提交**

```bash
git add backend/data/models/data_package.py backend/alembic/versions/0011_package_dashboard_facts.py backend/data/services/package_dashboard_facts.py backend/data/services/collection_intake_review.py backend/tests/test_package_dashboard_facts.py
git commit -m "feat(dashboard): 数据包落精确时长、原始大小与采集开始时间明细"
```

---

### Task 2: 解析完成时写入采集明细

**Files:**
- Modify: `backend/data/services/collection_upload_parse.py`（约第 324–347 行持久化来源路径；约第 474–492 行 fixture 路径）
- Test: `backend/tests/test_collection_upload_parse.py`

**Interfaces:**
- Consumes: `capture_facts(db, package_id)`、`apply_facts(package, facts)`（Task 1）

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_collection_upload_parse.py` 末尾追加（`_fixture_session` 已在文件内）：

```python
from datetime import datetime as _datetime


def test_parse_writes_exact_capture_facts(db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package = make_assigned_package(db_session, workspace, project)
    start_ns = 1_790_000_000_000_000_000
    upload_session = _fixture_session(
        db_session,
        workspace,
        project,
        package,
        start_ns=start_ns,
        end_ns=start_ns + 1_234_567_000_000,
    )

    parse_collection_upload_session(db_session, session_id=upload_session.id)
    db_session.expire_all()
    db_session.refresh(package)

    assert package.captured_duration_s == Decimal("1234.567")
    assert package.captured_started_at == _datetime(2026, 9, 21, 14, 13, 20)
    assert package.captured_size_bytes == 1  # fixture raw object is 1 byte
    assert package.intake_valid_duration_s is None


def test_reparse_recomputes_capture_facts_without_accumulating(db_session):
    # Same preparation as test_parse_reuses_package_episode_by_source_fingerprint:
    # a second upload session re-declares the same source before intake review.
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package = make_assigned_package(db_session, workspace, project)
    fingerprint = uuid4().hex
    first = _fixture_session(db_session, workspace, project, package, source_fingerprint=fingerprint)
    parse_collection_upload_session(db_session, session_id=first.id)
    db_session.expire_all()
    db_session.refresh(package)
    package.status = "uploading"
    db_session.commit()
    second = _fixture_session(db_session, workspace, project, package, source_fingerprint=fingerprint)

    parse_collection_upload_session(db_session, session_id=second.id)
    db_session.expire_all()
    db_session.refresh(package)

    assert package.captured_duration_s == Decimal("3600.000")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `$PYTEST backend/tests/test_collection_upload_parse.py -k "capture_facts"`
Expected: FAIL，`captured_duration_s` 为 `None`

- [ ] **Step 3: 两处写入**

在 `collection_upload_parse.py` 顶部导入：

```python
from data.services.package_dashboard_facts import apply_facts, capture_facts
```

持久化来源路径：在

```python
        package.captured_duration_hours = (duration / Decimal(3600)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
```

之后加一行：

```python
        apply_facts(package, capture_facts(db, package.id))
```

fixture 路径：把第一个 `_advance_package(... "ingested", ...)` 调用改为带上采集事实（Episode 与准入事实在循环中已由 `record_episode_admission_fact` flush）：

```python
            _advance_package(
                db,
                package.id,
                ("parsing",),
                "ingested",
                capture_mode=str(package_facts["capture"]["mode"]),
                qrdf_facts_json=package_facts,
                captured_duration_hours=hours,
                upload_completed_at=package.upload_completed_at or now,
                parse_error_code="",
                parse_error_message="",
                **capture_facts(db, package.id),
            )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `$PYTEST backend/tests/test_collection_upload_parse.py backend/tests/test_collection_upload_intake_smoke.py backend/tests/test_collection_admission_worker.py`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/data/services/collection_upload_parse.py backend/tests/test_collection_upload_parse.py
git commit -m "feat(dashboard): 解析完成按全部 Episode 重算数据包采集明细"
```

---

### Task 3: 存量数据回填脚本

**Files:**
- Create: `backend/scripts/backfill_package_dashboard_facts.py`
- Test: `backend/tests/test_package_dashboard_facts.py`（追加）

**Interfaces:**
- Consumes: `recompute_package_dashboard_facts`（Task 1）
- Produces: `backfill(db: Session, *, batch_size: int = 200) -> dict[str, Any]`，返回 `{"packages": int, "mismatches": list[dict]}`；`mismatches` 为新有效秒数换算小时与旧 `intake_valid_duration_hours` 相差超过 0.005 的包。

- [ ] **Step 1: 写失败测试**

追加到 `backend/tests/test_package_dashboard_facts.py`：

```python
from scripts.backfill_package_dashboard_facts import backfill


def test_backfill_is_idempotent_and_reports_hour_mismatch(db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package, episodes = seed_package_pending_intake_review(db_session, workspace, project)
    _timed(db_session, episodes[0], start_ns=START_NS, seconds=1800, raw_size=100)
    _timed(db_session, episodes[1], start_ns=START_NS, seconds=600, raw_size=40)
    db_session.commit()
    review_data_package_intake(
        db_session,
        workspace_id=workspace.id,
        data_package_id=package.id,
        reviewer_user_id=None,
        verdict="approved",
        rejected_episode_ids=[],
        reason="",
    )
    db_session.commit()
    package.intake_valid_duration_s = None
    package.intake_valid_duration_hours = Decimal("9.00")  # legacy drift
    db_session.commit()

    first = backfill(db_session)
    second = backfill(db_session)
    db_session.refresh(package)

    assert package.intake_valid_duration_s == Decimal("2400.000")
    assert first["packages"] >= 1
    assert [row["data_package_id"] for row in first["mismatches"]] == [package.id]
    assert second["mismatches"] == first["mismatches"]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `$PYTEST backend/tests/test_package_dashboard_facts.py -k backfill`
Expected: FAIL，`ModuleNotFoundError: No module named 'scripts.backfill_package_dashboard_facts'`

- [ ] **Step 3: 写脚本**

`backend/scripts/backfill_package_dashboard_facts.py`：

```python
"""Idempotently rebuild data package dashboard facts from durable rows.

Run after migration 0011 and before switching the frontend:
    python -m scripts.backfill_package_dashboard_facts
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from data.models.data_package import DataPackage
from data.services.package_dashboard_facts import recompute_package_dashboard_facts

_HOUR_TOLERANCE = Decimal("0.005")


def backfill(db: Session, *, batch_size: int = 200) -> dict[str, Any]:
    last_id = 0
    count = 0
    mismatches: list[dict[str, Any]] = []
    while True:
        packages = (
            db.query(DataPackage)
            .filter(DataPackage.id > last_id)
            .order_by(DataPackage.id.asc())
            .limit(batch_size)
            .all()
        )
        if not packages:
            break
        for package in packages:
            recompute_package_dashboard_facts(db, package)
            count += 1
            new_seconds = package.intake_valid_duration_s
            old_hours = package.intake_valid_duration_hours
            if new_seconds is not None and old_hours is not None:
                delta = abs(Decimal(new_seconds) / Decimal(3600) - Decimal(old_hours))
                if delta > _HOUR_TOLERANCE:
                    mismatches.append(
                        {
                            "data_package_id": package.id,
                            "intake_valid_duration_s": str(new_seconds),
                            "intake_valid_duration_hours": str(old_hours),
                        }
                    )
            last_id = package.id
        db.commit()
    return {"packages": count, "mismatches": mismatches}


def main() -> None:
    from data.database import SessionLocal

    db = SessionLocal()
    try:
        print(json.dumps(backfill(db), ensure_ascii=False, indent=2))
    finally:
        db.close()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `$PYTEST backend/tests/test_package_dashboard_facts.py`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/scripts/backfill_package_dashboard_facts.py backend/tests/test_package_dashboard_facts.py
git commit -m "feat(dashboard): 数据包统计明细幂等回填脚本"
```

---

### Task 4: 数采看板聚合服务

**Files:**
- Create: `backend/data/services/collection_dashboard.py`
- Test: `backend/tests/test_collection_dashboard_service.py`

**Interfaces:**
- Produces:
  - `class DashboardFilterError(ValueError)`
  - `@dataclass(frozen=True) class DashboardFilters`：`workspace_id: int`、`start_date: date`、`end_date: date`、`project_ids: tuple[int, ...]`、`task_ids: tuple[int, ...]`、`scene_label_ids: tuple[int, ...]`、`purpose_label_ids: tuple[int, ...]`、`granularity: str`、`basis: str`、`tz: str`
  - `resolve_filters(db, *, workspace_id, start_date=None, end_date=None, project_ids=(), task_ids=(), scene_label_ids=(), purpose_label_ids=(), granularity="day", basis="valid", tz="Asia/Shanghai", now: datetime | None = None) -> DashboardFilters`
  - `data_board(db, filters: DashboardFilters, *, now: datetime | None = None) -> dict[str, Any]`（结构见 spec §4.4）
  - `data_board_csv(payload: dict[str, Any]) -> str`（带 BOM）
  - `csv_filename(filters: DashboardFilters) -> str`

- [ ] **Step 1: 写失败测试**

`backend/tests/test_collection_dashboard_service.py`：

```python
"""Collection data board aggregates over package facts only."""

from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from tests.collection_api_fixtures import (
    make_collector,
    make_label,
    make_project,
    make_task,
    make_workspace,
)

from data.models.collection_core import CollectionTaskLabel
from data.models.data_package import DataPackage
from data.services.collection_dashboard import (
    DashboardFilterError,
    data_board,
    data_board_csv,
    resolve_filters,
)

NOW = datetime(2026, 9, 29, 4, 0, tzinfo=timezone.utc)  # 12:00 Asia/Shanghai


def _package(db, task, *, status="pending_assignment", created_at=None, captured_at=None,
             captured_s=None, valid_s=None, size=None, valid_size=None, uploaded=False):
    collector = make_collector(db, task_workspace(db, task)) if status != "pending_assignment" else None
    package = DataPackage(
        package_uid=f"pkg_{uuid4().hex}",
        workspace_id=task.workspace_id,
        collection_project_id=task.collection_project_id,
        collection_task_id=task.id,
        status=status,
        target_duration_hours=Decimal("2.00"),
        responsible_collector_id=collector.id if collector else None,
        operator_collector_id=collector.id if collector else None,
        created_at=created_at or datetime(2026, 9, 20, 2, 0),
        captured_started_at=captured_at,
        captured_duration_s=captured_s,
        intake_valid_duration_s=valid_s,
        captured_size_bytes=size,
        intake_valid_size_bytes=valid_size,
        upload_completed_at=datetime(2026, 9, 28, 0, 0) if (uploaded or captured_s is not None) else None,
    )
    db.add(package)
    db.commit()
    return package


def task_workspace(db, task):
    from data.database import Workspace

    return db.get(Workspace, task.workspace_id)


def _filters(db, workspace, **overrides):
    params = {"start_date": date(2026, 9, 1), "end_date": date(2026, 9, 29), "now": NOW}
    params.update(overrides)
    return resolve_filters(db, workspace_id=workspace.id, **params)


def test_totals_status_counts_and_frozen_package_trend(db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    task = make_task(db_session, project)
    _package(db_session, task)  # pending assignment, never uploaded
    _package(db_session, task, status="assigned")
    _package(db_session, task, status="pending_intake_review", captured_at=datetime(2026, 9, 28, 1, 0),
             captured_s=Decimal("1800.000"), size=100)
    _package(db_session, task, status="intake_approved", captured_at=datetime(2026, 9, 29, 1, 0),
             captured_s=Decimal("3600.000"), valid_s=Decimal("3000.000"), size=200, valid_size=150)
    _package(db_session, task, status="voided", captured_at=datetime(2026, 9, 29, 2, 0),
             captured_s=Decimal("600.000"), valid_s=Decimal("0.000"), size=50, valid_size=0)

    payload = data_board(db_session, _filters(db_session, workspace), now=NOW)

    assert payload["packages"]["total"] == 5
    assert payload["packages"]["status_counts"] == {
        "pending_assignment": 1, "assigned": 1, "other": 2, "voided": 1,
    }
    assert payload["packages"]["today"] == 2  # frozen: approved + rejected, captured today
    assert payload["duration"]["basis"] == "valid"
    assert payload["duration"]["total_s"] == 3000.0
    assert payload["duration"]["today_s"] == 3000.0
    assert payload["duration"]["pending_review_duration_s"] == 1800.0
    assert payload["size"]["total_bytes"] == 150
    assert payload["projects"]["total"] == 1
    assert payload["tasks"]["total"] == 1


def test_basis_all_uses_captured_values(db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    task = make_task(db_session, project)
    _package(db_session, task, status="pending_intake_review", captured_at=datetime(2026, 9, 28, 1, 0),
             captured_s=Decimal("1800.000"), size=100)

    payload = data_board(db_session, _filters(db_session, workspace, basis="all"), now=NOW)

    assert payload["duration"]["total_s"] == 1800.0
    assert payload["size"]["total_bytes"] == 100
    assert payload["packages"]["today"] == 0  # not frozen


def test_trend_buckets_use_shanghai_day_boundary(db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    task = make_task(db_session, project)
    # 2026-09-27 16:30 UTC == 2026-09-28 00:30 Asia/Shanghai
    _package(db_session, task, status="intake_approved", captured_at=datetime(2026, 9, 27, 16, 30),
             captured_s=Decimal("60.000"), valid_s=Decimal("60.000"), size=1, valid_size=1)

    payload = data_board(
        db_session,
        _filters(db_session, workspace, start_date=date(2026, 9, 27), end_date=date(2026, 9, 28)),
        now=NOW,
    )

    assert payload["duration"]["trend"] == [
        {"bucket": "2026-09-27", "value": 0.0},
        {"bucket": "2026-09-28", "value": 60.0},
    ]
    assert payload["packages"]["trend"] == [
        {"bucket": "2026-09-27", "value": 0},
        {"bucket": "2026-09-28", "value": 1},
    ]


def test_hour_and_month_buckets_are_filled(db_session):
    workspace = make_workspace(db_session)
    filters = _filters(db_session, workspace, start_date=date(2026, 9, 28), end_date=date(2026, 9, 28),
                       granularity="hour")
    hours = data_board(db_session, filters, now=NOW)["duration"]["trend"]
    assert len(hours) == 24
    assert hours[0]["bucket"] == "2026-09-28 00:00"

    months = data_board(
        db_session,
        _filters(db_session, workspace, start_date=date(2026, 7, 15), end_date=date(2026, 9, 1),
                 granularity="month"),
        now=NOW,
    )["projects"]["trend"]
    assert [point["bucket"] for point in months] == ["2026-07", "2026-08", "2026-09"]


def test_incomplete_packages_are_counted_not_zeroed(db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    task = make_task(db_session, project)
    _package(db_session, task, status="parsing", uploaded=True)

    payload = data_board(db_session, _filters(db_session, workspace, basis="all"), now=NOW)

    assert payload["incomplete_packages"] == 1
    assert payload["duration"]["total_s"] == 0.0


def test_task_label_filters_narrow_packages_tasks_and_projects(db_session):
    workspace = make_workspace(db_session)
    project_a = make_project(db_session, workspace)
    project_b = make_project(db_session, workspace)
    scene = make_label(db_session, category="scene")
    task_a = make_task(db_session, project_a)
    task_b = make_task(db_session, project_b)
    db_session.add(CollectionTaskLabel(collection_task_id=task_a.id, collection_label_id=scene.id))
    db_session.commit()
    _package(db_session, task_a)
    _package(db_session, task_b)

    payload = data_board(db_session, _filters(db_session, workspace, scene_label_ids=[scene.id]), now=NOW)

    assert payload["projects"]["total"] == 1
    assert payload["tasks"]["total"] == 1
    assert payload["packages"]["total"] == 1


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"start_date": date(2026, 9, 29), "end_date": date(2026, 9, 1)}, "start_date"),
        ({"start_date": date(2025, 1, 1), "end_date": date(2026, 9, 29)}, "366"),
        ({"granularity": "hour"}, "7"),
        ({"granularity": "week"}, "granularity"),
        ({"basis": "gross"}, "basis"),
        ({"tz": "Mars/Base"}, "tz"),
    ],
)
def test_invalid_filters_are_rejected(db_session, overrides, message):
    workspace = make_workspace(db_session)
    with pytest.raises(DashboardFilterError, match=message):
        _filters(db_session, workspace, **overrides)


def test_rejects_foreign_project_and_wrong_label_category(db_session):
    workspace = make_workspace(db_session)
    other = make_workspace(db_session)
    foreign = make_project(db_session, other)
    purpose = make_label(db_session, category="purpose")
    with pytest.raises(DashboardFilterError, match="project"):
        _filters(db_session, workspace, project_ids=[foreign.id])
    with pytest.raises(DashboardFilterError, match="scene"):
        _filters(db_session, workspace, scene_label_ids=[purpose.id])


def test_default_range_is_last_30_days(db_session):
    workspace = make_workspace(db_session)
    filters = resolve_filters(db_session, workspace_id=workspace.id, now=NOW)
    assert (filters.start_date, filters.end_date) == (date(2026, 8, 31), date(2026, 9, 29))


def test_csv_has_bom_header_and_one_row_per_bucket(db_session):
    workspace = make_workspace(db_session)
    payload = data_board(
        db_session,
        _filters(db_session, workspace, start_date=date(2026, 9, 28), end_date=date(2026, 9, 29)),
        now=NOW,
    )
    text = data_board_csv(payload)
    lines = text.lstrip("﻿").strip().splitlines()
    assert text.startswith("﻿")
    assert lines[0] == "时间,项目数,任务数,数据包数,时长（秒，有效）,大小（字节，有效）"
    assert [line.split(",")[0] for line in lines[1:]] == ["2026-09-28", "2026-09-29"]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `$PYTEST backend/tests/test_collection_dashboard_service.py`
Expected: FAIL，`ModuleNotFoundError: No module named 'data.services.collection_dashboard'`

- [ ] **Step 3: 实现服务**

`backend/data/services/collection_dashboard.py`：

```python
"""Collection dashboards: one aggregate function per board over package facts.

Queries touch only data_packages, collection_projects, collection_tasks and
collection_task_labels. A daily summary table can later replace the package
reads without changing the payload.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import case, exists, func, literal, select
from sqlalchemy.orm import Session

from data.models.collection_config import CollectionLabel
from data.models.collection_core import CollectionProject, CollectionTask, CollectionTaskLabel
from data.models.data_package import DataPackage

GRANULARITIES = ("hour", "day", "month")
BASES = ("all", "valid")
MAX_RANGE_DAYS = 366
MAX_HOUR_RANGE_DAYS = 7
DEFAULT_RANGE_DAYS = 30
_BUCKET_FORMATS = {"hour": "%Y-%m-%d %H:00", "day": "%Y-%m-%d", "month": "%Y-%m"}


class DashboardFilterError(ValueError):
    """Raised for filters the dashboard refuses to interpret."""


@dataclass(frozen=True)
class DashboardFilters:
    workspace_id: int
    start_date: date
    end_date: date
    project_ids: tuple[int, ...]
    task_ids: tuple[int, ...]
    scene_label_ids: tuple[int, ...]
    purpose_label_ids: tuple[int, ...]
    granularity: str
    basis: str
    tz: str

    @property
    def has_task_filters(self) -> bool:
        return bool(self.task_ids or self.scene_label_ids or self.purpose_label_ids)


def _ids(values) -> tuple[int, ...]:
    return tuple(sorted({int(value) for value in values or ()}))


def resolve_filters(
    db: Session,
    *,
    workspace_id: int,
    start_date: date | None = None,
    end_date: date | None = None,
    project_ids=(),
    task_ids=(),
    scene_label_ids=(),
    purpose_label_ids=(),
    granularity: str = "day",
    basis: str = "valid",
    tz: str = "Asia/Shanghai",
    now: datetime | None = None,
) -> DashboardFilters:
    try:
        zone = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise DashboardFilterError(f"unknown tz: {tz}") from exc
    if granularity not in GRANULARITIES:
        raise DashboardFilterError(f"granularity must be one of {', '.join(GRANULARITIES)}")
    if basis not in BASES:
        raise DashboardFilterError(f"basis must be one of {', '.join(BASES)}")
    today = (now or datetime.now(timezone.utc)).astimezone(zone).date()
    end = end_date or today
    start = start_date or end - timedelta(days=DEFAULT_RANGE_DAYS - 1)
    if start > end:
        raise DashboardFilterError("start_date must not be after end_date")
    span = (end - start).days + 1
    if span > MAX_RANGE_DAYS:
        raise DashboardFilterError(f"date range must not exceed {MAX_RANGE_DAYS} days")
    if granularity == "hour" and span > MAX_HOUR_RANGE_DAYS:
        raise DashboardFilterError(f"hour granularity allows at most {MAX_HOUR_RANGE_DAYS} days")
    filters = DashboardFilters(
        workspace_id=workspace_id,
        start_date=start,
        end_date=end,
        project_ids=_ids(project_ids),
        task_ids=_ids(task_ids),
        scene_label_ids=_ids(scene_label_ids),
        purpose_label_ids=_ids(purpose_label_ids),
        granularity=granularity,
        basis=basis,
        tz=tz,
    )
    _validate_scope(db, filters)
    return filters


def _validate_scope(db: Session, filters: DashboardFilters) -> None:
    def owned(model, ids) -> set[int]:
        if not ids:
            return set()
        return set(
            db.scalars(
                select(model.id).where(model.id.in_(ids), model.workspace_id == filters.workspace_id)
            )
        )

    if set(filters.project_ids) - owned(CollectionProject, filters.project_ids):
        raise DashboardFilterError("project_ids must belong to the workspace")
    if set(filters.task_ids) - owned(CollectionTask, filters.task_ids):
        raise DashboardFilterError("task_ids must belong to the workspace")
    for category, ids in (("scene", filters.scene_label_ids), ("purpose", filters.purpose_label_ids)):
        if not ids:
            continue
        found = set(
            db.scalars(
                select(CollectionLabel.id).where(
                    CollectionLabel.id.in_(ids), CollectionLabel.category == category
                )
            )
        )
        if set(ids) - found:
            raise DashboardFilterError(f"{category}_label_ids must be existing {category} labels")


def _utc_bounds(start: date, end_inclusive: date, zone: ZoneInfo) -> tuple[datetime, datetime]:
    def utc(day: date) -> datetime:
        return datetime.combine(day, time(), tzinfo=zone).astimezone(timezone.utc).replace(tzinfo=None)

    return utc(start), utc(end_inclusive + timedelta(days=1))


def _bucket_labels(filters: DashboardFilters) -> list[str]:
    fmt = _BUCKET_FORMATS[filters.granularity]
    labels: list[str] = []
    if filters.granularity == "month":
        year, month = filters.start_date.year, filters.start_date.month
        while (year, month) <= (filters.end_date.year, filters.end_date.month):
            labels.append(date(year, month, 1).strftime(fmt))
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        return labels
    step = timedelta(hours=1) if filters.granularity == "hour" else timedelta(days=1)
    cursor = datetime.combine(filters.start_date, time())
    stop = datetime.combine(filters.end_date + timedelta(days=1), time())
    while cursor < stop:
        labels.append(cursor.strftime(fmt))
        cursor += step
    return labels


def _number(value: Any) -> float | int:
    if value is None:
        return 0
    if isinstance(value, Decimal):
        return float(value)
    return value


class _Board:
    def __init__(self, db: Session, filters: DashboardFilters, now: datetime | None) -> None:
        self.db = db
        self.filters = filters
        zone = ZoneInfo(filters.tz)
        self.range = _utc_bounds(filters.start_date, filters.end_date, zone)
        today = (now or datetime.now(timezone.utc)).astimezone(zone).date()
        self.today = _utc_bounds(today, today, zone)
        self.labels = _bucket_labels(filters)
        tasks = select(CollectionTask.id).where(CollectionTask.workspace_id == filters.workspace_id)
        if filters.project_ids:
            tasks = tasks.where(CollectionTask.collection_project_id.in_(filters.project_ids))
        if filters.task_ids:
            tasks = tasks.where(CollectionTask.id.in_(filters.task_ids))
        for ids in (filters.scene_label_ids, filters.purpose_label_ids):
            if ids:
                tasks = tasks.where(
                    exists().where(
                        CollectionTaskLabel.collection_task_id == CollectionTask.id,
                        CollectionTaskLabel.collection_label_id.in_(ids),
                    )
                )
        self.task_ids = tasks
        self.package_scope = (
            DataPackage.workspace_id == filters.workspace_id,
            DataPackage.collection_task_id.in_(tasks),
        )
        project_scope = [CollectionProject.workspace_id == filters.workspace_id]
        if filters.project_ids:
            project_scope.append(CollectionProject.id.in_(filters.project_ids))
        if filters.has_task_filters:
            project_scope.append(
                CollectionProject.id.in_(
                    select(CollectionTask.collection_project_id).where(CollectionTask.id.in_(tasks))
                )
            )
        self.project_scope = tuple(project_scope)
        self.task_scope = (CollectionTask.id.in_(tasks),)

    def scalar(self, expr, *conditions):
        return self.db.execute(select(expr).where(*conditions)).scalar()

    def between(self, column, bounds):
        return (column >= bounds[0], column < bounds[1])

    def trend(self, column, measure=None, *conditions) -> list[dict[str, Any]]:
        """Count rows (measure=None) or sum ``measure`` per local-time bucket of ``column``.

        Bucketing happens in a subquery so GROUP BY references a plain column
        rather than an expression carrying bound parameters.
        """
        bucket = func.date_trunc(
            self.filters.granularity, func.timezone(self.filters.tz, func.timezone("UTC", column))
        ).label("bucket")
        value = (measure if measure is not None else literal(1)).label("value")
        inner = (
            select(bucket, value).where(*conditions, *self.between(column, self.range)).subquery()
        )
        aggregate = func.count() if measure is None else func.sum(inner.c.value)
        rows = self.db.execute(
            select(inner.c.bucket, aggregate).group_by(inner.c.bucket)
        ).all()
        fmt = _BUCKET_FORMATS[self.filters.granularity]
        values = {row[0].strftime(fmt): row[1] for row in rows}
        return [{"bucket": label, "value": _number(values.get(label, 0))} for label in self.labels]

    def counted(self, model_created_at, scope) -> dict[str, Any]:
        count = func.count()
        return {
            "total": int(self.scalar(count, *scope, *self.between(model_created_at, self.range)) or 0),
            "today": int(self.scalar(count, *scope, *self.between(model_created_at, self.today)) or 0),
            "trend": self.trend(model_created_at, None, *scope),
        }

    def measured(self, column, *extra) -> tuple[Any, Any, list[dict[str, Any]]]:
        captured = DataPackage.captured_started_at
        conditions = (*self.package_scope, column.is_not(None), *extra)
        total = self.scalar(func.sum(column), *conditions, *self.between(captured, self.range))
        today = self.scalar(func.sum(column), *conditions, *self.between(captured, self.today))
        return _number(total), _number(today), self.trend(captured, column, *conditions)


def data_board(db: Session, filters: DashboardFilters, *, now: datetime | None = None) -> dict[str, Any]:
    board = _Board(db, filters, now)
    valid = filters.basis == "valid"
    duration_column = DataPackage.intake_valid_duration_s if valid else DataPackage.captured_duration_s
    size_column = DataPackage.intake_valid_size_bytes if valid else DataPackage.captured_size_bytes
    created_in_range = board.between(DataPackage.created_at, board.range)
    status = DataPackage.status
    status_row = db.execute(
        select(
            func.count(),
            func.sum(case((status == "pending_assignment", 1), else_=0)),
            func.sum(case((status == "assigned", 1), else_=0)),
            func.sum(case((status == "voided", 1), else_=0)),
        ).where(*board.package_scope, *created_in_range)
    ).one()
    total, pending, assigned, voided = (int(value or 0) for value in status_row)
    frozen = (*board.package_scope, DataPackage.intake_valid_duration_s.is_not(None))
    captured = DataPackage.captured_started_at
    duration_total, duration_today, duration_trend = board.measured(duration_column)
    size_total, size_today, size_trend = board.measured(size_column)
    pending_review = board.scalar(
        func.sum(DataPackage.captured_duration_s),
        *board.package_scope,
        DataPackage.captured_duration_s.is_not(None),
        DataPackage.intake_valid_duration_s.is_(None),
        *board.between(captured, board.range),
    )
    incomplete = board.scalar(
        func.count(),
        *board.package_scope,
        DataPackage.upload_completed_at.is_not(None),
        DataPackage.captured_duration_s.is_(None),
    )
    return {
        "workspace_id": filters.workspace_id,
        "filters": {
            "start_date": filters.start_date.isoformat(),
            "end_date": filters.end_date.isoformat(),
            "granularity": filters.granularity,
            "basis": filters.basis,
            "tz": filters.tz,
            "project_ids": list(filters.project_ids),
            "task_ids": list(filters.task_ids),
            "scene_label_ids": list(filters.scene_label_ids),
            "purpose_label_ids": list(filters.purpose_label_ids),
        },
        "projects": board.counted(CollectionProject.created_at, board.project_scope),
        "tasks": board.counted(CollectionTask.created_at, board.task_scope),
        "packages": {
            "total": total,
            "today": int(board.scalar(func.count(), *frozen, *board.between(captured, board.today)) or 0),
            "trend": board.trend(captured, None, *frozen),
            "status_counts": {
                "pending_assignment": pending,
                "assigned": assigned,
                "other": total - pending - assigned - voided,
                "voided": voided,
            },
        },
        "duration": {
            "basis": filters.basis,
            "total_s": float(duration_total),
            "today_s": float(duration_today),
            "pending_review_duration_s": float(_number(pending_review)),
            "trend": [{"bucket": p["bucket"], "value": float(p["value"])} for p in duration_trend],
        },
        "size": {
            "basis": filters.basis,
            "total_bytes": int(size_total),
            "today_bytes": int(size_today),
            "trend": [{"bucket": p["bucket"], "value": int(p["value"])} for p in size_trend],
        },
        "incomplete_packages": int(incomplete or 0),
        "computed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def data_board_csv(payload: dict[str, Any]) -> str:
    basis = "有效" if payload["duration"]["basis"] == "valid" else "全部"
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["时间", "项目数", "任务数", "数据包数", f"时长（秒，{basis}）", f"大小（字节，{basis}）"])
    series = [
        payload["projects"]["trend"],
        payload["tasks"]["trend"],
        payload["packages"]["trend"],
        payload["duration"]["trend"],
        payload["size"]["trend"],
    ]
    for index, point in enumerate(series[0]):
        writer.writerow([point["bucket"], *(values[index]["value"] for values in series)])
    return "﻿" + buffer.getvalue()


def csv_filename(filters: DashboardFilters) -> str:
    return f"collection-data-board_{filters.start_date.isoformat()}_{filters.end_date.isoformat()}.csv"
```

- [ ] **Step 4: 运行测试确认通过**

Run: `$PYTEST backend/tests/test_collection_dashboard_service.py`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/data/services/collection_dashboard.py backend/tests/test_collection_dashboard_service.py
git commit -m "feat(dashboard): 数采看板按数据包明细实时聚合"
```

---

### Task 5: 数采看板路由、CSV 与性能测试

**Files:**
- Create: `backend/data/routers/collection_dashboard.py`
- Modify: `backend/data/main.py`（导入列表约第 30 行、`include_router` 约第 216 行）
- Modify: `backend/pytest.ini`
- Test: `backend/tests/test_collection_dashboard_api.py`、`backend/tests/test_collection_dashboard_perf.py`

**Interfaces:**
- Consumes: `resolve_filters`、`data_board`、`data_board_csv`、`csv_filename`、`DashboardFilterError`（Task 4）
- Produces: `GET /api/v1/collection-dashboard/data`

- [ ] **Step 1: 写失败测试**

`backend/tests/test_collection_dashboard_api.py`：

```python
"""Collection dashboard HTTP contract."""

from tests.collection_api_fixtures import make_label, make_project, make_workspace


def _get(client, headers, **params):
    return client.get("/api/v1/collection-dashboard/data", headers=headers, params=params)


def test_returns_data_board_payload(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    make_project(db_session, workspace)

    response = _get(client, admin_headers, workspace_id=workspace.id)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["workspace_id"] == workspace.id
    assert data["filters"]["granularity"] == "day"
    assert data["filters"]["basis"] == "valid"
    assert len(data["duration"]["trend"]) == 30
    assert set(data) >= {"projects", "tasks", "packages", "duration", "size", "incomplete_packages"}


def test_rejects_foreign_project_and_wrong_label_category(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    other = make_workspace(db_session)
    foreign = make_project(db_session, other)
    purpose = make_label(db_session, category="purpose")

    assert _get(client, admin_headers, workspace_id=workspace.id, project_ids=[foreign.id]).status_code == 422
    assert _get(client, admin_headers, workspace_id=workspace.id, scene_label_ids=[purpose.id]).status_code == 422
    assert _get(client, admin_headers, workspace_id=workspace.id, format="xml").status_code == 422
    assert _get(
        client, admin_headers, workspace_id=workspace.id,
        start_date="2026-09-01", end_date="2026-09-29", granularity="hour",
    ).status_code == 422


def test_csv_download(client, db_session, admin_headers):
    workspace = make_workspace(db_session)

    response = _get(
        client, admin_headers, workspace_id=workspace.id,
        start_date="2026-09-28", end_date="2026-09-29", format="csv",
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert 'filename="collection-data-board_2026-09-28_2026-09-29.csv"' in response.headers["content-disposition"]
    assert response.content.decode("utf-8").startswith("﻿时间,")


def test_unknown_workspace_is_404(client, admin_headers):
    assert _get(client, admin_headers, workspace_id=999_999_999).status_code == 404
```

`backend/tests/test_collection_dashboard_perf.py`：

```python
"""Data board stays fast at the expected scale (~20k packages). Run with RUN_SLOW=1."""

import os
import time
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import insert, text
from tests.collection_api_fixtures import make_project, make_task, make_workspace

from data.models.data_package import DataPackage
from data.services.collection_dashboard import data_board, resolve_filters

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(os.environ.get("RUN_SLOW") != "1", reason="set RUN_SLOW=1 to run"),
]


def test_data_board_over_20k_packages(db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    tasks = [make_task(db_session, project) for _ in range(20)]
    base = datetime(2026, 1, 1)
    rows = [
        {
            "package_uid": f"pkg_{uuid4().hex}",
            "workspace_id": workspace.id,
            "collection_project_id": project.id,
            "collection_task_id": tasks[index % 20].id,
            "status": "pending_assignment",
            "target_duration_hours": Decimal("0.50"),
            "created_at": base + timedelta(minutes=index * 20),
            "captured_started_at": base + timedelta(minutes=index * 20),
            "captured_duration_s": Decimal("1800.000"),
            "captured_size_bytes": 1_000_000,
            "intake_valid_duration_s": Decimal("1700.000"),
            "intake_valid_size_bytes": 900_000,
            "qrdf_facts_json": {},
            "parse_error_code": "",
            "parse_error_message": "",
            "supplement_reason": "",
            "updated_at": base,
        }
        for index in range(20_000)
    ]
    db_session.execute(insert(DataPackage), rows)
    db_session.commit()
    db_session.execute(text("ANALYZE data_packages"))
    filters = resolve_filters(
        db_session, workspace_id=workspace.id,
        start_date=datetime(2026, 1, 1).date(), end_date=datetime(2026, 12, 31).date(),
    )

    started = time.perf_counter()
    payload = data_board(db_session, filters)
    elapsed = time.perf_counter() - started

    assert payload["packages"]["total"] == 20_000
    assert elapsed < 2.0, f"data board took {elapsed:.2f}s"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `$PYTEST backend/tests/test_collection_dashboard_api.py`
Expected: FAIL，404（路由不存在）

- [ ] **Step 3: 实现路由并注册**

`backend/data/routers/collection_dashboard.py`：

```python
"""Workspace-scoped collection dashboards (one endpoint per board)."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from data.database import get_db
from data.services.collection_access import require_collection_workspace
from data.services.collection_dashboard import (
    DashboardFilterError,
    csv_filename,
    data_board,
    data_board_csv,
    resolve_filters,
)
from data.utils.helpers import get_current_user, require_permission, success

router = APIRouter(prefix="/collection-dashboard", tags=["采集概览看板"])


def _actor_id(user: dict) -> int | None:
    try:
        return int(user.get("sub"))
    except (TypeError, ValueError):
        return None


@router.get("/data")
def collection_data_board(
    workspace_id: int = Query(..., gt=0),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    project_ids: list[int] = Query(default=[]),
    task_ids: list[int] = Query(default=[]),
    scene_label_ids: list[int] = Query(default=[]),
    purpose_label_ids: list[int] = Query(default=[]),
    granularity: str = Query(default="day"),
    basis: str = Query(default="valid"),
    tz: str = Query(default="Asia/Shanghai"),
    format: str = Query(default="json"),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "episode:read")
    try:
        require_collection_workspace(db, actor_id=_actor_id(user), workspace_id=workspace_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if format not in {"json", "csv"}:
        raise HTTPException(status_code=422, detail="format must be json or csv")
    try:
        filters = resolve_filters(
            db,
            workspace_id=workspace_id,
            start_date=start_date,
            end_date=end_date,
            project_ids=project_ids,
            task_ids=task_ids,
            scene_label_ids=scene_label_ids,
            purpose_label_ids=purpose_label_ids,
            granularity=granularity,
            basis=basis,
            tz=tz,
        )
    except DashboardFilterError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    payload = data_board(db, filters)
    if format == "csv":
        return Response(
            content=data_board_csv(payload).encode("utf-8"),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{csv_filename(filters)}"'},
        )
    return success(payload)
```

`backend/data/main.py`：在 `from data.routers import (` 列表中 `collection_overview,` 后加 `collection_dashboard,`；在 `app.include_router(collection_overview.router, prefix=prefix)` 后加：

```python
app.include_router(collection_dashboard.router, prefix=prefix)
```

`backend/pytest.ini` 末尾追加：

```ini
markers =
    slow: long-running checks, enable with RUN_SLOW=1
```

- [ ] **Step 4: 运行测试确认通过**

Run: `$PYTEST backend/tests/test_collection_dashboard_api.py backend/tests/test_collection_dashboard_service.py`
Expected: PASS

Run: `RUN_SLOW=1 $PYTEST backend/tests/test_collection_dashboard_perf.py`
Expected: PASS（若超过 2 秒，先用 `EXPLAIN ANALYZE` 查看是否命中 `ix_data_packages_workspace_captured`，再调整查询，不放宽阈值）

- [ ] **Step 5: 提交**

```bash
git add backend/data/routers/collection_dashboard.py backend/data/main.py backend/pytest.ini backend/tests/test_collection_dashboard_api.py backend/tests/test_collection_dashboard_perf.py
git commit -m "feat(dashboard): 数采看板接口与 CSV 导出"
```

---

### Task 6: 前端图表公共工具

**Files:**
- Create: `frontend/js/dashboard-charts.js`
- Test: `frontend/tests/dashboard-charts.test.mjs`

**Interfaces:**
- Produces: `QuicStudioDashboardCharts.{ formatDuration(seconds) -> string, formatBytes(bytes) -> string, ensureEcharts(loader?) -> Promise<echarts>, lineOption(points, { color, formatter }) -> object, createRegistry(echartsLib) -> { render(key, el, option), resize(), dispose(), size() } }`

- [ ] **Step 1: 写失败测试**

`frontend/tests/dashboard-charts.test.mjs`：

```js
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../js/dashboard-charts.js', import.meta.url), 'utf8');
function load(extra = {}) {
  const context = vm.createContext({ ...extra });
  vm.runInContext(`${source}\nglobalThis.charts = QuicStudioDashboardCharts;`, context);
  return context.charts;
}

test('duration and size formatting keep unknown values visible as a dash', () => {
  const charts = load();
  assert.equal(charts.formatDuration(3661.4), '1:01:01');
  assert.equal(charts.formatDuration(0), '0:00:00');
  assert.equal(charts.formatDuration(null), '—');
  assert.equal(charts.formatDuration(-1), '—');
  assert.equal(charts.formatBytes(0), '0 B');
  assert.equal(charts.formatBytes(1536), '1.50 KB');
  assert.equal(charts.formatBytes(84.68 * 1024 ** 3), '84.68 GB');
  assert.equal(charts.formatBytes(undefined), '—');
});

test('line option maps buckets and values', () => {
  const option = load().lineOption([{ bucket: '2026-09-28', value: 3 }, { bucket: '2026-09-29', value: '4' }]);
  assert.deepEqual(JSON.parse(JSON.stringify(option.xAxis.data)), ['2026-09-28', '2026-09-29']);
  assert.deepEqual(JSON.parse(JSON.stringify(option.series[0].data)), [3, 4]);
});

test('registry reuses a chart per element and disposes all', () => {
  const created = [];
  const lib = { init(el) { const chart = { el, options: [], disposed: false, getDom: () => el, setOption(o) { this.options.push(o); }, resize() {}, dispose() { this.disposed = true; } }; created.push(chart); return chart; } };
  const registry = load().createRegistry(lib);
  const el = {};
  registry.render('a', el, { x: 1 });
  registry.render('a', el, { x: 2 });
  assert.equal(created.length, 1);
  registry.render('a', {}, { x: 3 });
  assert.equal(created.length, 2);
  assert.equal(created[0].disposed, true);
  registry.dispose();
  assert.equal(registry.size(), 0);
  assert.equal(registry.render('b', null, {}), null);
});

test('ensureEcharts loads the charts asset once when missing', async () => {
  let loads = 0;
  const charts = load();
  const loader = { async load(name) { loads += 1; assert.equal(name, 'charts'); } };
  await assert.rejects(() => charts.ensureEcharts(loader), /图表/);
  assert.equal(loads, 1);
});
```

- [ ] **Step 2: 运行测试确认失败**

Run: `node --test frontend/tests/dashboard-charts.test.mjs`
Expected: FAIL，`ENOENT … dashboard-charts.js`

- [ ] **Step 3: 实现**

`frontend/js/dashboard-charts.js`：

```js
/* Shared helpers for collection dashboards: formatting, lazy ECharts, chart lifecycle. */
const QuicStudioDashboardCharts = (() => {
  function known(value) {
    return value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value)) && Number(value) >= 0;
  }

  function formatDuration(value) {
    if (!known(value)) return '—';
    const total = Math.round(Number(value));
    return `${Math.floor(total / 3600)}:${String(Math.floor(total / 60) % 60).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}`;
  }

  const UNITS = ['B', 'KB', 'MB', 'GB', 'TB'];
  function formatBytes(value) {
    if (!known(value)) return '—';
    let size = Number(value);
    let unit = 0;
    while (size >= 1024 && unit < UNITS.length - 1) { size /= 1024; unit += 1; }
    return `${unit === 0 ? size : size.toFixed(2)} ${UNITS[unit]}`;
  }

  async function ensureEcharts(loader = globalThis.QuicStudioPageAssets) {
    if (typeof globalThis.echarts !== 'undefined') return globalThis.echarts;
    if (!loader) throw new Error('图表组件不可用 / Charts are unavailable');
    await loader.load('charts');
    if (typeof globalThis.echarts === 'undefined') throw new Error('图表组件加载失败 / Charts failed to load');
    return globalThis.echarts;
  }

  function lineOption(points, { color = '#3568d4', formatter = (value) => String(value) } = {}) {
    const list = Array.isArray(points) ? points : [];
    return {
      color: [color],
      tooltip: { trigger: 'axis', valueFormatter: formatter },
      grid: { left: 64, right: 16, top: 24, bottom: 28 },
      xAxis: { type: 'category', boundaryGap: false, data: list.map((point) => point.bucket) },
      yAxis: { type: 'value', minInterval: 1, axisLabel: { formatter } },
      series: [{
        type: 'line',
        smooth: true,
        showSymbol: list.length <= 31,
        areaStyle: { opacity: 0.12 },
        data: list.map((point) => Number(point.value) || 0),
      }],
    };
  }

  function createRegistry(echartsLib) {
    const charts = new Map();
    return {
      render(key, el, option) {
        if (!el || !echartsLib) return null;
        let chart = charts.get(key);
        if (chart && chart.getDom() !== el) { chart.dispose(); chart = null; }
        if (!chart) { chart = echartsLib.init(el); charts.set(key, chart); }
        chart.setOption(option, true);
        return chart;
      },
      resize() { charts.forEach((chart) => chart.resize()); },
      dispose() { charts.forEach((chart) => chart.dispose()); charts.clear(); },
      size() { return charts.size; },
    };
  }

  return { formatDuration, formatBytes, ensureEcharts, lineOption, createRegistry };
})();
```

- [ ] **Step 4: 运行测试确认通过**

Run: `node --test frontend/tests/dashboard-charts.test.mjs`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add frontend/js/dashboard-charts.js frontend/tests/dashboard-charts.test.mjs
git commit -m "feat(dashboard): 看板图表公共工具"
```

---

### Task 7: API 客户端、演示数据与全局筛选栏

**Files:**
- Modify: `frontend/js/api.js`（`getCollectionOverview` 之后，约第 705–709 行；`request` 之后加下载函数）
- Modify: `frontend/js/demo-data.js`（`read()` 内 `/collection-overview` 分支之后）
- Create: `frontend/js/collection-dashboard-filters.js`
- Test: `frontend/tests/collection-dashboard-filters.test.mjs`、`frontend/tests/demo-mode-flows.test.mjs`（改）

**Interfaces:**
- Produces:
  - `QuicDataAPI.getCollectionDashboardData(params) -> Promise<payload>`
  - `QuicDataAPI.downloadCollectionDashboardCsv(params) -> Promise<Blob>`
  - `QuicStudioCollectionDashboardFilters.{ defaultFilters(today?) , toParams(filters), visibleTasks(tasks, projectIds), pruneTasks(taskIds, tasks, projectIds), daySpan(range), component }`
  - 筛选对象：`{ dateRange: [startIso, endIso], projectIds: number[], taskIds: number[], sceneLabelIds: number[], purposeLabelIds: number[] }`
  - `toParams` 输出：`{ start_date, end_date, project_ids, task_ids, scene_label_ids, purpose_label_ids }`

- [ ] **Step 1: 写失败测试**

`frontend/tests/collection-dashboard-filters.test.mjs`：

```js
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const context = vm.createContext({});
vm.runInContext(`${readFileSync(new URL('../js/collection-dashboard-filters.js', import.meta.url), 'utf8')}\nglobalThis.filters = QuicStudioCollectionDashboardFilters;`, context);
const f = context.filters;
const plain = (value) => JSON.parse(JSON.stringify(value));

test('default filters cover the last 30 days and nothing else', () => {
  const value = plain(f.defaultFilters(new Date(2026, 8, 29)));
  assert.deepEqual(value, { dateRange: ['2026-08-31', '2026-09-29'], projectIds: [], taskIds: [], sceneLabelIds: [], purposeLabelIds: [] });
  assert.equal(f.daySpan(value.dateRange), 30);
});

test('params use API names and numeric ids', () => {
  const params = plain(f.toParams({ dateRange: ['2026-09-01', '2026-09-02'], projectIds: ['3'], taskIds: [5], sceneLabelIds: [], purposeLabelIds: [9] }));
  assert.deepEqual(params, { start_date: '2026-09-01', end_date: '2026-09-02', project_ids: [3], task_ids: [5], scene_label_ids: [], purpose_label_ids: [9] });
});

test('tasks narrow to selected projects and stale task picks are pruned', () => {
  const tasks = [{ id: 1, collection_project_id: 10 }, { id: 2, project_id: 20 }, { id: 3, collection_project_id: 20 }];
  assert.deepEqual(plain(f.visibleTasks(tasks, [20]).map((t) => t.id)), [2, 3]);
  assert.deepEqual(plain(f.visibleTasks(tasks, []).map((t) => t.id)), [1, 2, 3]);
  assert.deepEqual(plain(f.pruneTasks([1, 3], tasks, [20])), [3]);
});
```

在 `frontend/tests/demo-mode-flows.test.mjs` 的 `lists` 对象中 `collectionOverview` 一行后加：

```js
    collectionDashboard: () => api.getCollectionDashboardData({ workspace_id: 1 }),
```

- [ ] **Step 2: 运行测试确认失败**

Run: `node --test frontend/tests/collection-dashboard-filters.test.mjs frontend/tests/demo-mode-flows.test.mjs`
Expected: FAIL（文件不存在；`api.getCollectionDashboardData is not a function`）

- [ ] **Step 3: 实现 API、演示数据与筛选组件**

`frontend/js/api.js`：在 `request` 函数之后加：

```js
  async function download(path, params, retried = false) {
    if (demoMode) {
      const text = await QuicDataDemo.read(path, params);
      return new Blob([String(text)], { type: 'text/csv;charset=utf-8' });
    }
    const url = new URL(`${base}${path}`, window.location.origin);
    setQuery(url, params);
    const headers = {};
    if (accessToken) headers.Authorization = `Bearer ${accessToken}`;
    const response = await fetch(url, { method: 'GET', headers, credentials: 'same-origin' });
    if (response.status === 401 && !retried && await refresh()) return download(path, params, true);
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      if (response.status === 401) clearAuth();
      throw responseError(response, payload, `导出失败 (${response.status})`);
    }
    return response.blob();
  }
```

在 `getCollectionOverview(...) { ... },` 之后加：

```js
    getCollectionDashboardData(params) {
      return request('GET', '/collection-dashboard/data', { params });
    },
    downloadCollectionDashboardCsv(params) {
      return download('/collection-dashboard/data', { ...params, format: 'csv' });
    },
```

`frontend/js/demo-data.js`：在 `/collection-overview` 分支之后加：

```js
    if (path === '/collection-dashboard/data') {
      const end = params?.end_date || '2026-09-29';
      const start = params?.start_date || '2026-08-31';
      const buckets = [];
      for (let day = new Date(`${start}T00:00:00Z`); day <= new Date(`${end}T00:00:00Z`); day = new Date(day.getTime() + 86400000)) buckets.push(day.toISOString().slice(0, 10));
      const series = (scale) => buckets.map((bucket, index) => ({ bucket, value: Math.round(((index * 7) % 5) * scale) }));
      const payload = {
        workspace_id: Number(params?.workspace_id || 1),
        filters: { start_date: start, end_date: end, granularity: params?.granularity || 'day', basis: params?.basis || 'valid', tz: 'Asia/Shanghai', project_ids: [], task_ids: [], scene_label_ids: [], purpose_label_ids: [] },
        projects: { total: collectionProjects.length, today: 0, trend: series(0.2) },
        tasks: { total: collectionTasks.length, today: 1, trend: series(0.4) },
        packages: { total: dataPackages.length, today: 2, trend: series(1), status_counts: { pending_assignment: 1, assigned: 1, other: dataPackages.length - 2, voided: 0 } },
        duration: { basis: params?.basis || 'valid', total_s: 15588, today_s: 3600, pending_review_duration_s: 5400, trend: series(1800) },
        size: { basis: params?.basis || 'valid', total_bytes: 90924000000, today_bytes: 2147483648, trend: series(1073741824) },
        incomplete_packages: 0,
        computed_at: '2026-09-29T04:00:00Z',
      };
      if (params?.format === 'csv') return `﻿时间,项目数,任务数,数据包数,时长（秒）,大小（字节）\n${buckets.map((bucket, i) => [bucket, payload.projects.trend[i].value, payload.tasks.trend[i].value, payload.packages.trend[i].value, payload.duration.trend[i].value, payload.size.trend[i].value].join(',')).join('\n')}\n`;
      return payload;
    }
```

`frontend/js/collection-dashboard-filters.js`：

```js
/* Global filter bar shared by the three collection dashboards. */
const QuicStudioCollectionDashboardFilters = (() => {
  const DAY_MS = 86400000;

  function isoDate(value) {
    return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, '0')}-${String(value.getDate()).padStart(2, '0')}`;
  }

  function defaultFilters(today = new Date()) {
    const end = new Date(today.getFullYear(), today.getMonth(), today.getDate());
    const start = new Date(end.getTime() - 29 * DAY_MS);
    return { dateRange: [isoDate(start), isoDate(end)], projectIds: [], taskIds: [], sceneLabelIds: [], purposeLabelIds: [] };
  }

  const ids = (values) => (values || []).map(Number).filter((value) => Number.isInteger(value) && value > 0);

  function toParams(filters) {
    return {
      start_date: filters?.dateRange?.[0] || undefined,
      end_date: filters?.dateRange?.[1] || undefined,
      project_ids: ids(filters?.projectIds),
      task_ids: ids(filters?.taskIds),
      scene_label_ids: ids(filters?.sceneLabelIds),
      purpose_label_ids: ids(filters?.purposeLabelIds),
    };
  }

  const taskProjectId = (task) => Number(task?.collection_project_id ?? task?.project_id);

  function visibleTasks(tasks, projectIds) {
    const list = Array.isArray(tasks) ? tasks : [];
    const selected = new Set(ids(projectIds));
    return selected.size ? list.filter((task) => selected.has(taskProjectId(task))) : list;
  }

  function pruneTasks(taskIds, tasks, projectIds) {
    const allowed = new Set(visibleTasks(tasks, projectIds).map((task) => Number(task.id)));
    return ids(taskIds).filter((id) => allowed.has(id));
  }

  function daySpan(range) {
    if (!range?.[0] || !range?.[1]) return 0;
    return Math.round((Date.parse(`${range[1]}T00:00:00Z`) - Date.parse(`${range[0]}T00:00:00Z`)) / DAY_MS) + 1;
  }

  const component = {
    props: {
      modelValue: { type: Object, required: true },
      projects: { type: Array, default: () => [] },
      tasks: { type: Array, default: () => [] },
      labels: { type: Array, default: () => [] },
      locale: { type: String, default: 'zh-CN' },
    },
    emits: ['update:modelValue'],
    setup(props, { emit }) {
      const text = (zh, en) => (props.locale === 'en-US' ? en : zh);
      const update = (patch) => {
        const next = { ...props.modelValue, ...patch };
        next.taskIds = pruneTasks(next.taskIds, props.tasks, next.projectIds);
        emit('update:modelValue', next);
      };
      const taskOptions = Vue.computed(() => visibleTasks(props.tasks, props.modelValue.projectIds));
      const labelOptions = (category) => props.labels.filter((label) => label.category === category && label.is_active !== false);
      const disabledDate = (value) => value.getTime() > Date.now();
      return { text, update, taskOptions, labelOptions, disabledDate };
    },
    template: `
      <div class="page-actions collection-dashboard-filters">
        <el-date-picker :model-value="modelValue.dateRange" type="daterange" value-format="YYYY-MM-DD" :clearable="false" :disabled-date="disabledDate"
          :start-placeholder="text('开始日期', 'Start date')" :end-placeholder="text('结束日期', 'End date')" @update:model-value="update({ dateRange: $event })" />
        <el-select :model-value="modelValue.projectIds" multiple collapse-tags clearable filterable :placeholder="text('全部采集项目', 'All projects')" @update:model-value="update({ projectIds: $event })">
          <el-option v-for="project in projects" :key="project.id" :label="project.name" :value="Number(project.id)" />
        </el-select>
        <el-select :model-value="modelValue.taskIds" multiple collapse-tags clearable filterable :placeholder="text('全部任务', 'All tasks')" @update:model-value="update({ taskIds: $event })">
          <el-option v-for="task in taskOptions" :key="task.id" :label="task.name" :value="Number(task.id)" />
        </el-select>
        <el-select :model-value="modelValue.sceneLabelIds" multiple collapse-tags clearable filterable :placeholder="text('全部场景', 'All scenes')" @update:model-value="update({ sceneLabelIds: $event })">
          <el-option v-for="label in labelOptions('scene')" :key="label.id" :label="label.name" :value="Number(label.id)" />
        </el-select>
        <el-select :model-value="modelValue.purposeLabelIds" multiple collapse-tags clearable filterable :placeholder="text('全部任务用途', 'All purposes')" @update:model-value="update({ purposeLabelIds: $event })">
          <el-option v-for="label in labelOptions('purpose')" :key="label.id" :label="label.name" :value="Number(label.id)" />
        </el-select>
      </div>`,
  };

  return { defaultFilters, toParams, visibleTasks, pruneTasks, daySpan, component };
})();
```

- [ ] **Step 4: 运行测试确认通过**

Run: `node --test frontend/tests/collection-dashboard-filters.test.mjs frontend/tests/demo-mode-flows.test.mjs`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add frontend/js/api.js frontend/js/demo-data.js frontend/js/collection-dashboard-filters.js frontend/tests/collection-dashboard-filters.test.mjs frontend/tests/demo-mode-flows.test.mjs
git commit -m "feat(dashboard): 看板 API 客户端、演示数据与全局筛选栏"
```

---

### Task 8: 数采看板组件

**Files:**
- Create: `frontend/js/collection-data-board.js`
- Test: `frontend/tests/collection-data-board.test.mjs`

**Interfaces:**
- Consumes: `QuicStudioDashboardCharts`（Task 6，运行时按全局名访问）、`QuicStudioCollectionDashboardFilters.daySpan`（Task 7）、`QuicDataAPI.getCollectionDashboardData` / `downloadCollectionDashboardCsv`（Task 7）
- Produces: `QuicStudioCollectionDataBoard.{ requestParams(workspaceId, filterParams, { granularity, basis }), cards(payload), pendingHint(payload, basis), hourAllowed(filterParams), csvName(filterParams), createLoader(api, state), component }`
  - 组件 props：`workspaceId`、`filters`（`toParams` 的输出）、`locale`

- [ ] **Step 1: 写失败测试**

`frontend/tests/collection-data-board.test.mjs`：

```js
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const read = (name) => readFileSync(new URL(`../js/${name}`, import.meta.url), 'utf8');
const context = vm.createContext({});
vm.runInContext(`${read('dashboard-charts.js')}\n${read('collection-dashboard-filters.js')}\n${read('collection-data-board.js')}\nglobalThis.board = QuicStudioCollectionDataBoard;`, context);
const board = context.board;
const plain = (value) => JSON.parse(JSON.stringify(value));
const payload = (workspaceId = 1) => ({
  workspace_id: workspaceId,
  projects: { total: 2, today: 1, trend: [] },
  tasks: { total: 3, today: 0, trend: [] },
  packages: { total: 10, today: 2, trend: [], status_counts: { pending_assignment: 1, assigned: 2, other: 6, voided: 1 } },
  duration: { basis: 'valid', total_s: 3600, today_s: 60, pending_review_duration_s: 120, trend: [] },
  size: { basis: 'valid', total_bytes: 1024, today_bytes: 0, trend: [] },
  incomplete_packages: 0,
});

test('request params combine workspace, filters, granularity, basis and time zone', () => {
  assert.deepEqual(plain(board.requestParams(4, { start_date: '2026-09-01', project_ids: [1] }, { granularity: 'day', basis: 'all' })), {
    workspace_id: 4, start_date: '2026-09-01', project_ids: [1], granularity: 'day', basis: 'all', tz: 'Asia/Shanghai',
  });
});

test('cards show totals and today values; missing values stay unknown', () => {
  const cards = plain(board.cards(payload()));
  assert.deepEqual(cards.map((card) => card.key), ['projects', 'tasks', 'packages', 'duration', 'size']);
  assert.deepEqual(cards[3], { key: 'duration', kind: 'duration', total: 3600, today: 60 });
  assert.equal(plain(board.cards({}))[0].total, null);
});

test('pending review hint only appears on the valid basis', () => {
  assert.equal(board.pendingHint(payload(), 'valid'), 120);
  assert.equal(board.pendingHint(payload(), 'all'), null);
  assert.equal(board.pendingHint({ duration: { pending_review_duration_s: 0 } }, 'valid'), null);
});

test('hour granularity is only allowed for ranges up to 7 days', () => {
  assert.equal(board.hourAllowed({ start_date: '2026-09-23', end_date: '2026-09-29' }), true);
  assert.equal(board.hourAllowed({ start_date: '2026-09-22', end_date: '2026-09-29' }), false);
  assert.equal(board.csvName({ start_date: '2026-09-01', end_date: '2026-09-29' }), 'collection-data-board_2026-09-01_2026-09-29.csv');
});

test('loader shares identical requests and drops late responses for another scope', async () => {
  const requests = [];
  const state = {};
  const loader = board.createLoader({ getCollectionDashboardData: (params) => new Promise((resolve) => requests.push({ params, resolve })) }, state);
  const first = loader.load({ workspace_id: 1, basis: 'valid' });
  assert.equal(loader.load({ workspace_id: 1, basis: 'valid' }), first);
  const second = loader.load({ workspace_id: 2, basis: 'valid' });
  requests[1].resolve(payload(2));
  await second;
  requests[0].resolve(payload(1));
  await first;
  assert.equal(requests.length, 2);
  assert.equal(state.data.workspace_id, 2);
});

test('loader rejects a response for an unexpected workspace and clears data on failure', async () => {
  const state = { data: payload(1) };
  await board.createLoader({ getCollectionDashboardData: async () => payload(9) }, state).load({ workspace_id: 1 });
  assert.equal(state.data, null);
  assert.match(state.error, /工作空间/);
  await board.createLoader({ getCollectionDashboardData: async () => { throw new Error('unavailable'); } }, state).load({ workspace_id: 1 });
  assert.equal(state.error, 'unavailable');
  assert.equal(state.loading, false);
});
```

与 spec §5.2 的差异：「刷新」「导出 CSV」放在看板内部工具栏（与粒度、口径切换同一行），不放在全局筛选栏，因为导出只属于数采看板；全局筛选栏只含筛选条件。

- [ ] **Step 2: 运行测试确认失败**

Run: `node --test frontend/tests/collection-data-board.test.mjs`
Expected: FAIL，`ENOENT … collection-data-board.js`

- [ ] **Step 3: 实现**

`frontend/js/collection-data-board.js`：

```js
/* Collection data board: API facts only, page-owned loading, five trend charts. */
const QuicStudioCollectionDataBoard = (() => {
  const TZ = 'Asia/Shanghai';
  const CARD_KEYS = [
    ['projects', 'count'], ['tasks', 'count'], ['packages', 'count'], ['duration', 'duration'], ['size', 'size'],
  ];
  const known = (value) => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value)) ? Number(value) : null;

  function requestParams(workspaceId, filterParams, { granularity, basis }) {
    return { workspace_id: Number(workspaceId), ...filterParams, granularity, basis, tz: TZ };
  }

  function cards(payload) {
    return CARD_KEYS.map(([key, kind]) => {
      const block = payload?.[key] || {};
      if (key === 'duration') return { key, kind, total: known(block.total_s), today: known(block.today_s) };
      if (key === 'size') return { key, kind, total: known(block.total_bytes), today: known(block.today_bytes) };
      return { key, kind, total: known(block.total), today: known(block.today) };
    });
  }

  function pendingHint(payload, basis) {
    const value = known(payload?.duration?.pending_review_duration_s);
    return basis === 'valid' && value !== null && value > 0 ? value : null;
  }

  function hourAllowed(filterParams) {
    return QuicStudioCollectionDashboardFilters.daySpan([filterParams?.start_date, filterParams?.end_date]) <= 7;
  }

  function csvName(filterParams) {
    return `collection-data-board_${filterParams?.start_date || ''}_${filterParams?.end_date || ''}.csv`;
  }

  function createLoader(api, state) {
    let generation = 0;
    let pending = null;
    let pendingKey = '';
    function invalidate() {
      generation += 1;
      pending = null;
      pendingKey = '';
      state.data = null;
      state.error = '';
      state.loading = false;
    }
    function load(params) {
      const key = JSON.stringify(params);
      if (pending && pendingKey === key) return pending;
      invalidate();
      if (!params?.workspace_id) return Promise.resolve();
      const current = generation;
      state.loading = true;
      pendingKey = key;
      pending = (async () => {
        try {
          const data = await api.getCollectionDashboardData(params);
          if (current !== generation) return;
          if (Number(data?.workspace_id) !== Number(params.workspace_id)) throw new Error('统计数据的工作空间不匹配');
          state.data = data;
        } catch (error) {
          if (current === generation) state.error = error?.message || '统计加载失败';
        } finally {
          if (current === generation) {
            state.loading = false;
            pending = null;
          }
        }
      })();
      return pending;
    }
    return { load, dispose: invalidate };
  }

  function saveBlob(blob, name) {
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = name;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 0);
  }

  const component = {
    props: {
      workspaceId: { type: [Number, String], default: null },
      filters: { type: Object, required: true },
      locale: { type: String, default: 'zh-CN' },
    },
    setup(props) {
      const Charts = QuicStudioDashboardCharts;
      const state = Vue.reactive({ data: null, loading: false, error: '', chartError: '' });
      const granularity = Vue.ref('day');
      const basis = Vue.ref('valid');
      const exporting = Vue.ref(false);
      const els = { projects: Vue.ref(null), tasks: Vue.ref(null), packages: Vue.ref(null), duration: Vue.ref(null), size: Vue.ref(null) };
      const loader = createLoader(QuicDataAPI, state);
      let registry = null;
      const text = (zh, en) => (props.locale === 'en-US' ? en : zh);
      const canUseHour = Vue.computed(() => hourAllowed(props.filters));
      const params = () => requestParams(props.workspaceId, props.filters, { granularity: granularity.value, basis: basis.value });

      async function renderCharts() {
        if (!state.data) return;
        try {
          registry = registry || Charts.createRegistry(await Charts.ensureEcharts());
          state.chartError = '';
        } catch (error) {
          state.chartError = error?.message || text('图表加载失败', 'Charts failed to load');
          return;
        }
        await Vue.nextTick();
        const count = (value) => String(value);
        registry.render('projects', els.projects.value, Charts.lineOption(state.data.projects?.trend, { formatter: count }));
        registry.render('tasks', els.tasks.value, Charts.lineOption(state.data.tasks?.trend, { color: '#2f9e74', formatter: count }));
        registry.render('packages', els.packages.value, Charts.lineOption(state.data.packages?.trend, { color: '#7b6fd6', formatter: count }));
        registry.render('duration', els.duration.value, Charts.lineOption(state.data.duration?.trend, { color: '#d4a017', formatter: Charts.formatDuration }));
        registry.render('size', els.size.value, Charts.lineOption(state.data.size?.trend, { color: '#4aa3df', formatter: Charts.formatBytes }));
      }

      async function reload() {
        if (granularity.value === 'hour' && !canUseHour.value) granularity.value = 'day';
        await loader.load(params());
        await renderCharts();
      }

      async function exportCsv() {
        exporting.value = true;
        try {
          const blob = await QuicDataAPI.downloadCollectionDashboardCsv(params());
          saveBlob(blob, csvName(props.filters));
        } catch (error) {
          ElementPlus.ElMessage.error(error?.message || text('导出失败', 'Export failed'));
        } finally {
          exporting.value = false;
        }
      }

      const onResize = () => registry?.resize();
      Vue.watch(() => [props.workspaceId, JSON.stringify(props.filters), granularity.value, basis.value], reload, { immediate: true });
      Vue.onMounted(() => window.addEventListener('resize', onResize));
      Vue.onBeforeUnmount(() => {
        window.removeEventListener('resize', onResize);
        registry?.dispose();
        loader.dispose();
      });

      const labels = Vue.computed(() => ({
        projects: text('项目总量', 'Projects'),
        tasks: text('任务总量', 'Tasks'),
        packages: text('数据包总量', 'Data packages'),
        duration: basis.value === 'valid' ? text('有效采集时长', 'Valid duration') : text('采集时长', 'Captured duration'),
        size: basis.value === 'valid' ? text('有效数据大小', 'Valid size') : text('采集数据大小', 'Captured size'),
      }));
      const statusRows = Vue.computed(() => {
        const counts = state.data?.packages?.status_counts || {};
        return [
          { key: 'pending_assignment', label: text('待分配', 'Awaiting assignment'), value: known(counts.pending_assignment) },
          { key: 'assigned', label: text('已分配', 'Assigned'), value: known(counts.assigned) },
          { key: 'other', label: text('上传及后续处理', 'Upload and subsequent processing'), value: known(counts.other) },
          { key: 'voided', label: text('已作废', 'Voided'), value: known(counts.voided) },
        ];
      });
      function display(card, value) {
        if (value === null) return '—';
        if (card.kind === 'duration') return Charts.formatDuration(value);
        if (card.kind === 'size') return Charts.formatBytes(value);
        return String(value);
      }

      return {
        state, granularity, basis, exporting, canUseHour, labels, statusRows, text, reload, exportCsv, display,
        cardList: Vue.computed(() => cards(state.data)),
        pending: Vue.computed(() => pendingHint(state.data, basis.value)),
        formatDuration: Charts.formatDuration,
        projectsChart: els.projects, tasksChart: els.tasks, packagesChart: els.packages, durationChart: els.duration, sizeChart: els.size,
      };
    },
    template: `
      <section class="view-stack" v-loading="state.loading" aria-label="Collection data board">
        <div class="page-actions">
          <el-radio-group v-model="granularity" size="small">
            <el-radio-button value="hour" :disabled="!canUseHour">{{ text('小时', 'Hour') }}</el-radio-button>
            <el-radio-button value="day">{{ text('日', 'Day') }}</el-radio-button>
            <el-radio-button value="month">{{ text('月', 'Month') }}</el-radio-button>
          </el-radio-group>
          <el-radio-group v-model="basis" size="small">
            <el-radio-button value="all">{{ text('全部', 'All') }}</el-radio-button>
            <el-radio-button value="valid">{{ text('有效', 'Valid') }}</el-radio-button>
          </el-radio-group>
          <el-button size="small" :disabled="state.loading || !workspaceId" @click="reload">{{ text('刷新', 'Refresh') }}</el-button>
          <el-button size="small" :loading="exporting" :disabled="!state.data" @click="exportCsv">{{ text('导出 CSV', 'Export CSV') }}</el-button>
        </div>
        <el-alert v-if="state.error" type="error" :closable="false" :title="state.error" show-icon />
        <el-alert v-if="state.data?.incomplete_packages" type="info" :closable="false" show-icon
          :title="text(state.data.incomplete_packages + ' 个包统计中，未计入时长和大小', state.data.incomplete_packages + ' packages are still being counted')" />
        <template v-if="state.data">
          <div class="metric-grid">
            <article v-for="card in cardList" :key="card.key" class="metric-card">
              <span>{{ labels[card.key] }}</span>
              <strong>{{ display(card, card.total) }}</strong>
              <small>{{ text('今日', 'Today') }} +{{ display(card, card.today) }}</small>
              <small v-if="card.key === 'duration' && pending !== null" class="muted">{{ text('另有 ' + formatDuration(pending) + ' 待审核，未计入', formatDuration(pending) + ' awaiting review, not counted') }}</small>
            </article>
          </div>
          <el-alert v-if="state.chartError" type="warning" :closable="false" :title="state.chartError" show-icon />
          <div class="dashboard-grid is-three">
            <section class="surface-panel"><div class="panel-heading"><h2>{{ text('项目数量趋势', 'Projects trend') }}</h2></div><div ref="projectsChart" class="dashboard-chart"></div></section>
            <section class="surface-panel"><div class="panel-heading"><h2>{{ text('任务数量趋势', 'Tasks trend') }}</h2></div><div ref="tasksChart" class="dashboard-chart"></div></section>
            <section class="surface-panel"><div class="panel-heading"><h2>{{ text('数据包数量趋势', 'Packages trend') }}</h2></div><div ref="packagesChart" class="dashboard-chart"></div></section>
          </div>
          <div class="dashboard-grid">
            <section class="surface-panel"><div class="panel-heading"><h2>{{ labels.duration }}{{ text('趋势', ' trend') }}</h2></div><div ref="durationChart" class="dashboard-chart"></div></section>
            <section class="surface-panel"><div class="panel-heading"><h2>{{ labels.size }}{{ text('趋势', ' trend') }}</h2></div><div ref="sizeChart" class="dashboard-chart"></div></section>
          </div>
          <section class="surface-panel table-panel">
            <div class="panel-heading"><div><h2>{{ text('数据包状态', 'Package status') }}</h2></div></div>
            <el-table :data="statusRows" row-key="key" class="data-table">
              <el-table-column :label="text('阶段', 'Stage')" prop="label" />
              <el-table-column :label="text('数据包数', 'Packages')"><template #default="scope">{{ scope.row.value ?? '—' }}</template></el-table-column>
            </el-table>
          </section>
        </template>
        <el-empty v-else-if="!state.loading && !state.error" :description="text('请选择工作空间', 'Select a workspace')" />
      </section>`,
  };

  return { requestParams, cards, pendingHint, hourAllowed, csvName, createLoader, component };
})();
```

在 `frontend/css/app.css` 末尾追加：

```css
.dashboard-grid.is-three { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.collection-dashboard-filters .el-select { width: 200px; }
@media (max-width: 1200px) { .dashboard-grid.is-three { grid-template-columns: 1fr; } }
```

（执行时先确认 `app.css` 中已有 `.dashboard-grid` 与 `.dashboard-chart` 规则，沿用它们的尺寸；没有则补 `.dashboard-chart { height: 260px; }`。）

- [ ] **Step 4: 运行测试确认通过**

Run: `node --test frontend/tests/collection-data-board.test.mjs`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add frontend/js/collection-data-board.js frontend/tests/collection-data-board.test.mjs frontend/css/app.css
git commit -m "feat(dashboard): 数采看板组件（卡片、五张趋势图、状态表、CSV 导出）"
```

---

### Task 9: 采集概览外壳接入

**Files:**
- Modify（改写）: `frontend/js/collection-overview.js`
- Modify: `frontend/js/page-components.js`
- Test: `frontend/tests/collection-overview.test.mjs`（改写）

**Interfaces:**
- Consumes: `QuicStudioCollectionDashboardFilters`（Task 7）、`QuicStudioCollectionDataBoard`（Task 8）、`QuicDataAPI.listCollectionTasks`、`QuicDataAPI.listCollectionLabels`
- Produces: `QuicStudioCollectionOverview.{ install(app) }`；页面 `collection-overview` 的 props 不变（`workspaceId`、`projects`、`locale`）

- [ ] **Step 1: 写失败测试**

改写 `frontend/tests/collection-overview.test.mjs`：

```js
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const read = (name) => readFileSync(new URL(`../js/${name}`, import.meta.url), 'utf8');

function installOverview() {
  const context = vm.createContext({});
  vm.runInContext(`${read('dashboard-charts.js')}\n${read('collection-dashboard-filters.js')}\n${read('collection-data-board.js')}\n${read('collection-overview.js')}\nglobalThis.overview = QuicStudioCollectionOverview;`, context);
  let registered = null;
  context.overview.install({ component(name, definition) { registered = { name, definition }; } });
  return registered;
}

test('overview shell registers three dashboard tabs with the shared filter bar and data board', () => {
  const { name, definition } = installOverview();
  assert.equal(name, 'collection-overview');
  assert.ok(definition.components['collection-dashboard-filters']);
  assert.ok(definition.components['collection-data-board']);
  for (const tab of ['产能看板', '数采看板', '人效看板']) assert.match(definition.template, new RegExp(tab));
  assert.match(definition.template, /<collection-data-board v-if="board === 'data'"/);
});

test('overview source does not touch dependencies at load time', () => {
  const context = vm.createContext({});
  assert.doesNotThrow(() => vm.runInContext(read('collection-overview.js'), context));
});

test('page loader fetches overview dependencies before the page script', () => {
  const source = read('page-components.js');
  assert.match(source, /'collection-overview': \{ deps: \['\/js\/dashboard-charts\.js\?v=1', '\/js\/collection-dashboard-filters\.js\?v=1', '\/js\/collection-data-board\.js\?v=1'\]/);
  assert.match(source, /page\.deps/);
});
```

- [ ] **Step 2: 运行测试确认失败**

Run: `node --test frontend/tests/collection-overview.test.mjs`
Expected: FAIL（`definition.components` 未定义）

- [ ] **Step 3: 改写外壳与加载器**

`frontend/js/collection-overview.js` 全文替换为：

```js
/* Collection overview shell: three dashboards sharing one filter bar. */
const QuicStudioCollectionOverview = (() => {
  function install(app) {
    const Filters = QuicStudioCollectionDashboardFilters;
    const DataBoard = QuicStudioCollectionDataBoard;
    app.component('collection-overview', {
      components: {
        'collection-dashboard-filters': Filters.component,
        'collection-data-board': DataBoard.component,
      },
      props: {
        workspaceId: { type: [Number, String], default: null },
        projects: { type: Array, default: () => [] },
        locale: { type: String, default: 'zh-CN' },
      },
      setup(props) {
        const board = Vue.ref('data');
        const filters = Vue.ref(Filters.defaultFilters());
        const tasks = Vue.ref([]);
        const labels = Vue.ref([]);
        let generation = 0;
        const items = (payload) => (Array.isArray(payload?.items) ? payload.items : (Array.isArray(payload) ? payload : []));
        async function loadOptions(workspaceId) {
          const current = ++generation;
          tasks.value = [];
          labels.value = [];
          if (!workspaceId) return;
          const [taskResult, labelResult] = await Promise.allSettled([
            QuicDataAPI.listCollectionTasks(workspaceId),
            QuicDataAPI.listCollectionLabels({ workspace_id: workspaceId }),
          ]);
          if (current !== generation) return;
          tasks.value = taskResult.status === 'fulfilled' ? items(taskResult.value) : [];
          labels.value = labelResult.status === 'fulfilled' ? items(labelResult.value) : [];
        }
        Vue.watch(() => props.workspaceId, (workspaceId) => {
          filters.value = Filters.defaultFilters();
          loadOptions(workspaceId);
        }, { immediate: true });
        const text = (zh, en) => (props.locale === 'en-US' ? en : zh);
        return { board, filters, tasks, labels, text, params: Vue.computed(() => Filters.toParams(filters.value)) };
      },
      template: `
        <section class="view-stack" aria-label="Collection overview">
          <el-tabs v-model="board" class="overview-board-tabs">
            <el-tab-pane :label="text('产能看板', 'Capacity dashboard')" name="capacity" />
            <el-tab-pane :label="text('数采看板', 'Collection dashboard')" name="data" />
            <el-tab-pane :label="text('人效看板', 'Efficiency dashboard')" name="people" />
          </el-tabs>
          <collection-dashboard-filters v-model="filters" :projects="projects" :tasks="tasks" :labels="labels" :locale="locale" />
          <collection-data-board v-if="board === 'data'" :workspace-id="workspaceId" :filters="params" :locale="locale" />
          <el-empty v-else :description="text('该看板建设中', 'This dashboard is under construction')" />
        </section>`,
    });
  }
  return { install };
})();
```

`frontend/js/page-components.js`：

1. 把 `collection-overview` 条目改为：

```js
    'collection-overview': { deps: ['/js/dashboard-charts.js?v=1', '/js/collection-dashboard-filters.js?v=1', '/js/collection-data-board.js?v=1'], js: '/js/collection-overview.js?v=4', module: () => QuicStudioCollectionOverview },
```

2. `load(name)` 中把

```js
    const promise = Promise.all([loadAsset(page.js), ...(page.css ? [loadAsset(page.css, true)] : [])])
```

改为

```js
    const promise = (page.deps || []).reduce((chain, url) => chain.then(() => loadAsset(url)), Promise.resolve())
      .then(() => Promise.all([loadAsset(page.js), ...(page.css ? [loadAsset(page.css, true)] : [])]))
```

3. `frontend/index.html` 中 `page-components.js?v=8` 改为 `?v=9`。

- [ ] **Step 4: 运行测试确认通过**

Run: `node --test frontend/tests/*.test.mjs`
Expected: 除基线已知失败（admin-console 工作空间成员用例）外全部 PASS。若 `session-restore`、`list-filter-sort` 等引用 `collection-overview.js` 的测试失败，按新外壳（不在加载时访问依赖、props 不变）修正断言，不回退外壳。

- [ ] **Step 5: 提交**

```bash
git add frontend/js/collection-overview.js frontend/js/page-components.js frontend/index.html frontend/tests/collection-overview.test.mjs
git commit -m "feat(dashboard): 采集概览外壳接入全局筛选与数采看板"
```

---

### Task 10: 数采任务分配修复（入口、状态、拆包时长）

**Files:**
- Modify: `backend/data/services/collection_tasks.py`（`list_collection_tasks_with_progress`，约第 165–196 行）
- Modify: `backend/data/routers/collection_tasks.py`（`_task_item`、`_progress_for_task`、列表与详情调用处）
- Modify: `frontend/js/mining-utils.js`
- Modify: `frontend/js/app.js`（`miningTaskAssignDone` 约第 9102 行；`openMiningTaskAssignDialog` 约第 9216 行；`submitMiningAssign` 约第 9290–9310 行；列表映射约第 8257 行；新建任务约第 598、9018 行；模板：任务行约第 11696 行、新建任务对话框约第 11713 行、分配对话框约第 11740 行、抽屉顶部约第 11760 行、数据包行约第 11828 行）
- Modify: `frontend/js/demo-data.js`（`/collection-tasks` 列表补计数）
- Modify: `frontend/index.html`（`mining-utils.js?v=1` → `?v=2`）
- Test: `backend/tests/test_collection_tasks_api.py`（追加）、`frontend/tests/mining-utils.test.mjs`（追加）、`frontend/tests/mining-assign-entry.test.mjs`（新）

**Interfaces:**
- Produces:
  - 任务列表与详情每项新增 `pending_assignment_count: int`
  - `QuicDataMiningUtils.canAssignPackage(pkg) -> boolean`（`(pkg.raw_status || pkg.status) === 'pending_assignment'`）
  - `QuicDataMiningUtils.taskAssignDone(task) -> boolean`（`assign_mode === 'task'` 仍为真，供演示数据；否则 `package_count > 0 && pending_assignment_count === 0`；缺计数时为 `false`）
  - `QuicDataMiningUtils.packageCountPreview(targetHours, packageHours) -> number | null`（与后端 `split_package_durations` 一致：按 0.01 小时取整后 `整包数 + (余数 > 0 ? 1 : 0)`；非法输入为 `null`）

- [ ] **Step 1: 写失败测试**

追加到 `backend/tests/test_collection_tasks_api.py`：

```python
def test_list_reports_pending_assignment_count(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    created = client.post(
        "/api/v1/collection-tasks",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "collection_project_id": project.id,
            "name": "Pending count",
            "target_duration_hours": "6.00",
            "default_package_duration_hours": "2.00",
            "label_ids": [],
        },
    )
    task_id = created.json()["data"]["id"]
    packages = (
        db_session.query(DataPackage)
        .filter(DataPackage.collection_task_id == task_id)
        .order_by(DataPackage.id)
        .all()
    )
    collector = make_collector(db_session, workspace, name="Assigned")
    packages[0].status = "assigned"
    packages[0].responsible_collector_id = collector.id
    packages[0].operator_collector_id = collector.id
    packages[1].status = "voided"
    db_session.commit()

    listed = client.get(
        "/api/v1/collection-tasks", headers=admin_headers, params={"workspace_id": workspace.id}
    )
    item = next(row for row in listed.json()["data"]["items"] if row["id"] == task_id)
    detail = client.get(
        f"/api/v1/collection-tasks/{task_id}", headers=admin_headers, params={"workspace_id": workspace.id}
    )

    assert item["package_count"] == 3
    assert item["pending_assignment_count"] == 1
    assert detail.json()["data"]["pending_assignment_count"] == 1
```

追加到 `frontend/tests/mining-utils.test.mjs`：

```js
test('only pending_assignment packages can be assigned', () => {
  const utils = loadUtils();
  assert.equal(utils.canAssignPackage({ status: 'pending_assignment' }), true);
  assert.equal(utils.canAssignPackage({ raw_status: 'pending_assignment', status: 'planned' }), true);
  assert.equal(utils.canAssignPackage({ raw_status: 'assigned', status: 'collecting' }), false);
  assert.equal(utils.canAssignPackage({ status: 'planned' }), false);
  assert.equal(utils.canAssignPackage(null), false);
});

test('taskAssignDone ignores batches and uses list counts', () => {
  const utils = loadUtils();
  assert.equal(utils.taskAssignDone({ package_count: 3, pending_assignment_count: 0, batches: [] }), true);
  assert.equal(utils.taskAssignDone({ package_count: 3, pending_assignment_count: 1, batches: [{ assignees: [{ collector_id: 1 }] }] }), false);
  assert.equal(utils.taskAssignDone({ package_count: 0, pending_assignment_count: 0 }), false);
  assert.equal(utils.taskAssignDone({ package_count: 3 }), false);
  assert.equal(utils.taskAssignDone({ assign_mode: 'task' }), true);
});

test('package count preview matches backend split rule', () => {
  const utils = loadUtils();
  assert.equal(utils.packageCountPreview(100, 2), 50);
  assert.equal(utils.packageCountPreview(5, 2), 3);
  assert.equal(utils.packageCountPreview(0.3, 0.1), 3);
  assert.equal(utils.packageCountPreview(1, 0), null);
  assert.equal(utils.packageCountPreview('', 2), null);
});
```

`frontend/tests/mining-assign-entry.test.mjs`：

```js
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const app = readFileSync(new URL('../js/app.js', import.meta.url), 'utf8');

test('task row and task drawer expose task-level assignment', () => {
  assert.match(app, /@click\.stop="openMiningAssignModeDialog\(scope\.row\)"/);
  assert.match(app, /@click="openMiningAssignModeDialog\(miningSelectedTask\)"/);
});

test('package rows expose per-package assignment only when assignable', () => {
  assert.match(app, /v-if="canAssignPackage\(scope\.row\)"[^>]*@click\.stop="openMiningAssignDialog\(scope\.row\)"/);
});

test('assignment status and filters use the shared helpers', () => {
  assert.match(app, /function miningTaskAssignDone\(task\) \{\s*return QuicDataMiningUtils\.taskAssignDone\(task\);/);
  assert.doesNotMatch(app, /miningAssignOnlyUnassigned/);
  assert.match(app, /没有待分配的数据包/);
});

test('device and window fields are demo-only; copy describes round-robin', () => {
  assert.match(app, /<label v-if="demoMode" class="login-field"><span>\{\{ t\('collectionDevice'\) \}\}/);
  assert.match(app, /<label v-if="demoMode && miningAssignScope === 'batch'" class="login-field"><span>\{\{ t\('batchWindow'\) \}\}/);
  assert.match(app, /assignModeEven: '按顺序轮流分配待分配数据包'/);
  assert.doesNotMatch(app, /assignAvgHint/);
});

test('task creation lets the administrator set package duration', () => {
  assert.match(app, /v-model="miningTaskForm\.package_hours"/);
  assert.match(app, /default_package_duration_hours: packageHours\.toFixed\(2\)/);
  assert.match(app, /const miningPackageCountPreview = computed\(\(\) => QuicDataMiningUtils\.packageCountPreview\(miningTaskForm\.target_duration_hours, miningTaskForm\.package_hours\)\)/);
  assert.match(app, /count: miningPackageCountPreview \?\? '—'/);
});
```

- [ ] **Step 2: 运行测试确认失败**

Run: `$PYTEST backend/tests/test_collection_tasks_api.py -k pending_assignment_count`
Expected: FAIL，`KeyError: 'pending_assignment_count'`

Run: `node --test frontend/tests/mining-utils.test.mjs frontend/tests/mining-assign-entry.test.mjs`
Expected: FAIL（`utils.canAssignPackage is not a function`；源码断言不匹配）

- [ ] **Step 3: 后端计数**

`list_collection_tasks_with_progress` 的 `db.query(...)` 中，在 `assigned_count` 之后加：

```python
            func.sum(
                case((DataPackage.status == "pending_assignment", 1), else_=0)
            ).label("pending_assignment_count"),
```

返回元组改为 5 项：

```python
        (task, int(package_count), int(assigned_count or 0), Decimal(intake_duration), int(pending or 0))
        for task, package_count, assigned_count, intake_duration, pending in rows
```

（执行时以该函数实际的 select 列顺序为准，保证元组顺序与解包一致。）

`backend/data/routers/collection_tasks.py`：`_task_item` 增加参数 `pending_assignment_count: int = 0`，并在返回字典中加 `"pending_assignment_count": pending_assignment_count,`。用 `grep -n "for task, package_count, assigned_count, intake_duration\|for candidate, package_count" backend/data/routers/collection_tasks.py` 找到所有解包处，统一改为 5 项并把第 5 项传给 `_task_item(pending_assignment_count=...)`；`_progress_for_task` 返回值同样补上第 4 项并在详情接口传入。

- [ ] **Step 4: 前端纯函数**

`frontend/js/mining-utils.js`：在 `QuicDataMiningUtils` 内、`return Object.freeze({` 之前加：

```js
  function canAssignPackage(pkg) {
    return Boolean(pkg) && (pkg.raw_status || pkg.status) === 'pending_assignment';
  }

  function taskAssignDone(task) {
    if (!task) return false;
    if (task.assign_mode === 'task') return true;
    const total = Number(task.package_count);
    const pending = Number(task.pending_assignment_count);
    return Number.isFinite(total) && total > 0 && Number.isFinite(pending) && pending === 0;
  }

  function packageCountPreview(targetHours, packageHours) {
    if (targetHours === '' || packageHours === '' || targetHours == null || packageHours == null) return null;
    const target = Math.round(Number(targetHours) * 100);
    const size = Math.round(Number(packageHours) * 100);
    if (!Number.isFinite(target) || !Number.isFinite(size) || target <= 0 || size <= 0) return null;
    return Math.floor(target / size) + (target % size > 0 ? 1 : 0);
  }
```

并在 `Object.freeze({ ... })` 中加入 `canAssignPackage, taskAssignDone, packageCountPreview,`。`frontend/index.html` 中 `mining-utils.js?v=1` 改为 `?v=2`。

- [ ] **Step 5: app.js 修改**

1. 状态判定：`miningTaskAssignDone` 函数体替换为

```js
      function miningTaskAssignDone(task) {
        return QuicDataMiningUtils.taskAssignDone(task);
      }
      function canAssignPackage(row) {
        return QuicDataMiningUtils.canAssignPackage(row);
      }
```

并把 `canAssignPackage` 加入 setup 的 return 列表（与 `openMiningAssignDialog` 同一行）。

2. 列表映射（约第 8257 行起的 `miningTasks.value = rawTasks.map(...)`）返回对象中补上 `pending_assignment_count: task.pending_assignment_count,`（`...task` 已展开，显式写出以示依赖）。

3. 待分配判定统一：拆包处（约第 9073 行）的 filter 改为 `.filter((b) => QuicDataMiningUtils.canAssignPackage(b))`；`submitMiningAssign` 任务分支改为

```js
            const targetBatches = batches.filter((b) => QuicDataMiningUtils.canAssignPackage(b));
            if (!targetBatches.length) { ElMessage.warning('没有待分配的数据包'); return; }
            if (collectorIds.length > targetBatches.length) { ElMessage.warning('数采员数量不能超过待分配包数量'); return; }
```

删除 `onlyUnassigned` 变量、`miningAssignOnlyUnassigned` 的 `ref` 声明、所有赋值与 return 导出；演示分支中 `only_unassigned: assignScope === 'task' ? onlyUnassigned : false` 改为 `only_unassigned: assignScope === 'task'`。

4. `openMiningTaskAssignDialog` 中把

```js
        if (!currentTask.batches || !currentTask.batches.length) {
          ElMessage.warning('该任务暂无可用数据包，请先拆分数据包');
          return;
        }
```

改为

```js
        if (!(currentTask.batches || []).some((batch) => QuicDataMiningUtils.canAssignPackage(batch))) {
          resetMiningAssignDialogState();
          ElMessage.warning('没有待分配的数据包');
          return;
        }
```

5. 分配对话框模板：删除 `assignOnlyUnassigned` 开关那一行与 `assignAvgHint` 那一行；「采集设备」label 改为 `<label v-if="demoMode" class="login-field">`；「采集窗口」label 改为 `<label v-if="demoMode && miningAssignScope === 'batch'" class="login-field">`。文案：中文 `assignModeEven: '按顺序轮流分配待分配数据包'`，英文 `assignModeEven: 'Assign pending packages in turn'`；中文 `assignToTaskHint` 改为 `'所选采集员将按顺序轮流分配到该任务的待分配数据包，已分配的数据包不受影响。'`，英文改为 `'Selected collectors are assigned in turn to the task\'s pending packages; assigned packages are unchanged.'`；删除中英文 `assignAvgHint` 键及 `miningAssignAvgHours` 等只为该提示存在的计算属性（用 `grep -n "miningAssignAvgHours\|miningAssignTargetHours\|assignAvgHint" frontend/js/app.js` 确认无其他引用后删除）。

6. 入口：
   - 任务行操作列（约第 11696 行）：宽度 `220` 改回 `270`，在「查看数据包」按钮与下拉之间插入

     ```html
     <el-button link type="primary" @click.stop="openMiningAssignModeDialog(scope.row)">{{ t('assignTaskAction') }}</el-button><el-divider direction="vertical" style="margin: 0 8px; height: 12px;" />
     ```
   - 抽屉顶部 `summary-actions` 中「目标拆解为数据包」按钮之前插入

     ```html
     <el-button type="primary" size="small" @click="openMiningAssignModeDialog(miningSelectedTask)">{{ t('assignTaskAction') }}</el-button>
     ```
   - 数据包行操作列（约第 11828 行）：宽度 `230` 改为 `280`，在「数采审核」按钮后的分隔线之后、`更多` 下拉之前插入

     ```html
     <el-button v-if="canAssignPackage(scope.row)" link type="primary" @click.stop="openMiningAssignDialog(scope.row)">{{ t('assignPackageAction') }}</el-button><el-divider v-if="canAssignPackage(scope.row)" direction="vertical" style="margin: 0 8px; height: 12px;" />
     ```
   - 文案：中文 `assignPackageAction: '分配'`，英文 `assignPackageAction: 'Assign'`。

7. 新建任务单包时长：
   - `miningTaskForm` 初值加 `package_hours: 2`；`resetMiningTaskDialogState` 中加 `miningTaskForm.package_hours = 2;`。
   - `createMiningTask` 中把 `const defaultPkgHours = 2.0;` 替换为

     ```js
        const packageHours = Number(miningTaskForm.package_hours);
        if (!Number.isFinite(packageHours) || packageHours < 0.01) {
          ElMessage.warning(locale.value === 'en-US' ? 'Package duration must be at least 0.01 hours.' : '单包目标时长必须至少为 0.01 小时');
          return;
        }
     ```
     并把 `default_package_duration_hours: defaultPkgHours.toFixed(2),` 改为 `default_package_duration_hours: packageHours.toFixed(2),`。
   - 新建任务对话框中「目标时长」label 之后插入

     ```html
     <label class="login-field"><span>{{ t('packageDurationHours') }}</span><el-input-number v-model="miningTaskForm.package_hours" :min="0.01" :step="0.5" :precision="2" style="width: 100%;" /><span class="muted" style="margin-left: 8px;">{{ t('packageCountPreview', { count: miningPackageCountPreview ?? '—' }) }}</span></label>
     ```
     Vue 模板不能访问任意全局对象，所以在 setup 中定义 `const miningPackageCountPreview = computed(() => QuicDataMiningUtils.packageCountPreview(miningTaskForm.target_duration_hours, miningTaskForm.package_hours));` 并加入 return 列表，模板里只引用 `miningPackageCountPreview`。
   - 文案：中文 `packageDurationHours: '单包目标时长（小时）'`、`packageCountPreview: '将生成 {count} 个数据包'`；英文 `packageDurationHours: 'Package target duration (h)'`、`packageCountPreview: '{count} packages will be generated'`。确认 `t()` 支持 `{count}` 占位（`assignAvgHint` 使用了同样的写法）。

8. `frontend/js/demo-data.js` 的 `/collection-tasks` 分支改为给每项补计数：

```js
    if (path === '/collection-tasks') {
      const items = collectionTasks
        .filter((task) => !params.project_id || Number(task.project_id) === Number(params.project_id))
        .map((task) => {
          const packages = dataPackages.filter((pkg) => Number(pkg.collection_task_id) === Number(task.id));
          return { ...task, package_count: packages.length, pending_assignment_count: packages.filter((pkg) => pkg.status === 'pending_assignment').length };
        });
      return { items, total: items.length };
    }
```

- [ ] **Step 6: 运行测试确认通过**

Run: `$PYTEST backend/tests/test_collection_tasks_api.py`
Expected: PASS

Run: `node --test frontend/tests/*.test.mjs`
Expected: 除基线已知失败外全部 PASS（`scope-write-guards.test.mjs` 若引用了 `miningAssignOnlyUnassigned`，同步删除该引用）

- [ ] **Step 7: 提交**

```bash
git add backend/data/services/collection_tasks.py backend/data/routers/collection_tasks.py backend/tests/test_collection_tasks_api.py frontend/js/mining-utils.js frontend/js/app.js frontend/js/demo-data.js frontend/index.html frontend/tests/mining-utils.test.mjs frontend/tests/mining-assign-entry.test.mjs frontend/tests/scope-write-guards.test.mjs
git commit -m "fix(mining): 恢复分配采集员入口，分配状态改按任务计数，新建任务可填单包时长"
```

---

### Task 11: 全量验证与浏览器验收

**Files:** 无代码改动（发现问题回到对应 Task 修复）

- [ ] **Step 1: 后端全量**

Run: `$PYTEST backend/tests`
Expected: 全部 PASS（`slow` 用例默认跳过）

- [ ] **Step 2: 前端全量**

Run: `node --test frontend/tests/*.test.mjs`
Expected: 仅基线已知失败 1 项

- [ ] **Step 3: 本地浏览器验收**

1. 迁移并回填：`make dev-migrate`，然后 `cd backend && ../.venv/bin/python -m scripts.backfill_package_dashboard_facts`，确认输出中 `mismatches` 为空或逐一解释。
2. 启动：`make dev-api`（及前端静态服务，按 README），在浏览器中打开采集概览：
   - 三个标签页；数采看板默认选中；5 张卡、5 张趋势图、状态表；状态表四项之和等于「数据包总量」。
   - 切换粒度（7 天以上范围时「小时」不可选）、口径（「有效」时出现待审核提示）。
   - 选项目后任务选项收窄；导出 CSV 可下载，用 Excel 打开中文不乱码。
3. 数采任务：
   - 新建任务填写单包时长，预览包数与实际生成一致。
   - 任务行与抽屉顶部「分配任务」、数据包行「分配」均可用；分配后「更多」中清单下载可用。
   - 进出「查看数据包」抽屉前后，任务「分配状态」不变；全部分配后显示「已完成」。
4. 抽取若干包，将看板数字与数据包详情逐一对照。

- [ ] **Step 4: 汇报**

记录验收结果与任何偏差，不自行扩大范围。

<!-- fmt:on -->
