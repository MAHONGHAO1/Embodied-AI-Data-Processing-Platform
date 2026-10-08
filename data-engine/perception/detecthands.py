"""
人手 Pose 检测业务门面模块。
负责：I/O 解耦输入（绝对路径或内存帧）→ 调用 models/ 防腐层 → 组装 core 契约 → 统一持久化。
算子版本由此处 ``__version__`` 提供；``pyproject.toml`` 通过 attr 传参引用，勿在 toml 中写死版本号。
"""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import List, Optional, Sequence, Union

from quic_op.core.data_types import (
    HAND_POSE_OPERATOR_ID,
    HandDetectionResult,
    HandPoseOperatorResult,
)
from quic_op.core.persistence import JsonResultPersister
from quic_op.perception.models.yolo_wrapper import DEFAULT_CONF, YoloHandWrapper

# YOLOv8m-pose 人手 Pose 检测算子版本（供 pyproject / 发行元数据 attr 引用）
__version__ = "0.1.0"

# 默认权重：外置 ONNX（架构内置目录，权重文件外挂）
_DEFAULT_WEIGHTS = (
    Path(__file__).resolve().parent / "weights" / "YOLO" / "detector.onnx"
)
# 默认落盘目录（项目根目录下 output/yolo）
_DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output" / "yolo"


