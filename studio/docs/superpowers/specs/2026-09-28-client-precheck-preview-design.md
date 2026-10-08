# 客户端预检：Toolchain 生成 Preview 与完整性校验，Studio 免下载入库

日期：2026-09-28
涉及仓库：quic_studio（`feature/data-backend-completion`）、quic-data-toolchain（Duance，`feat/quicstudio-sk-upload`）、qrdf（`feature/preview-hw-encoder`，已完成部分见 §7）
不涉及：quicdata

## 1. 背景与目标

现在 Duance 只上传 `metadata.json` 与 MCAP，Studio 的 admission worker 为每个 episode：从对象存储下载 MCAP → 重算 SHA-256 → `QRDFValidator` 结构与媒体校验 → libx264 编码 preview → 打 `process.tar`（含 MCAP 副本）上传 process 桶。preview 编码是主要耗时，下载与打包使大文件的服务端处理时间和存储量成倍增长。

目标：

1. Duance 在本地完成完整性校验与 preview 生成（可用硬件编码），与原始数据一起上传；Studio 信任客户端结论，**不下载 MCAP**，直接写入 admission 事实，数据包即可入库审核。
2. 旧链路保留：Duance 关闭该功能、服务端不接受、或客户端产物验证失败时，走现有的服务端校验与生成。
3. 去掉 `process.tar`，统一为“episode 对象清单”。
4. 本地校验发现文件损坏时，经用户确认后用 `qrdf.repair` 修复，修复后的 episode 可上传。

## 2. 已确定的决策

| 决策 | 结论 |
| --- | --- |
| 服务端是否重跑完整性校验 | 不重跑。信任客户端报告，但由服务端按自己的规则重新判定 issue codes；版本白名单与总开关兜底。 |
| 传输完整性 | 客户端上传每个分片时带 `Content-MD5`，存储端拒收损坏分片；服务端只 HEAD 核对大小与 etag，不下载。 |
| `process.tar` | 删除。两条链路都产出“episode 对象清单”。1.0 开发阶段，不兼容历史数据。 |
| Preview 位置 | 写入标准 episode 目录 `media/preview/`，兼作本地缓存。 |
| 本地校验有阻断错误 | 该 episode 不带 `client_admission`、不上传 preview，按旧路径上传原始数据，界面显示原因。服务端复检；integrity 失败时入库审核自动 `excluded`（`integrity_failed`）。同包其余 episode 照常上传。 |
| 不符合上传条件 | 仅 `in_progress`：不声明、不改文件、不提示修复、不生成 preview，界面显示「录制未结束，本次未声明、未上传」。 |
| 文件损坏 | 扫描后展示预计恢复结果，用户确认才修复；替换 `data.mcap`，原文件留在 `.qrdf-repair/<UTC>/`，不上传；`metadata.repair` 标记。拒绝修复、无法修复或没有可恢复消息时，原文件按旧路径上传，由服务端复检。 |
| Toolchain 开关 | “本地生成 Preview 与校验”，默认开启，关闭即走旧链路。 |

## 3. 数据流

```
Duance（开关开启）                                   Studio
──────────────────────────────                      ──────────────────────────────
读 capabilities.client_admission
  └ 不接受 → 旧链路
每个 episode：
  1 QRDFValidator 结构 + 媒体校验
  2 MCAP_UNREADABLE → scan_mcap → 用户确认
      → repair_episode → 回到 1
  3 按服务端下发的非阻断 code 表判定；
      有阻断错误 → 不带 client_admission，旧路径上传，不传 preview
      不符合录制条件 → 不声明、不改文件
  4 通过时 generate_rgb_previews（auto 硬件编码；
      指纹匹配则复用）；媒体检查报错则不声明 preview
  5 通过的 episode 声明：原字段 + client_admission
  6 上传 MCAP（raw 桶）与 preview 文件
    （process 桶），分片带 Content-MD5    ──►   complete → 解析作业 → 每 episode 一个 admission 作业
                                                 客户端预检模式（不下载 MCAP）：
                                                   a HEAD 全部对象：大小、etag
                                                   b 版本白名单 + 重新判定 issues → integrity
                                                   c 只下载 preview 小文件并验证
                                                   d metadata.json、admission-report.json 写入 process 桶
                                                   e 写 fact：objects_json + integrity_source=client
                                                 任一步骤无法完成 → 回退服务端模式（§4.4）
```

## 4. Studio 改动

### 4.1 capabilities

`GET` 上传 capabilities 新增：

