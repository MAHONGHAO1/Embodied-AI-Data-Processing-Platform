# 采集按日开包规格（Studio + Duance）

> 依据：2026-09-30 设计讨论。现场日产量波动，无法按「每天预期 8 小时」预拆数据包。
> 本规格取代 [init 采集运维与数据治理](2026-09-14-collection-studio-init-design.md) §64 的「按默认单包目标时长预生成待分配包」，
> 以及 [采集概览第一期](2026-09-29-collection-dashboard-data-board-design.md) §9 中与分配、拆包相关的修复。
> > 涉及两个仓库：`quic_studio`（本仓库）与 `duance`（采集工具链，`../duance`），两者同时发版。

## 1. 背景与目标

现状：任务创建时按「单包目标时长」一次性拆出全部包 → 管理员把包分配给采集员并锁定 →
Duance 由人勾选一个包、把 Episode 导入 `<本地目录>/<package_uid>/` → 上传。
包的目标时长同时充当「计划配额」和「交付单元」。日产量波动时，包装不满、装超、跨多天追加、
锁定的包压在采集员手里，按天统计也会失真（跨天的包整包计入开始那天）。

目标：

1. 采集员当天采多少交多少，不需要凑满包。
2. 按天、按人的产量统计准确。
3. 任务进度只看入库审核有效时长；达标时提示管理员结束任务。
4. 数据离线拷到电脑，联网后自动开包；上传、审核完成的包可以安全清理本地文件。

## 2. 决策

| # | 决策 | 理由 |
|---|---|---|
| D1 | 包的粒度为「任务 × 采集员 × 采集日 × 序号」，由客户端在上传前开包，不再预拆 | 包反映实际产出，日产波动只影响包的大小 |
| D2 | 任务目标时长是唯一目标；包不再有目标时长 | 进度按有效时长累计，与包数无关 |
| D3 | 采集日 = Episode 开始时间换算到 Asia/Shanghai 的日期 | 与看板默认时区一致；按采集时间而不是上传时间 |
| D4 | 包上只保留一个采集员字段 `collector_id`；取消负责人／操作员两个角色 | 两者已被强制为同一人（`one_collector_per_package_required`），双字段无意义。一个 Episode 多人参与下期再做 |
| D5 | 采集员标识（`metadata.operator.id`）匹配不到在职的工作空间人员档案时拒绝开包 | 人员统计完全可靠；数据挂起，由现场改正或管理员补建档案后再传 |
| D6 | 不要求显式结包；同一组合已有未冻结、未封口的包时返回该包（追加），否则开序号 +1 的续包 | 迟到的数据不丢、不拒收；审核员随时可审 |
| D7 | 新增可选的「封口」：封口后开包不再返回该包，但不拒绝上传。Duance 在本地审核锁定时封口 | Duance 支持先离线审核锁定、后上传；锁定的包不应再被追加 |
| D8 | 不做任务级分配：工作空间内在职的采集员都可以在进行中的任务下开包。取消包分配、批量分配 | 现场调人灵活；「谁在做」由实际开出的包反映 |
| D9 | 任务新增状态「进行中／已结束」，由管理员手动结束；达标只提示，不自动拦截 | 有效时长在冻结后才确定，自动拦截会误伤在途数据 |
| D10 | 看板的时间分桶仍用 `captured_started_at`，不改为 `capture_date` | 看板支持按小时粒度，`capture_date` 只到天；§5.3 保证包内 Episode 同属一个采集日，按包最早开始时间分桶即不再跨天失真 |
| D11 | 取消补采包、待分配包调整（拆分／合并／改时长）、离线清单 | 重采的数据自然进入之后的日包；没有预拆包和分配，也就没有清单 |
| D12 | 一次性切换，不保留旧模式；迁移只考虑 UAT 数据，不提供 downgrade | 尚未发版，只有 UAT 测试数据 |
| D13 | Duance 分「收件（离线）→ 绑定（联网自动开包）」两阶段；收件时由人选任务，采集员与采集日自动分组 | 现场一台设备基本不在一天内切换任务；拷贝不依赖网络 |
| D14 | 收件与绑定用移动而不是复制，但有安全规则（§8.2） | 复制太慢、太占空间 |
| D15 | Duance 提供「清理已完成的包」：上传完成、已冻结且逐 Episode 哈希与 Studio 一致时才可删除本地文件 | 移动后本地只有一份，需要安全释放空间；驳回的 Episode 原始数据仍保存在 Studio |

