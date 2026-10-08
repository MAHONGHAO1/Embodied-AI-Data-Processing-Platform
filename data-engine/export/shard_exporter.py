"""
导出库：分片导出模块。
执行分布式数据分片 (Data Sharding)，避免单个包过大（如控制在 500MB/片），方便云端读取与训练。
"""
import json
import os
from typing import List, Dict, Any

from quic_op.core.data_types import QRDFEpisode
from quic_op.core.exceptions import ExporterException

class ShardExporter:
    """
    分布式数据分片导出器
    """
    
    def __init__(self, output_dir: str, shard_size_mb: int = 500):
        """
        初始化分片导出器。
        
        :param output_dir: 导出落盘的绝对路径
        :param shard_size_mb: 单个分片的最大容量限制（兆字节）
        """
        self.output_dir = output_dir
        self.shard_size_mb = shard_size_mb
        self.current_shard_id = 0
        self.current_shard_size = 0
        self.current_buffer: List[Dict[str, Any]] = []
        
        if not os.path.exists(self.output_dir):
            os.makedirs(self.output_dir)
            
    def _flush_shard(self):
        """
        将当前内存中的数据刷写为独立的分片文件
        """
        if not self.current_buffer:
            return
            
        shard_file_name = os.path.join(
            self.output_dir,
            f"shard_{self.current_shard_id:04d}.json",
        )
        try:
            with open(shard_file_name, "w", encoding="utf-8") as file:
                json.dump(self.current_buffer, file, ensure_ascii=False, indent=2)
            print(
                f"INFO: [Data Sharding] 成功导出分片 {shard_file_name}，"
                f"包含 {len(self.current_buffer)} 条记录"
            )
        except Exception as e:
            raise ExporterException(f"导出分片失败: {str(e)}")
            
        self.current_shard_id += 1
        self.current_buffer = []
        self.current_shard_size = 0

    def add_episode(self, episode_data: QRDFEpisode):
        """
        向导出队列添加 Episode 数据，达到容量阈值则自动分片。
        
        注：这里为了演示简化了容量计算逻辑。
        """
        # 转换为字典形式存储
        episode_dict = {
            "episode_id": episode_data.episode_id,
            "video_paths": episode_data.video_paths,
            "action_states": episode_data.action_states,
            "semantic_label": episode_data.semantic_label,
        }
        
        # 伪逻辑：估算单条数据占用大小（假设 10MB）
        estimated_size_mb = 10
        
        if self.current_shard_size + estimated_size_mb > self.shard_size_mb:
            self._flush_shard()
            
        self.current_buffer.append(episode_dict)
        self.current_shard_size += estimated_size_mb
        
    def finalize(self):
        """
        结束导出流程，将尾部缓冲数据落盘
        """
        self._flush_shard()
        print("INFO: 导出任务全部完成。")
