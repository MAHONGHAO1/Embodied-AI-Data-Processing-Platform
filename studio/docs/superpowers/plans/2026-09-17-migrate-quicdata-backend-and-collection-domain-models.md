# 迁移 quicdata 后端并建立采集域模型 Implementation Plan

> 2026-09-17 检查收口：Task 1–9 的代码已落地并参加全量回归；启动脚本旧包引用和本地前端 Make 入口已修复。当前进度、验证与限制见 [Plan 1–3 检查记录](../../PLAN_1_3_REVIEW.md)。下方保留原实施步骤，不用历史步骤复选框表示当前完成率。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 quicdata 的后端整体迁入 quic_studio 的 `backend/data/`，压平迁移历史，并建立 QuicStudio `init` 采集域的数据库模型基线。

**Architecture:** quic_studio 目前只有静态前端和文档。前四个任务把 quicdata 的 `backend/app/` 复制成 `backend/data/`（包名随之改），把 43 个历史迁移压成单个 baseline，搬运部署与开发脚本，并为同事预留平行的 `backend/train/`。后五个任务在新包下的 `data/models/` 子包里建采集域模型，每个任务自带迁移——测试夹具走 `alembic upgrade head` 建库，没有迁移的模型无法测试。

**Tech Stack:** Python 3.11、FastAPI、SQLAlchemy 2.0（经典 `Column()` 风格，非 `Mapped[]`）、Alembic、PostgreSQL、Redis、Celery、pytest

**Spec:** `docs/superpowers/specs/2026-09-14-collection-studio-init-design.md`

**源仓库:** `/Users/qingmuhy/Documents/devlop/quicrobot/quicdata`（分支 `develop`）。本计划只读它，不修改它。

**本计划是 4 份顺序计划的第 ①份。** 后续：② 采集管理 API ③ 接入与入库审核 ④ 治理、标注审核、资产与数据集。

## Global Constraints

以下取值直接抄自规格，每个任务的要求都隐含包含本节。

- 采集模式本期固定为 `offline`，在线分发不实现。
- Episode 三种有效性状态：`有效`、`人工不合格`（入库审核标记）、`QC 淘汰`（数据批治理标记）。两种失效状态用独立来源字段区分，不互相覆盖。
- 数据包主路径：`待分配 → 已分配 → 待上传 → 上传中 → 解析中 → 已接入 → 待入库审核 → 入库审核通过 → 已建批 → 治理中 → 已发布`。两个终止分支：`解析失败`、`已作废`。
- 数据包一经分配即锁定，不可改派。
- 入库审核由管理员执行，强制环节，支持批量通过。
- 标签字典至少五类：场景标签、任务用途、训练标签、项目标签、数据模态。
- 双有效时长：**入库有效时长**（入库审核后不变，看板与任务进度用）与**治理后有效时长**（扣除 QC 淘汰，资产与数据集用）。
- `init` 中一个数据包最多归属一个数据批。
- 任务不设地区字段，不设人员字段。
- 数据格式版本：QRDF `0.2`、LeRobot `3.0`。
- 继承自 quicdata `AGENTS.md`：后端 AuthN/AuthZ 是权威，前端权限检查仅用于 UX；作用域无法解析时默认拒绝；文件读写与预览必须留在 `storage_root` 内；不提交明文密钥；保持每次变更聚焦单一关注点。

## 迁移决策

| 决策 | 取值 | 理由 |
|---|---|---|
| 迁移范围 | 全量 backend，遗留能力先留着 | 92 个 service 互相引用，外科式裁剪等于重写；遗留模型在后续计划里随新模型上线逐步废弃 |
| 迁移历史 | 43 个历史迁移压成单个 `0001_baseline` | 新库无存量数据，历史修复脚本是噪音 |
| 前端 | 不迁 quicdata 前端 | quic_studio 已有新设计前端，规格要求沿用它 |
| deploy / scripts / Makefile | 迁并改路径 | 部署链路要能立刻验证，重写会拖慢联调 |
| 运行时存储根 | `backend/runtime/storage` | 新仓无 `backend/data/` 冲突，把运行时数据放在 Python 包外比嵌套 `data/data/` 干净；生产仍走 `STORAGE_ROOT` 环境变量 |

## File Structure

```text
quic_studio/
  backend/
    data/                     ← 源自 quicdata backend/app/，包名改为 data
      main.py  config.py  database.py  runtime.py  celery_app.py  bootstrap.py
      routers/  services/  schemas/  tasks/  utils/  security/  infra/  integrations/  realtime/
      models/                 ← 本计划新建：采集域模型子包
        __init__.py
        collection_config.py  ← CollectionLabel、CollectionDeviceModel
        collection_core.py    ← CollectionProject、CollectionTask、CollectionTaskLabel
        data_package.py       ← DataPackage、PackageIntakeReview
        data_batch.py         ← DataBatch、DataBatchPackage、DataBatchLabel
    train/                    ← 占位包，同事后续填充
    alembic/versions/         ← 0001_baseline + 0002–0006
    tests/  scripts/  vendor/qrdf/
    runtime/storage/          ← 运行时存储（gitignore）
    pyproject.toml  pytest.ini  run.py  .env.example
  deploy/  scripts/  Makefile
  frontend/                   ← 保持不动
  docs/
```

新模型放 `data/models/` 而不是追加进 `database.py`：后者迁过来就有 1605 行、45 个模型类，继续堆会更难维护；既有模型保持原位，避免把机械迁移和业务重构混在一起。

**测试命令**（quic_studio 根目录执行，需要本机 PostgreSQL 与 Redis）：

```bash
TEST_DATABASE_URL=postgresql+psycopg://quicdata:quicdata@127.0.0.1:5432/quicdata_test_studio .venv/bin/python -m pytest backend/tests
```

数据库名必须以 `quicdata_test` 开头——`conftest.py` 有硬校验。

---

### Task 1: 迁入后端骨架

**Files:**
- Create: `backend/data/**`（源 `quicdata/backend/app/**`）
- Create: `backend/alembic/**`、`backend/tests/**`、`backend/scripts/**`、`backend/vendor/qrdf/**`
- Create: `backend/pyproject.toml`、`backend/pytest.ini`、`backend/run.py`、`backend/.env.example`
- Modify: `backend/data/config.py`（`BASE_DIR` 与存储路径）
- Modify: `backend/data/utils/storage_paths.py`
- Modify: `backend/alembic/env.py`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: 无
- Produces: 可导入的 `data` 包；`data.main:app`、`data.celery_app:celery_app`；后续所有任务的导入前缀

- [x] **Step 1: 复制后端，排除环境与运行时产物**

```bash
cd /Users/qingmuhy/Documents/devlop/quicrobot/quic_studio
SRC=/Users/qingmuhy/Documents/devlop/quicrobot/quicdata
mkdir -p backend
rsync -a \
  --exclude '.venv/' --exclude '__pycache__/' --exclude '*.pyc' \
  --exclude '.pytest_cache/' --exclude '*.egg-info/' \
  --exclude 'data/' --exclude 'logs/' --exclude '.pids/' \
  "$SRC/backend/app/" backend/data/
rsync -a --exclude '__pycache__/' --exclude '*.pyc' \
  "$SRC/backend/alembic/" backend/alembic/
rsync -a --exclude '__pycache__/' --exclude '*.pyc' --exclude '.pytest_cache/' \
  "$SRC/backend/tests/" backend/tests/
rsync -a --exclude '__pycache__/' --exclude '*.pyc' \
  "$SRC/backend/scripts/" backend/scripts/
rsync -a --exclude '__pycache__/' --exclude '*.pyc' --exclude '.venv/' \
  "$SRC/backend/vendor/" backend/vendor/
cp "$SRC/backend/pyproject.toml" "$SRC/backend/pytest.ini" "$SRC/backend/run.py" backend/
cp "$SRC/backend/.env.example" backend/
ls backend/
```

预期：`backend/` 下有 `data alembic tests scripts vendor pyproject.toml pytest.ini run.py .env.example`。

`backend/data/` 是从 `app/` 复制来的，`backend/vendor/qrdf/` 是 QRDF SDK（源仓里 gitignore，部署时从 Git 拉；这里先带一份供本地开发）。

- [x] **Step 2: 改写导入前缀**

```bash
grep -rl "from app\.\|from app import\|import app\b" --include="*.py" backend/ \
  | grep -v "backend/vendor/\|__pycache__" \
  | xargs sed -i '' -E 's/\bfrom app\./from data./g; s/\bfrom app import\b/from data import/g; s/\bimport app\./import data./g; s/^import app$/import data/g'
grep -rn "from app\.\|from app import\|import app\b" --include="*.py" backend/ | grep -v "backend/vendor/\|__pycache__" | head
```

预期：第二条命令无输出。有残留就逐条手工改，不要加通配兜底。

- [x] **Step 3: 修正路径基准与存储根**

`config.py` 原来在 `backend/app/`，`BASE_DIR = Path(__file__).resolve().parent.parent` 指向 `backend/`。包路径深度没变（`backend/data/`），所以 `BASE_DIR` 不用改，仍然指向 `backend/`。只改存储路径，让运行时数据落在 Python 包外面。

`backend/data/config.py` 第 137 行与 149 行的存储路径改为：

```python
    storage_root: str = str(BASE_DIR / "runtime" / "storage")
```

```python
    ego_source_root: str = str(BASE_DIR / "runtime" / "ego-source")
```

第 739 行的运行时配置路径改为：

```python
_RUNTIME_CONFIG_PATH = BASE_DIR / "runtime" / "runtime_config.json"
```

`DEFAULT_EMBODIED_VL_SDK_DIR`、`DEFAULT_EMBODIED_VL_CHECKSUMS_FILE`（第 27-28 行）与 `env_file`（第 34 行）都基于 `BASE_DIR`，语义未变，保持原样。

- [x] **Step 4: 更新存储路径前缀归一化**

`backend/data/utils/storage_paths.py` 第 132 行。源仓的两个历史前缀在新仓没有存量数据，直接换成新前缀：