```json
"client_admission": {
  "enabled": true,
  "accepted_qrdf_versions": ["0.2.1"],
  "accepted_policy_versions": ["v1"],
  "non_blocking_issue_codes": ["NO_VALID_TRAINING_FRAMES", "NO_VALID_EGO_FRAMES", "NO_STATE_TOPIC", "NO_ACTION_TOPIC"]
}
```

- `enabled` 由新配置 `accept_client_admission`（默认 `true`）控制，运维可一键强制全部走服务端。
- `accepted_qrdf_versions` 与声明中的 `qrdf_version` 指客户端 qrdf **SDK 包版本**（`qrdf.__version__`）；metadata 内的 `qrdf_version` 仍表示数据格式版本（当前示例为 `0.2.0`），两者不是同一字段。
- `non_blocking_issue_codes` 即 `TRAINING_READINESS_ISSUE_CODES`，是阻断判定的唯一来源；客户端据此决定该 episode 是否附 `client_admission`，服务端据此重新判定。阻断错误不再阻止整包上传。

### 4.2 声明

`PackageSourceDeclarationRequest` 新增可选 `client_admission`：

```json
"client_admission": {
  "qrdf_version": "0.2.1",
  "policy_version": "v1",
  "report": {"ok": true, "issues": [...], "media_validation": {...}},
  "files": [
    {"path": "media/preview/manifest.json", "size_bytes": 812, "sha256": "..."},
    {"path": "media/preview/generations/<id>/head_rgb.mp4", "size_bytes": 154235611, "sha256": "..."},
    {"path": "media/preview/generations/<id>/head_rgb.timeline.json", "size_bytes": 902113, "sha256": "..."}
  ]
}
```

- `files[].path` 必须位于 `media/preview/` 下，按现有 portable relative path 规则校验。
- 响应为每个 preview 文件返回 `file_id`；preview 文件复用 OSS multipart（init / sign-part / complete，按 `file_id`），签名目标为 process 桶的服务端生成 key。
- 不带 `client_admission` 的声明即旧链路，接口行为不变。

### 4.3 Admission worker：客户端预检模式

触发条件：source 带 `client_admission`，且 `enabled`、版本在白名单内。

1. HEAD raw MCAP 与每个 preview 对象：大小与声明一致、etag 存在；不下载 MCAP。
2. 报告结构校验：`issues` 每项含 `severity`、`code`；服务端按 `non_blocking_issue_codes` 重新判定 `integrity_status`，不采用客户端的 `ok`。
3. 下载 preview 小文件到 scratch，用现有 `_preview_artifacts_valid` 规则验证，唯一差异是 `source` 指纹与**声明的** `data_mcap_sha256`、`metadata_sha256` 比较（不再对本地 MCAP 计算）。
4. `metadata.json`（声明原文）与 `admission-report.json` 写入 process 桶。
5. 写 fact：`integrity_source="client"`、`objects_json`（§4.5）、`report_ref` 中保留客户端报告与服务端判定结果。

### 4.4 回退到服务端模式

以下任一情况，当次尝试改走完整服务端链路（下载 MCAP、校验、生成 preview），回退原因写入报告 `client_admission_fallback`：

- 版本不在白名单或 `enabled=false`；
- 报告缺字段或无法解析；
- preview 对象缺失、大小不符或验证不通过。

服务端判定 `integrity=failed` 时**不回退**，按现有逻辑写 failed 事实。服务端模式同样产出 §4.5 的对象清单，不再打 tar，`integrity_source="server"`。

### 4.5 对象清单与数据模型

`EpisodeAdmissionFact` 用 `objects_json` 取代 `source_objects_json` 与 `process_ref_json`，新增 `integrity_source`（`client` / `server`）。直接新写 alembic 迁移，不迁移旧数据。

```json
[
  {"path": "data.mcap", "role": "raw", "ref": {"bucket_role": "raw", "object_key": "...", "version_id": "...", "etag": "...", "size_bytes": 3188624643, "sha256": "..."}},
  {"path": "metadata.json", "role": "process", "ref": {...}},
  {"path": "admission-report.json", "role": "process", "ref": {...}},
  {"path": "media/preview/manifest.json", "role": "process", "ref": {...}},
  {"path": "media/preview/generations/<id>/head_rgb.mp4", "role": "process", "ref": {...}},
  {"path": "media/preview/generations/<id>/head_rgb.timeline.json", "role": "process", "ref": {...}}
]
```

`path` 即 QRDF episode 目录内的相对路径；按清单下载即还原标准 episode 目录。

### 4.6 下游改动

