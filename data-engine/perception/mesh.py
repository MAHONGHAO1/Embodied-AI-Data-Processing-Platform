"""
物理感知库业务入口模块。
负责将模型防腐层输出的裸张量转换为系统标准的 core.data_types 数据契约。
"""
from quic_op.core.data_types import HandMesh3D
from quic_op.perception.models.hawor_wrapper import HaworWrapper
from typing import List

class HandReconstructor:
    """
    手部重建业务调度类
    """
    
    def __init__(self, weights_path: str):
        # 实例化防腐层的模型 Wrapper，此时不会触发 torch 导入
        self.model_wrapper = HaworWrapper(weights_path)
        
    def reconstruct_hands(self, frames_list: List[object]) -> List[HandMesh3D]:
        """
        处理帧序列，返回符合 Core 契约的 3D Mesh 实体列表
        """
        mesh_results = []
        for frame in frames_list:
            # 1. 调用底层模型推理获取裸数据
            raw_joints = self.model_wrapper.predict_mesh(frame)
            
            # 2. 组装为跨库共享的标准实体对象
            mesh_entity = HandMesh3D(
                joints3d=raw_joints,
                confidence_score=0.95,
            )
            mesh_results.append(mesh_entity)
            
        return mesh_results
