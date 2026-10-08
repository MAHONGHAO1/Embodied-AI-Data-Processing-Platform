# UAT 浏览器与静态传输验收（2026-09-23）

环境：`https://studio-uat.quicrobot.xyz`，独立 source 服务，浏览器为 Codex In-app Browser。Chrome 授权后当前工具库存仍只提供该浏览器，本报告没有声称在 Chrome 扩展里完成测试。浏览器对象一直使用同一 UAT 标签页。

## 已部署版本与实际业务结果

- `8ec5569`：包标注、固定审核提交、精确区间导出与独立 Catalog 页面；迁移到 `0006_dashboard_package_facts`，8 个 UAT 服务正常。
- `11f4c46`：真实 OSS ETag 固定身份兼容修复；只重启 UAT API/export worker。未配置版本号的对象仍核对 ETag、大小、SHA256；有版本号的对象仍必须匹配该版本。
- `70e8e07`：标注交互、设置失败提示、退休 Catalog 代码清理、训练与二维码按需加载。
- admin=5 在 UI 完成 package=4 入库通过、batch=2 建批。annotator=7 在真实视频上标记两段并描述，提交后只读；auditor=8 退回，annotator 修改第二段描述后重提；auditor 通过 submission=2，生成 asset=2。
- 资产明确引用 admission attempt=1、submission=2、annotation revision=2；有效范围合计 `19399389184 ns`。原始视频 419.46 秒，资产页显示有效时长 0:19；没有把原始全长当成有效标注时长。
- admin 在 UI 创建 dataset=2 / version=2，并发起 QRDF 与 LeRobot 导出。首次实际失败原因是对象身份要求与 provider 契约不一致；UI 显示失败和重试，修复后的 attempt=2 成功且保留失败记录。
- UI 默认 TTL=3600、内网地址；选择公网重新交付后域名从 `oss-cn-beijing-internal.aliyuncs.com` 变为 `oss-cn-beijing.aliyuncs.com`。签名 URL 不写入本报告。新版本已增加选择变化立即清除旧交付链接，并丢弃迟到响应。
- 下载产物、帧范围和真实内网下载的独立证据见 [导出验收](FRONTEND_UAT_EXPORT_EVIDENCE_2026-09-23.md)。补采、角色和无有效片段分支见 [HTTP 验收](FRONTEND_UAT_ROLE_HTTP_2026-09-23.md)。
- 另用已授权外部 Key 验证真实 fetch manifest：`data_batch=3` 默认内网仅返回冻结接受的 Episode=7（raw/process 共 2 个对象，不含被拒绝的 Episode=6）；`data_packages=4` 显式公网返回 Episode=3 的 2 个对象及完整身份。按返回的 If-Match 下载原始 MCAP 的 1,024 字节范围得到 206 和正确 magic。错配 workspace=4/package=4 返回 404，已移除的 include 参数返回 422。

## 按需加载与重复切页

在 `70e8e07` 刷新 `#/datasets` 后，DOM 中的 script 列表确认：未加载 demo-data、mining-console、ECharts、train-console、二维码生成器和未访问的工作台页面模块。Catalog 只加载自身页面脚本和样式。共享旧工作台纯 helper 仍随外壳加载，不能把它们描述为已全部拆出首屏。

随后进行 30 次“包级结果工作台 → 资产页”快速切换：

| 指标 | 观察值 |
| --- | --- |
| 返回资产页后的 video 元素 | 每次 0 |
| 已加载 script 数量 | 每次 26 |
| stylesheet link 数量 | 每次 4 |
| DOM 元素数量 | 206 或 233（列表加载中／加载完成），没有随切换次数增长 |
| 控制台新增 error / warn | 0 |

这是快速切页和尚在加载的媒体卸载检查，不能把它当作 30 次完整播放测试。之后留在工作台再次确认视频 `readyState=4`、duration=419.46238、media error 为空。未取得 JS 堆、GC 或事件订阅计数，不能据此宣称不存在内存泄漏。

在采集资源页首次点击“QR codes”后，二维码引擎才出现在 script 列表，两个控制码正常呈现；训练与图表引擎仍未提前载入。

## 体积与 HTTP 实测

统计 `70e8e07` 登录入口直接引用的 JS/CSS，加上 bootstrap 实际加载的 app；不计 HTML、API、媒体和未访问页面模块。每个静态文件向真实 UAT 发起 `Accept-Encoding: gzip` 的 HTTP 读取，26 项均返回 gzip。

| 指标 | 值 |
| --- | --- |
| 首屏静态原始字节 | 2,574,096 B |
| 同口径 gzip level 6 估算 | 627,278 B |
| UAT gzip 响应体实际合计 | 627,935 B |
| app.js | 828,199 B / 12,683 行 |

历史审计同口径 gzip 估算为 1,022,095 B，本轮估算减少约 38.6%。这是静态依赖体积改善，不是“页面快了 38.6%”。HTTP 读取不等于浏览器冷／暖缓存传输记录，也不提供脚本解析、首屏可操作或长任务时间。

详细临时测量位于 `/tmp/quicstudio-frontend-static-measurement-70e8e07.json`；其文件路径只作为本次工作记录，不是持久产品依赖。

## 798b23f 部署后复核

`798b23f` 已部署，数据库迁移到 `0007_dashboard_package_lifecycle`，API 与 analytics worker 重启后 `/health` 为 200/ready=true。