```python
    for prefix in (f"{root_name}/", "runtime/storage/", "backend/runtime/storage/"):
```

- [x] **Step 5: 更新 alembic 与入口**

`backend/alembic/env.py` 第 9 行：

```python
from data.database import Base
```

`backend/run.py`：

```python
"""QuicStudio 数据后端入口。"""

import uvicorn

import data.bootstrap  # noqa: F401
from data.config import settings

if __name__ == "__main__":
    uvicorn.run("data.main:app", host=settings.api_host, port=settings.api_port, reload=True)
```

`backend/pyproject.toml` 的包发现段与项目名：

```toml
[project]
name = "quicstudio-backend"
```

```toml
[tool.setuptools.packages.find]
where = ["."]
include = ["data*", "scripts*"]
```

其余依赖列表原样保留。

- [x] **Step 6: 写 `.gitignore`**

quic_studio 现有的 `.gitignore` 只有 4 行，要补齐后端所需。覆盖写入：

```gitignore
# Python
__pycache__/
*.py[cod]
*.egg-info/
.venv/
venv/
.env
backend/.env
deploy/secrets/*
!deploy/secrets/.gitignore
deploy/.env
deploy/runtime/*
!deploy/runtime/.gitignore

# Runtime data & uploads
backend/runtime/
backend/uploads/
backend/logs/
backend/.pids/
*.db

# IDE
.idea/
.vscode/
*.swp
.qoder/

# OS
.DS_Store
Thumbs.db

# 测试与缓存
.pytest_cache/
.mypy_cache/
.ruff_cache/
*.log
celerybeat-schedule*

# QRDF SDK：部署时从 Git 拉取，不提交源码
backend/vendor/qrdf/*
!backend/vendor/qrdf/.gitkeep

# Local linked worktrees
.worktrees/

.superpowers/
```

建占位文件让 vendor 目录结构保留：

```bash
touch backend/vendor/qrdf/.gitkeep
mkdir -p backend/runtime/storage
```

- [x] **Step 7: 建虚拟环境并装依赖**

```bash
cd /Users/qingmuhy/Documents/devlop/quicrobot/quic_studio
python3.11 -m venv .venv
uv sync --project backend --no-dev --group test --active
.venv/bin/python -c "import fastapi, sqlalchemy, alembic; print('deps ok')"
```

预期：输出 `deps ok`。若本机没有 `uv`，改用 `.venv/bin/pip install -e backend` 加上 `pytest httpx2`。

- [x] **Step 8: 验证包可导入**

```bash
cd backend && PYTHONPATH=. ../.venv/bin/python -c "import data.config; print(data.config.settings.storage_root)" && cd ..
```

预期：打印以 `/backend/runtime/storage` 结尾的绝对路径。此时还不能跑测试——迁移链要等 Task 2 压平。

- [x] **Step 9: 提交**

```bash
git add -A backend/ .gitignore
git status --short | head -20
git commit -m "feat: import quicdata backend as the data package"
```

提交前看一眼 `git status`，确认没有把 `.venv/`、`backend/vendor/qrdf/` 源码或 `backend/runtime/` 带进来。

---

### Task 2: 把迁移历史压成单个 baseline

**Files:**
- Delete: `backend/alembic/versions/0001_*.py` 至 `0043_*.py`（43 个文件）
- Create: `backend/alembic/versions/0001_baseline.py`
- Test: `backend/tests/test_baseline_migration.py`

**Interfaces:**
- Consumes: Task 1 的 `data.database.Base` 全量 metadata
- Produces: 迁移链头 `0001_baseline`；后续任务的 `down_revision` 起点

新库没有存量数据要兼容，43 个历史迁移里大量是针对 quicdata 生产数据的修复脚本，搬过来只是噪音。baseline 用 autogenerate 从完整 metadata 生成，保证 schema 与源仓 head 等价。

- [x] **Step 1: 记录源仓 head schema 作为比对基准**

```bash
cd /Users/qingmuhy/Documents/devlop/quicrobot/quicdata
TEST_DATABASE_URL=postgresql+psycopg://quicdata:quicdata@127.0.0.1:5432/quicdata_test_ref \
  DATABASE_URL=postgresql+psycopg://quicdata:quicdata@127.0.0.1:5432/quicdata_test_ref \
  .venv/bin/python -c "
from alembic import command
from alembic.config import Config
cfg = Config('alembic.ini')
cfg.set_main_option('sqlalchemy.url', 'postgresql+psycopg://quicdata:quicdata@127.0.0.1:5432/quicdata_test_ref')
command.upgrade(cfg, 'head')
print('ref schema built')
"
pg_dump --schema-only --no-owner --no-privileges \
  "postgresql://quicdata:quicdata@127.0.0.1:5432/quicdata_test_ref" \
  | grep -E "^CREATE TABLE|^CREATE.*INDEX" | sort > /tmp/quicdata_ref_schema.txt
wc -l /tmp/quicdata_ref_schema.txt
cd /Users/qingmuhy/Documents/devlop/quicrobot/quic_studio
```

预期：打印 `ref schema built`，`/tmp/quicdata_ref_schema.txt` 行数非零。数据库 `quicdata_test_ref` 需事先存在（`createdb quicdata_test_ref`）。

- [x] **Step 2: 清空旧迁移**

```bash
cd /Users/qingmuhy/Documents/devlop/quicrobot/quic_studio
rm -f backend/alembic/versions/*.py
ls backend/alembic/versions/
```

预期：目录为空（`__pycache__` 若在，一并删）。

- [x] **Step 3: 生成 baseline**

```bash
createdb quicstudio_baseline_gen 2>/dev/null || true
cd backend
PYTHONPATH=. DATABASE_URL=postgresql+psycopg://quicdata:quicdata@127.0.0.1:5432/quicstudio_baseline_gen \
  ../.venv/bin/python -m alembic revision --autogenerate -m "baseline"
ls alembic/versions/
cd ..
```

预期：`alembic/versions/` 下出现一个新文件。把它重命名为 `0001_baseline.py`，并把文件内的 `revision` 改为 `"0001_baseline"`、`down_revision` 改为 `None`：

```python
revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None
```

autogenerate 不会产出函数式唯一索引（`lower(btrim(...))` 那几个）。在 `upgrade()` 末尾补上源仓里的四个：

```python
op.execute("CREATE UNIQUE INDEX uq_workspaces_normalized_name ON workspaces (lower(btrim(name)))")
op.execute(
    "CREATE UNIQUE INDEX uq_task_sets_workspace_normalized_name "
    "ON task_sets (workspace_id, lower(btrim(name)))"
)
op.execute(
    "CREATE UNIQUE INDEX uq_collection_devices_workspace_normalized_serial "
    "ON collection_devices (workspace_id, upper(btrim(serial_number)))"
)
```

对照源仓 `backend/app/database.py` 里所有模块级 `Index(..., unique=True)` 调用逐个核对，漏一个就少一条约束。核对命令：

```bash
grep -n "^Index(" -A 6 /Users/qingmuhy/Documents/devlop/quicrobot/quicdata/backend/app/database.py
```

- [x] **Step 4: 写失败的测试**

`backend/tests/test_baseline_migration.py`：

```python
"""baseline 迁移必须建出与源仓 head 等价的 schema。"""

from sqlalchemy import inspect

from data.database import Base, engine


def test_baseline_creates_every_mapped_table():
    """所有 ORM 映射的表都必须在迁移后的库里存在。"""
    existing = set(inspect(engine).get_table_names())
    mapped = set(Base.metadata.tables)
    assert mapped - existing == set()


def test_functional_unique_indexes_exist():
    """autogenerate 不产出函数式索引，必须手工补齐。"""
    inspector = inspect(engine)
    workspace_indexes = {index["name"] for index in inspector.get_indexes("workspaces")}
    assert "uq_workspaces_normalized_name" in workspace_indexes
    task_set_indexes = {index["name"] for index in inspector.get_indexes("task_sets")}
    assert "uq_task_sets_workspace_normalized_name" in task_set_indexes
    device_indexes = {index["name"] for index in inspector.get_indexes("collection_devices")}
    assert "uq_collection_devices_workspace_normalized_serial" in device_indexes
```

- [x] **Step 5: 跑测试**

```bash
createdb quicdata_test_studio 2>/dev/null || true
TEST_DATABASE_URL=postgresql+psycopg://quicdata:quicdata@127.0.0.1:5432/quicdata_test_studio .venv/bin/python -m pytest backend/tests/test_baseline_migration.py -q
```

预期：2 passed。失败说明 baseline 漏表或漏索引，回到 Step 3 补齐。

- [x] **Step 6: 跑全量测试建立回归基线**

```bash
TEST_DATABASE_URL=postgresql+psycopg://quicdata:quicdata@127.0.0.1:5432/quicdata_test_studio .venv/bin/python -m pytest backend/tests -q 2>&1 | tail -15
```

记录通过数与失败清单。部分测试可能因为源仓特有的 fixture 路径或历史数据假设而失败——逐个判断：路径类失败就修路径；依赖 quicdata 生产数据形态的测试，在测试文件里加 `pytest.mark.skip` 并注明原因，不要删。把最终通过数写进提交信息，它是后续任务的回归基线。

- [x] **Step 7: 提交**

```bash
git add backend/alembic/versions/ backend/tests/
git commit -m "refactor: squash inherited migrations into a single baseline"
```

---

### Task 3: 迁移部署与开发脚本

**Files:**
- Create: `deploy/**`、`scripts/**`、`Makefile`、`alembic.ini`（源自 quicdata 同名文件）
- Modify: `backend/scripts/validate_mcap_pipeline.py`（Task 1 已复制进来）
- Modify: 上述新建文件里的模块路径与存储路径

**Interfaces:**
- Consumes: Task 1 的 `data.main:app`、`data.celery_app:celery_app`
- Produces: 可启动的部署配置（后续任务不依赖）

- [x] **Step 1: 复制**

