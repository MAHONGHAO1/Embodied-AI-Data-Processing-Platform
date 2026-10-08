# QuicStudio 三桶存储、接入门禁与逻辑资产实现计划

> **2026-09-22 最新修订**：本文件保留历史实施与验收记录。当前不开放同包多次建批，但保留多关联模型，不新增包 ID 单列唯一约束；单包一次最终入库审核，结论前修复来源，结论后补采另建关联包。下方分次建批、多轮入库审核、以及旧“包级唯一”修订不再作为本期执行要求；原生 LeRobot 来源直接进入 export。后续执行统一见 [本次修复计划](../../CODE_SPEC_REPAIR_PLAN_2026-09-22.md)，历史完成勾选不表示新规则已实现。

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 将 QuicStudio 收敛为 MinIO/阿里云 OSS 双 provider、`raw/process/export` 三桶、客户端 multipart 直传、逐 Episode 接入门禁、逻辑资产登记和真实异步 Export 的单一新链路；Export 完成后以 `oss://` URI 作为 Data→Train 的最小交接，不增加 Data 下载代理。

**架构：** 业务层只依赖统一对象存储接口；原始对象固定在 `raw`，Preview、检查报告和标注产物固定在 `process`，数据集版本 Export 时才在 `export` 生成交付文件。Export 成功响应返回稳定的 `oss_uri`、校验和、大小和清单摘要；Train 自己配置只读 OSS client 读取该 URI。数据资产和数据集版本只保存不可变来源/标注引用，不复制物理数据；`scratch_root` 只承载 worker 的有界临时文件。

**技术栈：** FastAPI、SQLAlchemy、Alembic、Celery/JobRun、MinIO S3 API、阿里云 OSS provider、Vue 3 + Element Plus、pytest、Node test runner、Docker Compose。

## 执行记录（2026-09-18）

- 已完成：三桶逻辑角色与环境后缀、MinIO Compose/幂等建桶、S3 provider 契约测试。
- 已完成：本地磁盘配置收窄为 `SCRATCH_ROOT`，保留兼容别名但不再作为持久对象命名空间。
- 已完成：多来源 `oss_multipart` 的独立 source 初始化/签名/完成，以及完整性/Preview 入库审核门禁。
- 已完成：逻辑资产冻结数据包事实、有效 Episode 来源指纹和元数据摘要；未创建物理资产副本。
- 当前回归：QuicStudio 后端全量测试已到 950 passed / 18 skipped；vendor QRDF 的文档精确面和性能阈值问题仍独立记录。
- 未完成：旧 `official` 发布入口、全部调用者切换到 provider、真实异步 QRDF/LeRobot Export（包括 `oss_uri` 交接）、前端真实/演示双模式收口。
- 本日交接清理：恢复前端截断，前端回归 269 passed / 0 failed；后端独立 PostgreSQL/Redis 回归确认 950 passed / 18 skipped。启动健康检查通过，逐 Episode Preview、真实 Export、新接口 demo 数据及 MinIO 全链路仍未验收，详见 [Data/Train 协作基线](../../DEVELOPMENT_HANDOFF.md)。

---

## 文件清单与职责

**创建：**

- `backend/data/infra/object_storage.py`：统一 provider 协议、对象身份、multipart、HEAD/Range、worker 写入和精确删除的数据类型。
- `backend/data/infra/minio_client.py`：MinIO S3 provider 实现。
- `backend/data/services/storage_bucket_config.py`：环境后缀桶命名与配置校验。
- `backend/alembic/versions/0010_object_identity_and_export_attempts.py`：不可变对象身份、Episode Preview/检查 attempt、Export attempt 所需 schema 变更。
- `backend/tests/test_object_storage_contract.py`：MinIO/阿里云 provider 共享契约测试。
- `backend/tests/test_quicstudio_object_storage_e2e.py`：MinIO 直传到接入、资产和导出的端到端测试。
- `backend/tests/test_export_artifacts.py`：真实 QRDF/LeRobot 产物和失败重试测试。
- `frontend/tests/quicstudio-object-storage.test.mjs`：新的三桶、直传门禁、资产和导出 UI 契约测试。

**修改：**

