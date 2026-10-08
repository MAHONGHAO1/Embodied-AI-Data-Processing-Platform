# 采集概览看板第一期规格（共用地基 · 数采看板）

> 本规格另含附带修复：数采任务的分配入口、分配状态与新建任务拆包（第 9 节）。

> 依据：钉钉文档《QuicStudio 需求整理》3.3.1 目录管理、3.3.2 数采概览（做简化版）；
> [init 采集运维与数据治理](../specs/2026-09-14-collection-studio-init-design.md) §6（采集进度使用入库审核后的有效时长）。
> 本规格是采集概览三个看板中的第一期，只交付共用地基与「数采看板」。
> 产能看板、人效看板各自另开规格。

## 1. 目标与适用范围

「采集管理 → 采集概览」页按文档分为 **产能看板、数采看板、人效看板** 三个标签页，三者共用一套全局筛选。
本期交付：

1. 数据包上的统计明细（精确秒数、字节数、采集开始时间），在写入时一次算好。
2. 按看板切分的实时聚合接口，本期实现数采看板。
3. 全局筛选栏、数采看板页面、图表公共工具与 CSV 导出。

不在本期：

- 产能看板、人效看板的指标（标签页保留，显示「建设中」）。
- 机器人类型、数据类型、地区三个筛选维度（字段或口径尚未定义）。
- 工作时长、活跃时长等依赖 App 上报的数据。
- 旧 dashboard 的任何改动。
- 目录标签（已与产品核对，不改）。

## 2. 已确定的决策

| # | 决策 | 理由 |
|---|---|---|
| D1 | 「有效时长」统一指**入库审核有效时长** | 审核后立即可得，可落到包与采集员；与 init 规格 §6 一致。治理有效、标注有效属于数据侧，不进采集概览 |
| D2 | 统计一律使用精确秒数与字节数，展示时才换算 | 包上现有 `*_hours` 为 0.01 小时四舍五入值，累加会产生误差 |
| D3 | 不建公共汇总层（DWS），只在数据包上落明细（DWD），聚合接口层现算 | 目标 1 万小时、每包约 30 分钟，约 2 万个包；PostgreSQL 按包聚合足够。聚合模块独立，日后可无感切换到日汇总表 |
| D4 | 未审核包的有效时长为「未知」，不当 0；整包驳回为明确的 0 | 避免待审核数据拉低产出比；驳回是结论，应当体现 |
| D5 | 时长、大小的趋势按**采集时间**；整包计入包内最早一条 Episode 的采集开始时间 | 反映真实产量；包只有约 30 分钟，按天几乎无误差 |
| D6 | 项目、任务的个数按**创建时间**。数据包**总量**含全部包（按创建时间）；数据包**今日新增与趋势**只统计**已冻结的包**（已有入库审核结论，含整包驳回），按采集时间计入，不随口径切换 | 数据包是任务拆出的工作单元，总量应与状态表一致；包在新建任务时一次性拆出，按创建时间看新增只反映拆包，冻结的包才代表完成交付 |
| D7 | 大小：全部 = 已入库 Episode 的 `raw` 对象大小之和；有效 = 入库审核通过的 Episode 的 `raw` 对象大小之和 | 只统计准入成功的 Episode（准入失败的事实不记录对象，无大小可取），因此采集大小与采集时长的统计范围有细微差异，接受该差异。有效时长与有效大小统计范围完全一致。解析失败、未生成 Episode 的上传文件不计入 |
| D8 | 接口按看板切分：一个看板一个接口，一次返回一屏数据 | 2 万包量级下一次返回无压力；结构与三个看板一一对应 |
| D9 | 数采看板 5 张趋势图全部摊开 | 文档要求每个模块均有趋势图，摊开便于对照 |
| D10 | 旧 dashboard 保留，仍是点击 QuicStudio 品牌位进入的首页 | 其漏斗、待办队列属于数据处理链路，不属于采集概览 |

