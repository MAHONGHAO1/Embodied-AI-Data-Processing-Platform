# 证据总表 · v0.15.0

本文件是 README 中版本、测试数量、规模实验和训练冒烟的统一出处。README 只指向这里，不再复制会漂移的数字。

最后更新：2026-11-19。环境：Windows 11、Python 3.12.10、Node.js 24、本地单用户。

## 1. 版本链

| 位置 | 值 | 校验 |
| --- | --- | --- |
| README 首行 | 0.15.0 | `tests/test_version_consistency.py` |
| README release 徽章 | 0.15.0 | 同上 |
| `pyproject.toml` | 0.15.0 | 同上 |
| `robodata/config.py` `APP_VERSION` | 0.15.0 | 同上 |
| `robodata/web.py` FastAPI | 0.15.0 | 同上 |
| `robodata/web.py` 健康检查 | 0.15.0 | 同上 |
| `frontend/package.json` | 0.15.0 | 同上 |
| `frontend/src/App.tsx` 版本条 | 0.15.0 | 同上 |
| `RELEASE_NOTES_v0.15.0.md` | 存在 | 同上 |

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

本机本轮（2026-11-19）：

| 套件 | 结果 |
| --- | --- |
| Python 全量（Windows） | **359 passed / 16 skipped / 13 subtests passed** |
| 已知坏样本 `verify-fixtures` | **6/6 通过** |
| 前端单测（Vitest） | **26 passed** |
| 前端生产构建 | 通过 |
| Chromium 浏览器流程 | **2 passed** |

跳过项主要来自：需要 `tools/conversion/` 独立环境或固定真实数据的用例、可选 ROS2 MCAP 依赖，以及默认不启用的 Git tag 校验（`CHECK_RELEASE_TAG=1`）。跳过不等于通过。v0.14 新增 `tests/test_adapters.py`、`tests/test_process_plan.py`、`tests/test_registry.py`；v0.15 新增 `tests/test_lerobot_v21.py`（通用 v2.1 探测、标记、子集拒绝、官方转换）、`tests/test_mcap_adapter.py`（inspection-only 与 topic 检查）与 `tests/test_registry_api.py`（只读 API）。`scripts/verify_matching_optional.py` 仍在独立转换环境中实跑 3 条转换路径 × 6 个阶段。

v0.14.0 CI 结果（2026-11-19，提交 `9e20975`）：

