"""
语义标注库：VLM (大语言模型) 交互客户端。
作为业务入口，调用底层的视觉大模型进行动作短语打标。
"""
import json
from typing import Dict, List

from quic_op.core.config_manager import global_config


class VLMClient:
    """
    大视觉语言模型交互客户端，负责向 VLM 提交多帧图像并解析返回的结构化标签。
    """
    
    def __init__(self):
        # 严格遵守规范：从 core 配置中心获取鉴权凭证
        self.api_key = global_config.get_odps_access_id()
        self.endpoint = "https://api.aliyun.com/vlm"
        
    def _parse_dual_hand_response(self, response_text: str) -> Dict[str, str]:
        """
        解析 VLM 返回的双手动作标签 JSON 字符串。
        预期格式: {"left":"<phrase or None>","right":"<phrase or None>"}
        """
        text = str(response_text or "").strip()
        if not text:
            return {"left": "None", "right": "None"}
            
        # 尝试清理 Markdown 代码块格式
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:].strip()
                
        try:
            # 兼容带有 choices 包裹的格式 (OpenAI-like)
            outer = json.loads(text)
            if isinstance(outer, dict) and outer.get("choices"):
                text = str(outer["choices"][0]["message"]["content"] or "").strip()
                
            parsed = json.loads(text)
            return {
                "left": str(parsed.get("left", "")).strip() or "None",
                "right": str(parsed.get("right", "")).strip() or "None",
            }
        except Exception as e:
            print(f"WARNING: VLM 返回格式解析失败 ({e}), 原始返回: {response_text}")
            return {"left": "None", "right": "None"}

    def call_ai_func_vlm(self, image_paths: List[str], prompt_template: str = "") -> Dict[str, str]:
        """
        向云端大模型发送多帧画面与 Prompt，获取双手动作语义标签。
        
        :param image_paths: 需要分析的图像绝对路径或 URL 列表 (通常为 4 帧接触表)
        :param prompt_template: 提示词模板
        :return: 解析后的双手动作字典 {"left": "xxx", "right": "yyy"}
        """
        if not prompt_template:
            prompt_template = (
                "You are annotating an egocentric human-hand manipulation video. "
                "Look at the 4 frames and describe the atomic action for BOTH hands separately, "
                "using imperative verb phrases. If a hand is idle or not visible, return 'None'. "
                'Return STRICT JSON: {"left":"<phrase or None>","right":"<phrase or None>"}'
            )
            
        if not self.api_key:
            print("WARNING: 未配置 VLM API Key，将返回 Mock 标签")
            return {"left": "Pick up the red block", "right": "None"}
            
        print(f"INFO: [VLM Client] 正在提交 {len(image_paths)} 帧图像进行语义标注...")
        
        # TODO: 这里在生产环境中会通过 MaxFrame AI FUNC 或 requests 调用大模型 API
        # 伪造一个正常的 VLM 响应
        mock_vlm_response = '{"left": "Pick up the red block", "right": "Hold the tray"}'
        
        return self._parse_dual_hand_response(mock_vlm_response)
