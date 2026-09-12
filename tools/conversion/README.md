# 独立转换与官方加载环境

本文同时包含 V0.3 的历史实测与 V0.4 开发中的预览、候选转换及交付验证接口。当前状态见 [V0.4 说明](../../docs/STATUS_v04.md)，完整页面操作见 [SOP](../../docs/SOP.md)。

本目录为 Windows x64、Python 3.12 CPU 环境。依赖装入 `tools/conversion/.venv`，不会向工作台主环境添加 PyTorch。锁定 LeRobot 0.4.4 输出的 LeRobotDataset v3.0；这里没有使用模型训练、CUDA 或机器人硬件。

## 安装

在代码库根目录运行：

```powershell
uv sync --project tools/conversion --frozen
```

`uv.lock` 锁定完整依赖。PyTorch 两个包从官方 CPU 源安装，其余包从 PyPI 安装。代理如有需要只通过当前进程环境变量配置，不写进此目录。

固定核心版本：`lerobot==0.4.4`、`torch==2.7.1+cpu`、`torchvision==0.22.1+cpu`、`av==15.1.0`、`h5py==3.14.0`、`numpy==2.2.6`、`datasets==4.1.1`。

固定 torchvision 0.22.1 是为了保留 `torchvision.io.VideoReader`；LeRobot 0.4.4 的 PyAV 路径调用这个接口，但 torchvision 0.24 已移除它。不要只升级 torchvision。LeRobot 0.4.4 并非最新发布版，使用它是为了固定已验证的接口组合。

## 合成兼容探针

```powershell
.\tools\conversion\.venv\Scripts\python.exe .\tools\conversion\compat_probe.py --output .\work\conversion-compat\manual-run-001
```

输出目录必须不存在。再次运行时换一个目录名，保留之前的证据。

探针包含两条**明确标记为合成的兼容性任务**，长度为 8 帧和 10 帧；图像是程序生成的颜色图案，不是机器人任务。它只测试运行环境，不计入真实 HDF5 数据转换验收。

创建进程调用官方 `LeRobotDataset.create/add_frame/save_episode/finalize`，保存 20 FPS、H.264、64×64 相机图像及状态／动作。随后另起一个离线进程，重新用官方 `LeRobotDataset` 加载，并读取两条任务的首、中、末位置及大小为 2 的 DataLoader 批次。第二条任务检查覆盖合并视频的时间偏移。H.264 为有损编码；探针对合成图像采用明确的平均误差阈值，不声称图像无损。

输出包含：

- `compatibility.json`：包版本、解释器、运行状态、两阶段耗时和检查证据。
- `create.json`、`validate.json`：创建和离线加载结果。
- `create.stdout.log`、`create.stderr.log`、`validate.stdout.log`、`validate.stderr.log`：各进程原始日志。
- `dataset/`：只用于兼容性测试的合成 LeRobot v3.0 文件。

成功命令返回 0；失败返回非零。真实数据转换仍必须单独通过源文件与输出的一致性检查及官方加载检查。

## 进程接口

工作台需要运行官方写入或官方验证时，应启动独立解释器 `tools/conversion/.venv/Scripts/python.exe`，读取 JSON 结果和进程退出码；主页面不能直接导入本环境里的 torch/lerobot。离线验证设置 `HF_HUB_OFFLINE=1`、`HF_DATASETS_OFFLINE=1`、`HF_HUB_DISABLE_TELEMETRY=1`，并显式使用 `video_backend="pyav"`、`num_workers=0`。

所有转换和官方验证子进程还必须设置 `PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8`。官方 LeRobot 0.4.4 生成视频拼接清单时没有指定文本编码，在 Windows 中文目录下，系统默认编码会使第二条任务合并视频失败。UTF-8 进程模式解决了本机实测问题；无需更改系统编码或第三方源码。

## 本机验证记录（V0.3 历史）

2026-09-10，在 Windows x64、Python 3.12.10 上安装完成；88 个运行依赖，主工作台环境中仍没有 torch 和 lerobot。

第一次探针 `work/conversion-compat/gate-001` 因上述拼接清单编码问题失败。日志保留了失败位置和原始异常。设置子进程 UTF-8 后，第二次 `work/conversion-compat/gate-002/compatibility.json` 状态为 `passed`：创建耗时 7.203 秒，离线加载验证耗时 6.469 秒；6 个首／中／末样本、两条任务的视频偏移和 batch size 2 均通过，CUDA 不可用且未使用。

