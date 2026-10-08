"""
稠密深度与相机内参估计业务入口模块。
负责将模型防腐层输出的裸数据转换为业务层所需的 Numpy 数组与几何实体。
"""
import numpy as np
from typing import Tuple

from quic_op.perception.models.moge_wrapper import MogeWrapper

class DepthEstimator:
    """
    深度估计业务调度类
    """
    
    def __init__(self, weights_path: str):
        # 实例化防腐层的模型 Wrapper，此时不会触发 torch 导入
        self.model_wrapper = MogeWrapper(weights_path)
        
    def estimate_depth_moge2(self, image_array: np.ndarray) -> Tuple[np.ndarray, float]:
        """
        处理单张 RGB 图像，返回稠密深度图与估计焦距。
        
        :param image_array: RGB 图像 (numpy.ndarray)，格式为 HWC
        :return: (稠密深度图 numpy.ndarray, 估计的焦距像素值 float)
        """
        # 1. 调用底层模型推理获取裸数据
        result_dict = self.model_wrapper.estimate(image_array)
        
        # 2. 提取业务数据
        depth_map = result_dict.get("depth")
        focal_px = result_dict.get("focal_px")
        
        return depth_map, focal_px
