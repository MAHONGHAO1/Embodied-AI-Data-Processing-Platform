"""
clean 模块集成测试用例。

本测试会使用真实视频输入，对以下能力进行验证：
1. 视频切片
2. 均匀抽帧
3. 死帧过滤
4. 几何去畸变示例输出

测试产物统一写入 data/out/clean_module_case/
"""
import sys
import shutil
import unittest
import importlib.util
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_ROOT.parent

# 兼容直接执行测试文件的场景，让 Python 能找到 quic_op 包。
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

INPUT_VIDEO_PATH = PROJECT_ROOT / "data/episode_000002/media/preview/generations/47f482ec5a1e7e3b/head_rgb.mp4"
OUTPUT_ROOT = PROJECT_ROOT / "data/out/clean_module_case"
CLIP_OUTPUT_DIR = OUTPUT_ROOT / "clips"
FRAME_OUTPUT_DIR = OUTPUT_ROOT / "frames"
UNDISTORT_OUTPUT_PATH = OUTPUT_ROOT / "undistorted_preview.jpg"


class TestCleanModule(unittest.TestCase):
    """
    clean 模块真实输入集成测试。
    """

    @classmethod
    def setUpClass(cls):
        """
        初始化测试输入与输出目录。
        """
        if not INPUT_VIDEO_PATH.exists():
            raise unittest.SkipTest(f"找不到测试输入视频，跳过 clean 模块集成测试: {INPUT_VIDEO_PATH}")

        if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
            raise unittest.SkipTest("当前环境缺少 ffmpeg 或 ffprobe，跳过 clean 模块集成测试")

        if importlib.util.find_spec("cv2") is None:
            raise unittest.SkipTest("当前环境缺少 opencv-python，跳过 clean 模块集成测试")

        if OUTPUT_ROOT.exists():
            shutil.rmtree(OUTPUT_ROOT)

        CLIP_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        FRAME_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    def test_clean_pipeline(self):
        """
        验证 clean 模块的主流程可运行，并产出预期文件。
        """
        # 直接使用相对路径导入，避免包名冲突
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from clean import GeometryProcessor, VideoEditor

        video_editor = VideoEditor()

        clip_paths = video_editor.split_with_overlap(
            video_path=str(INPUT_VIDEO_PATH),
            output_dir=str(CLIP_OUTPUT_DIR),
            duration_sec=5,
            overlap_sec=1,
            max_clips=5, #最大切片数限制
        )
        self.assertGreater(len(clip_paths), 0, "视频切片结果为空")

        clip_path = clip_paths[0]
        per_clip_frame_dir = FRAME_OUTPUT_DIR / Path(clip_path).stem
        frame_paths = video_editor.extract_keyframes(
            clip_path=clip_path,
            output_dir=str(per_clip_frame_dir),
            frame_interval=30,
        )
        self.assertGreater(len(frame_paths), 0, "抽帧结果为空")

        valid_frame_paths = video_editor.drop_dead_frames(frame_paths)
        self.assertGreater(len(valid_frame_paths), 0, "有效帧过滤结果为空")

        import cv2

        first_frame = cv2.imread(valid_frame_paths[0])
        self.assertIsNotNone(first_frame, "首帧读取失败")

        height, width = first_frame.shape[:2]
        intrinsics = GeometryProcessor.build_isotropic_intrinsics(
            focal_px=max(width, height) * 0.8,
            k1=-0.15,
            width=width,
            height=height,
        )
        undistorted_image = GeometryProcessor.undistort_image(
            image_array=first_frame,
            intrinsics=intrinsics,
            crop=False,
        )

        success = cv2.imwrite(str(UNDISTORT_OUTPUT_PATH), undistorted_image)
        self.assertTrue(success, "去畸变预览图写出失败")
        self.assertTrue(UNDISTORT_OUTPUT_PATH.exists(), "去畸变预览图不存在")

        print(f"INFO: clean 测试输出目录: {OUTPUT_ROOT}")
        print(f"INFO: 生成切片数量: {len(clip_paths)}")
        print(f"INFO: 生成抽帧数量: {len(frame_paths)}")
        print(f"INFO: 有效帧数量: {len(valid_frame_paths)}")
        print(f"INFO: 去畸变预览图: {UNDISTORT_OUTPUT_PATH}")


if __name__ == "__main__":
    unittest.main()