这是 V0.3 阶段合成探针的实测结果。真实 HDF5 的历史阶段验证与故障验证见下文，注册和页面集成见 [V0.3 验收](../../docs/VALIDATION_v03.md)。这些结果不替代 V0.4 候选转换与交付的完整验收。

## 固定 HDF5 的阶段入口

在代码库根目录调用：

```powershell
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:HF_HUB_OFFLINE = '1'
$env:HF_DATASETS_OFFLINE = '1'
.\tools\conversion\.venv\Scripts\python.exe .\tools\conversion\worker.py --stage file_validation --input .\data\robomimic\test.hdf5 --output .\work\example-export --artifacts .\work\example-run
```

`--stage` 依次使用 `file_validation`、`hdf5_validation`、`field_mapping`、`format_write`、`official_verify`。每一阶段是独立进程，都会重新确认源 SHA256。写入时输出目录必须不存在或为空；已有数据绝不覆盖。`official_verify` 可对已有导出单独执行，结果写入新的 `--artifacts` 目录，无需重复转换。正常使用由主应用运行管理器统一启动以下工具；不要在页面处理期间另外启动转换，避免绕开本机单运行限制。

固定来源为 `robomimic/robomimic_datasets`，版本 `74fa018461f479cd9fd15b924a16103012096203`，文件 `test/test.hdf5`，45,939,724 字节，SHA256 `80dea9ca9bb99dd7b96109712fc6b46bb6fa6b87e9622b8aebbe1e4ee5fadbab`。采用 `demo_0/1/2` 的 59/58/57 帧。它们是 Panda / Lift 的仿真测试片段，不声明为完整真实机器人轨迹。

状态以 `obs/robot0_eef_pos` 的3维、`obs/robot0_eef_quat` 的4维、`obs/robot0_gripper_qpos` 的2维顺序组成9维；动作保留 `actions` 的7维，均显式映射为 float32；不补充未核实单位或改写控制含义。任务文本为来源环境名 `Lift`。相机为 `obs/agentview_image` 的84×84 RGB。20 Hz 来自源环境 `control_freq`，时间由帧序号派生。源 `data.total=9666` 与10条任务的实际531帧不一致，作为警告保留；输出统计重新计算为3条174帧。

成功退出0并写入 `RUN_DIR/steps/<stage>.json`，包含 `status="passed"`、输入路径、实际 SHA256、阶段耗时与 `details`。失败退出非零，阶段状态为 `failed`，并写入 `RUN_DIR/diagnostic.json`：中文原因、规则代码、文件、源任务、字段、从0起始的行位置、原始错误与完整调用堆栈。不会生成可用产物注册标记。

官方验收先核对 `source_manifest.json` 的输入标记及实际输出文件哈希，再离线加载官方数据集。全174帧数值与源 float32 映射、索引和派生时间进行一致性检查；每任务首／中／末9帧走官方 `dataset[index]`，并实际读取大小为2的 DataLoader。`decoded_samples.npz` 保存 `images`（uint8，N×H×W×3）、`episode_indices`、`frame_indices`、`global_indices`，供主环境逐帧读取比对。H.264 的源像素误差单独记录，不当成无损转换。

## V0.4：原始预览与业务候选集

HDF5 导入后先生成原始预览，此时尚未写入 LeRobot。独立环境入口如下；来源必须通过固定哈希，明确标注的故障副本才可使用 `--test-mode`。

```powershell
.\tools\conversion\.venv\Scripts\python.exe .\tools\conversion\preview.py --input .\data\robomimic\test.hdf5 --output .\work\preview-example
```

输出 `metadata.json`、`quality_report.json`、同内容的 `report.json`、每任务 Parquet 及图像 NPZ。NPZ 保留已存储的原始 84×84 RGB 图像，状态和动作保留可读的原始数值；缺失字段省略，不以零值补齐。每任务有独立问题、实际动作记录数、固定清单记录数及 10 项检查覆盖。NaN、缺字段和图像长度异常被定位到 HDF5 路径及行；单任务异常不阻断其他任务的预览。无法打开整个文件或不符合已验证环境配置时，本次预览失败。

预览中的帧索引及 20 Hz 时间是派生浏览坐标；索引／时间规则检查的是这些派生坐标，不证明源时钟或硬件同步。源 `data.total` 与实际动作总数不一致作为来源警告保留，不能据此忽略其他阻断性数据问题。

