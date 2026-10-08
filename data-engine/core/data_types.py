"""
具身智能核心数据类型与契约定义模块。
本模块提供跨库共享的纯数学与数据结构定义，严禁引入 torch 或 cv2 等重型依赖。
所有类型与接口均采用大驼峰命名法。
"""
from dataclasses import dataclass, field
from typing import List, Dict, Optional

# 常量定义：采用全大写下划线分隔
DEFAULT_COORDINATE_SYSTEM = "right_hand_z_up"
MAX_JOINTS_COUNT = 21
HAND_POSE_OPERATOR_ID = "perception.hand_pose_yolo"


# clean 质量检测算子标识
LOWLIGHT_OPERATOR_ID = "clean.quality_lowlight"
BLUR_OPERATOR_ID = "clean.quality_blur"
FRAME_DROP_OPERATOR_ID = "clean.quality_frame_drop"


@dataclass
class CameraIntrinsics:
    """
    相机内参矩阵模型，用于所有视觉几何计算的基础契约。
    """
    focal_length_x: float  # X轴焦距
    focal_length_y: float  # Y轴焦距
    principal_point_x: float  # X轴主点
    principal_point_y: float  # Y轴主点
    k1: float  # 径向畸变系数


@dataclass
class HandMesh3D:
    """
    3D手部网格数据类，用于感知层与导出层的数据流通契约。
    """
    joints3d: List[List[float]]  # 包含3D坐标的嵌套列表，避免强制依赖 numpy
    confidence_score: float  # 整体置信度评分
    coordinate_system: str = DEFAULT_COORDINATE_SYSTEM


@dataclass
class HandDetectionResult:
    """
    单帧手部检测结果数据类，用于感知层手部检测的标准输出契约。
    """
    frame_id: int
    left_hand_present: bool
    right_hand_present: bool
    left_bbox: Optional[List[float]] = None       # [x1, y1, x2, y2]
    right_bbox: Optional[List[float]] = None      # [x1, y1, x2, y2]
    left_confidence: float = 0.0
    right_confidence: float = 0.0


@dataclass
class HandPoseOperatorResult:
    """
    人手 Pose 检测算子的统一对外返回契约。
    同时携带内存中的结构化检测结果与持久化 JSON 绝对路径。
    """
    frames: List[HandDetectionResult]
    json_path: str
    source_path: Optional[str]
    source_type: str
    num_frames: int
    conf_threshold: float
    weights_path: str
    fps: Optional[float] = None
    stats: Dict[str, int] = field(default_factory=dict)
    operator: str = HAND_POSE_OPERATOR_ID
class LowLightSample:
    """低光检测单采样点契约。"""
    t_sec: float
    mean_gray: float
    dark_pixel_ratio: float
    low_light_flag: bool


@dataclass
class BlurSample:
    """模糊检测单采样点契约。"""
    t_sec: float
    laplacian_var: float
    blur_flag: bool


@dataclass
class FrameDropEvent:
    """掉帧事件契约。"""
    t_prev_sec: float
    t_curr_sec: float
    frame_interval_ms: float
    drop_frame_count: int
    frame_drop_flag: bool = True


@dataclass
class LowLightOperatorResult:
    """
    低光检测算子统一对外返回契约。
    同时携带结构化采样结果与持久化 JSON 绝对路径。
    """
    samples: List[LowLightSample]
    json_path: str
    source_path: Optional[str]
    num_samples: int
    stats: Dict[str, int] = field(default_factory=dict)
    operator: str = LOWLIGHT_OPERATOR_ID


@dataclass
class BlurOperatorResult:
    """
    模糊检测算子统一对外返回契约。
    """
    samples: List[BlurSample]
    json_path: str
    source_path: Optional[str]
    num_samples: int
    stats: Dict[str, int] = field(default_factory=dict)
    operator: str = BLUR_OPERATOR_ID


@dataclass
class FrameDropOperatorResult:
    """
    掉帧检测算子统一对外返回契约。
    """
    events: List[FrameDropEvent]
    json_path: str
    source_path: Optional[str]
    num_events: int
    expected_fps: float
    drop_threshold_sec: float
    stats: Dict[str, int] = field(default_factory=dict)
    operator: str = FRAME_DROP_OPERATOR_ID


@dataclass
class QRDFEpisode:
    """
    具身智能原子数据包实体类，贯穿全生命周期的 L2.5 资产格式。
    """
    episode_id: str
    video_paths: List[str]
    action_states: List[Dict[str, float]]
    semantic_label: Optional[str] = None
