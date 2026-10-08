# 物理感知库 (`quic_op.perception`)

从 2D 视觉逆解 3D 物理世界的重 GPU 算子集合。遵循项目洋葱架构：
业务门面只消费内存对象 / 本地绝对路径；重型深度学习依赖隔离在 `models/` 防腐层并懒加载。

## 模块一览

| 模块 | 入口类 | 说明 |
| :--- | :--- | :--- |
| `detecthands.py` | `HandDetector` | YOLOv8m-pose 人手 Pose 检测（ONNX） |
| `depth.py` | `DepthEstimator` | MoGe-2 稠密深度估计 |
| `mesh.py` | `HandReconstructor` | HaWoR 3D 手部网格重建 |
| `physics.py` | `KinematicsAnalyzer` | 运动学分析与重定向 |

## 目录隔离

```
perception/
├── models/          ← 防腐层（重型推理库懒加载；人手检测为 ONNX）
├── weights/         ← 模型权重外置目录（YOLO 使用 detector.onnx）
├── detecthands.py   ← 业务门面
├── depth.py
├── mesh.py
├── physics.py
└── README_YOLO.md   ← 人手 Pose 检测接口 / 数据结构 / JSON Schema
```

人手 Pose 检测的完整调用方式、结构化返回值与 JSON Schema，见 **[README_YOLO.md](./README_YOLO.md)**。
