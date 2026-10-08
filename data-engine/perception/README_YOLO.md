# YOLOv8m-pose 人手 Pose 检测算子

## 概述

基于 YOLOv8m-pose 的左右手 Pose 检测算子（**推理权重为 ONNX**），遵循 `quic_op` 洋葱架构与 I/O 解耦规范：
算子仅接受**本地绝对路径**或**内存图像对象**，通过 `models/` 防腐层懒加载重型依赖，
并经统一持久化层将过程数据写入 JSON。

外部每次调用返回 **结构化数据**（`HandPoseOperatorResult`）+ **JSON 绝对路径**。

## 架构与隔离

```
perception/
├── models/yolo_wrapper.py      ← 防腐层：ONNX 推理懒加载，仅输出裸 dict
├── detecthands.py              ← 业务门面：HandDetector，组装 core 契约并落盘
├── weights/YOLO/detector.onnx  ← ONNX 权重外置（架构内置、权重外挂）
└── README_YOLO.md              ← 本文件

core/
├── data_types.py               ← HandDetectionResult / HandPoseOperatorResult
├── persistence.py              ← JsonResultPersister 统一 JSON 持久化层
├── capability.py               ← CapabilitySpec / 能力注册表
└── invoke.py                   ← quic_op.invoke 统一调度
```

调用链：

```
HandDetector  →  YoloHandWrapper  →  ultralytics.YOLO(*.onnx)
      │                                    │
      └─ JsonResultPersister.save()  ←─────┘（业务层组装后落盘）

quic_op.invoke("perception.hand_pose_yolo", video_path=...)
      └─ CapabilitySpec → HandDetector.detect_video(...)
```

| 层级 | 职责 | 禁止事项 |
| :--- | :--- | :--- |
| `core/` | 纯数据契约 + JSON 持久化 + 能力声明/invoke | 禁止引入 torch / cv2 / ultralytics |
| `models/` | ONNX 模型懒加载与裸推理 | 禁止做业务编排与落盘 |
| `detecthands.py` | 路径/内存入参、契约组装、调用持久化 | 禁止顶层 import 重型推理库 |
| `capabilities.py` | 向 SDK 注册可 invoke 能力 | 禁止执行重推理 |

## 权重文件

本算子**仅支持 ONNX 权重**（不接收 `.pt` / `.pth`）。

| 文件 | 说明 |
| :--- | :--- |
| `perception/weights/YOLO/detector.onnx` | 默认且仓库内置的 ONNX 推理权重 |

模型分类：`class 0 = 左手`，`class 1 = 右手`。

覆盖权重时必须传入 `.onnx` 绝对路径；后缀不符将抛出 `ModelWeightNotFoundException`。

## 安装依赖

推荐通过 SDK extras 安装（版本与环境配置见根目录 `pyproject.toml`）：

```bash
# 默认：CPU 推理（onnxruntime）
pip install -e ".[hand-pose]"

# GPU 节点：安装 GPU 运行时；代码仍默认 CPU，
# 仅当 CUDA 可用且本环境完整时自动切 GPU
pip install -e ".[hand-pose-gpu]"
```

> `hand-pose` 与 `hand-pose-gpu` 的 onnxruntime 包互斥，请按目标机器二选一安装。

也可手动安装同等依赖：

```bash
# CPU
pip install opencv-python numpy h5py ultralytics onnxruntime
# GPU
pip install opencv-python numpy h5py ultralytics onnxruntime-gpu
```

## 接口调用方式

```python
from quic_op.perception import HandDetector

# 1) 使用默认 ONNX：perception/weights/YOLO/detector.onnx
detector = HandDetector()

# 2) 显式传入其他 ONNX 权重绝对路径
detector = HandDetector("/abs/path/to/detector.onnx")
```

### SDK 统一 invoke

```python
import quic_op

for cap in quic_op.list_capabilities(domain="perception"):
    print(cap.operator_id, cap.method, cap.version)

# 等价于 HandDetector(device="cpu").detect_video(...)
result = quic_op.invoke(
    "perception.hand_pose_yolo",
    video_path="/abs/path/to/video.mp4",
    conf=0.3,
    device="cpu",           # 构造参数，自动剥离给 HandDetector.__init__
    output_dir="/abs/path/to/output/yolo",
)
print(result.stats, result.json_path)
```

能力声明文件：`perception/capabilities.py`（导入 `quic_op` / `quic_op.perception` 时自动注册）。

