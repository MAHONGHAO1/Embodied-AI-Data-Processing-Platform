# `quic_op.clean` 模块设计与开发规范

## 1. 模块定位
`quic_op.clean` 是具身智能数据流水线中的基础清洗与图像预处理算子库，定位为纯 CPU / I/O 密集模块。

- 视频与图像的物理处理：切分、抽帧、重组、过滤
- 几何与相机基础算子：去畸变、内参矩阵处理
- 物理画质与时序质检：低光、模糊、掉帧（输出结构化契约 + JSON 持久化）

---

## 2. 工程红线（必须遵守）
- 禁止深度学习依赖：禁止 `torch` / `tensorflow` 等模型推理框架
- 禁止云端 I/O：只接受本地绝对路径或内存对象；禁止在算子内拉取/上传 OSS、S3、ODPS
- 长视频必须流式降维：必须采用 ffmpeg/ffprobe 管道流；严禁全尺寸逐帧解码
- 过程数据统一经 `core.persistence.JsonResultPersister` 落盘；禁止业务类内手写零散 `json.dump`

---

## 3. 目录结构与对外 API

```text
clean/
├── __init__.py
├── video_editor.py
├── geometry.py
├── quality_inspector.py
└── README_CLEAN.md

core/
├── data_types.py      ← LowLight*/Blur*/FrameDrop* 契约
└── persistence.py     ← JsonResultPersister
```

### 核心 API
- `VideoEditor.split_with_overlap(...)`: 按时序带重叠切片
- `VideoEditor.extract_keyframes(...)`: 均匀抽帧
- `VideoEditor.drop_dead_frames(...)`: 死帧/花屏剔除
- `GeometryProcessor.build_isotropic_intrinsics(...)`: 构建各向同性内参
- `GeometryProcessor.undistort_image(...)`: OpenCV 去畸变
- `QualityInspector.detect_lowlight_metrics(...) -> LowLightOperatorResult`: 低光逐采样帧 QC
- `QualityInspector.detect_blur_metrics(...) -> BlurOperatorResult`: 模糊逐采样帧 QC
- `QualityInspector.detect_frame_drop_metrics(...) -> FrameDropOperatorResult`: 掉帧事件 QC（基于 ffprobe PTS，不解帧）

调用示例：

```python
from quic_op.clean import QualityInspector

inspector = QualityInspector()
lowlight = inspector.detect_lowlight_metrics("/abs/path/video.mp4")
blur = inspector.detect_blur_metrics("/abs/path/video.mp4")
drops = inspector.detect_frame_drop_metrics("/abs/path/video.mp4", expected_fps=15.0)

print(lowlight.stats, lowlight.json_path)
print(blur.samples[0].blur_flag)
print(drops.events)
```

默认 JSON 目录：`<repo>/output/clean/{lowlight|blur|frame_drop}/`

### SDK 统一 invoke

```python
import quic_op

# 列出 clean 能力
for cap in quic_op.list_capabilities(domain="clean"):
    print(cap.operator_id, cap.method, cap.version)

# 统一调用（等价于 QualityInspector().detect_lowlight_metrics(...)）
result = quic_op.invoke(
    "clean.quality_lowlight",
    video_path="/abs/path/video.mp4",
    output_dir="/abs/path/output/clean/lowlight",
)
print(result.stats, result.json_path)
```

能力声明文件：`clean/capabilities.py`（导入 `quic_op.clean` / `quic_op` 时自动注册）。

---

## 4. 质量检测契约与判定规则（QC 标准）

### 4.1 输入/输出契约
- 输入：`video_path`（本地 MP4 绝对路径）
- 输出：返回结构化算子结果（同时落盘 JSON），而非裸 `List[dict]`

| 返回类型 | 明细字段 | 原子契约 | 说明 |
| --- | --- | --- | --- |
| `LowLightOperatorResult` | `samples` | `LowLightSample` | `t_sec, mean_gray, dark_pixel_ratio, low_light_flag` |
| `BlurOperatorResult` | `samples` | `BlurSample` | `t_sec, laplacian_var, blur_flag` |
| `FrameDropOperatorResult` | `events` | `FrameDropEvent` | `t_prev_sec, t_curr_sec, frame_interval_ms, drop_frame_count, frame_drop_flag` |