| 位置 | 改为 |
| --- | --- |
| `routers/collection_packages.py`、`routers/episodes.py` preview 播放 | 从 `objects_json` 取 `media/preview/` 对象 |
| `services/data_assets.py` 快照 | 冻结 `objects_json`；要求含 `metadata.json`、`admission-report.json` |
| `services/catalog_export_jobs.py` 导出 | 按清单下载到 episode 目录，删除 tar 解压 |
| `services/fetch_manifests.py` | 列出清单对象，不再附 tar |
| `_record_verified_source_artifact`、`derived_preview_batch`、`legacy_preview_migration` | 改读清单；`legacy_preview_migration` 若只服务历史 tar 数据则删除 |
| `integrations/qrdf/admission.py` | 删除 tar 打包与 `_normalize_ego_metadata`（见 §6） |
| `docs/COLLECTION_UPLOAD_API.md` | 更新声明字段、capabilities；改正入库审核只要求 integrity（`31b0be9` 起） |

### 4.7 多模态页相机按 metadata 流声明显示（启用该页前完成，不在本期范围）

QuicEgo 的佩戴位置可选 `head`、`chest`、`wrist`、`handheld`、`stand`（quic_ego `feature/qrdf-standard-capture-metadata`），topic 随之为 `/camera/<mount>/rgb` 等。`multimodal_preview.py` 的 `CAMERA_STREAM_DEFS` 只有 `front`、`left_wrist`、`right_wrist` 三个写死槽位，除 `head` 外的 EGO 录制不会匹配到相机槽位。

目前前端不调用任何 multimodal 接口（`/episodes/{id}/multimodal/*`、`/annotate/{task_id}/multimodal/*` 均未使用），工作台时间轴只读 metadata 的 `reference_topic`；入库、校验、preview 生成、参考相机也不受影响。因此本期不改，启用多模态页前按下述方式完成：

- `resolve_camera_streams` 优先读 `metadata.devices[].streams[]` 的 `camera_name`、`rgb_topic`、`depth_topic`，每个声明的流一个槽位，标签按 `camera_name`（`head` 头戴、`chest` 胸前、`wrist` 手腕、`handheld` 手持、`stand` 支架，未知名称原样显示）。
- metadata 没有流声明的历史数据，继续按现有 `CAMERA_STREAM_DEFS` 匹配。
- `_resolve_reference_topic` 优先取第一个声明流的 `rgb_topic`，再走现有兜底。

## 5. Duance 改动

现状：上传路径（`qrdf_archive.direct_source_for_episode`）只检查文件齐全、`recording_status` 与 SHA-256。`complete`、历史 `completed` 和缺省状态在 start < end 时用 metadata，即使结束时间比可读 MCAP 最后一条消息早；结束时间缺失、为 0 或空、小于等于开始时间时，须用户确认后才用 MCAP 实际范围覆盖 metadata。`interrupted` 在结束时间早于最后一条消息时同样须确认。确认后先备份到 `.qrdf-repair/`，再写回起止、`duration_s` 和 `timing_correction`，然后按写回后的 metadata 声明。秒数显示相同时，确认文案写明具体差值。用户拒绝或推断失败则该 episode 不能声明。关闭本地预检或 Studio 不接受时不做这次确认、不改文件，未修复的 `interrupted` 不上传。不调用 `QRDFValidator`；本地 EGO QC（`run_folder_ego_qc`）与上传脱钩。因此本地校验是新增能力，不是迁移已有逻辑。Studio 的声明校验不放宽：起止必须严格等于 metadata 且 start < end。