```bash
cd /Users/qingmuhy/Documents/devlop/quicrobot/quic_studio
SRC=/Users/qingmuhy/Documents/devlop/quicrobot/quicdata
rsync -a --exclude 'runtime/' --exclude 'secrets/' --exclude '.env' "$SRC/deploy/" deploy/
rsync -a "$SRC/scripts/" scripts/
cp "$SRC/Makefile" "$SRC/alembic.ini" .
mkdir -p deploy/runtime deploy/secrets
touch deploy/runtime/.gitignore deploy/secrets/.gitignore
ls deploy/ scripts/
```

- [x] **Step 2: 改写模块路径**

```bash
sed -i '' 's|app\.main:app|data.main:app|g; s|app\.celery_app:celery_app|data.celery_app:celery_app|g' \
  deploy/docker-compose.prod.yml scripts/start.sh scripts/start-worker.sh scripts/start-worker.ps1 \
  scripts/dev-screen-service.sh scripts/reset-data.sh
grep -rn "app\.main:app\|app\.celery_app" deploy/ scripts/ | grep -v "data\.main\|data\.celery_app"
```

预期：第二条命令无输出。

- [x] **Step 3: 改写存储路径**

`scripts/setup.sh` 里创建存储目录的那行：

```bash
mkdir -p "$ROOT/backend/runtime/storage"
```

`scripts/deploy-ubuntu.sh` 里的默认存储根：

```bash
STORAGE_ROOT=./runtime/storage
```

`backend/scripts/validate_mcap_pipeline.py` 的临时输出目录（`BACKEND_ROOT` 指向 `backend/`）：

```python
        BACKEND_ROOT / "runtime" / "storage" / "hot" / f"pipeline_{mcap_path.stem}_{stamp}"
```

`backend/.env.example` 的 `STORAGE_ROOT` 保持指向 `../deploy/runtime/storage`，不改。

- [x] **Step 4: 改写仓库标识**

quicdata 的脚本里有若干处硬编码仓库名或镜像名。逐一检查并改为 quic_studio 对应值：

```bash
grep -rn "quicdata" Makefile alembic.ini deploy/ scripts/ | grep -v "quicdata:quicdata\|quicdata_test" | head -20
```

`quicdata:quicdata` 是数据库用户名密码、`quicdata_test*` 是测试库名，两者保持不变。其余出现（镜像名、容器名、服务名、仓库 URL）改成 `quicstudio`。改完复跑上面的 grep 确认只剩这两类。

`alembic.ini` 的 `script_location` 保持 `%(here)s/backend/alembic` 不变。

- [x] **Step 5: 语法自检**

```bash
for f in scripts/*.sh; do bash -n "$f" || echo "FAIL $f"; done
.venv/bin/python -c "import yaml; yaml.safe_load(open('deploy/docker-compose.prod.yml')); print('compose ok')"
```

预期：无 `FAIL` 行，输出 `compose ok`。

- [x] **Step 6: 验证迁移命令可跑**

```bash
PYTHONPATH=backend DATABASE_URL=postgresql+psycopg://quicdata:quicdata@127.0.0.1:5432/quicdata_test_studio \
  .venv/bin/python -m scripts.migrate 2>&1 | tail -5
```

预期：迁移执行成功或报告已是最新。若 `scripts.migrate` 引用了源仓特有路径，按报错修正。

- [x] **Step 7: 提交**

```bash
git add deploy/ scripts/ Makefile alembic.ini backend/scripts/validate_mcap_pipeline.py
git commit -m "chore: migrate deploy and dev scripts for quic_studio"
```

---

### Task 4: 建立 `backend/train/` 占位包

**Files:**
- Create: `backend/train/__init__.py`
- Create: `backend/train/README.md`
- Modify: `backend/pyproject.toml`

**Interfaces:**
- Consumes: 无
- Produces: 可导入的 `train` 包（同事后续在此开发训练流程；本计划后续任务不依赖）

- [x] **Step 1: 建包**

`backend/train/__init__.py`：

```python
"""训练流程后端包，与 data 包平行，独立演进。"""
```

`backend/train/README.md`：

```markdown
# train

训练流程后端。与 `backend/data/`（数据平台后端）平行，通过数据集版本与导出件交互。

跨包交互一律走 `data` 暴露的 API 或数据库中已发布的数据集版本，不要 `from data.services...` 直接引用实现细节。
```

- [x] **Step 2: 纳入打包范围**

`backend/pyproject.toml`：

```toml
[tool.setuptools.packages.find]
where = ["."]
include = ["data*", "train*", "scripts*"]
```

- [x] **Step 3: 验证可导入**

```bash
cd backend && PYTHONPATH=. ../.venv/bin/python -c "import train; print(train.__doc__)" && cd ..
```

预期：打印包的 docstring。

- [x] **Step 4: 提交**

```bash
git add backend/train/ backend/pyproject.toml
git commit -m "chore: add train package placeholder alongside data"
```

---

### Task 5: 采集标签字典与设备型号目录

**Files:**
- Create: `backend/data/models/__init__.py`
- Create: `backend/data/models/collection_config.py`
- Create: `backend/alembic/versions/0002_collection_config_dictionary.py`
- Modify: `backend/data/database.py`（文件末尾注册模型模块）
- Test: `backend/tests/test_collection_config_models.py`

**Interfaces:**
- Consumes: Task 1 的 `data.database.Base`、`data.database.JsonDocument`
- Produces:
  - `data.models.collection_config.CollectionLabel`，表 `collection_labels`，字段 `id, category, name, description, is_active, created_at, updated_at`
  - `data.models.collection_config.COLLECTION_LABEL_CATEGORIES: tuple[str, ...]` = `("scene", "purpose", "training", "project", "modality")`
  - `data.models.collection_config.CollectionDeviceModel`，表 `collection_device_models`，字段 `id, vendor, model, device_type, modalities_json, is_active, created_at`

规格要求标签字典至少五类且「已被使用的标签只能停用，不能物理删除」，所以用 `is_active` 而非删除。不复用遗留的 `task_labels` 表：那是动作词表，与采集业务标签语义无关。

- [x] **Step 1: 写失败的测试**

`backend/tests/test_collection_config_models.py`：

```python
"""采集配置域模型：标签字典与设备型号目录。"""

import pytest
from sqlalchemy.exc import IntegrityError

from data.database import SessionLocal
from data.models.collection_config import (
    COLLECTION_LABEL_CATEGORIES,
    CollectionDeviceModel,
    CollectionLabel,
)


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def test_label_categories_cover_the_five_required_kinds():
    assert set(COLLECTION_LABEL_CATEGORIES) == {
        "scene",
        "purpose",
        "training",
        "project",
        "modality",
    }


def test_label_rejects_unknown_category(db):
    db.add(CollectionLabel(category="region", name="华东"))
    with pytest.raises(IntegrityError):
        db.commit()


def test_label_name_is_unique_per_category_ignoring_padding(db):
    db.add(CollectionLabel(category="scene", name="厨房"))
    db.commit()
    db.add(CollectionLabel(category="scene", name="  厨房  "))
    with pytest.raises(IntegrityError):
        db.commit()


def test_same_name_allowed_in_a_different_category(db):
    db.add(CollectionLabel(category="scene", name="通用"))
    db.add(CollectionLabel(category="purpose", name="通用"))
    db.commit()
    assert db.query(CollectionLabel).filter_by(name="通用").count() == 2


def test_label_defaults_to_active(db):
    label = CollectionLabel(category="training", name="预训练")
    db.add(label)
    db.commit()
    assert label.is_active is True


def test_device_model_vendor_model_pair_is_unique(db):
    db.add(CollectionDeviceModel(vendor="Quic", model="EGO-1", device_type="iphone"))
    db.commit()
    db.add(CollectionDeviceModel(vendor="quic", model=" ego-1 ", device_type="iphone"))
    with pytest.raises(IntegrityError):
        db.commit()


def test_device_model_modalities_default_to_empty_list(db):
    entry = CollectionDeviceModel(vendor="Quic", model="UMI-1", device_type="iphone")
    db.add(entry)
    db.commit()
    assert entry.modalities_json == []
```

- [x] **Step 2: 跑测试确认失败**

```bash
TEST_DATABASE_URL=postgresql+psycopg://quicdata:quicdata@127.0.0.1:5432/quicdata_test_studio .venv/bin/python -m pytest backend/tests/test_collection_config_models.py -q
```

预期：collection error，`ModuleNotFoundError: No module named 'data.models'`。

- [x] **Step 3: 建模型子包**

`backend/data/models/__init__.py`：

```python
"""QuicStudio 采集域模型。

每个模块聚焦一个子域；`data.database` 在文件末尾统一导入本包，使
`Base.metadata` 与 mapper 的字符串引用都能解析到这些类。
"""
```

`backend/data/models/collection_config.py`：

```python
"""数采配置域：业务标签字典与设备型号目录。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    func,
)

from data.database import Base, JsonDocument

COLLECTION_LABEL_CATEGORIES = ("scene", "purpose", "training", "project", "modality")


class CollectionLabel(Base):
    """数采配置维护的业务标签。

    与遗留的 ``TaskLabel``（动作词表）语义无关，刻意不复用同一张表。
    已被引用的标签只允许停用，因此没有删除路径，只有 ``is_active``。
    """

    __tablename__ = "collection_labels"
    __table_args__ = (
        CheckConstraint(
            "category IN ('scene', 'purpose', 'training', 'project', 'modality')",
            name="ck_collection_labels_category",
        ),
        Index("ix_collection_labels_category_active", "category", "is_active"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    category = Column(String(32), nullable=False)
    name = Column(String(128), nullable=False)
    description = Column(Text, nullable=False, default="")
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


Index(
    "uq_collection_labels_category_normalized_name",
    CollectionLabel.category,
    func.lower(func.btrim(CollectionLabel.name)),
    unique=True,
)


class CollectionDeviceModel(Base):
    """采集设备型号目录。

    本期由预置数据初始化并在后台维护，前端新建入口置灰。设备实例
    (``CollectionDevice``) 仍按 SN 单独登记，型号目录只规范可选项。
    """

    __tablename__ = "collection_device_models"

    id = Column(Integer, primary_key=True, autoincrement=True)
    vendor = Column(String(128), nullable=False)
    model = Column(String(128), nullable=False)
    device_type = Column(String(64), nullable=False)
    modalities_json = Column(JsonDocument, nullable=False, default=list)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


Index(
    "uq_collection_device_models_vendor_model",
    func.lower(func.btrim(CollectionDeviceModel.vendor)),
    func.lower(func.btrim(CollectionDeviceModel.model)),
    unique=True,
)
```

