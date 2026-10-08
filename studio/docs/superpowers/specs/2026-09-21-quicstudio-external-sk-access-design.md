# QuicStudio 外部工具接入规格（长期令牌 · 离线上传 · 算法标注）

> 依据规格：[三桶与逻辑资产](../specs/2026-09-18-quicstudio-object-storage-logical-assets-design.md)、
> [数据后端补全](../specs/2026-09-18-quicstudio-data-backend-completion-design.md)、
> [init 采集运维与数据治理](../specs/2026-09-14-collection-studio-init-design.md)。
> 本规格同时覆盖平台侧（`quic_studio`）与客户端侧（`duance` / `quic-data-toolchain`）。
> 领域层级以 init 规格为准：`Workspace → CollectionProject → CollectionTask → DataPackage → Episode`，
> 旧 `task_set` / `batch` 命名与入口不再对外暴露。

## 1. 目标与适用范围

让外部工具用**绑定到人的长期凭据**安全地调用平台：

1. **采集/运维工具（duance）**：离线优先地完成「下载采集任务清单 → 离线绑定文件与数据包 → 联网断点上传 → 入库」，
   不再依赖浏览器式会话（密码 + cookie），也不再挂在旧的 Batch 上。
2. **算法批跑标注**：用同一枚凭据领取标注工作项、批量下载所需原始数据、批量提交标注结果，并进入现有审核链路。
3. **数据获取**：算法按数据包 / 采集任务 / 数据批 / 自己的工作项维度，一次性拿到含签名 URL 的取数清单，
   不需要平台产生任何物理副本。

本规格不改变业务层级与审核语义，只补齐「对外凭据」「离线上传契约」「算法取数与写入」三件事，
并顺带完成三桶存储收敛（只允许 MinIO / 阿里云 OSS）。

## 2. 已确定的决策

| 主题 | 决策 |
|---|---|
| 凭据主体 | 凭据**绑定到人**，权限完全继承该用户的角色与工作空间成员关系；v1 绑定 Admin，后续可切到素材运维角色 |
| 凭据形态 | **Bearer 长期令牌**（`qs_<key_id>_<secret>`），HTTPS 直传 header；不做请求签名 |
| 标注写入路径 | **工作项领取/状态沿用现有接口，写入走批量提交**（幂等 + 只见该账号已领取的工作项 + 记录算法版本） |
| 数据获取 | **批量取数清单**（对象清单 + 签名 URL + 校验和 + Episode 元数据），不打包、不复制 |
| 算法产出 | 记录 `source=algorithm`（算法名/版本/运行 ID/置信度）；**默认必须人工复核**，提交时可带 `review_required` 参数 |
| 复核开关权限 | `review_required=false` 需该用户具备 `annotation:approve`（Admin 满足），否则降级为需要审核并写审计 |
| 上传清单 | 现有采集任务清单（CSV/JSON）**扩展为包级行**，离线可下载，作为离线期间的唯一契约 |
| 离线绑定粒度 | **包级清单 + 目录约定**：平台不预设 Episode 数量，工具按 `<package_uid>/<episode>/` 放一组文件，一组 = 一个 Episode |
| 脱敏声明 | **包级声明 + 责任人**；v1 只记录不门禁，预留工作空间开关，脱敏落地后打开即为硬门禁 |
| 令牌治理 | 具名多枚、可设过期（默认 90 天）、吊销、轮换（新旧并存 1 小时）、每令牌限流、全量审计；v1 不做 IP 白名单 |
| 客户端凭据 | GUI 保留密码登录，新增令牌模式；**令牌 > 会话 cookie**；凭据存配置目录 |
| 客户端存储 | 离线绑定与断点状态改用 **SQLite**（`.duance/state.db`），凭据用**独立**库（配置目录 `credentials.db`） |
| 落地方式 | **方案 1**：复用现有接口 + 三处新增（凭据层、取数清单、标注批量提交） |