## 3. 数据层：数据包统计明细

### 3.1 新增列（`data_packages`，全部可空）

空值表示「尚无此事实」，不当作 0。

| 列 | 类型 | 含义 | 写入时机 |
|---|---|---|---|
| `captured_started_at` | DateTime（UTC） | 包内最早一条 source Episode 的采集开始时间 | 解析完成 |
| `captured_duration_s` | Numeric(14, 3) | 采集时长，秒 | 解析完成 |
| `captured_size_bytes` | BigInteger | 已入库 Episode 的 `raw` 对象大小之和 | 解析完成 |
| `intake_valid_duration_s` | Numeric(14, 3) | 入库审核有效时长，秒；整包驳回为 0 | 入库审核 |
| `intake_valid_size_bytes` | BigInteger | 审核通过 Episode 的 `raw` 对象大小之和；整包驳回为 0 | 入库审核 |

检查约束：各列非负；`intake_valid_duration_s <= captured_duration_s`，`intake_valid_size_bytes <= captured_size_bytes`（两侧均非空时）。

### 3.2 口径

- 采集开始时间取 `metadata_json.timing.start_timestamp_ns`。采集上传解析时该字段必填，缺失或起止倒置时整包解析失败（`collection_upload_parse._parse_fixture_source` 与 manifest 校验），因此有 Episode 的包必有采集时间。
- 时长沿用 `collection_duration.episode_duration_seconds`：优先纳秒起止差，不从小时反推秒数。
- 统计范围与现有采集概览一致：`Episode.kind == "source"`，且 `workflow_status` 不属于 `IMPORT_PLACEHOLDER_WORKFLOW_STATUSES`。
- 采集大小取 Episode 当前准入事实（`EpisodeAdmissionFact.is_current`）`objects_json` 中 `bucket_role == "raw"` 对象的 `size_bytes` 之和。准入失败的事实 `objects_json` 为空，这类 Episode 计入采集时长但不计入采集大小（见 D7）。
- 有效集合与 `accepted_intake_episode_sql` 一致：审核通过、列入 `accepted_episode_ids_json`，且准入 attempt 与来源指纹匹配。
- 有效大小取**审核冻结的那次准入事实**（`PackageIntakeReview.fact_attempts_json` 记录的 attempt）的 `raw` 对象大小，不取当前事实，保证与有效时长统计的是同一版来源。

### 3.3 写入位置

- 解析完成：`collection_upload_parse.py` 中现有两处写 `captured_duration_hours` 的位置改为调用同一个函数，同时写入新旧字段。每次都**按包内当前全部 source Episode 重算**，不增量累加，因此审核前的补传不会重复计数。
- 入库审核：`collection_intake_review.py` 写 `intake_valid_duration_hours` 的两处（驳回、通过）同时写入新列。
- 旧 `*_hours` 列保留，现有页面继续使用，本期不删。

### 3.4 迁移、回填与索引

- Alembic 迁移 `0011`：加列、加约束、加索引 `(workspace_id, captured_started_at)`、`(collection_task_id, captured_started_at)`。
- 回填：独立的幂等脚本 `backend/scripts/backfill_package_dashboard_facts.py`，按现有 Episode、准入事实与最新入库审核重算，不放进迁移。
- 回填核对：新的有效秒数换算成小时后，与旧 `intake_valid_duration_hours` 差值不超过 0.005 小时；超出的包列出并人工排查。

## 4. 聚合接口

### 4.1 端点

`GET /api/v1/collection-dashboard/data`

- 权限：`episode:read`，外加 `require_collection_workspace` 工作空间校验，与现有采集概览一致。
- 旧 `/api/v1/collection-overview` 本期保留，新页面上线后另行删除。

### 4.2 请求参数