各结果均包含：`json_path`、`source_path`、`stats`、`operator`。

算子 ID：
- `clean.quality_lowlight`
- `clean.quality_blur`
- `clean.quality_frame_drop`

JSON 外壳（与 pose 算子同构，`schema_version = "1.0"`）：

```json
{
  "schema_version": "1.0",
  "operator": "clean.quality_lowlight",
  "meta": {
    "created_at": "2026-08-06T12:00:00+00:00",
    "source_path": "/abs/path/video.mp4",
    "source_type": "video",
    "num_samples": 100
  },
  "stats": { "total_samples": 100, "flagged_count": 12 },
  "samples": [
    {
      "t_sec": 0.0,
      "mean_gray": 28.5,
      "dark_pixel_ratio": 0.41,
      "low_light_flag": true
    }
  ]
}
```

（掉帧算子载荷字段为 `events`，非 `samples`。）

### 4.2 默认参数（`QualityInspector.detect_*`）

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `expected_fps` | `15.0` | 视频实际帧率基准 |
| `drop_threshold_sec` | `None` | 掉帧阈值（`None` 表示自适应：`1.35 * expected_interval`） |
| `blur_threshold` | `22.0` | 模糊阈值（Laplacian 方差） |
| `light_mean_threshold` | `35.0` | 低光判定：均值阈值 |
| `dark_pixel_thresh` | `30` | 低光判定：暗像素阈值（灰度） |
| `dark_area_ratio` | `0.28` | 低光判定：暗像素面积占比阈值 |
| `min_duration_sec` | `0.5` | 区间最短持续时长（秒） |
| `sample_fps` | `5` | ffmpeg 抽帧 fps（用于模糊/低光明细） |
| `scale_height` | `360` | ffmpeg 降维高度（用于模糊/低光） |
| `timeout_sec` | `600` | ffprobe 超时（秒） |
| `output_dir` | `None` | JSON 落盘目录；默认 `<repo>/output/clean/...` |

### 4.3 一次解码原则
- 掉帧：`ffprobe` 解析 PTS（不解帧）
- 模糊/低光：ffmpeg 灰度降维流（单次解码，逐采样帧计算并返回明细）
- 降维推荐：`sample_fps=5`、`scale_height=360`、灰度 `gray`，并显式指定 `-sws_flags fast_bilinear`

### 4.4 规则明细
1. **掉帧/丢帧（`frame_drop_events`）**
   - 指标：相邻帧 PTS 间隔 `diff`
   - 基准：以视频实际帧率 `expected_fps` 为基准（默认 15fps，理论间隔约 66.67ms）
   - 阈值：默认 `drop_threshold_sec = 1.35 * expected_interval`（15fps 时约 90ms），也可显式传参
2. **低光（`lowlight_metrics`）**
   - 指标：灰度均值 `mean(gray)` 与暗像素占比 `ratio(gray < 30)`
   - 判定：`mean < 35` 或 `ratio > 0.28`
3. **模糊（`blur_metrics`）**
   - 指标：`var(Laplacian(gray))`
   - 判定：方差 `< 22.0`（匹配 360p 降维后的物理特征）

---

## 5. 功能边界（放行 / 拒收）
### 5.1 推荐放在 `clean`
- 纯 CPU 可实现的物理缺陷与画质缺陷检测（如低光、模糊、掉帧）

### 5.2 必须移交到其他模块
- ❌ Pose/关键点缺失检测：移交 `perception`
- ❌ 语义级遮挡检测：移交 `label`
- ❌ 多模态传感器对齐：移交 `sync`
- ❌ 最终产物 Schema 校验：移交 `export`

---

## 6. 测试约定
- 质量检测：`tests/test_quality_inspector.py`
- 清洗算子：`tests/test_clean_module.py`
