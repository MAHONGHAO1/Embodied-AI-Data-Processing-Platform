# v0.13.0 — Matching Cable：编码一次，复用预览视频

**把 Matching Cable 的第二次视频编码去掉，同时保留逐帧源数值回核和全有或全无回退。**

v0.12.1 的 Matching Cable 转换会在预览阶段编码一次视频，`format_write` 再编码一次。10 GB 量级数据的瓶颈正是这第二次 4 路 H.264 编码。v0.13.0 让预览为每个 Episode、每个相机生成独立 MP4；转换直接复制这些视频，只重写数值 parquet 和 LeRobot v3 元数据。

## 改了什么

- **预览布局**：预览阶段在 `LeRobotDataset.create` 后调用 `update_chunk_settings(data_files_size_in_mb=0.001, video_files_size_in_mb=0.001)`，并显式 `dataset.finalize()`，保证每个 Episode 一个数值 parquet、每路相机一个 MP4，且 parquet footer 已写完。
- **复用清单**：`source_manifest.json` 新增 `reuse` 块，记录每个 Episode 的数值 parquet、视频路径、大小、SHA-256、帧数、分辨率、帧率、时长和 `from_timestamp/to_timestamp`。清单只在 `finalize()` 之后生成。
- **转换复用**：`format_write` 校验复用清单、视频哈希、帧数、分辨率、时长和相机集合，然后复制预览 MP4、重写数值 parquet 的行索引/任务索引，并用预览统计与视频统计重建 `meta/episodes`、`meta/stats.json`、`meta/tasks.parquet` 和 `info.json`。
- **源数值回核**：复用路径逐帧重读已选源 HDF5，把 `observation.state`、`action`、`source.timestamp_ms`、可选数值字段与预览 parquet 对照；任一不一致即回退重编码。`format_write` 记录 `source_numeric_consistency_rechecked=true`，`official_verify` 仍记录 `false`，两者范围不同。
- **全有或全无回退**：预览布局不兼容、视频哈希变化、统计缺失、复用数量不完整等任一问题，整批回退到 v0.12 的重编码路径，并记录 `fallback_reason`；不会产生“部分 Episode 复用、部分重编码”的混合输出。
- **子集重编号**：输出 Episode 可以只选预览中的一部分，并重新编号为 `0..N-1`；`index`、`episode_index`、`task_index` 和统计会按输出顺序重写。
- **版本固定**：`tools/conversion/pyproject.toml` 固定 `lerobot==0.4.4`；预览和转换入口会检查运行时版本，内部 API 漂移时直接失败。

## 500 帧 A/B（Episode 68，同机实测）

| 阶段 / 策略 | 耗时 |
| --- | ---: |
| 预览（首次编码，4 路相机） | 60.79 s |
| `format_write` 复用预览视频 | **18.28 s** |
| `format_write` 强制重编码 | 58.71 s |
| 复用输出 `official_verify` | 16.44 s |
| 重编码输出 `official_verify` | 18.20 s |

复用与重编码的数值 parquet 逐列相等，4 路视频文件 SHA-256 也逐字节相等。复用路径约为重编码路径的 31%。这是 500 帧实测，不是全量外推。

## 全量验收

本机对电信 Matching Cable 的 7 Episode / 11,879 帧 / 10,038,385,032 字节做了完整运行。源数据解压到仓库外临时目录，不进入 Git；预览与输出在 `work/telecom-full-v013/`，不随仓库分发。

| 步骤 | 结果 |
| --- | --- |
| 全量预览（首次编码 4 路相机） | **1,338.44 s（22 分 18.44 秒）**；7 Episode / 28 个独立 MP4 |
| `format_write` 复用预览视频 | **206.46 s（3 分 26.46 秒）**；28 个视频全部复用 |
| `format_write` 干净重编码 A/B | **1,072.68 s（17 分 52.68 秒）** |
| 复用输出 `official_verify` | 28.51 s；84 个采样位置；`batch_size=2` |
| 复用输出 `output_quality` | 73.09 s；7/7 Episode；77/77 覆盖；0 issue |
| 复用输出 `archive_verify` | 27.89 s；通过 |

复用路径约为干净重编码耗时的 **19.2%**（5.2×）。11,879 行 × 12 列数值逐列相等；28/28 个视频路径与 SHA-256 相同。28 个视频的规范哈希汇总为 `c8fcf19f308b965580e20da1c12e288d6582796f34382905ece400fdf52f451e`。逐项命令与证据见 [docs/EVIDENCE.md](docs/EVIDENCE.md) 5.2。

## 验证

- Python 全量：**328 passed / 14 skipped / 13 subtests passed**
- `scripts/verify_matching_optional.py`：预览复用、子集重编号、篡改视频回退、官方加载与 archive verify 全部通过
- 2×2 合成夹具完整业务链：14 个节点全部 `completed`，`outcome=passed`，登记 validation `source_numeric=true`、`source_numeric_scope=format_write`
- 前端单测与生产构建：见 `docs/EVIDENCE.md`
- 全量 7 Episode / 11,879 帧：见 `docs/EVIDENCE.md`

## 不声明

- 不声明策略训练效果、任务成功率或生产容量。
- 不声明相机硬件同步；文件级 20 FPS 映射不等于硬件同步。
- 不声明缺失流被自动修复；缺失相机、深度、触觉和可选字段仍是来源事实。
- 视频 `std` 仍为 0，来自 LeRobot 对 `uint8` 帧的流式平方溢出；这是 v0.12.1 已存在的遗留问题，v0.13.0 没有修改统计算法。
- 全量结论只引用实际跑完的记录；未跑过的规模不写成已验证。

完整证据、命令、计数和未验证边界见 [docs/EVIDENCE.md](docs/EVIDENCE.md)。