- **开关**：同步窗口“本地生成 Preview 与校验”，默认开启，保存在配置 JSON。
- **能力协商**：上传前读 `client_admission`；不接受时提示原因并走旧链路。
- **本地流程**（每个 episode，上传前）：校验 → 必要时修复 → 阻断判定 → 通过时生成或复用 preview；新增“本地校验 / 生成预览”进度阶段。一个 episode 的本地失败不停止对其余 episode 的检查，也不跳过它们的数据。
- **修复确认**：校验出现 `MCAP_UNREADABLE` 且该 episode 仍有上传资格时调用 `scan_mcap`，列出“可恢复消息数、可恢复时长、丢弃 chunk 数、是否截断”，用户确认后 `repair_episode`，再重新校验。拒绝、修复失败或没有可恢复消息时不改文件，按旧路径上传。`in_progress` 与 MCAP 可读的 `interrupted` 不提示修复。
- **上传资格**：本地预检开启且 Studio 接受时，`complete`（兼容历史 `completed` 与缺省）和所有 `interrupted` 都上传。损坏的 `interrupted` 先询问修复，拒绝、失败或没有可恢复消息时走旧路径；MCAP 可读且没有修复标记的 `interrupted` 直接走旧路径（不带 `client_admission`、不传 preview、不提示修复、不改 data.mcap）。已修复的 `interrupted` 可以走客户端预检。仅 `in_progress` 不声明。这条旧路径在 metadata 的 `end_timestamp_ns` 缺失、为 0 或空、小于等于 start 时，若能从 MCAP 索引得到范围，先弹窗确认（列出原起止、新起止和时长变化；秒数显示相同时写明具体差值，至少 1 毫秒用毫秒，更小用纳秒；确认绑定 metadata.json 与 data.mcap 的 SHA-256，文件变化后重新确认，拒绝过的组合不再询问）。`interrupted` 的结束时间早于可读 MCAP 最后一条消息时同样弹窗。`complete`、`completed` 与缺省在 start < end 时按 metadata 声明、不弹窗，即使只早 1 ns。用户确认后把原 metadata.json 备份到 `.qrdf-repair/<UTC>/metadata.json.orig`（不参与上传），再写回起止、`duration_s` 和 `timing_correction`，不改 `recording_status`、不改 data.mcap，然后按写回后的 metadata 声明。用户拒绝，或索引失败且没有可用 metadata 范围，则该 episode 不声明、不改文件并带原因，同包其余 episode 照常声明上传。已经覆盖最后一条消息的 metadata 范围保持不变、不弹窗。关闭开关或 Studio 不接受客户端预检时与旧版一致：不做时间确认、不改文件；未修复的 `interrupted` 不上传，关闭开关时原因是「录制中断且未修复，关闭本地预检时不上传」，开关仍开但 Studio 不接受时原因是「录制中断且未修复，当前 Studio 不支持本地预检时不上传」。声明前 Duance 按本服务的 `build_duance_import_manifest` 自检，预计会被拒绝的 episode 不声明，避免整包 422。服务端声明规则不放宽。
- **声明**：预检通过的 episode，`declaration_for_episode` 附 `client_admission`；preview 文件按服务端返回的 `file_id` 上传。本地阻断、拒绝修复、媒体检查报错，以及 MCAP 可读且无修复标记的 `interrupted`，不带 `client_admission`、不声明 preview。只要本包有客户端预检声明，所有分片带 `Content-MD5`（MCAP 分片同样加）。
- **缓存**：`generate_rgb_previews` 已按指纹复用，中断重传不重复编码。
- **依赖**：qrdf 固定到含 `feature/preview-hw-encoder` 与 §6 的版本。

## 6. EGO 别名（qrdf 已完成，`6ab1986`）

- qrdf `detect_episode_type()` 把 `human_demonstration`、`human_ego_demo`（去除首尾空白后比较）按 `human_ego` 路由；校验、指标、导出走 EGO 分支，metadata 中原值不改写。这调整了 `6fe16a9` 中“历史值不作 EGO alias”的规范，规范文档已同步。
- 结果：客户端上传的 metadata 原文、preview 指纹、服务端校验对象三者一致，两端都不再改写 `metadata.json`。
- Studio 删除 `_normalize_ego_metadata` 与 `_EGO_EPISODE_TYPE_ALIASES`，依赖升级后的 qrdf。

## 7. 已完成（qrdf `feature/preview-hw-encoder`，`d41392c`、`6ab1986`）

- Preview 硬件编码：auto 依次 NVENC、QSV(low_power)、VideoToolbox、libx264，按实际分辨率打开失败即回退；显式码率；QSV 最小 PTS 步长 12 µs；manifest 记录请求策略与每路实际编码器。
- 真实数据（10 min，9137 帧 JPEG 960×720）：NVENC 50.0 s、QSV 50.7 s、libx264 102.9 s；三者均通过 Studio 的 preview 验证规则（指纹、帧数、faststart、H.264/yuv420p）。
- `qrdf.repair`：1 GiB 截断文件扫描 1.3 s、修复 3.8 s，修复后校验与 preview 生成通过。

## 8. 错误处理

