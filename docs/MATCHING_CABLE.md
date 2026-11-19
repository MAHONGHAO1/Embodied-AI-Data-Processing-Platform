# Matching Cable 本机遥操数据

这个适配器接收 Matching Cable 的逐帧 HDF5 目录，并把**实际存在的流**写进批次清单。缺失的相机、深度或可选数值字段不再直接报错，而是记录为 `not_provided`，在预览、质检、界面和交付说明中逐条可见。

![v0.12.0 合成夹具：右侧来源锁定显示已写入 camera_0、缺失 camera_1..3、缺失两路深度、触觉 not_provided](assets/matching-cable-v0.12.0.png)

> 这张图来自 2 Episode × 2 帧的合成夹具，视频是 32×24 纯色画面；它的作用是证明缺失流在界面上逐条可见，不是真实 Matching Cable 数据的效果图。

## 目录与必填字段

父目录下每个数字目录是一条 Episode（例如 `68`–`74`），每个 `.hdf5` 是一帧：

```text
matching_cable/
  68/
    0.hdf5
    1.hdf5
  70/
    0.hdf5
    ...
```

必填：

| 字段 | 维度 | 用途 |
| --- | --- | --- |
| `master/joint_position` | float32[16] | `action` |
| `puppet/joint_position` | float32[16] | `observation.state` |
| `timestamps` | 单值毫秒 | 校验 50 ± 10 ms/帧，输出按 20 FPS 派生 |

至少需要一路 RGB 相机；没有任何 RGB 时明确失败，因为当前预览和时间轴需要至少一路视频。

## 可选流

| 流 | 字段 | 缺失时 |
| --- | --- | --- |
| RGB 相机 | `observations/rgb_images/camera_0..3` | 只写入存在的相机；其余记入 `missing_cameras` |
| 深度 | `observations/depth_images/camera_1..2` | 存在的校验可读；缺失记入 `missing_depth_source_cameras`，不阻断 |
| 末端位姿 | `puppet/eef`、`master/eef` | 缺失记入 `optional_fields=not_provided`，不写入特征 |
| 关节力矩 / 六维力 | `puppet/joint_effort`、`puppet/6f` | 同上 |
| 触觉 | 未建模 | 固定记录 `tactile.status=not_provided`；缺失或存在都不影响导入 |

清单会同时记录 `cameras`、`missing_cameras`、`depth_source_cameras`、`missing_depth_source_cameras`、`optional_fields` 和 `tactile`。转换只写入实际存在的特征；验证语义分两层：

- `format_write`：逐帧重读**已选 Episode**的源 HDF5，数值、时间戳、可选字段和相机/深度存在性与预览 parquet 对照；复用失败时整批回退到重编码，不混合来源。
- `official_verify`：`output_loader_consistency_checked=true` 表示输出 parquet 与 `lerobot 0.4.4` 官方加载器一致；`source_numeric_consistency_rechecked=false` 表示**这一阶段**不重读源 HDF5。源数值回核记录在 `format_write` 的 `conversion_strategy.source_numeric_consistency_rechecked=true`，不要把它误读成官方验证阶段完成了回核。

## 怎么跑

完整业务链路从界面导入父目录或单条 Episode 目录，选择「Matching Cable · 单帧 HDF5 遥操目录」。也可以直接用 CLI：

```bash
uv run --locked robodata import-batch \
  --input "E:/遥操数据/matching_cable" \
  --source matching_cable \
  --label "Matching Cable 68-70" \
  --runs-root work/runs \
  --business-root work/business
```

预览和转换需要独立环境（包含 h5py、PyTorch、LeRobot）：

```bash
uv sync --project tools/conversion --locked
uv run --project tools/conversion --locked python tools/conversion/matching_preview.py \
  --input "E:/遥操数据/matching_cable" --output work/matching-preview
```

## 速度与全量运行

真实 10 GB 数据的瓶颈是 4 路 RGB 的 H.264 编码，不是 HDF5 读取。v0.13.0 起，预览阶段已经生成每 Episode、每相机一个 MP4；`format_write` 直接复用这些视频，只重写数值 parquet 与元数据，不再做第二次视频编码。

500 帧 Episode 68 同机实测（20 个逻辑 CPU，默认 `max(1, min(4, CPU//4))` 编码线程）：

| 阶段 / 策略 | 耗时 |
| --- | ---: |
| 预览（首次编码，4 路相机） | 60.79 s |
| `format_write` 复用预览视频 | 18.28 s |
| `format_write` 强制重编码（A/B） | 58.71 s |
| 复用输出 `official_verify` | 16.44 s |
| 重编码输出 `official_verify` | 18.20 s |

500 帧 A/B 中，复用与重编码的数值 parquet 逐列相等，4 路视频文件 SHA-256 也逐字节相等。复用路径耗时约为重编码路径的 **31%**（500 帧实测，不是全量外推）。

全量 7 Episode / 11,879 帧也已实跑：预览 **1,338.44 s（22 分 18.44 秒）**，复用 `format_write` **206.46 s（3 分 26.46 秒）**，干净重编码 **1,072.68 s（17 分 52.68 秒）**；复用约为重编码的 **19.2%**。11,879 行 × 12 列数值逐列相等，28/28 个视频路径与 SHA-256 相同。复用输出 `official_verify` 28.51 s、`output_quality` 73.09 s（77/77 覆盖、0 issue）、`archive_verify` 27.89 s。完整命令、哈希与边界见 [EVIDENCE.md](EVIDENCE.md) 5.2。

