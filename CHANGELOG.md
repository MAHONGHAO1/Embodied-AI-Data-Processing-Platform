# 变更记录

## v0.16.0（2026-11-19 发布）· 可运维：单机队列、预览级续跑、目录治理

**不扩格式，把“能跑通一次”变成“中断可恢复、任务可排队、目录可治理”。**

- **持久队列**：新增 SQLite 任务队列与独立 worker，支持租约、接管、取消、重试、幂等提交；`jobs` / `worker` CLI 与 `/api/jobs`。
- **预览续跑**：Matching Cable 预览按 Episode 编码并写 journal，重启只复用输入摘要与产物哈希都一致的单元，再用官方 `aggregate_datasets` 合并；`source_manifest` 记录复用/编码集数。
- **目录治理**：新增 `layout.py`、`status`、`gc --dry-run|--apply`、磁盘预检；GC 只删失败/中断且未被批次或登记表引用的运行目录。
- **Linux/Docker**：Python 测试改为 Ubuntu + Windows 矩阵；acceptance 新增 `linux-conversion`；Docker 增加 web + 一次性 worker，容器内验证队列可打开，但不含隔离转换环境。
- **实测**：500 帧电信 Matching Cable 经队列完整业务链 218.27 s；重复提交返回已有任务；预览续跑 2×2 夹具复用 1 集、只编码 1 集；取消任务进入 `canceled`。
- **测试**：Python 374 passed / 16 skipped；前端与 CI 见 `docs/EVIDENCE.md`。
- 版本链统一到 0.16.0；发布说明：[RELEASE_NOTES_v0.16.0.md](RELEASE_NOTES_v0.16.0.md)

## v0.15.0（2026-11-19 发布）· 有选择的广度：LeRobot v2.1、MCAP 检查、Train 交接

**把通用 v2.1 和 Train 交接做成真实链路；MCAP 只做检查，不假装能转换。**

- **LeRobot v2.1**：新增通用适配器，要求 `meta/episodes_stats.jsonl`；转换调用官方 `convert_dataset_v21_to_v30`；`official_verify` 全量对照源 parquet；当前只支持全量 Episode，子集明确失败。
- **MCAP 检查**：新增 `mcap` inspection-only 适配器，读取 topic/type/计数并校验可选 `robodata_mcap.json`；不推断语义、不导入、不转换。
- **Registry API**：新增只读 `/api/registry`、`/api/registry/{id}`、`/api/registry/{id}/{version}`。
- **Train 交接**：新增 `scripts/verify_train_handoff.py`，安全解包、验卡、验结论、官方加载；新增 `docs/TRAIN_HANDOFF.md`。
- **UI**：来源下拉新增 LeRobot v2.1；主布局未变。
- **实测**：v2.1 2×5 合成夹具 `process` 97.00 s，幂等重跑 0.67 s；数据卡 `lerobot-v21-demo` v1；Train 交接 `status=passed`；MCAP 合成 fixture 检查通过，无真实样本。
- **测试**：Python 359 passed / 16 skipped；前端与 CI 见 `docs/EVIDENCE.md`。
- 版本链统一到 0.15.0；发布说明：[RELEASE_NOTES_v0.15.0.md](RELEASE_NOTES_v0.15.0.md)

## v0.14.0（2026-11-19 发布）· 契约化：适配器、无头计划与数据卡

**不加新格式，先把“支持”做成可测试、可拒绝、可插拔的契约。**

