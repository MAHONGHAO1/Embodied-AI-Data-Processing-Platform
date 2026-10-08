"""
语义标注库：轨迹覆盖与接触表渲染算子。
负责在画面上渲染手部轨迹，降低 VLM 在复杂长序列下的理解难度与时序幻觉。
"""
import os
from typing import List, Tuple

import cv2
import numpy as np

class PromptBuilder:
    """
    视觉 Prompt 构建器，提供 3D 轨迹重投影与覆盖渲染能力。
    """
    
    def __init__(self, focal_px: float):
        """
        :param focal_px: 相机焦距 (用于将 3D 轨迹重投影到 2D 图像平面)
        """
        self.focal_px = focal_px
        # 定义双手的渲染颜色 (BGR)
        self.colors = {
            "left": (255, 80, 0),   # 蓝色偏浅
            "right": (0, 0, 255)    # 红色
        }
        
    def _project_3d_to_2d(
        self,
        points_3d: np.ndarray,
        width: int,
        height: int,
    ) -> List[Tuple[int, int]]:
        """
        利用简单的针孔相机模型，将 3D 轨迹点重投影到 2D 像素坐标。
        """
        result = []
        for x, y, z in points_3d:
            if z > 1e-3:  # 避免除以 0 或负深度
                px = int(self.focal_px * x / z + width / 2.0)
                py = int(self.focal_px * y / z + height / 2.0)
                result.append((px, py))
        return result

    def render_trajectory_overlay(
        self,
        frame_paths: List[str],
        left_track_3d: np.ndarray,
        right_track_3d: np.ndarray,
        output_dir: str,
    ) -> List[str]:
        """
        在连续帧上渲染手部 3D 轨迹投影，增强 VLM 对动作连贯性的感知。
        
        :param frame_paths: 原始图像帧绝对路径列表
        :param left_track_3d: 左手 3D 轨迹数组，形状为 [N, 3]
        :param right_track_3d: 右手 3D 轨迹数组，形状为 [N, 3]
        :param output_dir: 渲染后的图像输出目录
        :return: 渲染后的图像绝对路径列表
        """
        if self.focal_px <= 0:
            print("WARNING: 焦距无效，跳过轨迹渲染。")
            return frame_paths
            
        os.makedirs(output_dir, exist_ok=True)
        rendered_paths = []
        
        tracks = {"left": left_track_3d, "right": right_track_3d}
        
        for offset, path in enumerate(frame_paths):
            if not os.path.exists(path):
                continue
                
            image = cv2.imread(path)
            if image is None:
                continue
                
            h, w = image.shape[:2]
            
            # 渲染左右手轨迹
            for hand_type, color in self.colors.items():
                track = tracks[hand_type]
                if track is None or track.size == 0 or track.ndim != 2:
                    continue
                    
                projected = self._project_3d_to_2d(track, w, h)
                
                # 绘制轨迹连线
                for pt1, pt2 in zip(projected[:-1], projected[1:]):
                    cv2.line(image, pt1, pt2, color, 2, cv2.LINE_AA)
                    
                # 绘制当前帧所在的轨迹点位置 (高亮大圆点)
                # 假设轨迹长度与帧序列对齐
                if 0 <= offset < len(track):
                    current_pt = self._project_3d_to_2d(track[offset:offset + 1], w, h)
                    if current_pt:
                        cv2.circle(image, current_pt[0], 6, color, -1, cv2.LINE_AA)
                        
            out_name = f"rendered_{os.path.basename(path)}"
            out_path = os.path.join(output_dir, out_name)
            cv2.imwrite(out_path, image)
            rendered_paths.append(out_path)
            
        print(f"INFO: [Prompt Builder] 成功渲染了 {len(rendered_paths)} 帧的物理轨迹")
        return rendered_paths
