# QuicStudio 外部工具接入前端收尾规格（令牌管理 · 包内归类 · 算法审核筛选）

> 依据规格：[外部工具接入](../specs/2026-09-21-quicstudio-external-sk-access-design.md)、
> [数据后端补全](../specs/2026-09-18-quicstudio-data-backend-completion-design.md)、
> [init 采集运维与数据治理](../specs/2026-09-14-collection-studio-init-design.md)。
> 本规格只补平台 Web 前端（`quic_studio/frontend`）在「外部工具接入」线上缺失的三块能力；
> 后端与客户端（duance）已在前序规格落地，除一处列表 payload 补字段外，本规格不改后端语义。

## 1. 目标与适用范围

让「外部工具接入」规格（2026-09-21）在前端闭环：

1. **令牌自助管理**：任何已登录用户在 Web 上给自己签发、列出、轮换、吊销长期令牌，并安全展示一次性 secret。
2. **数采审核包内归类**：入库审核时，管理员能看清包内每个 Episode 的合格/不合格/处理中归类与失败原因，
   可逐个标记不合格（`rejected_episode_ids`），通过时提交勾选的不合格项。
3. **算法标注审核筛选**：标注审核界面可按「仅算法标注」「仅人工标注」「低置信度（现场可调阈值）」筛选。

本规格不改变业务层级、审核语义与后端契约，只补前端呈现与交互；
后端仅 `/review-work-items` 列表 payload 补 `source` / `confidence` 两个只读字段。

## 2. 已确定的决策

| 主题 | 决策 |
|---|---|
| 范围 | 三项都做，一次规格覆盖：令牌管理 + 包内归类 + 算法审核筛选 |
| 令牌入口 | 账号菜单（右上角用户下拉）新增「API 令牌」，全屏 view（复用 `login-view` 拦截布局，与「修改密码」同套路） |
| 令牌展示 | 创建/轮换成功时 secret **仅展示一次**（明文 `qs_<key_id>_<secret>` + 复制按钮），关闭即不可再取 |
| 过期选项 | 默认 90 天；可选 30 / 90 / 180 / 365 / 永久（后端上限 3650 天），后端为唯一权威 |
| 低置信度阈值 | **审核页现场可调**：筛选栏「来源」下拉 + 「仅低置信度」开关 + 阈值下拉（<0.6 / <0.7 / <0.8）；不做平台全局配置 |
| 包内归类 | 数采审核用**全屏审核页**（独立 view，同工作台模式），抽屉只保留包详情查看；页内含归类汇总 + 失败原因列 + 隐私敏感高亮 + 逐 Episode「不合格」勾选；交互保持「包级通过 + 单 Episode 不合格 + 整包驳回」，通过时提交 `rejected_episode_ids` |
| 后端改动 | 仅 `/review-work-items` 列表补 `source` / `confidence`（从 `annotation_item.draft_json.source` 读取）；其余字段前序规格已返回 |
| 错误处理 | 复用现有 `errorMessage` / `ElMessage`；令牌 401/403/404/409 按后端 `detail` 文案提示 |

## 3. 现状核对（缺失点证据）

| 规格条款 | 要求 | 现状 |
|---|---|---|
| 外部工具接入 §4.3 | 任何登录用户可自助签发/列出/轮换/吊销 | 后端 `backend/data/routers/tokens.py` 四端点已完备；前端无任何 `/tokens` 调用与 UI |
| init 规格 §109 | 包内逐个 Episode 查看、可标记不合格 | `approvePackageIntake`（app.js:10370）只发 `{verdict:'approved'}`，无 `rejected_episode_ids` |
| init 规格 §115 | `privacy_sensitive` 入库审核界面高亮 | 后端已返回 `privacy_sensitive`，前端未消费 |
| 数据后端补全 §120/§230 | 按包返回排除项 ID 与原因 | 后端已返回 `excluded_episodes`/`admission_reason`/`admission_counts`，前端未渲染 |
| 外部工具接入 §7.3 | 审核界面可按「仅算法标注/低置信度」筛选 | `/review-work-items` 列表未返回 `source`/`confidence`；审核队列无筛选 |