- **Adapter Contract v1**：新增 `robodata/adapters/`，四个内置适配器迁移到探测/预检/托管复制/预览/清单契约；导入编排不再按 `kind` 分支；`adapter_id == source_kind` 有硬校验。
- **CLI**：新增 `robodata adapters list|detect|inspect`、`robodata doctor`；`import-batch --source auto` 自动探测，失败返回专用退出码与逐项原因。
- **无头计划**：`robodata process --plan` 一条命令跑完导入、质检、标注、审核、转换、验证、打包与登记；退出码区分通过/业务拒绝/无适配器/计划无效/运行失败。
- **阶段级幂等**：复用已导入批次、已批准标注、当前产物与交付 ZIP；500 帧同计划重跑 0.54 s；节点级断点续跑未做。
- **数据卡**：交付 ZIP 内新增 `dataset/dataset_card.json`，`card_digest` 可重算；新增 `registry.sqlite3` 与 `robodata registry` 命令；同 ID/版本不同内容冲突，不覆盖。
- **视频统计**：转换入口把整数批次的平方/方差计算转 float64，直方图保持整数再计算，修复 `uint8` 平方溢出导致的 `std=0`；旧包不追溯改写。
- **500 帧端到端**：`process` 209.67 s；数据卡 `telecom-matching-cable-500` v1；ZIP 26 个文件含数据卡；登记表 1 条；修复后视频 `std≈0.202`。
- **全量转换链路**：7 Episode / 11,879 帧预览 1,354.58 s，复用 `format_write` 184.75 s，`official_verify` 28.10 s；11,879 行、28 个视频，视频 `std≈0.207`。完整 7 集 `process`（打包/交付/登记）未跑。
- **测试**：Python 353 passed / 14 skipped；前端 26 passed；`verify-fixtures` 6/6；浏览器流程 2 passed。
- 版本链统一到 0.14.0；发布说明：[RELEASE_NOTES_v0.14.0.md](RELEASE_NOTES_v0.14.0.md)

## v0.13.0（2026-11-19 发布）· Matching Cable 编码一次

**去掉第二次视频编码，保留逐帧源数值回核和全有或全无回退。**

- **预览布局**：每个 Episode 一个数值 parquet、每路相机一个 MP4；预览显式 `finalize()` 后才生成 `reuse` 清单和哈希。
- **转换复用**：`format_write` 校验复用清单并复制预览视频，重写数值 parquet、任务索引、`meta/episodes`、统计和 `info.json`；不再第二次编码。
- **源数值回核**：逐帧重读已选源 HDF5，与预览 parquet 对照；`format_write` 记录 `source_numeric_consistency_rechecked=true`，`official_verify` 仍为 `false`（范围不同）。
- **全有或全无回退**：预览不兼容、视频哈希/帧数/分辨率/统计异常或数量不完整时，整批回退重编码并记录 `fallback_reason`，不混合来源。
- **子集重编号**：支持从预览中选子集并重写 `index`、`episode_index`、`task_index` 和统计。
- **500 帧 A/B**：预览 60.79 s；复用 `format_write` 18.28 s；强制重编码 58.71 s；数值逐列相等，4 路视频 SHA-256 逐字节相等。
- **全量 7 集 / 11,879 帧**：预览 1,338.44 s；复用 `format_write` 206.46 s；干净重编码 1,072.68 s；复用约为重编码的 19.2%。复用输出 `official_verify` 28.51 s、`output_quality` 73.09 s（77/77 覆盖、0 issue）、`archive_verify` 27.89 s；11,879 行数值逐列相等，28/28 个视频 SHA-256 相同。
- **测试**：`scripts/verify_matching_optional.py` 增加复用、子集重编号、篡改视频回退和 archive verify；Python 全量 328 passed / 14 skipped。
- 版本链统一到 0.13.0；发布说明：[RELEASE_NOTES_v0.13.0.md](RELEASE_NOTES_v0.13.0.md)

## v0.12.1（2026-11-19 发布）· 大样本编码提速与超时校准

**不改变字段语义，只让 10 GB 量级的 Matching Cable 数据跑得完。**

- **默认编码线程**：四路相机每路编码器线程数改为 `max(1, min(4, CPU//4))`，给解码和 I/O 留余量；本机 500 帧预览 75.73 s → 64.74 s（约 15%）。
- **可选硬件编码**：新增 `ROBODATA_MATCHING_VCODEC`，本机 `h264_nvenc` 500 帧 63.59 s，仅比 4 线程 x264 快约 2%；没有 NVENC 的机器保持 `h264`。
- **超时校准**：`format_write` 30→60 分钟，`official_verify` 20→30 分钟，`output_quality` 20→40 分钟，打包/验包 30→60 分钟，`file_validation` 10→20 分钟，`matching_validation`/`field_mapping` 20→30 分钟。
- **真实数据基线**：电信 7 Episode / 11,879 帧 / 10.04 GB 结构兼容；2 Episode × 2 帧完整业务链与 500 帧格式写入/官方加载通过。
- 文档更新：`docs/MATCHING_CABLE.md` 增加速度、环境变量与全量运行建议。
- 发布说明：[RELEASE_NOTES_v0.12.1.md](RELEASE_NOTES_v0.12.1.md)

