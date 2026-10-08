"""GPU / NVENC capability detection (QuicData platform layer, without modifying vendor/qrdf)."""

from __future__ import annotations

import subprocess
from functools import lru_cache


@lru_cache(maxsize=1)
def nvidia_smi_available() -> bool:
    try:
        proc = subprocess.run(
            ["nvidia-smi", "-L"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return proc.returncode == 0 and "GPU" in (proc.stdout or "")
    except Exception:
        return False


@lru_cache(maxsize=1)
def gpu_device_names() -> list[str]:
    if not nvidia_smi_available():
        return []
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if proc.returncode != 0:
            return []
        return [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    except Exception:
        return []


@lru_cache(maxsize=1)
def imageio_ffmpeg_nvenc_available() -> bool:
    """Detect whether ffmpeg invoked by imageio supports NVENC."""
    try:
        import imageio_ffmpeg
    except ImportError:
        return False
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    if not ffmpeg:
        return False
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        text = (proc.stdout or "") + (proc.stderr or "")
        return "h264_nvenc" in text
    except Exception:
        return False


@lru_cache(maxsize=1)
def opencv_cuda_available() -> bool:
    try:
        import cv2

        return hasattr(cv2, "cuda") and cv2.cuda.getCudaEnabledDeviceCount() > 0
    except Exception:
        return False


def gpu_status_report() -> dict:
    """Return GPU availability diagnostic information."""
    names = gpu_device_names()
    return {
        "nvidia_smi": nvidia_smi_available(),
        "gpu_devices": names,
        "opencv_cuda": opencv_cuda_available(),
        "ffmpeg_nvenc": imageio_ffmpeg_nvenc_available(),
        "note": (
            "pip 安装的 opencv-python-headless 不含 CUDA；"
            "图像 YUY2→JPEG 默认走 CPU 多线程。"
            "preview 可尝试 h264_nvenc 硬件编码。"
        ),
    }