- [x] **Step 4: 在 `database.py` 末尾注册模型模块**

追加到 `backend/data/database.py` 文件最末尾（`Base` 已在文件上方定义，此处导入不会循环失败）：

```python
# 在文件末尾导入采集域模型，让 Base.metadata 与 mapper 字符串引用都能看到
# 它们，调用方无需各自 import。放在末尾是因为这些模块反向依赖上面的 Base。
from data.models import collection_config  # noqa: E402,F401
```

- [x] **Step 5: 写迁移**

`backend/alembic/versions/0002_collection_config_dictionary.py`：

```python
"""采集配置域：业务标签字典与设备型号目录。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002_collection_config_dictionary"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "collection_labels",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "category IN ('scene', 'purpose', 'training', 'project', 'modality')",
            name="ck_collection_labels_category",
        ),
    )
    op.create_index(
        "ix_collection_labels_category_active", "collection_labels", ["category", "is_active"]
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_collection_labels_category_normalized_name "
        "ON collection_labels (category, lower(btrim(name)))"
    )

    op.create_table(
        "collection_device_models",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("vendor", sa.String(length=128), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("device_type", sa.String(length=64), nullable=False),
        sa.Column("modalities_json", JSONB(), nullable=False, server_default="[]"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_collection_device_models_vendor_model "
        "ON collection_device_models (lower(btrim(vendor)), lower(btrim(model)))"
    )


def downgrade() -> None:
    op.drop_table("collection_device_models")
    op.drop_index("ix_collection_labels_category_active", table_name="collection_labels")
    op.drop_table("collection_labels")
```

- [x] **Step 6: 跑测试确认通过**

```bash
TEST_DATABASE_URL=postgresql+psycopg://quicdata:quicdata@127.0.0.1:5432/quicdata_test_studio .venv/bin/python -m pytest backend/tests/test_collection_config_models.py -q
```

预期：7 passed。

- [x] **Step 7: 验证迁移可回滚**

```bash
cd backend && export DATABASE_URL=postgresql+psycopg://quicdata:quicdata@127.0.0.1:5432/quicdata_test_studio && \
  ../.venv/bin/python -m alembic downgrade 0001_baseline && \
  ../.venv/bin/python -m alembic upgrade head && cd ..
```

预期：降级与升级都成功。

- [x] **Step 8: 提交**

```bash
git add backend/data/models/ backend/data/database.py backend/alembic/versions/0002_collection_config_dictionary.py backend/tests/test_collection_config_models.py
git commit -m "feat: add collection label dictionary and device model catalog"
```

---

### Task 6: 采集项目与采集任务

**Files:**
- Create: `backend/data/models/collection_core.py`
- Create: `backend/alembic/versions/0003_collection_project_and_task.py`
- Modify: `backend/data/database.py`（末尾注册行追加模块）
- Test: `backend/tests/test_collection_core_models.py`

**Interfaces:**
- Consumes: Task 5 的 `CollectionLabel`、`CollectionDeviceModel`；既有 `Workspace`、`User`
- Produces:
  - `data.models.collection_core.CollectionProject`，表 `collection_projects`，字段 `id, workspace_id, name, description, owner_user_id, status, created_at, updated_at`；`status` ∈ `enabled|archived`
  - `data.models.collection_core.CollectionTask`，表 `collection_tasks`，字段 `id, workspace_id, collection_project_id, name, description, target_duration_hours, default_package_duration_hours, capture_mode, sop_text, device_model_id, created_by_user_id, created_at, updated_at`
  - `data.models.collection_core.CollectionTaskLabel`，表 `collection_task_labels`，复合主键 `(collection_task_id, collection_label_id)`

五类标签都可多选，用一张关联表统一承载而不是五个数组列——标签停用时能直接查出引用方。任务不设地区与人员字段（规格明确排除）；创建人只作审计。

- [x] **Step 1: 写失败的测试**

`backend/tests/test_collection_core_models.py`：

```python
"""采集项目与采集任务模型。"""

import pytest
from sqlalchemy.exc import IntegrityError

from data.database import SessionLocal, Workspace
from data.models.collection_config import CollectionDeviceModel, CollectionLabel
from data.models.collection_core import CollectionProject, CollectionTask, CollectionTaskLabel


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def workspace(db):
    entry = Workspace(name=f"ws-core-{id(db)}")
    db.add(entry)
    db.commit()
    return entry


def test_project_defaults_to_enabled(db, workspace):
    project = CollectionProject(workspace_id=workspace.id, name="厨房采集")
    db.add(project)
    db.commit()
    assert project.status == "enabled"


def test_project_rejects_unknown_status(db, workspace):
    db.add(CollectionProject(workspace_id=workspace.id, name="X", status="paused"))
    with pytest.raises(IntegrityError):
        db.commit()


def test_project_name_is_unique_within_workspace_ignoring_padding(db, workspace):
    db.add(CollectionProject(workspace_id=workspace.id, name="厨房"))
    db.commit()
    db.add(CollectionProject(workspace_id=workspace.id, name="  厨房 "))
    with pytest.raises(IntegrityError):
        db.commit()


def test_task_capture_mode_is_offline_only(db, workspace):
    project = CollectionProject(workspace_id=workspace.id, name="P1")
    db.add(project)
    db.commit()
    db.add(
        CollectionTask(
            workspace_id=workspace.id,
            collection_project_id=project.id,
            name="T1",
            target_duration_hours=10,
            capture_mode="online",
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()


def test_task_defaults(db, workspace):
    project = CollectionProject(workspace_id=workspace.id, name="P2")
    db.add(project)
    db.commit()
    task = CollectionTask(
        workspace_id=workspace.id,
        collection_project_id=project.id,
        name="T2",
        target_duration_hours=10,
    )
    db.add(task)
    db.commit()
    assert task.capture_mode == "offline"
    assert float(task.default_package_duration_hours) == 2.0


def test_task_target_duration_must_be_positive(db, workspace):
    project = CollectionProject(workspace_id=workspace.id, name="P3")
    db.add(project)
    db.commit()
    db.add(
        CollectionTask(
            workspace_id=workspace.id,
            collection_project_id=project.id,
            name="T3",
            target_duration_hours=0,
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()


def test_task_accepts_multiple_labels_across_categories(db, workspace):
    project = CollectionProject(workspace_id=workspace.id, name="P4")
    db.add(project)
    db.commit()
    task = CollectionTask(
        workspace_id=workspace.id,
        collection_project_id=project.id,
        name="T4",
        target_duration_hours=4,
    )
    scene = CollectionLabel(category="scene", name=f"客厅-{workspace.id}")
    purpose = CollectionLabel(category="purpose", name=f"正式任务-{workspace.id}")
    db.add_all([task, scene, purpose])
    db.commit()
    db.add_all(
        [
            CollectionTaskLabel(collection_task_id=task.id, collection_label_id=scene.id),
            CollectionTaskLabel(collection_task_id=task.id, collection_label_id=purpose.id),
        ]
    )
    db.commit()
    assert db.query(CollectionTaskLabel).filter_by(collection_task_id=task.id).count() == 2


def test_task_label_pair_cannot_repeat(db, workspace):
    project = CollectionProject(workspace_id=workspace.id, name="P5")
    db.add(project)
    db.commit()
    task = CollectionTask(
        workspace_id=workspace.id,
        collection_project_id=project.id,
        name="T5",
        target_duration_hours=4,
    )
    label = CollectionLabel(category="training", name=f"后训练-{workspace.id}")
    db.add_all([task, label])
    db.commit()
    db.add(CollectionTaskLabel(collection_task_id=task.id, collection_label_id=label.id))
    db.commit()
    db.add(CollectionTaskLabel(collection_task_id=task.id, collection_label_id=label.id))
    with pytest.raises(IntegrityError):
        db.commit()


def test_task_binds_one_device_model(db, workspace):
    project = CollectionProject(workspace_id=workspace.id, name="P6")
    device_model = CollectionDeviceModel(
        vendor=f"V{workspace.id}", model="M1", device_type="iphone"
    )
    db.add_all([project, device_model])
    db.commit()
    task = CollectionTask(
        workspace_id=workspace.id,
        collection_project_id=project.id,
        name="T6",
        target_duration_hours=4,
        device_model_id=device_model.id,
    )
    db.add(task)
    db.commit()
    assert task.device_model.model == "M1"
```

- [x] **Step 2: 跑测试确认失败**

```bash
TEST_DATABASE_URL=postgresql+psycopg://quicdata:quicdata@127.0.0.1:5432/quicdata_test_studio .venv/bin/python -m pytest backend/tests/test_collection_core_models.py -q
```

预期：collection error，`ImportError: cannot import name 'CollectionProject'`。

- [x] **Step 3: 写模型**

`backend/data/models/collection_core.py`：

```python
"""采集域核心：采集项目与采集任务。"""

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
    """采集项目。归档后保留历史查询能力，但禁止新增任务、数据包与上传。"""

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
    """采集任务。

    目标时长是唯一的任务目标；数据包数量由目标时长除以单包时长推导，
    不提供任务级数量输入。任务不承载地区与人员字段：人员在数据包分配
    环节指定，``created_by_user_id`` 仅作审计。
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


class CollectionTaskLabel(Base):
    """任务与业务标签的多对多关联。

    五类标签都可多选，统一走这张表而不是五个数组列，这样标签停用时
    能直接查出引用方。
    """

    __tablename__ = "collection_task_labels"

    collection_task_id = Column(Integer, ForeignKey("collection_tasks.id"), primary_key=True)
    collection_label_id = Column(Integer, ForeignKey("collection_labels.id"), primary_key=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    task = relationship("CollectionTask", back_populates="labels")
    label = relationship("CollectionLabel")
```