事实依据：`collection_upload_parse` 按 `package_uid` 分组处理声明并**逐包推进状态**，
因此一个会话可承载多个包，某包失败不阻塞其他包。

## 3. 架构总览

```
外部工具（duance / 算法）
     │  Authorization: Bearer qs_<key_id>_<secret>
     ▼
平台凭据层（新增）── 解析为与 JWT 同形状的 principal（sub / role / 权限）
     │
     ├── 上传：/collection-upload-sessions（现有，未改协议）
     │        └── declarations 按 package_uid 分组 → OSS multipart 直传
     ├── 取数：/fetch-manifests（新增）
     ├── 标注：/annotation-work-items（现有）+ /annotation-work-items/batch-submit（新增）
     └── 清单：/collection-tasks/{id}/offline-manifest（现有导出扩展列）
```

- 现有 JWT 鉴权路径不变；令牌与 JWT 共用同一个 principal 解析入口，因此**现有接口对令牌自动可用**。
- 第 0 步先做存储收敛（第 9 节），使上传链路只剩 MinIO / 阿里云 OSS 两种 provider。

## 4. 凭据模型

### 4.1 数据

新表 `api_tokens`：

| 列 | 说明 |
|---|---|
| `id` | 主键 |
| `user_id` | 绑定用户（外键，非空） |
| `name` | 具名（如 `duance-运维机`、`算法批-A`），同用户内唯一 |
| `key_id` | 令牌公开标识（出现在 `qs_<key_id>_<secret>` 中） |
| `secret_hash` | 只存哈希，创建时明文仅返回一次 |
| `scopes` | 预留列（v1 不启用，恒为空表示继承用户权限） |
| `expires_at` / `last_used_at` / `revoked_at` | 生命周期 |
| `created_by` / `created_at` | 审计 |

### 4.2 鉴权

- 请求头 `Authorization: Bearer qs_<key_id>_<secret>`。
- 校验顺序：格式 → key_id 存在 → 未吊销 → 未过期 → secret 哈希匹配 → 取出绑定的 user。
- 解析结果与 JWT 路径返回**同形状的 principal**（`sub` / `role` / 权限集合），因此所有现有接口与工作空间校验逻辑无需改动。
- 每个业务请求更新 `last_used_at`（可采样，避免写放大）。
- 吊销即时生效，解析缓存上限 30 秒。

### 4.3 治理

- **自助签发**：令牌是账户密码的替身——任何已登录用户都可以**给自己**签发、列出、轮换、吊销令牌；
  不接受为他人签发，也看不到/管理他人的令牌（管理员越权吊销等能力后续按需再加）。令牌的调用权限完全跟随本人。
- **有效期**：默认 90 天，签发人可自选更短或不过期（上限 3650 天）。
- **轮换**：`POST /api/v1/tokens/{id}/rotate` 生成新 secret，旧 secret 在 1 小时内继续有效，随后自动失效；旧记录保留审计。
- **吊销**：`DELETE /api/v1/tokens/{id}`；吊销后所有使用立即被拒（缓存窗口内最多 30 秒）。
- **限流**：按令牌独立限流，默认 600 请求/分钟（可配置）；超限返回 429 且不重试。
- **审计**：记录创建、使用（令牌名、用户、IP、UA、路由）、吊销、轮换；不记录 secret 与请求体中的敏感字段。
- **日志**：secret 永不进入应用日志与错误信息。

## 5. 上传链路（离线优先）

### 5.1 离线清单契约

现有采集任务清单（CSV/JSON）扩展为**包级行**，新增列：

| 列 | 说明 |
|---|---|
| `package_uid` | 数据包标识（上传与绑定主键） |
| `collection_project` / `collection_task` | 采集项目 / 采集任务 |
| `modality` / `target_duration_hours` | 模态与目标时长 |
| `expected_files` | 期望文件类型与命名约定（如 `*.mcap` + `metadata.json`） |
| `required_metadata` | 必需元数据字段（采集员、设备 SN、采集时间） |
| `upload_status` | `pending_upload / uploading / parsing / ingested / pending_intake_review / intake_approved / …` |
| `desensitization_status` | `declared / unknown / not_applicable` |
| `manifest_revision` | 清单版本，用于离线对账与冲突检测 |

