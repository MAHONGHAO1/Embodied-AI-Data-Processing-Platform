# 代码与 spec 差异核对（2026-09-22）

核对基线：`e03acc8`；开始检查时工作区干净。报告随讨论更新：G02 保留未来同包多批扩展能力，G03 明确包内逐 Episode 提交，G04 暂不阻塞，G05 明确完整清单、默认一小时 TTL 和默认内网 OSS，G06 确认补齐免审流程，G07 确认原生 LeRobot 直接进入 export，G08 按单包一次最终入库审核收口，G09 下线旧链路，G10 修复内容冲突与并发幂等。本轮已完成代码、迁移、前端和定向回归；真实 OSS/duance 联调仍未纳入本次提交。执行记录见 [修复计划](CODE_SPEC_REPAIR_PLAN_2026-09-22.md)。

结论：G01/G02/G03/G05/G06/G08/G09/G10 已按本轮产品口径收口；G07 已对齐；G04 继续保留为暂不阻塞项。当前限制是：同包多批模型仍保留但业务入口暂不开放，入库审核后的补采走新包，取数真实 OSS 下载和 duance 客户端尚未联调。另有少量历史测试仍针对已退役入口，需要后续按新契约迁移。

## 核对口径

覆盖 `docs/superpowers/specs/` 下 5 份设计规格，以及平台侧相关 API、服务、前端和测试。按依赖关系解释增量修订：

- 2026-09-22 用户最新明确：当前按包组织数据批，暂不支持一个包多次建批，后续会开放这一能力。此限制通过当前前端入口和后端业务校验落实；包→批数据模型保留多关联能力，不要求增加包 ID 单列唯一约束。此前本报告要求数据库永久限制一包一批的建议撤回。
- 09-18 存储规格已把完整性/适用 Preview 改为审核前置条件。
- 09-22 前端规格已把入库审核改为全屏页。
- `duance` 客户端不在本仓库，本次未独立验证其 SQLite 迁移、凭据保存和同步 UI。
- 训练业务本身不属于这些 Data spec 的交付范围；本次只看 Data→Train 交付约定。

P1 表示应先修复的数据范围或主流程问题；P2 表示接口、状态语义、存储职责或收尾问题。以下结论区分代码静态证据与实际执行的定向检查，不把历史测试数字当成本轮结果。

## 已有实现的部分

| 范围 | 代码核对结果 |
| --- | --- |
| 采集项目/任务/分包/分配 | 有独立领域模型与服务，存在项目归档限制、分配锁定等校验 |
| 逐 Episode 准入 | 有持久准入事实、attempt、指纹、完整性/Preview/输出验证门禁；审核与建批读取数据库事实 |
| 建批占用 | `DataBatchEpisode.episode_id` 有唯一约束；包→批允许多条关联可作为后续扩展基础，本身不列为缺陷；当前业务入口仍需统一限制重复建批，见 G02 |
| 逻辑资产与版本 | 有来源对象身份、标注修订、报告和固定成员快照；资产登记主要为数据库写入 |
| Catalog Export | 已有异步任务、真实物化/校验/上传、完成标记、失败恢复与独立 attempt；不是 README 所说的 stub |
| API 令牌 | 有自助签发/列表/轮换/吊销、哈希保存、过期和使用鉴权；前端有一次性展示与复制 |
| 最新前端收尾 | 全屏入库审核、排除项/失败原因/隐私提示、算法来源和低置信度筛选均有实现 |

以上表示存在相应实现，不等同于本轮重新完成生产验收。

## 差异清单

### G01 · P1：取数范围从冻结 Episode 扩大为整个包

规格：外部接入 §6.2 要求只访问分配给自己的工作项；后端补全 §4.3 要求批次和工作项沿用冻结 Episode 清单。

现状：已修复。`data_batch` 使用 `DataBatchEpisode` 的冻结成员，`my_annotation_work_items` 使用工作项冻结成员；数据包和采集任务 scope 才按当前包内容展开。他人工作项在授权后显式返回 403，跨工作空间或缺失 ID 返回 404，不返回部分结果。

证据：[按 scope 固定成员](/Users/qingmuhy/Documents/devlop/quicrobot/quic_studio/backend/data/services/fetch_manifests.py:24)、[请求授权](/Users/qingmuhy/Documents/devlop/quicrobot/quic_studio/backend/data/routers/fetch_manifests.py:39)。`test_fetch_manifests_api.py` 已覆盖批次/工项只返回冻结 Episode 和越权拒绝。

