# 可复现验收 · 2 Episode 真实样本

这条路径回答一个比「我本机跑过」更强的问题：**陌生人拿到仓库，能不能自己下载固定来源、跑完导入、质检、审核、转换、打包，并独立验证交付包。**

它不证明策略训练、任务成功率、硬件同步或生产容量。边界写在最后。

只想先判断环境能否跑通、不下载样本，见 [五分钟复现](REPRODUCTION.md) 的 Tier 1。

## 1. 准备

需要 Python 3.12、[uv](https://docs.astral.sh/uv/getting-started/installation/) 和约 100 MB 磁盘。

```bash
git clone https://github.com/MAHONGHAO1/Embodied-AI-Data-Processing-Platform.git
cd Embodied-AI-Data-Processing-Platform
uv sync --locked
```

转换与官方加载需要隔离环境（约 2 GB，含 CPU 版 PyTorch）：

```bash
uv sync --project tools/conversion --locked
```

只跑来源、导入和表格质检时不需要这一步。

## 2. 下载固定样本

```bash
uv run --locked robodata fetch-sample \
  --episodes 0-1 \
  --data-root work/samples/so100-e0-1
```

下载的是上游固定 revision `54141acb0bd6bcb868e34b4eb328f26481d333b2` 的**完整 Episode 0 和 1**，共 9 个文件、约 19.2 MB。没有裁帧，没有重写视频；每个文件都由上游清单的大小与哈希校验，清单写入 `work/samples/so100-e0-1/source_manifest.json`。

如果本机到 `huggingface.co` 不通，可以显式指定传输镜像：

```bash
uv run --locked robodata fetch-sample \
  --episodes 0-1 \
  --data-root work/samples/so100-e0-1 \
  --endpoint https://hf-mirror.com
```

镜像只改变传输路径；revision 仍是固定的，清单会记录 `transport_endpoint`，导入时还会与仓库内冻结的 Episode 0–4 SHA-256 清单交叉校验。镜像不是信任根，固定哈希才是。

离线复核本地字节：

```bash
uv run --locked robodata fetch-sample \
  --verify-only --episodes 0-1 \
  --data-root work/samples/so100-e0-1
```

联网复核固定 revision 的上游文件标识：

```bash
uv run --locked robodata fetch-sample \
  --verify-only --verify-upstream --episodes 0-1 \
  --data-root work/samples/so100-e0-1 \
  --endpoint https://hf-mirror.com
```

## 3. 一条命令跑完

```bash
uv run --locked python scripts/verify_pipeline.py \
  --sample work/samples/so100-e0-1 \
  --episodes 0-1 \
  --mode full \
  --output work/acceptance/pipeline-full.json
```

三种模式：

| 模式 | 跑到哪 | 需要转换环境 | 用途 |
| --- | --- | --- | --- |
| `source` | 来源校验 + 导入 + 基础质检 + 审核夹具 | 否 | Linux CI / 快速核对来源与表格 |
| `fast` | 再加转换 + 官方 DataLoader 探针 + 打包 + 独立 ZIP 验证 | 是 | 默认核心闭环，不做视频抽帧 |
| `full` | 再加原始预览与输出视频像素抽样 | 是 | 最完整的小样本验收 |

退出码 `0` 表示报告 `summary.passed=true`；`1` 表示某阶段失败，报告仍会写出，`summary.error` 给出阶段与原因。

## 4. 本机实测（2026-11-18）

环境：Windows 11、Python 3.12.10、CPU、2 Episode，`--mode full`。

| 阶段 | 结果 | 耗时 |
| --- | --- | ---: |
| 来源清单校验 | 9 个文件 / 19,215,391 字节 | 0.05 s |
| 导入 + 基础质检 | 2 Episode / 1,300 帧 / 22 项覆盖 / 0 问题 | 7.97 s |
| 原始预览 | 6 个采样位置，`480×640×3`，无问题 | 2.03 s |
| 审核夹具 | 2 条保留，全部批准 | 0.20 s |
| 转换 + 官方离线验证 | 1,300 帧，22/22 输出覆盖 | 48.14 s |
| 输出抽帧 | 6 个位置与转换运行保存的解码像素逐一相等 | 1.55 s |
| 官方 DataLoader 探针 | 4 个 batch / 32 帧；动作、状态、图像形状与有限性通过 | 31.23 s |
| 交付 | ZIP 独立解包 + 官方加载 | 14.88 s |
| 独立 ZIP 校验 | 2 Episode / 1,300 帧 | 0.05 s |
| **合计** | 9 个阶段 | **约 106.1 s** |

原始报告：`work/acceptance/pipeline-full.json`（运行时生成，不随仓库分发）。本次运行同时确认：

- 官方 `official_verify`：1,300 帧数值检查通过，`offline=true`；
- 交付 ZIP：19,203,961 字节，独立解包后 2 Episode / 1,300 帧，6 个采样位置通过；
- DataLoader 探针读到的帧数、Episode 数与输出清单一致。

DataLoader 探针使用独立转换环境中的 `lerobot 0.4.4` + `torch 2.7.1+cpu`，在 `HF_HUB_OFFLINE=1` 下按 `batch_size=8`、`num_workers=0`、不打乱顺序读取前 4 个 batch。它核对：

- 加载帧数与 Episode 数等于输出清单；
- `action` / `observation.state` 为 `float32 [B,6]` 且全部有限；
- 图像为 `float32 [B,3,480,640]`、值域 `[0,1]`；
- `task` 是非空字符串列表；
- 不打乱时首尾动作与 `dataset[0]` / `dataset[-1]` 逐元素相等。

这是“官方训练加载器能消费这份交付物”的证据，**不是**训练效果、GPU 吞吐或策略成功率。本次探针在本机 CPU 上 32 帧解码 23.4 秒（约 1.37 帧/秒）；同一版本前一次 `full` 运行是 29.5 秒（约 1.08 帧/秒），更早的 `fast` 运行是 15.3 秒（约 2.09 帧/秒）。三次相差约 2 倍，说明它受系统负载与视频定位缓存影响很大，只反映该机器上逐帧视频定位的真实成本，不作为性能上限或稳定吞吐。完整分阶段记录见 [流水线性能基线](benchmarks/2026-11-18-pipeline-2-episodes.md)。

ZIP 整包 SHA-256 会随运行目录、时间戳和内容版本变化，**不要把它当成跨运行常量**；可复现的是源文件哈希、行数、Episode 数和各阶段结论。

## 5. 失败排查

| 现象 | 先查 |
| --- | --- |
| 下载超时 / 429 | 换 `--endpoint` 镜像或稍后重试；已下载文件会复用并重新校验 |
| `缺少转换环境` | `uv sync --project tools/conversion --locked` |
| `source_manifest.json` revision 不一致 | 删除样本目录重新下载；不要手工改清单 |
| 导入报文件哈希不一致 | 运行 `--verify-only`；确认没有删除或覆盖样本文件 |
| 转换失败 | 看 `work/acceptance/runs/<run_id>/diagnostic.json` 和 `steps/*.json` |
| ZIP 验证失败 | 不要手工解包后重压；重新跑 `verify_pipeline.py` |

## 6. 边界

- 这是**固定来源的 2 Episode 子集**，不是 ALOHA / SO-100 全量历史验收，也不是任意 LeRobot 数据集的保证。
- 审核标签是验收脚本写入的流程夹具，**不代表源任务实际成功或失败**。
- 转换通过证明格式、字段映射、数值和交付包一致，**不证明策略训练效果、任务成功率或硬件同步**。
- 没有本机 Docker 验证；Linux 容器只在 GitHub Actions 的 `ubuntu-latest` 上构建并检查健康页与坏样本，见 [DOCKER.md](DOCKER.md)。容器内不安装转换环境，因此不做 Linux 真实数据转换声明。Linux CI 的 `--mode source` 只覆盖来源、导入和表格质检。
- 采样和耗时是这台机器的单次记录，不是性能上限，也没有测内存峰值或并发。
