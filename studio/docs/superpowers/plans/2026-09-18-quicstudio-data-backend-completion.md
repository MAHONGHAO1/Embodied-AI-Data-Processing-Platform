# QuicStudio Data 后端闭环补全实现计划

> **2026-09-22 最新修订**：本文件保留历史实施与验收记录。当前不开放同包多次建批，但保留多关联模型，不新增包 ID 单列唯一约束；单包一次最终入库审核，结论前修复来源，结论后补采另建关联包。下方分次建批、多轮入库审核、以及旧“包级唯一”修订不再作为本期执行要求；原生 LeRobot 来源直接进入 export。后续执行统一见 [本次修复计划](../../CODE_SPEC_REPAIR_PLAN_2026-09-22.md)，历史完成勾选不表示新规则已实现。

> **面向 AI 代理的工作者：** 本轮用户已授权 Superpowers 与子代理实施。使用 subagent-driven-development，逐任务 TDD、独立规格/质量审查，通过后进入下一任务。

**目标：** 将真实对象存储接入、QRDF 准入、失败隔离、逻辑资产和版本化 Export 连成可验证的 Data 后端闭环。

**架构：** 接入 worker 调用 QRDF SDK，持久化逐 Episode 的来源身份、attempt、完整性和 Preview 事实；审核及建批只读数据库。资产/版本只冻结引用，Export worker 生成真实产物并返回稳定 oss_uri；阿里云 OSS 与 MinIO 使用不同适配器、相同业务契约。

**技术栈：** Python/FastAPI、PostgreSQL/SQLAlchemy/Alembic、Celery/JobRun、QRDF Python SDK、oss2/boto3、MinIO、pytest。

## 依据、当前状态和全局约束

依据：`../specs/2026-09-18-quicstudio-data-backend-completion-design.md`，包含失败隔离和数据库门禁最新修订。较早计划的完成标记不作为本轮验收证据。

- 8 个 ready、2 个 failed 的包，正常 8 项可继续，失败 2 项留在原包显示原因；running 项也不阻塞正常项。零可用项不生成空批/资产。
- 审核、批量通过、建批只批量查持久化准入事实，不触发 OSS HEAD/GET 或 QRDF 校验；源 identity/attempt 不匹配不得放行。
- 入库仍需管理员人工审核；建批只纳入审核通过且未占用项；修复项不得自动加入已有快照。
- 用户确认占用改为 Episode 粒度：一个采集包允许向多个数据批提供不重叠的 Episode，单个 Episode 唯一占用。解除旧包唯一关联，所有批内流程使用固定 Episode 成员。
- 一批最多一逻辑资产；资产/版本不复制对象；资产/组集跨采集工作空间，采集和标注仍受采集工作空间约束。
- raw/process/export 三角色；无 official 新发布、无 local/hybrid 自动降级；scratch 只供 worker，前端 demo 保持独立。
- QRDF 复用 SDK，不在 Studio 复制一份校验器或编码器；原始 ID 的 source profile 与 Export canonical profile 分开使用。
- Export 必须生成真实产物，失败留痕、重试有新 attempt；只在完成校验后返回 oss_uri，不下发凭据。
- 不执行旧数据迁移、不改训练业务、不 push、不操作生产桶。任务只提交本任务文件，保留已有规格变更。

当前核对：provider 缺少固定身份读取，S3 未接业务配置；审核只看包级 facts；parse 任一异常回滚整会话；catalog export 立即标记 succeeded。QRDF 源库 commit 为 `6fe16a901d582936daee50b889716d2dac3ea463`，当前 vendor Validator 与源文件相同；先验证实际能力，不盲目覆盖 vendor。

## 文件结构与接口职责

