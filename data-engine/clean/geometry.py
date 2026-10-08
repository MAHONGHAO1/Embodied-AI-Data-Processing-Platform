"""
物理畸变矫正与几何处理基础算子。
属于清洗阶段 (clean)，仅包含纯 CPU 计算和 OpenCV 操作，不含模型推理。
模型推理 (如 GeoCalib) 位于 sync 库中。
"""
from typing import Tuple

import cv2
import numpy as np

from quic_op.core.data_types import CameraIntrinsics

class GeometryProcessor:
    """
    几何预处理算子类，负责构建相机内参矩阵并执行畸变矫正。
    """
    
    @staticmethod
    def build_isotropic_intrinsics(
        focal_px: float,
        k1: float,
        width: int,
        height: int,
    ) -> CameraIntrinsics:
        """
        基于等距假设 (fx=fy=focal_px) 构建相机内参实体。
        
        :param focal_px: 焦距 (像素单位)
        :param k1: 径向畸变系数
        :param width: 图像宽度
        :param height: 图像高度
        :return: CameraIntrinsics 实例
        """
        cx = width * 0.5
        cy = height * 0.5
        
        return CameraIntrinsics(
            focal_length_x=float(focal_px),
            focal_length_y=float(focal_px),
            principal_point_x=float(cx),
            principal_point_y=float(cy),
            k1=float(k1)
        )

    @staticmethod
    def get_opencv_matrices(intrinsics: CameraIntrinsics) -> Tuple[np.ndarray, np.ndarray]:
        """
        将通用内参契约转换为 OpenCV 的 K (内参矩阵) 和 D (畸变系数矩阵)。
        """
        K = np.array([
            [intrinsics.focal_length_x, 0, intrinsics.principal_point_x],
            [0, intrinsics.focal_length_y, intrinsics.principal_point_y],
            [0, 0, 1]
        ], dtype=np.float64)
        
        # k1, k2, p1, p2, k3
        D = np.array([intrinsics.k1, 0.0, 0.0, 0.0, 0.0], dtype=np.float64)
        
        return K, D

    @staticmethod
    def undistort_image(
        image_array: np.ndarray,
        intrinsics: CameraIntrinsics,
        crop: bool = False,
    ) -> np.ndarray:
        """
        对单张图像执行 OpenCV 畸变矫正。
        
        :param image_array: BGR 图像 (numpy.ndarray)
        :param intrinsics: 相机内参
        :param crop: 是否裁剪边缘的黑边
        :return: 矫正后的图像
        """
        if intrinsics.k1 == 0.0:
            return image_array.copy()
            
        h, w = image_array.shape[:2]
        K, D = GeometryProcessor.get_opencv_matrices(intrinsics)
        
        if crop:
            # 获取最优新内参矩阵 (裁剪掉无效像素)
            new_k, roi = cv2.getOptimalNewCameraMatrix(K, D, (w, h), 1, (w, h))
            undistorted = cv2.undistort(image_array, K, D, None, new_k)
            x, y, rw, rh = roi
            undistorted = undistorted[y:y+rh, x:x+rw]
        else:
            undistorted = cv2.undistort(image_array, K, D, None, K)
            
        return undistorted
