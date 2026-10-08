# QuicStudio Data 后端闭环补全规格

日期：2026-09-18
状态：待用户审阅；本文件是对 `2026-09-18-quicstudio-object-storage-logical-assets-design.md` 的实现补全规格，不表示代码已经完成。

> **2026-09-22 最新规则（待实施）**：当前按整包组织数据批，暂不开放同包多次建批，保留包→批多关联模型，不新增包 ID 单列唯一约束。每包只有一次最终入库审核；结论前可修复失败来源，结论后补采在同任务下新建关联包。分配、最终审核、建批和资产发布分阶段冻结。执行与验收以 [本次修复计划](../../CODE_SPEC_REPAIR_PLAN_2026-09-22.md) 为准。

## 1. 目标

把 Data 后端补到可以独立支撑下面这条生产闭环：

```text
客户端直传 raw
  → QRDF 完整性分析
  → QRDF Preview 生成与校验
  → 管理员入库审核
  → 建批、标注、单审、QC
  → 登记逻辑资产
  → 创建跨采集工作空间的数据集版本
  → 异步 Export 到 export
  → 返回稳定 oss_uri 给 Train
```

本规格解决的不是重新实现 QRDF，而是将 QRDF 已有的 Validator 和 Preview 能力接入 QuicStudio 的对象存储、任务状态、审核门禁和错误恢复体系，并补齐当前代码中仍然是占位或旧架构的部分。

## 2. QRDF 能力边界（已核实）

### 2.1 完整性校验

QRDF 提供：

- `QRDFValidator.validate_episode(path)`：兼容来源 profile 的 episode 校验。
- `QRDFValidator.validate_canonical_episode(path)`：新发布/平台准入使用的 canonical 校验。
- `QRDFValidator.validate_dataset(path)` 和 `validate_canonical_dataset(path)`：数据集级校验。
- `ValidationReport`：包含 `ok`、错误/警告数量及结构化 `ValidationIssue`（severity、code、message、path、topic）。

采集 raw 接入使用 `validate_episode` 的 source compatibility profile，保留安全的外部 Episode ID，不要求采集目录提前变成 canonical；QRDF Export 输出使用 `validate_canonical_dataset` 检查严格发布格式。建批不重跑 SDK，仅检查已持久化的准入及格式兼容事实。QRDF 的校验结果必须原样保留 issue code 和摘要，不能只落一个布尔值。

完整 Validator 同时包含结构校验和训练 readiness，平台必须分别保存“接入完整性结论”和“训练 readiness 发现”。结构、引用、MCAP 可解析性及必需流错误阻断该项准入；单纯训练对齐/可训练帧等发现进入 QC/Export 的目标格式检查，不能把可跳过的建批 QC 偷换成接入强制 QC。分类使用明确、版本化的 SDK issue code 策略，未知 ERROR 默认阻断该项并展示原因，不能一律忽略 ERROR。

### 2.2 Preview

QRDF 提供：

- `Episode.generate_rgb_previews()` / `generate_episode_rgb_previews()`。
- 每路 RGB topic 生成 VFR H.264 MP4 和原始 `timestamp_ns` timeline。
- 以 `media/preview/manifest.json` 作为唯一入口。
- 生成过程支持 staging、可复用 manifest 和失败清理。
- `QRDFValidator(..., check_media=True)` 可以检查已有 Preview 媒体。

但 QRDF 把 Preview 定义为可选派生物：`check_media=True` 的失败不会自动使 raw QRDF 无效。因此 QuicStudio 必须额外定义平台策略：对存在可预览 RGB 流的 episode，Preview 是入库审核前的强制门禁；对没有适用 RGB 流的 episode，状态为 `not_applicable`，并保留原因。

### 2.3 QuicStudio 不能假设的能力

QRDF 当前接口接收本地 `Path`，不直接读取 `oss://`。因此 worker 需要把固定版本的 raw 对象下载到隔离的 `scratch_root`，调用 QRDF，再把 Preview 和报告上传到 process。raw 仍以 OSS 对象为唯一权威来源，scratch 不是资产存储。

## 3. 统一对象存储与上传

### 3.1 三桶和环境命名

正式对象仅使用：

```text
quicstudio-{env}-raw
quicstudio-{env}-process
quicstudio-{env}-export
```

provider 为 `aliyun_oss` 或 `minio`。删除新链路中的 `local`、`hybrid`、`official`、本地长期镜像和缺凭据自动降级。保留 `scratch_root` 只用于 worker 临时物化。

