"""固定来源配置与可覆盖的数据范围。

HDF5 数据范围支持通过环境变量扩容，无需改动代码：

    ROBODATA_HDF5_EPISODES=0-9        # 使用全部 10 条 demo（531 帧）
    ROBODATA_HDF5_EPISODES=0-4,6,8    # 混合区间与单点

默认保持 V0.4 验收所用的 3 条（demo_0-2 / 174 帧），避免既有验收数字失效。
范围变化会改变输入指纹，已导入批次需重新导入，这是预期行为。
"""
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPO_ID = "jmrog/so100_sweet_pick"
REVISION = "54141acb0bd6bcb868e34b4eb328f26481d333b2"
EPISODES = (0, 1, 2, 3, 4)
CAMERA = "observation.images.laptop"
APP_VERSION = "0.4.0"
RULE_VERSION = "0.3.0"
DEFAULT_DATA_ROOT = PROJECT_ROOT / "data" / "so100_sweet_pick"
DEFAULT_REPORT_ROOT = PROJECT_ROOT / "reports"
DEFAULT_RULE_PARAMS = {"interval_relative_tolerance": 0.10, "timestamp_absolute_tolerance": 0.00001}
FIELD_NAMES = ["main_shoulder_pan", "main_shoulder_lift", "main_elbow_flex", "main_wrist_flex", "main_wrist_roll", "main_gripper"]


def parse_episode_range(raw, default):
    """解析 "0-9" / "0,1,2" / "0-2,5" 形式的数据范围。

    空值返回默认范围的副本；非法输入抛出 ValueError，避免静默使用错误范围。
    返回值已排序去重，便于直接参与输入指纹计算。
    """
    text = (raw or "").strip()
    if not text:
        return list(default)
    items = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo_text, _, hi_text = part.partition("-")
            try:
                lo, hi = int(lo_text), int(hi_text)
            except ValueError as exc:
                raise ValueError(f"数据范围片段无法解析：{part}") from exc
            if hi < lo:
                raise ValueError(f"数据范围区间上界小于下界：{part}")
            items.extend(range(lo, hi + 1))
        else:
            try:
                items.append(int(part))
            except ValueError as exc:
                raise ValueError(f"数据范围片段无法解析：{part}") from exc
    if not items:
        raise ValueError(f"数据范围为空：{raw!r}")
    if any(index < 0 for index in items):
        raise ValueError(f"数据范围不能为负：{raw!r}")
    return sorted(set(items))


HDF5_EPISODES = parse_episode_range(os.environ.get("ROBODATA_HDF5_EPISODES"), (0, 1, 2))