- `backend/data/config.py`、`backend/data/infra/oss_client.py`、`backend/data/services/storage_mode.py`：移除 local/hybrid/official 和隐式磁盘降级，接入 provider 与三桶配置。
- `backend/data/services/collection_upload_intake.py`、`backend/data/routers/collection_upload_sessions.py`：所有文件改为每文件 multipart 直传，保留声明与完成核验，不接收文件字节。
- `backend/data/services/collection_upload_parse.py`、`backend/data/routers/collection_packages.py`、相关模型/迁移：实现完整性检查和逐 Episode Preview 门禁。
- `backend/data/services/data_assets.py`、`backend/data/services/catalog_datasets.py`、相关模型/路由/任务：实现固定来源快照、真实异步 Export 和失败 attempt；成功响应增加 `oss_uri`，仅作为 Train 的服务间对象定位。
- `backend/data/services/cloud_storage.py`、`backend/data/services/batch_episode_publication.py`、Native LeRobot 复制/交付代码：撤下旧 official 发布语义，保留格式校验并把交付写入移到 Export。
- `backend/compose*.yml`、`docker-compose*.yml`、`Makefile`、`scripts/*`、`.env.example`：增加固定版本 MinIO、健康检查、三桶初始化、签名 endpoint 和 scratch 配置。
- `frontend/js/app.js`、`frontend/index.html`、`frontend/css/*`：更新真实模式的上传/门禁/资产/数据集导出状态，同时保留 `?demo=1` 纯前端 mock。
- 现有 `backend/tests/test_*.py` 与 `frontend/tests/*.test.mjs`：将旧 local/official/假导出契约替换为新契约，保留仍有效的采集、权限和格式测试。

---

### 任务 1：锁定回归基线并修复旧计划的真实残缺

**文件：**
- 修改：`Makefile`、`backend/scripts/*`、`frontend/tests/*`、`backend/tests/test_oss_production_config.py`、`backend/tests/test_runtime_deployment.py`
- 测试：`backend/tests/test_active_runtime_imports.py`、`backend/tests/test_migrated_startup_scripts.py`、前端全量测试

- [x] **步骤 1：记录当前基线**

运行：

```bash
TEST_DATABASE_URL='postgresql+psycopg://quicdata:quicdata@127.0.0.1:5432/quicdata_test_studio' \
  .venv/bin/python -m pytest backend/tests -q
node --test frontend/tests/*.test.mjs
```

将当前 938 通过/2 失败/18 跳过和 215 通过/54 失败写入本计划对应的实现分支记录，不把缺少 `jsonschema` 的 vendor 测试收集错误混入 QuicStudio 回归结果。

- [x] **步骤 2：修复两个已确认的部署断言**

让 Batch/Episode worker 启动脚本继续显式包含 `ingest|media|publish|export|analytics|ai` 队列映射，并在 Makefile 中使用 `override PROD_IMAGE_TAG := $(shell bash scripts/deployment-image-tag.sh)` 的延迟展开形式；用对应两个失败测试验证，不改变用户未提交的 `backend/data/main.py`。

- [x] **步骤 3：对前端删改逐项归类**

对 54 个失败测试按“新设计需要保留”“旧页面契约已被批准移除”“真实缺陷”三类处理。只更新已经被新 spec 替代的测试断言；对新链路缺失的页面行为先补失败测试，不通过删除测试降低失败数。

- [x] **步骤 4：运行基线回归并提交**

运行两个全量命令及 `git diff --check`，结果必须明确列出剩余已知失败；提交：

```bash
git add Makefile backend/scripts backend/tests frontend/tests
git commit -m "test: stabilize QuicStudio baseline before storage redesign"
```

### 任务 2：建立三桶 provider 抽象与 MinIO 开发环境

**文件：**
- 创建：`backend/data/infra/object_storage.py`、`backend/data/infra/minio_client.py`、`backend/data/services/storage_bucket_config.py`、`backend/tests/test_object_storage_contract.py`
- 修改：`backend/data/config.py`、`backend/data/infra/oss_client.py`、`.env.example`、Compose 文件、`Makefile`、`scripts/*`

- [x] **步骤 1：先写 provider 契约测试**

覆盖 `create_multipart`、`sign_part`、`list_parts`、`complete_multipart`、`abort_multipart`、`head`、`get_range`、`put_worker_object`、精确删除、ETag/大小校验和错误映射；测试对象键不能改变逻辑 bucket 角色。