**2026-09-22 用户确认**：上述“采集客户端直传 raw → 准入审核”的链路适用于 QRDF 采集来源。原生 LeRobot 走独立数据集入口，直接上传到 export，格式与完整性校验通过后登记固定数据集版本；不要求写 raw，也不进入采集包、建批、治理或标注。版本登记不复制来源对象，export 中被版本引用的直传来源同样受保留保护；具体职责见三桶规格 §7.2。

旧 `official`、旧 bucket 名和本地镜像代码不能通过字符串批量删除；必须先切换所有新调用者，再删除不可达实现和旧契约测试。历史数据迁移另行设计，本规格不执行迁移。

### 3.2 直传契约

- 每个源文件独立 multipart upload。
- API 只创建上传会话、签发短期分片地址、完成/取消并核对对象；不接收 MCAP、视频或压缩包字节。
- 完成时以 provider 分片清单、实际大小、HEAD 和服务端 SHA-256 为准，不能把客户端声明或 multipart ETag 当作内容校验。
- 支持多个来源、多文件、续传、授权过期、重复完成幂等、取消和越权拒绝。
- 已接纳的 raw 对象记录不可变 object identity；后续校验、Preview、资产快照和 Export 均引用该 identity，不引用可覆盖的路径。

## 4. QRDF 接入分析编排

### 4.1 任务和状态

新增或补全每个来源/episode 的分析记录，至少包括：

- `source_object_id`、episode ID、attempt ID；
- `integrity_status`：`queued/running/passed/failed`；
- `preview_status`：`not_applicable/queued/running/ready/failed`；
- QRDF 版本、校验 profile、参数摘要、开始/结束时间；
- `report_json`（完整 `ValidationReport`）、`preview_manifest_json`；
- 输入对象 size/SHA-256，输出 process 对象清单、size/SHA-256；
- 错误码、可重试性和 worker attempt。

包级只聚合展示可审核、处理中、失败和已审核的 episode 数量，不能再用一个包级 `preview_available` 代替逐 episode 状态，也不能用单个失败结果阻断整个包。

### 4.2 编排顺序

```text
upload completed
  → integrity_queued
  → 下载固定 raw object 到 scratch
  → QRDF Validator
  → 若有适用 RGB：生成 Preview 到 scratch
  → QRDF check_media=True
  → 上传 process 对象并 HEAD/hash 校验
  → 写入不可变报告和 manifest
  → 当前 episode 可进入入库审核；包展示可审核数量及失败/处理中数量
```

任一必需步骤失败，仅该 episode 进入 `failed`，保留在原包内展示失败阶段和结构化原因，不删除、不自动视为通过、不要求用户先修复；同包已就绪的 episode 可立即继续入库审核和建批，不等待失败项或仍在处理的项。共享源文件损坏时，仅将依赖该文件的 episode 标记失败；整包无法解析时保留包级错误，不编造可用 episode，也不阻塞其他包。

失败项可长期留存。最终入库审核前，系统故障可重试同一输入 identity；修复缺失/损坏来源创建新上传 attempt，并重新完成准入检查。旧 attempt 的迟到结果不能推进新 attempt。每包只形成一次最终审核结论，固定有效与排除项及来源版本；结论后补采另建同任务下的关联包，不向原包追加数据再审。建批后的治理阶段重试仍按原有规则处理，不能改变批内输入快照。

### 4.3 门禁策略

- QRDF Validator 有接入结构类 ERROR：仅该 episode 不可审核通过或纳入建批，正常项继续；训练 readiness 发现按第 2.1 节单独记录，不改变原始报告。
- 有 RGB 流但 Preview 未 `ready`：仅该 episode 暂不参与审核和建批，不阻塞其他项。
- 无适用 RGB 流：`preview_status=not_applicable`，必须记录检测依据。
- Preview 存在但 manifest、视频、timeline 任一无法读取或校验：失败，不视为“有文件即可”。
- warning 不阻断，但要在审核详情和审计记录中展示。
- 入库审核 API、批量通过 API 和建批 API 都逐 episode 检查资格，不能只依赖前端按钮禁用。按包操作处理其中符合条件的 episode，响应返回实际通过/纳入的 ID，以及排除项 ID 和原因；不能因夹带失败项而让整包失败，也不能悄悄把失败项计入成功。
- 建批只选择入库审核通过且尚未归属任何数据批的数据包，并冻结每个包当时已通过入库审核的有效 episode 清单。失败项、处理中项和未审核项留在原包，不进入标注、QC、资产或 Export；包的总数与实际纳入数量分别展示，有效时长只统计实际纳入项。整包归属一个批次不表示把不合格项也纳入批内有效成员。
- 如果包内全部失败，包仍可打开查看原因，显示“无可审核数据”；不生成空数据批或空资产，不阻塞其他包。尚未最终审核的包恢复可用项后仍须人工入库审核；已形成最终结论的包需要补采时另建关联包。
- 建批占用以数据包为粒度：当前候选列表和后端业务校验不开放同包多次建批；创建事务锁定包并检查已有批关联，防止并发绕过限制。保留包→批多关联及 batch/package 联合唯一，不新增包 ID 单列唯一约束，现有 Episode 占用约束本期保持。8 项有效、2 项失败时，可在最终审核前修复再统一审核，也可最终确认排除 2 项后建批；后续补采使用新包。治理、标注、取数和资产均使用批内固定有效成员及来源版本。未来同包多批独立定义，不改写既有批次与资产。