清单在**采集前联网时下载**，离线期间是工具与平台之间唯一的契约。

### 5.2 绑定约定

- 目录约定：`<package_uid>/<episode 目录>/{data.mcap, metadata.json}`。
- **一组文件 = 一个 Episode**；平台不预设 Episode 数量与序号。
- 平台在解析时按声明逐组建 Episode、逐个校验（缺失/损坏/校验和不符只排除该 Episode，不影响同包其他 Episode）。
- 工具在本地维护「包 ↔ 文件组 ↔ 上传状态」绑定（见第 8 节 SQLite schema）。

### 5.3 上传流程

1. `POST /collection-upload-sessions` 建会话（可含多个包）。
2. `POST /collection-upload-sessions/{id}/declarations` 提交声明，**按 `package_uid` 分组**。
3. 对象存储 multipart 直传：`oss/init` → `oss/sign-part`（逐分片签名）→ `oss/complete`；API 不接收文件字节。
4. 解析与入库：按包独立推进状态；包内失败项保留原因，不影响其他包与其他 Episode。

### 5.4 断点与幂等

- raw 对象以 SHA-256 为身份：同一对象重复上传不产生重复 Episode。
- 中断后恢复 = 重新 init 分片 + 只补传未完成分片；已 complete 的包重复提交幂等。
- 会话与分片的过期重签不影响已上传分片。

### 5.5 脱敏声明

- 包级字段 `desensitization = { status, by, tool, at, policy_version }`。
- v1：`status` 记录但不作为门禁；`unknown` 表示未声明，**不得被展示为已脱敏**。
- 预留工作空间配置 `require_desensitization_declaration`；打开后，未声明的包不允许通过入库审核。

## 6. 取数链路

### 6.1 接口

`POST /api/v1/fetch-manifests`，body：

```json
{
  "workspace_id": 1,
  "scope": "data_packages",
  "ids": [5101, 5102],
  "url_ttl_seconds": 3600,
  "oss_network": "internal"
}
```

> **2026-09-22 接口修订（用户确认，待实现）**：去掉 `include`，固定返回完整清单；保留 TTL，默认一小时；支持内网/公网 OSS 选择，默认内网。下方原验收记录不代表此修订已经通过验证。

| 字段 | 约定 |
| --- | --- |
| `scope` | `data_packages` / `collection_task` / `data_batch` / `my_annotation_work_items`，决定 `ids` 的资源类型 |
| `url_ttl_seconds` | 可选，默认 `3600`；正整数，按服务端/provider 支持范围校验，不支持的有效期明确拒绝 |
| `oss_network` | 可选，`internal`（内网 OSS）/ `public`（公网 OSS），默认 `internal` |

返回：`manifest_version`、`expires_at`、以及逐对象条目——`episode_uid`、对象 key、`size`、`sha256`、`mime`、
**签名下载 URL**、Episode 元数据（时长、话题、`task_label`、采集员/设备）。固定返回授权 scope 内的完整来源文件清单和元数据，不只选择每个 Episode 的第一个 artifact；不提供 `include` 裁剪选项。

### 6.2 规则

- 只签名、不打包、不复制对象（与「无物理副本」规格一致）。
- `oss_network` 只选择服务端配置的内网或公网 OSS endpoint；不接受客户端指定任意 endpoint、桶或对象 key。按选定 endpoint 直接签名，签名后不得替换域名。内网签名面向可访问该内网 endpoint 的工具，不套用浏览器仅允许公共 endpoint 的限制；客户端需公网下载时显式传 `public`。
- 所选网络的 endpoint 未配置时返回明确错误，不自动改用另一网络。签名有效期采用请求 TTL，省略时为一小时；返回准确的过期时间，不静默改用其他 TTL。
- 范围受令牌绑定用户的可见工作空间限制；`my_annotation_work_items` 进一步收窄到「已分配给该用户的工作项」。
- `data_batch` 与 `my_annotation_work_items` 按建批时冻结的有效 Episode 清单取数。当前业务入口不开放同包多次建批，模型保留后续扩展能力；包级归属不等于允许取出包内被排除或未审核的 Episode。“完整返回”不扩大 scope 或用户权限。
- 清单可重复请求（幂等）；签名 URL 过期前可重新拉取续签。
- 签名 URL 只读、只对清单内对象有效；请求他人工项数据返回 403。