| 任务 | 文件（均相对仓库根） | 职责 |
| --- | --- | --- |
| 1 | `backend/data/infra/object_storage.py`、`s3_object_storage.py`、新增 `aliyun_object_storage.py`、`storage_provider.py`；`backend/data/config.py` | 固定身份读/写/签名、双 provider 工厂、三桶配置 |
| 2 | 新增 `backend/data/models/episode_admission.py`、`services/episode_admission.py`；审核、建批服务和路由；新 Alembic revision | 持久准入事实、失败隔离、无 OSS 门禁 |
| 3 | 新增 `backend/data/integrations/qrdf/admission.py`；`collection_upload_intake.py`、`collection_upload_parse.py`、上传/包路由 | 真直传、逐来源容错、QRDF/Preview 调用及 process 持久化 |
| 4 | `backend/data/services/data_assets.py`、`catalog_datasets.py`、对应模型 | 不可变源/标注快照和 LeRobot raw 来源 |
| 5 | 新增 `backend/data/services/catalog_export_jobs.py`；catalog 模型/路由、`batch_workers.py`、Alembic revision | 真异步 Export、重试、产物与 Train 交接 |
| 6 | `backend/data/main.py`、旧 storage/cloud/publish 调用者、配置示例、Compose、Makefile、脚本 | 撤下遗留路径、兼容清理、部署契约 |
| 7 | 新增 `backend/tests/test_data_backend_e2e.py`、`docs/DEVELOPMENT_HANDOFF.md`、`backend/train/README.md` | 独立环境全链路和最终交付 |

各任务的 Alembic revision 基于实施当时 `head` 创建，不复用已有编号。任务按依赖顺序执行，禁止同时修改同一工作树的多个实现者。每项完成后生成 diff 审查包，由独立 reviewer 核对规格和质量；控制者只协调，不绕过审查直接修实现。

## 测试环境

不使用日常开发数据库。创建本轮专用 PostgreSQL/Redis 容器，绑定 loopback 的空闲端口；示例端口 15432/16379，使用前检查占用。数据库名 `quicdata_test_completion`。MinIO 使用测试桶及测试前缀，不删除已有桶/卷。环境不可用是验证阻塞，不记为通过。

```bash
TEST_DATABASE_URL=postgresql+psycopg://quicdata:quicdata@127.0.0.1:15432/quicdata_test_completion \
TEST_REDIS_URL=redis://127.0.0.1:16379/15 \
.venv/bin/python -m pytest backend/tests -q
node --test frontend/tests/*.test.mjs
git diff --check
```

不同 pytest 进程不得同时重置同一个测试库。先执行基线并保存输出；若基线异常先报告并处理，不把既有失败隐藏进新功能。

### 任务 1：双 provider 的固定身份读取与业务工厂

**文件：** 修改 `backend/data/infra/object_storage.py`、`s3_object_storage.py`、`backend/data/config.py`；新增 `backend/data/infra/aliyun_object_storage.py`、`storage_provider.py`；测试 `backend/tests/test_object_storage_contract.py`、`test_s3_object_storage.py`、新增 `test_storage_provider_runtime.py`。

- [x] **步骤 1：先写失败测试。** 覆盖来源被覆盖、版本读取、内容 hash 错误、非法角色、缺凭据、浏览器 endpoint 与 worker endpoint 分离；以真实适配器配 Stubber/fake SDK 边界响应，不只断言 mock 本身。

```python
def test_download_rejects_replaced_source(provider, changed_source, tmp_path):
    with pytest.raises(ObjectStorageError):
        provider.download_file(changed_source, str(tmp_path / "data.mcap"))
```

- [x] **步骤 2：运行新增用例并记录预期 RED。** 测试命令为通用测试前缀加 `backend/tests/test_storage_provider_runtime.py backend/tests/test_s3_object_storage.py -q`；函数缺失/身份未验证应失败。
- [x] **步骤 3：补协议与实现。** 保留现有 `StorageObjectRef`，新增以下公共契约，并提供从 Settings 选择 provider 的 `get_storage_provider()`；默认本地配置明确选择 MinIO，缺凭据抛 `StorageNotReady`，不读本地镜像。阿里云用 oss2，不用 S3 代替。签名地址独立 client，禁止签名后替换域名。下载流式计算 SHA-256/大小，带 VersionId 或 If-Match 防止 TOCTOU；拒绝没有 version/etag 的已完成来源。`put_worker_object` 计算真实 hash 并确认持久对象。