| 运行 | 提交 | 结果 |
| --- | --- | --- |
| [tests](https://github.com/MAHONGHAO1/Embodied-AI-Data-Processing-Platform/actions/runs/44533668246) | `9e20975` | success：前端单测、构建、Chromium 流程；Python 全量；`verify-fixtures` |
| [linux-smoke](https://github.com/MAHONGHAO1/Embodied-AI-Data-Processing-Platform/actions/runs/44533668354) | `9e20975` | success：Ubuntu 版本一致性、表格质检、坏样本校验、100 步冒烟 |
| [acceptance](https://github.com/MAHONGHAO1/Embodied-AI-Data-Processing-Platform/actions/runs/44533685581) | `9e20975` | success：`source-linux`、`full-windows`、`docker-linux` 三个 job 全部通过；`full-windows` 的 Matching Cable 复用、子集重编号、篡改回退与 `verify_matching_optional` 也通过 |

`v0.14.0` tag 指向本文件更新后的文档提交；`v0.13.0` 保持在 `e06e5c6`，`v0.12.1` 保持在 `d5668ea`，均未移动。

下面是 v0.13.0 的历史 CI 记录：

| 运行 | 提交 | 结果 |
| --- | --- | --- |
| [tests](https://github.com/MAHONGHAO1/Embodied-AI-Data-Processing-Platform/actions/runs/44513279659) | `2df17d9` | success：前端单测、构建、Chromium 流程；Python 全量；`verify-fixtures` |
| [linux-smoke](https://github.com/MAHONGHAO1/Embodied-AI-Data-Processing-Platform/actions/runs/44513279638) | `2df17d9` | success：Ubuntu 版本一致性、表格质检、坏样本校验、100 步冒烟 |
| [acceptance](https://github.com/MAHONGHAO1/Embodied-AI-Data-Processing-Platform/actions/runs/44513297289) | `2df17d9` | success：`source-linux`、`full-windows`、`docker-linux` 三个 job 全部通过；`full-windows` 的 Matching Cable 复用、子集重编号与篡改回退步骤也通过 |

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

首屏截图 `docs/assets/workbench-v0.12.1.png` 来自本机 FastAPI 服务（同源托管 `frontend/dist`），批次为固定 revision 的 2 Episode 样本，时间轴定位 `t = 8.37 s / 帧 251`，右侧检视显示来源锁定与质检结果；`docs/assets/matching-cable-v0.12.0.png` 是 2 Episode × 2 帧合成夹具，右侧显示缺失相机/深度与触觉 `not_provided`。两者都不是设计稿或合成图；Matching Cable 图只证明缺失流可见，不代表真实数据效果。`workbench-v0.11.0.png` 与 `workbench-v0.8.0.png` 保留作旧版对照。

只判断环境能否跑通时，按 [五分钟复现](REPRODUCTION.md) 走 Tier 1：不联网、不下载样本、不装 Docker；预期 `6/6` 坏样本命中。Tier 2 才下载 19.2 MB 固定样本并跑 `--mode source`。

## 5. Matching Cable 可选流（v0.12）

`scripts/verify_matching_optional.py` 生成 2 条 Episode × 2 帧、只有 `camera_0`、没有深度/触觉/可选数值字段的合成夹具，实际运行预览、完整复用转换、子集重编号和篡改视频回退（3 条转换路径 × 6 个阶段）：

```powershell
uv sync --project tools/conversion --locked
uv run --locked python scripts/verify_matching_optional.py
```

本机结果：

| 项 | 结果 |
| --- | --- |
| RGB 相机 | 只有 `observation.images.camera_0` |
| 缺失相机 | `camera_1..3` 共 3 路，写入清单 |
| 深度 | 两路均缺失，写入 `missing_depth_source_cameras`，不阻断 |
| 可选数值 | `puppet/eef`、`master/eef`、`puppet/joint_effort`、`puppet/6f` 均为 `not_provided` |
| 触觉 | `tactile.status=not_provided` |
| 阶段 | 3 条转换路径 × 6 个阶段全部通过：主转换、子集重编号、篡改视频回退 |
| 预览布局 | 2 Episode / 2 个独立 MP4；`reuse.layout=one_video_per_episode` |
| 复用 | 主转换 `strategy=preview_video_reuse`、`video_files_reused=2`、`preview_video_hashes_verified=true`、`source_numeric_consistency_rechecked=true` |
| 子集 | 源 Episode 1 → 输出 Episode 0；`index=[0,1]`、`episode_index=[0,0]`、`task_index=[0,0]` |
| 回退 | 篡改预览视频后 `strategy=legacy_reencode`，`fallback_reason` 非空，输出仍通过官方加载 |
| 官方加载 | 2 Episode / 4 帧；`output_loader_consistency_checked=true`、`source_numeric_consistency_rechecked=false` |

原始报告写在 `work/matching-optional/report.json`，不随仓库分发。字段与命令见 [MATCHING_CABLE.md](MATCHING_CABLE.md)。

v0.13.0 还用同一 2×2 合成夹具跑完整业务链（导入 → 最终质检 → 转换复用 → 官方验证 → 输出质检 → 打包/验包/归档 → 登记）：14 个节点全部 `completed`，`outcome=passed`，登记 validation 为 `source_numeric=true`、`source_numeric_scope=format_write`。这验证业务门禁接受新的复用证据，不是真实数据验收。

这不是真实 9 GB Matching Cable 数据的验收，也不证明相机硬件同步、策略训练或生产容量；它证明的是「缺深度/缺夹爪相机/缺可选字段时，平台按事实记录并继续」。

### 5.1 电信 Matching Cable 真实数据（v0.12.1 基线）

本机收到一份约 10 GB 的 Matching Cable ZIP，只读取清单并抽取小样本，未把原始数据复制进仓库：

- 7 Episode（68–74），11,879 个 HDF5，解压后 10,038,385,032 字节；
- 每帧 4 路 RGB、2 路深度、eef/关节力矩/六维力齐全；触觉字段不存在，记录 `not_provided`；
- 帧号连续；抽样 7×207 个相邻时间戳全部 49–51 ms；
- 2 Episode × 2 帧真实子集跑完导入、质检、审核、最终质检、转换、交付，官方加载通过，交付 ZIP 158,199 字节；
- 500 帧编码实测：默认 75.73 s，四线程 64.74 s，`h264_nvenc` + 四线程 63.59 s；500 帧 `format_write` 62.79 s、`official_verify` 17.71 s。

v0.12.1 没有跑全量 11,879 帧；上面的 25 分钟是按 500 帧外推，不是实测。v0.13.0 的全量结果见 5.2。数据与 `work/` 产物不随仓库分发；结构明细见本机 `work/telecom-compatibility.md`。

### 5.2 全量 7 集 / 11,879 帧（v0.13.0 历史）

本机把 9.66 GB ZIP 解压到仓库外的临时目录，源数据不进入 Git；预览和输出放在 `work/telecom-full-v013/`（`work/` 不随仓库分发）。全量源文件 11,879 个 / 10,038,385,032 字节，Episode 68–74 长度分别为 1,777 / 1,806 / 1,787 / 1,946 / 1,707 / 1,027 / 1,829 帧。

| 步骤 | 结果 |
| --- | --- |
| 全量预览（首次编码 4 路相机） | **1,338.44 s（22 分 18.44 秒）**；7 Episode / 28 个独立 MP4 / 7 个数值 parquet |
| `file_validation` / `matching_validation` / `field_mapping` | 7 Episode / 11,879 帧；`preview_video_layout=one_video_per_episode` |
| `format_write` 复用预览视频 | **206.46 s（3 分 26.46 秒）**；`strategy=preview_video_reuse`、`video_files_reused=28`、`source_numeric_consistency_rechecked=true`、`fallback_reason=null` |
| `format_write` 干净重编码 A/B | **1,072.68 s（17 分 52.68 秒）**；`--no-reuse`、`strategy=legacy_reencode`、`fallback_reason=null` |
| 复用输出数值 vs 重编码输出数值 | 11,879 行 × 12 列逐列相等 |
| 复用视频 vs 重编码视频 | 28/28 个路径相同，28/28 个 SHA-256 相同 |
| 28 个视频 SHA-256 规范汇总 | `c8fcf19f308b965580e20da1c12e288d6582796f34382905ece400fdf52f451e`（按输出 Episode、相机排序，路径 + SHA-256） |
| 输出体积 | 48 个文件 / 526,066,900 字节；28 个视频 / 516,204,117 字节；预览 526,064,271 字节 |
| 复用输出 `official_verify` | 28.51 s；7 Episode / 11,879 帧；`sample_count=84`；`batch_size=2`；`offline=true` |
| 复用输出 `archive_verify` | 27.89 s；通过 |
| 复用输出 `output_quality` | 73.09 s；7/7 Episode 通过；11,879 行；77/77 覆盖；`issue_count=0`、`load_failure_count=0`、`not_checked_count=0` |

`format_write` 复用路径没有调用视频编码器；日志里出现的 libx264 行来自同一预览首次编码或 `--no-reuse` 重编码路径，不作为复用路径的编码证据。复用时对全部 11,879 个源文件重新计算 SHA-256，再逐帧重读源 HDF5 数值与预览 parquet 对照。全量复用路径为 206.46 s，干净重编码为 1,072.68 s；复用约为重编码的 **19.2%**（5.2×）。这是本机单次测量，不是跨机器保证，也不证明硬件同步或策略效果。

全量运行在 commit `381a4f7` 上完成；随后 `2df17d9` 只修复 `file_validation` / `matching_validation` 的候选行数展示（原先把输出索引之和显示成 21，而不是 11,879），不改变 `format_write` 复用路径或输出字节。CI 在 `2df17d9` 上复跑。

### 5.3 v0.14 契约化与无头计划

500 帧 Episode 68 端到端：

```powershell
uv run --locked robodata process --plan work/telecom-500/process-plan.json `
  --business-root work/business-v14 --runs-root work/runs-v14
```

| 项 | 结果 |
| --- | --- |
| `process --plan` 全链路 | **209.67 s**（导入、质检、标注、转换复用、打包、交付、登记） |
| 同计划幂等重跑 | **0.54 s**；没有新增运行目录 |
| 转换策略 | `preview_video_reuse`；`video_files_reused=4`；`fallback_reason=null` |
| 数据卡 | `telecom-matching-cable-500` v1；`card_digest=fcd35d753afceafba0822b4961828cb6891090c7350798864baecb86712cb877`；`verify_card=(True, "ok")` |
| 交付 ZIP | 26 个文件，包含 `dataset/dataset_card.json` |
| 本地登记表 | 1 条数据集 / 1 个版本；同版本重复登记返回 `unchanged` |
| 视频统计 | 修复后 `std≈0.202`（修复前为 0）；`tools/conversion/check_video_stats.py` 输出 `patched=true` |
| Episode 68 完整 1,777 帧预览（v0.14） | **204.58 s**；4 路相机；证明统计修复没有把长 Episode 拖成不可用 |

全量 7 集 / 11,879 帧的 v0.14 转换链路（不含业务打包/登记）：

| 步骤 | 结果 |
| --- | --- |
| 全量预览（7 Episode / 4 路相机） | **1,354.58 s（22 分 34.58 秒）**；28 个独立 MP4 / 7 个数值 parquet |
| `format_write` 复用预览视频 | **184.75 s（3 分 4.75 秒）**；`strategy=preview_video_reuse`、`video_files_reused=28`、`source_numeric_consistency_rechecked=true`、`fallback_reason=null` |
| `official_verify` | **28.10 s**；7 Episode / 11,879 帧；`sample_count=84`；`batch_size=2`；`offline=true` |
| 输出 | 11,879 行 × 7 Episode；28 个视频 / 48 个文件 / 526,067,367 字节；视频 `std≈0.207`（不再是 0） |
| 28 个视频 SHA-256 规范汇总 | `2ab6e8fa3f59ac213d355f26ec8eb94d3febe208da791ddf0285bcc8dc8d7f4e` |

v0.14 **没有跑完整 7 集的 `process`（打包、交付、登记）**；这三项只在 500 帧端到端验证。全量打包/交付证据仍来自 v0.13.0（5.2），不要把 500 帧的交付结论外推到全量。

### 5.4 v0.15 有选择的广度

LeRobot v2.1 全链路（2 Episode × 5 帧合成夹具，`process --plan`）：

| 项 | 结果 |
| --- | --- |
| 导入、质检、标注、官方 v2.1→v3.0、官方加载、打包、交付、登记 | **97.00 s** |
| 同计划幂等重跑 | **0.67 s**；没有新增运行目录 |
| 转换策略 | `lerobot_v21_official_converter`；官方 `convert_dataset_v21_to_v30` |
| 数据卡 | `lerobot-v21-demo` v1；`card_digest=2905ac1f25ff64d35284292e72a2e1338724752e3a582adaf518bcb4fa15f02c` |
| 官方加载 | 2 Episode / 10 帧；源 parquet 全量行对照通过 |
| Train 交接 | `scripts/verify_train_handoff.py --archive ...`：`status=passed`、`card_valid=true`、10 行 / 2 Episode |

MCAP 检查（合成 fixture，无真实样本）：

- `adapters detect` 识别 `.mcap`，列出 `std_msgs/msg/Float64` topic 与消息计数；
- `preflight` 校验可选 `robodata_mcap.json`，引用不存在 topic 时返回 `profile_missing_topics`；
- `mcap` 标记 `inspection_only: true`；`--source auto` 跳过，`prepare` 明确拒绝；
- 没有 MCAP 转换结果，也没有真实 ROS 2 数据验收。

v0.15 没有重跑电信 11,879 帧；全量转换链路证据仍来自 v0.14（5.3），完整 7 集打包/登记仍未跑。

## 6. 历史真实数据验收

以下记录来自对应版本的验收文档，原始数据与 `work/` 产物不随仓库分发，需自行复现。

| 数据集 | 范围 | 结果 | 文档 |
| --- | --- | --- | --- |
| ALOHA Coffee | Episode 0–4，每条 1,100 帧；四路相机合计每路 5,500 帧；50 FPS；640×480 | 官方离线加载与全量数值一致性通过；输出质检 55/55；ZIP 独立解包与加载通过 | [v0.9 审计](LAUNCH_AUDIT_REPORT.md)、[时间轴 manifest](assets/timeline/manifest.json) |
| SO-100 sweet_pick | 固定子集 Episode 0–4，5 条 / 3,562 帧 | 转换、55/55 输出质检、15 个采样位置、53,034,503 字节交付 ZIP 独立验证通过 | [V0.5 验收](VALIDATION_v05.md) |
| robomimic HDF5 | 源任务 0、2 → 输出 2 条 / 116 帧 | float32 全量比对、6 个图像位置、22/22 输出质检、DataLoader(batch_size=2) 与离线加载通过；端到端 29.26 秒 | [V0.4 验收](VALIDATION_v04.md) |

边界：这些是特定机器、特定版本上的历史运行记录；不证明任意 HDF5、全部 LeRobot 版本、硬件同步或机器人执行成功率。

## 7. 规模实验

| 行数 | 重复 | 中位数 | 最小–最大 | 表格行/秒 |
| ---: | ---: | ---: | ---: | ---: |
| 1,000 | 3 | 0.012552 秒 | 0.012324–0.012738 秒 | 79,669 |
| 5,000 | 3 | 0.060519 秒 | 0.060111–0.061008 秒 | 82,619 |
| 10,000 | 3 | 0.119009 秒 | 0.118712–0.119878 秒 | 84,027 |
| 100,000 | 3 | 4.719314 秒 | 1.210178–5.166282 秒 | 21,190 |
| 1,000,000 | 1 | 61.245329 秒 | 61.245329 秒 | 16,328 |

范围：只测 `run_checks(check_video=False)` 的表格路径，合成 6 维状态/动作，视频关闭，数据构造不计时。**不含** Parquet 读取、视频解码、LeRobot 转换、打包、并发或服务开销。100,000 行档方差很大，100 万行档只跑 1 次；不据此拟合复杂度曲线或宣称生产容量。

复现与完整边界见 [2026-11-18 规模实验](benchmarks/2026-11-18-scale-experiment.md)，原始结果见 [JSON](benchmarks/quality-table-2026-11-18.json)。

## 8. 100 步训练输入冒烟

```powershell
uv run --locked python scripts\train_smoke.py --synthetic --steps 100
```

本机结果：2,048 行合成数据，初始 MSE `0.71632246`，100 步后 `0.00012241`，损失比 `0.000171`，参数全部有限，CPU 耗时 `0.003 秒`。

这**不是策略训练**：不使用 ACT / Diffusion Policy / VLA，不证明任务成功率、泛化或硬件安全。仓库不包含已验收的 ALOHA / SO-100 产物，因此没有在这里重跑真实数据集；拿到数据后可用 `--dataset-root` 复现。详见 [TRAINING_SMOKE.md](TRAINING_SMOKE.md) 与 [原始 JSON](benchmarks/train-smoke-2026-11-18.json)。

## 9. 未验证 / 不宣称

- 不宣称训练效果、策略成功率、硬件同步或真实机器人执行安全。
- 不宣称 2 Episode 验收等于 SO-100 全量 50 条 / 32,068 帧，也不等于任意 LeRobot 数据集。
- 不宣称 100 万行表格检查等于 100 万行端到端转换。
- 本机没有 Docker；Linux 容器只在 GitHub Actions 构建并检查健康页与坏样本，不做容器内真实数据转换声明，也不宣称 Linux 桌面启动。
- 不宣称公网多用户可上线；服务仍只监听 `127.0.0.1`，本机单用户。
- 真实数据历史记录不可在无原始数据时复跑；跳过项不等于通过。
- 外部复现记录为 **0 条**，见 [FEEDBACK.md](FEEDBACK.md)；陌生环境能否跑通仍未被外部验证。