## 7. 标注链路

### 7.1 领取与暂存（沿用现有）

标注工作项**由管理员派发**（`assignee_user_id`），没有自助领取的概念。算法用令牌调用现有
`GET /annotation-work-items`、`PATCH /annotation-work-items/{id}`（草稿）等接口，只能操作派发给本账号的工作项，状态流转语义不变。

### 7.2 批量提交（新增）

`POST /api/v1/annotation-work-items/batch-submit`：

```
{
  "workspace_id": 1,
  "client_request_id": "run-2026-09-21-001",
  "items": [
    {
      "work_item_id": 7001,
      "payload": {
        "episodes": {
          "101": { "kind": "episode_annotation", "qrdf_version": "0.2.0", "source": { "episode_id": "<Episode 101 的 QRDF 标识>", "data_sha256": "<来源 SHA-256>" }, "episode": { "outcome": "unknown" }, "tracks": [] },
          "102": { "kind": "episode_annotation", "qrdf_version": "0.2.0", "source": { "episode_id": "<Episode 102 的 QRDF 标识>", "data_sha256": "<来源 SHA-256>" }, "episode": { "outcome": "unknown" }, "tracks": [] }
        }
      },
      "source": { "kind": "algorithm", "name": "ego-vl", "version": "1.3.0", "run_id": "run-001", "confidence": 0.82 },
      "review_required": true
    }
  ]
}
```

规则：

- 仅允许提交**管理员派发给该令牌用户**的工作项（`assignee_user_id` 等于令牌绑定用户），否则 403。
- 工作项按包派发，`payload.episodes` 必须完整覆盖该工项所需的冻结 Episode；key 为平台 Episode ID 的字符串，值为合法 `QrdfEpisodeAnnotation`。上例仅展示结构，尖括号占位值须替换为真实来源身份/hash；不能直接照抄为有效标注。缺失、额外 Episode 或空 payload 明确拒绝。
- `client_request_id` 按工作空间和调用用户隔离：同键同规范化请求返回首次结果；同键不同 items/payload/source/review_required 返回 409。持久占位、标注写入与结果提交原子协调；并发重放不产生重复快照、审计或发布，整批失败回滚后可重试。以上为本次修订要求，原验收记录不代表已覆盖。
- 已提交或已审核的工作项不可覆盖，冲突返回 409 并保留历史（结合工项状态、版本与事务校验；当前接口不因此新增未经定义的版本字段）。
- `review_required` 默认 `true`；传 `false` 需该用户具备 `annotation:approve`，否则**降级为 true** 并写审计。
- 需要复核的提交进入「待审核」，审核通过后按现有链路进入资产与数据集。有 `annotation:approve` 权限且明确传 `review_required=false` 时，标注校验与修订保存完成后该工作项直接完成，并记录免审操作者及依据，不再等待人工审核。同批其他工作项仍按各自流程完成，全部启用流程结束后才可生成资产；不能因单项免审提前发布整个批次。此免审状态流转为 2026-09-22 确认的待补实现，不能只记录或返回开关值。

### 7.3 来源与复核

- 工作项与其标注快照记录 `source`（`algorithm` / `human`）与置信度。
- 审核界面可按「仅算法标注」「低置信度（阈值可配）」筛选。
- 审核不通过退回时，退回到该工作项的原处理人（算法账号），并可携带原因供算法侧重跑。

## 8. 客户端（duance）改造