| 参数 | 说明 |
|---|---|
| `workspace_id` | 必填 |
| `start_date`、`end_date` | 日期（含首尾），按 `tz` 解释；跨度不超过 366 天；默认最近 30 天 |
| `project_ids`、`task_ids` | 多选，不传即全部 |
| `scene_label_ids`、`purpose_label_ids` | 多选，经 `collection_task_labels` 关联到任务后过滤；标签是平台级字典（`collection_labels` 无工作空间字段），须校验存在且 `category` 分别为 `scene`、`purpose` |
| `granularity` | `hour`、`day`、`month`；`hour` 仅允许不超过 7 天的范围；默认 `day` |
| `basis` | `all`（全部）或 `valid`（入库有效），作用于时长和大小；默认 `valid` |
| `tz` | IANA 时区名，默认 `Asia/Shanghai`；决定「今日」与分桶边界 |
| `format` | `json`（默认）或 `csv` |

### 4.3 统计口径

| 模块 | 总量 | 今日新增 | 趋势 | 时间字段 |
|---|---|---|---|---|
| 项目 | 满足筛选的项目数 | 今日创建 | 按桶计数 | `collection_projects.created_at` |
| 任务 | 满足筛选的任务数 | 今日创建 | 按桶计数 | `collection_tasks.created_at` |
| 数据包 | 满足筛选的全部包（含待分配、已分配），按 `created_at` 落在范围内 | 今日采集且已冻结 | 已冻结的包按桶计数 | 总量：`created_at`；今日与趋势：`captured_started_at` |
| 时长 | `basis` 对应列之和 | 今日采集 | 按桶求和 | `data_packages.captured_started_at` |
| 大小 | `basis` 对应列之和 | 今日采集 | 按桶求和 | `data_packages.captured_started_at` |

- 「总量」受日期范围约束：只统计时间字段落在范围内的对象。「今日新增」不受日期范围约束，始终是 `tz` 下的今天。
- 项目按 `project_ids` 过滤；任务、场景、用途筛选作用于任务及其下的包；项目模块在带任务类筛选时，只统计含有匹配任务的项目。
- 「已冻结」即已有入库审核结论：以 `intake_valid_duration_s IS NOT NULL` 判定（该列只在入库审核时写入，整包驳回写 0）。不以包状态判定，因为 `voided` 也可能来自非审核的作废。
- 整包驳回的包计入采集时长与大小，有效值为 0；整包驳回很少见，通常只驳回包内部分 Episode。
- `pending_review_duration_s`：范围内已上传（`captured_duration_s` 非空）且 `intake_valid_duration_s` 为空的包的采集时长之和。
- `incomplete_packages`：已上传（`upload_completed_at` 非空）但 `captured_duration_s` 为空的包数（解析中或回填未完成），不计入时长、大小。
- 库内时间为无时区的 UTC，分桶时先按 UTC 解释再转换到 `tz` 后截断（PostgreSQL `date_trunc(g, (col AT TIME ZONE 'UTC') AT TIME ZONE tz)`）；日期范围边界同样按 `tz` 换算成 UTC 后比较，以便命中索引。
- 趋势空桶补 0，桶标签按 `tz` 输出（`hour` 为 `YYYY-MM-DD HH:00`，`day` 为 `YYYY-MM-DD`，`month` 为 `YYYY-MM`）。
- 附带 `package_status_counts`：`pending_assignment`、`assigned`、`other`、`voided`，四项之和等于数据包总量。

### 4.4 返回结构（示意）