## v0.12.0（2026-11-19 发布）· Matching Cable 缺失流可见

**不要求数据完整才配进流程。** 在 PR #2 的 Matching Cable 适配器上，把缺失相机、深度和可选字段改成逐条可见的来源事实，并补合成回归。

- **可选流**：`camera_0..3` 可缺，至少 1 路 RGB；深度 `camera_1..2`、`puppet/eef`、`master/eef`、`puppet/joint_effort`、`puppet/6f` 均可缺。缺失写入 `missing_cameras`、`missing_depth_source_cameras`、`optional_fields`，不补造、不阻断。
- **触觉**：适配器不读取触觉字段，固定记录 `tactile.status=not_provided`；存在也不进入交付。
- **时间与完整性**：50 ± 10 ms 硬校验；源帧数必须与预览完全相等；清单含 `sha256` 时重新计算哈希；候选集拒绝重复索引。
- **验证语义**：官方验证字段改为 `output_loader_consistency_checked`，同时明确 `source_numeric_consistency_rechecked=false`；业务门禁同步，不再把输出加载检查冒充源数值重核。
- **合成回归**：新增 `scripts/verify_matching_optional.py`，2 Episode × 2 帧、只有一路 RGB、无深度/触觉/可选字段，预览 + 六阶段通过；`acceptance.yml` 的 `full-windows` 增加该步骤。
- **界面**：来源锁定面板显示已写入相机、缺失相机、缺失深度和触觉状态。
- **文档**：新增 `docs/MATCHING_CABLE.md`，更新 README 数据配置、数据字典与证据总表；新增 `docs/REPRODUCTION.md` 五分钟复现页与外部复现 issue 模板。
- 版本链统一到 0.12.0，并补齐 `RELEASE_NOTES_v0.12.0.md`。
- 发布说明：[RELEASE_NOTES_v0.12.0.md](RELEASE_NOTES_v0.12.0.md)

## v0.11.0（2026-11-18 发布）· 可复现验收

**不加格式、不加页面。** 把固定来源的真实样本验收做成一条可重跑命令，并接入官方 DataLoader 与 Docker/Linux CI。

- **真实样本**：`robodata fetch-sample` 支持 `--episodes` 子集、`--verify-only`、`--verify-upstream`、`--endpoint`；逐文件核对上游清单哈希，写出 `source_manifest.json`。本机固定 revision 的 Episode 0–1 共 9 个文件 / 19,215,391 字节。
- **一条命令**：`scripts/verify_pipeline.py` 串联来源、导入、质检、审核、转换、输出抽帧、DataLoader、交付和独立 ZIP 验证；`--mode source|fast|full` 按需选择。
- **终态一致性**：转换失败时先把未执行节点收口为 `skipped`，再写入 `failed` 终态；轮询方不会读到「已失败但后续节点仍 pending」的中间状态，新增回归测试。
- **官方加载器**：`scripts/probe_dataloader.py` 在 `lerobot 0.4.4` + `torch 2.7.1+cpu` 下检查动作/状态 `float32 [B,6]`、图像 `float32 [B,3,480,640]`、task 文本与不打乱顺序。
- **实测**：`--mode full` 跑完 9 个阶段约 106.1 秒；2 Episode / 1,300 帧；DataLoader 读取 4 个 batch / 32 帧；ZIP 独立解包通过。
- **Docker/Linux**：新增 `Dockerfile`、`.dockerignore` 与 `acceptance.yml` 的 `source-linux`、`full-windows`、`docker-linux` 三个 job；本机无 Docker，容器结论仅来自 CI。
- **文档**：新增 `docs/ACCEPTANCE.md`、`docs/DOCKER.md`、`docs/SCORECARD.md` 与流水线性能基线。
- 版本链统一到 0.11.0，并补齐 `RELEASE_NOTES_v0.11.0.md`。
- 发布说明：[RELEASE_NOTES_v0.11.0.md](RELEASE_NOTES_v0.11.0.md)

## v0.10.0（2026-11-18 发布）· Evidence-grade

**只做可信度收口，不加导入格式、页面或适配器。**