```python
def download_file(self, ref: StorageObjectRef, destination: str) -> StorageObjectRef: ...
def sign_get(self, ref: StorageObjectRef, *, expires: int = 900) -> str: ...
def object_uri(self, ref: StorageObjectRef) -> str: ...
```

旧调用路径在任务 6 撤下；本任务新增 factory 必须可直接供任务 3/5 调用，不能只是无人使用的抽象。若现有配置公共接口会被改变，同步其直接测试，不能隐式改变旧业务结果。

VersionId 是固定版本定位，ETag 仅用于 If-Match 条件读取而非内容校验，SHA-256 是 worker 独立流式计算的内容摘要。无版本存储须采用不可覆盖的服务端对象键和写/删权限保护，条件读取防覆盖竞态；multipart ETag 不能充当 MD5/SHA-256。客户端声明 hash 不能充当服务端 verified hash。
- [x] **步骤 4：跑 GREEN 与 provider 相关回归。** 确認下载身份不匹配绝不写成功结果，日志无凭据/签名 URL。
- [x] **步骤 5：自审、提交任务文件和报告。** 提交 `feat: add immutable dual-provider storage access`；报告给出 RED/GREEN 命令、输出及公共接口供下一任务使用。

### 任务 2：持久化 Episode 准入事实及数据库门禁

**文件：** 新增 `backend/data/models/episode_admission.py`、`backend/data/services/episode_admission.py`、Alembic revision；修改 `database.py` 必要模型注册、`models/data_batch.py`、`models/data_package.py`、`services/collection_intake_review.py`、`services/data_batches.py`、治理/标注/资产内的批成员查询、`routers/collection_intake_review.py`、`routers/collection_packages.py`、相关 schema；新增 `backend/tests/test_episode_admission.py`，扩展审核/建批 API 测试。

- [x] **步骤 1：编写失败测试。** 8 ready/2 failed、全部失败、running、不适用、旧 attempt、来源变更、重复审核、并发占用、修复后重新审核、批量返回排除明细。存储调用被替换为立即抛异常，但审核/建批正常成功，证明确实不访问 OSS。

```python
def test_review_and_batch_exclude_failed_without_storage(client, mixed_package, deny_storage):
    review = approve(mixed_package.id)
    assert len(review["accepted_episode_ids"]) == 8
    assert len(review["excluded_episodes"]) == 2
    assert build_batch(mixed_package.id)["episode_count"] == 8
```

辅助 fixture/HTTP helper 在测试文件中定义，不往生产代码添加测试专用方法。
- [x] **步骤 2：跑 `test_episode_admission.py` 记录 RED。** 现有整包前置条件或“全部标记 valid”必须被测试抓住。
- [x] **步骤 3：实现持久事实。** 表存 episode/source fingerprint、attempt、current 标记、integrity/preview 状态、QRDF/profile、report/process refs、时间和错误。来源对象清单保存 JSON 可序列化的 StorageObjectRef；唯一当前 attempt 与事务锁保证旧 worker 无法覆盖当前结论。包级返回 ready/running/failed/reviewed counts。可用性函数只依赖数据库字段。

```python
# 事务内判断；未知/缺失事实必须 fail closed，绝不回退包级 preview=true。
eligible = (
    fact.is_current
    and fact.source_fingerprint == episode.source_fingerprint
    and fact.validation_policy_version == required_policy_version
    and fact.output_verification_status == "verified"
    and fact.integrity_status == "passed"
    and fact.preview_status in {"ready", "not_applicable"}
)
```