| operator_id | method |
| :--- | :--- |
| `perception.hand_pose_yolo` | `detect_video` |
| `perception.hand_pose_yolo.frames` | `detect_frames` |
| `perception.hand_pose_yolo.hdf5` | `detect_hdf5` |

### 检测视频（本地绝对路径）

```python
result = detector.detect_video(
    "/abs/path/to/video.mp4",
    conf=0.3,
    output_dir="/abs/path/to/output/yolo",  # 可选，默认 <repo>/output/yolo
)

print(result.num_frames)
print(result.stats)
print(result.json_path)          # JSON 绝对路径
print(result.frames[0].left_bbox)
```

### 检测 HDF5

```python
result = detector.detect_hdf5("/abs/path/to/episode_0000.hdf5")
# JSON → <output_dir>/episode_0000_hand_labels.json
```

### 检测内存帧序列（云边端同构推荐入口）

```python
import cv2

frames = [cv2.imread(p) for p in frame_paths]  # 上层负责 I/O
result = detector.detect_frames(
    frames,
    conf=0.25,
    artifact_name="episode_0000",
    fps=30.0,
)
```

### 单帧检测

```python
result = detector.detect_frame(bgr_image, persist=True)
# persist=False 时仅返回内存结构，json_path 为空字符串
```

### 统计

```python
stats = HandDetector.get_stats(result)   # 也可传入 result.frames
# → {"total_frames", "left_count", "right_count", "both_count", "none_count"}
```

## 结构化数据（返回值）

### `HandPoseOperatorResult`（算子统一对外契约）

| 字段 | 类型 | 说明 |
| :--- | :--- | :--- |
| `frames` | `List[HandDetectionResult]` | 逐帧检测结果 |
| `json_path` | `str` | 持久化 JSON 的绝对路径；`persist=False` 时为空串 |
| `source_path` | `str \| None` | 输入源绝对路径；内存帧时为 `None` |
| `source_type` | `str` | `video` / `hdf5` / `frames` / `frame` |
| `num_frames` | `int` | 帧数 |
| `fps` | `float \| None` | 帧率（若可知） |
| `conf_threshold` | `float` | 本次调用使用的置信度阈值 |
| `weights_path` | `str` | 实际加载的权重绝对路径 |
| `stats` | `Dict[str, int]` | 左右手出现频次统计 |
| `operator` | `str` | 固定为 `perception.hand_pose_yolo` |

### `HandDetectionResult`（单帧契约，定义于 `core.data_types`）

| 字段 | 类型 | 说明 |
| :--- | :--- | :--- |
| `frame_id` | `int` | 帧序号，从 0 开始 |
| `left_hand_present` | `bool` | 是否检测到左手 |
| `right_hand_present` | `bool` | 是否检测到右手 |
| `left_bbox` | `List[float] \| None` | `[x1, y1, x2, y2]` 像素坐标 |
| `right_bbox` | `List[float] \| None` | `[x1, y1, x2, y2]` 像素坐标 |
| `left_confidence` | `float` | `0.0 ~ 1.0` |
| `right_confidence` | `float` | `0.0 ~ 1.0` |

## JSON 文件 Schema

文件名：`<artifact_stem>_hand_labels.json`  
由 `core.persistence.JsonResultPersister` 统一写出，`schema_version` 当前为 `"1.0"`。