复用不是“跳过验证”：

- 预览必须先在 `dataset.finalize()` 后关闭 parquet footer 与流式编码器，然后才写 `reuse` 清单与视频哈希；
- 预览必须是一集、一相机、一个 MP4，`from_timestamp=0`，且 Episode 之间不共享视频；
- `format_write` 会逐帧重读已选源 HDF5 的数值与时间戳，与预览 parquet 对照；任一 Episode/相机哈希、帧数、分辨率、时长或数值不一致，整批回退到重编码并记录 `fallback_reason`；
- 不混合“部分复用、部分重编码”的输出，避免同一交付里出现两种来源。

默认编码线程按 `max(1, min(4, CPU//4))` 计算，四路相机共享 CPU 时给解码和 I/O 留余量。也可以显式覆盖：

```powershell
$env:ROBODATA_MATCHING_ENCODER_THREADS='4'
$env:ROBODATA_MATCHING_VCODEC='h264_nvenc'   # 可选；先用 detect_available_hw_encoders() 确认
uv run --locked robodata import-batch ...
```

`h264_nvenc` 必须实际存在于本机；没有 NVENC 时保持默认 `h264`。v0.12.1 实测 NVENC 只比 4 线程 x264 快约 2%，主要收益来自限制每路编码线程数，不要把 NVENC 当成数量级提升。

全量 11 GB 数据建议：

1. 先把 ZIP 解压到本机磁盘，不要直接在 ZIP 内跑；
2. 先用 `ROBODATA_MATCHING_EPISODES=68` + `ROBODATA_MATCHING_MAX_FRAMES_PER_EPISODE=100` 做烟测；
3. 全量跑时确认磁盘至少还有 30–40 GB 空间（预览 + 输出 + 临时视频）；
4. `MATCHING_TIMEOUTS` 已按 10 GB 量级调整：`format_write` 1 小时、`official_verify` 30 分钟、`output_quality` 40 分钟、打包/验包各 1 小时。超时仍不是“无限等待”，失败会停在对应节点。
5. 复用路径会额外对已选源帧做一次 SHA-256 复核；这是拿时间换来源可信度，不要为了快而关掉。

## 编码一次的实现边界

- 预览是编码真源：最终视频字节来自预览，转换不再重新编码。预览目录必须保留到转换完成；预览被删除或 `reuse` 清单缺失时，worker 会回退到 v0.12 的两遍编码路径。
- 数值来源是预览 parquet，但 `format_write` 会用全部已选源帧逐帧对照；对照失败会回退重编码，不会把“预览值”直接当成源值交付。
- 深度图在预览阶段已校验可读，转换复用阶段只核对存在性；源文件 SHA-256 与数值回核共同保证源未变化。
- 复用依赖 `lerobot==0.4.4` 的 `save_episode`、`update_chunk_settings`、`finalize` 等内部行为；`tools/conversion/pyproject.toml` 已固定精确版本，预览与转换入口也会在运行时检查版本，漂移时直接失败。
- `matching_worker.py --no-reuse` 只用于 A/B 基准或故障排查；业务链路不会传这个参数，用户的正常转换默认走复用。
- 已知限制：LeRobot 流式统计对 `uint8` 帧做平方时溢出，视频 `std` 仍为 0。这一现象在 v0.12.1 及预览中已存在，v0.13.0 没有顺带修改；它不影响解码、数值一致性或官方加载检查，但不应被当作“视频统计已正确”的证据。

## 合成回归

不需要 9 GB 真实数据。下面的脚本会生成 2 条 Episode × 2 帧、只有一路 RGB、没有深度/触觉/可选数值的夹具，跑预览、完整复用转换、子集重编号和故意篡改后的回退路径：

```bash
uv run --locked python scripts/verify_matching_optional.py
```

期望输出：

- `cameras` 只有 `observation.images.camera_0`；
- `missing_cameras` 3 路，`missing_depth_source_cameras` 2 路；
- `optional_fields` 全部 `not_provided`，`tactile.status=not_provided`；
- 预览有 2 个独立 MP4，`reuse.layout=one_video_per_episode`；
- 主转换 `strategy=preview_video_reuse`、`video_files_reused=2`、`source_numeric_consistency_rechecked=true`；
- 子集转换把源 Episode 1 重编号为输出 0，数值行 `index=[0,1]`、`episode_index=[0,0]`、`task_index=[0,0]`；
- 篡改一个预览视频后自动回退 `strategy=legacy_reencode`，`fallback_reason` 非空，输出仍通过官方加载；
- `official_verify` 仍记录 `output_loader_consistency_checked=true`、`source_numeric_consistency_rechecked=false`（它不重读源 HDF5）。

原始报告写在 `work/matching-optional/report.json`，不随仓库分发。

## 边界

- 不声明相机硬件同步；逐路文件时间映射不等于硬件同步。
- 不声明策略训练效果、任务成功率或生产容量。
- 缺失流是**来源事实**，不是自动修复；平台不会补造深度、触觉或相机画面。
- 本适配器只读取上述字段；其他 Matching Cable 字段不会进入交付。
- 视频 `std` 统计仍为 0（LeRobot `uint8` 流式平方溢出），是已知遗留问题；v0.13.0 没有修改统计算法，也没有把它当作视频统计正确性的证据。