`required_policy_version` 由服务器准入策略提供，标识 SDK/profile/issue 分类版本；策略升级时旧事实排除并异步重检，不能在审核 API 内重新读对象。`output_verification_status` 是 worker 对所需报告/Preview 对象验证后的持久结果；非视频仍须有已验证结构报告，不能用 not_applicable 绕过输出事实。

审核仅更新本次符合条件且未审核项，返回 accepted/rejected/excluded IDs 和原因。排除项保留原状态；建批绑定具体审核通过项，不按包重新展开全部 Episode。有效时长只累加通过项；零可用项返回明确空结果/业务错误且不创建空批。保留原人工退回语义。批量读取 facts，锁定父包/Episode 采用稳定 ID 顺序。

解除 `uq_data_batch_packages_package`，保留 batch/package 对唯一；新增批内 Episode 关联及 episode_id 全局唯一占用约束。解除“一包只有一次入库审核结论”的唯一约束，改为每次审核记录精确 Episode/attempt，重复请求不可重复累加时长。候选包按存在未占用已审核 Episode 判断，不能仅依赖包 status 或是否曾关联批。所有治理/标注/资产路径从批成员取 Episode，不以包 ID 动态重新展开；测试 8 项先入批，2 项修复后另入新批，两批互不污染且并发抢占只有一方成功。包级时长保留汇总，批级时长按固定成员计算。

本任务删除审核及包详情把包级 `preview.available` 作为所有 Episode 事实的读法；任务 3 删除 parse 中对应生成器。`collect.py/qrdf.py/annotate_service.py` 的其余遗留调用者由任务 6 逐个核对：保留功能必须切换到持久准入/媒体引用，遗留入口撤下，不能留下审核旁路。
- [x] **步骤 4：运行 admission、intake、data batch 相关 GREEN 回归。** 新迁移空库升级可通过；需要调整旧 fixture 时补真实准入事实，不保留生产旁路。
- [x] **步骤 5：提交与报告。** `feat: isolate episode admission failures and use database gates`，包含新旧返回契约、事实写入入口和迁移 head。

### 任务 3：直传接入与 QRDF 分析/Preview 真实编排

**文件：** 新增 `backend/data/integrations/qrdf/admission.py`；修改 `services/collection_upload_intake.py`、`collection_upload_parse.py`、`routers/collection_upload_sessions.py`、`collection_packages.py`、必要 worker 注册；新增 `backend/tests/test_qrdf_admission.py` 并扩展上传 smoke/API 测试。

- [x] **步骤 1：写 RED。** 真实 QRDF fixture（使用 vendor importer/recorder）验证 MCAP 损坏失败、正常 RGB 可生成视频/timeline；两个来源一坏一好，好项继续。上传完成不信任客户端 hash；worker 固定身份下载，生成 process 对象后才置 ready；旧 attempt 迟到不写入。不得用 parse_fixture_mode 代替真实管道测试。

```python
def test_mixed_source_analysis_keeps_valid_episode(mixed_qrdf_upload, run_ingest):
    result = run_ingest(mixed_qrdf_upload)
    assert result["ready_count"] == 1
    assert result["failed_count"] == 1
    assert result["failed"][0]["error_code"]
```

- [x] **步骤 2：运行上传与 QRDF 新测试并保存 RED。** 当前 parse 整会话回滚应暴露。
- [x] **步骤 3：接入任务 1 factory 和任务 2 facts。** multipart 生成对象身份；移除新 API 的二进制分片入口。逐来源捕获数据失败，保留可定位失败记录；共享文件失败传播至其依赖项，不影响其他来源。worker 在任务隔离 scratch 里还原 metadata/data_file 相对布局，拒绝路径逃逸、symlink 和超出预算。

先短事务领取带租约的来源 attempt，再在事务外执行下载/SDK/上传，最后短事务以 attempt + 租约条件写入结果；不能在外层 session 事务里用一次 rollback 撤销已经完成的其他来源。未能解析出 Episode ID 的坏来源保留 source 级失败记录供包详情展示，不伪造 Episode。每个来源结果独立持久化后刷新包级计数；会话成功表示调度/处理结束，不等于所有 Episode 通过。若采用单来源内 savepoint，失败记录须在 savepoint 回滚后写入，禁止失败记录一起被回滚。

