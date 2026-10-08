# YOLO 人手 Pose 权重目录

模型架构内置于 `perception/models/yolo_wrapper.py`，**推理权重仅使用 ONNX**，外挂于本目录。

| 文件 | 说明 |
| :--- | :--- |
| `detector.onnx` | 默认 / 唯一约定的推理权重（class 0 = 左手，class 1 = 右手） |

默认加载本目录下的 `detector.onnx`；如需覆盖，传入**其他 ONNX 文件的绝对路径**：

```python
from quic_op.perception import HandDetector

# 默认：perception/weights/YOLO/detector.onnx
detector = HandDetector()

# 显式指定其他 ONNX 权重
detector = HandDetector("/abs/path/to/detector.onnx")
```

> 本算子暂不接收 `.pt` / `.pth` 权重；请使用已导出的 ONNX 模型，后续再考虑扩展。  
> 默认 CPU：`pip install -e ".[hand-pose]"`；GPU 环境：`pip install -e ".[hand-pose-gpu]"`（代码按条件启用 GPU）。