## 4. 设计 A：令牌自助管理（前端）

### 4.1 入口与导航

- 账号菜单（app.js:11350 附近）在「修改密码」下方新增 `el-dropdown-item command="api-tokens"`「API 令牌」。
- `handleAccountCommand` 新增分支：置 `showApiTokens = true`（新增 ref，与 `showChangePassword` 并列）。
- 全屏 view：在 `login-view` 条件链（app.js:11277 附近）加 `<section v-else-if="showApiTokens" class="login-view">`。
- 「返回控制台」：`showApiTokens = false`。

### 4.2 api.js 新增方法

| 方法 | 请求 | 说明 |
|---|---|---|
| `listApiTokens()` | `GET /tokens` | 返回 `{items:[{id,name,key_id,expires_at,rotated_at,last_used_at,revoked_at,created_at}]}`，**不含 secret** |
| `createApiToken(body)` | `POST /tokens` | body `{name, expires_in_days?}`；响应 `{...view, secret}`，secret 仅此一次 |
| `rotateApiToken(id)` | `POST /tokens/{id}/rotate` | 响应含新 secret，仅此一次 |
| `revokeApiToken(id)` | `DELETE /tokens/{id}` | 返回行视图 |

### 4.3 页面结构

1. **头部**：标题「API 令牌」+「返回控制台」按钮 +「创建令牌」按钮。
2. **创建弹窗**：名称（必填，≤64）＋ 过期下拉（30/90/180/365/永久）。
   - 「永久」映射 `expires_in_days = 3650`（后端上限）。
3. **一次性 secret 弹窗**：创建或轮换成功后弹出，展示完整 `qs_…` secret（`<code>` 大字号）＋ 复制按钮，
   文案明确「该令牌仅显示一次，关闭后无法再次查看」；确认关闭后清空内存中的 secret。
4. **令牌列表**（`el-table`）：名称 / key_id / 创建时间 / 过期时间 / 最后使用 / 状态 / 操作。
   - 状态：`revoked_at` 非空→已吊销；`expires_at` 过期→已过期；否则→有效。
   - 操作：轮换（确认弹窗提示「旧 secret 一小时内仍有效」，成功后一次性展示新 secret）、吊销（确认弹窗）。
   - 无令牌时 `el-empty`。
5. 全部操作沿用现有 `saving` / `errorMessage` 反馈。

### 4.4 配套

- demo-data.js：mock `GET/POST /tokens`、`POST /tokens/{id}/rotate`、`DELETE /tokens/{id}`；
  创建/轮换返回 `secret`，列表与详情不含 `secret`。
- i18n（zh/en）：`apiTokens` 系列文案（标题、创建、名称、过期、状态、仅显示一次提示、轮换/吊销确认、成功提示）。
- 前端测试：`frontend/tests/*.test.mjs` 补页面渲染与流程（创建展示 secret、列表不泄 secret、轮换/吊销调用）。

## 5. 设计 B：数采审核「包内不合格归类」（全屏审核页）

### 5.1 载体决策

数据包详情抽屉（app.js:13151）只保留**只读详情**（元数据、离线清单下载、Episode 列表）。
**入库审核**拆到新增的**全屏审核页**（独立 view，模式同现有工作台），避免 680px 侧栏承载完整审核流程。

### 5.2 入口与导航

- 新增 `VIEWS` 成员 `intake-review`（app.js:11 VIEWS 集合），不占侧边栏。
- 入口两处：数采审核队列（`batches`）行内「进入审核」、数据包详情抽屉内「进入审核」按钮。
- 进入时 `navigate('intake-review')` 并载入该包详情；「返回」回到来源（`batches` 并刷新队列）。
- 载入数据：`getDataPackage(id, workspaceId)`（已返回 `admission_counts` / `episodes[]` / `intake_review` / `desensitization`）。

