# 代码与规格差异修复计划（2026-09-22）

状态：本轮代码修复已执行。G01/G02/G03/G05/G06/G08/G09/G10 已实现并完成定向回归；G04 暂缓，G07 保留现有链路。真实 OSS/duance 联调及历史退役测试迁移仍待后续。

勾选说明：`[x]` 表示本轮已有代码和定向测试证据；未勾选项是需要真实对象存储、外部客户端或历史测试迁移的后续验收，不把 mock、skip 或静态核对当成完成。

基线：`e03acc8`；问题证据见 [差异报告](CODE_SPEC_GAP_REVIEW_2026-09-22.md)。此前计划的完成勾选属于历史记录；与本次决定冲突时，以本计划及同步修订的规格为准。

## 1. 本期决定

| 项目 | 本期处理 |
| --- | --- |
| G01 取数范围 | 按授权 scope 解析 Episode；批次和工项只用冻结成员及来源版本，越权显式拒绝 |
| G02 包与批次 | 整包选择、有效 Episode 入批；当前禁止同包重复建批，保留未来多批模型，不增加包 ID 单列唯一约束 |
| G03 标注契约 | 包级工作项，`payload.episodes` 逐个提交全部必需 Episode 的真实标注 |
| G04 本地默认 MinIO | 暂不修复，不阻塞本期；不将绕过签名校验的测试算作默认配置验收 |
| G05 取数参数 | 去掉 `include`，完整来源文件和元数据；TTL 默认 3600 秒；`oss_network=internal/public`，默认 internal |
| G06 授权免审 | 有权限且明确关闭复核时直接完成该项，保存标注和审计，按整个批次完成条件发布 |
| G07 原生 LeRobot | 直接进入 export，保持独立数据集来源链路，保护被版本引用的来源对象 |
| G08 入库审核 | 单包一次最终结论；抽屉只读、审核进入全屏；撤回多轮历史 UI 要求 |
| G09 旧链路 | 下线旧 task-set 对外入口及旧物理发布调度，保留新链路共用能力和历史数据 |
| G10 幂等 | 同键同内容返回首次结果；同键不同内容 409；并发、回滚与发布副作用一并处理 |
| 补采/拆包/分配 | 最终审核后补采新建关联包；优化未分配包调整、批量分配及任务进度，预留在线下发边界 |

本期不开放同包多次建批，不建设完整在线采集客户端、在线消息系统、自助抢单或复杂多轮入库审核。

## 2. 采集业务与冻结边界

```text
采集任务（目标时长、设备型号、SOP、标签）
  → 按时长拆成待分配数据包
  → 管理员将数据包分配给数采员
  → 离线清单下发（未来可替换为在线下发）
  → 上传、解析、校验；最终审核前可修复失败来源
  → 一次最终入库审核
       通过：固定有效/不合格/排除明细 → 建批 → 治理/标注/审核 → 资产
       驳回：原包作废，保留原因
  → 如需补采：管理员在同一任务下另建包，关联原包和补采原因
```

- 分配锁定项目、任务、目标时长和数采员归属；分配错误作废后新建。任务表单不增加人员字段，分配流程不要求另选一个“负责人”。
- 最终入库审核锁定 accepted/rejected/excluded、准入 attempt、来源版本和入库有效时长；之后不能给原包加数据再审。允许同一次请求幂等重放。
- 建批锁定包清单、有效 Episode、来源版本、标签及流程配置；不因后台晚到结果扩大成员。
- 标注审核完成并生成资产时固定标注修订和治理结果；后续操作不能改写已有资产。
- 最终审核前的系统重试读取相同来源；修复损坏/缺失来源产生新 attempt，保留旧失败记录。审核与来源变更必须串行校验，旧 worker 不能推进新 attempt。
- 最终审核后的补采使用新包。任务目标不增加，旧包结论不回滚、不转移旧 Episode，也不重复计数。

例：任务目标 10 小时，包 A 最终入库有效 8 小时，管理员新建关联 A 的补采包 B，目标 2 小时。任务目标仍为 10；B 在执行时显示“有效 8、在途预计 2”，B 审核通过后有效为 10。

任务展示目标与已入库有效时长，旁边列出待分配、执行中的包及其目标时长，供管理员判断缺口。不自动补包、不自动分摊补采缺口、不把在途目标算入完成量。補采包仍是普通数据包，只额外保存来源包、原因和创建人。

## 3. 实施任务

### P0：统一契约与准备验证环境