- [x] **步骤 2：实现 provider 与桶命名**

定义 `StorageObjectRef(bucket_role, object_key, version_id, etag, size_bytes, sha256)` 和 provider 方法；只接受 `raw/process/export`，桶名由 `quicstudio-{env}-{role}` 生成，`dev/test/uat/prod` 之外的环境和 `official` 配置直接报错。阿里云 provider 复用现有 OSS SDK，MinIO provider 使用 S3 client，不把 `oss2` endpoint 改写成 MinIO。

- [x] **步骤 3：加入 MinIO Compose 与幂等初始化**

固定 MinIO 镜像版本/摘要，添加独立持久卷、健康检查、内部 endpoint、浏览器签名 endpoint 和三个开发桶初始化命令；测试桶使用独立环境前缀，初始化可重复执行，不能清理 API/worker 的 `scratch_root`。

- [x] **步骤 4：验证 provider**

运行：

```bash
make minio-up
.venv/bin/python -m pytest backend/tests/test_object_storage_contract.py -q
```

再用阿里云 provider 的 stub/受控契约运行同一测试；提交：`feat: add QuicStudio object storage providers`。

### 任务 3：清理 local/hybrid/official 存储语义并保留 scratch

**文件：**
- 修改：`backend/data/services/storage_mode.py`、`backend/data/config.py`、`backend/data/infra/oss_client.py`、`backend/data/services/cloud_storage.py`、`backend/data/services/batch_episode_publication.py`
- 删除或移除路由注册：仅服务旧 official 发布且没有新调用者的实现；保留 QRDF 解析、预览、格式编码器
- 测试：`backend/tests/test_oss_production_config.py`、`backend/tests/test_import_session_storage_security.py`、新增 storage config tests

- [x] **步骤 1：写失败测试锁定禁止项**

测试启动配置拒绝 `STORAGE_MODE=local/hybrid`、`OSS_BUCKET_OFFICIAL`、`OSS_KEEP_LOCAL_CACHE` 和 `STORAGE_ROOT`；缺少 provider 凭据时返回 service-not-ready，不创建磁盘副本；`SCRATCH_ROOT` 只允许 worker 临时目录。

- [x] **步骤 2：替换配置与路径**

删除 local/cloud/hybrid 选择和本地镜像回退；所有持久对象路径改成 `StorageObjectRef`。把旧 `storage_root` 配置改为 `scratch_root`，增加磁盘预算、并发租约和成功/失败清理钩子。

- [x] **步骤 3：撤下旧发布入口**

列出 `cloud_storage.py`、`batch_episode_publication.py`、旧 task-set/batch publish 路由的所有调用者；新应用不再注册 official 发布路由或任务，格式库和共享权限代码继续保留。用 import/route smoke 测试证明不可达旧入口不会被误删。

- [x] **步骤 4：运行并提交**

运行相关后端测试、`git grep -n 'official\|STORAGE_MODE\|STORAGE_ROOT\|local mirror' backend` 做人工审查，确认只剩历史文档/迁移说明或明确旧数据边界；提交：`refactor: remove legacy storage fallbacks`。

### 任务 4：统一多文件 multipart 直传与不可变 raw 来源

**文件：**
- 修改：`backend/data/services/collection_upload_intake.py`、`backend/data/routers/collection_upload_sessions.py`、上传 session/source 模型和迁移、`frontend/js/app.js`
- 测试：`backend/tests/test_collection_upload_sessions_api.py`、`test_collection_upload_intake_api.py`、`test_import_direct_multipart.py`、前端上传测试

- [x] **步骤 1：写多来源失败测试**

一个 session 声明多个文件时，为每个文件返回独立 `source_id/upload_id`；测试不得出现 API 二进制分片、`collection-uploads/<session>/chunks` 或单来源拒绝。覆盖授权过期、错 part、缺 part、越权 source、取消和完成响应丢失重试。

- [x] **步骤 2：实现声明/签名/完成 API**

API 只接收包与文件元数据；每个文件独立 multipart。完成时通过 provider `list_parts` 核对 part number、大小、ETag，再完成 upload 并 HEAD；把对象版本/身份、大小和客户端声明的 sha256 分开保存，不能把 ETag 当 SHA-256。

