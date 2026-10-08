"""
QualityInspector 长视频极速质量雷达的集成测试用例。
验证：结构化契约返回 + JsonResultPersister 落盘。
"""
import sys
import json
import tempfile
import unittest
import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

INPUT_VIDEO_PATH = PROJECT_ROOT / "data/episode_000002/media/preview/generations/47f482ec5a1e7e3b/head_rgb.mp4"


class TestQualityInspector(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if not INPUT_VIDEO_PATH.exists():
            raise unittest.SkipTest(f"找不到测试输入视频，跳过测试: {INPUT_VIDEO_PATH}")
        if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
            raise unittest.SkipTest("缺少 ffmpeg 依赖，跳过测试")

    def test_inspect_video_returns_bundle_and_json(self):
        from quic_op.clean import QualityInspector
        from quic_op.core.data_types import (
            LowLightOperatorResult,
            BlurOperatorResult,
            FrameDropOperatorResult,
        )
        from quic_op.core.persistence import PERSISTENCE_SCHEMA_VERSION

        print("\nINFO: 开始执行低光/模糊/掉帧 QC（结构化 + 持久化）...")
        inspector = QualityInspector()

        with tempfile.TemporaryDirectory() as tmp:
            lowlight = inspector.detect_lowlight_metrics(
                video_path=str(INPUT_VIDEO_PATH),
                light_mean_threshold=35.0,
                dark_pixel_thresh=30,
                dark_area_ratio=0.28,
                sample_fps=5,
                scale_height=360,
                output_dir=tmp,
            )
            blur = inspector.detect_blur_metrics(
                video_path=str(INPUT_VIDEO_PATH),
                blur_threshold=22.0,
                sample_fps=5,
                scale_height=360,
                output_dir=tmp,
            )
            drops = inspector.detect_frame_drop_metrics(
                video_path=str(INPUT_VIDEO_PATH),
                expected_fps=15.0,
                output_dir=tmp,
            )

            self.assertIsInstance(lowlight, LowLightOperatorResult)
            self.assertIsInstance(blur, BlurOperatorResult)
            self.assertIsInstance(drops, FrameDropOperatorResult)

            for result, key in (
                (lowlight, "samples"),
                (blur, "samples"),
                (drops, "events"),
            ):
                self.assertTrue(Path(result.json_path).is_file())
                with open(result.json_path, "r", encoding="utf-8") as file:
                    document = json.load(file)
                self.assertEqual(document["schema_version"], PERSISTENCE_SCHEMA_VERSION)
                self.assertIn(key, document)
                self.assertIn("stats", document)

            print(f"\nINFO: 检测完毕！")
            print(f"  - lowlight: {lowlight.num_samples} samples -> {lowlight.json_path}")
            print(f"  - blur: {blur.num_samples} samples -> {blur.json_path}")
            print(f"  - frame_drop: {drops.num_events} events -> {drops.json_path}")

            if blur.samples:
                print("\nINFO: 模糊算子示例输出（前 5 条）")
                for i, m in enumerate(blur.samples[:5]):
                    print(
                        f"  [{i+1:02d}] t={m.t_sec:.3f}s "
                        f"laplacian_var={m.laplacian_var:.3f} blur={m.blur_flag}"
                    )


if __name__ == "__main__":
    unittest.main()