### G02 · P2：当前重复建批入口限制与冻结边界需统一

当前产品口径：按数据包组织数据批，当前不开放同包多次建批，未来会支持。保留包→批多关联模型；不以缺少 `data_package_id` 单列唯一约束判定实现错误，也不要求为了本期限制做破坏性的模型收缩或历史数据迁移。

现状：已修复。`DataBatchPackage` 仍保留联合唯一约束和未来多关联能力；候选列表排除已有批关联，建批事务要求 `intake_approved` 且无既有批关联，并按稳定顺序锁定包。最终入库审核有独立的一次性门禁。

证据：[包关联约束](/Users/qingmuhy/Documents/devlop/quic_studio/backend/data/models/data_batch.py:105)、[候选列表](/Users/qingmuhy/Documents/devlop/quic_studio/backend/data/services/data_batches.py:74)、[建批状态限制](/Users/qingmuhy/Documents/devlop/quic_studio/backend/data/services/data_batches.py:219)、[入库审核服务](/Users/qingmuhy/Documents/devlop/quic_studio/backend/data/services/collection_intake_review.py:72)。本轮已完成代码修改，并由批次、审核、补采、取数和标注提交定向回归覆盖；完整历史测试仍需按旧链路迁移。

建议的冻结时点如下。冻结对象是业务归属、审核事实及具体批次/资产的快照，不把数据包设计成永久不可复用的实体。

| 时点 | 冻结内容 | 仍可进行的操作 |
| --- | --- | --- |
| 数据包分配 | 项目、任务、目标时长、责任人及实际采集人员 | 最终审核前上传与失败重试；分配错误作废后新建 |
| 入库审核形成最终结论 | accepted/rejected/excluded 清单、准入 attempt、来源版本和入库有效时长 | 通过包进入建批；整包驳回作废；需要补采另建关联包 |
| 创建数据批次成功 | 本批包清单、有效 Episode 清单、来源版本、标签与流程配置 | 按既定流程治理、标注、审核及失败重试；不变更本批输入 |
| 标注审核通过并生成资产 | 本资产采用的标注修订及治理结果 | 后续操作不改写已生成资产的修订快照 |

例如包内 8 项就绪、2 项失败：最终审核前可以先修复失败来源再审核，也可以确认排除 2 项，一次审核通过当前有效的 8 项。结论形成后，不再向原包新增或替换数据重新审核；需要补采时在同一任务下新建关联包。建批展示失败、处理中和未审核项的排除数量，迟到结果不能追加成员。未来开放同包多次建批时另建批次快照，历史批次与资产保持不变；是否允许同一 Episode 重复参与不同批次，届时单独定义。

当前建议：前端候选和后端建批校验排除已有批关联的包；后端在事务中锁定包并检查已有批关联，防止并发绕过本期限制。保留现有多关联模型和批内成员快照，不新增包 ID 单列唯一约束。冻结标注修订放在审核完成、生成资产时，不能在建批时把尚未完成的标注一起锁死。

原 G02 要求立即实现“同包 8 项先建批、2 项另建新批”的建议撤回。此前 `package_not_uploadable` 的定向结果保留为代码事实：仅对尚未形成最终审核结论的包补齐失败来源修复入口；不开放 `intake_approved` 包继续补传再审。未来同包多次建批是复用固定输入的独立能力，不等同于重新打开入库审核。

### G03 · P1：算法批量标注的规格 payload 不能实际提交

已确认的修改方向：**标注工作仍按数据包派发，提交时按包内 Episode 分别填写标注结果。** 一个工作项可包含多个有效 Episode，因此每份标注必须明确对应的 Episode ID。这里使用的是该工作项冻结的有效成员，不包含包内被排除的失败项或未审核项。

例如，工作项对应数据包 A，需标注 Episode 101 和 102。提交结构应为 `payload.episodes` 中分别放入 `"101"` 对应的标注、`"102"` 对应的标注，服务端检查两项是否齐全、是否属于该工作项。批量接口外层的 `items` 可以提交多个工作项，每个工作项内部仍按这个规则组织。

修订前的规格与代码差异：外部接入 §7.2 示例直接使用 `payload={mode,segments,outcome,rating}`，没有说明这份标注属于包内哪个 Episode；现已统一为 `payload={episodes:{"<episode_id>": annotation}}`，并检查是否覆盖工作项所需的全部 Episode。