## 3. 端到端流程

| 步骤 | 联网 | 执行方 | 内容 |
|---|---|---|---|
| 1. 采集 | 否 | 采集 App | 元数据写入 `operator.id` 与开始时间 |
| 2. 收件 | 否 | Duance | 选任务 → 选源目录 → 分组预览并确认 → 移动到 `_inbox/` |
| 3. 绑定 | 是 | Duance → Studio | 先补发待发的封口；每组调用开包 → 把组内文件移到 `<package_uid>/` |
| 4. 本地审核 | 否 | Duance | 按包逐条审核；锁定时封口（离线则排队） |
| 5. 上传 | 是 | Duance → Studio | 沿用现有上传会话流程 |
| 6. 同步审核结论 | 是 | Duance → Studio | 解析完成后提交审核结论，包冻结 |
| 7. 清理 | 是 | Duance | 条件满足后删除本地文件 |
| — | 是 | Studio 管理员 | 任务达标后点「结束」，此后拒绝开包 |

## 4. Studio 数据模型

### 4.1 `collection_tasks`

- 新增 `status`：`active`（进行中）／`closed`（已结束），非空，默认 `active`；检查约束限定取值。
- 新增 `closed_at`（可空）、`closed_by_user_id`（可空，外键 `users.id`）。
- 删除 `default_package_duration_hours` 及其检查约束。`target_duration_hours` 保留。
- 模型注释改为：目标时长是唯一目标，包在采集时按日开出。

### 4.2 `data_packages`

- 删除 `responsible_collector_id`、`operator_collector_id`，新增 `collector_id`（外键 `personnel_profiles.id`）。
- 新增 `capture_date`（Date）、`sequence_no`（Integer，从 1 开始）、`sealed_at`（DateTime，可空）。
- 删除 `target_duration_hours` 及其检查约束。
- `assigned_at` 改名为 `opened_at`。
- 删除 `supplement_for_package_id`、`supplement_reason`。
- 状态：删除 `pending_assignment`、`assigned`，新增 `open`（已开包，待上传）。其余状态与流转不变。
  可上传状态改为 `open`、`parse_failed`、`pending_intake_review`。
- 约束：
  - `status = 'voided' OR (collector_id IS NOT NULL AND capture_date IS NOT NULL AND sequence_no IS NOT NULL)`，
    替换原 `ck_data_packages_assigned_requires_collectors`。
  - 唯一约束 `(collection_task_id, collector_id, capture_date, sequence_no)`，包含已作废的包，
    所以作废的序号不会被复用。
  - `sequence_no >= 1`。
- 索引：`(collection_task_id, collector_id, capture_date)` 由唯一约束覆盖；新增 `(workspace_id, capture_date)` 供看板使用。

### 4.3 「未冻结」与「可追加」

- **冻结**：包已有入库审核结论（`package_intake_reviews` 存在记录）。与现有定义一致。
- **可追加**：未作废、未冻结、`sealed_at` 为空。开包只返回可追加的包。

## 5. Studio 规则

### 5.1 开包

输入：任务、`collector_identifier`（`metadata.operator.id` 原值）、`capture_date`。

1. 任务不存在或不属于该工作空间 → 404 `task_not_found`。
2. 项目已归档 → 409 `project_archived`；任务已结束 → 409 `task_closed`。
3. `capture_date` 格式错误或晚于 Asia/Shanghai 的今天 → 422 `capture_date_invalid`。
4. 按 `capture_provenance` 的现有规则解析采集员（正整数字符串，按 `profile_key` 匹配工作空间人员档案，且档案在职）；
   缺失、`unknown`、格式非法、匹配不到或已停用 → 422 `collector_unmatched`，响应带上报原值。
5. 按任务行加锁串行化；取该组合下序号最大的可追加包，有则返回（`created=false`）。
6. 否则新建：`sequence_no = 该组合现有最大序号 + 1`（没有则为 1），`status=open`，`opened_at=now`，
   `package_uid=pkg_<uuid hex>`，返回 `created=true`。唯一约束冲突时重读并返回已存在的包。
