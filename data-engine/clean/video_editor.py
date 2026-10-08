"""
图像与视频基础清洗处理模块。
负责抽帧、切片、花屏死区过滤等纯 CPU/IO 密集型任务。
"""
import os
import re
import json
import shutil
import tempfile
import subprocess
import numpy as np
from typing import List

from quic_op.core.exceptions import VideoCorruptedException

# 常量定义
DEFAULT_SPLIT_DURATION_SEC = 15
DEFAULT_OVERLAP_SEC = 3
DEFAULT_MAX_CLIPS = -1  # 负数表示不限制
DEFAULT_FRAME_INTERVAL = 15  # 抽帧间隔，例如每15帧抽一帧

class VideoEditor:
    """
    视频编辑算子类，封装对 FFmpeg 等底层工具的调用。
    注意：所有路径必须为本地绝对路径，禁止直接耦合 OSS 逻辑。
    """
    
    def __init__(self):
        self.ffmpeg_exe = shutil.which("ffmpeg") or "ffmpeg"
        self.ffprobe_exe = shutil.which("ffprobe") or "ffprobe"

    def split_with_overlap(
        self,
        video_path: str,
        output_dir: str,
        duration_sec: int = DEFAULT_SPLIT_DURATION_SEC,
        overlap_sec: int = DEFAULT_OVERLAP_SEC,
        max_clips: int = DEFAULT_MAX_CLIPS,
    ) -> List[str]:
        """
        按指定时长与重叠时间切分视频。
        
        :param video_path: 原始视频的绝对路径
        :param output_dir: 切片输出目录的绝对路径
        :param duration_sec: 单个切片的目标时长
        :param overlap_sec: 相邻切片的重叠时长，便于下游连续性建模
        :param max_clips: 最大切片数限制
        :return: 生成的切片绝对路径列表
        """
        if not os.path.exists(video_path):
            raise VideoCorruptedException(f"视频文件不存在: {video_path}")
            
        os.makedirs(output_dir, exist_ok=True)
        clip_paths: List[str] = []
        fps = 0.0
        duration = 0.0

        # 使用 ffprobe 探测时长和帧率
        if self.ffprobe_exe:
            try:
                probe_output = subprocess.check_output(
                    [
                        self.ffprobe_exe, "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "format=duration:stream=r_frame_rate",
                        "-of", "json", video_path
                    ],
                    stderr=subprocess.STDOUT, timeout=120
                ).decode(errors="ignore")
                probe = json.loads(probe_output)
                duration = float(probe.get("format", {}).get("duration") or 0)
                rate = (probe.get("streams") or [{}])[0].get("r_frame_rate") or "0/1"
                if "/" in rate:
                    num, den = rate.split("/", 1)
                    fps = round(float(num) / max(float(den), 1.0), 3)
                else:
                    fps = round(float(rate), 3)
            except Exception as e:
                print(f"WARNING: ffprobe 探测失败: {e}，尝试回退到 ffmpeg 解析")

        # 回退使用 ffmpeg 解析
        if duration <= 0:
            try:
                probe_output = subprocess.run(
                    [self.ffmpeg_exe, "-hide_banner", "-i", video_path],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    timeout=120, check=False
                ).stdout.decode(errors="ignore")
                dur_match = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", probe_output)
                if dur_match:
                    h, m, s = dur_match.groups()
                    duration = int(h) * 3600 + int(m) * 60 + float(s)
                fps_match = re.search(r"([\d.]+)\s*fps", probe_output)
                if fps_match:
                    fps = round(float(fps_match.group(1)), 3)
            except Exception as e:
                raise VideoCorruptedException(f"无法探测视频时长或帧率: {e}")

        if duration <= 0:
            raise VideoCorruptedException("获取到的视频时长为 0 或非法")

        print(f"INFO: 视频 {video_path} 时长 {duration}s，帧率 {fps}fps。开始切分...")

        # 计算切分起始点
        step = max(duration_sec - overlap_sec, 1.0)
        starts = np.arange(0, max(duration - overlap_sec, 0) + step, step)
        if max_clips > 0:
            starts = starts[:max_clips]

        video_id = os.path.splitext(os.path.basename(video_path))[0]
        
        # 执行 ffmpeg 切片
        with tempfile.TemporaryDirectory() as tmp_dir:
            for idx, start in enumerate(starts):
                out_name = f"{video_id}_clip_{idx:04d}.mp4"
                local_tmp_path = os.path.join(tmp_dir, out_name)
                final_path = os.path.join(output_dir, out_name)
                
                try:
                    subprocess.check_call(
                        [
                            self.ffmpeg_exe, "-y", "-ss", f"{start:.2f}",
                            "-i", video_path, "-t", f"{duration_sec}",
                            local_tmp_path
                        ],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600
                    )
                    
                    if os.path.getsize(local_tmp_path) > 0:
                        shutil.copy2(local_tmp_path, final_path)
                        clip_paths.append(final_path)
                except subprocess.CalledProcessError as e:
                    print(f"WARNING: 切片 {out_name} 生成失败: {e}")
                    continue

        return clip_paths

    def extract_keyframes(
        self,
        clip_path: str,
        output_dir: str,
        frame_interval: int = DEFAULT_FRAME_INTERVAL,
    ) -> List[str]:
        """
        对视频切片进行均匀抽帧。
        
        :param clip_path: 切片视频的绝对路径
        :param output_dir: 抽帧图像的输出目录
        :param frame_interval: 抽帧间隔，如 15 表示每 15 帧抽取 1 帧
        :return: 生成的图像绝对路径列表
        """
        if not os.path.exists(clip_path):
            raise VideoCorruptedException(f"切片文件不存在: {clip_path}")
            
        os.makedirs(output_dir, exist_ok=True)
        frame_paths: List[str] = []
        
        # 清空旧产物，避免历史脏数据
        for old_name in os.listdir(output_dir):
            old_path = os.path.join(output_dir, old_name)
            if os.path.isfile(old_path):
                os.remove(old_path)

        with tempfile.TemporaryDirectory() as tmp_dir:
            try:
                subprocess.check_call(
                    [
                        self.ffmpeg_exe, "-y", "-i", clip_path,
                        "-vf", f"select='not(mod(n\\,{max(1, frame_interval)}))'",
                        "-vsync", "vfr",
                        os.path.join(tmp_dir, "frame_%05d.jpg")
                    ],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1800
                )
                
                for name in sorted(os.listdir(tmp_dir)):
                    local_path = os.path.join(tmp_dir, name)
                    if os.path.getsize(local_path) > 0:
                        final_path = os.path.join(output_dir, name)
                        shutil.copy2(local_path, final_path)
                        frame_paths.append(final_path)
            except Exception as e:
                print(f"ERROR: 抽帧失败 {clip_path}: {e}")
                
        return frame_paths

    def drop_dead_frames(self, frame_paths: List[str]) -> List[str]:
        """
        花屏与死帧过滤算法。基于纯 CPU 的基础图像校验。
        """
        import cv2
        valid_frames = []
        for path in frame_paths:
            if not os.path.exists(path):
                continue
            # 基础死区过滤：例如图像全黑检查
            img = cv2.imread(path)
            if img is not None:
                mean_val = img.mean()
                if mean_val > 2.0:  # 均值极低认为是死区/全黑帧
                    valid_frames.append(path)
        return valid_frames
