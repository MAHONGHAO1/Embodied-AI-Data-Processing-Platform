"""
具身智能算子库全局统一配置管理器。
负责统一读取环境变量、Secrets及默认配置参数。
"""
import os
from typing import Optional

# 默认重试次数常量
DEFAULT_RETRY_COUNT = 3

class ConfigManager:
    """
    统一配置与凭证管理器 (大驼峰命名)
    """
    
    def __init__(self):
        self.env_prefix = "QUICOP_"
        
    def get_odps_access_id(self) -> Optional[str]:
        """
        获取 ODPS 访问 ID
        """
        return os.environ.get(f"{self.env_prefix}ODPS_ACCESS_ID")
        
    def get_oss_endpoint(self) -> str:
        """
        获取对象存储端点，带默认值回退
        """
        return os.environ.get(
            f"{self.env_prefix}OSS_ENDPOINT",
            "oss-cn-hangzhou.aliyuncs.com",
        )

    def get_model_weights_path(self, model_name: str) -> str:
        """
        根据模型名称动态获取外部权重的挂载路径
        """
        base_path = os.environ.get(f"{self.env_prefix}WEIGHTS_DIR", "/mnt/oss/weights")
        return os.path.join(base_path, f"{model_name}.pth")

# 全局单例实例化
global_config = ConfigManager()