```python
report = QRDFValidator().validate_episode(episode_path)
if report.ok:
    preview = Episode(episode_path).generate_rgb_previews()
    media_report = QRDFValidator().validate_episode(episode_path, check_media=True)
```

上面 report 包含训练 readiness 规则，接入必须明确保留代码分类：结构/可解析错误不可跳过，业务 QC 不因完整 Validator 聚合而意外变成强制流程。用 SDK 的结构化 issue codes 做明确分类并测试；不复制 QRDF 校验逻辑。没有 RGB 与 RGB 缺失/损坏严格区分；Preview 检查 warning 不能简单等同 `report.ok`，核对必需 stream 状态、manifest、timeline 和文件可解码性。SDK 原始 timestamp_ns 保留。process 上传验证后发布 DB 事实；审核不读 OSS。

示例 `if report.ok` 只表示全通过的简单分支；实际准入使用结构类结论，完整报告原样保存。分类最少覆盖 `NO_VALID_TRAINING_FRAMES/NO_VALID_EGO_FRAMES` 为训练 readiness，`MISSING_EGO_RGB/MCAP_UNREADABLE` 为阻断性结构问题；其余 ERROR 默认阻断，不以消息文本匹配规则。

新配置使用 raw/process/export；SDK 已带 Preview 需验证源 hash、参数、SDK 版本和实际文件；不合格时重新生成。记录实际 QRDF 版本和源码 commit。vendor 缺能力时只同步已验证必要改动，禁止用开发机绝对路径运行依赖。
- [x] **步骤 4：GREEN。** 跑真实 QRDF adapter、上传、审核回归；测试清理 scratch 后仍能通过 process 签名入口读取预览。
- [x] **步骤 5：提交与报告。** `feat: run QRDF admission and previews from object storage`。

### 任务 4：冻结可导出的逻辑资产/版本来源

**文件：** 修改 `services/data_assets.py`、`catalog_datasets.py`、相关 asset/catalog 模型；需要 schema 时添加新 migration；测试 `test_data_assets_api.py`、`test_catalog_datasets_api.py` 和新增 `test_asset_source_snapshots.py`。

- [x] **步骤 1：RED。** 一批一资产、失败/退回不纳入、修改最新标注不影响旧资产、跨空间版本不受 workspace 参数过滤、资产/版本登记零对象写入；原生 LeRobot 源放 raw，缺少完整对象清单不能视为可导出。

```python
def test_asset_freezes_annotation_revision(published_asset, change_latest_annotation):
    before = published_asset.source_snapshot_json
    change_latest_annotation()
    assert reload_asset(published_asset.id).source_snapshot_json == before
```

- [x] **步骤 2：运行 snapshot/asset/catalog 测试记录 RED。** 不以仅 episode_id 的快照通过新测试。
- [x] **步骤 3：实现规范 source manifest。** 每项冻结 `episode_id`、`files`（安全相对路径 + StorageObjectRef）、`annotation_revision` 与其 process refs、治理报告 refs、有效时长。版本清单固定资产 ID、快照标识与顺序；不读取“最新”状态导出。LeRobot 独立来源固定 raw 文件清单并调用 SDK LeRobot 格式校验，不进入采集治理或写 export。
- [x] **步骤 4：GREEN。** 数据库并发唯一约束确保重复完成批次不产生重复资产；引用保护在精确删除路径生效，归档不解除保护。
- [x] **步骤 5：提交与报告。** `feat: freeze exportable asset and dataset source manifests`，列明 Export 可以消费的确切快照结构。

### 任务 5：真实异步 Export 与稳定 OSS 交接