证据：[真实 payload 校验](/Users/qingmuhy/Documents/devlop/quicrobot/quic_studio/backend/data/services/annotation_work_items.py:226)、[批量提交调用真实 submit](/Users/qingmuhy/Documents/devlop/quicrobot/quic_studio/backend/data/services/annotation_work_items.py:488)。定向调用实际校验函数已复现该错误。

现有测试为什么没发现：[批量提交测试](/Users/qingmuhy/Documents/devlop/quicrobot/quic_studio/backend/tests/test_annotation_batch_submit_api.py:71) 把真正的提交函数 `submit_annotation_work_item` 临时替换成了“直接标记已提交”，没有实际检查和保存标注内容；外部工具 E2E 使用空 payload。因此测试通过不能证明客户端照文档提交真实标注能够成功。

已落实：批量接口经过真实 submit、QRDF 标注校验、修订保存和工作项状态更新；测试覆盖完整、多 Episode、缺失/额外 Episode、同键冲突、并发和回滚。客户端 `duance` 不在本仓库，仍需外部联调。

### G04 · 暂不阻塞：默认本地 MinIO 配置不能走产品签名直传

处理决定：按用户意见，本项保留事实记录，暂不安排修复，不作为当前推进的阻塞项。此决定不等于该链路已经通过验收，也不影响 G05 的内网/公网 OSS 取数需求。

规格：三桶规格 §4、§10.2 要求开发 MinIO 和通过签名地址完成 multipart 直传。

现状：Makefile 默认签名 endpoint 是 `http://127.0.0.1:9000`；通用浏览器 URL 策略仅允许公共 HTTPS。采集签名接口因此拒绝默认本地 MinIO，取数清单也会返回 `available:false`。

证据：[默认配置](/Users/qingmuhy/Documents/devlop/quicrobot/quic_studio/Makefile:42)、[URL 策略](/Users/qingmuhy/Documents/devlop/quicrobot/quic_studio/backend/data/security/browser_oss.py:69)、[签名拒绝分支](/Users/qingmuhy/Documents/devlop/quicrobot/quic_studio/backend/data/infra/oss_client.py:381)。定向检查确认默认 endpoint 被拒绝。

验收边界：[外部工具 E2E](/Users/qingmuhy/Documents/devlop/quicrobot/quic_studio/backend/tests/test_external_tool_e2e.py:249) 明确断言签名返回 422，改用 provider client 上传；[另一组 MinIO E2E](/Users/qingmuhy/Documents/devlop/quicrobot/quic_studio/backend/tests/test_data_backend_e2e.py:140) 修改 endpoint 校验函数后执行签名上传。两类测试都有价值，但均不能证明原样默认开发配置可用。外部接入 spec 的验收备注也承认了这个限制，说明规格目标与验收口径未完全一致。

后续如需启用默认本地直传，可为开发/测试提供显式受限的本地签名策略，或提供实际可运行的 HTTPS MinIO 配置；保留生产限制，并用原样配置重跑链路。

### G05 · P2：取数清单需完整返回，并支持 TTL 与内网/公网 OSS 选择

已确认的接口约定：去掉 `include`，固定返回请求范围内的完整来源文件清单及 Episode 元数据；保留 `url_ttl_seconds`，省略时默认 **3600 秒（一小时）**；新增 `oss_network`，可选 `internal`（内网 OSS）或 `public`（公网 OSS），省略时默认 **internal**。`scope` 和权限仍决定可返回的数据范围，“全部返回”不表示返回整个桶，也不绕过 G01 的批次/工项成员限制。

请求示例：

```json
{
  "workspace_id": 1,
  "scope": "data_packages",
  "ids": [5101, 5102],
  "url_ttl_seconds": 3600,
  "oss_network": "internal"
}
```

网络选项只选择服务端预配置的 OSS endpoint，不接受调用方提供任意 endpoint、桶或对象 key。按所选 endpoint 直接签发下载 URL，不能签名后再替换域名；内网签名适用于能访问该内网 endpoint 的工具，公网调用方可显式选择 `public`。所选网络未配置时明确报错，不自动切换内外网；内网取数不应被浏览器专用的“仅公共 endpoint”校验误拦。

现状：已修复。请求不接受 `include`，`url_ttl_seconds` 默认 3600 秒并限制在 provider 支持范围，`oss_network` 默认 `internal`，可显式选择 `public`。冻结来源对象和当前来源文件组全部返回，并带 size、SHA-256、mime、版本/ETag、Episode 元数据和签名 URL。

