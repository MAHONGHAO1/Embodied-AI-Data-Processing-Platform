# Data / Train 开发协作基线

日期：2026-09-18。基线分支：`init`。这是可启动、可跑回归的开发交接版本，不是完整生产验收版。

## 分工与接口边界

- Data：`backend/data/`，维护采集、上传、审核、治理、逻辑资产、目录数据集版本和 Export。
- Train：`backend/train/`，目前只有包与 README，供训练任务、调度和数据消费模块独立开发。
- 共用：`frontend/`、`backend/alembic/`、`backend/pyproject.toml`、Makefile 和部署脚本；修改前协调，避免同时改同一入口或创建相同迁移号。
- 建议各自从 `origin/init` 创建开发分支，例如 `init/data` 和 `init/train`，不要两个人直接推同一分支。
- Train 优先消费 Data 的公开 API，不导入 `data.services`，不修改 Data 表。直接读库需要另行约定，不能把内部 JSON 或数据库字段视为稳定公共接口。

## Train 现在可以对接什么

所有路径均以 `/api/v1` 开头，使用现有登录认证；读取需 `dataset:read` 权限。

| 接口 | 用途 |
| --- | --- |
| `GET /data-assets`、`GET /data-assets/{id}` | 读取逻辑资产；一批至多一个非空资产；默认跨采集工作空间，可用 `source_workspace_id` 显式筛选 |
| `GET /catalog-datasets`、`GET /catalog-datasets/{id}` | 全局目录数据集，不要求当前采集工作空间 |
| `GET /catalog-datasets/{id}/versions` | 固定版本清单：`id`、`dataset_id`、`version`、`status`、`data_asset_ids`、创建信息 |

Train 应保存 `dataset_id` 和不可变的版本 `id`，不要在任务运行时自动解析成“最新版本”。目录接口不同于遗留 `/datasets`；新训练开发应围绕 `/catalog-datasets`。

Export now runs as a durable worker job. A successful QRDF or LeRobot export records an immutable attempt, verified size/hash and stable `oss_uri` in the export bucket; failures remain queryable and retries create a new attempt. Train uses its own read-only OSS client to read that URI. Data does not proxy downloads, issue browser URLs, or pass credentials, and consumers must not scan raw/process prefixes outside the frozen dataset-version snapshot.

## 原生 LeRobot 直传

管理员可通过 `/api/v1/native-lerobot-direct-uploads` 声明原生 LeRobot 文件清单，并仅向 `export` bucket 获取服务端拥有的 multipart 上传目标。声明中的工作空间只保留采集审计来源；完成校验后会创建全局 `lerobot_direct` 目录数据集版本，不创建 `Batch`、`DataBatch`、`DataAsset`、Episode、标注或审核工作项。因此该版本的 `data_asset_ids` 为空是预期行为。

直传校验会在有界 `scratch_root` 中按冻结的 export 对象 identity 下载，并调用 QRDF vendor 的 LeRobot 校验器。来源缺失、对象漂移或 SDK 内容校验失败必须新建上传尝试；存储或运行时故障可由管理员通过 `POST /api/v1/native-lerobot-direct-uploads/{source_id}/retry` 重试相同冻结清单。Train 可安全通过只读凭证访问 export 桶中的直传交付物或 catalog export 产物。

## 本地运行

纯前端演示（无需数据库/OSS）：

```bash
make dev-frontend
# http://127.0.0.1:8090/?demo=1
```

端口冲突时使用 `make dev-frontend FRONTEND_PORT=8091`。演示模式仅使用前端内存数据，不代表后端功能已完成。当前新建批、逻辑资产、目录数据集的 demo 数据接口尚未补齐，会展示空列表；旧演示看板与部分交互仍可用，不把空列表当成真实数据库内容。

后端开发环境（需要 Python 环境及 Docker）：依赖安装按 `backend/pyproject.toml`，已有 uv 环境可执行 `uv sync --project backend`；Make 自动选用根目录 `.venv` 或 `backend/.venv`。

```bash
make infra-up
make dev-migrate
make dev-seed-local
make dev-api
# API 与同源前端：http://127.0.0.1:8000/；接口文档：/docs
```

异步服务使用 `make dev-up`，或按需启动 `make dev-worker-ingest` 等 worker 目标。禁止把开发 seed、测试库重置或 downgrade 命令运行在业务环境。

MinIO 已有 Compose 和 provider 实现，但上传/发布/导出调用者未全部切换；`make infra-up` 启动 MinIO 不代表真实三桶闭环就绪。真实对象存储、Celery 多进程和逐 Episode Preview 仍需专项联调。

## 本轮清理和验证

- 恢复 `frontend/js/app.js` 被截断的末尾 1036 行，保留已提交的 UI 结构；统一审核退回按钮样式。
- `backend/data/main.py` 只统一换行格式，无逻辑变化。
- 保留“一数据批一资产”的规格修订；将 Plan 3 旧自检标为历史快照，更新多来源 OSS 和审核前置条件的接口说明。
- 前端测试桩改为真实使用的标注/审核工作项 API，保留“不预加载无权限数据集”的行为校验；旧 cut/annotation 审核筛选不恢复到新批次审核页面。
- `node --check frontend/js/app.js` 通过；`node --test frontend/tests/*.test.mjs`：**269 passed / 0 failed**。
- 后端全量：**950 passed / 18 skipped**，97.77 秒。18 项跳过不算验收通过；vendor QRDF 测试不包含在此数字中。
- 使用本轮独立 PostgreSQL/Redis 容器（15432/16379），未清空现有 5432/6379 开发环境。pytest 在独立测试库重建 schema 并执行迁移，未在业务库做降级测试。
- 浏览器确认前端概览、接入、数据资产与数据集页面可渲染；这是演示页面冒烟，不是上传到导出的端到端验收。
- Uvicorn 在独立测试依赖上启动成功，`/health` 与 `/` 均返回 HTTP 200；数据库/Redis 可用，未启动 Celery worker、未配置真实 OSS，因此不算异步链路验收。

复现后端测试需自行准备隔离服务：PostgreSQL 用户/库见下方 URL；独立 Redis 的 13、14、15 库均会被清空，不可复用业务 Redis。

```bash
TEST_DATABASE_URL=postgresql+psycopg://quicdata:quicdata@127.0.0.1:15432/quicdata_test_handoff \
TEST_REDIS_URL=redis://127.0.0.1:16379/15 \
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  .venv/bin/python -m pytest -p anyio.pytest_plugin backend/tests -q
node --test frontend/tests/*.test.mjs
git diff --check
```

## 接下来由 Data 侧完成

1. 统一 provider 调用，退出服务端分片落盘与旧 official 发布链路。
2. 从包级事实门禁收敛为完整的逐 Episode 完整性/Preview 生成与状态流程。
3. 真实异步 QRDF/LeRobot Export、失败记录、重试及 `oss_uri` 交接协议；Train 侧使用只读 OSS client 直接读取。
4. 新接口的 demo 数据和真实前端交互收口，MinIO 与真实 worker 端到端验证。

详细工作仍以 [三桶与逻辑资产计划](superpowers/plans/2026-09-18-quicstudio-object-storage-logical-assets.md) 为准，本次交接不把未完成项改为已完成。