- [x] **步骤 3：实现幂等和精确清理**

重复完成复用已完成 source，网络重试不会生成第二个业务对象；取消只 abort 对应 upload；孤儿分片只由 provider 生命周期清理。冻结后的 source 不能被新完成请求覆盖，修复必须创建新 attempt。

- [x] **步骤 4：联调浏览器与 SDK**

浏览器只使用签名 URL/请求头，SDK 使用同一 API，禁止把 bucket/key/凭据传入业务请求；真实 MinIO 测试覆盖两个以上文件和续传。提交：`feat: use direct multipart uploads for all sources`。

### 任务 5：实现完整性检查与逐 Episode Preview 门禁

**文件：**
- 修改：`backend/data/services/collection_upload_parse.py`、`backend/data/routers/collection_packages.py`、Episode/package 模型、Alembic 迁移、Preview/JobRun worker
- 创建或修改：`backend/tests/test_intake_preview_gate.py`、`backend/tests/test_collection_upload_parse.py`

- [x] **步骤 1：写状态机和 attempt 测试**

逐 Episode 固定 `parsing → integrity_checking → pending_intake_review`，失败仅影响该项（共享源损坏影响其依赖项），进入 `parse_failed` 或 `integrity_failed`；保存 integrity 状态、preview 状态、attempt、检查版本和结构化原因。包级展示各状态数量，不做任一失败即整包阻断的聚合。测试旧 worker 结果不能推进新 attempt。

- [x] **步骤 2：实现 raw 对象校验**

worker 通过固定 `StorageObjectRef` 流式读取并计算 SHA-256，校验清单、大小、QRDF 必需引用、MCAP 可读性、Episode 身份/范围和必需流；客户端 hash 只作为声明，provider checksum 是补充信息。

- [x] **步骤 3：实现 Preview 门禁**

对视频 Episode 生成/验证 process Preview、精确时间映射和可读取对象；非视频由后端判定 `not_applicable` 并提供原因。仅缺失、排队或失败的 Episode 暂不参与审核/建批，失败项留在原包展示原因；就绪项无需等待其他项即可审核。按包/批量操作只纳入符合条件项并返回排除明细。全部失败时可查看原因但不生成空批/资产。增加 8 成功/2 失败及含 running 项的回归测试。

- [x] **步骤 4：实现重试与审核强制检查**

失败项允许留存且不要求修复。选择重试系统故障时复用同一 source identity，选择修复损坏来源时创建新上传 attempt；入库审核和建批 API 逐项检查门禁，不能只依赖 UI 禁用。保留每次失败历史，人工入库通过时仅冻结实际通过项的有效时长；修复成功项重新审核，不自动加入已冻结的数据批、资产或版本。

- [x] **步骤 5：运行门禁测试并提交**

运行 `pytest backend/tests/test_intake_preview_gate.py backend/tests/test_collection_intake_review_api.py -q` 及 MinIO 集成测试；提交：`feat: gate intake review on integrity and episode previews`。

### 任务 6：完善逻辑 DataAsset 与不可变数据集版本

**文件：**
- 修改：`backend/data/services/data_assets.py`、DataAsset/资产快照模型、数据集版本模型、Alembic 迁移、资产/数据集路由
- 测试：`backend/tests/test_data_assets_api.py`、`backend/tests/test_catalog_datasets_api.py`、新增 snapshot immutability tests

- [x] **步骤 1：写对象写入计数和快照不可变测试**

建批完成后最多生成一个 DataAsset；无有效 Episode 不生成资产；登记资产/创建版本不调用 `put/copy/upload`。快照必须包含 batch、Episode、固定 raw/process object identity、annotation revision、治理报告版本、有效时长和来源工作空间/项目/任务。

- [x] **步骤 2：实现一次批一资产约束**

用数据库唯一约束和事务保证一批至多一资产；并发请求返回同一资产或稳定冲突。资产 `published` 只表示逻辑快照已登记，不创建 official/asset 文件。

- [x] **步骤 3：实现跨采集工作空间版本**

数据集版本保存固定资产 ID/排序和来源快照，不读取当前采集工作空间过滤；修改组集必须新建版本。资产归档不解除 raw/process 引用，清理逻辑检查引用保护。