### 4.4 预览访问

平台只返回资源 ID 或短期签名读取地址，不持久化签名 URL。签名请求只能访问该 episode 已登记的 process 对象，不允许客户端指定任意 bucket/key。时间轴使用 QRDF 的原始 `timestamp_ns`，禁止按视频帧号或 nominal FPS 反推标注时间。

### 4.5 门禁的性能边界

完整性校验、内容 SHA-256 计算、Preview 生成和对象验证只在异步处理阶段执行。入库审核、批量通过和建批 API 仅批量查询数据库中持久化的准入事实，不执行 OSS HEAD/GET，不下载原文件，不重新运行 QRDF。

准入事实绑定输入对象不可变身份、当前 attempt 和校验版本；审核/建批在数据库事务内锁定相关状态并检查事实仍属于当前来源。来源变化须使原事实失效并异步重检。批量操作不能用逐项 OSS 请求代替批量数据库查询。

私有桶的对象覆盖/删除保护保障已冻结引用；若对象被外部误删，后续实际读取（例如 Export）明确失败并保留原因，不能以每次审核重读 OSS 掩盖保留策略缺失。添加审核/建批时存储访问即抛错的测试，证明这些 API 的正常路径完全不依赖 OSS 可用性。

## 5. 数据批、逻辑资产和数据集版本

以下既有业务语义保持不变：

- 一个数据批最多登记一个逻辑 DataAsset；零有效 episode 不产生资产。
- DataAsset 只保存不可变快照，不复制 raw/process 对象，不生成 `official` 副本。
- 快照冻结数据批、episode 清单、raw object identity、标注修订、QC/治理报告版本和有效时长。
- 数据集版本保存固定资产清单和排序，跨采集工作空间组集，不受当前采集工作空间过滤。
- 修改组集内容通过新建数据集版本完成，不能修改已发布版本的输入集合。
- 资产、版本和 Export 引用保护底层 raw/process 对象；归档不能解除引用。

建批时的“批次一致性检查”只检查冻结引用、格式兼容、重复身份和业务一致性，不承担第一次文件完整性或 Preview 生成，也不能绕过接入门禁。

## 6. 真实 Export 和 Train 交接

### 6.1 Export 任务

当前 `export_catalog_version()` 的立即 `succeeded` 占位实现必须删除。新任务状态为：

```text
queued → running → succeeded
                 ↘ failed
```

worker 使用数据集版本冻结的输入：

- QRDF 0.2：生成可读取、可验证的 QRDF 目录/包。
- LeRobot 3.0：生成原生 LeRobot 交付目录/包；不做反向 QRDF 转换。

产物写入 export 的独立 task/attempt 前缀。只有文件清单、数量、大小、SHA-256、格式校验和必要 manifest 全部通过后，才将任务置为 `succeeded`。

### 6.2 Export 记录

Export 记录必须新增或补齐：

- `attempt_id`、状态、错误码、错误详情、重试次数；
- `oss_uri`、format、size_bytes、sha256；
- manifest 摘要、输入数据集版本、QRDF/转换器版本；
- created/running/finished 时间；
- 幂等键。

失败 attempt 永久保留；重试产生新 attempt，不覆盖失败历史。成功 attempt 的 `oss_uri` 固定，不能指向可变对象。

### 6.3 Train 接口

成功响应只提供：

```json
{
  "status": "succeeded",
  "format": "lerobot_3_0",
  "oss_uri": "oss://quicstudio-prod-export/exports/<task>/<attempt>/dataset.tar.gz",
  "media_type": "application/gzip",
  "size_bytes": 123,
  "sha256": "...",
  "manifest_summary": {}
}
```

Data 不代理下载、不返回 AK/SK、不向 Train 下发浏览器签名 URL。Train 使用自己的只读 OSS client，通过环境配置访问该 URI。Data 只负责交接 URI 和可验证的产物元数据。

## 7. scratch 与资源安全

