# 证据总表 · v0.11.0

本文件是 README 中版本、测试数量、规模实验和训练冒烟的统一出处。README 只指向这里，不再复制会漂移的数字。

最后更新：2026-11-18。环境：Windows 11、Python 3.12.10、Node.js 24、本地单用户。

## 1. 版本链

| 位置 | 值 | 校验 |
| --- | --- | --- |
| README 首行 | 0.11.0 | `tests/test_version_consistency.py` |
| README release 徽章 | 0.11.0 | 同上 |
| `pyproject.toml` | 0.11.0 | 同上 |
| `robodata/config.py` `APP_VERSION` | 0.11.0 | 同上 |
| `robodata/web.py` FastAPI | 0.11.0 | 同上 |
| `robodata/web.py` 健康检查 | 0.11.0 | 同上 |
| `frontend/package.json` | 0.11.0 | 同上 |
| `frontend/src/App.tsx` 版本条 | 0.11.0 | 同上 |
| `RELEASE_NOTES_v0.11.0.md` | 存在 | 同上 |

`APP_VERSION` 会写进质量报告；版本链漂移会让报告声称自己来自一个从未发布的版本，因此用测试而不是人工纪律守住。

## 2. 自动化测试

```powershell
uv sync --locked
uv run --locked python -m pytest -q
uv run --locked robodata verify-fixtures
Push-Location frontend
npm ci
npm test
npm run build
npx playwright install chromium
npm run test:e2e
Pop-Location
```

本机本轮（2026-11-18）：

| 套件 | 结果 |
| --- | --- |
| Python 全量（Windows） | **318 passed / 14 skipped / 13 subtests passed** |
| 已知坏样本 `verify-fixtures` | **6/6 通过** |
| 前端单测（Vitest） | **26 passed** |
| 前端生产构建 | 通过 |
| Chromium 浏览器流程 | **2 passed** |

跳过项主要来自：需要 `tools/conversion/` 独立环境或固定真实数据的用例、可选 ROS2 MCAP 依赖，以及默认不启用的 Git tag 校验（`CHECK_RELEASE_TAG=1`）。跳过不等于通过。

CI 结果按提交记录（2026-11-18）：