- [x] **Step 4: 注册模块**

把 `backend/data/database.py` 末尾的注册行改为：

```python
from data.models import collection_config, collection_core  # noqa: E402,F401
```

- [x] **Step 5: 写迁移**

`backend/alembic/versions/0003_collection_project_and_task.py`：

```python
"""采集项目、采集任务与任务标签关联。"""

import sqlalchemy as sa
from alembic import op

revision = "0003_collection_project_and_task"
down_revision = "0002_collection_config_dictionary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "collection_projects",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("owner_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="enabled"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('enabled', 'archived')", name="ck_collection_projects_status"
        ),
    )
    op.create_index(
        "ix_collection_projects_workspace_status", "collection_projects", ["workspace_id", "status"]
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_collection_projects_workspace_normalized_name "
        "ON collection_projects (workspace_id, lower(btrim(name)))"
    )

    op.create_table(
        "collection_tasks",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column(
            "collection_project_id",
            sa.Integer(),
            sa.ForeignKey("collection_projects.id"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("target_duration_hours", sa.Numeric(10, 2), nullable=False),
        sa.Column(
            "default_package_duration_hours", sa.Numeric(10, 2), nullable=False, server_default="2"
        ),
        sa.Column("capture_mode", sa.String(length=16), nullable=False, server_default="offline"),
        sa.Column("sop_text", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "device_model_id",
            sa.Integer(),
            sa.ForeignKey("collection_device_models.id"),
            nullable=True,
        ),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("capture_mode = 'offline'", name="ck_collection_tasks_capture_mode"),
        sa.CheckConstraint("target_duration_hours > 0", name="ck_collection_tasks_target_duration"),
        sa.CheckConstraint(
            "default_package_duration_hours > 0",
            name="ck_collection_tasks_default_package_duration",
        ),
    )
    op.create_index(
        "ix_collection_tasks_project_created",
        "collection_tasks",
        ["collection_project_id", "created_at"],
    )

    op.create_table(
        "collection_task_labels",
        sa.Column(
            "collection_task_id",
            sa.Integer(),
            sa.ForeignKey("collection_tasks.id"),
            primary_key=True,
        ),
        sa.Column(
            "collection_label_id",
            sa.Integer(),
            sa.ForeignKey("collection_labels.id"),
            primary_key=True,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("collection_task_labels")
    op.drop_index("ix_collection_tasks_project_created", table_name="collection_tasks")
    op.drop_table("collection_tasks")
    op.drop_index("ix_collection_projects_workspace_status", table_name="collection_projects")
    op.drop_table("collection_projects")
```

- [x] **Step 6: 跑测试确认通过**

```bash
TEST_DATABASE_URL=postgresql+psycopg://quicdata:quicdata@127.0.0.1:5432/quicdata_test_studio .venv/bin/python -m pytest backend/tests/test_collection_core_models.py -q
```

预期：9 passed。

- [x] **Step 7: 提交**

```bash
git add backend/data/models/collection_core.py backend/data/database.py backend/alembic/versions/0003_collection_project_and_task.py backend/tests/test_collection_core_models.py
git commit -m "feat: add collection project and task models"
```

---

### Task 7: 数据包

**Files:**
- Create: `backend/data/models/data_package.py`
- Create: `backend/alembic/versions/0004_data_package.py`
- Modify: `backend/data/database.py`（末尾注册行追加模块）
- Test: `backend/tests/test_data_package_models.py`

**Interfaces:**
- Consumes: Task 6 的 `CollectionProject`、`CollectionTask`；既有 `PersonnelProfile`、`CollectionDevice`、`Workspace`
- Produces:
  - `data.models.data_package.DataPackage`，表 `data_packages`
  - `data.models.data_package.DATA_PACKAGE_STATUSES: tuple[str, ...]`
  - `data.models.data_package.DATA_PACKAGE_TERMINAL_STATUSES: frozenset[str]` = `{"parse_failed", "voided"}`

状态用规格状态的英文化取值。双有效时长用 `intake_valid_duration_hours` 与 `governed_valid_duration_hours`，前者入库审核后不变。改派由服务层拒绝，数据库侧保证已分配的包两个人员引用都在。

- [x] **Step 1: 写失败的测试**

`backend/tests/test_data_package_models.py`：

```python
"""数据包模型：状态取值、分配约束与双有效时长。"""

import pytest
from sqlalchemy.exc import IntegrityError

from data.database import PersonnelProfile, SessionLocal, Workspace
from data.models.collection_core import CollectionProject, CollectionTask
from data.models.data_package import DATA_PACKAGE_TERMINAL_STATUSES, DataPackage


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def task(db):
    workspace = Workspace(name=f"ws-pkg-{id(db)}")
    db.add(workspace)
    db.commit()
    project = CollectionProject(workspace_id=workspace.id, name=f"P-{workspace.id}")
    db.add(project)
    db.commit()
    entry = CollectionTask(
        workspace_id=workspace.id,
        collection_project_id=project.id,
        name=f"T-{workspace.id}",
        target_duration_hours=8,
    )
    db.add(entry)
    db.commit()
    return entry


def _collector(db, key: str) -> PersonnelProfile:
    profile = PersonnelProfile(name=f"c{key}", profile_key=key)
    db.add(profile)
    db.commit()
    return profile


def _package(task, suffix: str, **overrides) -> DataPackage:
    fields = {
        "workspace_id": task.workspace_id,
        "collection_project_id": task.collection_project_id,
        "collection_task_id": task.id,
        "package_uid": f"pkg-{task.id}-{suffix}",
        "target_duration_hours": 2,
    }
    fields.update(overrides)
    return DataPackage(**fields)


def test_terminal_statuses_are_parse_failed_and_voided():
    assert DATA_PACKAGE_TERMINAL_STATUSES == frozenset({"parse_failed", "voided"})


def test_package_starts_pending_assignment(db, task):
    package = _package(task, "1")
    db.add(package)
    db.commit()
    assert package.status == "pending_assignment"
    assert package.intake_valid_duration_hours is None
    assert package.governed_valid_duration_hours is None


def test_package_rejects_unknown_status(db, task):
    db.add(_package(task, "2", status="reassigned"))
    with pytest.raises(IntegrityError):
        db.commit()


def test_package_uid_is_globally_unique(db, task):
    db.add(_package(task, "dup"))
    db.commit()
    db.add(_package(task, "dup"))
    with pytest.raises(IntegrityError):
        db.commit()


def test_assigned_package_requires_both_collectors(db, task):
    owner = _collector(db, f"{task.id}01")
    db.add(_package(task, "3", status="assigned", responsible_collector_id=owner.id))
    with pytest.raises(IntegrityError):
        db.commit()


def test_assignment_with_both_collectors_succeeds(db, task):
    owner = _collector(db, f"{task.id}02")
    operator = _collector(db, f"{task.id}03")
    package = _package(
        task,
        "4",
        status="assigned",
        responsible_collector_id=owner.id,
        operator_collector_id=operator.id,
    )
    db.add(package)
    db.commit()
    assert package.responsible_collector.id == owner.id
    assert package.operator_collector.id == operator.id


def test_valid_durations_cannot_be_negative(db, task):
    db.add(_package(task, "5", intake_valid_duration_hours=-1))
    with pytest.raises(IntegrityError):
        db.commit()


def test_governed_duration_cannot_exceed_intake_duration(db, task):
    db.add(_package(task, "6", intake_valid_duration_hours=2, governed_valid_duration_hours=3))
    with pytest.raises(IntegrityError):
        db.commit()
```

- [x] **Step 2: 跑测试确认失败**

```bash
TEST_DATABASE_URL=postgresql+psycopg://quicdata:quicdata@127.0.0.1:5432/quicdata_test_studio .venv/bin/python -m pytest backend/tests/test_data_package_models.py -q
```

预期：collection error，`ModuleNotFoundError: No module named 'data.models.data_package'`。

- [x] **Step 3: 写模型**

`backend/data/models/data_package.py`：

```python
"""数据包：采集、接入、入库审核与治理的业务主体。"""

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
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from data.database import Base

DATA_PACKAGE_STATUSES = (
    "pending_assignment",
    "assigned",
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

_STATUS_SQL_LIST = ", ".join(f"'{status}'" for status in DATA_PACKAGE_STATUSES)


class DataPackage(Base):
    """一次采集分配的最小业务对象。

    分配一经落定即锁定：改派由服务层拒绝，数据库侧只保证已分配的包两个
    人员引用都在。两个有效时长分别服务看板与资产：``intake_*`` 在入库审核
    完成后不再变化，``governed_*`` 随 QC 结果下调。
    """

    __tablename__ = "data_packages"
    __table_args__ = (
        UniqueConstraint("package_uid", name="uq_data_packages_package_uid"),
        CheckConstraint(f"status IN ({_STATUS_SQL_LIST})", name="ck_data_packages_status"),
        CheckConstraint("target_duration_hours > 0", name="ck_data_packages_target_duration"),
        CheckConstraint(
            "intake_valid_duration_hours IS NULL OR intake_valid_duration_hours >= 0",
            name="ck_data_packages_intake_duration_non_negative",
        ),
        CheckConstraint(
            "governed_valid_duration_hours IS NULL OR governed_valid_duration_hours >= 0",
            name="ck_data_packages_governed_duration_non_negative",
        ),
        CheckConstraint(
            "governed_valid_duration_hours IS NULL "
            "OR intake_valid_duration_hours IS NULL "
            "OR governed_valid_duration_hours <= intake_valid_duration_hours",
            name="ck_data_packages_governed_within_intake",
        ),
        CheckConstraint(
            "status = 'pending_assignment' "
            "OR (responsible_collector_id IS NOT NULL AND operator_collector_id IS NOT NULL)",
            name="ck_data_packages_assigned_requires_collectors",
        ),
        Index("ix_data_packages_task_status", "collection_task_id", "status"),
        Index("ix_data_packages_workspace_status", "workspace_id", "status"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    package_uid = Column(String(64), nullable=False)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    collection_project_id = Column(Integer, ForeignKey("collection_projects.id"), nullable=False)
    collection_task_id = Column(Integer, ForeignKey("collection_tasks.id"), nullable=False)
    status = Column(String(32), nullable=False, default="pending_assignment")
    target_duration_hours = Column(Numeric(10, 2), nullable=False)
    captured_duration_hours = Column(Numeric(10, 2), nullable=True)
    intake_valid_duration_hours = Column(Numeric(10, 2), nullable=True)
    governed_valid_duration_hours = Column(Numeric(10, 2), nullable=True)
    responsible_collector_id = Column(Integer, ForeignKey("personnel_profiles.id"), nullable=True)
    operator_collector_id = Column(Integer, ForeignKey("personnel_profiles.id"), nullable=True)
    collection_device_id = Column(Integer, ForeignKey("collection_devices.id"), nullable=True)
    assigned_at = Column(DateTime, nullable=True)
    upload_completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    task = relationship("CollectionTask")
    project = relationship("CollectionProject")
    responsible_collector = relationship(
        "PersonnelProfile", foreign_keys=[responsible_collector_id]
    )
    operator_collector = relationship("PersonnelProfile", foreign_keys=[operator_collector_id])
    device = relationship("CollectionDevice")
```