- [ ] 对照本计划核查 API 字段、状态、错误码和文档示例；本次已修订关键规格口径，实施时继续补充准确的 API 文档和客户端接入说明。
- [ ] 准备独立 PostgreSQL、Redis 和对象存储测试资源；确认测试配置与共享服务隔离后运行会重建 schema/flush Redis 的测试。
- [x] 盘点历史重复审核、多批关联、旧任务和实际前端调用者，生成只读检查结果，作为迁移与 G09 下线依据。

约束：新 schema 使用增量迁移，不重写既有基线；不为匹配新规则删除历史审核或包→批关联。历史完成记录和本次验证结果分开记录。

### P1：一次最终入库审核与新包补采（G08 / G02 基础）

涉及：`backend/data/models/data_package.py`、`services/collection_intake_review.py`、`services/collection_packages.py`、`routers/collection_packages.py`、`services/collection_upload_sessions.py`、`services/collection_upload_intake.py`、`services/collection_upload_parse.py` 及相关增量迁移（后五项相对 `backend/data/`）。

- [x] 以包锁和事务保证只能形成一次最终审核结论。重复相同请求返回原结果，修改结论/成员的重放返回 409；并发审核不重复累加时长。
- [x] 通过时保存精确来源与准入事实；整包驳回作废。全部不合格时沿用零有效时长的展示规则，但不能生成空批/空资产。
- [x] 上传入口、声明、上传完成及异步落库共同检查冻结边界；补齐最终审核前失败来源修复入口。终审后的晚到结果保留技术记录，不能改变业务有效清单或时长。
- [x] 新增补采创建能力：同任务新包关联来源包、补采原因、创建人，复用任务设备型号/SOP/标签规则；创建与审计幂等，不自动增加任务目标。
- [x] 旧审核记录完整保留；历史多轮包继续引用原有有效来源，不能简单只取最后一条而丢掉历史有效成员。如需整理权威快照，先做只读差异检查，再做可核对的增量回填。

验收：审核与审核、审核与上传修复、审核与解析完成并发结果一致；旧 attempt 无法覆盖新事实；通过/驳回后的原包不可追加再审；新补采包完整走一次独立流程，任务时长不重复累计。

### P2：拆包、分配、进度与审核页面（G08）

涉及：`backend/data/services/collection_tasks.py`、`collection_packages.py`、采集概览服务与对应路由；`frontend/js/app.js`、`api.js`、`mining-console.js`、`demo-data.js`。

- [x] 复用现有待分配包 add/delete/目标时长调整事务，补全拆分、合并操作；限制同任务、未分配包，拆合前后总时长守恒。已分配包禁止调整、合并或改派。
- [x] 分配统一为一包一个数采员；批量选择多名数采员时将不同包分给他们，每个包明确一个人，原子提交。既有双字段兼容保存，新分配两字段同值，界面不再要求负责人/操作人两个角色。
- [x] 包详情提供“创建补采包”，创建同任务普通包，只附加来源关联、原因、创建人；管理员手动填写目标时长，不增加任务目标。
- [x] 任务展示已入库有效时长与各待分配/执行中包目标，不新增自动补采计算和调度。
- [x] 抽屉移除直接通过/驳回，保留详情和进入全屏审核；最终审核后展示只读结论、审核人/时间、accepted/rejected/excluded 和原因。前端可审核条件与后端一致。
- [x] 全屏提交确认明确本次有效及排除数量、结论不可追加、补采另建包；批量通过复用同一后端门禁并展示排除明细。
- [x] 同步 demo 状态与真实 API，避免页面演示仍允许已分配调整或终审后再次审核。

在线预留仅保持稳定 package ID 与清晰的人员分配关系；本期不新增交付状态、接收回执、交付 attempt 模型，也不开放 online 入口。未来在线下发复用已分配包，传输重试不能生成补采包。

验收：创建任务自动拆包、未分配包拆合、批量分配、错误分配作废重建、补采和离线清单可用；抽屉没有审核写按钮；10→8+2 示例在真实接口和页面一致。

### P3：当前一包一批限制与快照（G02）

涉及：`backend/data/services/data_batches.py`、`models/data_batch.py`、建批路由与前端建批选择器。

- [x] 候选列表排除已有批关联的包，不能仅凭包状态或“有未占用 Episode”再次入选。
- [x] 创建事务按稳定顺序锁包，校验最终审核通过及尚无批关联；并发建批只有一方成功，另一方明确冲突，多包请求不留半成品。
- [x] 按最终入库有效成员保存来源版本/准入事实和流程快照；被排除项、后来完成项不会进入治理、工项、资产或 Export。
- [x] 保留包→批多关联及联合唯一约束，不新增 `data_package_id` 单列唯一约束；现有 Episode 占用限制本期保持，未来复用策略另定。

验收：重复及并发建批拒绝；混合包只冻结有效成员；后续来源、任务标签或异步结果变化不改写已有批/资产；历史多批记录可读。