7. 新建时写审计事件 `collection.package.open`。

### 5.2 封口

- 只对未作废、未冻结的包生效；设置 `sealed_at=now`。重复封口幂等，返回原 `sealed_at`。
- 已冻结的包封口直接返回成功（冻结已包含封口语义），不改动数据。
- 封口不影响上传：已封口的包仍可创建上传会话。「锁定后不再追加」由 Duance 本地保证。
- 不提供解除封口。

### 5.3 上传声明校验

两条上传链路（SDK／chunked 与 OSS 直传）在声明来源时都执行，对每个 Episode：

- 元数据 `operator.id` 解析出的人员档案必须等于包的 `collector_id`，否则记为 `episode_collector_mismatch`。
- `start_ns` 换算到 Asia/Shanghai 的日期必须等于包的 `capture_date`，否则记为 `episode_capture_date_mismatch`。

任一 Episode 不满足时，整个声明请求返回 422，`detail` 列出每个不匹配 Episode 的 `episode_id` 与错误码。

### 5.4 任务结束与重新开启

- 结束：`status=closed`，写 `closed_at`、`closed_by_user_id`，审计事件 `collection.task.close`。
- 重新开启：`status=active`，清空 `closed_at`、`closed_by_user_id`，审计事件 `collection.task.reopen`。
- 结束后，已开出的包照常上传、审核，只拒绝开新包。

## 6. Studio 接口

所有接口以 `/api/v1` 为前缀。

### 6.1 新增

- `POST /collection-tasks/{task_id}/packages/open`
  - 权限与创建上传会话相同（Duance、CLI 使用的 `qs_` Key 可调用）。
  - 请求：`{"workspace_id": 3, "collector_identifier": "42", "capture_date": "2026-09-30"}`
  - 响应：`{"data_package_id": 101, "package_uid": "pkg_…", "sequence_no": 1, "capture_date": "2026-09-30",
    "status": "open", "sealed_at": null, "collector": {"id": 7, "name": "张三"}, "created": true}`
- `POST /data-packages/{data_package_id}/seal`（请求体 `workspace_id`）：权限同上，返回包的 `sealed_at`。
- `POST /collection-tasks/{task_id}/close`、`POST /collection-tasks/{task_id}/reopen`：`workspace:write`。

### 6.2 变更

- `GET /collection-tasks`：
  - 新增查询参数 `status`。
  - 每项新增 `status`、`closed_at`、`target_reached`；有效时长沿用已有字段 `intake_valid_duration_hours`，
    `target_reached` 定义为 `intake_valid_duration_hours >= target_duration_hours`。
  - 删除 `pending_assignment_count`、`assigned_count`、`default_package_duration_hours`。
- `POST /collection-tasks`：删除 `default_package_duration_hours`，不再生成包。
- `GET /collection-tasks/{task_id}`、`/collection-tasks/{task_id}/packages`、`GET /data-packages`、`GET /data-packages/{id}`：
  包字段改为 `collector`（id、name）、`capture_date`、`sequence_no`、`sealed_at`、`opened_at`；
  删除 `responsible_collector_id`、`operator_collector_id`、`target_duration_hours`、`assigned_at`、补采字段。
  `GET /data-packages` 新增筛选 `collector_id`、`capture_date_from`、`capture_date_to`。
- `GET /data-packages/{id}`：每个源 Episode 新增 `external_episode_id`、`metadata_sha256`、`data_mcap_sha256`，供 Duance 清理前核对。
  两个哈希目前只存在上传会话的声明记录中；解析建 Episode 时把声明里的值原样复制进
  `Episode.metadata_json.collection_upload`。**不下载、不重算**：OSS 直传的解析本就不读 MCAP 字节（`verify_bytes=False`，
  以对象存储的分片清单确认完整性），本规格不改变这一点。本规格上线前入库的 Episode 没有这两个值，接口返回 `null`，不做回填。
- 上传会话声明：新增 §5.3 的 422 错误码。

### 6.3 删除

`POST /data-packages/{id}/assign`、`POST /data-packages/batch-assign`、`POST /data-packages/adjust`、
`GET /data-packages/{id}/offline-manifest`、`GET /collection-tasks/{task_id}/offline-manifest`、
`POST /data-packages/{id}/supplements`，以及对应的服务函数与测试。`POST /data-packages/{id}/void` 保留。

