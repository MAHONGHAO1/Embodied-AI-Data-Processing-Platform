"""
模型防腐层：MoGe-2 稠密深度与相机内参估计模型的懒加载 Wrapper。
本文件必须且只能放在 models/ 目录下，严禁被核心业务层以外的代码导入。
"""
import os
import numpy as np
from typing import Dict, Any

from quic_op.core.exceptions import ModelWeightNotFoundException

class MogeWrapper:
    """
    MoGe-2 模型推理封装器
    用于单目图像的稠密深度估计与焦距盲猜。
    """
    
    def __init__(self, weights_path: str):
        if not os.path.exists(weights_path):
            raise ModelWeightNotFoundException(f"找不到 MoGe-2 权重文件/目录: {weights_path}")
            
        self.weights_path = weights_path
        self.model_instance = None
        
    def _lazy_load(self):
        """
        延迟加载深度学习库，防止 CPU 环境崩溃
        """
        if self.model_instance is None:
            # 【架构规范】严格要求在此处进行懒加载
            import torch
            try:
                from moge.model.v2 import MoGeModel
                device = "cuda" if torch.cuda.is_available() else "cpu"
                self.model_instance = MoGeModel.from_pretrained(self.weights_path).to(device).eval()
                print(f"INFO: [Lazy Load] 成功导入 MoGeModel 并加载至 {device}")
            except ImportError:
                print("WARNING: 未安装 moge，启动 Mock 模式 (仅供开发测试)")
                self.model_instance = "Mocked_MoGe_Model"

    def estimate(self, image_array: np.ndarray) -> Dict[str, Any]:
        """
        执行前向推理，预测深度与焦距。
        
        :param image_array: RGB 图像 (numpy.ndarray)，格式为 HWC
        :return: 包含深度图与焦距的字典
        """
        self._lazy_load()
        print("INFO: 正在使用 MoGe-2 估计稠密深度与焦距...")
        
        if self.model_instance == "Mocked_MoGe_Model":
            # 伪造深度图与焦距
            h, w = image_array.shape[:2]
            return {
                "depth": np.ones((h, w), dtype=np.float32) * 2.0,
                "focal_px": max(h, w) * 0.8
            }
            
        import torch
        # 真实推理逻辑 (参考 MoGe 官方实现)
        # 输入规范化
        tensor_input = torch.from_numpy(image_array).permute(2, 0, 1).float() / 255.0
        tensor_input = tensor_input.unsqueeze(0).to(next(self.model_instance.parameters()).device)
        
        with torch.no_grad():
            output = self.model_instance.infer(tensor_input)
            
        # 提取并转换为 Numpy
        depth = output["depth"].squeeze().cpu().numpy()
        intrinsics = output["intrinsics"].squeeze().cpu().numpy()
        focal_px = intrinsics[0, 0] # 假设 fx 位于 [0,0]
        
        return {
            "depth": depth,
            "focal_px": float(focal_px)
        }