证据：[请求模型](/Users/qingmuhy/Documents/devlop/quicrobot/quic_studio/backend/data/routers/fetch_manifests.py:20)、[完整来源对象选择](/Users/qingmuhy/Documents/devlop/quicrobot/quic_studio/backend/data/services/fetch_manifests.py:64)、[浏览器签名路径](/Users/qingmuhy/Documents/devlop/quicrobot/quic_studio/backend/data/services/fetch_manifests.py:72)、[返回元数据](/Users/qingmuhy/Documents/devlop/quicrobot/quic_studio/backend/data/services/fetch_manifests.py:142)。

已验证：`test_fetch_manifests_api.py` 覆盖默认值、TTL、内外网 endpoint、完整文件组和越权；真实 OSS 下载仍未联调。

### G06 · P2：`review_required=false` 只记录，不改变审核流程

规格：外部接入 §7.2 定义有 `annotation:approve` 权限时可关闭复核，无权限则降级并审计。

现状：已修复。无 `annotation:approve` 权限时 `review_required=false` 会降级为 true 并记录原因；有权限且明确关闭复核时，真实标注提交会直接完成该工作项并参与批次完成判断，不伪造人工审核。混合批次仍等待其他需要复核的工作项。

证据：[开关处理](/Users/qingmuhy/Documents/devlop/quicrobot/quic_studio/backend/data/services/annotation_work_items.py:480)、[无条件进入审核](/Users/qingmuhy/Documents/devlop/quicrobot/quic_studio/backend/data/services/annotation_work_items.py:348)。

批量提交定向回归已覆盖默认复核、权限降级、授权免审、发布和重放；标注修订与审计在同一事务内保存。

### G07 · 已对齐：原生 LeRobot 直接写入 export

最新产品决定：原生 LeRobot 作为独立数据集来源，直接上传到 **export**，格式与完整性校验通过后创建 catalog 数据集版本；不要求先写 raw，也不进入采集包、建批、治理和标注流程。此前规格要求“原生 LeRobot 来源必须在 raw”已修订，不再把当前桶选择列为实现缺陷。

现状：`native-lerobot-direct-uploads` 把文件写入 `export/v1/native-lerobot-direct/...`，对象引用角色固定为 `export`，校验后再创建 catalog 版本。

证据：[源文件前缀](/Users/qingmuhy/Documents/devlop/quicrobot/quic_studio/backend/data/services/native_lerobot_direct_uploads.py:122)、[固定 export 角色](/Users/qingmuhy/Documents/devlop/quicrobot/quic_studio/backend/data/services/native_lerobot_direct_uploads.py:1100)。

已同步三桶规格及后端补全规格：export 可保存原生 LeRobot 直传来源和后续导出件；两者使用独立前缀、固定对象身份和清单。版本登记引用已校验对象，不为了创建版本复制文件；被版本引用的直传来源同样受保留保护，不能作为临时导出件删除。后续 Export 保持独立任务语义，不提供反向 QRDF 转换。

本项关闭的是桶职责与实现之间的差异，不代表本次重新执行了原生 LeRobot 全链路验收。旧计划中“来源放 raw”的要求为历史记录，后续以修订规格为准。

### G08 · P2：统一全屏入库审核，按单次最终结论收口

最新口径：入库审核每包只有一次最终结论。最终提交前可修复失败来源；通过后固定有效成员及排除明细，整包驳回则作废。最终结论后的补采新建数据包并关联原包，不在原包追加数据再审。网络重试是同一操作的幂等重放，不是新一轮审核。

现状：已修复。数据包详情抽屉保留只读结论和进入全屏审核入口，写操作集中在全屏页；服务层锁包、单次结论、相同请求幂等和不同请求 409 均已实现。终审后上传、解析和准入事实写入被拒绝，补采使用同任务新包。

此前“必须展示多轮审核历史”的建议撤回：按单次最终结论，详情返回一个 `intake_review` 本身不再列为缺陷。展示 accepted/rejected/excluded、审核人、时间、原因即可，历史库中已存在的审核记录仍保留，不能为了收口删历史。

证据：[抽屉直接操作](/Users/qingmuhy/Documents/devlop/quicrobot/quic_studio/frontend/js/app.js:13683)、[单次最终审核门禁](/Users/qingmuhy/Documents/devlop/quicrobot/quic_studio/backend/data/services/collection_intake_review.py:72)。服务层锁包、单次结论、上传/解析冻结和新包补采已由后端定向用例覆盖；本轮未做浏览器真实操作。

已验证：服务层锁包、单次结论、上传/解析冻结和新包补采已由后端定向用例覆盖；浏览器真实操作仍未作为本轮验收。

