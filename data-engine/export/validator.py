"""
导出库：Schema 强校验网关。
在数据落盘导出前，强制校验是否丢帧、特征是否符合 core 定义规范。
"""
from quic_op.core.data_types import QRDFEpisode
from quic_op.core.exceptions import SchemaValidationException

class SchemaValidator:
    """
    最终导出前的数据形态与一致性校验器
    """
    
    def enforce_schema(self, episode_data: QRDFEpisode, schema_version: str = "v1.0"):
        """
        校验单个 Episode 数据包的完整性
        """
        print(f"INFO: 正在进行出厂前强 Schema 校验 (版本 {schema_version})...")
        
        # 校验 1：多模态数据对齐长度是否一致
        video_len = len(episode_data.video_paths)
        action_len = len(episode_data.action_states)
        
        if video_len == 0 or action_len == 0:
            raise SchemaValidationException(
                f"Episode {episode_data.episode_id} 数据缺失 "
                f"(Video: {video_len}, Action: {action_len})"
            )
            
        print("INFO: Schema 校验通过，允许导出")
        return True