```json
{
  "workspace_id": 2,
  "filters": { "start_date": "2026-08-31", "end_date": "2026-09-29", "granularity": "day", "basis": "valid", "tz": "Asia/Shanghai", "project_ids": [], "task_ids": [], "scene_label_ids": [], "purpose_label_ids": [] },
  "projects": { "total": 12, "today": 1, "trend": [{ "bucket": "2026-09-28", "value": 3 }] },
  "tasks":    { "total": 40, "today": 2, "trend": [] },
  "packages": { "total": 136, "today": 5, "trend": [], "status_counts": { "pending_assignment": 1, "assigned": 127, "other": 6, "voided": 2 } },
  "duration": { "basis": "valid", "total_s": 848.809, "today_s": 0, "pending_review_duration_s": 2100.3, "trend": [] },
  "size":     { "basis": "valid", "total_bytes": 0, "today_bytes": 0, "trend": [] },
  "incomplete_packages": 0,
  "computed_at": "2026-09-29T08:00:00Z"
}
```

### 4.5 CSV

同一组筛选条件下的趋势明细，每个桶一行，列为：桶、项目数、任务数、数据包数、时长（秒）、大小（字节）。文件名 `collection-data-board_<start>_<end>.csv`，UTF-8 带 BOM 以便 Excel 打开。

### 4.6 代码组织

- 聚合逻辑：`backend/data/services/collection_dashboard.py`，一个看板一个函数，本期为 `data_board(...)`。
- 路由：`backend/data/routers/collection_dashboard.py`，只做参数校验、权限与格式转换。
- 查询只读 `data_packages`、`collection_projects`、`collection_tasks`、`collection_task_labels`，不读 Episode。

## 5. 前端

### 5.1 文件

| 文件 | 职责 |
|---|---|
| `collection-overview.js`（改造） | 页面外壳：三个标签页、全局筛选状态，向当前看板传递筛选 |
| `collection-dashboard-filters.js`（新） | 全局筛选栏：日期范围、项目、任务、场景、用途（多选）；选项目后任务选项随之收窄 |
| `collection-data-board.js`（新） | 数采看板：取数、卡片、5 张趋势图、数据包状态表、CSV 导出 |
| `dashboard-charts.js`（新） | ECharts 懒加载、实例创建/销毁/缩放、折线图配置；时长与大小格式化 |

- `app.js` 与旧 dashboard 不改动。
- 新文件接入 `page-components.js` 的按需加载，脚本带版本号。
- 文案沿用 `app.js` 中已存在但未使用的 `dash*` 中文措辞，组件内以 `text(中文, 英文)` 书写。

### 5.2 布局

```
[ 全局筛选栏：日期 | 项目 | 任务 | 场景 | 用途 | 刷新 | 导出 CSV ]
[ 粒度：小时/天/月 ]                         [ 口径：全部/有效 ]
[项目总量] [任务总量] [数据包总量] [采集时长] [数据总量]      每张卡附「今日 +N」
[项目数量趋势] [任务数量趋势] [数据包数量趋势]
[采集时长趋势]            [数据大小趋势]
[数据包状态：待分配 / 已分配 / 上传及后续处理 / 已作废]
```

### 5.3 交互规则

- 粒度与口径切换在看板内部，不属于全局筛选。
- 口径为「有效」且 `pending_review_duration_s > 0` 时，时长卡下方显示「另有 X:XX:XX 待审核，未计入」。
- `incomplete_packages > 0` 时显示「N 个包统计中」提示。
- 任一条件变化整屏重新取数；沿用现有 loader 的代次号机制丢弃晚到的旧请求，并校验返回的 `workspace_id`。
- 时长显示 `时:分:秒`；大小自动换算 MB/GB/TB；空值显示 `—`。
- 导出 CSV 使用当前全部条件，浏览器直接下载。

## 6. 错误处理

| 情况 | 行为 |
|---|---|
| 无权限 / 工作空间不可访问 | 403 / 404 |
| 日期范围超过 366 天、开始晚于结束、`hour` 粒度超过 7 天 | 422，附原因 |
| 筛选的项目、任务不存在或不属于该工作空间；标签不存在或分类不符 | 422，不静默忽略 |
| `tz`、`granularity`、`basis`、`format` 取值非法 | 422 |
| 包的明细列为空 | 不报错，计入 `incomplete_packages` |
| 前端请求失败 | 显示错误并清空旧数据 |
| ECharts 加载失败 | 卡片照常显示，图表位置提示加载失败 |
| CSV 导出失败 | 弹出错误消息，页面不受影响 |