- **版本链**：`pyproject.toml` 0.6.0 → 0.10.0，`config.py` 0.8.0 → 0.10.0，与 README、release 徽章、`frontend/package.json` 对齐；新增 `tests/test_version_consistency.py`，版本漂移或缺少发布说明即失败。
- **运行列表**：抽出 `_state_from_directory()`，`list_runs` 不再对每条记录重复 `resolve()` 运行根目录；300 条运行记录的 `/api/runs` 恢复到 5 秒阈值内，只读与中断派生语义不变。
- **规模证据**：`scripts/benchmark_quality.py` 支持按规模指定重复次数；新增 10 万行 ×3 与 100 万行 ×1 实测，原始 JSON 与边界见 `docs/benchmarks/2026-11-18-scale-experiment.md`。100 万行为 61.245 秒、16,328 行/秒，仅表格检查。
- **训练输入冒烟**：新增 `scripts/train_smoke.py` 与 `docs/TRAINING_SMOKE.md`；合成数据 100 步线性动作探针 MSE 0.71632246 → 0.00012241，明确不宣称策略训练效果。
- **Linux**：新增 `.github/workflows/linux-smoke.yml`，在 ubuntu-latest 跑核心 Python 检查与 `verify-fixtures`；Docker 与 Linux 桌面未验证。
- **README**：首屏增加「60 秒证据」，测试数量移入 `docs/EVIDENCE.md` 单一出处；v0.9.0 段落保留发布时 289 passed / 7 skipped 的历史记录。
- 发布说明：[RELEASE_NOTES_v0.10.0.md](RELEASE_NOTES_v0.10.0.md)

## v0.9.0（2026-11-14 发布）· 上线级审计与状态回退修复

针对「提交审核后离开一小时，回来发现页面全部回退到导入阶段」的反馈，完成前后端上线级审计、复现、修复与全量回归：

- **P0 根因**：GET / SSE 读路径曾无条件调用 `business.reconcile_run`，重放质检登记会把全部任务审核打回草稿、清掉清洗确认、递增内容版本，并让已验证输出与交付包失效；前端硬编码的阶段进度因此只剩「导入」。SSE 终态未下发 `done`，浏览器 `EventSource` 无限重连，进一步放大了读路径副作用。
- **后端**：读路径改为纯只读（含 `get_run(mark_interrupted=False)` 派生视图）；恢复登记改为显式 `POST /api/runs/{id}/recover`，并对质检登记增加破坏性确认门禁；SSE 终态 `event: done` + 心跳 + 7200s 超时；`reconcile_run` 增加 `force`、状态早返回与重试硬上限；补充上传大小上限、路径脱敏、文件锁与版本校验加固。
- **前端**：`listenRun` 终态 / `done` / 错误事件自动关闭；URL 与 localStorage 会话恢复；`refresh` 竞态防护与写操作串行化；阶段进度改由 `deriveStages(batch, runs)` 推导；质检重跑与恢复质检登记均需显式确认；内容版本变更提示与草稿自动失效。
- **409 可操作兜底**：API 客户端保留 HTTP 状态码；质检重跑 / 恢复质检登记 / 修改筛选决定被后端门禁拒绝时，错误条提供「确认并重试」，先取服务器最新 `revision` 与 `content_version` 再带确认标记重发，消除重复 409 循环。
- **产物失效提示**：保存标注、提交审核、审核、确认筛选在已有当前产物时先说清「产物与交付包将变为非当前版本」，成功后不再静默。
- **归因纠正**：写成功后同步版本基准，`refresh` 的版本变更提示改为中性表述，不再把用户自己的操作报成「后台处理」。
- **测试**：新增 `tests/test_launch_audit.py`（状态持久化、负向边界、并发幂等、性能压力、SSE 生命周期、双入口只读护栏）与 `frontend/src/progress.test.ts`、`session.test.ts`、`api.test.ts` 及扩展的 `App.test.tsx`（合计 26 项）。完整结论见[上线级审计报告](docs/LAUNCH_AUDIT_REPORT.md)。
- 验收：Python 全量 **289 passed, 7 skipped, 13 subtests passed**；审计套件 **43 passed**；前端 26 项单测与生产构建通过；真实 `uvicorn` 终态 SSE 8 ms 内闭环。发布为 **v0.9.0**，运行边界仍为本机单用户 loopback。

## 未发布 · 工程扩展实验（2026-11-12）