| 运行 | 提交 | 结果 |
| --- | --- | --- |
| [tests #44](https://github.com/MAHONGHAO1/Embodied-AI-Data-Processing-Platform/actions/runs/44326128873) | `a481caf` | success：前端单测、构建、Chromium 流程；Python 全量；`verify-fixtures` |
| [linux-smoke #7](https://github.com/MAHONGHAO1/Embodied-AI-Data-Processing-Platform/actions/runs/44326128974) | `a481caf` | success：Ubuntu 版本一致性、表格质检、坏样本校验、100 步冒烟 |
| [acceptance #2](https://github.com/MAHONGHAO1/Embodied-AI-Data-Processing-Platform/actions/runs/44326142455) | `a481caf` | success：`source-linux`、`full-windows`、`docker-linux` 三个 job 全部通过 |

`acceptance #1`（run 44325548369）的 `full-windows` 在 9 个阶段全部通过后，因 Windows 控制台 cp1252 无法打印中文「报告：」而退出 1；`a481caf` 把输出改为 UTF-8 后重跑得到 #2。失败记录保留；修复前的运行不算通过。

`v0.11.0` tag 指向本文件更新后的提交；它与 `a481caf` 的差异仅为文档，未改代码。

`acceptance.yml` 的三个 job 分别验证：Linux 上跑 `--mode source`；Windows 上下载固定样本并跑 `--mode full`（含转换与 DataLoader）；Linux 容器构建、健康页与坏样本。本机没有 Docker，容器结论只以 `docker-linux` 为准。

## 3. 已知坏样本

`uv run --locked robodata verify-fixtures` 从每个 `EXPECTED.md` 读取「应命中哪条判据」，再实跑比对：

| 样本 | 期望命中 | 实测 |
| --- | --- | --- |
| frame-gap | `frame_index` | 命中 |
| length-mismatch | `row_count` | 命中 |
| missing-camera | `video_decode` | 命中 |
| nan-joint | `numeric_finite` | 命中 |
| timestamp-jump | `timestamp_interval`, `video_timestamp_mapping` | 命中 |
| video-length | `video_frame_count` | 命中 |

结果：**6/6 通过**。这是规则命中验证，不是模型训练或硬件测试。

## 4. 可复现真实样本验收（v0.11）

固定来源：上游 revision `54141acb0bd6bcb868e34b4eb328f26481d333b2` 的完整 Episode 0 和 1，9 个文件 / 19,215,391 字节。没有裁帧，没有重写视频；下载清单记录每个文件的大小、SHA-256 和 `transport_endpoint`，导入时再与仓库内冻结的 Episode 0–4 SHA-256 清单交叉校验。

```powershell
uv run --locked python scripts/verify_pipeline.py `
  --sample work/samples/so100-e0-1 `
  --episodes 0-1 `
  --mode full `
  --output work/acceptance/pipeline-full.json
```

本机实测（`--mode full`，9 个阶段）：

| 阶段 | 结果 | 耗时 |
| --- | --- | ---: |
| 来源清单校验 | 9 个文件 / 19,215,391 字节 | 0.05 s |
| 导入 + 基础质检 | 2 Episode / 1,300 帧 / 22 项覆盖 / 0 问题 | 7.97 s |
| 原始预览 | 6 个采样位置，`480×640×3` | 2.03 s |
| 审核夹具 | 2 条保留，全部批准 | 0.20 s |
| 转换 + 官方离线验证 | 1,300 帧，22/22 输出覆盖 | 48.14 s |
| 输出抽帧 | 6 个位置与解码像素逐一相等 | 1.55 s |
| 官方 DataLoader 探针 | 4 个 batch / 32 帧，形状与有限性通过 | 31.23 s |
| 交付 | ZIP 独立解包 + 官方加载 | 14.88 s |
| 独立 ZIP 校验 | 2 Episode / 1,300 帧 | 0.05 s |
| **合计** | 9 个阶段 | **约 106.1 s** |

DataLoader 探针使用独立转换环境中的 `lerobot 0.4.4` + `torch 2.7.1+cpu`，在 `HF_HUB_OFFLINE=1` 下按 `batch_size=8`、`num_workers=0`、`shuffle=False` 读取前 4 个 batch。数据集加载 0.089 秒，32 帧解码 23.373 秒（1.37 帧/秒）；同一台机器前一次 `full` 运行是 29.497 秒（1.08 帧/秒），更早的 `fast` 运行是 15.3 秒（2.09 帧/秒），说明该数字受系统负载与视频定位缓存影响很大。检查项：

- 加载帧数与 Episode 数等于输出清单；
- `action` / `observation.state` 为 `float32 [B,6]` 且全部有限；
- 图像为 `float32 [B,3,480,640]`、值域 `[0,1]`；
- `task` 是非空字符串列表；
- 不打乱时首尾动作与 `dataset[0]` / `dataset[-1]` 逐元素相等。

交付 ZIP 本次为 19,203,961 字节、SHA-256 `1f536c39…fe8a378`；整包哈希随运行时间戳变化，**不是跨运行常量**。可复现的是源文件哈希、Episode 数、帧数和各阶段结论。原始报告与样本不随仓库分发，按 [ACCEPTANCE.md](ACCEPTANCE.md) 自行生成。

这是「官方训练加载器能消费这份交付物」和「交付包可独立验证」的证据，不是训练效果、任务成功率、硬件同步或生产容量。

## 5. 历史真实数据验收

以下记录来自对应版本的验收文档，原始数据与 `work/` 产物不随仓库分发，需自行复现。

| 数据集 | 范围 | 结果 | 文档 |
| --- | --- | --- | --- |
| ALOHA Coffee | Episode 0–4，每条 1,100 帧；四路相机合计每路 5,500 帧；50 FPS；640×480 | 官方离线加载与全量数值一致性通过；输出质检 55/55；ZIP 独立解包与加载通过 | [v0.9 审计](LAUNCH_AUDIT_REPORT.md)、[时间轴 manifest](assets/timeline/manifest.json) |
| SO-100 sweet_pick | 固定子集 Episode 0–4，5 条 / 3,562 帧 | 转换、55/55 输出质检、15 个采样位置、53,034,503 字节交付 ZIP 独立验证通过 | [V0.5 验收](VALIDATION_v05.md) |
| robomimic HDF5 | 源任务 0、2 → 输出 2 条 / 116 帧 | float32 全量比对、6 个图像位置、22/22 输出质检、DataLoader(batch_size=2) 与离线加载通过；端到端 29.26 秒 | [V0.4 验收](VALIDATION_v04.md) |

边界：这些是特定机器、特定版本上的历史运行记录；不证明任意 HDF5、全部 LeRobot 版本、硬件同步或机器人执行成功率。

## 6. 规模实验

| 行数 | 重复 | 中位数 | 最小–最大 | 表格行/秒 |
| ---: | ---: | ---: | ---: | ---: |
| 1,000 | 3 | 0.012552 秒 | 0.012324–0.012738 秒 | 79,669 |
| 5,000 | 3 | 0.060519 秒 | 0.060111–0.061008 秒 | 82,619 |
| 10,000 | 3 | 0.119009 秒 | 0.118712–0.119878 秒 | 84,027 |
| 100,000 | 3 | 4.719314 秒 | 1.210178–5.166282 秒 | 21,190 |
| 1,000,000 | 1 | 61.245329 秒 | 61.245329 秒 | 16,328 |

范围：只测 `run_checks(check_video=False)` 的表格路径，合成 6 维状态/动作，视频关闭，数据构造不计时。**不含** Parquet 读取、视频解码、LeRobot 转换、打包、并发或服务开销。100,000 行档方差很大，100 万行档只跑 1 次；不据此拟合复杂度曲线或宣称生产容量。

复现与完整边界见 [2026-11-18 规模实验](benchmarks/2026-11-18-scale-experiment.md)，原始结果见 [JSON](benchmarks/quality-table-2026-11-18.json)。

## 7. 100 步训练输入冒烟

```powershell
uv run --locked python scripts\train_smoke.py --synthetic --steps 100
```

本机结果：2,048 行合成数据，初始 MSE `0.71632246`，100 步后 `0.00012241`，损失比 `0.000171`，参数全部有限，CPU 耗时 `0.003 秒`。

这**不是策略训练**：不使用 ACT / Diffusion Policy / VLA，不证明任务成功率、泛化或硬件安全。仓库不包含已验收的 ALOHA / SO-100 产物，因此没有在这里重跑真实数据集；拿到数据后可用 `--dataset-root` 复现。详见 [TRAINING_SMOKE.md](TRAINING_SMOKE.md) 与 [原始 JSON](benchmarks/train-smoke-2026-11-18.json)。

## 8. 未验证 / 不宣称

- 不宣称训练效果、策略成功率、硬件同步或真实机器人执行安全。
- 不宣称 2 Episode 验收等于 SO-100 全量 50 条 / 32,068 帧，也不等于任意 LeRobot 数据集。
- 不宣称 100 万行表格检查等于 100 万行端到端转换。
- 本机没有 Docker；Linux 容器只在 GitHub Actions 构建并检查健康页与坏样本，不做容器内真实数据转换声明，也不宣称 Linux 桌面启动。
- 不宣称公网多用户可上线；服务仍只监听 `127.0.0.1`，本机单用户。
- 真实数据历史记录不可在无原始数据时复跑；跳过项不等于通过。
- 外部复现记录为 **0 条**，见 [FEEDBACK.md](FEEDBACK.md)；陌生环境能否跑通仍未被外部验证。