- 审核终态 `approved/returned` 已正确识别；真实 item=2 显示 Approved、固定的两条语言描述均只读，没有再次通过／退回入口。播放器正常，控制台无新增 error/warn。
- 采集概览 workspace3 显示目标 `0:08:24`、采集 `0:07:39.555`、入库有效 `0:07:09.555`、3 个包。切换 workspace4 后全部归零。存储精度与标注时长口径见 [看板验收](FRONTEND_UAT_DASHBOARD_EVIDENCE_2026-09-23.md)。
- resources/settings 的范围选择可见。资源从 workspace4 的 0 个数采员切到 workspace3 的 1 个真实数采员；设置从 workspace3 的 iPhone 项目切到 workspace4 后显示 No collection projects。资源目录依照现有 `require_workspace_actor` 允许 admin 查看全部空间；采集任务与项目按 collection_access 筛选，没有通过改权限掩盖差异。
- 旧 QRDF 数据集 revision builder 的状态、模板、入口脚本与专用样式已删除。app 为 11,842 行／778,313 B（包括 UTC 及物理时长文案），六个页面按需装载；活动历史工作台与采集页仍在根组件。
- 新首屏 25 项 JS/CSS：原始 2,497,498 B；gzip level 6 估算 613,651 B；UAT 实际 gzip 响应 614,359 B。逐文件解压内容与本地提交内容完全相同。相对审计估算基线减少约 40.0%，不转换成页面耗时百分比。详细临时记录 `/tmp/quicstudio-frontend-static-measurement-798b23f.json`。
- 完整前端 340 项回归通过；后端仓库与包生命周期 30 项通过。训练控制面 UAT runtime 的隔离修复仍在进行，尚未启动训练任务。

## 后续工作台大包回归（本地）

包内导航每页最多渲染 40 条 Episode，描述编辑区每页最多 20 条片段；时间轴仍展示当前 Episode 的全部区间。选择时间轴片段会自动切到对应编辑页，新建片段也会定位到所在页。进度计算由逐项二次查找改为单次遍历。

组件回归使用仅测试内存中的 1,001 条 Episode／101 条片段，确认屏外的未完成结论仍阻止提交，跳转最后一页不丢数据，保存仍提交全部 1,001 条结论与完整 101 个片段。另验证已选片段可直接修改出点，不要求先开启新片段。此为隔离组件行为测试，不向 UAT 或生产注入这些压力记录，也不声称已取得大包浏览器耗时／堆内存数字。

另用仅监听本机 `127.0.0.1:50690` 的临时页面加载相同 Vue 3.5.13、workbench 与样式，生成 1,001 条 Episode、当前条目 1,000 个片段。该页面使用测试内存 API、两点媒体映射且不播放视频，未接入 UAT，也未进入任何生产入口。原生浏览器 UI 确认渲染 40 个目录按钮、20 个描述编辑行、1,000 个时间轴条；DOM 共 1,460 个元素，翻到第二页仍为此数量，包进度仍统计全部成员。

本机同一 IAB、正常视口、无 CPU/网络限速下，页面自带 input 事件到第二次 requestAnimationFrame 的十次记录为 `21,19,20,18,18,18,17,17,17,17 ms`，中位数 18 ms、最大 21 ms；页面 PerformanceObserver 在该次测试未记录长任务。这个指标是隔离编辑渲染观察，不能替代真实 API 自动保存耗时、视频播放压力、首屏耗时或堆快照。无改造前的同条件对照，不报告提速比例。临时页面和服务器均已关闭，记录保存在 `/tmp/quicstudio-workbench-perf/results.json`。

测试暴露短于一秒的区间显示被截成同一秒，后续补充毫秒显示，仍在 title 保留完整纳秒端点；浏览器已看到 `0:00.001 → 0:00.002` 等准确可分辨范围，新增对应组件回归。

## 最终版本 cc74255

- 冻结写操作的工作空间、弹窗关闭竞态和看板未知状态修复已部署；前端全套 359 项通过，最终预检展示调整再跑相关 13 项通过。训练页面脚本和独立样式仍按需装载，不加入登录首屏。
- 同口径首屏仍为 25 项 JS/CSS；原始 2,521,648 B，gzip level 6 估算 616,992 B，实际 UAT gzip 响应合计 617,653 B。逐文件解压与本地提交完全一致；与审计 gzip 估算基线相比减少约 39.6%。请求范围及不能推算加载时间的限制与前文相同。
- 根 app 为 12,301 行／802,428 B；相比 798b23f 增加了范围与请求隔离代码。六个页面已拆为按需模块，剩余活动历史工作台和采集页仍有进一步拆分空间，不将根组件描述为已彻底精简。
- 952×912 常规视口下，数据集长 OSS URI 已在右侧卡片内换行；“重新导出”实际产生新 attempt，pending 与成功状态正确变化。训练登记的元数据可折叠，未知字段不填默认事实；中英文随全局语言切换。真实登记与训练前阻断见 [登记验收](FRONTEND_UAT_TRAIN_REGISTRATION_2026-09-23.md)。
- 验收结束后原测试会话失效，浏览器刷新显示登录页。临时大列表页面和 HTTP 服务均已关闭，没有向正式 UI 注入压力测试数据。