- 新增 `scripts/benchmark_quality.py` 与 `docs/benchmarks/`：对现有 `run_checks(check_video=False)` 做 1k / 5k / 10k 行合成表格吞吐测量，保存 JSON 原始结果；明确不包括 IO、视频、转换和打包，不外推生产容量。
- 新增独立 `examples/ros2_mcap/` 单 topic ROS2 MCAP 示例：用可选锁定依赖解析 `std_msgs/msg/Float64`，保留 `log_time` 纳秒语义、来源 SHA-256、重复时间与 gap 检查；不接入 Episode/API、不改主项目依赖。
- 随附的 5 条 MCAP 文件为合成 fixture，不是实机采集数据；专项测试在可选依赖安装后运行。
- 新增 `docs/demo-5min.md` 面试短讲稿与追问证据索引；README 增加规模实验、MCAP 示例和 5 分钟讲稿入口。

## v0.8.0（2026-11-12 发布）

- 两套应用界面重做为 **16:9 固定工作台**：顶栏 48px、侧栏 216px、主区自适应、检视面板 320px；具身底部时间轴 96px、基金底部口径状态条 34px。整页不再滚动，仅主区与检视面板局部滚动。
- 导航从顶栏迁入侧栏；新增右侧检视面板（当前帧、任务、逐路检查、质检与候选、来源锁定），可收起。
- 具身侧把播放／逐帧／拖动／5 个百分比书签集中到全局时间轴，统一驱动真实视频 `currentTime`；状态与动作曲线新增当前帧游标；点击相机放大，支持空格、← →、1–4、Esc 快捷键。
- 基金侧改为全宽表格（32px 行高、固定表头），10 行在 1920×1080 首屏全部可见；点行联动右侧检视，收益不可用时显示带原因的说明块。
- 新增 4 张 1920×1080 界面截图；作品集展示页增加 16:9 新界面区块并更新版本标识。
- **纯前端布局层重构**：API 契约、Python 业务实现、数据口径、验收范围与「仅本机运行」边界均未改变；面板拖拽与布局保存未实现。

## v0.7.0（2026-11-11 发布）

- 展示页新增四路相机同步时间轴：8 个时刻可拖动／播放，帧来自已验收交付包四路视频按相同帧索引解码拼接；采样参数与逐路亮度／方差核验写入 `docs/assets/timeline/manifest.json`。
- 新增真实数据图集（8 帧时间轴总览、5 条 Episode × 4 路相机抽样、质检报告聚焦图）与九个界面视角画廊；历史界面明确标注版本。
- 扩充两仓 README 介绍、30 秒读懂卡片、架构与职责说明、能力边界与面试追问速查表。
- Chromium 桌面 1280px / 移动 390px 双视口实跑通过：无坏图、无横向溢出、无脚本错误，交互全部正常。
- 业务代码与数据验收范围未变；时间轴为静态帧序列，不代表硬件同步或实时播放。

## v0.6.0（2026-11-10 发布）

- 在固定公开 ALOHA Coffee 配置中加入四路相机预览、逐相机视频质检和 LeRobot v3.0 转换／交付流程。
- 固定上游 revision 与文件清单校验；支持范围限于 ALOHA Coffee Episode 0–4，不声明未知来源兼容或硬件相机同步。
- 增加 Windows / Python 3.12 手动集成工作流；固定 ALOHA Episode 0–4 端到端转换、官方加载、质检、独立验包与源哈希复核通过。
- 增加 React + TypeScript 工作台、FastAPI 同源本地服务及 Node.js 24 锁定前端构建；旧 Streamlit 页面保留为回退入口。
- 添加工作台、episode 与相机浏览、运行诊断和交付核验视图；README 展示实际 ALOHA 四路相机静态截图与短演示，并保留独立标注的 SO-100 流程演示。
- 更新 E2E 流程、README、架构说明与启动器的 Node.js 24 检查。
- 自动验收使用脚本明确标注的模拟审核门禁；完整真实数据处理范围和复现证据见 [V0.6 状态](docs/STATUS_v0.6.0.md) 与 [V0.6 验收记录](docs/VALIDATION_v0.6.0.md)。

## v0.5.0

- 在工作台打通 SO-100 的审核后最终质检、LeRobot v2.0 → v3.0 转换、输出质检、打包和独立验包。
- 只有所有阶段验证通过后才登记当前输出；失败运行保留诊断且不可作为交付下载。
- 更新版本状态、验收文档和作品展示边界。