### P4：准确、完整且可配置的取数（G01 + G05）

涉及：`backend/data/routers/fetch_manifests.py`、`services/fetch_manifests.py`、`infra/object_storage.py`、对象存储 provider、`infra/oss_client.py` 和 endpoint 配置。

- [x] 按 scope 分别解析和授权全部请求 ID；他人工项 403，不存在或跨工作空间按既有资源隐藏策略处理，不以成功空清单掩盖拒绝，也不返回部分已授权结果。
- [x] 数据批直接使用批成员，工项使用冻结工项成员；来源对象同样绑定对应快照，不能只固定 Episode ID 却读取后来替换的文件。
- [x] 返回范围内完整来源文件组和规定元数据，包括 size、SHA-256、mime、只读 URL、准确过期时间；不把任意衍生产物都当来源，也不静默丢失必需文件。
- [x] 请求不接受 `include`；增加 `url_ttl_seconds`（默认 3600）与 `oss_network`（默认 internal，可选 public）。TTL 按 provider 支持范围校验，不能静默截断。
- [x] 网络选项只映射服务端配置，在选定 endpoint 上直接签名；配置缺失明确报错，不自动切网，不在签名后替换域名。工具内网取数不套用浏览器公共 endpoint 限制。

验收：包 E11/E12、批/工项只含 E11 时只返回 E11；多个文件完整返回；默认参数、自定义 TTL、非法参数、配置缺失及越权全部覆盖。使用配置好的内网/公网环境实际下载并核对文件及有效期；单测签名字符串不能替代下载验收。

### P5：真实标注提交与持久幂等（G03 + G10）

涉及：`backend/data/routers/annotation_work_items.py`、`services/annotation_work_items.py`、幂等持久化模型/迁移、审计与提交后调度、外部接入 spec 和 API 示例。

- [x] 统一包级工项契约：`payload.episodes` 的 key 为 Episode ID，值为合法 `QrdfEpisodeAnnotation`；完整覆盖必需冻结成员，缺失/额外成员明确拒绝。禁止用空 payload 绕过算法提交验证。
- [x] 按工作空间、用户、client_request_id 保存规范化请求摘要和首次完整响应；摘要覆盖全部 items、payload、source 和请求的 review_required。补全默认字段、固定对象键序；列表保持顺序，不擅自把不同列表当相同请求；拒绝重复 work_item_id。
- [x] 在任何业务写入前原子取得持久幂等占位。并发同键协调到同一结果，不发生两次业务提交；同键不同内容返回 409。不能只在执行后写缓存。
- [x] 幂等占位、标注修订、工项状态与可持久化审计在同一事务提交；任一 item 校验失败整体回滚。发布任务采用事务内持久任务/提交后可靠调度，重放不重复发布。
- [x] 重试仍检查当前身份和访问范围；摘要保留原始审核意图，首次结果保存实际生效的审核策略。权限变化不重新执行已完成请求或改写既有结果。
- [x] 对无摘要的旧缓存明确处理：能从持久信息验证时回放，无法验证时返回可识别的旧键冲突并要求新请求 ID，不能盲目认作同内容成功。
- [ ] 补齐真实标注示例；客户端 `duance` 不在本仓库，交付准确契约与联调样例，客户端修改和实测另行记录，不能标为本仓库已验收。

验收：多个 Episode 的请求经过真实 submit、QRDF 校验和修订持久化；同键同内容、只变 JSON 键顺序、改变 payload/source/review_required、并发同键、失败后重试、多 item 中途失败均有集成验证；仅生成一组有效修订和应有审计/发布任务。

### P6：补齐授权免审的完成与发布（G06）

涉及：`backend/data/services/annotation_work_items.py`、`review_work_items.py`、`data_assets.py` 及对应路由/测试；依赖 P5。

- [x] 默认需复核；无 `annotation:approve` 权限请求免审时降级 true，返回实际生效值并审计。
- [x] 有权限的本人已分配工项免审提交，完成内容校验与修订保存后结束该项审核等待；明确记录免审人、来源和依据，不伪造人工审核动作。
- [x] 统一普通审核与免审的批次完成判断，全部必要流程完成才发布；混合批次保留其他工项的审核待办。
- [x] 资产引用此次完成时固定的标注修订，重复提交或调度不产生重复资产。

验收：默认复核、无权限降级、有权限免审、免审与人工复核混合批次、并发完成和重放均正确；返回开关、实际状态、待办及资产一致。

### P7：下线旧入口与旧调度（G09）

涉及：`backend/data/main.py`、`routers/workspace.py`、旧原生 LeRobot 路由、`tasks/batch_workers.py`、相关任务创建/恢复入口；`frontend/js/api.js`、`app.js` 及旧 UI 测试。