### 5.3 页面结构

1. **顶部上下文条**：包 uid / 项目 / 任务 / 模态 / 目标与有效时长 / 责任采集员 / 采集设备 / 状态（`dataPackageStatusLabel`）。
2. **归类汇总条**：读 `packageDetail.admission_counts`（后端已返回 `{ready, running, failed, reviewed}`）：
   - 「就绪 N · 失败 M · 处理中 K」；`failed > 0` 时红色强调。
   - `running > 0` 时附注「处理中项不阻塞审核，通过时按当前事实处理」。
3. **Episode 表格**（主区，全宽）：现有列 episode_uid / modality / duration_hours / admission_status / validity_status / preview_available，新增：

   | 新增 | 实现 |
   |---|---|
   | 失败原因列 | 读 `episode.admission_reason`，映射友好文案（见 5.5），无则 `—` |
   | 隐私敏感高亮 | `episode.privacy_sensitive === true` 时行背景高亮 + 「隐私敏感」tag（对应 init §115） |
   | 不合格勾选 | `canReviewPackage(packageDetail)` 时每行加 `el-checkbox`，勾选集合 = `rejected_episode_ids`；不勾选即视为有效 |

4. **操作区**：包级「通过」（确认含勾选不合格项数）·「驳回」（填原因弹窗）·「返回」。
5. **审核结论区**：`packageDetail.intake_review` 渲染——`accepted_episode_ids` / `rejected_episode_ids` / `excluded_episodes`（`{episode_id, reason}` 列表），
   同时显示审核人、时间和原因；已有单条摘要可复用，历史库记录保留，不新增多轮审核需求。

### 5.4 审核交互

交互保持产品原有口径（init 规格 §109）：**包级通过 + 单 Episode 不合格 + 整包驳回**，无「轮次」概念。

- 通过：确认文案显示「通过本包」及当前勾选的「不合格 N 个」，
  提交 `{verdict:'approved', rejected_episode_ids:[勾选项]}`（api.js `reviewIntakePackage` 已是通用 body 透传，无需改）。
- 不合格：审核态（pending_intake_review）时逐 Episode 勾选「不合格」；不勾选即视为有效。
- 驳回：维持现有整包驳回（`{verdict:'rejected', reason}`）不变；驳回后整包作废，不可再审。
- 每包只有一次最终入库审核结论；结论前可修复失败来源，结论后只读。补采在同任务下另建关联包，不在原包补传再审。相同提交的网络重试幂等返回原结果，冲突修改拒绝。当前不开放同包多次建批，模型保留未来扩展；未来复用不等于重新打开入库审核。

### 5.5 失败原因分类（读 `admission_reason`）

按 `episode_admission.py:admission_eligibility` 返回码归类展示：

| 归类 | reason 前缀/值 | 文案 |
|---|---|---|
| 未解析/缺事实 | `admission_fact_missing` | 未解析（文件缺失或未生成校验事实） |
| 完整性失败 | `integrity_*` | 完整性校验失败 |
| 预览失败 | `preview_*` | 预览生成失败 |
| 输出校验失败 | `output_*` | 输出校验失败 |
| 指纹异常 | `source_fingerprint_*` | 源指纹缺失/变化 |
| 策略过期 | `validation_policy_outdated` | 校验策略过期 |
| 人工不合格 | `episode_*`（如 `episode_intake_rejected`） | 人工标记不合格 |
| 其他 | 其余 | 原样显示 reason 码 |

### 5.6 配套

- demo-data.js：补 `intake-review` view 的数据——episodes 的 `admission_reason` / `privacy_sensitive`、`admission_counts` 非零样例、`intake_review` 的 excluded 明细。
- i18n（zh/en）：进入审核、归类汇总、失败原因列、不合格勾选、隐私敏感、包级通过确认等文案。
- 前端测试：`frontend/tests/*.test.mjs` 补审核页渲染与流程（进入、勾选、通过提交 `rejected_episode_ids`、驳回）。