### 8.1 凭据

- 新增令牌模式：GUI 的「登录」改为「凭据（令牌 / 账号密码）」，优先级 **令牌 > 会话 cookie**；两者并存，人工临时操作仍可用密码。
- 令牌存配置目录 `credentials.db`（0600），字段：`name / secret(加密或钥匙串引用) / issued_at / expires_at / backend`。
- 预留 `credential_backend = file | keychain`，v1 实现 `file`；切换后端不影响调用方。
- 令牌与密码都不写入日志；`credentials.db` 位于配置目录，**不随采集数据目录迁移**。

### 8.2 本地状态（SQLite）

每个采集目录一份 `.duance/state.db`（`sqlite3` 标准库，无新依赖）：

```
meta(schema_version, manifest_revision, updated_at)
packages(package_uid PK, task_id, project_id, target_hours, modality, status, manifest_revision)
episode_files(id PK, package_uid, episode_slug, rel_path, size, sha256, upload_state, updated_at)
upload_attempts(id, package_uid, session_id, part_state, updated_at)
```

- 一次性从 `sync-state.json` 迁移；旧 JSON 保留只读一版兼容后废弃。
- 在可移动介质上按目录探测选择 journal 模式，避免掉电损坏。
- `folder-summary.json` / `collector-summary.csv` 保持不变（人可读产物）。

### 8.3 同步目标与交互

- 同步窗口目标：**工作区 → 采集项目 → 采集任务 → 数据包（多选）**；去掉任务集与 Batch 选择。
- 导入平台离线清单（CSV/JSON）后建立本地绑定；界面显示体检结果：未绑定文件、缺必需元数据、缺脱敏声明、与清单 `manifest_revision` 不一致。
- 上传时一次同步建一个会话、声明按包分组；逐包显示状态并支持部分重试。
- 包级脱敏声明可离线勾选，随声明上传。

## 9. 第 0 步：存储收敛与旧链路清理

1. `storage_mode` 收敛为只允许 `minio | aliyun_oss`；非法值在启动时失败，删除 `local / cloud / hybrid` 归一与降级分支。
2. 旧导入调用者（前端「导入数据」入口与 duance 的 Batch 同步）切到新链路后，删除 `batches / imports / duance_imports` 路由及其服务与旧契约测试。
3. 工作队列与 Episode 归属以 `data_package_id` 为唯一采集路径；`task_set + batch` 作为遗留路径，在调用者清空后随迁移下线。
4. 删除代码前先确认调用者清单为空（路由、任务注册、前端调用、客户端调用、测试依赖），替换旧语义测试为新契约测试。

## 10. 错误处理与边界

| 场景 | 结果 |
|---|---|
| 令牌格式错误 / 已吊销 / 已过期 | 401（错误体区分 `invalid_token` / `revoked` / `expired`） |
| 令牌绑定的用户已停用或失去工作空间成员资格 | 401/403，并写审计 |
| 跨工作空间取数、提交他人工项 | 403 |
| `review_required=false` 但无 `annotation:approve` | 降级为需要审核 + 审计，返回可解释字段 |
| 工作项版本冲突 / 已提交或已审核 | 409，返回当前状态 |
| 幂等键重复且内容一致 | 200，返回首次结果 |
| 签名 URL 过期 | 重新请求取数清单；不返回 5xx |
| 清单声明的文件缺失 / 大小或 SHA-256 不符 | 仅排除该 Episode，原因留在包内；其他 Episode 继续 |
| 存储 provider 非法 | 启动失败并给出明确配置错误 |

## 11. 验收标准

> **验收结果（2026-09-22 收尾）**：1–7 均按下面的证据逐条核对。真实 MinIO 端到端由
> `backend/tests/test_external_tool_e2e.py` 跑通（`1 passed`）；阿里云真实 smoke 未执行，
> 因此不计入通过项（第 7 条要求区分报告）。

