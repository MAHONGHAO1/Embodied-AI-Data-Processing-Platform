"""
视频长时质量检测算子门面模块。
负责：本地视频路径入参 → 极速降维/PTS 质检 → 组装 core 契约 → 统一 JSON 持久化。
核心原则：严禁全尺寸逐帧解码，依赖 ffmpeg/ffprobe 管道流。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import List, Optional

import cv2
import numpy as np

from quic_op.core.data_types import (
    BLUR_OPERATOR_ID,
    FRAME_DROP_OPERATOR_ID,
    LOWLIGHT_OPERATOR_ID,
    BlurOperatorResult,
    BlurSample,
    FrameDropEvent,
    FrameDropOperatorResult,
    LowLightOperatorResult,
    LowLightSample,
)
from quic_op.core.exceptions import VideoCorruptedException
from quic_op.core.persistence import JsonResultPersister

# 默认落盘目录（项目根目录下 output/clean）
_DEFAULT_OUTPUT_ROOT = Path(__file__).resolve().parent.parent / "output" / "clean"

# 算子版本（与 feature/pose 同构：版本在包内，pyproject 可 attr 引用）
__version__ = "0.1.0"


class QualityInspector:
    """
    长视频物理画质与时序检测门禁类。
    对外仅接受本地绝对路径；每次调用返回结构化契约 + JSON 绝对路径。
    """

    VERSION = __version__

    def __init__(self, persister: Optional[JsonResultPersister] = None):
        self.persister = persister or JsonResultPersister()

    # ── 公开入口 ──────────────────────────────────────────────────────

    def detect_lowlight_metrics(
        self,
        video_path: str,
        light_mean_threshold: float = 35.0,
        dark_pixel_thresh: int = 30,
        dark_area_ratio: float = 0.28,
        sample_fps: int = 5,
        scale_height: int = 360,
        output_dir: Optional[str] = None,
    ) -> LowLightOperatorResult:
        """
        低光检测（逐采样帧 QC 明细）。

        :return: LowLightOperatorResult（含 samples 与 json_path）
        """
        source = Path(video_path).expanduser().resolve()
        if not source.exists():
            raise VideoCorruptedException(f"视频文件不存在: {source}")

        samples: List[LowLightSample] = []
        for t_sec, gray_frame in self._extract_video_stream(
            str(source), sample_fps, scale_height
        ):
            mean_gray = float(gray_frame.mean())
            dark_pixel_ratio = float((gray_frame < int(dark_pixel_thresh)).mean())
            low_light_flag = (mean_gray < float(light_mean_threshold)) or (
                dark_pixel_ratio > float(dark_area_ratio)
            )
            samples.append(
                LowLightSample(
                    t_sec=float(t_sec),
                    mean_gray=mean_gray,
                    dark_pixel_ratio=dark_pixel_ratio,
                    low_light_flag=bool(low_light_flag),
                )
            )

        stats = {
            "total_samples": len(samples),
            "flagged_count": sum(1 for s in samples if s.low_light_flag),
        }
        save_dir = (
            Path(output_dir).expanduser().resolve()
            if output_dir
            else _DEFAULT_OUTPUT_ROOT / "lowlight"
        )
        json_path = self.persister.save(
            operator=LOWLIGHT_OPERATOR_ID,
            payload={
                "stats": stats,
                "samples": [asdict(s) for s in samples],
            },
            output_dir=str(save_dir),
            filename=f"{source.stem}_lowlight",
            meta={
                "source_path": str(source),
                "source_type": "video",
                "num_samples": len(samples),
                "light_mean_threshold": float(light_mean_threshold),
                "dark_pixel_thresh": int(dark_pixel_thresh),
                "dark_area_ratio": float(dark_area_ratio),
                "sample_fps": int(sample_fps),
                "scale_height": int(scale_height),
            },
        )
        print(f"  [lowlight] samples={stats['total_samples']} flagged={stats['flagged_count']}")
        print(f"  Saved -> {json_path}")

        return LowLightOperatorResult(
            samples=samples,
            json_path=json_path,
            source_path=str(source),
            num_samples=len(samples),
            stats=stats,
        )

    def detect_blur_metrics(
        self,
        video_path: str,
        blur_threshold: float = 22.0,
        sample_fps: int = 5,
        scale_height: int = 360,
        output_dir: Optional[str] = None,
    ) -> BlurOperatorResult:
        """
        模糊检测（逐采样帧 QC 明细）。

        :return: BlurOperatorResult（含 samples 与 json_path）
        """
        source = Path(video_path).expanduser().resolve()
        if not source.exists():
            raise VideoCorruptedException(f"视频文件不存在: {source}")

        samples: List[BlurSample] = []
        for t_sec, gray_frame in self._extract_video_stream(
            str(source), sample_fps, scale_height
        ):
            laplacian_var = float(cv2.Laplacian(gray_frame, cv2.CV_64F).var())
            blur_flag = laplacian_var < float(blur_threshold)
            samples.append(
                BlurSample(
                    t_sec=float(t_sec),
                    laplacian_var=laplacian_var,
                    blur_flag=bool(blur_flag),
                )
            )

        stats = {
            "total_samples": len(samples),
            "flagged_count": sum(1 for s in samples if s.blur_flag),
        }
        save_dir = (
            Path(output_dir).expanduser().resolve()
            if output_dir
            else _DEFAULT_OUTPUT_ROOT / "blur"
        )
        json_path = self.persister.save(
            operator=BLUR_OPERATOR_ID,
            payload={
                "stats": stats,
                "samples": [asdict(s) for s in samples],
            },
            output_dir=str(save_dir),
            filename=f"{source.stem}_blur",
            meta={
                "source_path": str(source),
                "source_type": "video",
                "num_samples": len(samples),
                "blur_threshold": float(blur_threshold),
                "sample_fps": int(sample_fps),
                "scale_height": int(scale_height),
            },
        )
        print(f"  [blur] samples={stats['total_samples']} flagged={stats['flagged_count']}")
        print(f"  Saved -> {json_path}")

        return BlurOperatorResult(
            samples=samples,
            json_path=json_path,
            source_path=str(source),
            num_samples=len(samples),
            stats=stats,
        )

    def detect_frame_drop_metrics(
        self,
        video_path: str,
        expected_fps: float = 15.0,
        drop_threshold_sec: Optional[float] = None,
        timeout_sec: int = 600,
        output_dir: Optional[str] = None,
    ) -> FrameDropOperatorResult:
        """
        掉帧检测（事件明细，基于 PTS，不解码帧）。

        :return: FrameDropOperatorResult（含 events 与 json_path）
        """
        source = Path(video_path).expanduser().resolve()
        if not source.exists():
            raise VideoCorruptedException(f"视频文件不存在: {source}")

        expected_fps = float(expected_fps)
        if expected_fps <= 0:
            expected_fps = self._probe_fps(
                video_path=str(source), timeout_sec=timeout_sec
            )

        if expected_fps <= 0:
            raise VideoCorruptedException("无法获取有效帧率进行掉帧检测")

        expected_interval = 1.0 / expected_fps
        resolved_threshold = (
            float(drop_threshold_sec)
            if drop_threshold_sec is not None
            else expected_interval * 1.35
        )

        timestamps = self._probe_timestamps(
            video_path=str(source), timeout_sec=timeout_sec
        )
        events: List[FrameDropEvent] = []

        for i in range(1, len(timestamps)):
            prev_ts = float(timestamps[i - 1])
            curr_ts = float(timestamps[i])
            diff = curr_ts - prev_ts
            if diff <= 0:
                continue
            if diff <= resolved_threshold:
                continue

            drop_frame_count = int(round(diff / expected_interval) - 1)
            events.append(
                FrameDropEvent(
                    t_prev_sec=prev_ts,
                    t_curr_sec=curr_ts,
                    frame_interval_ms=float(diff) * 1000.0,
                    drop_frame_count=max(drop_frame_count, 0),
                    frame_drop_flag=True,
                )
            )

        stats = {
            "total_events": len(events),
            "dropped_frames": sum(e.drop_frame_count for e in events),
            "probed_timestamps": len(timestamps),
        }
        save_dir = (
            Path(output_dir).expanduser().resolve()
            if output_dir
            else _DEFAULT_OUTPUT_ROOT / "frame_drop"
        )
        json_path = self.persister.save(
            operator=FRAME_DROP_OPERATOR_ID,
            payload={
                "stats": stats,
                "events": [asdict(e) for e in events],
            },
            output_dir=str(save_dir),
            filename=f"{source.stem}_frame_drop",
            meta={
                "source_path": str(source),
                "source_type": "video",
                "num_events": len(events),
                "expected_fps": expected_fps,
                "drop_threshold_sec": resolved_threshold,
                "timeout_sec": int(timeout_sec),
            },
        )
        print(
            f"  [frame_drop] events={stats['total_events']} "
            f"dropped_frames={stats['dropped_frames']}"
        )
        print(f"  Saved -> {json_path}")

        return FrameDropOperatorResult(
            events=events,
            json_path=json_path,
            source_path=str(source),
            num_events=len(events),
            expected_fps=expected_fps,
            drop_threshold_sec=resolved_threshold,
            stats=stats,
        )

    # ── 内部工具 ──────────────────────────────────────────────────────

    @classmethod
    def _probe_fps(cls, video_path: str, timeout_sec: int) -> float:
        ffprobe_exe = shutil.which("ffprobe") or "ffprobe"

        try:
            probe_cmd = [
                ffprobe_exe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=r_frame_rate",
                "-of",
                "csv=p=0",
                video_path,
            ]
            rate_str = subprocess.check_output(
                probe_cmd, text=True, timeout=timeout_sec
            ).strip()
            if "/" in rate_str:
                num, den = rate_str.split("/", 1)
                return float(num) / max(float(den), 1.0)
            return float(rate_str)
        except Exception:
            return 0.0

    @classmethod
    def _probe_timestamps(cls, video_path: str, timeout_sec: int) -> List[float]:
        ffprobe_exe = shutil.which("ffprobe") or "ffprobe"

        def _parse(raw: str) -> List[float]:
            timestamps: List[float] = []
            for line in raw.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    timestamps.append(float(line))
                except ValueError:
                    continue
            return timestamps

        try:
            ts_cmd = [
                ffprobe_exe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "frame=best_effort_timestamp_time",
                "-of",
                "csv=p=0",
                video_path,
            ]
            ts_output = subprocess.check_output(ts_cmd, text=True, timeout=timeout_sec)
            timestamps = _parse(ts_output)

            if not timestamps:
                ts_cmd[6] = "frame=pkt_pts_time"
                ts_output = subprocess.check_output(
                    ts_cmd, text=True, timeout=timeout_sec
                )
                timestamps = _parse(ts_output)
        except subprocess.CalledProcessError as e:
            print(f"WARNING: ffprobe 提取时间戳失败: {e}")
            return []

        return timestamps

    @classmethod
    def _extract_video_stream(cls, video_path: str, sample_fps: int, scale_height: int):
        """内部方法：拉起 ffmpeg 极速降维管道，并 yield 灰度帧。"""
        ffmpeg_exe = shutil.which("ffmpeg") or "ffmpeg"
        ffprobe_exe = shutil.which("ffprobe") or "ffprobe"

        try:
            probe_cmd = [
                ffprobe_exe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "csv=p=0",
                video_path,
            ]
            w_h = subprocess.check_output(probe_cmd, text=True).strip().split(",")
            orig_w, orig_h = int(w_h[0]), int(w_h[1])
            scale_width = int(orig_w * (scale_height / orig_h)) & ~1
        except Exception as e:
            print(f"WARNING: 无法探测分辨率，使用强制 640x360 降维: {e}")
            scale_width, scale_height = 640, 360

        cmd = [
            ffmpeg_exe,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            video_path,
            "-vf",
            f"fps={sample_fps},scale={scale_width}:{scale_height}",
            "-sws_flags",
            "fast_bilinear",
            "-f",
            "image2pipe",
            "-pix_fmt",
            "gray",
            "-vcodec",
            "rawvideo",
            "-",
        ]

        frame_size = scale_width * scale_height
        frame_idx = 0

        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=10**8
        )

        try:
            while True:
                raw_bytes = process.stdout.read(frame_size)
                if not raw_bytes or len(raw_bytes) != frame_size:
                    break

                current_time = frame_idx / sample_fps
                gray_frame = np.frombuffer(raw_bytes, dtype=np.uint8).reshape(
                    (scale_height, scale_width)
                )

                yield current_time, gray_frame
                frame_idx += 1
        finally:
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                process.stderr.close()
            process.terminate()
            process.wait()
