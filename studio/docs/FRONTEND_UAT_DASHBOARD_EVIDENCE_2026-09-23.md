# F01 真实看板数据验收（2026-09-23）

范围为 UAT `workspace3` 的既有真实采集、入库、标注审核事实，以及空的 `workspace4`。使用既有管理员 API key2 读取；SQL 查询使用只读事务。未创建采集样本、统计人数、人工统计行或训练任务。本文不记录凭据或临时签名地址。

## 统计口径

[初始化设计 §6](superpowers/specs/2026-09-14-collection-studio-init-design.md) 规定采集管理与任务进度使用入库审核后的有效时长，资产与数据集使用治理后的有效时长；[标注工作台设计 §7](superpowers/specs/2026-09-22-collection-review-annotation-workbench-design.md) 进一步规定，启用标注时下游只消费已审核的有效片段，标注时长不能回写原包入库时长。人员工时统计不在本轮范围。

| 对象 | 采集源时长（秒） | 入库验收有效（秒） | 已审核标注有效（秒） | 结论 |
| --- | ---: | ---: | ---: | --- |
| task2 / package4 / Episode3 | 419.554561024 | 419.554561024 | 19.399389184 | submission2 审核通过，asset2 已发布 |
| task3 / package5 / Episodes4、5 | 20 | 0 | 0 | 两条均拒收，包作废 |
| task3 / 补采 package6 / Episodes6、7 | 20 | 10 | 0 | Episode6 拒收；Episode7 审核确认无有效片段，batch3 `no_publishable_asset` |
| workspace3 总计 | 459.554561024 | 429.554561024 | 19.399389184 | 原始采集、入库有效与标注有效分别展示 |

源时长使用持久化 metadata 的 `end_timestamp_ns - start_timestamp_ns`，缺少端点时才使用准确 `duration_s`，不从 `Numeric(10,2)` 小时反推。task2 目标 0.12 小时与 task3 目标 0.02 小时合计 504 秒；补采没有扩大原任务目标。

`total_duration_s` 保持兼容，仍是可计数 Episode 的物理时长总和（含旧模型的派生 Episode）；新的 `source_duration_s` 只累计源 Episode，避免将二者当成同一口径。在当前 workspace3 无派生 Episode，因此两者仅有旧字段三位小数舍入的差别。

`collected_today` 与 `collect_trend_7d` 沿用 Episode 在平台创建／接入的 UTC 日期，不是 QRDF 原始采集日期。当前样本两者恰好均在 2026-09-22 UTC，因此当日接入 5 条；前端应注明「接入」及 UTC，不能将导入历史数据称为当天实地采集。

## 修复前真实证据

UAT 数据库为 `0006_dashboard_package_facts`。只读核对确认 Episode3 至 Episode7 已进入 `dwd_episode_fact`，旧 `task_set_id`、`batch_id` 为 NULL 的包级 Episode 没有遗漏。

但其状态投影仍取旧 Episode 业务字段：5 条均被归为 `intake`，API 返回待采集 5、标注完成 0、QRDF 已发布 0。实际已存在发布资产与已审核无有效片段终态，因此这是状态投影缺陷，不能通过改写源 Episode 业务状态纠正。

旧增量条件只检查 `Episode.updated_at` 与旧 `PublishedEpisode`，包、批次、工作项、固定 submission 审核和资产发布不会使 DWD 失效。读到的 DWD 更新时间早于最终包级审核。workspace3/4 当时均无 scope 对应的持久 ADS 快照或 `dashboard_etl` JobRun。

旧 collection-overview 返回 intake `0.12` 小时，补采验收通过的 10 秒会在两位小数小时存储中舍为 `0.00`。空 workspace4 的 KPI、设备数量均为零，但仍返回七个零值日期点；没有正数假数据。

## 后端修复与接口

新增迁移 `0007_dashboard_package_lifecycle`，仅扩展仓库事实；源 Episode 的业务状态、更新时间和原文件保持不变。

