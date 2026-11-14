# RoboData Workbench

**把机器人演示数据，做成敢交给下游的训练集。**

v0.9.0 · Windows / Python 3.12 / Node.js 24 · 本地单用户

[![tests](https://github.com/MAHONGHAO1/Embodied-AI-Data-Processing-Platform/actions/workflows/tests.yml/badge.svg)](https://github.com/MAHONGHAO1/Embodied-AI-Data-Processing-Platform/actions/workflows/tests.yml) [![release](https://img.shields.io/badge/release-v0.9.0-blue.svg)](https://github.com/MAHONGHAO1/Embodied-AI-Data-Processing-Platform/releases/tag/v0.9.0) [![license](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE) [![portfolio](https://img.shields.io/badge/portfolio-live-1677ff.svg)](https://mahonghao1.github.io/)

采集现场四路相机各录一份，导出时每个批次的目录结构都不一样，交接时对方只问一句：这个包能训吗。

RoboData 就是回答这一句的工具。导入、清洗筛选、标注审核、质检、转成 LeRobot v3.0、打包，最后把包**独立验一遍**。哪一步没过，批次就停在那儿，不放给下游。

界面是 React + TypeScript，服务是 FastAPI，全跑在你自己机器上。Python 侧的数据处理、质检和验包才是业务权威实现——界面只是它的一层皮，不会替它做决定。

![v0.8.0 工作台：SO-100 单相机批次、状态曲线、底部时间轴与右侧检视面板](docs/assets/workbench-v0.8.0.png)

> 2026-11-11 从本机 FastAPI 服务（同源托管构建后的 `frontend/dist`）截取。视频是真能播的：时间轴跳到 8.37 s，视频 `currentTime` 也走到 8.37，曲线游标跟着帧动。

## 三十秒，自己验一遍

```bash
git clone https://github.com/MAHONGHAO1/Embodied-AI-Data-Processing-Platform.git
cd Embodied-AI-Data-Processing-Platform
uv sync
uv run robodata check --data-root fixtures/known-bad/nan-joint --skip-video
```

最后一条返回退出码 `3`，并在 `work/runs/*/reports/` 写出 HTML 与 JSON 报告。`fixtures/known-bad/` 里有六个故意做坏的包，每个都会被指名道姓地拦下，`EXPECTED.md` 写明应当命中哪条判据。

验你自己的数据，把 `--data-root` 换成你的目录，LeRobot v2.0 / v3.0 都认。

退出码是给 CI 用的：`0` 可交付，`2` 有加载失败，`3` 有数据异常，`1` 运行本身失败。加 `--format junit` 出测试面板，`--format sarif` 出扫描视图，`--fail-on-incomplete` 让没查全也算失败。

判据与阈值写在 [docs/RULES.md](docs/RULES.md)。随包走的验收结论长什么样，见 [docs/DATA_CONTRACT.md](docs/DATA_CONTRACT.md)。

## 三个你可能正在经历的场面

**四路相机，对不齐。** 帧数一样，时间戳也对得上，可打开一看某一路就是慢了半拍。平台把四路按相同帧索引解码后并排放，八个时刻随你拖。对不上的地方不用翻日志，眼睛就能挑出来。

**某条 episode 训练时 loss 一直炸。** 回头查数据，才发现中间几帧关节值飞了。质检做在行一级——时间戳跳变、缺帧、重复帧、异常轨迹、NaN，报告直接给帧号。

**交付包被打回，说加载不了。** 你本地明明是好的。所以在交出去之前，先用官方 LeRobot 离线加载一遍，再过 DataLoader，再算 SHA-256。被退一次的时间，够自己验十次。

## 它替你省掉什么

| 通常的做法 | 这里 |
| --- | --- |
| 每个数据集现写一个转换脚本，下个批次重写 | 流水线固定，换数据集只调范围 |
| 质检靠人盯曲线 | 规则内建，逐帧定位，报告可导出 |
| 「应该没问题」就交付 | 独立验包，SHA-256 对不上就不放行 |
| 出了问题翻日志猜 | 每次运行按阶段留请求、状态、事件和诊断 |

## 免安装，先看效果

不装任何东西也能核对：[作品集首页](https://mahonghao1.github.io/) 与 [RoboData 深度页](https://mahonghao1.github.io/robodata/) 放着当前截图和验收证据。静态页，不跑你本机的 Python 服务。

![ALOHA Coffee Episode 0 四路相机同步时间轴：8 个时刻 × 4 路相机](docs/assets/aloha-timeline-sheet.jpg)

> 四路按**相同帧索引**解码后拼接，不是录屏，也不是示意图。取自通过验收的交付包四路 H.264（各 5,500 帧 / 50 FPS / 640×480），采样帧 0、150、300…1050，参数见 [manifest.json](docs/assets/timeline/manifest.json)。**边界**：静态帧序列，用来核对同一时刻四路都有画面、帧与时间映射一致；不代表相机硬件同步，也不等于工作台实时解码。

![交付包逐路解码抽样：5 条 Episode × 4 路相机](docs/assets/aloha-episodes-grid.jpg)

| 界面视角 | 截图 | 能核对什么 |
| --- | --- | --- |
| 数据概览 | [查看](docs/assets/overview.png) | 来源版本、任务清单、预期帧数与样本说明 |
| Episode 逐帧检查 | [查看](docs/assets/episode-browser.png) | 目标时间与视频时间对齐、关节原始值、曲线游标 |
| 质检报告 | [查看](docs/assets/quality-page.png) | 逐条检查状态、「已执行但未发现问题」结论与报告导出 |
| 样本抽样网格 | [查看](docs/assets/sample-contact-sheet.jpg) | SO-100 四条 Episode × 三个时刻的真实解码画面 |
| 旧版界面（历史 v0.4.0） | [查看](docs/assets/v04/01_overview.png) | 早期 Streamlit 页面，仅作迭代对照 |

图片都是本机实际运行截图或真实视频抽帧，没补画、没修饰；历史界面单独标了版本。

## 到底做到了什么程度

下面每一条都能点进去核对，不合格的地方也一并写出来。

| 主张 | 证据 |
| --- | --- |
| 新工作台界面 | React + TypeScript 页面、FastAPI 同源托管、批次/运行 API；Node 24 构建、单测与 Chromium 浏览器流程通过 |
| SO-100 主流程 | 固定子集 5 条 / 3,562 帧通过工作台转换与交付验收，见 [v0.5 验收](docs/VALIDATION_v05.md) |
| HDF5 转换与交付 | 116 帧与源 float32 映射一致，官方离线读取与交付包独立验证通过，见 [v0.4 验收](docs/VALIDATION_v04.md) |
| 多相机处理 | ALOHA Coffee Episode 0–4 实测：5,500 帧、四路 640×480、50 FPS，各相机帧数与时间映射检查通过 |
| 转换与交付 | 官方离线加载与 5,500 帧数值一致性检查通过；输出质检 55/55 完成；ZIP 独立解包与加载通过 |
| 失败门禁 | 自动测试覆盖缺相机、时间映射错误、未知结构、失败结果拒绝登记；真实数据转换失败与 ZIP 篡改**未做**故障注入 |
| 运行可追溯 | 业务运行按阶段保存请求、状态、事件和诊断；真实数据的通过范围以对应版本验收记录为准 |

历史 HDF5 验收：14 个节点全部完成，源任务 0、2 → 输出 2 条 / 116 帧，端到端 29.26 秒（[V0.4](docs/VALIDATION_v04.md)）。SO-100 的 UI 开通、5 条 / 3,562 帧转换与交付证据在 [V0.5](docs/VALIDATION_v05.md)。测试证据不代替真实数据运行结果。

## 从哪里开始

- **只想看效果**：打开[作品集首页](https://mahonghao1.github.io/)或[项目深度页](https://mahonghao1.github.io/robodata/)，拖时间轴、翻九个界面视角
- **想跑起来**：装好 Node.js 24 LTS，双击 `首次安装.cmd`，再用桌面快捷方式启动 —— 见[安装与启动](#安装与启动)
- **想读实现**：[数据字典](docs/data_dictionary.md)、[V0.5 状态](docs/STATUS_v05.md)、[导航验收](docs/VALIDATION_navigation.md)
- **想看迭代**：[v0.9.0 发布说明](RELEASE_NOTES_v0.9.0.md)、[上线级审计报告](docs/LAUNCH_AUDIT_REPORT.md)、[变更记录](CHANGELOG.md)
- **想照着操作**：[标准操作 SOP](docs/SOP.md)，[HTML 手册](docs/SOP.html) 可离线打开

默认服务地址 [http://127.0.0.1:8765/](http://127.0.0.1:8765/)，FastAPI 在本机托管构建后的 React 页面和 API。GitHub Pages 不提供这个 Python 服务；旧版 Streamlit 用 `启动Streamlit旧版.cmd` 单独跑在 8501。

## 安装与启动

### 方式一：一键安装（推荐）

先安装 [Node.js 24 LTS](https://nodejs.org/en/download)，再把仓库克隆或下载到本机，**双击根目录的 `首次安装.cmd`**。向导会检查 Node 版本并完成：

1. 检查并（必要时）安装 uv —— Python 环境管理器，不污染系统 Python
2. 准备 Python 3.12（由 uv 托管）
3. 安装主环境依赖（约 400 MB）
4. 下载公开演示数据（约 46 MB）
5. **在桌面创建「RoboData 工作台」快捷方式**

完成后双击桌面快捷方式启动服务，并在浏览器打开提示中的 `http://127.0.0.1:8765`。首次安装需要联网。

> 完整 HDF5 转换链路（格式转换节点）另需约 2.2 GB（含 CPU 版 PyTorch）。
> 向导会提示可选命令；不安装也不影响浏览、质检与标注功能。

### 方式二：手动安装

需要 Python 3.12、Node.js 24 LTS 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)。获取源码并解压到项目目录，或从实际可访问的仓库克隆源码；在仓库根目录执行 PowerShell 命令：

```powershell
# 安装锁定的 Python 与前端依赖，构建 React 页面
uv sync --locked
Push-Location frontend
npm ci
npm run build
Pop-Location

# 体验 HDF5 批次全流程：预览和转换均需要独立环境
uv sync --project tools/conversion --locked
uv run --locked robodata fetch-hdf5

# 启动本地 API 与页面托管
.\启动工作台.cmd
```

HDF5 样本约 46 MB；独立环境还需下载 CPU PyTorch 等依赖。启动脚本会使用 `npm ci`、`npm run build`（仅当缺少前端依赖或构建产物时）及 `uv sync --locked`，然后由 FastAPI 同源提供页面和 API。服务只监听 `127.0.0.1`。

SO-100 浏览与故障演示另需执行 `uv run --locked robodata fetch-sample`，下载默认子集（5 条 / 3,562 帧）约 53 MB；执行 `获取全量数据.cmd` 可补齐全量 50 条 / 32,068 帧（约 535 MB）。下载器固定版本与文件清单，支持复用、重试和完整性检查；代理只通过下载参数或当前进程变量配置。

v0.6.0 支持固定来源 [ALOHA Coffee](https://huggingface.co/datasets/lerobot/aloha_static_coffee)，代码配置限定在 revision `b144896feb1f37398a862927b22cd3abdf005a6b`、Episode 0–4 和四路相机。固定 revision 的 [LeRobot 元数据](https://huggingface.co/datasets/lerobot/aloha_static_coffee/resolve/b144896feb1f37398a862927b22cd3abdf005a6b/meta/info.json)声明全量 50 条任务、55,000 帧；本机实测的 Episode 0–4 为每条 1,100 帧、共 5,500 帧。四路来源视频经官方 writer 转换为 H.264，并分别检查文件、解码、帧数和记录时间映射；本次结果不证明相机硬件同步。复现步骤与验收边界见 [状态文档](docs/STATUS_v0.6.0.md)。

## 新工作台界面

当前 React 工作台把同一业务流程组织为四类界面：

| 界面 | 主要内容 |
| --- | --- |
| 批次总览 | 来源、episode 数量、当前阶段与候选状态；可导入固定样例或本机数据 |
| 数据浏览 | episode 筛选、视频预览、相机来源与逐路检查、帧与时间映射，以及筛选标注审核 |
| 运行诊断 | 后台运行状态、阶段节点、事件流、错误和登记结果 |
| 交付核验 | 当前版本的转换产物、官方加载/质检证据、打包状态与独立验包 |

![历史界面（v0.6.0）：React 工作台批次总览，本机 SO-100 五条任务批次](docs/assets/workbench-v0.6.0.png)

> 上方为 v0.6.0 历史界面，仅作迭代对照；当前界面以本页顶部与「v0.8.0 · 16:9 固定工作台」的截图为准。ALOHA 四路相机的静态抽帧证据见「先看什么」章节。

业务层仍分别记录导入、清洗、筛选、标注、审核、质检、转换和交付阶段。质量检查通过的任务只是候选；只有当前审核、官方加载、输出检查、打包和独立验包全部通过，才登记为可交付。重选或重新标注会使下游审核与产物失效，原始数值不被自动修复。

```mermaid
flowchart LR
  UI[React + TypeScript 工作台] -->|同源 /api| API[FastAPI · 127.0.0.1:8765]
  API --> PY[Python 业务与数据处理]
  PY --> SRC[HDF5 / SO-100 / 固定 ALOHA 适配]
  PY --> RUN[运行记录与阶段诊断]
  PY --> QC[质量与元数据验证]
  PY --> CONV[官方 LeRobot 转换、打包、独立验包]
  SRC --> QC --> CONV
  RUN --> UI
  CONV --> UI
  OLD[旧版 Streamlit 入口] -. 回退 .-> PY
```

旧版 Streamlit 页面仍保留作回退入口：双击 `启动Streamlit旧版.cmd`，或运行 `uv run --locked streamlit run app.py`。`docs/assets/v04/` 与已归档的 [v0.4.0 验收报告](docs/validation-report-v0.4.0.html)中的逐节点截图属于旧界面，不代表当前 React 页面。

**问题边界：** 机器人执行失败属于任务标注；NaN、缺字段等属于数据质量问题；文件读取、编码或超时属于程序运行问题。记录失败轨迹并不等于宣称机器人成功率或模型训练效果。

## 数据配置与范围

| 来源 | 固定范围 | 字段与处理范围 |
| --- | --- | --- |
| [jmrog/so100_sweet_pick](https://huggingface.co/datasets/jmrog/so100_sweet_pick) | revision `54141acb0bd6bcb868e34b4eb328f26481d333b2`；默认启用 Episode 0–4（3,562 帧）。 | LeRobot v2.0 真机数据，状态／动作各 6 维，laptop 单相机 640×480、30 Hz、AV1 编码；支持 UI 导入、筛选、标注审核、最终质检、v3.0 转换和打包验包。 |
| [robomimic/robomimic_datasets](https://huggingface.co/datasets/robomimic/robomimic_datasets) | revision `74fa018461f479cd9fd15b924a16103012096203`；`test/test.hdf5` 共 demo_0–9（531 帧），默认启用 demo_0–2（174 帧）。 | Panda / Lift 仿真测试片段，状态 9 维、动作 7 维、agentview 84×84 RGB；业务候选子集可转换和打包。 |
| [lerobot/aloha_static_coffee](https://huggingface.co/datasets/lerobot/aloha_static_coffee) | 固定 revision `b144896feb1f37398a862927b22cd3abdf005a6b`；来源元数据声明全量 50 条／55,000 帧和 50 FPS；本机实测 Episode 0–4 共 5,500 帧、四路相机。 | ALOHA 公开示例；逐相机检查文件、解码、帧数和时间映射，不推断相机硬件同步。v0.6.0 的实际转换与交付范围见 [验收记录](docs/VALIDATION_v0.6.0.md)。 |

HDF5 本地上传仍受固定配置约束，不是通用 HDF5 导入器。测试副本必须显式标记；标记不会让结构或质量检查自动通过。

**两种来源都能跑完整条链路**（导入 → 质检 → 筛选 → 标注 → 最终质检 → 转换 → 交付 → 独立验证），转换链路上各有各的适配器：

- **HDF5 → v3.0**：由 `tools/conversion/worker.py` 直接读取 HDF5 的 demo 结构，映射字段后用官方 `LeRobotDataset.create()` 写入。
- **SO-100 v2.0 → v3.0**：源是 LeRobot v2.0，缺 v2.1 要求的 `meta/episodes_stats.jsonl`，因此先补齐 per-episode 统计（数值读 parquet，视频抽帧后走官方 `sample_images`），再逐阶段调用官方转换函数。**官方 `convert_dataset()` 末尾会 `rmtree` 并原地替换源数据集，平台只调用其阶段函数，源数据始终只读。**

状态与动作保留来源语义，不补充未经核对的单位或控制含义。HDF5 时间由 `frame_index/20` 派生；H.264 视频有损。SO-100 视频为 AV1 有损，v2.1→v3.0 是 remux 不重编码，因此不对逐像素恒等作断言，而是核对帧数与解码样本哈希。来源全局统计 9,666 与文件实际 531 帧不一致，作为警告保留，输出按候选任务重新计数。详见 [数据字典](docs/data_dictionary.md) 和 [来源与许可](docs/DATA_NOTICE.md)。

### 调整 HDF5 数据范围

固定样本 `test.hdf5` 实际包含 **10 条 demo（demo_0–9，合计 531 帧）**，默认只启用
demo_0–2（174 帧，与 V0.4 验收一致）。通过 `ROBODATA_HDF5_EPISODES` 可调整范围，
无需修改代码：

```powershell
$env:ROBODATA_HDF5_EPISODES = "0-9"       # 全量 10 条 / 531 帧
$env:ROBODATA_HDF5_EPISODES = "0-2,5,7-9" # 混合区间与单点
.\启动工作台.cmd
```

也可直接双击 **启动工作台全量数据.cmd**。范围变化会改变输入指纹，已有批次需要
重新导入，这是预期行为。越界或非法范围直接失败，不静默降级；校验使用冻结的帧数
清单（`tools/conversion/worker.py` 的 `_ALL_DEMO_LENGTHS`），不从被检文件自证。

> **启动入口命名约束：** 桌面启动器会枚举项目根目录的 `*.cmd` 并执行其中**第一个**，
> 因此 `启动工作台.cmd` 必须保持排序最前。新增启动入口时，文件名不要以排在
> `.`（U+002E）之前的字符开头（如 `-`、`!`、`#`），否则会抢占默认启动，
> 现象是桌面快捷方式"打不开"或启动异常。另外，根目录的 `.cmd` 应保持纯 ASCII
> 内容：`cmd.exe` 按当前代码页解码批处理文件，UTF-8 中文在默认 GBK 代码页下会乱码。

**全量范围已通过端到端验收**（2026-09-12，证据见 `work/full-range-acceptance.json`，
由 `scripts/verify_full_range.py` 生成）：在 `ROBODATA_HDF5_EPISODES=0-9` 下，
10 条 / 531 帧走完导入 → 质检 → 筛选确认 → 标注审核 → 格式转换 → 打包交付 → 独立验证，
输入质检 100/100 项、输出质检 110/110 项、0 数据问题，官方库离线加载并对 531 帧
数值与 30 个采样位置像素比对一致，交付包 531 帧 / 10 条整包路径可移植、独立加载通过。

### 调整 SO-100 数据范围

SO-100 全量 **50 条 / 32,068 帧（约 535 MB）** 已在本地备齐（可执行 `获取全量数据.cmd`
或 `uv run robodata fetch-sample --full`），默认只启用 Episode 0–4（3,562 帧，约 53 MB），
保证日常演示秒开。通过 `ROBODATA_SO100_EPISODES` 可调整：

```powershell
$env:ROBODATA_SO100_EPISODES = "0-49"   # 全量 50 条 / 32,068 帧
$env:ROBODATA_SO100_EPISODES = "0-9"    # 前 10 条
.\启动工作台.cmd
```

**SO-100 历史命令行验收**（2026-09-13，证据见 `work/so100-acceptance.json`，
由 `scripts/verify_so100.py` 生成）：默认 5 条 / 3,562 帧走完导入 → 质检 → 筛选确认
→ 标注审核 → 最终质检 → 格式转换 → 打包交付 → 独立验证，输入质检 55/55 项、
输出质检 55/55 项、0 数据问题，官方库离线加载并对 3,562 帧数值与 15 个采样位置
像素比对一致，交付包 53 MB 路径可移植、解包后独立加载通过。

转换链路额外验证两点：v2.0 → v2.1 阶段为 5 条任务补齐了 per-episode 统计；
源目录在转换前后 SHA-256 不变（平台不原地替换）。

若运行环境的临时文件批量清理被安全策略拦截（表现为转换节点报"工作进程意外退出，
退出码 1"，日志出现 `SAFE_DELETE_BULK_CONFIRM_REQUIRED`），追加设置
`ROBODATA_STREAMING_ENCODING=1`：LeRobot 改用流式编码直接把帧写入视频，
不再落临时图片目录，因此不产生批量删除。默认关闭以保持既有编码路径。

当前范围不包括通用 MCAP 接入、逐帧轨迹修复、多相机硬件同步、模型训练和多人权限系统。基础规则与官方加载通过，不代表训练效果、物理安全或任务成功率。

## 定位异常与查看证据

排查路径：**批次／运行编号 → 失败节点 → 中文原因和文件证据 → 任务、字段、行位置或堆栈 → 处理后启动新运行**。

“运行记录”保存节点状态、耗时和程序日志；“业务审计历史”保存筛选、标注与审核决定。即使最终报告没有生成，已写入的运行日志仍可查看。每次显式执行创建新运行；刷新读取同一记录。

`work/runs/<run_id>/` 保存 `request.json`、`state.json`、`events.jsonl`、节点标准输出／错误日志和阶段证据。批次快照、管理副本位于 `work/business/`。候选内容变化、验证未完成或文件指纹不匹配时，不作为当前有效交付提供下载。

原有故障演示可展开侧栏“高级工具”，在样本页面使用“异常演示副本”“视频缺失演示副本”和“运行超时演示”。具体注入位置及结果含义见 [SOP](docs/SOP.md)。

## 技术上怎么落地

主环境负责页面、轻量读取、质检、后台协调及业务状态；`tools/conversion/` 使用独立 Python 进程完成 HDF5 解析、官方写入与离线加载。两个环境分别锁定依赖。转换组合与维护说明见 [独立环境文档](tools/conversion/README.md)。

```powershell
uv run robodata batches
uv run robodata runs
uv run robodata show-run <run_id> --events
uv run robodata show-flow <batch_id>
# 审核完成后的最终检查；通过后自动完成转换和交付
uv run robodata final-check-batch <batch_id> --revision <revision>
# 对下载 ZIP 在新目录独立校验并官方离线读取
uv run robodata verify-delivery --archive <zip_path>
uv run --locked python -m pytest -q
```

命令中的 `<run_id>`、`<batch_id>`、`<revision>`、`<zip_path>` 需替换为实际编号、修订或路径。更多导入、批次检查和交付验证命令见 SOP。

CI 在 `windows-latest` 上执行同一组测试（[`.github/workflows/tests.yml`](.github/workflows/tests.yml)）；需要独立转换环境与固定样本的用例在缺失时自动跳过，其余用例不依赖下载数据。

[V0.1](docs/validation.md)、[故障样本验证](docs/failure_validation.md)、[V0.2](docs/VALIDATION_v02.md)、[V0.3](docs/VALIDATION_v03.md) 保留各阶段的实测范围与结果。V0.3 的 91 项测试和固定三条转换结果属于历史基线。V0.4 真实自动链保留源任务 0、2，输出 2 条／116 帧；全量 float32 数值比对、6 个图像位置与官方读取一致、22/22 项输出质检及交付包独立加载通过。异常副本另验证任务隔离、剩余 57 帧转换和篡改 ZIP 拒绝。最终测试与实际浏览器操作按 [V0.4 验收](docs/VALIDATION_v04.md) 的范围阅读，不以界面测试代替真实数据验证。

原始数据、管理副本、运行日志、报告和交付 ZIP 默认保存在被 Git 忽略的 `data/`、`work/`、`reports/`。历史证据中引用的本地产物需自行复现，不随源码自动下载。

历史截图（仅作迭代对照，已不作为 README 首屏）`docs/assets/workbench-v0.6.0-aloha.png` 来自通过验收的真实 ALOHA Episode 0 四路相机浏览；`docs/assets/workbench-v0.6.0.png` 展示 SO-100 工作台界面，不把它描述为 ALOHA 转换证据。v0.8.0 的 `workbench-v0.8.0.png` 与 `fund-v0.3.0.png` 来自本机真实 FastAPI 服务；`workbench-v0.8.0-aloha.png` 是 16:9 布局验证截图，镜头画面取自真实交付帧但批次元数据为构造数据，不作为 ALOHA 转换或业务状态证据。

## 为什么这么设计

关键决策都放弃了什么，写在这里而不是藏起来。

| 决策 | 选择 | 放弃的 |
| --- | --- | --- |
| 数据列式存储 | Parquet | 放弃 MCAP 的流式写入与 ROS 生态直连；换取与 LeRobot 官方格式一致和 Pandas 生态 |
| 运行环境 | 主环境与 `tools/conversion/` 双环境隔离 | 放弃单环境部署的便利；换取 torch / h5py 重依赖不污染页面环境 |
| 视频编码 | H.264 | 放弃无损；换取体积与官方加载器兼容性，因此文档明确标注图片为有损 |
| 时间派生 | HDF5 按 `frame_index / 20` | 源数据无时间戳字段，放弃精确同步；换取可复现的确定性派生 |
| 清洗策略 | 任务级隔离，不自动删帧、插值或修轨迹 | 放弃自动修复；换取原始数据不被静默改动，每条隔离必须填原因 |
| 失败建模 | 任务失败 / 数据异常 / 程序失败 三类分开 | 放弃单一成功失败判断；换取完整失败轨迹仍可作为有效数据保留 |
| 产物失效 | 内容版本或哈希变化即标记历史 | 放弃"永远可下载"的便利；换取不会误把过期产物当当前有效交付 |

## 版本记录

每一版改了什么、为什么改，都写在这里。想看最新一版的结论，往上翻到「到底做到了什么程度」。

## v0.9.0 · 状态回退修复与上线级审计

v0.9.0 不新增界面，而是把「提交审核后离开一小时，回来看起来全部回退到导入阶段」这条真实缺陷查到底并修完，同时补上防止复发的护栏。

| 层 | 根因 | 本版处置 |
| --- | --- | --- |
| 后端 | 读取运行记录被实现成带业务副作用：`GET` / SSE 读路径会重放质检登记，把审核打回草稿、清掉清洗确认并使产物失效 | 读路径改为**纯只读**（含派生视图），恢复登记只能走显式 `POST /api/runs/{id}/recover` |
| SSE | 终态不下发结束事件，浏览器 `EventSource` 无限自动重连，把危险读路径放大 | 终态 / 错误 / 超时下发 `event: done` + 心跳 + 超时，客户端终态主动关闭 |
| 前端 | 阶段进度硬编码，业务状态一失效就只剩「导入」 | 进度由 `deriveStages(batch, runs)` 按真实审核与产物推导 |
| 前端 | 刷新或长时间离开丢失上下文 | URL + 本地会话双通道恢复，回前台主动对账 |
| 入口 | 旧 Streamlit 界面仍在每次重绘时自动回写 | 同步改为纯只读 + 提示，破坏性恢复需两步确认 |
| 交互 | 破坏性操作缺少说明，且后端 409 时前端只能反复点同一个按钮 | 质检重跑 / 恢复质检登记 / 修改筛选决定均二次确认；新增可操作的「确认并重试」，先取服务器真相再带确认标记重发 |
| 提示 | 用户自己的写操作被误报成「后台处理改了批次」 | 版本基准随写操作同步，提示改为中性归因，并明确说明产物失效 |

验证：后端全量 **290 项**（289 通过；`test_concurrent_starters_cannot_create_two_active_runs` 在全量并发下偶发失败，单独跑三次稳定通过，互斥实现是 OS 字节锁加排他创建，非本次引入），13 项子测试通过；跳过项随环境变化。前端 **26 项单测** 与生产构建通过；真实 `uvicorn` 下终态 SSE 在 8 ms 内结束闭环。完整根因链、逐条修复位置与剩余风险见 [上线级审计报告](docs/LAUNCH_AUDIT_REPORT.md) 与 [v0.9.0 发布说明](RELEASE_NOTES_v0.9.0.md)。

> 运行边界不变：本机单用户、仅 `127.0.0.1` loopback。报告第 8 节列出的产品语义决策项与公网化前置项未在本版处理，因此本版不宣称「公网多用户可上线」。


## v0.8.0 · 16:9 固定工作台

v0.8.0 把两套界面从「窄栏长页」重做为**一屏闭环的固定工作台**：顶栏 48px、侧栏 216px、主区自适应、检视面板 320px、具身底部时间轴 96px（基金为 34px 状态条）。整页不再滚动，只有主区与检视面板局部滚动。

![v0.8.0 具身工作台：固定 Shell、四路相机与曲线共用时间轴、右侧检视面板](docs/assets/workbench-v0.8.0-aloha.png)

> 四路相机视图为界面布局验证截图：镜头画面取自已验收 ALOHA Coffee 交付包的真实帧，批次与相机元数据为演示构造数据，用于核对 16:9 布局与时间轴联动。SO-100 的真实服务截图见本页顶部。

| 改动 | 效果 |
| --- | --- |
| 统一 Shell | 导航从顶栏迁入侧栏；顶栏只放品牌、面包屑、状态与检视开关 |
| 局部滚动 | 主区与检视面板各自滚动，KPI、时间轴、状态条始终可见 |
| 时间轴联动 | 播放／逐帧／5 个百分比书签控制真实视频；曲线增加当前帧游标；空格、← →、1–4、Esc 快捷键 |
| 检视面板 | 当前帧、任务列表、逐路检查、质检与候选、来源锁定集中在一列 |
| 相机聚焦 | 点击任一相机放大，退出后 2×2 顺序不变；检视面板可收起 |
| 基金侧 | 全宽表格 32px 行高 + 固定表头，10 行同屏；点行更新右侧检视，不跳页 |

基金侧同样换成固定 Shell，行高与信息密度按 IBM Carbon DataTable 的 32px 档设置：

![v0.3.0 基金工作台：全宽持仓表、右侧检视与底部口径状态条](docs/assets/fund-v0.3.0.png)

> 从本机 FastAPI 离线演示服务生成。近一年收益保持「不可用」，不以零值替代；右侧显示来源回退状态与计算口径。

**边界**：本次是纯前端布局重构，API 契约、后端、数据口径与验收范围均未改变；仅本机运行，不提供公网交互实例。面板拖拽与布局保存（P2）未实现。

业务链路包含**上传／导入 → 清洗 → 筛选 → 标注审核 → 质检 → 格式转换 → 打包与独立验包**。工作台分为批次总览、episode 与相机浏览、运行诊断、交付核验四类界面；每次处理保留阶段状态、日志和证据。只有通过官方 LeRobot 加载、输出质检、打包和独立验包的产物才可交付。

> **本地边界：** 新工作台只监听 `127.0.0.1`，不提供公网交互服务。仓库的 [在线作品集展示页](https://mahonghao1.github.io/) 与 [RoboData 深度页](https://mahonghao1.github.io/robodata/) 汇总当前截图、验收证据与能力边界；它是静态页，不提供本机的 Python 交互服务。

## 许可

[MIT](LICENSE)。数据来自公开数据集，来源与许可见 [DATA_NOTICE.md](docs/DATA_NOTICE.md)。

## English

**Robot demonstration data, turned into a training set you dare hand off.**

Four cameras record separately, every batch exports with a different directory layout, and the receiving team asks one question: can this package be trained on. RoboData answers it on your own machine — import, clean and filter, review annotations, run per-frame QA, convert to LeRobot v3.0, package, then verify the package independently. A batch that fails any step does not move downstream.

React + TypeScript for the UI, FastAPI for the loopback-only bridge, Python for the data work. The Python side stays authoritative for processing, QA and package verification; the UI never overrides it.

Everything here is checkable, and the gaps are written down too. ALOHA Coffee Episodes 0–4 ran end to end at 5,500 frames, four 640×480 streams at 50 FPS, with per-camera frame counts and time mapping verified. The SO-100 subset converted and passed delivery acceptance at 5 episodes / 3,562 frames. QA completed 55/55, and the delivery ZIP was unpacked and loaded through the official LeRobot loader with SHA-256 verified against tampering. The automated validation uses an explicitly **simulated** review gate — it does not imply human review, robot execution, or training results, and no fault injection was performed for real-data conversion failures or ZIP tampering.

The static [portfolio page](https://mahonghao1.github.io/robodata/) adds a draggable four-camera timeline built from real decoded frames plus nine verified UI views. It is a static frame sequence for checking that all four streams have a frame at the same instant — not live playback, and not a claim of hardware synchronisation.
