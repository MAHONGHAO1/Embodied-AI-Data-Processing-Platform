# 兼容矩阵（v0.15）

本文件回答售前和接入时最先被问到的三个问题：

1. 我的输入能不能进？
2. 进去之后能不能转成训练格式？
3. 不支持的时候，平台会不会明确告诉我？

口径：**“支持”= 有固定来源或明确测试标记 + 有契约测试 + 有失败样本。** 没有测试的格式不列入支持。

## 1. 输入

| 输入 | 版本 / 形态 | 导入 `import-batch` | 质检 `check` | 转换 | 说明 |
| --- | --- | --- | --- | --- | --- |
| Robomimic HDF5 | 固定公开版本 | 支持 | 支持 | 支持 | 默认校验 SHA-256；故障副本必须 `--test-label` |
| SO-100 | 固定 LeRobot v3.0 | 支持 | 支持 | 支持 | 固定 revision；完整 Episode 子集可导入 |
| ALOHA Coffee | 固定 LeRobot v3.0 | 支持 | 支持 | 支持 | 固定 repo/revision/四相机/50 FPS |
| Matching Cable | 逐帧 HDF5 目录 | 支持 | 支持 | 支持 | 4 路 RGB 可缺；深度/可选字段按实际记录 |
| LeRobot v3.0（任意来源） | 未固定 | **不支持** | 支持 | 不支持 | 可以质检，但不能作为可追溯导入 |
| LeRobot v2.1 | 任意来源，含 `meta/episodes_stats.jsonl` | 支持 | 支持 | 支持（官方 v2.1→v3.0） | 当前只支持全量 Episode，不支持子集；输入目录会复制进批次 |
| LeRobot v2.0 | 任意来源 | **不支持** | 支持（v2 布局） | 不支持 | 先升级到 v2.1 |
| ROS2 MCAP | 任意 `.mcap` | **不支持** | **仅检查** | **不支持** | 只读取 topic/type/计数并校验可选 `robodata_mcap.json`；不推断语义、不转换 |
| ZIP / 远程 URL / 对象存储 | — | **不支持** | 不支持 | 不支持 | 先解压/下载到本地，再走导入 |

`check` 与 `import-batch` 不是同一能力：`check` 是只读质检器，`import-batch` 需要可追溯来源。不要用“能质检”推断“能导入”。

## 2. 输出

| 输出 | 状态 | 说明 |
| --- | --- | --- |
| LeRobot v3.0 | 支持 | 当前唯一交付格式；官方加载器验证 |
| LeRobot v2.1 | 不支持 | 训练侧存量需求高，v0.15 评估 |
| RLDS | 不支持 | 等 Train 侧真实需求 |
| Zarr / WebDataset | 不支持 | 不列入近期路线 |

## 3. 运行形态

| 形态 | 状态 | 证据 |
| --- | --- | --- |
| Windows 本机单用户 | 支持（主开发环境） | 本机全量测试与 10 GB Matching Cable 验收 |
| Linux CPU（CI） | 支持 | `linux-smoke`、`acceptance/source-linux` |
| Docker Linux | 通过 CI | `acceptance/docker-linux`；本机无 Docker，不冒充本机验证 |
| 多用户 / 权限 / 审计 | 不支持 | 团队版范围，未开始 |
| 无头单命令 `process --plan` | 支持 | 500 帧电信样本端到端 209.67 s；幂等重跑 0.54 s |
| 全量 11,879 帧转换链路 | 支持 | 预览 1,354.58 s；复用 `format_write` 184.75 s；`official_verify` 28.10 s；完整 7 集打包/登记未跑 |
| 阶段级幂等 | 支持 | 复用批次与当前产物；节点级断点续跑未做 |
| 数据卡 / 本地登记表 | 支持 | `dataset/dataset_card.json` + `registry.sqlite3` |
| Registry 只读 API | 支持 | `GET /api/registry`、`/api/registry/{id}`、`/api/registry/{id}/{version}` |
| Train 交接示例 | 支持 | `scripts/verify_train_handoff.py`：验卡、验结论、官方加载 |
| LeRobot v2.1 全链路 | 支持 | 2×5 合成夹具 `process` 97 s；幂等重跑 0.67 s |
| MCAP 检查 | 支持 | 合成 fixture 的 topic/type/计数；无真实样本 |
| 对象存储 / NAS | 不支持 | v0.16 评估 |

## 4. 交付与登记

| 能力 | 状态 | 说明 |
| --- | --- | --- |
| 交付 ZIP | 支持 | 只收 allowlist 文件；独立解包复核 |
| 数据卡 `dataset_card.json` | 支持 | 随 ZIP 走；`card_digest` 可重算 |
| 本地登记表 | 支持 | SQLite；同 ID/版本不同内容会冲突，不覆盖 |
| Train 侧契约 | 提供 | 只读数据卡与登记记录；不暴露内部路径 |
| 外部复现 | 0 条 | 不伪造采用记录 |

## 5. 版本策略

- 新增输入/输出格式：**minor**。
- 移除已声明支持的版本：先弃用一个 minor，再移除；移除属于 **major**。
- 已发布数据集的 `source_kind`、`format_version`、`rule_version` 不因升级改写。
- `lerobot` 固定版本变化必须同时更新：转换环境锁、契约测试、本矩阵、`docs/EVIDENCE.md`。

## 6. 明确不做

- 不承诺“所有机器人数据格式自动兼容”。
- 不在没有 golden/坏样本的情况下声明支持。
- 不把“示例脚本能解码”写成“平台支持导入”。
- 不自动补造缺失相机、深度、触觉或数值字段。

## 7. 已知边界

- 视频 `std=0`：LeRobot `uint8` 流式统计溢出；v0.14 已在转换入口把整数批次的平方/方差计算转 float64，直方图仍用整数批次，`tools/conversion/check_video_stats.py` 与合成夹具均可复现修复。旧版本生成的包仍保留旧统计，不追溯改写。
- Matching Cable 的 20 FPS 是文件级时间映射，不声明相机硬件同步。
- 复用预览视频依赖 `lerobot==0.4.4` 的内部 writer 行为；升级需要重跑契约测试与全量 A/B。
- LeRobot v2.1 转换调用官方 `convert_dataset_v21_to_v30`；只支持全量 Episode，且要求 `meta/episodes_stats.jsonl`。子集选择会明确失败。
- MCAP 当前是 **inspection-only**：`adapters detect/inspect` 可用，`prepare/import/process` 会拒绝；真实转换需要真实样本、冻结 topic 映射、时间对齐和 QoS/clock 语义。
- 外部复现记录当前为 **0**；本矩阵只代表本机与 CI 已验证范围。