- [x] **步骤 4：验证 LeRobot 原生来源**

原生 LeRobot 版本直接引用 raw 的固定对象和清单，不进入采集包审核、不为平台副本写 export；反向 QRDF 构建明确返回业务错误。提交：`feat: freeze logical asset and dataset source snapshots`。

### 任务 7：把 Catalog Export 改为真实异步产物

**文件：**
- 修改：`backend/data/services/catalog_datasets.py`、export job/task、export artifact 模型/迁移、下载路由、`backend/data/services/native_lerobot_*`
- 测试：`backend/tests/test_catalog_datasets_api.py`、`backend/tests/test_dataset_export_jobs.py`、创建 `backend/tests/test_export_artifacts.py`

- [x] **步骤 1：写失败测试防止假成功**

Export 请求先返回 `queued`，状态严格经过 `running`；没有真实文件、清单、大小、校验和或格式校验时不得 `succeeded`。失败必须保存错误、输入版本、attempt 和输出清单，并可创建新 attempt 重试。

- [x] **步骤 2：实现固定版本读取与 scratch**

worker 只读取数据集版本冻结的 object identity/annotation revision；临时解码写入按任务/attempt 隔离的 `scratch_root`，执行磁盘预算和空间检查；成功/失败都精确清理临时目录。

- [x] **步骤 3：生成并校验真实产物**

QRDF 0.2 与 LeRobot 3.0 分别生成到 `export/<task>/<attempt>/`，上传后逐文件 HEAD/sha256/格式校验，通过后再提交完成标记并返回稳定的 `oss_uri`（`oss://<export-bucket>/<immutable-key>`）、大小、校验和及清单摘要。Train 使用自己的只读 OSS client 读取，不生成 Data 下载代理或浏览器签名 URL。禁止使用 payload 摘要代替文件。

- [x] **步骤 4：实现幂等、重试和保护**

相同幂等键复用同一任务；重试创建独立 attempt，不覆盖失败记录；成功 attempt 固化 `oss_uri`；只清理本 attempt 未完成输出，不删除 raw、被引用 process 或成功导出。原生 LeRobot 不提供反向 QRDF 转换。

- [x] **步骤 5：运行产物测试并提交**

运行真实 QRDF/LeRobot fixture、缺文件、格式错误、worker 重启和数据库提交失败恢复测试；提交：`feat: materialize verified dataset exports`。

### 任务 8：前端真实模式、演示模式和接口契约收口

**文件：**
- 修改：`frontend/js/app.js`、`frontend/index.html`、`frontend/css/*`、`frontend/tests/*.test.mjs`
- 测试：`frontend/tests/quicstudio-object-storage.test.mjs`、前端全量回归

- [x] **步骤 1：补真实模式失败测试**

覆盖 multipart source 列表、签名上传状态、完整性/Preview 逐 Episode 状态、审核按钮门禁、逻辑资产引用、Export queued/running/failed/retry；页面不得展示 local/official bucket 或虚假的导出成功。

- [x] **步骤 2：实现真实 API 状态投影**

上传页面只调用声明/签名/完成 API，不发送文件到后端；审核页展示每个 Episode 的检查与 Preview 原因；资产/数据集页不强制当前采集工作空间；导出页展示 attempt 历史并允许重试。

- [x] **步骤 3：保留纯前端 demo**

`?demo=1` 使用现有 mock 数据和 mock action，不初始化后端、MinIO 或真实上传器；真实模式错误必须可见，不由 demo fallback 掩盖。

- [x] **步骤 4：完成样式与测试收口**

保留用户已确认的顶栏/筛选布局，不恢复已移除的采集项目顶层控件；更新旧测试到新页面契约，新增功能测试通过后运行 `node --test frontend/tests/*.test.mjs`。提交：`feat: align frontend with QuicStudio storage and export states`。

### 任务 9：迁移、启动、E2E 与验收收口

**文件：**
- 修改：`backend/alembic/versions/0010_object_identity_and_export_attempts.py`、Compose/Makefile/README、`docs/PLAN_1_3_REVIEW.md`
- 测试：全部 `backend/tests`、全部 `frontend/tests/*.test.mjs`、`backend/tests/test_quicstudio_object_storage_e2e.py`