| 情况 | 处理 |
| --- | --- |
| 服务端不接受客户端版本或总开关关闭 | Duance 不做本地生成，也不做时间确认、不改文件，走旧链路。未修复的 `interrupted` 不上传：关闭开关时原因是「录制中断且未修复，关闭本地预检时不上传」；开关仍开但 Studio 不接受时原因是「录制中断且未修复，当前 Studio 不支持本地预检时不上传」 |
| 本地阻断错误（非 `MCAP_UNREADABLE`） | 该 episode 不带 `client_admission`、不传 preview，按旧路径上传；显示错误码。服务端复检，integrity 失败则审核时 `excluded`（`integrity_failed`） |
| `MCAP_UNREADABLE` 且用户拒绝修复 | 文件保持原样，按上一条旧路径上传 |
| 修复无可恢复消息或修复失败 | episode 保持原样，按旧路径上传 |
| 本地预检开启时，旧路径结束时间缺失、为 0 或空、≤ start | 能从 MCAP 索引得到范围时，弹窗确认后才备份并写回 metadata（含 `timing_correction`），再按写回结果走旧路径声明。秒数显示相同时写明具体差值。用户拒绝，或索引失败且没有可用 metadata 范围，则该 episode 不声明、不改文件，界面显示原因；同包其余 episode 照常上传。`complete`、`completed` 与缺省在 start < end 时按 metadata 声明、不弹窗，即使结束时间早于最后一条消息。确认绑定 metadata.json 与 data.mcap 的 SHA-256。关闭开关或 Studio 不接受时本行不执行 |
| MCAP 可读且无修复标记的 `interrupted` | 本地预检开启时：不带 `client_admission`、不传 preview、不提示修复、不改 data.mcap，按旧路径上传。时间范围按上一行，并且结束时间早于最后一条消息时也须用户确认后才写回 MCAP 实际范围；拒绝或推断不出则该 episode 不声明并带原因。关闭开关或 Studio 不接受时不上传、不改文件，原因见第一行 |
| `in_progress` | 不声明、不改文件、不提示修复、不生成 preview，界面显示「录制未结束，本次未声明、未上传」 |
| 本地媒体检查报错 | 该 episode 不声明 preview，不带 `client_admission` |
| 本地 preview 生成失败 | 该 episode 不带 `client_admission`，服务端生成 |
| 分片 MD5 不符 | 存储端拒收，Duance 重传该分片 |
| 服务端 preview 验证不通过 / 报告无效 | 自动回退服务端模式，记录原因 |
| 服务端重新判定 integrity 为 failed | 写 failed 事实，不回退 |

## 9. 测试与验收

**Studio**

- 声明：带 / 不带 `client_admission` 均可创建；`files[].path` 越界、重复被拒；preview 文件签名目标为 process 桶。
- Worker 客户端模式：不调用 MCAP 下载（provider 桩断言）；issue 由服务端规则判定（客户端 `ok=true` 但含阻断 code 时判 failed）；fact 含 `integrity_source=client` 与完整清单。
- 回退：版本不在白名单、preview 指纹不符、preview 对象缺失、`accept_client_admission=false` 四种情况都走服务端模式并记录原因。
- 服务端模式不再产出 tar；清单与客户端模式结构一致。
- 下游：preview 播放、快照、导出（按清单还原目录后可被 qrdf `Episode` 打开）、fetch manifest 各有用例；删除 tar 相关测试。
- 迁移：新 alembic 迁移可在空库升级。
- 真实字节回归：`test_collection_upload_bytes.py` 扩展客户端预检路径。

**Duance**

- 开关关闭或服务端不接受：声明不含 `client_admission`，不做时间确认、不改文件，与旧版一致。未修复的 `interrupted` 不上传，原因分别为「录制中断且未修复，关闭本地预检时不上传」和「录制中断且未修复，当前 Studio 不支持本地预检时不上传」。
- 阻断判定使用服务端下发的 code 表。混合包（一个通过 + 一个阻断，或一个通过 + 一个拒绝修复）整体上传，只有通过的 episode 带 `client_admission` 与 preview。
- 修复流程：扫描结果展示、确认后修复并复检；拒绝、失败或没有可恢复消息则原文件按旧路径上传。MCAP 可读且无修复标记的 `interrupted` 直接按旧路径上传，文件不变、无修复提示、无 preview、无 `client_admission`。仅 `in_progress` 不声明。每个 episode 都会被检查。
- `interrupted + repair` 可走客户端预检。
- 分片带 `Content-MD5`；preview 已存在且指纹匹配时不重新编码。

**端到端验收（company-pc + UAT Studio）**

- 用 10 分钟真实 episode 走客户端预检：服务端不下载 MCAP，数据包进入可审核状态，审核页可播放 preview。
- 同一 episode 关闭开关上传：走服务端模式，结果清单结构一致。
- 截断 episode：本地确认修复后上传成功，审核页可见修复标记。

## 10. 发布顺序与兼容

1. qrdf：合并 `feature/preview-hw-encoder`（含 §6），发布版本。
2. Studio：部署 capabilities、声明、worker 双模式与下游清单改动。
3. Duance：升级 qrdf 依赖并接入客户端预检。

新 Duance 连旧 Studio：capabilities 无 `client_admission`，自动走旧链路。旧 Duance 连新 Studio：声明无 `client_admission`，服务端模式处理，不再产出 tar。