## 7. 测试

后端（pytest，PostgreSQL）：

- 明细写入：解析完成后 5 列正确；整包驳回有效值为 0；部分通过只计通过的 Episode；审核前补传后按全部 Episode 重算、不重复累加；大小只计 `raw` 对象；准入失败的 Episode 计入采集时长、不计入采集大小。
- 回填脚本：幂等；与旧 hours 列差值不超过 0.005 小时。
- 聚合接口：各筛选维度及多选组合；`Asia/Shanghai` 零点前后分桶正确；空桶补 0；`basis` 切换；待审核时长；数据包总量含未上传包且状态四项之和等于总量；数据包今日新增与趋势只含已冻结的包（含整包驳回），且不随 `basis` 变化；跨工作空间 ID 被拒；参数校验；CSV 内容与表头。
- 性能（慢测试标记，不进默认集）：2 万个包下接口耗时在可接受范围内，`EXPLAIN` 命中新索引。

前端（node test，vm 加载）：

- 筛选参数拼接；loader 丢弃晚到请求；时长与大小格式化及空值；仅「有效」口径显示待审核提示；5 张图的数据来自接口；CSV 请求参数与当前条件一致。

手工验收：UAT 浏览器中抽取若干包，将看板数字与数据包详情逐一对照。

## 8. 已知风险

- 采集时间来自设备时钟，设备未校准会落到错误日期；本期不做校正。
- 按采集时间统计，审核前补传可能改写历史某天的数字；「今日」数字随上传持续增长。
- 回填前，新列为空的包不计入时长和大小，页面以「统计中」提示；上线顺序应为迁移 → 回填 → 前端切换。

## 9. 附带修复：数采任务的分配与拆包

> 本节只改数采任务页（`app.js` 中 mining 相关代码）与任务列表接口的一个只读字段，不影响看板部分。

### 9.1 问题与根因

**P1 没有分配采集员的入口。** 数据包清单中没有任何分配入口，新拆出的包一直停在「待分配」。
后端只为已分配的包生成离线任务清单（`collection_packages.build_offline_manifest` 拒绝 `pending_assignment`），
采集员拿不到清单就无法采集和上传，流程被卡住。

- 提交 `31b0be9`（2026-09-24，fix: refine collection UI…）删掉了两处「分配任务」按钮：任务行操作列、数据包清单抽屉顶部。提交说明未提及去掉分配，判定为误删。
- 按包分配的 `openMiningAssignDialog` 已实现，但模板中从未提供调用它的按钮。
- 分配对话框、`submitMiningAssign`、`assignDataPackage` / `batchAssignDataPackages` 均完好，只缺入口。

**P2 任务级分配对话框的缺陷。**

- 「仅分配待分配数据包」开关关闭时，已分配的包也会被提交；后端批量分配在一个事务内，任一包 `assignment_locked` 即整批回滚（409），所以关闭开关只会失败。
- 「待分配」的判定在拆包、批量分配等处各写一套，写法不同。
- 「采集设备」「采集窗口」字段在真实模式下不提交、不生效（设备来自上传时的采集来源信息），只有演示模式使用。
- 「按人员平均分配时长」文案与实际不符：实际是把现有包按顺序轮流分给所选采集员，不会按人数重新拆包。

**P3 分配状态随进出抽屉跳变。**

- 任务列表接口只返回 `package_count`、`assigned_count`，不返回包明细；前端构建列表时从 `task.packages` 取包，得到的 `batches` 恒为空。
- `miningTaskAssignDone` 按 `batches` 判断，于是所有任务先显示「未完成」。
- 打开「查看数据包」时 `selectMiningTask` 只为这一个任务拉取包明细并改写它的 `batches`，这一行状态随之按真实数据重算；关闭抽屉不还原；列表重载后又清空，并只为当前选中的任务重新拉取。同一任务因此先后按空数据和真实数据各算一次。
- 顶部「待分配任务数」卡片同样受影响。
- 口径问题：当前「有任意一个包已分配」即显示「已完成」。