导入块里只列本任务真正用到的名字。`Boolean`、`Text`、`JsonDocument` 要等 Task 8 追加 `PackageIntakeReview` 时才有消费方，现在导入会被 ruff 判为 F401 未使用导入，所以本任务不要写它们；Task 8 的 Step 4 会给出补全后的完整导入块。

- [x] **Step 4: 注册模块**

`backend/data/database.py` 末尾注册行改为：

```python
from data.models import collection_config, collection_core, data_package  # noqa: E402,F401
```

- [x] **Step 5: 写迁移**

`backend/alembic/versions/0004_data_package.py`：

```python
"""数据包：状态取值、双有效时长与分配约束。"""

import sqlalchemy as sa
from alembic import op

revision = "0004_data_package"
down_revision = "0003_collection_project_and_task"
branch_labels = None
depends_on = None

# 状态取值在迁移里写死而不是从 data.models 导入：迁移是历史快照，
# 不能随应用代码里的常量一起漂移。
_STATUS_SQL_LIST = (
    "'pending_assignment', 'assigned', 'pending_upload', 'uploading', 'parsing', "
    "'ingested', 'pending_intake_review', 'intake_approved', 'batched', 'governing', "
    "'published', 'parse_failed', 'voided'"
)


def upgrade() -> None:
    op.create_table(
        "data_packages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("package_uid", sa.String(length=64), nullable=False),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column(
            "collection_project_id",
            sa.Integer(),
            sa.ForeignKey("collection_projects.id"),
            nullable=False,
        ),
        sa.Column(
            "collection_task_id",
            sa.Integer(),
            sa.ForeignKey("collection_tasks.id"),
            nullable=False,
        ),
        sa.Column(
            "status", sa.String(length=32), nullable=False, server_default="pending_assignment"
        ),
        sa.Column("target_duration_hours", sa.Numeric(10, 2), nullable=False),
        sa.Column("captured_duration_hours", sa.Numeric(10, 2), nullable=True),
        sa.Column("intake_valid_duration_hours", sa.Numeric(10, 2), nullable=True),
        sa.Column("governed_valid_duration_hours", sa.Numeric(10, 2), nullable=True),
        sa.Column(
            "responsible_collector_id",
            sa.Integer(),
            sa.ForeignKey("personnel_profiles.id"),
            nullable=True,
        ),
        sa.Column(
            "operator_collector_id",
            sa.Integer(),
            sa.ForeignKey("personnel_profiles.id"),
            nullable=True,
        ),
        sa.Column(
            "collection_device_id",
            sa.Integer(),
            sa.ForeignKey("collection_devices.id"),
            nullable=True,
        ),
        sa.Column("assigned_at", sa.DateTime(), nullable=True),
        sa.Column("upload_completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("package_uid", name="uq_data_packages_package_uid"),
        sa.CheckConstraint(f"status IN ({_STATUS_SQL_LIST})", name="ck_data_packages_status"),
        sa.CheckConstraint("target_duration_hours > 0", name="ck_data_packages_target_duration"),
        sa.CheckConstraint(
            "intake_valid_duration_hours IS NULL OR intake_valid_duration_hours >= 0",
            name="ck_data_packages_intake_duration_non_negative",
        ),
        sa.CheckConstraint(
            "governed_valid_duration_hours IS NULL OR governed_valid_duration_hours >= 0",
            name="ck_data_packages_governed_duration_non_negative",
        ),
        sa.CheckConstraint(
            "governed_valid_duration_hours IS NULL "
            "OR intake_valid_duration_hours IS NULL "
            "OR governed_valid_duration_hours <= intake_valid_duration_hours",
            name="ck_data_packages_governed_within_intake",
        ),
        sa.CheckConstraint(
            "status = 'pending_assignment' "
            "OR (responsible_collector_id IS NOT NULL AND operator_collector_id IS NOT NULL)",
            name="ck_data_packages_assigned_requires_collectors",
        ),
    )
    op.create_index(
        "ix_data_packages_task_status", "data_packages", ["collection_task_id", "status"]
    )
    op.create_index(
        "ix_data_packages_workspace_status", "data_packages", ["workspace_id", "status"]
    )


def downgrade() -> None:
    op.drop_index("ix_data_packages_workspace_status", table_name="data_packages")
    op.drop_index("ix_data_packages_task_status", table_name="data_packages")
    op.drop_table("data_packages")
```

- [x] **Step 6: 跑测试确认通过**

```bash
TEST_DATABASE_URL=postgresql+psycopg://quicdata:quicdata@127.0.0.1:5432/quicdata_test_studio .venv/bin/python -m pytest backend/tests/test_data_package_models.py -q
```

预期：8 passed。

- [x] **Step 7: 提交**

```bash
git add backend/data/models/data_package.py backend/data/database.py backend/alembic/versions/0004_data_package.py backend/tests/test_data_package_models.py
git commit -m "feat: add data package model with dual valid-duration fields"
```

---

### Task 8: Episode 有效性字段与入库审核记录

**Files:**
- Modify: `backend/data/models/data_package.py`（追加 `PackageIntakeReview`）
- Modify: `backend/data/database.py`（`Episode` 追加字段与约束）
- Create: `backend/alembic/versions/0005_episode_validity_and_intake_review.py`
- Test: `backend/tests/test_intake_review_models.py`

**Interfaces:**
- Consumes: Task 7 的 `DataPackage`；既有 `Episode`、`User`
- Produces:
  - `Episode.validity_status`（`valid` / `intake_rejected` / `qc_dropped`，默认 `valid`）
  - `Episode.data_package_id`（可空 FK → `data_packages.id`）
  - `data.models.data_package.PackageIntakeReview`，表 `package_intake_reviews`
  - `data.models.data_package.EPISODE_VALIDITY_STATUSES: tuple[str, ...]`

Episode 复用既有表：它已经是接入产物的载体。`validity_status` 与遗留的 `quality_status` / `review_status` 并存，不互相覆盖——前者是规格定义的三态有效性，后两者是遗留的治理与标注审核状态。`data_package_id` 可空，因为遗留 Episode 没有数据包归属。

- [x] **Step 1: 写失败的测试**

`backend/tests/test_intake_review_models.py`：

```python
"""入库审核记录与 Episode 有效性三态。"""

import pytest
from sqlalchemy.exc import IntegrityError

from data.database import Batch, Episode, SessionLocal, TaskSet, Workspace
from data.models.collection_core import CollectionProject, CollectionTask
from data.models.data_package import (
    EPISODE_VALIDITY_STATUSES,
    DataPackage,
    PackageIntakeReview,
)


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def package(db):
    workspace = Workspace(name=f"ws-intake-{id(db)}")
    db.add(workspace)
    db.commit()
    project = CollectionProject(workspace_id=workspace.id, name=f"P-{workspace.id}")
    db.add(project)
    db.commit()
    task = CollectionTask(
        workspace_id=workspace.id,
        collection_project_id=project.id,
        name=f"T-{workspace.id}",
        target_duration_hours=8,
    )
    db.add(task)
    db.commit()
    entry = DataPackage(
        workspace_id=workspace.id,
        collection_project_id=project.id,
        collection_task_id=task.id,
        package_uid=f"pkg-intake-{workspace.id}",
        target_duration_hours=2,
    )
    db.add(entry)
    db.commit()
    return entry


@pytest.fixture
def legacy_scope(db):
    """Episode 仍带遗留的非空外键 task_set_id / batch_id，这里提供最小记录。"""
    task_set = db.query(TaskSet).order_by(TaskSet.id).first()
    batch = db.query(Batch).order_by(Batch.id).first()
    assert task_set is not None and batch is not None, (
        "种子数据缺少 TaskSet/Batch；补 seed 或在此建最小记录，不要把 Episode 的遗留外键改成可空"
    )
    return task_set.id, batch.id


def _episode(package, legacy_scope, suffix: str, **overrides) -> Episode:
    task_set_id, batch_id = legacy_scope
    fields = {
        "episode_uid": f"ep-{package.id}-{suffix}",
        "workspace_id": package.workspace_id,
        "task_set_id": task_set_id,
        "batch_id": batch_id,
        "kind": "source",
        "modality": "video",
    }
    fields.update(overrides)
    return Episode(**fields)


def test_validity_statuses_are_the_specified_three():
    assert set(EPISODE_VALIDITY_STATUSES) == {"valid", "intake_rejected", "qc_dropped"}


def test_episode_defaults_to_valid(db, package, legacy_scope):
    episode = _episode(package, legacy_scope, "1")
    db.add(episode)
    db.commit()
    assert episode.validity_status == "valid"
    assert episode.data_package_id is None


def test_episode_rejects_unknown_validity_status(db, package, legacy_scope):
    db.add(_episode(package, legacy_scope, "2", validity_status="discarded"))
    with pytest.raises(IntegrityError):
        db.commit()


def test_episode_can_belong_to_a_data_package(db, package, legacy_scope):
    episode = _episode(package, legacy_scope, "3", data_package_id=package.id)
    db.add(episode)
    db.commit()
    assert episode.data_package_id == package.id


def test_intake_review_records_verdict_and_bulk_flag(db, package):
    review = PackageIntakeReview(
        data_package_id=package.id,
        verdict="approved",
        is_bulk=True,
        rejected_episode_ids_json=[],
    )
    db.add(review)
    db.commit()
    assert review.verdict == "approved"
    assert review.is_bulk is True


def test_intake_review_rejects_unknown_verdict(db, package):
    db.add(PackageIntakeReview(data_package_id=package.id, verdict="partial"))
    with pytest.raises(IntegrityError):
        db.commit()


def test_package_has_at_most_one_intake_review(db, package):
    db.add(PackageIntakeReview(data_package_id=package.id, verdict="approved"))
    db.commit()
    db.add(PackageIntakeReview(data_package_id=package.id, verdict="rejected"))
    with pytest.raises(IntegrityError):
        db.commit()
```

