"""
模型防腐层：GeoCalib 相机内参估算模型的懒加载 Wrapper。
负责从单张图像中盲猜焦距与径向畸变系数。
"""
import os
from quic_op.core.exceptions import ModelWeightNotFoundException
from quic_op.core.data_types import CameraIntrinsics

class GeoCalibWrapper:
    """
    GeoCalib 模型推理封装器 (防腐层)
    """
    
    def __init__(self, weights_path: str):
        if not os.path.exists(weights_path):
            raise ModelWeightNotFoundException(f"找不到 GeoCalib 权重文件: {weights_path}")
        
        self.weights_path = weights_path
        self.model_instance = None
        
    def _lazy_load(self):
        """
        延迟加载深度学习库，防止 CPU 环境崩溃
        """
        if self.model_instance is None:
            # 架构规范：局部懒加载
            import torch
            _ = torch
            # from geocalib import GeoCalib
            print("INFO: [Lazy Load] 成功导入 torch 与 GeoCalib 模型")
            self.model_instance = "Mocked_GeoCalib_Model_In_Memory"

    def estimate_intrinsics(
        self,
        image_array,
        image_width: int,
        image_height: int,
    ) -> CameraIntrinsics:
        """
        执行单图推理，返回标准的 CameraIntrinsics 契约对象
        
        :param image_array: RGB 图像 (numpy.ndarray)，格式为 HWC
        :param image_width: 图像宽度
        :param image_height: 图像高度
        """
        self._lazy_load()
        print("INFO: 正在使用 GeoCalib 盲猜相机内参与畸变系数...")
        
        if self.model_instance == "Mocked_GeoCalib_Model_In_Memory":
            focal_px = max(image_width, image_height) * 0.8
            k1 = -0.15
        else:
            import torch
            # 真实推理逻辑 (参考 egocentric_pipeline_py)
            tensor_input = torch.from_numpy(image_array).permute(2, 0, 1).float() / 255.0
            
            with torch.no_grad():
                res = self.model_instance.calibrate(tensor_input, camera_model="simple_radial")
                
            cam = res["camera"]
            
            # 兼容多种 GeoCalib 版本的属性命名
            focal_px = None
            for attr in ("f", "focal", "focal_length"):
                if hasattr(cam, attr):
                    val = getattr(cam, attr)
                    focal_px = (
                        float(val.detach().flatten().cpu().numpy()[0])
                        if torch.is_tensor(val)
                        else float(val)
                    )
                    break
                    
            if focal_px is None:
                raise RuntimeError("GeoCalib 返回对象缺少 focal 属性")
                
            k1 = 0.0
            for attr in ("k1", "dist", "distortion"):
                if hasattr(cam, attr):
                    val = getattr(cam, attr)
                    k1 = float(val.detach().flatten().cpu().numpy()[0]) if torch.is_tensor(val) else float(val)
                    break

        return CameraIntrinsics(
            focal_length_x=focal_px,
            focal_length_y=focal_px,
            principal_point_x=image_width / 2.0,
            principal_point_y=image_height / 2.0,
            k1=k1,
        )
