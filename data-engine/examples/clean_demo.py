"""
clean 模块最小可运行示例。

本脚本面向下游同事，展示如何仅依赖 clean 模块完成：
1. 视频切片
2. 均匀抽帧
3. 死帧过滤
4. 去畸变预览图生成

默认输入：
    data/episode_000002/media/preview/generations/47f482ec5a1e7e3b/head_rgb.mp4

默认输出：
    data/out/clean_demo_case/
"""
import argparse
import importlib.util
import shutil
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PROJECT_ROOT.parent

# 允许直接通过 `python examples/clean_demo.py` 运行。
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))


DEFAULT_INPUT_VIDEO_PATH = (
    PROJECT_ROOT
    / "data/episode_000002/media/preview/generations/47f482ec5a1e7e3b/head_rgb.mp4"
)
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "data/out/clean_demo_case"


def build_parser() -> argparse.ArgumentParser:
    """
    构建命令行参数解析器。
    """
    parser = argparse.ArgumentParser(description="运行 clean 模块最小示例")
    parser.add_argument(
        "--input-video",
        default=str(DEFAULT_INPUT_VIDEO_PATH),
        help="输入视频绝对路径或相对路径",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_ROOT),
        help="输出目录绝对路径或相对路径",
    )
    parser.add_argument(
        "--duration-sec",
        type=int,
        default=5,
        help="单个切片时长（秒）",
    )
    parser.add_argument(
        "--overlap-sec",
        type=int,
        default=1,
        help="相邻切片重叠时长（秒）",
    )
    parser.add_argument(
        "--frame-interval",
        type=int,
        default=30,
        help="抽帧间隔，例如 30 表示每 30 帧抽一帧",
    )
    parser.add_argument(
        "--max-clips",
        type=int,
        default=5,
        help="最多处理多少个切片",
    )
    return parser


def ensure_runtime_dependencies() -> None:
    """
    检查 clean demo 所需的运行环境。
    """
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        raise RuntimeError("当前环境缺少 ffmpeg 或 ffprobe，请先安装系统依赖")

    if importlib.util.find_spec("cv2") is None:
        raise RuntimeError("当前环境缺少 opencv-python，请先执行 `pip install opencv-python`")


def run_clean_demo(
    input_video_path: Path,
    output_root: Path,
    duration_sec: int,
    overlap_sec: int,
    frame_interval: int,
    max_clips: int,
) -> None:
    """
    执行 clean 模块完整示例流程。
    """
    from quic_op.clean import GeometryProcessor, VideoEditor
    import cv2

    if not input_video_path.exists():
        raise FileNotFoundError(f"找不到输入视频: {input_video_path}")

    clip_output_dir = output_root / "clips"
    frame_output_dir = output_root / "frames"
    undistort_output_path = output_root / "undistorted_preview.jpg"

    output_root.mkdir(parents=True, exist_ok=True)
    clip_output_dir.mkdir(parents=True, exist_ok=True)
    frame_output_dir.mkdir(parents=True, exist_ok=True)

    video_editor = VideoEditor()

    print("=" * 60)
    print("启动 clean 模块示例")
    print("=" * 60)
    print(f"输入视频: {input_video_path}")
    print(f"输出目录: {output_root}")

    clip_paths = video_editor.split_with_overlap(
        video_path=str(input_video_path),
        output_dir=str(clip_output_dir),
        duration_sec=duration_sec,
        overlap_sec=overlap_sec,
        max_clips=max_clips,
    )
    if not clip_paths:
        raise RuntimeError("视频切片结果为空")

    clip_path = clip_paths[0]
    per_clip_frame_dir = frame_output_dir / Path(clip_path).stem
    frame_paths = video_editor.extract_keyframes(
        clip_path=clip_path,
        output_dir=str(per_clip_frame_dir),
        frame_interval=frame_interval,
    )
    if not frame_paths:
        raise RuntimeError("抽帧结果为空")

    valid_frame_paths = video_editor.drop_dead_frames(frame_paths)
    if not valid_frame_paths:
        raise RuntimeError("有效帧过滤结果为空")

    first_frame = cv2.imread(valid_frame_paths[0])
    if first_frame is None:
        raise RuntimeError(f"首帧读取失败: {valid_frame_paths[0]}")

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

    if not cv2.imwrite(str(undistort_output_path), undistorted_image):
        raise RuntimeError(f"去畸变图像写出失败: {undistort_output_path}")

    print(f"切片数量: {len(clip_paths)}")
    print(f"抽帧数量: {len(frame_paths)}")
    print(f"有效帧数量: {len(valid_frame_paths)}")
    print(f"去畸变预览图: {undistort_output_path}")
    print("=" * 60)
    print("clean 模块示例执行完成")
    print("=" * 60)


def main() -> None:
    """
    clean demo 启动入口。
    """
    parser = build_parser()
    args = parser.parse_args()

    ensure_runtime_dependencies()
    run_clean_demo(
        input_video_path=Path(args.input_video).expanduser().resolve(),
        output_root=Path(args.output_dir).expanduser().resolve(),
        duration_sec=args.duration_sec,
        overlap_sec=args.overlap_sec,
        frame_interval=args.frame_interval,
        max_clips=args.max_clips,
    )


if __name__ == "__main__":
    main()