- [x] 清点旧 task-set/create/list/options 与旧上传/发布调用者，将仍用于“采集项目”的页面改为 CollectionProject API；保留 workspace 管理及成员接口。
- [x] 撤下旧 task-set 对外路由、前端调用和入口；明确退役 `episode_publish`、`native_lerobot_scan`、`native_lerobot_copy`。逐个确认 `import_*`、`dataset_export`、`native_lerobot_bundle` 的消费者，属于旧链路的同步退役。
- [x] 删除旧任务生产入口和调度/恢复注册；发布切换前停止新增、盘点存量，执行中任务排空，未执行旧任务按明确退役原因终止并保留记录，不能丢成未知 handler 后无限重试。
- [x] 保留新采集 parse/admission、共享 QRDF/媒体处理、Catalog Export、`native_lerobot_direct_validate` 等真实消费者；先提取共用函数再删除旧实现。
- [x] 保留历史模型、对象引用和审计；旧数据迁移与物理清理不混入本次下线。

验收：新项目创建调用新模型；旧路由不再暴露且没有 UI 请求；旧任务不再创建/调度/恢复；新采集→资产→Catalog Export 和原生 LeRobot 直传仍可运行。搜索命中只用于排查，不能作为唯一验收。

### P8：联合回归与交付记录

- [ ] 跑下表中相关后端用例和前端全量，再通过真实 API/页面验证主链；禁止把真实 submit/审核替换为直接改状态来证明闭环。
- [ ] 真实主链覆盖：任务拆包分配→上传及终审前修复→一次入库审核→新包补采→建批→取数→真实标注与幂等→人工/免审→资产与 Catalog Export。
- [ ] G07 回归原生 LeRobot 直接上传 export、校验、登记版本；来源引用保护有效，Export 输出独立，无反向 QRDF 或为了版本登记而复制来源。
- [ ] 更新 README、DEVELOPMENT_HANDOFF、API 文档和差异报告，逐项记录改动、验证命令/环境/结果、跳过原因及剩余限制。

## 4. 依赖与执行顺序

1. P0 确定测试隔离与历史数据边界。
2. 优先完成 P4（G01/G05）、P5（G03/G10），分别解决取数范围和真实提交；P6 接在 P5 后。
3. 采集侧按 P1 → P2/P3 推进：先确定最终审核/补采模型，再完成页面、拆包分配和建批约束。
4. P7 的清点可先做；产品入口切换、新链回归通过、存量旧任务处置后，再撤销旧注册。
5. P8 完成联合验收。上述独立任务可以分开交付，所有复选框凭实际结果勾选，不沿用历史测试数字。

## 5. 验证矩阵与完成条件

以下是现有用例扩展入口，不表示现有测试已覆盖新要求；缺失场景随任务新增。

| 范围 | 现有测试入口（backend/tests/ 或 frontend/tests/） |
| --- | --- |
| 单次审核/上传修复 | `test_collection_intake_review_api.py`、`test_collection_upload_sessions_api.py`、`test_collection_upload_intake_api.py`、`test_collection_upload_parse.py`、`test_collection_admission_worker.py` |
| 拆包/分配/补采/进度 | `test_collection_packages_api.py`、`test_collection_tasks_api.py`、`test_collection_overview_api.py`、`test_collection_task_manifest_export.py`；`mining-console.test.mjs` |
| 建批与冻结 | `test_data_batches_api.py`、`test_data_batch_models.py`、`test_episode_admission.py` |
| 取数/网络/TTL | `test_fetch_manifests_api.py`、`test_object_storage_contract.py`、provider 契约测试 |
| 提交/幂等/免审 | `test_annotation_batch_submit_api.py`、`test_annotation_work_items_api.py`、`test_review_work_items_api.py`；新增事务并发场景 |
| 全屏审核 | `data-package-intake-review.test.mjs`、`intake-review-console.test.mjs` |
| 旧链下线/新链保留 | `test_legacy_native_lerobot_retirement.py`、`test_native_lerobot_direct_migration.py`、`test_batch_worker_recovery.py` |
| 联合主链 | `test_external_tool_e2e.py`、`test_data_backend_e2e.py`，另补真实页面与配置好的 OSS 下载验证 |

完成条件：G01/G02/G03/G05/G06/G08/G09/G10 均有实际验收证据；G07 维持已确认职责并完成受影响回归；G04 明确保留为暂缓项。新在线下发客户端和未来同包多批另立计划，不算本期漏项。环境缺失或客户端仓库未联调时明确记为未验证，不以 mock 或 skip 填补通过记录。
