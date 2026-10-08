"""Image acceleration: OpenCV YUY2 -> JPEG (aligned with droid_dataset/atom_mcap_rerun implementation)."""

from __future__ import annotations

from typing import Any

import numpy as np

_cv2_mod = None


def _get_cv2():
    global _cv2_mod
    if _cv2_mod is None:
        try:
            import cv2

            _cv2_mod = cv2
        except ImportError:
            import sys

            for extra in ("/usr/lib/python3/dist-packages",):
                if extra not in sys.path:
                    sys.path.append(extra)
            try:
                import cv2

                _cv2_mod = cv2
            except ImportError:
                _cv2_mod = False
    return _cv2_mod if _cv2_mod is not False else None


def ros_image_to_jpeg(msg: Any) -> tuple[bytes, int, int]:
    """ROS sensor_msgs/Image -> JPEG bytes (prioritize OpenCV, same logic as droid_dataset)."""
    width = int(msg.width)
    height = int(msg.height)
    encoding = (msg.encoding or "").lower()
    data = bytes(msg.data)

    if encoding in ("jpeg", "jpg", "mjpeg"):
        return data, width, height

    cv = _get_cv2()
    if cv is not None:
        buf = np.frombuffer(data, dtype=np.uint8)
        if encoding == "yuv422_yuy2":
            arr = buf.reshape((height, width, 2))
            bgr = cv.cvtColor(arr, cv.COLOR_YUV2BGR_YUY2)
            ok, encoded = cv.imencode(".jpg", bgr, [int(cv.IMWRITE_JPEG_QUALITY), 85])
            if ok:
                return bytes(encoded), width, height
        if encoding == "rgb8":
            arr = buf.reshape((height, width, 3))
            bgr = cv.cvtColor(arr, cv.COLOR_RGB2BGR)
            ok, encoded = cv.imencode(".jpg", bgr, [int(cv.IMWRITE_JPEG_QUALITY), 85])
            if ok:
                return bytes(encoded), width, height
        if encoding == "bgr8":
            arr = buf.reshape((height, width, 3))
            ok, encoded = cv.imencode(".jpg", arr, [int(cv.IMWRITE_JPEG_QUALITY), 85])
            if ok:
                return bytes(encoded), width, height

    from qrdf.utils.image import encode_jpeg

    if encoding == "rgb8":
        rgb = np.frombuffer(data, dtype=np.uint8).reshape(height, width, 3)
        return encode_jpeg(rgb), width, height
    if encoding == "bgr8":
        bgr = np.frombuffer(data, dtype=np.uint8).reshape(height, width, 3)
        return encode_jpeg(bgr[:, :, ::-1]), width, height
    if encoding == "yuv422_yuy2":
        yuy2 = np.frombuffer(data, dtype=np.uint8).reshape(height, width, 2)
        y0 = yuy2[:, :, 0].astype(np.float32)
        u = yuy2[:, ::2, 1].astype(np.float32) - 128.0
        v = yuy2[:, 1::2, 1].astype(np.float32) - 128.0
        u = np.repeat(u, 2, axis=1)[:, :width]
        v = np.repeat(v, 2, axis=1)[:, :width]
        r = np.clip(y0 + 1.402 * v, 0, 255)
        g = np.clip(y0 - 0.344 * u - 0.714 * v, 0, 255)
        b = np.clip(y0 + 1.772 * u, 0, 255)
        rgb = np.stack([r, g, b], axis=-1).astype(np.uint8)
        return encode_jpeg(rgb), width, height

    raise ValueError(f"不支持的图像编码: {encoding}")