1. **凭据**：令牌签发、列出、吊销、轮换（新旧并存 1 小时）、默认过期、独立限流、审计落地；撤销后 30 秒内生效。
   - 通过：`tests/test_api_tokens_service.py`、`tests/test_api_tokens_api.py`、`tests/test_token_auth_principal.py`；E2E 另以自签令牌完成整条链路。
2. **离线上传**：清单导出 → 离线绑定 → 断网中断 → 恢复续传 → 数据包进入「待入库审核」；部分包失败不影响其他包；重复上传不产生重复 Episode。
   - 通过：`tests/test_collection_task_manifest_export.py`、`tests/test_collection_upload_parse.py`、duance `tests/test_state_store.py`（断点状态）、E2E（真实 MinIO 上传 → `pending_intake_review`）。
3. **取数**：清单中签名 URL 可直接下载且 `size` / `sha256` 一致；过期可续签；越权取数 403。
   - 通过：`tests/test_fetch_manifests_api.py`（含越权 403、幂等）；E2E 校验 `size`/`sha256` 与对象 key。签名 URL 需要公开 HTTPS 端点，本机 loopback MinIO 按设计返回 `available:false`，故 E2E 在 `available` 为真时才下载校验（未伪造可用性）。
4. **标注**：批量提交幂等、冲突 409、`review_required` 权限校验、来源与置信度落库、审核界面可筛「仅算法标注 / 低置信度」。
   - 通过：`tests/test_annotation_batch_submit_api.py`、E2E（同一 `client_request_id` 两次提交同一条目、无审批权限时降级并写入 `annotation.review_skip_downgraded` 审计）。
5. **存储**：只接受 `minio` / `aliyun_oss` 两种 provider；旧路由删除后前端与客户端均无调用。
   - 通过：`tests/test_storage_provider_convergence.py`；任务 10 删除 `batches/imports/duance_imports` 路由与前端导入入口，`rg` 校验前端无残留调用。
6. **客户端**：`sync-state.json → state.db` 迁移正确；令牌模式免登录完成一次完整同步；目标切换为采集项目/采集任务/数据包后无 Batch 残留。
   - 通过：duance `tests/test_state_store.py`、`tests/test_token_credentials.py`、`tests/test_sync_dialog.py`、`tests/test_quicdata_client_packages.py`（路径断言）；工作区无 `task_set/batch` 交互残留（仅保留迁移兼容键）。
7. **回归**：后端单元/API、前端回归、duance 回归分开报告；MinIO 真实直传端到端至少跑一次；未执行的阿里云真实 smoke 不得记为通过。
   - 后端：`backend/tests` 982 passed / 10 failed（10 项与本次改动无关，已在干净 HEAD 基线复现：迁移回滚、SocketIO 端口、用户激活等环境相关用例）。
   - 前端：`node --test frontend/tests/*.test.mjs` 261 passed；`make demo-check` 22 个视图全部渲染。
   - duance：317 passed（含新增的同步窗口/清单导入/上传编排用例）。
   - MinIO 真实直传：`test_external_tool_e2e.py` 1 passed（真实对象落盘 + 真实 QRDF 校验）。
   - 阿里云真实 smoke：**未执行**，不计入通过。

## 12. 非目标

本轮不做：在线分发与外链分享、IP 白名单、算法自动通过白名单、系统钥匙串实现（仅预留接口）、
按接口细分的 scope、脱敏能力本身、训练侧实现、旧数据迁移。

## 13. 规格自检

- 占位符：无「待定」段落；所有决策已写明具体值（默认过期 90 天、缓存 30 秒、TTL 1 小时、限流 600 req/min）。
- 一致性：凭据绑定人 → 权限继承用户 → 算法复核开关依赖 `annotation:approve`，三处一致；取数与「无物理副本」规格一致。
- 范围：本规格可由一个实现计划覆盖，客户端（duance）改造作为其中的独立任务组。
- 模糊性：已明确「一组文件 = 一个 Episode」「一个会话可含多个包」「脱敏声明 v1 只记录不门禁」三处易歧义点。