## 6. 设计 C：审核「仅算法标注 / 低置信度」筛选

### 6.1 后端（唯一后端改动）

`backend/data/routers/review_work_items.py` `_item_payload` 补：

```
"source": (item.annotation_item.draft_json or {}).get("source"),
"confidence": ((item.annotation_item.draft_json or {}).get("source") or {}).get("confidence"),
```

- 仅只读透传，不改变审核语义；`source` 结构同 `batch-submit` 写入的 `{kind,name,version,run_id,confidence}`。
- 补一条 payload 断言测试（`backend/tests` 既有 `test_annotation_batch_submit_api.py` 或新增）。

### 6.2 前端（work-queue → 审核队列）

审核队列表格（app.js:12692 治理表格）顶部加筛选栏：

| 控件 | 语义 |
|---|---|
| 来源下拉 | 全部 / 仅算法标注（`source.kind === 'algorithm'`）/ 仅人工标注（无 `source` 或 `kind === 'human'`） |
| 仅低置信度开关 + 阈值下拉 | 开启后只显示 `confidence < 阈值`；阈值选项 `<0.6 / <0.7 / <0.8`，默认 `<0.7`，现场可调 |

- 行内新增「来源」列：算法标注显示 `source.name@source.version` tag（置信度 `source.confidence`），人工标注显示「人工」tag。
- 筛选在 `reviewQueueRows` computed 上做客户端过滤（列表量为工作队列规模，无需分页改造）。
- demo-data.js：`reviewWorkItems` 补 `source` / `confidence` 样例。

## 7. 错误处理与边界

| 场景 | 结果 |
|---|---|
| 令牌创建重名 | 后端 409，前端提示「令牌名称已存在」 |
| 轮换已吊销令牌 / 吊销不存在令牌 | 后端 404/409，前端按 `detail` 提示并刷新列表 |
| secret 仅展示一次 | 前端内存持有至弹窗关闭即清除；列表/详情接口永不展示 secret |
| 包内含 running 项 | 全屏审核页汇总条注明「不阻塞审核，通过时按当前事实处理」；不等待处理中项 |
| 全部 Episode 不合格仍通过 | 允许（与后端语义一致：`accepted_ids` 为空、`intake_valid_duration_hours=0`）；通过确认如实展示勾选的不合格项数 |
| review 列表无 source 字段 | 视为人工标注；置信度缺失时「仅低置信度」不命中该行 |
| 筛选无结果 | `el-empty` 展示 |

## 8. 验收标准

> **验收结果（2026-09-22 收尾）**：1–4 均按下面的证据逐条核对（任务 7 全量回归）。
> 前端全量 `node --test frontend/tests/*.test.mjs`：**273 passed / 0 failed**；
> `make demo-check`：demo 模式 6 项流程用例全过，`scripts/demo-smoke.mjs` 遍历 23 个视图全部渲染、
> 无 console error（`miningConfig`/`trainNew`/`trainSystem` 为预存表单页空态 WARN，非失败）；
> 其中 `intake-review?package_id=5102` 渲染 `rows=4`、无空态、无 console 错误。
> 后端聚焦回归 `test_review_work_items_api.py` + `test_annotation_batch_submit_api.py`：**8 passed / 0 failed**。

1. **令牌管理**：账号菜单进入全屏页；创建（含永久）、一次性 secret 展示与复制、列表不回 secret、轮换（含旧 secret 一小时提示）、吊销（含确认）全部可用；demo 模式 mock 与真实后端一致。
   - 通过（主要流程）：`frontend/tests/api-tokens-console.test.mjs`（创建展示 secret、列表不泄 secret、轮换/吊销流程、demo mock 一致性）。
     「永久」过期映射、复制按钮、一小时提示、吊销确认弹窗等 UI 交互未在单测覆盖，依赖人工核验。
