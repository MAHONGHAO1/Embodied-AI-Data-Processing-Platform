"""The two explicitly supported, pinned portfolio data profiles."""
from pathlib import Path
import os
import time
import requests
from .config import PROJECT_ROOT
from .source import file_sha256

HDF5_REPO_ID = 'robomimic/robomimic_datasets'
HDF5_REVISION = '74fa018461f479cd9fd15b924a16103012096203'
HDF5_SHA256 = '80dea9ca9bb99dd7b96109712fc6b46bb6fa6b87e9622b8aebbe1e4ee5fadbab'
HDF5_SIZE = 45939724
DEFAULT_HDF5_ROOT = PROJECT_ROOT / 'data' / 'robomimic'
HDF5_PATH = DEFAULT_HDF5_ROOT / 'test.hdf5'
V3_CAMERA = 'observation.images.agentview'
HDF5_SOURCE = {'repo_id': HDF5_REPO_ID, 'revision': HDF5_REVISION,
    'url': f'https://huggingface.co/datasets/{HDF5_REPO_ID}/tree/{HDF5_REVISION}/test',
    'license': 'MIT（来源声明）', 'camera': V3_CAMERA, 'format_version': 'v3.0',
    'robot': 'Panda / Lift 仿真测试数据', 'timestamp_semantics': '按 control_freq=20 派生的任务内时间'}
MAPPING = [
    {'输出': '9 维状态', '来源': 'obs/robot0_eef_pos + obs/robot0_eef_quat + obs/robot0_gripper_qpos', '处理': '顺序拼接，float32'},
    {'输出': '7 维动作', '来源': 'actions', '处理': '原顺序，float32'},
    {'输出': '主相机', '来源': 'obs/agentview_image', '处理': '84×84 RGB → H.264（有损）'},
    {'输出': '任务', '来源': 'env_args.env_name', '处理': 'Lift'},
    {'输出': '时间', '来源': 'env_args.env_kwargs.control_freq', '处理': 'frame_index / 20，派生时间'},
]

def fetch_hdf5(root: Path = DEFAULT_HDF5_ROOT, proxy: str | None = None, progress=None) -> Path:
    """Reuse only SHA-verified bytes; replace only a successfully downloaded part file."""
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = root / 'test.hdf5'
    if target.is_file() and target.stat().st_size == HDF5_SIZE and file_sha256(target) == HDF5_SHA256:
        if progress: progress('已复用通过固定 SHA-256 校验的 HDF5 文件')
        return target
    if target.exists():
        raise ValueError('已有 HDF5 与固定版本不一致；保留该文件，请使用新的下载目录')
    url = f'https://huggingface.co/datasets/{HDF5_REPO_ID}/resolve/{HDF5_REVISION}/test/test.hdf5'
    session = requests.Session()
    if proxy: session.proxies.update({'http': proxy, 'https': proxy})
    part = root / f'test.hdf5.{os.getpid()}.part'
    try:
        for attempt in range(3):
            try:
                if progress: progress(f'下载固定版本 HDF5（尝试 {attempt + 1}/3）')
                with session.get(url, stream=True, timeout=(20, 90)) as response:
                    response.raise_for_status()
                    with part.open('wb') as stream:
                        for chunk in response.iter_content(1024 * 1024):
                            if chunk: stream.write(chunk)
                if part.stat().st_size != HDF5_SIZE or file_sha256(part) != HDF5_SHA256:
                    raise ValueError('HDF5 大小或 SHA-256 不匹配，未登记为有效样本')
                os.replace(part, target)
                return target
            except (requests.RequestException, ValueError):
                if attempt == 2: raise
                time.sleep(attempt + 1)
    finally:
        session.close()
        part.unlink(missing_ok=True)
