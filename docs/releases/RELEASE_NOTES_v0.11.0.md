# v0.11.0 · 可复现验收

**这一版不加导入格式、不加页面、不加适配器。** 它把「拿固定来源的真实 Episode，一条命令跑完并交付」做成可复现路径，并把官方训练加载器拉进验收。

## 真实样本，固定来源

新增 `robodata fetch-sample`：按 Episode 子集从固定 revision 下载，逐文件校验上游清单的大小与 SHA-256，写出 `source_manifest.json`。

- 本机样本：revision `54141acb0bd6bcb868e34b4eb328f26481d333b2` 的完整 Episode 0 和 1，9 个文件 / 19,215,391 字节。
- 没有裁帧，没有重写视频；下载的是上游完整 Episode。
- `--endpoint` 只改变传输路径（本机使用 `hf-mirror.com`），revision 与固定哈希仍是信任根，清单记录 `transport_endpoint`。
- 导入时把下载清单与仓库内冻结的 Episode 0–4 SHA-256 清单交叉校验，不一致就失败。

## 一条命令，9 个阶段

新增 `scripts/verify_pipeline.py`，把来源校验、导入与基础质检、原始预览、审核夹具、转换、输出抽帧、官方 DataLoader、交付、独立 ZIP 验证串成一次运行，退出码与分阶段 JSON 报告同时给出。

本机 `--mode full`（Windows 11 / Python 3.12.10 / CPU / 2 Episode）：

| 阶段 | 结果 | 耗时 |
| --- | --- | ---: |
| 来源清单校验 | 9 个文件 / 19,215,391 字节 | 0.05 s |
| 导入 + 基础质检 | 2 Episode / 1,300 帧 / 22 项覆盖 / 0 问题 | 7.97 s |
| 原始预览 | 6 个采样位置，`480×640×3` | 2.03 s |
| 审核夹具 | 2 条保留，全部批准 | 0.20 s |
| 转换 + 官方离线验证 | 1,300 帧，22/22 输出覆盖 | 48.14 s |
| 输出抽帧 | 6 个位置与解码像素逐一相等 | 1.55 s |
| 官方 DataLoader 探针 | 4 个 batch / 32 帧 | 31.23 s |
| 交付 | ZIP 独立解包 + 官方加载 | 14.88 s |
| 独立 ZIP 校验 | 2 Episode / 1,300 帧 | 0.05 s |
| **合计** | 9 个阶段 | **约 106.1 s** |

失败路径由自动化测试覆盖：坏样本在 `file_validation` 停止，后续节点统一收口为 `skipped`；终态与节点状态在同一次写入中发布，轮询方不会看到「已失败但后续节点仍 pending」的中间状态。

## 官方 DataLoader 探针

新增 `scripts/probe_dataloader.py`，在独立转换环境中用 `lerobot 0.4.4` + `torch 2.7.1+cpu`，`HF_HUB_OFFLINE=1`、`batch_size=8`、`num_workers=0`、不打乱顺序读取前 4 个 batch。它核对：

- 加载帧数与 Episode 数等于输出清单；
- `action` / `observation.state` 为 `float32 [B,6]` 且全部有限；
- 图像为 `float32 [B,3,480,640]`、值域 `[0,1]`；
- `task` 是非空字符串列表；
- 不打乱时首尾动作与 `dataset[0]` / `dataset[-1]` 逐元素相等。

本机 32 帧解码 23.373 秒（1.37 帧/秒）；同一版本前一次 `full` 运行是 29.497 秒（1.08 帧/秒），更早的 `fast` 运行是 15.3 秒（2.09 帧/秒）。三次相差约 2 倍，说明它受系统负载与视频定位缓存影响很大。**这是官方加载器可消费交付物的证据，不是训练效果、GPU 吞吐或策略成功率。**

## Docker / Linux

新增 `Dockerfile`、`.dockerignore` 和 `acceptance.yml` 的三个 job：

- `source-linux`：Linux 上跑来源、导入与表格质检；
- `full-windows`：Windows 上下载固定样本并跑 `--mode full`；
- `docker-linux`：构建镜像、检查健康页与坏样本。

本机没有 Docker，因此容器结论只来自 CI。容器内不安装转换环境，不做 Linux 真实数据转换或 Linux 桌面启动声明。

## 证据入口

- [docs/ACCEPTANCE.md](../ACCEPTANCE.md)：下载、复核、三种模式与失败排查
- [docs/EVIDENCE.md](../EVIDENCE.md)：版本链、测试计数、真实样本验收、历史记录、规模实验与未验证事项
- [docs/benchmarks/2026-11-18-pipeline-2-episodes.md](../benchmarks/2026-11-18-pipeline-2-episodes.md)：分阶段耗时与 DataLoader 边界
- [docs/DOCKER.md](../DOCKER.md)：容器范围与非声明

## 不宣称

- 2 Episode 子集不等于 SO-100 全量 50 条 / 32,068 帧，也不等于任意 LeRobot 数据集。
- 审核标签是脚本写入的验收夹具，不代表源任务成功或失败。
- 转换通过不证明训练效果、任务成功率或硬件同步。
- 本机没有 Docker；Linux 容器只在 CI 构建验证，不做容器内转换声明。
- 仍是本机单用户、`127.0.0.1` loopback；公网多用户能力未在本版处理。