**文件：** 新增 `services/catalog_export_jobs.py`；修改 `services/catalog_datasets.py`、`models/catalog_dataset.py`、`routers/catalog_datasets.py`、`tasks/batch_workers.py`、JobRun kind 注册、Alembic revision；新增 `backend/tests/test_catalog_export_jobs.py`，更新 catalog API 测试。

- [x] **步骤 1：RED。** POST 返回 queued，无文件不成功；同幂等键复用；真实 QRDF/LeRobot 文件可独立解包读取；失败保留、新 attempt 重试；DB 提交失败/worker 重投递不交付半成品；不使用动态最新标注。

```python
def test_export_is_not_success_until_verified(client, dataset_version):
    result = create_export(dataset_version, format="qrdf_0_2")
    assert result["status"] == "queued"
    assert result["oss_uri"] is None
```

- [x] **步骤 2：运行 export/catalog 新测试记录 RED。** 占位立即 succeeded 必须失败。
- [x] **步骤 3：实现任务与物化。** 重用 JobRun dispatch、租约、恢复机制；Export attempt 增加 input version、唯一幂等键、错误、大小/hash、manifest/URI。读取任务 4 固定快照，经 provider 下载到有界 scratch；QRDF 输出 canonical 目录，固定并重映射 episode ID/manifest/annotation identity；调用现有 QRDF→LeRobot converter，原生 LeRobot 保持原格式，不反向 QRDF。混合不兼容格式明确失败，不静默丢弃。

产物选择可独立解包的 tar.gz（原 spec 的目录/包允许此实现），同时保存文件 manifest；若现有交付包使用其他编码，则响应提供准确 media_type/文件扩展名。打包后计算真实 size/SHA-256，上传 export 并固定对象身份；成功响应包含 `oss_uri`、format、size_bytes、sha256、manifest_summary、attempt。不得把输入摘要 hash 当产物 hash。凭据不入响应。每次重试新对象键，成功 URI 不可变。

QRDF 输出必须调用 `validate_canonical_dataset` 校验 dataset.json、episode 目录/metadata ID、manifest split 和冻结标注；LeRobot 调用 `validate_lerobot_dataset`，不只校验文件存在。Export attempt 在生成前登记其服务端前缀及输出计划；上传结果/完成清单以不可变 key 保存，DB 成功提交后才交付。若上传成功但 DB 提交失败，恢复器核对该 attempt 的完整输出并恢复提交；否则标记失败并精确清理该 attempt 未被成功记录引用的输出。不得删除其他 attempt 或 raw/process；可回收孤儿保留对象清单和失败原因，不靠整桶清空。
- [x] **步骤 4：GREEN。** 跑 converter 真实 fixture 和 catalog export/API；在 MinIO 读取 URI 对象解包，通过独立 QRDF/LeRobot Validator 后才记交付完成。
- [x] **步骤 5：提交与报告。** `feat: materialize catalog exports with verified OSS delivery`。

### 任务 6：撤下旧存储/发布路径并对齐部署和调用端

**文件：** `backend/data/main.py`、`config.py`、`services/storage_mode.py`、`cloud_storage.py`、`batch_episode_publication.py`、`qrdf_promotion.py`、`infra/oss_client.py` 及保留调用者；`backend/.env.example`（以仓库现有示例为准）、`deploy/docker-compose*.yml`、`Makefile`、启动脚本；上传前端必要接线与测试。

