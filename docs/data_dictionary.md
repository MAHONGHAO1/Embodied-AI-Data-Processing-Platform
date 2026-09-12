# 数据字典与语义边界：V0.3 数据配置、V0.4 业务映射

本文覆盖两个固定数据配置及 V0.4 新增的批次、筛选、标注审核和交付字段。SO-100 LeRobot v2.0 浏览子集与固定 robomimic HDF5 → LeRobot v3.0 转换已有 V0.3 验收；V0.4 业务流程仍在开发，见 [版本状态](STATUS_v04.md)。格式版本、来源版本、应用版本、规则版本分别记录。

## SO-100 原始公开样本

来源：[jmrog/so100_sweet_pick](https://huggingface.co/datasets/jmrog/so100_sweet_pick)，固定版本 `54141acb0bd6bcb868e34b4eb328f26481d333b2`。上游声明Apache-2.0、SO-100机器人，任务文字 `red sweet pick`。

读取Episode0–4，长度503、797、497、898、867，共3,562帧；仅下载laptop主相机。数据组织为每任务Parquet、每任务MP4、`meta/info.json`、`episodes.jsonl`、`tasks.jsonl`等元数据。

| 字段 | 类型／用途 |
| --- | --- |
| episode_index | 任务编号，确认记录属于预期任务。 |
| frame_index | 从0开始的任务内索引，检查连续性。 |
| index | 来源全局索引，保留来源值。 |
| timestamp | float32时间；检查单调性、间隔和视频对应，硬件时钟来源未说明。 |
| observation.state | float32[6]，原始状态值。 |
| action | float32[6]，原始动作值。 |
| task_index | 关联 `meta/tasks.jsonl` 的任务文本。 |
| observation.images.laptop | 主相机视频；RGB、640×480、AV1、名义30 FPS。 |

状态与动作的原始分量顺序均为：`main_shoulder_pan`、`main_shoulder_lift`、`main_elbow_flex`、`main_wrist_flex`、`main_wrist_roll`、`main_gripper`。

单位、零点、标定、归一化范围、绝对量／增量与控制模式未充分说明。不标注rad或degree，不从数据分布推导物理限位，也不把状态直接当动作真值。30 FPS及接近 `frame_index/30` 的时间序列不证明硬件同步。

下载的是浏览质检子集，源元数据仍包含完整数据集统计；本项目不直接声称该SO-100子集能用于训练。

## robomimic HDF5 固定输入

来源：[robomimic/robomimic_datasets](https://huggingface.co/datasets/robomimic/robomimic_datasets)，版本 `74fa018461f479cd9fd15b924a16103012096203`，文件 `test/test.hdf5`，上游数据集声明MIT。

- 大小：45,939,724字节。
- SHA256：`80dea9ca9bb99dd7b96109712fc6b46bb6fa6b87e9622b8aebbe1e4ee5fadbab`。
- 来源环境：Panda／Lift仿真，`env_args.env_kwargs.control_freq=20`。
- 选择：`/data/demo_0` 59帧、`demo_1` 58帧、`demo_2` 57帧，共174帧。
- 定位：文件 → `/data/<demo>/<field>` → 从0开始的行／列。

源文件含10条demo，按actions实际合计531帧，但 `/data` 的 `total` 属性为9666。此不一致作为来源统计警告保留，不修改原始文件；输出基于所选3条174帧生成新元数据和统计。它们是公开仿真测试片段，不据文件名或长度断言为完整任务轨迹。

| HDF5源字段（每条demo内） | 来源数组 | 输出字段与处理 |
| --- | --- | --- |
| `obs/robot0_eef_pos` | float64[T,3] | 组成 `observation.state` 第0–2维，float32。 |
| `obs/robot0_eef_quat` | float64[T,4] | 组成状态第3–6维，保留源四元数顺序，float32。 |
| `obs/robot0_gripper_qpos` | float64[T,2] | 组成状态第7–8维，float32。 |
| `actions` | float64[T,7] | 输出 `action`，float32，保留来源控制表示与分量顺序。 |
| `obs/agentview_image` | uint8[T,84,84,3] | 输出 `observation.images.agentview`，RGB、H.264 MP4有损编码。 |
| 来源环境名 `Lift` | 字符串 | V0.3 默认输出 `Lift`；V0.4 审核候选集采用 `annotation.task`，原始 `Lift` 随附保留。 |
| 帧序号与control_freq | 20 Hz | 输出 `timestamp=frame_index/20`，明确标记为派生时间。 |

状态名称使用 `robot0_eef_pos[0..2]`、`robot0_eef_quat[0..3]`、`robot0_gripper_qpos[0..1]`，动作名称使用 `actions[0..6]`。没有把末端控制表示转换为关节目标，没有重新排列四元数、改坐标系或补充未经核对的单位。

## V0.4 转换前的原始预览

主页面通过独立 HDF5 解析进程读取管理副本，使用 Parquet 表和 NPZ 图像缓存浏览；这一步不写 LeRobot。NPZ 中是原始已存储的 `uint8 RGB` 图像。状态只在组成字段全部可读且维度相符时拼接，动作字段可读才写入预览表；原始缺字段不补零、不插值、不自动删帧。NaN 可以在故障预览里保留，并同时保存其字段／行定位证据，无法直接进入正式转换候选集。

| 预览元数据 | 含义 |
| --- | --- |
| `kind=hdf5_preview` | 原始预览缓存，不是 LeRobot 格式交付。 |
| `source_statistics` | 整个 HDF5 的声明统计、按动作数组重数的实际统计及 demo 数。 |
| 每任务 `row_count` / `expected_length` | 固定导入清单的记录数，用于批次任务范围和清洗对账。 |
| 每任务 `actual_row_count` | 本次实际读到的动作记录数；动作缺失时用可读字段长度作预览范围，不能视为动作检查通过。 |
| `table_path` / `images_path` | 相对于预览目录的缓存位置；不可读时为空。 |
| `issues` / `coverage` / `status` | 中文定位证据、规则是否实际执行及任务结论；未检查不会标为通过。 |

预览检查包括必需字段、任务长度、状态／动作维度、有限数值、派生帧索引、派生时间单调性和间隔、图像形状、图像记录数及图像读取。其规则版本单独标记为 `hdf5-preview-0.4.0`。其中帧索引和时间由 `frame_index/20` 生成，检查对象是派生浏览坐标，未证明原始采集时间正确。单任务读取失败保留诊断并继续其他任务；整个文件无法识别时不登记为有效预览。

## V0.4 候选集与人工标注映射

任务级保留／隔离／排除决定是否进入转换；人工审核的执行结果表达机器人任务表现。`outcome=failure` 不自动等同于数据损坏，处理进程失败则记录在运行状态。存在未通过质检或未审核的保留任务时，不生成正式转换候选集。

| 冻结候选集字段 | 输出与追溯 |
| --- | --- |
| `source_episode_index` | 原 HDF5 的 demo 编号，始终保留。 |
| `output_episode_index` | 按候选集顺序从 0 连续重编号。 |
| `annotation.task` | 经人工审核后写入官方训练字段 `task`。 |
| `annotation.outcome/tags/notes` | 完整保留在 `annotations.json`；不暗中变成训练数值特征。 |
| `source_task=Lift` | 原始环境任务说明，写入随附标注与来源清单。 |
| `quality_run_id` | 该源任务依据的质检运行，关联检查证据。 |
| `batch_id/content_version/input_fingerprint/snapshot_id` | 绑定批次、输入及冻结业务内容，防止旧产物登记为新版本。 |

例如候选集选择源 `demo_0` 和 `demo_2`，格式输出编号为 `episode 0` 和 `episode 1`，记录数为 59+57=116。输出统计、任务区间、train 划分和视频偏移依实际候选集生成。无候选集参数的 V0.3 入口继续采用3条174帧。此处说明字段与接口行为，完整运行是否通过以实际验收记录为准。

## LeRobot v3.0 输出与视频定位

由固定官方LeRobot0.4.4写入，目录由来源映射、官方写入、官方离线加载和输出质检共同验证。主工作台轻量读取只承诺本项目这套输出配置，不声称兼容所有v3数据集。

```text
staging/
├─ data/chunk-000/file-000.parquet
├─ videos/observation.images.agentview/chunk-000/file-000.mp4
├─ meta/info.json
├─ meta/stats.json
├─ meta/tasks.parquet
├─ meta/episodes/chunk-000/file-000.parquet
├─ source_manifest.json
├─ annotations.json
└─ CONVERSION.md
```

路径模板以 `meta/info.json` 的 `data_path`／`video_path` 为准。单文件可包含多条任务，不能套用v2“一条episode一个视频”的路径假设。

| 任务元数据字段 | 用途 |
| --- | --- |
| episode_index / length / tasks | 任务身份、长度和任务描述。 |
| data/chunk_index、data/file_index | 格式化数据表路径。 |
| dataset_from_index、dataset_to_index | 全局记录起止，结束位置不包含；不是文件内iloc。 |
| videos/<相机>/chunk_index、file_index | 格式化共享视频路径。 |
| videos/<相机>/from_timestamp、to_timestamp | 任务在共享视频中的时间区间。 |

实际取帧时间 = **任务内timestamp + 该相机from_timestamp**。例如第二条任务从自己的0秒开始，但不代表共享MP4的0秒。任务的帧数与区间需单独检查，不能拿整个MP4帧数与单条任务长度比较。

`source_manifest.json` 保存固定来源、实际输入哈希、映射、任务清单、时间政策、来源统计警告、包版本及实际meta／Parquet／MP4／标注文件哈希。V0.4 另记录 `conversion_spec` 完整快照。可用登记位于输出目录同级，避免指纹自包含；文件改变后不继续视为已验证。

## 验证与结论边界

转换验证器检查全部所选帧状态／动作与源 float32 映射一致、索引与派生时间一致；每任务3个首／中／末样本由官方Dataset读取；DataLoader实际读取batch2。默认3条174帧对应9个样本；业务选择2条116帧对应6个样本。保存官方解码样本，供主环境比较同一输出帧。

交付 ZIP 在独立目录解包后，重新校验包内清单与哈希、实际表格数量、维度、有限数值、索引和划分，并在新离线进程重新执行官方首／中／末抽样与 DataLoader 加载。该步骤不读取源 HDF5；源数值一致性证据来自随包提供的转换验证记录，`source_numeric_consistency_rechecked=false` 明确区分此次验证范围。整包 SHA-256 保存在包外，清单不递归计算自身哈希。

源float64转换为float32存在精度压缩；数值一致性针对明确的float32映射。H.264有损，源图像与输出误差记录但不声明像素无损。图像与视频PTS的文件内部对应，不代表传感器硬件同步、执行延迟测量或多机位对齐。

数据异常、读取／环境失败、未检查分别报告。人为数据故障只在显式测试副本；运行超时演示只改变执行过程；合成兼容探针不计入真实机器人数据验收。基础规则通过不代表任务成功、物理安全或任意模型训练适用性。


## V0.4 批次与业务字段

批次 JSON 默认保存到 `work/business/<batch_id>.json`，管理副本位于 `work/business/managed/<import_run_id>/`。字段来自当前业务实现，完整验收状态另见版本说明。

| 字段 | 含义 |
| --- | --- |
| batch_id / label | 批次唯一编号与显示名称；名称不是输入身份。 |
| source.kind | 当前为 `hdf5` 或 `so100`。 |
| source.input_fingerprint | 管理副本文件清单、实际内容与解释配置形成的指纹；配置也参与区分输入。 |
| source.test_label | 人工故障副本说明；不是原始公开样本的来源标记，也不是免检开关。 |
| revision | 批次每次有效写入递增的保存修订号，用于拒绝过期页面提交。 |
| content_version | 业务内容版本；筛选、质检、标注或审核变化会使依赖旧内容的产物过期。 |
| total_episodes / total_rows | 导入批次范围的任务数与记录数，不是上游全量数据集统计。 |
| counts | 保留、隔离、排除各自的任务数与记录数。 |
| cleaning_confirmed | 当前候选集是否已经人工确认。 |
| episodes[].quality | 任务质检状态、检查覆盖、问题与关联运行编号。 |
| episodes[].disposition | `keep` 保留、`quarantine` 隔离、`exclude` 排除。 |
| episodes[].selection_reason | 筛选原因；隔离与排除必填。 |
| history | 业务变更、修订、时间、决定与原因；与进程事件日志分开。 |

批次刚登记时任务为尚未检查／隔离；当前导入协调流程随后执行基础清洗检查。完整通过的任务可进入保留集；异常、读取失败、检查不完整都不能算通过。基础检查回写会重新建议筛选并重置审核，重新确认后才能继续。审核后的最终质检保存独立证据，不以重置人工审核替代检查。

## 标注与审核字段

| 字段 | 含义 |
| --- | --- |
| annotation.task | 人工任务描述；审核通过后写入候选输出的任务文本。 |
| annotation.outcome | `unlabeled` 未标注、`success` 成功、`failure` 失败、`uncertain` 不确定；属于人工判断。 |
| annotation.tags / notes | 任务级标签与备注。 |
| review.status | `draft` 草稿、`submitted` 已提交、`returned` 已退回、`approved` 审核通过。 |
| review.reason | 审核意见／退回原因，也用于解释重新审核要求。 |

执行结果为失败的任务，只要记录完整且通过质检和审核，仍可保留。数据质量问题、机器人执行结果、处理进程状态三者不可相互替代。任务描述和结果标签并非上游原始标注，交付时需要保留其人工来源。

## 输出登记与交付清单

`outputs` 保存输出路径、候选快照、输入／输出指纹及验证证据。`deliveries` 保存对应输出、快照、ZIP 路径、整包 SHA-256 和打包运行编号。`current` 表示记录是否对应当前业务内容版本，不单独保证文件仍然完整；预览与下载还需检查文件及验证证据。

ZIP 的 `manifest.json` 包含格式版本、批次、内容版本、快照、任务／记录数和每个文件的相对路径、大小、SHA-256。清单不包含自身哈希；ZIP 整包哈希另存于运行证据。包内 `documents/` 保存来源、人工标注、编号映射、筛选汇总、转换验证、质量报告与来源许可说明。详见 [SOP 交付说明](SOP.md)。

当前交付路径仅覆盖固定 HDF5 候选转换。图像有损编码、派生时间、源数值语义等限制随包保留；正式交付记录需要通过独立解包和官方加载。

## 八节点流程状态与最终质检

当前界面按上传／导入、数据清洗、数据筛选、数据标注、标注审核、数据质检、格式转换、打包交付展示同一批次。节点记录包含 `node_id`、`status`、`outcome`、`reason`、`content_version`、`attempt_id`、`upstream_attempt`、输入／中间产物／输出引用和历史尝试。文件的 `current` 与 `valid` 分别表达版本归属与当前核验状态。

HDF5 预览基础规则版本为 `hdf5-preview-0.4.0`；审核后的最终规则版本为 `final-quality-0.4.0`，复核数据、候选映射、标注和审核状态。两者检查范围与数量不混用。最终质检通过后自动继续转换与交付是开发中的新增行为，验证范围见 [版本状态](STATUS_v04.md)。