- [x] **Step 2: 跑测试确认失败**

```bash
TEST_DATABASE_URL=postgresql+psycopg://quicdata:quicdata@127.0.0.1:5432/quicdata_test_studio .venv/bin/python -m pytest backend/tests/test_intake_review_models.py -q
```

预期：collection error，`ImportError: cannot import name 'EPISODE_VALIDITY_STATUSES'`。

- [x] **Step 3: 给 Episode 加有效性字段**

在 `backend/data/database.py` 的 `Episode.__table_args__` 元组里追加：

```python
(
    CheckConstraint(
        "validity_status IN ('valid', 'intake_rejected', 'qc_dropped')",
        name="ck_episodes_validity_status",
    ),
)
```

在 `Episode` 的 `review_status` 列之后追加两列：

```python
    validity_status = Column(String(32), nullable=False, default="valid")
    data_package_id = Column(Integer, ForeignKey("data_packages.id"), nullable=True)
```

- [x] **Step 4: 加入库审核模型**

把 `backend/data/models/data_package.py` 顶部的导入块确认为（Task 7 已按此写入，若当时省略了 `Boolean`/`Text`/`JsonDocument` 则现在补上）：

```python
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from data.database import Base, JsonDocument
```

追加到该文件末尾：

```python
EPISODE_VALIDITY_STATUSES = ("valid", "intake_rejected", "qc_dropped")


class PackageIntakeReview(Base):
    """入库审核结论。

    管理员职责，发生在上传完成之后、建批之前。一个数据包只有一条结论：
    ``approved`` 放行进建批池，``rejected`` 整包作废。``is_bulk`` 区分列表
    批量通过与逐包审核，供审计追溯。
    """

    __tablename__ = "package_intake_reviews"
    __table_args__ = (
        UniqueConstraint("data_package_id", name="uq_package_intake_reviews_package"),
        CheckConstraint(
            "verdict IN ('approved', 'rejected')", name="ck_package_intake_reviews_verdict"
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    data_package_id = Column(Integer, ForeignKey("data_packages.id"), nullable=False)
    reviewer_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    verdict = Column(String(16), nullable=False)
    is_bulk = Column(Boolean, nullable=False, default=False)
    rejected_episode_ids_json = Column(JsonDocument, nullable=False, default=list)
    reason = Column(Text, nullable=False, default="")
    reviewed_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    package = relationship("DataPackage")
```

- [x] **Step 5: 写迁移**

`backend/alembic/versions/0005_episode_validity_and_intake_review.py`：

```python
"""Episode 有效性三态与入库审核结论。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005_episode_validity_and_intake_review"
down_revision = "0004_data_package"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "episodes",
        sa.Column("validity_status", sa.String(length=32), nullable=False, server_default="valid"),
    )
    op.add_column(
        "episodes",
        sa.Column(
            "data_package_id", sa.Integer(), sa.ForeignKey("data_packages.id"), nullable=True
        ),
    )
    op.create_check_constraint(
        "ck_episodes_validity_status",
        "episodes",
        "validity_status IN ('valid', 'intake_rejected', 'qc_dropped')",
    )

    op.create_table(
        "package_intake_reviews",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "data_package_id", sa.Integer(), sa.ForeignKey("data_packages.id"), nullable=False
        ),
        sa.Column("reviewer_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("verdict", sa.String(length=16), nullable=False),
        sa.Column("is_bulk", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("rejected_episode_ids_json", JSONB(), nullable=False, server_default="[]"),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("reviewed_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("data_package_id", name="uq_package_intake_reviews_package"),
        sa.CheckConstraint(
            "verdict IN ('approved', 'rejected')", name="ck_package_intake_reviews_verdict"
        ),
    )


def downgrade() -> None:
    op.drop_table("package_intake_reviews")
    op.drop_constraint("ck_episodes_validity_status", "episodes", type_="check")
    op.drop_column("episodes", "data_package_id")
    op.drop_column("episodes", "validity_status")
```

- [x] **Step 6: 跑测试确认通过**

```bash
TEST_DATABASE_URL=postgresql+psycopg://quicdata:quicdata@127.0.0.1:5432/quicdata_test_studio .venv/bin/python -m pytest backend/tests/test_intake_review_models.py -q
```

预期：7 passed。

- [x] **Step 7: 确认既有 Episode 测试未受影响**

给 `episodes` 加列可能影响既有的投影与序列化测试。

```bash
TEST_DATABASE_URL=postgresql+psycopg://quicdata:quicdata@127.0.0.1:5432/quicdata_test_studio .venv/bin/python -m pytest backend/tests -q -k "episode" 2>&1 | tail -5
```

预期：与 Task 2 Step 6 记录的基线一致。若有新失败，是既有代码对 Episode 列做了全量断言，按报错补上新字段的期望值，不要回退新列。

- [x] **Step 8: 提交**

```bash
git add backend/data/models/data_package.py backend/data/database.py backend/alembic/versions/0005_episode_validity_and_intake_review.py backend/tests/test_intake_review_models.py
git commit -m "feat: add episode validity states and package intake review"
```

---

### Task 9: 数据批与一次性归属

**Files:**
- Create: `backend/data/models/data_batch.py`
- Create: `backend/alembic/versions/0006_data_batch.py`
- Modify: `backend/data/database.py`（末尾注册行追加模块）
- Test: `backend/tests/test_data_batch_models.py`

**Interfaces:**
- Consumes: Task 7 的 `DataPackage`；Task 5 的 `CollectionLabel`
- Produces:
  - `data.models.data_batch.DataBatch`，表 `data_batches`
  - `data.models.data_batch.DataBatchPackage`，表 `data_batch_packages`，`data_package_id` 唯一
  - `data.models.data_batch.DataBatchLabel`，表 `data_batch_labels`

一次性归属用 `data_batch_packages.data_package_id` 的唯一约束在数据库层保证，而不是只靠建批页面筛选。治理三项加标注开关用布尔列（固定四项，不会增长）；审核模式本期固定单审，`review_mode` 留双审扩展位。

- [x] **Step 1: 写失败的测试**

`backend/tests/test_data_batch_models.py`：

```python
"""数据批模型：一次性归属、治理开关与审核模式扩展位。"""

import pytest
from sqlalchemy.exc import IntegrityError

from data.database import SessionLocal, Workspace
from data.models.collection_core import CollectionProject, CollectionTask
from data.models.data_batch import DataBatch, DataBatchPackage
from data.models.data_package import DataPackage


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def context(db):
    workspace = Workspace(name=f"ws-batch-{id(db)}")
    db.add(workspace)
    db.commit()
    project = CollectionProject(workspace_id=workspace.id, name=f"P-{workspace.id}")
    db.add(project)
    db.commit()
    task = CollectionTask(
        workspace_id=workspace.id,
        collection_project_id=project.id,
        name=f"T-{workspace.id}",
        target_duration_hours=8,
    )
    db.add(task)
    db.commit()
    return workspace, project, task


def _package(db, context, suffix: str) -> DataPackage:
    workspace, project, task = context
    entry = DataPackage(
        workspace_id=workspace.id,
        collection_project_id=project.id,
        collection_task_id=task.id,
        package_uid=f"pkg-batch-{workspace.id}-{suffix}",
        target_duration_hours=2,
    )
    db.add(entry)
    db.commit()
    return entry


def test_batch_defaults_to_single_review_and_no_governance(db, context):
    workspace, _, _ = context
    batch = DataBatch(workspace_id=workspace.id, name=f"B-{workspace.id}")
    db.add(batch)
    db.commit()
    assert batch.review_mode == "single"
    assert batch.integrity_check_enabled is False
    assert batch.quality_check_enabled is False
    assert batch.compliance_check_enabled is False
    assert batch.annotation_enabled is False


def test_batch_rejects_unknown_review_mode(db, context):
    workspace, _, _ = context
    db.add(DataBatch(workspace_id=workspace.id, name=f"B2-{workspace.id}", review_mode="triple"))
    with pytest.raises(IntegrityError):
        db.commit()


def test_batch_name_is_unique_within_workspace_ignoring_padding(db, context):
    workspace, _, _ = context
    db.add(DataBatch(workspace_id=workspace.id, name="夜间批次"))
    db.commit()
    db.add(DataBatch(workspace_id=workspace.id, name=" 夜间批次 "))
    with pytest.raises(IntegrityError):
        db.commit()


def test_package_can_join_a_batch(db, context):
    workspace, _, _ = context
    batch = DataBatch(workspace_id=workspace.id, name=f"B3-{workspace.id}")
    package = _package(db, context, "a")
    db.add(batch)
    db.commit()
    db.add(DataBatchPackage(data_batch_id=batch.id, data_package_id=package.id))
    db.commit()
    assert db.query(DataBatchPackage).filter_by(data_batch_id=batch.id).count() == 1


def test_package_cannot_belong_to_two_batches(db, context):
    workspace, _, _ = context
    first = DataBatch(workspace_id=workspace.id, name=f"B4-{workspace.id}")
    second = DataBatch(workspace_id=workspace.id, name=f"B5-{workspace.id}")
    package = _package(db, context, "b")
    db.add_all([first, second])
    db.commit()
    db.add(DataBatchPackage(data_batch_id=first.id, data_package_id=package.id))
    db.commit()
    db.add(DataBatchPackage(data_batch_id=second.id, data_package_id=package.id))
    with pytest.raises(IntegrityError):
        db.commit()
```