- 按确切 `DataBatchEpisode` 关联包、批次、标注工作项、不可变 submission 与审核；发布状态与有效段取固定资产快照。
- 增量修订摘要包含包、入库审核、批次成员、批次、标注工作项、submission、审核工作项、审核决定与资产的身份、时间和关键状态。比较摘要而不是只比较最大时间，避免未来时间遮住其他来源变化；最大时间仍保留为证据。投影版本触发既有 DWD 回填，无需修改来源业务数据。
- 已发布、已拒收、入库冻结排除、QC 丢弃与明确无有效片段终态不计入待办队列。已审核无有效片段计入标注完成，但不计入已发布资产与有效时长。
- `dashboard.kpis` 新增 `source_duration_s`、`intake_valid_duration_s`、`annotation_effective_duration_s`；`duration_basis` 提供各字段口径，`time_zone: "UTC"` 明确日统计边界。空空间返回 `collect_trend_7d: []`。
- `collection-overview` 新增 `target_duration_s`、`captured_duration_s`、`intake_valid_duration_s`，旧小时字段保留。`duration_basis` 改为 `intake_valid_duration_s`。存在缺失的源时长时返回 null，不能用旧小时值补造精度。

## 验证记录

独立 PostgreSQL/Redis 回归执行 `test_dashboard_warehouse.py`、`test_dashboard_package_lifecycle.py`、`test_collection_overview_api.py`：30 passed；数据库从空 schema 经完整 Alembic 迁移到 0007。指定后端文件 pre-commit 通过。

新增回归经真实草稿保存、提交、审核与资产发布服务验证：混合有效／无效 Episode 只发布有效成员；全无效产生 `no_publishable_asset`；已审核有效时长与采集／入库时长分离；每一步都由包级依赖触发增量刷新；源 Episode 四个业务状态及 `updated_at` 不变；重复刷新不重写事实；旧投影版本自动回填；DWS/ADS 与 live 结果一致；10.000000001 秒保留；空 workspace 无趋势与设备项。

独立边界复核补测：部分入库时 admission 失败而被 `excluded_episodes_json` 冻结排除的 Episode 不进入有效量与待办；来源包时间在未来七天时，当前工作项提交仍触发刷新；纠正未来时间、最大水位倒退后也正确重投影。任务删除方面，当前 collection-task 无删除接口；包调整仅允许删除未分配包，已有 Episode 的关联另受数据库外键保护。现有 DWD 孤儿清理继续处理真正已删除的 Episode，不凭空保留计数。

## 修复后真实 UAT

UAT 部署 `798b23f` 后确认迁移为 `0007_dashboard_package_lifecycle`，API 与 analytics worker 已重启。使用既有 `POST /dashboard/refresh` 分别刷新 workspace3/4，通过正常 JobRun → analytics worker → DWD/DWS/ADS 链路完成，没有直接插入统计结果。

| 空间 | JobRun | 终态 | ADS computed_at（UTC） |
| --- | --- | --- | --- |
| workspace3 | `9c13bddc378b433e820adc8bf2c3f653` | succeeded | 2026-09-22 17:44:28.586840 |
| workspace4 | `829f6b1183c14f9babfedfdc6356f037` | succeeded | 2026-09-22 17:44:28.927587 |

workspace3 实际 API 与持久 ADS 的完整 KPI 对象一致：5 条源 Episode；采集 `459.554561024` 秒、入库有效 `429.554561024` 秒、已审核标注有效 `19.399389184` 秒；标注完成 2/2，完成率 1.0；已发布 1；所有待办队列均为 0。collection-overview 实际返回目标 `504` 秒、采集 `459.554561024` 秒、入库有效 `429.554561024` 秒，同时保留旧小时字段。

只读 SQL 核对 5 条 DWD 均为投影版本 2，且有依赖修订摘要：Episode3 为 `published / stored`，effective ns 为 `19399389184`；Episode7 为 `no_valid_segments / annotated`，effective ns 为 0；Episode4、5、6 均为 `intake_rejected`。五条 `queue_key` 都是 NULL。所有终态仍保留原始采集时长，未被标注有效时长覆盖。

workspace4 的 API 与 ADS 均为 0 条、各时长 0、待办 0、`collect_trend_7d: []`、`device_distribution: {total: 0, items: []}`。两个空间都没有人员工时或人数伪造项。

刷新前后 Episode3—7 的 `workflow_status`、`quality_status`、`annotation_status`、`review_status`、`validity_status`、`updated_at` 与完整 timing 元数据逐值相等。源业务状态仍为原先的 discovered/pending；新状态仅存在仓库投影中。

可复核证据保存在本机 `/tmp/quicstudio-uat-dashboard-before-fix.json`、`/tmp/quicstudio-uat-dashboard-after-fix.json`、`/tmp/quicstudio-uat-dashboard-db-before-fix.json`、`/tmp/quicstudio-uat-dashboard-db-after-fix.json`。`/tmp/quicstudio-uat-verify-dashboard-after.py` 的断言全部通过。这些文件只含白名单业务字段与统计结果，不含 API 凭据或签名 URL。