### 6.4 错误码汇总

| 错误码 | HTTP | 场景 | 客户端处理 |
|---|---|---|---|
| `task_not_found` | 404 | 开包／结束 | 检查任务配置 |
| `task_closed` | 409 | 开包 | 挂起该组，提示「任务已结束」 |
| `project_archived` | 409 | 开包 | 挂起该组 |
| `collector_unmatched` | 422 | 开包 | 挂起该组，不自动重试 |
| `capture_date_invalid` | 422 | 开包 | 客户端缺陷或设备时钟错误，挂起并提示 |
| `episode_collector_mismatch` | 422 | 声明 | 客户端分组缺陷 |
| `episode_capture_date_mismatch` | 422 | 声明 | 客户端分组缺陷 |
| `package_intake_finalized` | 409 | 创建上传会话 | 已有：包在绑定后被冻结；Duance 按 §8.5 处理 |

## 7. Studio 前端、看板与其他读取方

### 7.1 数采任务页（`frontend/js/app.js`）

- 删除：任务行、数据包抽屉顶部、数据包行的分配入口，分配与批量分配对话框，待分配包调整对话框，
  离线清单下载，审核页「创建补采包」，新建任务的「单包目标时长」输入与包数预览，
  以及 `canAssignPackage`、`miningTaskAssignDone` 等只服务于分配的代码。（撤销 `94bac74` 恢复的入口，属预期。）
- 任务列表：状态标签；进度「有效时长 / 目标时长」；`target_reached` 时显示「已达标，可结束」；结束／重新开启按钮（带确认）。
- 顶部卡片「待分配任务数」改为「进行中任务数」。
- 包列表：列为采集员、采集日、序号、状态、是否封口、原始时长、有效时长；可按采集员与采集日筛选。
- 同步修改 `frontend/js/demo-data.js` 与演示模式流程。

### 7.2 看板（`collection_dashboard.py`）

- 人员维度：`operator_collector_id` → `collector_id`。
- 包状态统计 `status_counts`：`pending_assignment`、`assigned` 两项改为 `open`（其余 `other`、`voided` 不变），前端同步。
- 时间分桶与日期筛选不改，仍按 `captured_started_at`（见 D10）。
- 项目、任务完成度口径不变（有效时长 / 目标时长）。

### 7.3 其他读取方

- `data_batches`：筛选 `responsible_collector_id`／`operator_collector_id` 合并为 `collector_id`，输出去掉包目标时长。
- `fetch_manifests.py`、`package_list_projection.py`、`collection_overview.py`：改读 `collector_id`。
- `scripts/seed_dev.py`：改为按日开包造数。

### 7.4 上传 CLI（`scripts/studio_upload.py`）

- `--package` 改为 `--task`。CLI 读取每个 Episode 元数据中的 `operator.id` 与开始时间；
  一次调用内的 Episode 必须属于同一采集员、同一采集日，否则在本地报错、不发请求。
- 先调用开包得到 `package_uid`，其余上传与恢复逻辑不变；状态文件记录开包结果，重跑时复用。
- 同步更新 `docs/COLLECTION_UPLOAD_API.md`、`docs/STUDIO_UPLOAD_CLI.md`（删除「采集包必须已经分配」等表述）。

## 8. Duance 改造（`../duance`）

### 8.1 收件（离线）

1. 同步窗口选择工作区 → 采集项目 → 采集任务（任务列表来自最近一次在线同步的缓存，只列进行中的任务）。
2. 选择源目录，递归查找 QRDF Episode。
3. 对每个 Episode 读取 `operator.id` 与开始时间，按「采集员 × 采集日」分组；
   采集员用本地缓存的采集员目录校验（`profile_key` 数值等价匹配，沿用现有规则）。
   缺失、`unknown`、目录中不存在或未在职的，归入「待处理」组并写明原因；`in_progress` 的 Episode 不收件。
4. **分组预览**：列出任务、每组的采集员、采集日、Episode 数、总时长，以及待处理条目与原因。确认后才移动。
5. 移动到 `<本地目录>/_inbox/<task_id>/<collector_profile_key>/<capture_date>/<episode>/`。
   待处理组移到 `_inbox/<task_id>/_unresolved/<episode>/`，改正采集员目录或元数据后可重新分组。