- [x] **Step 2: 跑测试确认失败**

```bash
TEST_DATABASE_URL=postgresql+psycopg://quicdata:quicdata@127.0.0.1:5432/quicdata_test_studio .venv/bin/python -m pytest backend/tests/test_data_batch_models.py -q
```

预期：collection error，`ModuleNotFoundError: No module named 'data.models.data_batch'`。

- [x] **Step 3: 写模型**

`backend/data/models/data_batch.py`：

```python
"""数据批：治理、标注与审核的分组单元。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import relationship

from data.database import Base


class DataBatch(Base):
    """从入库审核通过且未建批的数据包中创建的治理分组。

    创建后清单、流程与标签全部锁定，修改只能新建数据批。审核模式本期
    固定单审，``review_mode`` 留作双审扩展位。
    """

    __tablename__ = "data_batches"
    __table_args__ = (
        CheckConstraint("review_mode IN ('single', 'dual')", name="ck_data_batches_review_mode"),
        Index("ix_data_batches_workspace_created", "workspace_id", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    name = Column(String(128), nullable=False)
    integrity_check_enabled = Column(Boolean, nullable=False, default=False)
    quality_check_enabled = Column(Boolean, nullable=False, default=False)
    compliance_check_enabled = Column(Boolean, nullable=False, default=False)
    annotation_enabled = Column(Boolean, nullable=False, default=False)
    review_mode = Column(String(16), nullable=False, default="single")
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    packages = relationship("DataBatchPackage", back_populates="batch")
    labels = relationship("DataBatchLabel", back_populates="batch")


Index(
    "uq_data_batches_workspace_normalized_name",
    DataBatch.workspace_id,
    func.lower(func.btrim(DataBatch.name)),
    unique=True,
)


class DataBatchPackage(Base):
    """数据批与数据包的归属。

    ``data_package_id`` 上的唯一约束在数据库层保证「一个数据包最多归属
    一个数据批」，不依赖建批页面的筛选条件。
    """

    __tablename__ = "data_batch_packages"
    __table_args__ = (UniqueConstraint("data_package_id", name="uq_data_batch_packages_package"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    data_batch_id = Column(Integer, ForeignKey("data_batches.id"), nullable=False)
    data_package_id = Column(Integer, ForeignKey("data_packages.id"), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    batch = relationship("DataBatch", back_populates="packages")
    package = relationship("DataPackage")


class DataBatchLabel(Base):
    """建批时冻结的业务标签快照关联。"""

    __tablename__ = "data_batch_labels"

    data_batch_id = Column(Integer, ForeignKey("data_batches.id"), primary_key=True)
    collection_label_id = Column(Integer, ForeignKey("collection_labels.id"), primary_key=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    batch = relationship("DataBatch", back_populates="labels")
    label = relationship("CollectionLabel")
```

- [x] **Step 4: 注册模块**

`backend/data/database.py` 末尾注册行改为：

```python
from data.models import (  # noqa: E402,F401
    collection_config,
    collection_core,
    data_batch,
    data_package,
)
```

- [x] **Step 5: 写迁移**

`backend/alembic/versions/0006_data_batch.py`：

```python
"""数据批、一次性归属与标签快照。"""

import sqlalchemy as sa
from alembic import op

revision = "0006_data_batch"
down_revision = "0005_episode_validity_and_intake_review"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "data_batches",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column(
            "integrity_check_enabled", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("quality_check_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "compliance_check_enabled", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("annotation_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("review_mode", sa.String(length=16), nullable=False, server_default="single"),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("review_mode IN ('single', 'dual')", name="ck_data_batches_review_mode"),
    )
    op.create_index(
        "ix_data_batches_workspace_created", "data_batches", ["workspace_id", "created_at"]
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_data_batches_workspace_normalized_name "
        "ON data_batches (workspace_id, lower(btrim(name)))"
    )

    op.create_table(
        "data_batch_packages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("data_batch_id", sa.Integer(), sa.ForeignKey("data_batches.id"), nullable=False),
        sa.Column(
            "data_package_id", sa.Integer(), sa.ForeignKey("data_packages.id"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("data_package_id", name="uq_data_batch_packages_package"),
    )

    op.create_table(
        "data_batch_labels",
        sa.Column(
            "data_batch_id", sa.Integer(), sa.ForeignKey("data_batches.id"), primary_key=True
        ),
        sa.Column(
            "collection_label_id",
            sa.Integer(),
            sa.ForeignKey("collection_labels.id"),
            primary_key=True,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("data_batch_labels")
    op.drop_table("data_batch_packages")
    op.drop_index("ix_data_batches_workspace_created", table_name="data_batches")
    op.drop_table("data_batches")
```

- [x] **Step 6: 跑测试确认通过**

```bash
TEST_DATABASE_URL=postgresql+psycopg://quicdata:quicdata@127.0.0.1:5432/quicdata_test_studio .venv/bin/python -m pytest backend/tests/test_data_batch_models.py -q
```

预期：5 passed。

- [x] **Step 7: 全量回归**

```bash
TEST_DATABASE_URL=postgresql+psycopg://quicdata:quicdata@127.0.0.1:5432/quicdata_test_studio .venv/bin/python -m pytest backend/tests -q 2>&1 | tail -5
```

预期：通过数 = Task 2 Step 6 记录的基线 + 38（本计划新增 2+7+9+8+7+5 个测试）。

- [x] **Step 8: 验证完整迁移链可回滚**

```bash
cd backend && export DATABASE_URL=postgresql+psycopg://quicdata:quicdata@127.0.0.1:5432/quicdata_test_studio && \
  ../.venv/bin/python -m alembic downgrade 0001_baseline && \
  ../.venv/bin/python -m alembic upgrade head && cd ..
```

预期：0006 → 0001 逐级降级成功，再升回 head 成功。

- [x] **Step 9: 提交**

```bash
git add backend/data/models/data_batch.py backend/data/database.py backend/alembic/versions/0006_data_batch.py backend/tests/test_data_batch_models.py
git commit -m "feat: add data batch model with one-time package membership"
```

---

## 计划自检

**规格覆盖**（本计划范围＝迁移 + 领域模型基线；API 与业务逻辑在后续三份计划）：

| 规格章节 | 覆盖任务 |
|---|---|
| §2.1 采集项目 | Task 6 `CollectionProject`（`enabled`/`archived`、工作空间内唯一） |
| §2.2 采集任务 | Task 6 `CollectionTask`（`offline` 固定、目标时长、无人员与地区字段） |
| §2.3 采集设备型号目录 | Task 5 `CollectionDeviceModel` |
| §2.4 采集责任人与采集人员 | Task 7 `DataPackage` 双人员引用 + 已分配必填约束 |
| §3.1 状态机 | Task 7 `DATA_PACKAGE_STATUSES`；Task 8 Episode 三态 |
| §3.3 入库审核 | Task 8 `PackageIntakeReview`（含 `is_bulk` 批量通过审计） |
| §4 数据批与一次性归属 | Task 9 `DataBatch` + `data_package_id` 唯一约束 |
| §6 双有效时长 | Task 7 `intake_valid_duration_hours` / `governed_valid_duration_hours` + 大小关系约束 |
| §7 标签字典五类 | Task 5 `COLLECTION_LABEL_CATEGORIES` |

未覆盖且属于后续计划：§3.2 形态识别与预览策略、§5 标注与审核工作项、§8 数据资产与数据集、§9 API 与权限校验、§10 验证要求中的 API 与前端部分。

**类型一致性**：`DATA_PACKAGE_STATUSES`、`DATA_PACKAGE_TERMINAL_STATUSES`、`EPISODE_VALIDITY_STATUSES`、`COLLECTION_LABEL_CATEGORIES` 在定义任务与消费任务中拼写一致；`data.models.data_package` 在 Task 7 建立、Task 8 追加 `PackageIntakeReview`，Task 8 Step 4 给出完整导入块以消除两次修改导入的歧义。

**已知的执行注意点**：
1. Task 1 只读源仓 quicdata，任何情况下都不要修改它。
2. Task 2 Step 3 的 baseline 必须手工补齐函数式唯一索引，autogenerate 不会产出它们；漏一个就少一条约束，且测试不一定抓得到全部。
3. Task 2 Step 6 记录的回归基线是后续所有任务的比对基准，必须写进提交信息。
4. Task 8 的 Episode 测试依赖种子数据里的 `TaskSet` 与 `Batch`；缺失时在 fixture 里补最小记录，不要改 Episode 的遗留外键可空性。
5. 所有迁移的 `downgrade()` 都能真正回滚，Task 9 Step 8 验证整链。

---

## 审计结果（2026-09-22 收尾核对）

- 证据：本计划引用的 6 个测试文件全部存在并一起运行 **38 passed**（含 intake review 模型、数据资产与工作台模型）。
- 冲突说明：正文部分段落仍以 task set / Batch 作为采集层级；后续「采集项目 → 采集任务 → 数据包」规格已取代这些入口，本计划涉及的模型迁移本身仍然有效，未回填旧文。
- 结论：全部步骤按现有制品与测试勾选。