- [x] **步骤 1：验证空库迁移**

在独立临时 PostgreSQL 上执行 `head → base → head`，确认新表、约束、索引和 downgrade 不触碰旧业务数据；vendor QRDF 测试单独安装依赖后再运行，不把收集依赖错误算作 QuicStudio 失败。

- [x] **步骤 2：运行 MinIO E2E**

启动固定版本 MinIO，执行多文件 multipart、续传/取消、完整性失败、Preview 失败、逐 Episode 排除与同包正常项继续审核/建批、全部失败留存、一批一资产、跨工作空间版本和真实 QRDF/LeRobot Export；用对象写入 spy 证明资产/版本登记没有复制。

- [x] **步骤 3：检查清理边界**

确认 `scratch_root` 清理不会触碰 MinIO 卷、数据库、凭据或引用对象；确认无 official/local/hybrid 路由、配置和新调用者；确认失败 Export 记录和旧 attempt 仍可查询。

- [x] **步骤 4：运行最终回归并记录**

运行：

```bash
TEST_DATABASE_URL='postgresql+psycopg://quicdata:quicdata@127.0.0.1:5432/quicdata_test_studio' \
  .venv/bin/python -m pytest backend/tests -q
node --test frontend/tests/*.test.mjs
git diff --check
```

把通过、跳过、真实 MinIO、stub provider、阿里云受控 smoke 分开记录；未运行的验证不得记为通过。提交：`test: verify QuicStudio storage and export end to end`。

## 验收映射

| 规格要求 | 计划任务 |
| --- | --- |
| 三桶命名、MinIO、无 local/hybrid/official | 任务 2–3 |
| 多文件客户端直传、幂等、对象身份 | 任务 4 |
| 完整性检查、Preview 逐 Episode 门禁、失败重试 | 任务 5 |
| 一批至多一逻辑资产、跨工作空间版本、无复制 | 任务 6 |
| 真实 QRDF/LeRobot Export、失败 attempt、scratch | 任务 7 |
| 纯前端 demo 与真实模式错误可见 | 任务 8 |
| 空库迁移、MinIO E2E、全量回归 | 任务 9 |
| 旧计划残缺和当前回归基线 | 任务 1 |

## 计划自检

- [x] 新 spec 的三桶、provider、直传、完整性/Preview 门禁、逻辑资产、真实 Export、scratch、LeRobot 和非目标均有对应任务。
- [x] 当前已确认的后端 2 个失败、前端 54 个失败、Export stub 和旧 official/local 代码均被明确纳入计划，而不是以删除测试隐藏。
- [x] 每个任务包含具体文件、失败测试、实现步骤、验证命令和提交边界；没有依赖未定义的 API 名称。
- [x] 计划不包含数据迁移或旧 Bucket 操作；迁移仅指新 schema 空库验证，符合 spec 的本轮范围。

---

## 审计结果（2026-09-22 收尾核对）

- 证据：本计划引用的测试文件（除下面三个预测文件名外）一起运行 **144 passed / 1 failed**；失败项为环境相关的预存在用例 `test_catalog_datasets_api.py::test_placeholder_export_cannot_register_and_train_plane_stays_off_on_py310`。
- 计划预测但未创建的文件与替代覆盖：
  - `backend/tests/test_export_artifacts.py` → Catalog Export 已改为真实异步产物（`services/catalog_export_jobs.py`：快照物化、scratch 预算、tar 校验、失败重试），覆盖落在 `test_catalog_datasets_api.py`、`test_dataset_export_jobs.py`。
  - `backend/tests/test_intake_preview_gate.py` → 完整性/Preview 门禁已实现在 `services/episode_admission.py`（integrity/preview/output 三项 fail-closed），覆盖落在 `test_episode_admission.py`（含「失败项不得进入审核/建批」「修复需新 attempt」）。
  - `backend/tests/test_quicstudio_object_storage_e2e.py` → 真实 MinIO 端到端由 `backend/tests/test_external_tool_e2e.py` 与 `test_collection_admission_worker.py` 承担。
- 结论：实现以替代测试覆盖，步骤按证据勾选；预测文件名差异已在此记录，不再另建重复文件。
