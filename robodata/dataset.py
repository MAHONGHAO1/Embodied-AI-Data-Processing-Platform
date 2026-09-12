"""Read the sample without repairing or hiding malformed table values."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import pandas as pd

from .config import CAMERA, EPISODES, FIELD_NAMES, REPO_ID, REVISION
from .models import Episode
from .source import DEMO_MANIFEST_NAME, MANIFEST_NAME, SAMPLE_FILES


def _safe_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError('元数据引用的路径超出数据目录')
    return path


def _source(info: dict, manifest: dict) -> dict:
    if info.get('codebase_version') == 'v3.0':
        from .sources import HDF5_SOURCE
        source = manifest.get('source', {})
        return {**HDF5_SOURCE, **{k: v for k, v in source.items() if k in HDF5_SOURCE},
                'format_version': 'v3.0'}
    return {'repo_id': REPO_ID, 'revision': REVISION,
            'url': f'https://huggingface.co/datasets/{REPO_ID}/tree/{REVISION}',
            'license': 'Apache-2.0（来源声明）', 'camera': CAMERA,
            'format_version': info.get('codebase_version', 'v2.0'), 'robot': 'SO-100 机械臂'}


def episode_paths(root: Path, metadata: dict, index: int) -> tuple[Path, Path]:
    entry, info = metadata['episodes'][index], metadata['info']
    camera = metadata['source']['camera']
    if info.get('codebase_version') == 'v3.0':
        data = info['data_path'].format(chunk_index=int(entry['data/chunk_index']), file_index=int(entry['data/file_index']))
        video = info['video_path'].format(video_key=camera,
            chunk_index=int(entry[f'videos/{camera}/chunk_index']), file_index=int(entry[f'videos/{camera}/file_index']))
    else:
        data = f'data/chunk-000/episode_{index:06d}.parquet'
        video = f'videos/chunk-000/{camera}/episode_{index:06d}.mp4'
    return _safe_path(root, data), _safe_path(root, video)


def _json_lines(path: Path, key: str) -> dict[int, dict]:
    result = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path.name} 第 {line_number} 行必须是包含字段的 JSON 对象")
        identifier = value.get(key)
        if not isinstance(identifier, int) or isinstance(identifier, bool) or identifier in result:
            raise ValueError(f"{path.name}:{line_number} 缺少或重复的 {key}")
        result[identifier] = value
    return result


def load_metadata(root: Path) -> dict:
    root = Path(root)
    info = json.loads((root / "meta/info.json").read_text(encoding="utf-8"))
    if not isinstance(info, dict) or not isinstance(info.get("features"), dict):
        raise ValueError("meta/info.json 缺少数据字段定义")
    fps = info.get("fps")
    if isinstance(fps, bool) or not isinstance(fps, (int, float)) or not math.isfinite(fps) or fps <= 0:
        raise ValueError("meta/info.json 的 FPS 必须为正有限数值")
    manifest_path = root / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    if not isinstance(manifest, dict):
        raise ValueError("源数据清单必须是对象")
    if info.get('codebase_version') == 'v3.0':
        from .sources import V3_CAMERA
        if V3_CAMERA not in info['features']:
            raise ValueError('当前仅支持本项目的 Panda/Lift 主相机 v3.0 输出配置')
        files = sorted((root / 'meta/episodes').glob('chunk-*/file-*.parquet'))
        if not files:
            raise FileNotFoundError('缺少 v3.0 任务索引 meta/episodes')
        episode_table = pd.concat([pd.read_parquet(p) for p in files], ignore_index=True)
        if episode_table['episode_index'].duplicated().any():
            raise ValueError('v3.0 任务编号重复')
        episodes = {int(row['episode_index']): row for row in episode_table.to_dict('records')}
        task_table = pd.read_parquet(root / 'meta/tasks.parquet')
        tasks = {int(row['task_index']): {'task_index': int(row['task_index']), 'task': str(name)}
                 for name, row in task_table.iterrows()}
        indices = sorted(episodes)
    else:
        if info.get('codebase_version', 'v2.0') != 'v2.0':
            raise ValueError('当前仅验证 SO-100 v2.0 和本项目 Panda/Lift v3.0 数据配置')
        episodes = _json_lines(root / 'meta/episodes.jsonl', 'episode_index')
        tasks = _json_lines(root / 'meta/tasks.jsonl', 'task_index')
        indices = list(EPISODES)
    return {'info': info, 'episodes': episodes, 'tasks': tasks, 'manifest': manifest,
            'source': _source(info, manifest), 'episode_indices': indices}


def load_episode(root: Path, index: int) -> Episode:
    root = Path(root)
    metadata = load_metadata(root)
    if index not in metadata['episode_indices']:
        raise ValueError(f"当前配置仅支持 episode {metadata['episode_indices']}")
    if index not in metadata["episodes"]:
        raise ValueError(f"源元数据缺少 episode {index}")
    entry = metadata["episodes"][index]
    length = entry.get("length")
    if not isinstance(length, int) or isinstance(length, bool) or length < 1:
        raise ValueError(f"episode {index} 的源记录长度无效")
    data_path, video_path = episode_paths(root, metadata, index)
    table = pd.read_parquet(data_path)
    video_start, video_end = 0.0, None
    if metadata['info'].get('codebase_version') == 'v3.0':
        # Global dataset indices are values, not iloc offsets into a particular shard.
        table = table.loc[table['episode_index'] == index].reset_index(drop=True)
        start, end = int(entry['dataset_from_index']), int(entry['dataset_to_index'])
        if len(table) and (table['index'].min() < start or table['index'].max() >= end):
            raise ValueError(f'任务 {index} 的数据索引超出元数据范围 [{start}, {end})')
        camera = metadata['source']['camera']
        video_start = float(entry[f'videos/{camera}/from_timestamp'])
        video_end = float(entry[f'videos/{camera}/to_timestamp'])
        if not math.isfinite(video_start) or not math.isfinite(video_end) or video_start < 0 or video_end <= video_start:
            raise ValueError(f'任务 {index} 的视频片段范围无效')
    names = metadata["info"]["features"].get("observation.state", {}).get("names")
    if isinstance(names, dict):
        names = names.get("motors", next(iter(names.values()), None))
    if not isinstance(names, list) or not all(isinstance(x, str) for x in names):
        names = list(FIELD_NAMES)
    source = metadata['source']
    return Episode(episode_index=index, table=table,
                   metadata={**metadata, "episode": entry}, video_path=video_path,
                   data_path=data_path, field_names=names, fps=float(metadata["info"]["fps"]),
                   source=source, expected_length=length,
                   video_start_time=video_start, video_end_time=video_end)


def input_relative_paths(root: Path, metadata: dict | None = None) -> tuple[str, ...]:
    root = Path(root).resolve()
    if metadata is None:
        try:
            metadata = load_metadata(root)
        except (ValueError, OSError, KeyError):
            metadata = None
    if not metadata or metadata.get('info', {}).get('codebase_version') != 'v3.0':
        return (*SAMPLE_FILES, MANIFEST_NAME, DEMO_MANIFEST_NAME)
    paths = {'meta/info.json', 'meta/tasks.parquet', MANIFEST_NAME}
    for folder in ('meta', 'data', 'videos'):
        paths.update(p.relative_to(root).as_posix() for p in (root / folder).rglob('*') if p.is_file())
    for index in metadata['episode_indices']:
        paths.update(p.relative_to(root).as_posix() for p in episode_paths(root, metadata, index))
    # Also retain declared files as missing markers when deleted after conversion.
    for entry in metadata['manifest'].get('files', []):
        if isinstance(entry, dict) and isinstance(entry.get('path'), str):
            paths.add(_safe_path(root, entry['path']).relative_to(root).as_posix())
    return tuple(sorted(paths))


def dataset_fingerprint(root: Path) -> str:
    """Hash actual relevant bytes, including deterministic missing-file markers."""
    root = Path(root)
    digest = hashlib.sha256()
    for relative in input_relative_paths(root):
        path = root / relative
        digest.update(relative.encode("utf-8") + b"\0")
        if not path.is_file():
            digest.update(b"MISSING\0")
            continue
        digest.update(f"FILE:{path.stat().st_size}\0".encode("ascii"))
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0END\0")
    return digest.hexdigest()