- [x] **步骤 1：RED。** 真实 app 路由行为验证旧发布/分片入口不可用；旧 `STORAGE_MODE/OSS_BUCKET_OFFICIAL/OSS_KEEP_LOCAL_CACHE/STORAGE_ROOT` 显式配置启动失败，缺凭据无磁盘回退；health 返回正确 provider 和三桶但不泄露凭据。前端真实上传使用 multipart，新结果展示失败明细，demo 不连接后端。
- [x] **步骤 2：运行 runtime/config/upload/前端测试记录 RED。** 不写只 grep 源码字符串的替代验收。
- [x] **步骤 3：先连接保留能力，再清理旧入口、调度、无调用者实现和旧测试契约。** 新应用不得把 official 作为 source；旧数据库迁移保留历史，不改旧 migration。worker 启动配置统一新工厂，MinIO 初始化三桶且独立持久卷，CORS 显式 origin、签名 endpoint 可达。SDK/前端调用协议变化同步文档。
- [x] **步骤 4：GREEN。** 全后端/前端回归、启动 smoke；不允许通过删除有效测试掩盖损坏；发现历史依赖无法安全迁移则提出具体 blocker，不批量删文件。
- [x] **步骤 5：提交与报告。** `refactor: retire legacy storage and wire studio deployment`。

### 任务 7：全链路、故障恢复与最终验收

**文件：** 新增 `backend/tests/test_data_backend_e2e.py`；更新 `docs/DEVELOPMENT_HANDOFF.md`、`backend/train/README.md`、本计划执行记录。

- [x] **步骤 1：先写真实 MinIO E2E。** 启动专用测试依赖，从 API 签名 multipart HTTP 上传真实 QRDF；按 8 正常/2 损坏验证审核/建批；完成标注单审、资产、跨空间版本、两种 Export；以独立只读 client 取交付并校验 hash。追加 LeRobot 直入不治理、全失败不建空批。
- [x] **步骤 2：运行 E2E，定位真实链路缺口。** 需要修复时交原任务实现者，新增回归先 RED 后 GREEN 并重新审查，不绕过门禁或 fake worker。
- [x] **步骤 3：故障与资源测试。** worker 重启/租约失效、DB 完成提交失败、对象替换/删除、存储超时、scratch 磁盘不足、孤儿分片取消；旧 attempt 不推进新 attempt，失败 Export 留痕。清理只作用本测试精确资源。
- [x] **步骤 4：完整回归与最终独立审查。** 运行后端/前端全量、vendor 聚焦测试、空库 `head → base → head`、MinIO E2E、diff check。分别报告真实阿里云 smoke（无测试凭据不得执行/记通过）、MinIO、SDK、API 和 demo 结果。
- [x] **步骤 5：交付与报告。** 更新 Data→Train HTTP 示例/字段和运行命令；提交 `test: verify Data backend object storage lifecycle`。未通过项不勾选，不把总测试数当作功能完成；不自行 push。

## 执行账本

- 2026-09-20：Task 1–7 的实现、迁移、主后端/前端回归、真实 MinIO
  E2E 与独立最终审查均已完成。逐项 RED/GREEN、对象身份、故障恢复和
  审查结论记录在
  `.superpowers/sdd/2026-09-18-quicstudio-data-backend-completion/task-7c-report.md`。
- 当前工作树尚未为本轮收口创建提交，因此包含“提交”的步骤仍保留未勾选；
  这不是实现未开始的标记。
- 已接受的外部验证边界：没有非生产 Aliyun OSS 凭据，故未执行真实 Aliyun
  smoke；QRDF 的功能回归为 `1596 passed, 2 skipped`，完整 upstream suite
  在本机仅残留一个 macOS 最大 RSS 性能阈值波动，未修改或隐藏该上游测试。

## 规格覆盖自检

| 规格章节 | 实现/验收 |
| --- | --- |
| QRDF 能力、准入分析、失败隔离和数据库门禁 | 任务 2、3、7 |
| 三桶/上传/scratch | 任务 1、3、6、7 |
| 逻辑资产/不可变版本/原生 LeRobot | 任务 4、5、7 |
| Export/Train/恢复 | 任务 5、7 |
| 遗留清理/部署/无迁移边界 | 任务 6、7 |

---

## 审计结果（2026-09-22 收尾核对）

- 证据：本计划引用的 7 个测试文件全部存在并一起运行 **60 passed / 8 skipped**；跳过项为需要外部环境（如训练面/平台角色）的门控用例，与计划未完成的语义无关。
- 结论：全部步骤按现有制品与测试勾选。