2. **包内归类**：审核队列/抽屉可「进入审核」打开全屏审核页；页面显示「就绪/失败/处理中」汇总；失败项展示原因分类文案；`privacy_sensitive` 行高亮；审核时勾选不合格项并随通过提交 `rejected_episode_ids`；审核结论显示 accepted/rejected/excluded 明细及审核人、时间、原因；返回后队列刷新。
   - 入口接线（最终修复）：审核队列（`batches`）行内「进入审核」`@click="openIntakeReview(scope.row)"`（`canReviewPackage` 时显示，原「审核通过」快捷按钮同样改走 `openIntakeReview` 进全屏页）＋ 数据包详情抽屉「进入审核」`@click="openIntakeReview(packageDetail)"`；i18n 键 `enterIntakeReviewAction`（zh/en）已补。通过后 `submitIntakeApprove` 改走 `loadIntakeReviewPackage(pkg.id)` 重取详情，真实后端响应不含 `intake_review` 键时历史区/勾选列仍正常渲染。
   - 通过（主要流程）：`frontend/tests/intake-review-console.test.mjs`（hash 进入、`rejected_episode_ids` 随通过提交、返回标签/包 id 保留、入口按钮 ≥2 处 `@click="openIntakeReview(`、通过后 `loadIntakeReviewPackage` 重取）；`frontend/tests/demo-mode-flows.test.mjs`（demo 通过→`intake_approved`、驳回→`voided`）；demo-smoke 渲染 `intake-review?package_id=5102` 通过。
     历史明细（accepted/rejected/excluded）渲染依赖 demo-check 人工核验，未在单测断言。
3. **算法审核筛选**：`/review-work-items` 列表返回 `source`/`confidence`；审核队列可按来源与低置信度（阈值现场可调）过滤；行内显示来源 tag 与置信度。
   - 通过：`backend/tests/test_review_work_items_api.py`（payload 断言含 `source`/`confidence`）；前端 `frontend/tests/annotation-queue.test.mjs`（源码断言 `reviewFilterState`/`source.kind === 'algorithm'`/`confidence.*threshold`/`lowConfidence` 已落地）。
4. **回归**：后端 `backend/tests` 相关用例通过；前端 `node --test frontend/tests/*.test.mjs` 全绿；`make demo-check` 全部视图渲染。
   - 后端：`test_review_work_items_api.py`（6 passed）+ `test_annotation_batch_submit_api.py`（2 passed）= 8 passed。
   - 前端：273 passed / 0 failed；`make demo-check` 23 视图全部渲染、无 console error。

## 9. 非目标

本轮不做：令牌的 IP 白名单、按接口细分的 scope、他人令牌管理（管理员越权吊销）、系统钥匙串、
脱敏硬门禁（`require_desensitization_declaration` 打开后的审核拦截 UI）、算法自动通过白名单、审核筛选的服务端分页。

## 10. 规格自检

- 占位符：无「待定」段落；阈值默认值（<0.7）、过期选项（30/90/180/365/永久）均已写明具体值。
- 一致性：令牌展示「仅一次」与后端「明文仅返回一次」一致；`rejected_episode_ids` 与后端 `IntakeReviewRequest` 一致；
  `source`/`confidence` 读取路径与 `batch-submit` 写入位置一致；数采审核交互迁移到全屏 view（`intake-review`），抽屉只读详情不承载审核。
- 范围：可被一个实现计划覆盖，前端为主、一处后端只读字段。
- 模糊性：已明确「仅低置信度不命中无 confidence 行」「处理中项不阻塞审核，通过时按当前事实处理」「全部不合格仍可通过（时长为 0）」
  三处易歧义点；最新口径为单包一次最终入库审核，补采另建关联包，最终结论和批内快照保持固定。历史验收不代表本次修订已经实现。