class HandDetector:
    """
    人手 Pose 检测业务调度类。
    封装 YOLOv8m-pose（ONNX），对外只接受本地绝对路径或内存图像对象；
    每次调用均返回 ``HandPoseOperatorResult``（结构化数据 + JSON 绝对路径）。
    """

    OPERATOR_ID = HAND_POSE_OPERATOR_ID
    VERSION = __version__

    def __init__(
        self,
        weights_path: Optional[str] = None,
        persister: Optional[JsonResultPersister] = None,
        device: Optional[str] = None,
    ):
        """
        :param weights_path: ONNX 权重路径；默认 ``perception/weights/YOLO/detector.onnx``
        :param persister: 可选自定义持久化器
        :param device: 推理设备；``None`` 时默认 CPU，仅当 GPU 可用且
            ``hand-pose-gpu`` 运行时完整时自动使用 GPU
        """
        resolved = Path(weights_path).expanduser().resolve() if weights_path else _DEFAULT_WEIGHTS
        # 实例化防腐层 Wrapper，此时不会触发 ultralytics / onnxruntime 导入
        self.model_wrapper = YoloHandWrapper(str(resolved), device=device)
        self.weights_path = str(resolved)
        self.device = self.model_wrapper.device
        self.persister = persister or JsonResultPersister()

    # ── 公开入口 ──────────────────────────────────────────────────────

    def detect_video(
        self,
        video_path: str,
        conf: float = DEFAULT_CONF,
        output_dir: Optional[str] = None,
    ) -> HandPoseOperatorResult:
        """
        对视频文件逐帧执行人手 Pose 检测。

        :param video_path: 视频文件本地绝对（或可解析）路径
        :param conf: 检测置信度阈值
        :param output_dir: JSON 落盘目录，默认 ``<repo>/output/yolo``
        :return: HandPoseOperatorResult（含 frames 与 json_path）
        """
        source = Path(video_path).expanduser().resolve()
        print(f"Processing video: {source.name}")
        frames, meta = _read_video_frames(source)
        print(f"  Frames: {meta['num_frames']}, fps: {meta['fps']:.2f}")

        detections = self._detect_frames(frames, conf)
        return self._finalize(
            detections=detections,
            source_path=str(source),
            source_type="video",
            conf=conf,
            fps=meta["fps"],
            output_dir=output_dir,
            artifact_stem=source.stem,
        )

    def detect_hdf5(
        self,
        hdf5_path: str,
        conf: float = DEFAULT_CONF,
        output_dir: Optional[str] = None,
    ) -> HandPoseOperatorResult:
        """
        对 HDF5 episode 文件逐帧执行人手 Pose 检测。

        :param hdf5_path: HDF5 文件本地路径，需含 ``frame/head`` 与 ``meta_info/fps``
        :param conf: 检测置信度阈值
        :param output_dir: JSON 落盘目录
        :return: HandPoseOperatorResult
        """
        source = Path(hdf5_path).expanduser().resolve()
        print(f"Processing: {source.name}")
        frames, meta = _read_hdf5_frames(source)
        print(f"  Frames: {meta['num_frames']}, fps: {meta['fps']:.2f}")

        detections = self._detect_frames(frames, conf)
        return self._finalize(
            detections=detections,
            source_path=str(source),
            source_type="hdf5",
            conf=conf,
            fps=meta["fps"],
            output_dir=output_dir,
            artifact_stem=source.stem,
        )

    def detect_frames(
        self,
        frames: Sequence,
        conf: float = DEFAULT_CONF,
        output_dir: Optional[str] = None,
        artifact_name: str = "memory_frames",
        fps: Optional[float] = None,
    ) -> HandPoseOperatorResult:
        """
        对内存中的 BGR 帧序列执行人手 Pose 检测（云边端同构的核心入口）。

        :param frames: BGR 图像序列，元素为 ``numpy.ndarray``，形状 ``(H, W, 3)``
        :param conf: 检测置信度阈值
        :param output_dir: JSON 落盘目录
        :param artifact_name: 落盘文件名前缀
        :param fps: 可选帧率元信息
        :return: HandPoseOperatorResult
        """
        detections = self._detect_frames(list(frames), conf)
        return self._finalize(
            detections=detections,
            source_path=None,
            source_type="frames",
            conf=conf,
            fps=fps,
            output_dir=output_dir,
            artifact_stem=artifact_name,
        )

    def detect_frame(
        self,
        frame,
        conf: float = DEFAULT_CONF,
        output_dir: Optional[str] = None,
        artifact_name: str = "single_frame",
        persist: bool = True,
    ) -> HandPoseOperatorResult:
        """
        对单帧 BGR 图像执行人手 Pose 检测。

        :param frame: BGR 图像 (numpy.ndarray)，格式为 HWC
        :param conf: 检测置信度阈值
        :param output_dir: JSON 落盘目录
        :param artifact_name: 落盘文件名前缀
        :param persist: 是否写入 JSON；为 False 时 ``json_path`` 为空字符串
        :return: HandPoseOperatorResult
        """
        detections = self._detect_frames([frame], conf)
        if not persist:
            stats = self.get_stats(detections)
            return HandPoseOperatorResult(
                frames=detections,
                json_path="",
                source_path=None,
                source_type="frame",
                num_frames=1,
                conf_threshold=conf,
                weights_path=self.weights_path,
                fps=None,
                stats=stats,
            )
        return self._finalize(
            detections=detections,
            source_path=None,
            source_type="frame",
            conf=conf,
            fps=None,
            output_dir=output_dir,
            artifact_stem=artifact_name,
        )

    # ── 内部批量推理 ──────────────────────────────────────────────────

    def _detect_frames(
        self, frames: List, conf: float = DEFAULT_CONF
    ) -> List[HandDetectionResult]:
        """对帧列表逐帧检测，并转换为 core 契约实体。"""
        results: List[HandDetectionResult] = []
        num_frames = len(frames)

        for frame_idx, frame in enumerate(frames):
            detection = self.model_wrapper.detect(frame, conf=conf)
            results.append(
                HandDetectionResult(
                    frame_id=frame_idx,
                    left_hand_present=detection["left"]["present"],
                    right_hand_present=detection["right"]["present"],
                    left_bbox=detection["left"]["bbox"],
                    right_bbox=detection["right"]["bbox"],
                    left_confidence=detection["left"]["confidence"],
                    right_confidence=detection["right"]["confidence"],
                )
            )
            if (frame_idx + 1) % 200 == 0 or (frame_idx + 1) == num_frames:
                print(f"  ... {frame_idx + 1}/{num_frames}")

        return results

    def _finalize(
        self,
        detections: List[HandDetectionResult],
        source_path: Optional[str],
        source_type: str,
        conf: float,
        fps: Optional[float],
        output_dir: Optional[str],
        artifact_stem: str,
    ) -> HandPoseOperatorResult:
        """统计、落盘并组装统一返回契约。"""
        stats = self.get_stats(detections)
        self._print_stats(stats)

        save_dir = Path(output_dir).expanduser().resolve() if output_dir else _DEFAULT_OUTPUT_DIR
        json_path = self.persister.save(
            operator=self.OPERATOR_ID,
            payload={
                "stats": stats,
                "frames": [asdict(item) for item in detections],
            },
            output_dir=str(save_dir),
            filename=f"{artifact_stem}_hand_labels",
            meta={
                "source_path": source_path,
                "source_type": source_type,
                "num_frames": len(detections),
                "fps": fps,
                "conf_threshold": conf,
                "weights_path": self.weights_path,
                "device": self.model_wrapper.device,
            },
        )
        print(f"  Saved -> {json_path}")

        return HandPoseOperatorResult(
            frames=detections,
            json_path=json_path,
            source_path=source_path,
            source_type=source_type,
            num_frames=len(detections),
            conf_threshold=conf,
            weights_path=self.weights_path,
            fps=fps,
            stats=stats,
        )

    # ── 统计 ──────────────────────────────────────────────────────────

    @staticmethod
    def _print_stats(stats: dict) -> None:
        print(
            f"  Left: {stats['left_count']}, Right: {stats['right_count']}, "
            f"Both: {stats['both_count']}, None: {stats['none_count']}"
        )

    @staticmethod
    def get_stats(results: Union[List[HandDetectionResult], HandPoseOperatorResult]) -> dict:
        """统计检测结果中的左右手出现频次。"""
        frames = results.frames if isinstance(results, HandPoseOperatorResult) else results
        left_count = sum(1 for r in frames if r.left_hand_present)
        right_count = sum(1 for r in frames if r.right_hand_present)
        both_count = sum(
            1 for r in frames if r.left_hand_present and r.right_hand_present
        )
        none_count = sum(
            1 for r in frames if not r.left_hand_present and not r.right_hand_present
        )
        return {
            "total_frames": len(frames),
            "left_count": left_count,
            "right_count": right_count,
            "both_count": both_count,
            "none_count": none_count,
        }


# ── 数据源读取工具（仅接受本地路径，平台 I/O 由上层负责）──────────────

def _read_video_frames(video_path: Path) -> tuple:
    """逐帧读取视频，返回 BGR 图像列表及元信息。"""
    import cv2

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {video_path}")

    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
    frames = []
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frames.append(frame)
    cap.release()

    if not frames:
        raise RuntimeError(f"No frames read from: {video_path}")

    return frames, {"num_frames": len(frames), "fps": fps}


def _read_hdf5_frames(hdf5_path: Path) -> tuple:
    """读取 HDF5 中所有帧图像及元信息。"""
    import h5py
    import cv2
    import numpy as np

    with h5py.File(hdf5_path, "r") as file:
        jpeg_data = file["frame"]["head"][:]
        total = len(jpeg_data)
        frames = []
        for i in range(total):
            buffer = bytes(jpeg_data[i])
            arr = np.frombuffer(buffer, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is None:
                raise ValueError(f"Failed to decode JPEG frame at index {i}")
            frames.append(img)

        meta = {
            "num_frames": total,
            "fps": float(file["meta_info"]["fps"][()]),
        }

    return frames, meta
