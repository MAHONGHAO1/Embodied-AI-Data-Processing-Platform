# v0.12.1 · 大样本编码提速与超时校准

**不改变字段语义，只让 10 GB 量级的 Matching Cable 数据跑得完。**

## 真实数据基线

对电信发来的 Matching Cable ZIP（7 Episode / 11,879 帧 / 约 10.04 GB）做了结构检查与 500 帧实测：

- 4 路 RGB、2 路深度、全部可选数值字段存在；触觉不存在，记录 `not_provided`；
- 帧号连续，抽样 7×207 个相邻时间戳均为 49–51 ms；
- 10 帧预览 13.24 s，100 帧 27.08 s，500 帧 75.73 s（默认编码线程）。

## 编码优化

默认编码线程从“由编码器自行决定”改为：

```text
max(1, min(4, CPU 逻辑核数 // 4))
```

四路相机各用一个编码器时，给解码和 I/O 留出余量。20 逻辑核机器上从 500 帧 75.73 s 降到 64.74 s（约 **15%**）。

可选环境变量：

| 变量 | 作用 |
| --- | --- |
| `ROBODATA_MATCHING_ENCODER_THREADS` | 覆盖每路编码器线程数；默认按 CPU 计算 |
| `ROBODATA_MATCHING_VCODEC` | `h264`（默认）、`h264_nvenc`、`hevc_nvenc` 或 `auto` |

本机存在 `h264_nvenc`，500 帧预览 63.59 s、500 帧 `format_write` 62.79 s；比 4 线程 x264 只快约 2%，所以主要收益是线程上限，不是硬件编码。没有 NVENC 的机器保持默认 `h264`。

## 超时校准

`MATCHING_TIMEOUTS` 按 10 GB 量级调整：

| 阶段 | 旧值 | 新值 |
| --- | ---: | ---: |
| `format_write` | 30 min | 60 min |
| `official_verify` | 20 min | 30 min |
| `output_quality` | 20 min | 40 min |
| `package` / `verify` / `archive_verify` | 30 min | 60 min |
| `file_validation` | 10 min | 20 min |
| `matching_validation` / `field_mapping` | 20 min | 30 min |

按 500 帧稳态外推，11,879 帧全量 `format_write` 约 25 分钟、official 加载约 7 分钟；新超时留出了磁盘抖动和 Episode 边界余量，但仍会在失败时停在对应节点，不是无限等待。

## 全量运行建议

```powershell
$env:ROBODATA_MATCHING_EPISODES='68'
$env:ROBODATA_MATCHING_MAX_FRAMES_PER_EPISODE='100'
# 先烟测，再移除这两个变量跑全量
$env:ROBODATA_MATCHING_ENCODER_THREADS='4'
# 可选：确认本机有 NVENC 后再启用
$env:ROBODATA_MATCHING_VCODEC='h264_nvenc'
```

- 先解压 ZIP 到本机磁盘，不要直接在 ZIP 内跑；
- 全量至少预留 30–40 GB 空间（预览 + 输出 + 临时视频）；
- 预览和转换各编码一次；复用预览视频、只编码一次属于后续架构优化，不在本版。

## 不宣称

- 500 帧实测外推不是 11,879 帧全量实测；实际时间受磁盘、Episode 边界和系统负载影响。
- NVENC 不是数量级提升；本机只快约 2%。
- 不改变字段语义、不补造缺失流、不声明硬件同步或策略训练效果。

## 证据

- [Matching Cable 适配器](docs/MATCHING_CABLE.md)：字段、速度与全量运行建议
- [证据总表](docs/EVIDENCE.md)：测试计数、合成回归与真实数据兼容性记录
- [真实数据兼容性报告](work/telecom-compatibility.md)：本机生成，不随仓库分发