### 8.2 移动的安全规则

- 以 Episode 为单位：`metadata.json` 与其声明的数据文件一起移动；预览文件、`.qrdf-repair/` 等同目录内容一并移动。
- 同一文件系统：逐文件 `rename`。
- 跨文件系统：先完整复制，核对大小与 SHA-256 后再删除源文件；中途中断时源文件仍在，重新收件可继续。
  计算出的 SHA-256 写入本地状态，上传时复用。
- 源位置只读：退回复制，并在结果中提示「源为只读，已复制」。
- 同名目标：内容相同跳过，内容不同报告冲突、不覆盖（沿用现有导入规则）。
- 不修改 `metadata.json` 与数据文件内容。

### 8.3 绑定（联网，自动）

触发：登录成功、打开同步窗口、点击上传之前，只要在线就执行。

1. 先发送待发封口队列中的全部封口。
2. 对 `_inbox` 中每个已分组的组调用开包：
   - 成功：把组内 Episode 移到 `<本地目录>/<package_uid>/`（同盘 `rename`），写入 `packages` 表
     （`task_id`、`collector`、`capture_date`、`sequence_no`、`data_package_id`），扫描、体检沿用现有逻辑。
   - 返回已存在的包（`created=false`）：追加进该包目录。若该包在本地已锁定，说明封口尚未送达，
     立即补发封口并重新开包。
   - `task_closed`、`project_archived`、`collector_unmatched`、`capture_date_invalid`：该组留在收件区，标注原因，不自动重试。
3. 网络错误：该组留在收件区，下次在线时继续。

### 8.4 本地审核与封口

- 审核流程不变，只能对已绑定的包进行。
- 「完成本地审核并锁定」时调用封口；离线或失败则写入待发封口队列，§8.3 第 1 步补发。

### 8.5 上传

- 流程不变，每个包一个上传会话。
- 创建会话返回 `package_intake_finalized`（包在绑定后被审核冻结）：本地未上传的 Episode 移回收件区对应的组，
  重新绑定后得到续包。

### 8.6 清理已完成的包

可清理条件（点击时联网向 Studio 实时确认，不使用本地缓存）：

1. 本地所有文件上传状态为完成；
2. Studio 上该包已冻结；
3. Studio 包详情中的源 Episode 与本地 Episode 一一对应（按 `external_episode_id`），数量相等，
   且每个 Episode 的 `metadata_sha256`、`data_mcap_sha256` 与本地一致。
   预检时跳过、未上传的 Episode 会导致数量不一致，此时不可清理；Studio 返回的哈希为 `null` 时同样不可清理。

行为：

- 永久删除 `<package_uid>/` 下的 Episode 目录（含 `.qrdf-repair/` 备份），不进回收站。
- 删除前确认框列出包数、Episode 数、释放空间，以及包含的 `.qrdf-repair/` 备份数。
- `state.db` 中的包、审核记录保留，包标记为「已清理」（`packages.cleaned_at`）；汇总 CSV／JSON 保留。
- 支持「清理全部可清理的包」与勾选单个包；不满足条件的包按钮置灰并显示原因。不做自动清理。
- 驳回的 Episode 一并删除：其原始对象仍保存在 Studio（驳回只设置 `validity_status=intake_rejected`）。

### 8.7 界面与本地状态

- 包列表：按任务展示，列为采集员、采集日、序号、状态、本地审核状态、是否已清理，并显示任务进度（有效时长 / 目标时长）。
- 删除：勾选包后「导入 Episode」、「创建数据包目录」、离线清单的下载与导入（`manifest_import`、`.duance/offline-manifest.json`）、
  「目标时长 vs 已绑定时长（达标／缺口）」对比。
- `state.db` 新增：
  - `inbox_episodes`：源路径、目标路径、任务、采集员、采集日、分组状态（已分组／待处理／已绑定）、原因、SHA-256、移动状态。
  - `pending_seals`：`package_uid`、`data_package_id`、入队时间、最后错误。
  - `packages` 表新增 `collector_profile_key`、`capture_date`、`sequence_no`、`data_package_id`、`sealed_at`、`cleaned_at`；删除 `target_hours`。
  - 现有按 `package_uid` 组织的 `episode_files`、`upload_attempts`、`intake_reviews` 不变。