业务转换为每个阶段增加 `--spec <conversion_spec.json>`。主应用从通过质检并审核的候选集冻结这份文件，内容包含 `batch_id`、`content_version`、`input_fingerprint`、`snapshot_id`，以及每条任务的源编号、输出编号、记录数、完整标注和质检运行编号。工具校验快照摘要、编号唯一性和固定任务长度；不接受空候选集或重复源编号。

例如保留源任务 0、2 时，写出源 `demo_0 → episode 0`、`demo_2 → episode 1`，合计 2 条、116 帧；这是接口行为示例，整链实际验收以项目验收记录为准。`annotation.task` 写入官方训练字段 `task`，原始环境任务 `Lift` 及 `outcome/tags/notes` 等完整标注保存在 `annotations.json`，`source_manifest.json` 同时保留转换快照和源／输出编号映射。执行失败示范的人工标注不等于处理进程失败，也不覆盖原始来源语义。

不传 `--spec` 仍保留 V0.3 固定 `demo_0–2`、3 条174帧和任务文本 `Lift`。传入候选集后，统计、任务索引、train 划分、官方全量数值验证与每任务首／中／末抽样都随选择重算；多任务共用 MP4 时仍按每任务视频偏移定位。

## V0.4：解包后官方加载验证

交付模块将冻结产物打包，在新目录安全解包并检查交付清单与 SHA-256 后，使用独立离线进程调用：

```powershell
.\tools\conversion\.venv\Scripts\python.exe .\tools\conversion\worker.py --stage archive_verify --output .\work\unpacked-example\dataset --artifacts .\work\archive-verification-example
```

`archive_verify` 不需要原始 HDF5，也不重新写入数据。它检查来源清单与数据文件哈希、表格字段／维度／有限数值、任务数量、连续索引、20 Hz 派生时间和全部归入 train 的划分，然后使用官方离线 Dataset 读取每任务首／中／末帧及 `DataLoader(batch_size=2,num_workers=0)`。

结果为 `steps/archive_verify.json`。`source_numeric_consistency_rechecked=false` 表示这一步没有重新读取源 HDF5；此前转换时全量源数值一致性的证明随包保留，不能把包内表格复核描述成再次核对了源数值。只有交付清单校验、官方加载和业务版本登记均实际成功，主应用才显示可交付。

## 故障测试与实测证据

```powershell
.\tools\conversion\.venv\Scripts\python.exe .\tools\conversion\failure_checks.py --input .\data\robomimic\test.hdf5 --output .\work\conversion-failures\manual-run-001
```

测试只改明确标记的副本。`worker.py --test-mode <注入标签>` 允许检查改动副本，并在所有来源记录中写出标签与实际哈希；不带此参数的改动源首先在文件校验阶段被拒绝。该开关用于故障测试，不能把改动数据标为固定原始版本。

2026-09-10 工具级真实运行已成功写出3条174帧，官方离线验证174帧数值、9帧图像读取和 batch2，通过记录在 `work/conversion-real/artifacts/steps/official_verify.json`。此手动调试产物不注册为界面正式数据集。

2026-09-10 至11日跨午夜验证，最终 `work/conversion-failures/gate-002/failure_checks.json` 的7个真实子进程故障测试全部通过：

| 注入 | 实际失败节点与定位 |
|---|---|
| 删除字段 | HDF5结构校验；demo_1，`/data/demo_1/obs/robot0_eef_pos` |
| 未标记的改动源 | 文件校验；固定来源 SHA256 不一致 |
| NaN | HDF5结构与数值校验；demo_0，`/data/demo_0/actions`，行12、列3 |
| 缩短任务 | HDF5结构校验；demo_2，任务长度56与59/58/57配置中的57不一致 |
| 动作改为6维 | 字段映射；demo_0，`/data/demo_0/actions`，预期7维 |
| 截断HDF5 | HDF5结构校验；文件不可读 |
| 输出父路径被文件占用 | 格式写入；真实文件系统写入错误，诊断指向输出路径 |

所有故障均保留中文诊断和调用堆栈、返回非零；原始源文件 SHA256 保持不变。

## 来源

- [LeRobot 0.4.4 官方 PyPI 发布元数据](https://pypi.org/pypi/lerobot/0.4.4/json)
- [PyTorch 官方 CPU wheel 索引](https://download.pytorch.org/whl/cpu/torch/)
- [Torchvision 官方 CPU wheel 索引](https://download.pytorch.org/whl/cpu/torchvision/)
- [Torchvision 0.22.1 VideoReader 源码](https://raw.githubusercontent.com/pytorch/vision/v0.22.1/torchvision/io/video_reader.py)
