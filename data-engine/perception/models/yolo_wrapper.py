"""
模型防腐层：YOLOv8m-pose 人手 Pose 检测模型的懒加载 Wrapper。
本文件必须且只能放在 models/ 目录下，严禁被核心业务层以外的代码导入。
仅加载外置 ONNX 权重；隔离 ultralytics / onnxruntime 等重型依赖，业务层只消费纯 Python 字典。

推理设备策略：
- 默认 CPU（对应 extras ``hand-pose`` → onnxruntime）
- 仅当 GPU 硬件可用，且 ``hand-pose-gpu`` 运行时完整
  （onnxruntime 提供 CUDAExecutionProvider）时，才使用 GPU
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

from quic_op.core.exceptions import ModelWeightNotFoundException

# ── 常量 ────────────────────────────────────────────────────────────
LEFT_CLASS = 0   # YOLO cls: 0 = left
RIGHT_CLASS = 1  # YOLO cls: 1 = right
DEFAULT_CONF = 0.25
DEFAULT_DEVICE = "cpu"
# 可选覆盖：QUICOP_HAND_POSE_DEVICE=cpu|cuda|0
_DEVICE_ENV_KEY = "QUICOP_HAND_POSE_DEVICE"


def is_hand_pose_gpu_runtime_ready() -> bool:
    """
    判断 hand-pose-gpu 推理环境是否完整可用。

    条件：
    1. 可导入 onnxruntime
    2. 提供 CUDAExecutionProvider（通常由 onnxruntime-gpu 安装）
    3. 至少能看到一张 CUDA 设备（优先 torch.cuda，否则以 EP 可用性为准）
    """
    try:
        import onnxruntime as ort
    except ImportError:
        return False

    providers = ort.get_available_providers()
    if "CUDAExecutionProvider" not in providers:
        return False

    try:
        import torch
        if not torch.cuda.is_available():
            return False
    except ImportError:
        # ONNX GPU 可不依赖 torch；EP 已具备则视为运行时就绪
        pass

    return True


def resolve_hand_pose_device(prefer_gpu: bool = True) -> str:
    """
    解析人手 Pose 推理设备。默认返回 ``cpu``。

    :param prefer_gpu: 为 True 时，在 GPU 运行时完整时才升级到 GPU；为 False 时强制 CPU
    :return: ultralytics 设备字符串，``cpu`` 或 ``0``（第一块 GPU）
    """
    env_device = os.environ.get(_DEVICE_ENV_KEY, "").strip().lower()
    if env_device in {"cpu", "cuda", "0", "gpu"}:
        if env_device == "cpu":
            return DEFAULT_DEVICE
        # 环境变量显式要求 GPU 时，仍需运行时完整，否则回退 CPU 并告警
        if is_hand_pose_gpu_runtime_ready():
            return "0"
        print(
            f"WARNING: {_DEVICE_ENV_KEY}={env_device} 但 hand-pose-gpu 运行时不完整，"
            f"回退 {DEFAULT_DEVICE}"
        )
        return DEFAULT_DEVICE

    if not prefer_gpu:
        return DEFAULT_DEVICE

    if is_hand_pose_gpu_runtime_ready():
        return "0"

    return DEFAULT_DEVICE


class YoloHandWrapper:
    """
    YOLOv8m-pose 人手检测模型推理封装器（ONNX）。
    仅负责单帧前向推理，返回裸检测字典；不做 I/O、不做落盘。
    """

    def __init__(self, weights_path: str, device: Optional[str] = None):
        """
        :param weights_path: ONNX 权重绝对路径
        :param device: 推理设备；``None`` 表示自动解析（默认 CPU，GPU 条件满足时升级）
        """
        weights_path = os.path.abspath(os.path.expanduser(weights_path))
        if not os.path.exists(weights_path):
            raise ModelWeightNotFoundException(
                f"找不到 YOLOv8m-pose ONNX 权重文件: {weights_path}"
            )
        if not weights_path.lower().endswith(".onnx"):
            raise ModelWeightNotFoundException(
                f"人手 Pose 检测仅支持 ONNX 权重（.onnx），收到: {weights_path}"
            )

        self.weights_path = weights_path
        self.model_instance = None
        # 默认按 CPU 策略解析；显式传入则规范化
        if device is None:
            self.device = resolve_hand_pose_device(prefer_gpu=True)
        else:
            normalized = str(device).strip().lower()
            if normalized in {"cuda", "gpu"}:
                self.device = (
                    "0" if is_hand_pose_gpu_runtime_ready() else DEFAULT_DEVICE
                )
            elif normalized == "0":
                self.device = (
                    "0" if is_hand_pose_gpu_runtime_ready() else DEFAULT_DEVICE
                )
            else:
                self.device = DEFAULT_DEVICE

    def _lazy_load(self) -> None:
        """
        延迟加载 ultralytics 与 ONNX 模型，防止未安装推理依赖时启动即崩溃。
        """
        if self.model_instance is not None:
            return

        try:
            from ultralytics import YOLO

            # 加载前再次确认设备，避免环境在进程中途变化导致误用 GPU
            if self.device != DEFAULT_DEVICE and not is_hand_pose_gpu_runtime_ready():
                print(
                    f"WARNING: 请求 GPU 推理但 hand-pose-gpu 不完整，回退 {DEFAULT_DEVICE}"
                )
                self.device = DEFAULT_DEVICE

            # 该权重为左右手 Pose 检测模型，必须声明 task=pose，否则 ONNX 后处理会错乱
            self.model_instance = YOLO(self.weights_path, task="pose")
            print(
                f"INFO: [Lazy Load] 成功导入 YOLOv8m-pose ONNX 模型，"
                f"device={self.device}，权重: {self.weights_path}"
            )
        except ImportError:
            print("WARNING: 未安装 ultralytics，启动 Mock 模式 (仅供开发测试)")
            self.model_instance = "Mocked_YOLO_Model"

    def detect(
        self, image_array, conf: float = DEFAULT_CONF
    ) -> Dict[str, Any]:
        """
        对单帧 BGR 图像执行左右手检测。

        :param image_array: BGR 图像 (numpy.ndarray)，格式为 HWC
        :param conf: 检测置信度阈值
        :return: 裸检测字典::
            {
                "left":  {"present": bool, "bbox": [x1,y1,x2,y2] | None, "confidence": float},
                "right": {"present": bool, "bbox": [x1,y1,x2,y2] | None, "confidence": float},
            }
        """
        self._lazy_load()

        if self.model_instance == "Mocked_YOLO_Model":
            return {
                "left": {"present": False, "bbox": None, "confidence": 0.0},
                "right": {"present": False, "bbox": None, "confidence": 0.0},
            }

        results = self.model_instance(
            image_array,
            conf=conf,
            verbose=False,
            device=self.device,
        )

        has_left = False
        has_right = False
        left_bbox: Optional[list] = None
        right_bbox: Optional[list] = None
        left_conf = 0.0
        right_conf = 0.0

        for result in results:
            if result.boxes is None:
                continue
            boxes = result.boxes
            for i in range(len(boxes)):
                cls = int(boxes.cls[i].item())
                score = float(boxes.conf[i].item())
                xyxy = [float(v) for v in boxes.xyxy[i].tolist()]

                if cls == LEFT_CLASS:
                    if score >= left_conf:
                        has_left = True
                        left_conf = score
                        left_bbox = xyxy
                elif cls == RIGHT_CLASS:
                    if score >= right_conf:
                        has_right = True
                        right_conf = score
                        right_bbox = xyxy

        return {
            "left": {
                "present": has_left,
                "bbox": left_bbox,
                "confidence": left_conf,
            },
            "right": {
                "present": has_right,
                "bbox": right_bbox,
                "confidence": right_conf,
            },
        }
