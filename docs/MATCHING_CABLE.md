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

清单会同时记录 `cameras`、`missing_cameras`、`depth_source_cameras`、`missing_depth_source_cameras`、`optional_fields` 和 `tactile`。转换只写入实际存在的特征；`official_verify` 明确区分：

- `output_loader_consistency_checked=true`：输出 parquet 与官方加载器一致；
- `source_numeric_consistency_rechecked=false`：本次没有重新读取源 HDF5 做逐帧数值回核。

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

真实 10 GB 数据的瓶颈是 4 路 RGB 的 H.264 编码，不是 HDF5 读取。合成夹具无法代表这个量级，下面是对电信数据 Episode 68 做的实测（同一台机器，20 个逻辑 CPU）：

| 样本 | 配置 | 耗时 |
| --- | ---: | ---: |
| 10 帧 | 默认（编码线程 auto） | 13.24 s |
| 100 帧 | 默认 | 27.08 s |
| 500 帧 | 默认 | 75.73 s |
| 500 帧 | `ROBODATA_MATCHING_ENCODER_THREADS=4` | 64.74 s |
| 500 帧 | 4 线程 + `h264_nvenc` | 63.59 s |
| 500 帧 format_write | 4 线程 + `h264_nvenc` | 62.79 s |
| 500 帧 official_verify | 同上输出 | 17.71 s |

按 500 帧的稳态粗略外推，11,879 帧全量 `format_write` 约 **25 分钟量级**，official 加载约 **7 分钟量级**；实际会受磁盘、Episode 边界和系统负载影响。

默认编码线程现在按 `max(1, min(4, CPU//4))` 计算，四路相机共享 CPU 时给解码和 I/O 留余量。也可以显式覆盖：

```powershell
$env:ROBODATA_MATCHING_ENCODER_THREADS='4'
$env:ROBODATA_MATCHING_VCODEC='h264_nvenc'   # 可选；先用 detect_available_hw_encoders() 确认
uv run --locked robodata import-batch ...
```

`h264_nvenc` 必须实际存在于本机；没有 NVENC 时保持默认 `h264`。本机实测 NVENC 只比 4 线程 x264 快约 2%，主要收益来自**限制每路编码线程数**，不要把 NVENC 当成数量级提升。

全量 11 GB 数据建议：

1. 先把 ZIP 解压到本机磁盘，不要直接在 ZIP 内跑；
2. 先用 `ROBODATA_MATCHING_EPISODES=68` + `ROBODATA_MATCHING_MAX_FRAMES_PER_EPISODE=100` 做烟测；
3. 全量跑时确认磁盘至少还有 30–40 GB 空间（预览 + 输出 + 临时视频）；
4. `MATCHING_TIMEOUTS` 已按 10 GB 量级调整：`format_write` 1 小时、`official_verify` 30 分钟、`output_quality` 40 分钟、打包/验包各 1 小时。超时仍不是“无限等待”，失败会停在对应节点。

进一步的数量级优化（复用预览视频、只编码一次、按 Episode 并行）属于架构改动，不在当前适配器内；现在的可复现路径是“预览 + 转换各编码一次”，用线程数和可选 NVENC 把单次编码压到可接受范围。

## 合成回归

不需要 9 GB 真实数据。下面的脚本会生成 2 条 Episode × 2 帧、只有一路 RGB、没有深度/触觉/可选数值的夹具，跑完预览、六个转换/验证阶段并断言缺失流可见：

```bash
uv run --locked python scripts/verify_matching_optional.py
```

期望输出：

- `cameras` 只有 `observation.images.camera_0`；
- `missing_cameras` 3 路，`missing_depth_source_cameras` 2 路；
- `optional_fields` 全部 `not_provided`，`tactile.status=not_provided`；
- 6 个阶段通过，`output_loader_consistency_checked=true`、`source_numeric_consistency_rechecked=false`。

原始报告写在 `work/matching-optional/report.json`，不随仓库分发。

## 边界

- 不声明相机硬件同步；逐路文件时间映射不等于硬件同步。
- 不声明策略训练效果、任务成功率或生产容量。
- 缺失流是**来源事实**，不是自动修复；平台不会补造深度、触觉或相机画面。
- 本适配器只读取上述字段；其他 Matching Cable 字段不会进入交付。