**P4 新建任务单包时长写死。** init 规格 §64 规定任务创建时按「默认单包目标时长」预生成待分配包，任务可覆盖该值；
后端支持 `default_package_duration_hours`，但前端新建任务对话框没有该字段，固定传 `2.00`。

### 9.2 决策

| # | 决策 |
|---|---|
| F1 | 「待分配」唯一定义：包状态为 `pending_assignment`。前端统一用 `canAssignPackage(row)` 判定 |
| F2 | 任务「已完成」定义：至少有一个包，且没有待分配的包 |
| F3 | 任务分配状态只依赖任务列表接口返回的计数，不依赖包明细；后端任务列表新增只读字段 `pending_assignment_count` |
| F4 | 单包时长由管理员在新建任务时填写；默认值沿用 2 小时，不另作规定 |

### 9.3 修复

1. 恢复任务级「分配任务」：任务行操作列、数据包清单抽屉顶部各一个，调用 `openMiningAssignModeDialog`。
2. 新增按包「分配」：数据包行操作列在 `canAssignPackage(row)` 为真时显示，调用 `openMiningAssignDialog`，一个包只选一位采集员（沿用现有校验）。
3. 任务级分配：去掉「仅分配待分配数据包」开关，始终只提交待分配的包；任务下没有待分配的包时点击直接提示「没有待分配的数据包」，不打开对话框。
4. 对话框的「采集设备」「采集窗口」字段仅在演示模式显示。
5. 分配方式文案改为如实描述：「按顺序轮流分配待分配数据包」，去掉「÷ 人数」的时长估算。
6. 后端 `GET /collection-tasks` 每项新增 `pending_assignment_count`；前端 `miningTaskAssignDone` 改为 `package_count > 0 && pending_assignment_count === 0`，列表与抽屉、KPI 卡片一致。
7. 新建任务对话框增加「单包目标时长（小时）」输入，默认 2，最小 0.01；旁边实时显示「将生成 N 个数据包」（N 按后端同样的规则：整包数 + 余量包 1 个），提交时作为 `default_package_duration_hours`。

### 9.4 测试

- 后端：任务列表 `pending_assignment_count` 在待分配、已分配、作废混合时正确。
- 前端（源码与纯函数断言）：
  - 任务行、抽屉顶部存在 `openMiningAssignModeDialog` 入口；数据包行存在带 `canAssignPackage` 条件的 `openMiningAssignDialog` 入口。
  - `canAssignPackage` 仅对 `pending_assignment` 为真；`miningTaskAssignDone` 按 F2 且不读 `batches`。
  - 「仅分配待分配数据包」开关已移除；设备、窗口字段带演示模式条件。
  - 新建任务包数预览与后端拆分规则一致（如 100 小时、单包 2 小时 → 50；5 小时、单包 2 小时 → 3）。
- 手工验收（UAT）：新建任务填写单包时长 → 包数符合预览 → 用两种入口分配 → 状态在进出抽屉前后不变 → 全部分配后显示「已完成」，「更多」中清单下载可用。

## 10. 后续规格

- 产能看板与人效看板的二期范围按
  [《按现有元数据裁剪的二期范围》](2026-09-30-collection-dashboard-capacity-efficiency-scope.md)
  执行：只做项目/任务分布与完成度、原始/有效时长趋势、人员/设备时长、人员有效产出排名和人员时长明细。
- 按采集类型、机器人类型、数据类型、地区、工作时长、活跃时长和人效比率本期剔除；这些指标需要正式字段或 App 事件模型，不能从 `metadata_json` 推断。