### G09 · P2：旧 task-set 与物理发布链路没有完整下线

规格：外部接入开头和 §9、三桶规格 §9 要求旧 task_set/batch 对外入口及旧发布调度退出新应用。

现状：已修复。旧 task-set 路由不再挂载，前端创建/列表兼容函数改读 CollectionProject；`episode_publish`、`native_lerobot_scan/copy/bundle` 不再注册、创建或恢复，迁移会取消未执行存量并保留历史。当前仍保留实际使用中的 import 和 Dataset Export 任务。

证据：[旧创建接口](/Users/qingmuhy/Documents/devlop/quicrobot/quic_studio/backend/data/routers/workspace.py:245)、[前端调用](/Users/qingmuhy/Documents/devlop/quicrobot/quic_studio/frontend/js/api.js:284)、[旧任务注册](/Users/qingmuhy/Documents/devlop/quicrobot/quic_studio/backend/data/tasks/batch_workers.py:589)。

不能只因代码中仍出现 `official/local` 字符串就断言生产在用旧桶或磁盘降级：配置已经拒绝退役环境变量，新 provider 也有拒绝分支。本项依据是仍暴露的接口和仍注册的任务，不是关键词数量。

处理结果：旧入口和旧物理发布链路已下线；历史模型、对象引用、审计和仍在使用的 import/Catalog Export 能力保留。旧任务迁移只取消 queued/retry_pending，running 任务要求先排空，避免未知 handler 无限重试。

### G10 · P2：批量提交的幂等键没有校验请求内容

规格：外部接入 §7.2、§10 约定重复且内容一致的请求返回首次结果，并对冲突保留历史。

现状：已修复。批量提交按 workspace/user/request key 保存规范化请求摘要，使用数据库幂等占位和事务级协调；同键同内容返回首次结果，同键不同 items/payload/source/review_required 返回 409，失败事务可重试且不留下半成品。

证据：[缓存直接返回](/Users/qingmuhy/Documents/devlop/quicrobot/quic_studio/backend/data/services/annotation_work_items.py:465)。请求摘要、事务级锁和首次响应记录已由定向用例覆盖。

证据：请求摘要、事务级锁和首次响应记录位于 `annotation_work_items.py` 的批量提交服务；`test_annotation_batch_submit_api.py` 已覆盖同键、内容冲突、并发、回滚和发布副作用。

## 文档与验收记录需要同步

| 文档 | 与当前代码的差异 |
| --- | --- |
| README 后端边界 | 仍称 Catalog Export 为占位、Train 为预留包；当前已有真实 Export 和 Train 服务实现 |
| DEVELOPMENT_HANDOFF | 仍称新 demo 数据接口未补齐、Train 只有包与 README、三桶调用者未切换；混合了旧基线与后补内容 |
| 09-18 两份规格 | 开头仍为“待审阅/尚未实现”，后文或计划又有完成记录，应统一为按条款跟踪的当前状态 |
| 本次修订的 spec 与历史计划说明 | 已同步当前入口限制、保留多关联模型、单次最终入库审核及新包补采；历史计划中的完成勾选只代表旧验收，后续执行以本次修复计划为准 |
| 09-21/09-22 验收备注 | 声称对应条款已核对，但部分测试替换业务函数、跳过下载或修改 URL 校验，且存在本报告列出的差异 |

测试文件存在不代表该目标全部验收。例如完整 Data→Train MinIO 用例确实已存在，但缺少 `TEST_TRAIN_STORAGE_*` 环境时会 skip；本轮没有运行，不能称其通过或失败。

## 本轮验证与限制

- 前端全量：`node --test frontend/tests/*.test.mjs`，本轮代码修复后重新执行并记录最终结果。
- 后端定向：隔离 PostgreSQL/Redis 下 34 项新增及受影响用例全部通过，覆盖补采、退役入口、建批、取数和真实批量标注提交。
- 后端全量已执行；仍有一批历史 task-set/旧 LeRobot 测试按退役契约失败，真实 MinIO/阿里云下载和 duance 客户端回归未执行，不能将这些场景标记为已验收。
- 本次为主要契约和链路核对，不是对全部生产部署、真实对象存储下载和外部客户端兼容性的穷尽证明。

执行顺序与验收条件见 [修复计划](CODE_SPEC_REPAIR_PLAN_2026-09-22.md)。本轮已完成代码侧修复和定向验证；G04 暂不安排，G07 保留直接入 export。真实 OSS 下载、duance 联调及历史退役测试迁移另列后续工作。
