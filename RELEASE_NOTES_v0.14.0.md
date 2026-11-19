# v0.14.0 — 契约化：适配器、无头计划与数据卡

**这一版不加新格式。它把“支持一种输入”从口头承诺变成可测试、可拒绝、可插拔的契约，并补上工业场景真正需要的无头入口与交接物。**

v0.13.0 解决了 Matching Cable 的第二次视频编码；v0.14.0 解决的是“换一种数据、换一台机器、换一个人来跑，结论还能不能复现”。

## 改了什么

### 1. Adapter Contract v1

- 新增 `robodata/adapters/`：描述符、探测、预检、托管复制、预览、清单六个契约点。
- 现有 `hdf5`、`so100`、`aloha`、`matching_cable` 四个适配器迁移到契约；导入编排不再按 `kind` 写分支。
- 硬约束：`adapter_id == source_kind`；未知版本明确拒绝，不静默降级。
- 新增 `robodata adapters list|detect|inspect` 与 `robodata doctor`。
- `import-batch --source auto` 按目录结构选适配器；识别失败返回专用退出码与逐项原因。
- `docs/ADAPTER_CONTRACT.md` + `adapter.schema.json` 定义支持边界与新增格式步骤。

### 2. 无头单命令 `process --plan`

- 一份 JSON/TOML 计划覆盖：输入、标注、审核策略、是否打包、数据卡 ID/版本。
- 一条命令跑完导入 → 质检 → 清洗确认 → 标注 → 审核 → 转换 → 验证 → 打包 → 登记。
- 退出码区分：`0` 通过、`2` 业务拒绝、`3` 无适配器、`4` 计划无效、`5` 运行失败。
- 阶段级幂等：复用已导入批次、已批准标注、当前转换产物和交付 ZIP；节点级断点续跑仍未做。
- `docs/PROCESS_PLAN.md` 明确标注“不是调度器、不是任意中断续跑”。

### 3. 数据卡与本地登记表

- 交付 ZIP 内新增 `dataset/dataset_card.json`：来源、内容、质量、转换策略、证据哈希、限制。
- `card_digest` 可重算，排除 `created_at`；同内容重打包摘要一致，改字段即失效。
- 新增 `registry.sqlite3` 与 `robodata registry list|show|verify|register`。
- 同一 `dataset_id + version` 内容不同会冲突，不静默覆盖。
- Train 侧只需读数据卡和登记记录，不依赖 Process 的内部路径。

### 4. 视频统计修复

- LeRobot `RunningQuantileStats` 对 `uint8` 帧做平方会溢出，导致视频 `std=0`。
- v0.14 在所有转换入口把整数批次转成 `float64` 后再走原算法；不 fork LeRobot。
- `tools/conversion/check_video_stats.py` 可复现：修复前 `std=0`，修复后非零；合成夹具同步断言。
- 旧版本已生成的包不追溯改写统计。

### 5. 文档与兼容矩阵

- 新增 `docs/COMPATIBILITY.md`：输入、输出、运行形态、交付/登记的明确支持矩阵。
- 明确不支持：任意未固定 LeRobot v3.0 导入、LeRobot v2.x 导入、ROS2 MCAP、多用户/云部署。
- `check` 能读不等于 `import` 支持；矩阵把两者分开写。

## 500 帧端到端实测

同一台 Windows 机器，Matching Cable Episode 68 / 500 帧 / 4 路相机：

| 步骤 | 结果 |
| --- | --- |
| `process --plan` 全链路（导入、质检、标注、转换复用、打包、交付、登记） | **209.67 s** |
| 同计划幂等重跑 | **0.54 s**，没有新增运行目录 |
| 转换策略 | `preview_video_reuse`，`video_files_reused=4`，`fallback_reason=null` |
| 数据卡 | `telecom-matching-cable-500` v1，`card_digest=fcd35d753afceafba0822b4961828cb6891090c7350798864baecb86712cb877` |
| 交付 ZIP | 26 个文件，包含 `dataset/dataset_card.json` |
| 登记表 | 1 条数据集 / 1 个版本 |
| 视频统计 | 修复后 `std≈0.202`，不再为 0 |

## 全量复验（转换链路）

7 Episode / 11,879 帧的 v0.14 路径实测：

| 步骤 | 结果 |
| --- | --- |
| 全量预览 | **1,354.58 s（22 分 34.58 秒）**；28 个独立 MP4 |
| `format_write` 复用预览视频 | **184.75 s（3 分 4.75 秒）**；28 个视频复用 |
| `official_verify` | **28.10 s**；11,879 行；84 个采样位置 |
| 视频统计 | 修复后 `std≈0.207`，不再为 0 |

v0.14 **没有跑完整 7 集的 `process`**（打包、交付、登记）；这三项只在 500 帧端到端验证。全量交付证据仍来自 v0.13.0，见 [docs/EVIDENCE.md](docs/EVIDENCE.md) 5.2。不要把这组转换耗时外推成完整业务链耗时。

## 验证

- Python 全量：**353 passed / 14 skipped / 13 subtests passed**
- `robodata verify-fixtures`：**6/6**
- `scripts/verify_matching_optional.py`：3 条转换路径 × 6 个阶段，预览复用 / 子集重编号 / 篡改回退 / 官方加载 / archive verify 全部通过；预览与复用输出 `std>0`
- 前端单测：**26 passed**
- 前端生产构建：通过
- Chromium 浏览器流程：**2 passed**
- CI：见 `docs/EVIDENCE.md`

## 不声明

- 不声明新增 ROS2 MCAP 或 LeRobot v2.1 导入；v0.14 不扩大格式矩阵。
- 不声明节点级断点续跑、任务队列或多用户并发。
- 不声明策略训练效果、任务成功率或生产容量。
- 不声明相机硬件同步；文件级 20 FPS 映射不等于硬件同步。
- 不声明外部采用；外部复现记录仍为 **0**。
- 旧 tag 不移动；`v0.12.1`、`v0.13.0` 保持原指向。

完整证据、命令、计数和未验证边界见 [docs/EVIDENCE.md](docs/EVIDENCE.md)。