- 旧版本本地目录中已按 `package_uid` 组织的包继续可用（这些包由 Studio 迁移保留）。
- README 同步更新流程说明。

## 9. 迁移（Studio，单个 Alembic 版本）

1. `collection_tasks`：新增 `status`（默认 `active`）、`closed_at`、`closed_by_user_id`；删除 `default_package_duration_hours`。
2. `data_packages`：新增 `collector_id`、`capture_date`、`sequence_no`、`sealed_at`。
3. 作废空包：状态为 `pending_assignment`、`assigned` 的包改为 `voided`。
4. 回填其余未作废的包：
   - `collector_id = operator_collector_id`；
   - `capture_date` 取 `captured_started_at` 的 Asia/Shanghai 日期；为空时取包内最早源 Episode 的开始时间；
     仍取不到则迁移报错中止，由人工处理；
   - `sequence_no`：同一「任务 × 采集员 × 采集日」内按 `id` 升序编号。
5. 删除 `responsible_collector_id`、`operator_collector_id`、`target_duration_hours`、`supplement_for_package_id`、`supplement_reason`；
   `assigned_at` 改名为 `opened_at`。
6. 替换状态检查约束与采集员检查约束，新增唯一约束与索引。
7. `downgrade` 抛出异常，说明不支持回退。

## 10. 测试与验收

### 10.1 Studio 后端

- 开包：新建；重复请求返回同一包；已冻结后得到序号 +1 的续包；已封口后得到续包；作废的序号不复用；
  并发开包只生成一个包；§6.4 中开包相关错误码全部覆盖。
- 封口：幂等；已冻结的包封口成功且不改数据；封口后仍可创建上传会话。
- 声明校验：两条上传链路都覆盖采集员不匹配、采集日不匹配（含 Asia/Shanghai 跨零点边界）。
- 任务：结束后拒绝开包、已开出的包仍可上传；重新开启后恢复；列表 `status` 筛选、`target_reached`。
- 包详情返回 Episode 哈希。
- 迁移：用 fixture 覆盖作废空包、回填、同日多包编号、缺少采集时间时报错。
- 看板：人员维度按 `collector_id`；`status_counts` 含 `open`。
- 删除已移除功能的测试（分配、批量分配、调整、离线清单、补采）。

### 10.2 CLI 与前端

- CLI：混入不同采集员或采集日时本地报错；正常流程先开包后上传；重跑复用开包结果。
- 前端源码断言：分配、调整、离线清单、补采入口已移除；任务状态、进度、结束按钮存在；包列表新列存在。

### 10.3 Duance

- 收件：分组正确（含跨零点）；待处理组；分组预览；同盘 `rename`；跨盘复制校验后删除；中断后重来；只读源退回复制；同名冲突。
- 绑定：新建与追加；本地已锁定但返回同一包时补发封口后重开；各错误码留在收件区；离线跳过。
- 封口队列：离线入队、上线补发。
- `package_intake_finalized` 时未上传的 Episode 回到收件区。
- 清理：三个条件逐一不满足时不可清理；哈希不一致不可清理；成功后文件删除、记录保留。
- `tests/test_studio_upload_contract.py` 增加开包、封口、包详情哈希字段的契约。

### 10.4 UAT 手工验收

1. 新建任务，确认不生成包。
2. Duance 离线收件两名采集员、跨两天的数据，预览分组正确，源文件已移走。
3. 联网后自动绑定出 4 个包；再收件同一人同一天的数据，追加进同一包。
4. 本地审核锁定其中一个包，再收件同一人同一天的数据，得到序号 2 的续包。
5. 上传、同步审核结论后清理，确认本地文件删除、Studio 数据完整。
6. 用不存在的采集员 ID 收件，确认进入待处理组；绕过本地校验时开包被拒绝。
7. 结束任务后绑定被拒绝；重新开启后恢复。
8. 看板按天、按人的数字与包的实际数据一致。

## 11. 不在本期

- 一个 Episode 多人参与。
- 按近 7 日日均预测完工日期；计划日产量。
- 按设备 SN 与时间段自动匹配任务。
- 采集日时区可配置。
- 自动清理本地文件。