- QRDF 分析、Preview 和 Export 可在 `scratch_root` 物化必要文件，但必须按任务/attempt 隔离。
- 下载、转码和导出受磁盘预算、并发数和剩余空间检查约束；超限明确失败。
- 成功或失败均受控清理 scratch；运行中任务不能被 TTL 删除。
- MinIO 持久卷、数据库、凭据和已登记 process/export 对象不属于 scratch 清理范围。
- 任何删除只接受精确 object identity，不允许通过宽泛前缀误删其他任务对象。

## 8. 代码补全与清理顺序

按以下顺序实现，避免先删旧代码导致调用链断裂：

1. 引入 provider 接口和 MinIO/阿里云契约测试，统一三桶配置。
2. 将所有新上传调用者迁移到 direct multipart；关闭 API 分片、local mirror、official publish 新入口。
3. 引入 QRDF adapter：固定对象下载、Validator、Preview、process 上传、报告持久化。
4. 把逐 episode 门禁接入入库审核、批量审核和建批服务端校验。
5. 补资产快照中的 source object identity 和标注修订引用。
6. 实现真实 Export worker、attempt 表/状态、产物校验和 `oss_uri` 响应。
7. 清理 `storage_mode`、`oss_bucket_official`、旧 cloud/local fallback、旧发布入口和不可达测试。
8. 最后删除确认为无调用者的兼容代码，并更新 API/部署文档。

不得删除 QRDF 解析、Validator、Preview 或 LeRobot 转换能力；它们是新链路的核心依赖。前端 `?demo=1` 继续使用纯前端 mock，不因为后端清理而接入 MinIO。

## 9. 验收标准

### 9.1 QRDF 接入验收

- 有效 QRDF episode 能产生 `ValidationReport.ok=true`，并生成可播放 Preview、timeline 和 manifest。
- 缺失 metadata、data.mcap、必需 topic、损坏 MCAP、非法 schema 能保留结构化错误，仅排除受影响 episode，不阻断同包正常项。
- Preview 编码失败、manifest 缺失、视频/timeline 无法读取能阻断有 RGB episode；无 RGB episode 正确标记 `not_applicable`。
- 同一 raw object 重试幂等；新上传 attempt 不被旧结果覆盖。
- process 对象与报告通过 HEAD、size、SHA-256 验证后才能登记 ready。
- 混合包包含 8 个 ready、2 个 failed 时，8 个正常项可审核并建批，整包仅归属该批，2 个失败项保留原包及原因，不进入资产/Export；另含 running 项时也不等待其结束，迟到结果不得追加批内成员。覆盖最终审核前修复、单次最终结论及新包补采、已建批包重复/并发建批拒绝、全部失败、共享源损坏、批量操作排除明细及既有快照不变的测试。

### 9.2 存储和业务验收

- 新环境只有 raw/process/export 三桶，无 official 读写、无 local/hybrid 降级。
- 多文件 multipart 直传覆盖续传、错分片、取消、重复完成和越权。
- 一个批最多一个逻辑资产；资产和数据集版本创建不复制物理对象。
- 跨采集工作空间组集时仍可稳定读取冻结来源。

### 9.3 Export/Train 验收

- QRDF 和 LeRobot Export 都生成真实文件，能被独立客户端读取和格式校验。
- Export 失败保留失败记录，重试拥有独立 attempt；失败不能变成假成功。
- 成功响应包含稳定 `oss_uri`、大小、SHA-256 和 manifest 摘要，不包含凭据。
- Train 使用自己的只读 OSS client 读取并校验 checksum；Data 不提供下载代理。

### 9.4 回归验收

- 后端单元/API 回归、MinIO 实际直传回归、QRDF adapter 回归、完整 E2E 分开报告。
- E2E 至少覆盖：上传 → QRDF 校验 → Preview → 入库审核 → 建批 → 资产 → 数据集版本 → Export → Train 读取。
- worker 重启、DB/Redis 暂时失败、OSS 超时、磁盘不足、重复投递均有明确结果。
- 未执行的阿里云真实 smoke 不得标记为通过；不能用前端 demo 或 mock Export 代替真实验收。

## 10. 非目标

本轮不做：旧数据或旧 Bucket 迁移、在线分发、OSS 扫描、双审、多租户细粒度权限、训练调度、训练侧实现、脱敏和切分实体。训练侧只实现读取约定所需的客户端配置，不扩展训练业务。

## 11. 完成定义

只有当第 9 节全部通过，并且代码中不存在立即返回 `succeeded` 的 Export stub、旧链路的隐式 local/hybrid 降级和缺失 Preview 门禁时，才认为 Data 后端第一期完成。此前只能称为“业务 API 基线可用”。
