"""
SDK 统一 invoke / 能力声明冒烟测试
"""
import sys
import tempfile
import unittest
from pathlib import Path
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))


class TestSdkInvoke(unittest.TestCase):
    def test_list_perception_capabilities(self):
        import quic_op
        from quic_op.core.data_types import HAND_POSE_OPERATOR_ID

        caps = quic_op.list_capabilities(domain="perception")
        ids = {c.operator_id for c in caps}
        self.assertIn(HAND_POSE_OPERATOR_ID, ids)
        self.assertIn("perception.hand_pose_yolo.frames", ids)
        self.assertIn("perception.hand_pose_yolo.hdf5", ids)

        desc = quic_op.describe_capabilities(domain="perception")
        self.assertTrue(all("method" in item for item in desc))
    def test_list_clean_capabilities(self):
        import quic_op
        from quic_op.core.data_types import (
            LOWLIGHT_OPERATOR_ID,
            BLUR_OPERATOR_ID,
            FRAME_DROP_OPERATOR_ID,
        )

        caps = quic_op.list_capabilities(domain="clean")
        ids = {c.operator_id for c in caps}
        self.assertIn(LOWLIGHT_OPERATOR_ID, ids)
        self.assertIn(BLUR_OPERATOR_ID, ids)
        self.assertIn(FRAME_DROP_OPERATOR_ID, ids)

        desc = quic_op.describe_capabilities(domain="clean")
        self.assertTrue(all("method" in d for d in desc))

    def test_invoke_unknown_raises(self):
        import quic_op
        from quic_op.core.exceptions import CapabilityNotFoundException

        with self.assertRaises(CapabilityNotFoundException):
            quic_op.invoke("perception.not_exists", video_path="/tmp/x.mp4")

    def test_invoke_frames_mock(self):
        import quic_op
        from quic_op.core.data_types import HandPoseOperatorResult

        frames = [np.zeros((64, 64, 3), dtype=np.uint8) for _ in range(2)]
        with tempfile.TemporaryDirectory() as tmp:
            result = quic_op.invoke(
                "perception.hand_pose_yolo.frames",
                frames=frames,
                device="cpu",
                output_dir=tmp,
                artifact_name="invoke_smoke",
            )
            self.assertIsInstance(result, HandPoseOperatorResult)
            self.assertEqual(result.num_frames, 2)
            quic_op.invoke("clean.not_exists", video_path="/tmp/x.mp4")

    def test_invoke_lowlight_if_video_available(self):
        import shutil
        import quic_op
        from quic_op.core.data_types import (
            LOWLIGHT_OPERATOR_ID,
            LowLightOperatorResult,
        )

        video = PROJECT_ROOT / "data/episode_000002/media/preview/generations/47f482ec5a1e7e3b/head_rgb.mp4"
        alt = Path("/home/whisper/Data/file-000.mp4")
        path = video if video.exists() else alt
        if not path.exists():
            self.skipTest("无可用测试视频")
        if shutil.which("ffmpeg") is None:
            self.skipTest("缺少 ffmpeg")

        with tempfile.TemporaryDirectory() as tmp:
            result = quic_op.invoke(
                LOWLIGHT_OPERATOR_ID,
                video_path=str(path),
                sample_fps=2,
                scale_height=240,
                output_dir=tmp,
            )
            self.assertIsInstance(result, LowLightOperatorResult)
            self.assertTrue(Path(result.json_path).is_file())


if __name__ == "__main__":
    unittest.main()
