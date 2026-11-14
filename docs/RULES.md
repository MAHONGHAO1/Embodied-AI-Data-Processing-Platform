# 验收判据（RoboData Rules）

这份表是质检引擎实际执行的判据，不是建议。每条给出判什么、阈值是多少、不过时报告里怎么写。
规则版本 `RULE_VERSION = 0.3.0`。阈值变动会改这个版本号，旧报告不会被新版本覆盖。

判据来源：`robodata/quality.py` 的 `RULES` 元组与 `DEFAULT_RULE_PARAMS`。这份文档与代码同源，以代码为准。

---

## 阈值

| 参数 | 默认值 | 作用 |
|---|---|---|
| `interval_relative_tolerance` | `0.10` | 采样间隔容差，按期望间隔的 10% 计 |
| `timestamp_absolute_tolerance` | `1e-5` | 时间绝对容差，秒 |
| `video_timestamp_tolerance_frames` | `0.5` | 视频帧与样本时间对齐容差，单位帧 |

间隔容差合并计算：`期望间隔 × 0.10 + 1e-5` 秒。
视频对齐容差取两者较大：`max(1e-5, 0.5 ÷ fps)` 秒。

## 必需字段

```
episode_index  frame_index  timestamp  observation.state  action
```

---

## 一、表格检查（8 条）

### `required_fields` 必需字段

判据：上面五个字段是否都在表里。
不过时：`缺少必需字段`，证据带 `missing_fields` 数组。

### `row_count` 任务长度

判据：实际行数是否等于源元数据声明的 `expected_length`。行数为 0 是另一种情况，单独报。
不过时：`任务实际长度与源元数据不一致`，证据 `{"expected": …, "actual": …}`。

### `episode_identity` 任务编号

判据：每一行的 `episode_index` 是否都等于当前任务编号。
不过时：`样本任务编号与当前任务不一致`。

### `vector_shape` 状态与动作维度

判据：`observation.state` 和 `action` 的形状是否等于源 `features` 声明的 `shape`。源没声明时按 6 维。
不过时：`字段维度与声明不一致`，证据带 `expected_shape` 与 `actual_shape`。

### `numeric_finite` 数值有效性

判据：必需字段的每个值能否转成浮点数且全部有限（无 NaN、无 Inf）；`episode_index`、`frame_index`、`timestamp` 还必须是标量，不能是数组。
不过时：`字段含非数值、非有限值或无效标量`，证据带字段名与原值。

### `frame_index` 帧索引连续性

判据：任务内每条的 `frame_index` 是否正好等于行号（从 0 开始、步长 1）。
不过时：`帧索引不符合任务内连续序列`，证据 `{"expected": 行号, "actual": 实际值}`。

### `timestamp_monotonic` 时间单调性

判据：时间值非负，且相邻样本严格递增（不重复、不倒退）。只在任务内比较。
不过时：`任务时间值为负，无法作为浏览时间` 或 `时间值重复或倒退`。

### `timestamp_interval` 采样时间间隔

判据：相邻样本的时间间隔是否落在 `1/fps` 的容差带内。来源没给有效 fps 时这条记为未检查，不算通过也不算失败。
不过时：`相邻样本时间间隔超出容差`，证据 `{"expected_seconds", "actual_seconds", "tolerance_seconds"}`。

---

## 二、视频检查（3 条，逐相机）

多相机时逐路都查；任何一路没查完，对应规则整体记为未检查。

### `video_decode` 视频完整解码

判据：视频文件存在且能完整解码到末尾。
不过时：`读取相机视频失败`（单相机）或 `读取相机视频失败（相机名）`，归类 `load_failure`。这一类会让整个运行返回非 0。

### `video_frame_count` 视频帧数

判据：解码出的帧数是否等于表格行数。
不过时：`相机视频帧数与样本行数不一致`，证据 `{"video_frames", "table_rows"}`。

### `video_timestamp_mapping` 视频与样本时间对应

判据：每一帧的 pts 时间与同序号样本的 `timestamp` 差值是否在容差内，且样本时间落在视频可读取范围 `[首 pts − ε, 末 pts + ε]` 内，`ε = max(1e-5, 0.001 ÷ 视频fps)`。
不过时：`相机视频帧与样本时间差超出容差，或样本时间超出视频可读取范围`，证据 `reasons` 取值为 `same_index_time_error`（同序号时间差超容差）或 `outside_readable_video_range`（样本时间超出视频范围）。

---

## 三、状态与结论口径

每条规则的覆盖状态：

| 取值 | 含义 |
|---|---|
| `performed` | 真的执行了 |
| `unavailable` | 执行不了（缺字段、缺 fps、没开视频检查等） |

任务状态：

| 取值 | 含义 |
|---|---|
| `passed` | 无 issue 且 11 条全部 `performed` |
| `issues` | 有 issue |
| `incomplete` | 有规则没执行到，结论不完整 |

issue 的 `severity` 与 `category`：

| severity | 含义 |
|---|---|
| `error` | 判据不通过 |
| `info` | 只是记录，不代表失败 |

| category | 含义 | 退出码影响 |
|---|---|---|
| `data_issue` | 数据本身不合规 | 计入质量失败 |
| `load_failure` | 文件读不出、视频解不开 | 返回 2 |
| `not_checked` | 该项没检查 | 不计失败，但会让状态变 `incomplete` |

`summary` 里的四个计数：`data_issue_count`、`load_failure_count`、`not_checked_count`、`passed_episode_count`。

---

## 四、明确不判的

下面这些不在判据内，报告的 `limitations` 字段会原样写出来：

- 只检查记录与文件内视频的时间对应，不代表硬件同步或双相机同步。
- 不判断动作的控制含义、物理安全性、任务成功率或训练适用性。
- 完整性比对只针对本地下载清单，没有重新做在线来源校验。

另有两项由运行方式决定，不进判据：`--skip-video` 时三条视频规则整体记为 `unavailable`；`--execution-demo video_timeout` 是明确标注的人为等待演示，不代表真实超时行为。

---

## 五、失败样本集

`fixtures/known-bad/` 收了一组故意做坏的包，每个附 `EXPECTED.md` 写明应该报哪条规则。它们用来验证判据本身有没有在工作，也能拿去测你自己的检查器。

| 目录 | 注入了什么 | 应当命中 |
|---|---|---|
| `missing-camera/` | 缺一路相机视频 | `video_decode` |
| `frame-gap/` | 帧索引跳号 | `frame_index` |
| `nan-joint/` | 关节值出现 NaN | `numeric_finite` |
| `length-mismatch/` | 表格行数与元数据不符 | `row_count` |
| `timestamp-jump/` | 时间戳跳了一段 | `timestamp_interval` |
| `video-length/` | 视频帧数与行数不符 | `video_frame_count` |