### Schema（JSON Schema Draft-07 风格）

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "$id": "quic_op.perception.hand_pose_yolo.v1",
  "title": "HandPoseYoloResultDocument",
  "type": "object",
  "required": ["schema_version", "operator", "meta", "stats", "frames"],
  "properties": {
    "schema_version": {
      "type": "string",
      "const": "1.0",
      "description": "持久化契约版本"
    },
    "operator": {
      "type": "string",
      "const": "perception.hand_pose_yolo"
    },
    "meta": {
      "type": "object",
      "required": [
        "created_at",
        "source_type",
        "num_frames",
        "conf_threshold",
        "weights_path",
        "device"
      ],
      "properties": {
        "created_at": {
          "type": "string",
          "format": "date-time",
          "description": "UTC ISO-8601 时间戳"
        },
        "source_path": {
          "type": ["string", "null"],
          "description": "输入源绝对路径；内存帧为 null"
        },
        "source_type": {
          "type": "string",
          "enum": ["video", "hdf5", "frames", "frame"]
        },
        "num_frames": { "type": "integer", "minimum": 0 },
        "fps": { "type": ["number", "null"] },
        "conf_threshold": { "type": "number", "minimum": 0, "maximum": 1 },
        "weights_path": { "type": "string" },
        "device": {
          "type": "string",
          "description": "实际推理设备：cpu 或 0（GPU）"
        }
      },
      "additionalProperties": true
    },
    "stats": {
      "type": "object",
      "required": [
        "total_frames",
        "left_count",
        "right_count",
        "both_count",
        "none_count"
      ],
      "properties": {
        "total_frames": { "type": "integer", "minimum": 0 },
        "left_count": { "type": "integer", "minimum": 0 },
        "right_count": { "type": "integer", "minimum": 0 },
        "both_count": { "type": "integer", "minimum": 0 },
        "none_count": { "type": "integer", "minimum": 0 }
      },
      "additionalProperties": false
    },
    "frames": {
      "type": "array",
      "items": {
        "type": "object",
        "required": [
          "frame_id",
          "left_hand_present",
          "right_hand_present",
          "left_bbox",
          "right_bbox",
          "left_confidence",
          "right_confidence"
        ],
        "properties": {
          "frame_id": { "type": "integer", "minimum": 0 },
          "left_hand_present": { "type": "boolean" },
          "right_hand_present": { "type": "boolean" },
          "left_bbox": {
            "oneOf": [
              {
                "type": "array",
                "items": { "type": "number" },
                "minItems": 4,
                "maxItems": 4
              },
              { "type": "null" }
            ]
          },
          "right_bbox": {
            "oneOf": [
              {
                "type": "array",
                "items": { "type": "number" },
                "minItems": 4,
                "maxItems": 4
              },
              { "type": "null" }
            ]
          },
          "left_confidence": { "type": "number", "minimum": 0, "maximum": 1 },
          "right_confidence": { "type": "number", "minimum": 0, "maximum": 1 }
        },
        "additionalProperties": false
      }
    }
  },
  "additionalProperties": false
}
```

### 示例文档

```json
{
  "schema_version": "1.0",
  "operator": "perception.hand_pose_yolo",
  "meta": {
    "created_at": "2026-08-06T07:50:00+00:00",
    "source_path": "/data/demo.mp4",
    "source_type": "video",
    "num_frames": 2,
    "fps": 30.0,
    "conf_threshold": 0.25,
    "weights_path": "/repo/perception/weights/YOLO/detector.onnx",
    "device": "cpu"
  },
  "stats": {
    "total_frames": 2,
    "left_count": 1,
    "right_count": 1,
    "both_count": 1,
    "none_count": 0
  },
  "frames": [
    {
      "frame_id": 0,
      "left_hand_present": true,
      "right_hand_present": false,
      "left_bbox": [320.5, 140.2, 480.1, 380.7],
      "right_bbox": null,
      "left_confidence": 0.87,
      "right_confidence": 0.0
    },
    {
      "frame_id": 1,
      "left_hand_present": true,
      "right_hand_present": true,
      "left_bbox": [310.0, 150.0, 470.0, 390.0],
      "right_bbox": [520.0, 160.0, 680.0, 400.0],
      "left_confidence": 0.91,
      "right_confidence": 0.88
    }
  ]
}
```

## 设计说明

- **云边端同构**：`detect_frames` / `detect_frame` 只吃内存对象；`detect_video` / `detect_hdf5` 只吃本地路径，不耦合 OSS/ODPS。
- **ONNX 外置权重**：默认与约定格式均为 `detector.onnx`，架构内置、权重外挂。
- **默认 CPU 推理**：`pip install -e ".[hand-pose]"` 对应 CPU；仅当 GPU 可用且 `hand-pose-gpu`（CUDAExecutionProvider）完整时才自动用 GPU。
- **设备覆盖**：环境变量 `QUICOP_HAND_POSE_DEVICE=cpu|cuda`，或 `HandDetector(device="cpu")`。
- **懒加载**：构造 `HandDetector` 时不导入 ultralytics / onnxruntime，首次推理才加载。
- **Mock 降级**：未安装推理依赖时返回空检测，便于 CPU 环境联调。
- **权重校验**：路径不存在或非 `.onnx` 后缀时抛出 `ModelWeightNotFoundException`。
- **统一持久化**：所有落盘经 `JsonResultPersister`，保证跨算子 Schema 外壳一致；JSON `meta.device` 记录实际推理设备。
