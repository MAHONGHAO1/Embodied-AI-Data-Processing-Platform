# v0.15.0 — 有选择的广度：LeRobot v2.1、MCAP 检查、Train 交接

**这一版把“通用 LeRobot v2.1 能进来”和“Train 能只读数据卡接走”做成真实链路；MCAP 只做检查，不假装能转换。**

## 改了什么

### 1. LeRobot v2.1 通用输入

- 新增 `lerobot_v21` 适配器：接受任意结构合法的 v2.1 目录，要求 `meta/episodes_stats.jsonl`。
- 转换调用 LeRobot 官方 `convert_dataset_v21_to_v30`，不自己重写数据格式；数值与视频来源保留。
- `official_verify` 重新读取源 parquet，全量行对照 `observation.state` / `action`，再用官方加载器抽样。
- 当前只支持**全量 Episode**；子集选择会以 `subset_unsupported` 明确失败，不静默裁剪。
- 通用 v2.1 元数据读取支持 `meta/episodes.jsonl`、`meta/tasks.jsonl` 以及 parquet 变体；托管副本带 `robodata_source.json` 标记。

### 2. ROS 2 MCAP 检查

- 新增 `mcap` 适配器，标记 `inspection_only: true`。
- 可读 topic、消息类型、消息计数；可选 `robodata_mcap.json` 映射只校验 topic 是否存在。
- 不推断 state/action/相机语义，不做时间对齐，不导入、不转换。
- `--source auto` 会跳过 inspection-only 适配器；显式导入会明确拒绝。
- 可选依赖 `mcap==1.6.0`、`mcap-ros2-support==0.6.0`，未安装时给出安装提示。

### 3. Registry API 与 Train 交接

- 新增只读 API：`GET /api/registry`、`/api/registry/{id}`、`/api/registry/{id}/{version}`。
- 新增 `scripts/verify_train_handoff.py`：安全解包 ZIP、重算 `card_digest`、检查最终质检/官方验证结论、用官方加载器核对帧数与 Episode 数。
- 交接文档 `docs/TRAIN_HANDOFF.md` 明确：数据卡是契约，`source_manifest.json` 不是 Train 的稳定 API。

### 4. 兼容矩阵与 UI

- `docs/COMPATIBILITY.md` 更新：v2.1 导入/转换支持；v2.0 不支持；MCAP 仅检查。
- 工作台来源下拉新增 `LeRobot v2.1 · 通用目录`；主布局未变。

## 实测

### LeRobot v2.1 全链路（2×5 合成夹具）

| 项 | 结果 |
| --- | --- |
| `process --plan` 全链路（导入、质检、标注、官方 v2.1→v3.0、官方加载、打包、交付、登记） | **97.00 s** |
| 同计划幂等重跑 | **0.67 s**，无新增运行目录 |
| 转换策略 | `lerobot_v21_official_converter` |
| 数据卡 | `lerobot-v21-demo` v1，`card_digest=2905ac1f25ff64d35284292e72a2e1338724752e3a582adaf518bcb4fa15f02c` |
| 官方加载 | 2 Episode / 10 帧；源 parquet 全量行对照通过 |
| Train 交接脚本 | `status=passed`，`card_valid=true`，加载 10 行 / 2 Episode |
| Registry API | list / detail / version 三种读取通过 |

### MCAP 检查（合成 fixture）

- `std_msgs/msg/Float64` topic 能被识别，schema 与消息计数可见；
- 映射引用不存在 topic 时返回 `profile_missing_topics`；
- 没有真实 MCAP 样本，也没有转换结果。

## 验证

- Python 全量：**359 passed / 16 skipped / 13 subtests passed**
- `robodata verify-fixtures`：**6/6**
- 前端单测 / 生产构建 / Chromium 流程：见 `docs/EVIDENCE.md`
- CI：见 `docs/EVIDENCE.md`

## 不声明

- 不声明 MCAP 可以导入或转换；它是 inspection-only。
- 不声明 LeRobot v2.1 子集转换；只支持全量 Episode。
- 不声明 LeRobot v2.0 导入；先升级到 v2.1。
- 不声明真实 MCAP 样本已接入；仓库只有合成 fixture。
- 不声明外部试点或采用；外部复现记录仍为 **0**。
- 旧 tag 不移动；`v0.12.1`、`v0.13.0`、`v0.14.0` 保持原指向。

完整证据、命令、计数和未验证边界见 [docs/EVIDENCE.md](../EVIDENCE.md)。
