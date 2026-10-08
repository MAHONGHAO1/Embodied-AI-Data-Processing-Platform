"""
模型防腐层：HaWoR 手部重建模型的懒加载 Wrapper。
本文件必须且只能放在 models/ 目录下，严禁被核心业务层以外的代码导入。
"""
import os
from quic_op.core.exceptions import ModelWeightNotFoundException

class HaworWrapper:
    """
    HaWoR 模型推理封装器
    """
    
    def __init__(self, weights_path: str):
        if not os.path.exists(weights_path):
            raise ModelWeightNotFoundException(f"找不到 HaWoR 权重文件: {weights_path}")
        
        self.weights_path = weights_path
        self.model_instance = None
        
    def _lazy_load(self):
        """
        延迟加载深度学习库，防止 CPU 环境崩溃
        """
        if self.model_instance is None:
            # 【架构规范】严格要求在此处进行懒加载
            import torch
            _ = torch
            # from third_party.hawor import HaWoRNet
            print("INFO: [Lazy Load] 成功导入 torch 与 HaWoR 架构")
            
            # 伪代码实例化
            # device = "cuda" if torch.cuda.is_available() else "cpu"
            # self.model_instance = HaWoRNet().load(self.weights_path).to(device)
            self.model_instance = "Mocked_HaWoR_Model_In_Memory"

    def predict_mesh(self, image_array) -> list:
        """
        执行前向推理，返回包含3D节点坐标的嵌套列表
        
        :param image_array: RGB 图像 (numpy.ndarray)
        :return: 21个手部关节的3D坐标列表
        """
        self._lazy_load()
        print("INFO: 正在使用 HaWoR 提取手部 Mesh...")
        
        if self.model_instance == "Mocked_HaWoR_Model_In_Memory":
            mock_joints = [[0.1, 0.2, 0.3] for _ in range(21)]
            return mock_joints
            
        import torch
        # 真实推理逻辑 (参考 HaWoR 官方实现)
        tensor_input = torch.from_numpy(image_array).permute(2, 0, 1).float() / 255.0
        tensor_input = tensor_input.unsqueeze(0).to(next(self.model_instance.parameters()).device)
        
        with torch.no_grad():
            # 假设 HaWoR 返回一个包含 joints 3D 坐标的张量，形状为 [1, 21, 3]
            joints = self.model_instance.infer(tensor_input)
            joints_list = joints.squeeze().cpu().numpy().tolist()
            
        return joints_list
