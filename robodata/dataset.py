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


GENERIC_MARKER = "robodata_source.json"


def _generic_marker(root: Path) -> dict:
    path = Path(root) / GENERIC_MARKER
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{GENERIC_MARKER} 无法解析：{exc}") from exc
    if not isinstance(value, dict) or value.get("kind") != "lerobot_v21":
        raise ValueError(f"{GENERIC_MARKER} 的 kind 必须是 lerobot_v21")
    return value


def _v30_cameras(info: dict) -> list[str]:
    """Return declared LeRobot video features in metadata order."""
    cameras = [key for key, feature in (info.get("features") or {}).items()
               if isinstance(feature, dict) and feature.get("dtype") == "video"]
    if not cameras:
        raise ValueError("v3.0 任务的元数据未声明 video 字段，无法定位相机分片")
    return cameras


def _v30_camera(info: dict) -> str:
    """Backward-compatible default camera for existing single-camera callers."""
    return _v30_cameras(info)[0]


def _source(info: dict, manifest: dict, *, generic: bool = False) -> dict:
    if generic:
        features = info.get('features') or {}
        cameras = [key for key, feature in features.items()
                   if isinstance(feature, dict) and feature.get('dtype') == 'video']
        if not cameras:
            raise ValueError('通用 LeRobot v2.1 输入没有声明 video 相机')
        declared = manifest.get('source', {}) if isinstance(manifest, dict) else {}
        fps = float(info['fps'])
        allowed = {'repo_id', 'revision', 'url', 'license', 'timestamp_semantics'}
        return {**{key: value for key, value in declared.items() if key in allowed},
                'repo_id': declared.get('repo_id') or 'local/lerobot-v21',
                'revision': declared.get('revision') or 'local-source',
                'license': declared.get('license') or '由数据提供方确认',
                'robot': declared.get('robot') or declared.get('robot_type') or 'LeRobot v2.1 通用输入',
                'camera': cameras[0], 'cameras': cameras,
                'format_version': info.get('codebase_version', 'v2.1'), 'fps': fps,
                'camera_metadata': {camera: {'fps': fps,
                    'resolution': list(features[camera].get('shape', [])),
                    'source_path': f'videos/chunk-000/{camera}'} for camera in cameras}}
    if info.get('codebase_version') == 'v3.0':
        from .sources import HDF5_SOURCE
        declared = manifest.get('source', {})
        cameras = _v30_cameras(info)
        robot = (declared.get('robot')
                 or declared.get('robot_type')
                 or HDF5_SOURCE.get('robot', 'Panda / Lift'))
        allowed_source_fields = {'repo_id', 'revision', 'url', 'license', 'timestamp_semantics'}
        return {**HDF5_SOURCE, **{k: v for k, v in declared.items() if k in allowed_source_fields},
                'camera': cameras[0], 'cameras': cameras, 'format_version': 'v3.0',
                'robot': str(robot), 'fps': float(info['fps']),
                'camera_metadata': {camera: {'fps': float(info['fps']),
                    'resolution': list(info['features'][camera].get('shape', [])),
                    'source_path': info['video_path']} for camera in cameras}}
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


def episode_camera_paths(root: Path, metadata: dict, index: int) -> dict[str, Path]:
    """Resolve each episode's video feature to its own source file."""
    root = Path(root)
    info = metadata['info']
    entry = metadata['episodes'][index]
    if info.get('codebase_version') != 'v3.0':
        cameras = metadata['source'].get('cameras') or [metadata['source']['camera']]
        return {camera: _safe_path(root, f'videos/chunk-000/{camera}/episode_{index:06d}.mp4')
                for camera in cameras}
    paths = {}
    for camera in metadata['source'].get('cameras', [metadata['source']['camera']]):
        video = info['video_path'].format(
            video_key=camera,
            chunk_index=int(entry[f'videos/{camera}/chunk_index']),
            file_index=int(entry[f'videos/{camera}/file_index']),
        )
        paths[camera] = _safe_path(root, video)
    return paths


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


def _legacy_episodes(root: Path) -> dict[int, dict]:
    jsonl = root / "meta/episodes.jsonl"
    if jsonl.is_file():
        return _json_lines(jsonl, "episode_index")
    files = sorted((root / "meta/episodes").glob("chunk-*/file-*.parquet"))
    if files:
        table = pd.concat([pd.read_parquet(path) for path in files], ignore_index=True)
        if table["episode_index"].duplicated().any():
            raise ValueError("v2.1 任务编号重复")
        return {int(row["episode_index"]): row for row in table.to_dict("records")}
    raise FileNotFoundError("缺少 meta/episodes.jsonl 或 meta/episodes/ 任务索引")


def _legacy_tasks(root: Path) -> dict[int, dict]:
    jsonl = root / "meta/tasks.jsonl"
    if jsonl.is_file():
        return _json_lines(jsonl, "task_index")
    parquet = root / "meta/tasks.parquet"
    if parquet.is_file():
        table = pd.read_parquet(parquet)
        if "task" in table.columns:
            return {int(row["task_index"]): {"task_index": int(row["task_index"]), "task": str(row["task"])}
                    for row in table.to_dict("records")}
        return {int(index): {"task_index": int(index), "task": str(index)} for index in table.index}
    raise FileNotFoundError("缺少 meta/tasks.jsonl 或 meta/tasks.parquet")


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
    marker = _generic_marker(root)
    generic = bool(marker)
    declared_source = manifest.get('source', {})
    aloha_profile = manifest.get('profile')
    if not generic and declared_source.get('repo_id') == 'lerobot/aloha_static_coffee':
        from .aloha import ALOHA_CAMERAS, ALOHA_EPISODES, ALOHA_REVISION
        cameras = _v30_cameras(info) if info.get('codebase_version') == 'v3.0' else []
        if info.get('codebase_version') != 'v3.0':
            raise ValueError('固定 ALOHA Coffee 来源只支持 LeRobot v3.0')
        if declared_source.get('revision') != ALOHA_REVISION:
            raise ValueError('ALOHA Coffee revision 不符合已固定版本')
        if declared_source.get('license') != 'MIT（固定版本数据集页面声明）':
            raise ValueError('ALOHA Coffee 许可声明不符合固定来源配置')
        if set(cameras) != set(ALOHA_CAMERAS):
            raise ValueError('ALOHA Coffee 相机特征不符合固定四相机配置')
        if float(info['fps']) != 50.0:
            raise ValueError('ALOHA Coffee 帧率不符合固定 50 FPS 配置')
        if declared_source.get('camera') != ALOHA_CAMERAS[0]:
            raise ValueError('ALOHA Coffee 默认相机与固定来源配置不一致')
        if aloha_profile not in (None, 'aloha_static_coffee_reviewed_v1'):
            raise ValueError('ALOHA Coffee 派生数据集 profile 未在固定配置中注册')
    if info.get('codebase_version') == 'v3.0':
        cameras = _v30_cameras(info)
        if any(camera not in info['features'] for camera in cameras):
            raise ValueError('v3.0 元数据未声明相机 video 字段')
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
        if not generic and declared_source.get('repo_id') == 'lerobot/aloha_static_coffee':
            if aloha_profile == 'aloha_static_coffee_reviewed_v1':
                records = manifest.get('episodes')
                if not isinstance(records, list) or len(records) != len(ALOHA_EPISODES):
                    raise ValueError('ALOHA Coffee 审核子集清单必须精确记录 Episode 0–4')
                source_indices = [item.get('source_episode_index') for item in records
                                  if isinstance(item, dict)]
                output_indices = [item.get('output_episode_index') for item in records
                                  if isinstance(item, dict)]
                row_counts = {item.get('output_episode_index'): item.get('row_count')
                              for item in records if isinstance(item, dict)}
                expected_indices = list(range(len(ALOHA_EPISODES)))
                if (len(source_indices) != len(records) or sorted(source_indices) != list(ALOHA_EPISODES)
                        or sorted(output_indices) != expected_indices or indices != expected_indices
                        or info.get('total_episodes') != len(expected_indices)
                        or info.get('total_frames') != sum(row_counts.values())
                        or any(episodes[index].get('length') != row_counts.get(index)
                               for index in expected_indices)):
                    raise ValueError('ALOHA Coffee 审核子集元数据与 Episode 0–4 清单不一致')
            elif (info.get('total_episodes') not in (None, 50)
                  or len(episodes) != 50 or indices != list(range(50))):
                raise ValueError('ALOHA Coffee 原始数据必须符合固定 50 条任务配置')
    else:
        version = info.get('codebase_version', 'v2.0')
        if generic:
            if version not in {'v2.0', 'v2.1'}:
                raise ValueError(f'通用 LeRobot 输入只支持 v2.0/v2.1，检测到 {version!r}')
        else:
            if version != 'v2.0':
                raise ValueError('当前仅验证 SO-100 v2.0 和本项目 Panda/Lift v3.0 数据配置')
            if manifest and (manifest.get('repo_id') != REPO_ID or manifest.get('revision') != REVISION):
                raise ValueError('SO-100 来源清单的仓库或 revision 与固定配置不一致')
        episodes = _legacy_episodes(root)
        tasks = _legacy_tasks(root)
        declared = manifest.get('episodes')
        if declared is not None:
            if (not isinstance(declared, list) or not declared
                    or any(isinstance(i, bool) or not isinstance(i, int) for i in declared)
                    or len(set(declared)) != len(declared)):
                raise ValueError('来源清单的 episode 范围无效')
            absent = sorted(set(declared) - set(episodes))
            if absent:
                raise ValueError(f'来源清单引用了元数据中不存在的 episode：{absent}')
            indices = sorted(declared)
        else:
            indices = sorted(episodes) if generic else list(EPISODES)
    return {'info': info, 'episodes': episodes, 'tasks': tasks, 'manifest': manifest,
            'source': _source(info, manifest, generic=generic), 'episode_indices': indices,
            'generic': generic}


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
    camera_paths = episode_camera_paths(root, metadata, index)
    table = pd.read_parquet(data_path)
    video_start, video_end = 0.0, None
    camera_segments = {}
    if metadata['info'].get('codebase_version') == 'v3.0':
        # Global dataset indices are values, not iloc offsets into a particular shard.
        table = table.loc[table['episode_index'] == index].reset_index(drop=True)
        start, end = int(entry['dataset_from_index']), int(entry['dataset_to_index'])
        if len(table) and (table['index'].min() < start or table['index'].max() >= end):
            raise ValueError(f'任务 {index} 的数据索引超出元数据范围 [{start}, {end})')
        for camera in metadata['source'].get('cameras', [metadata['source']['camera']]):
            start = float(entry[f'videos/{camera}/from_timestamp'])
            end = float(entry[f'videos/{camera}/to_timestamp'])
            if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
                raise ValueError(f'任务 {index} 的 {camera} 视频片段范围无效')
            camera_segments[camera] = {'from_timestamp': start, 'to_timestamp': end}
        first_camera = metadata['source']['camera']
        video_start = camera_segments[first_camera]['from_timestamp']
        video_end = camera_segments[first_camera]['to_timestamp']
    else:
        cameras = metadata['source'].get('cameras') or [metadata['source']['camera']]
        camera_segments = {camera: {'from_timestamp': 0.0, 'to_timestamp': None}
                           for camera in cameras}
    names = metadata["info"]["features"].get("observation.state", {}).get("names")
    if isinstance(names, dict):
        names = names.get("motors", next(iter(names.values()), None))
    state_shape = metadata["info"]["features"].get("observation.state", {}).get("shape")
    state_dim = int(state_shape[0]) if isinstance(state_shape, (list, tuple)) and state_shape else None
    if (not isinstance(names, list) or not all(isinstance(x, str) for x in names)
            or (state_dim is not None and len(names) != state_dim)):
        names = list(FIELD_NAMES) if state_dim == len(FIELD_NAMES) else [
            f"state_{i}" for i in range(state_dim or len(FIELD_NAMES))]
    source = metadata['source']
    return Episode(episode_index=index, table=table,
                   metadata={**metadata, "episode": entry}, video_path=video_path,
                   data_path=data_path, field_names=names, fps=float(metadata["info"]["fps"]),
                   source=source, expected_length=length,
                   video_start_time=video_start, video_end_time=video_end,
                   camera_paths=camera_paths, camera_segments=camera_segments)


def input_relative_paths(root: Path, metadata: dict | None = None) -> tuple[str, ...]:
    root = Path(root).resolve()
    if metadata is None:
        try:
            metadata = load_metadata(root)
        except (ValueError, OSError, KeyError):
            metadata = None
    if metadata and metadata.get('generic'):
        paths = set()
        for folder in ('meta', 'data', 'videos'):
            paths.update(path.relative_to(root).as_posix()
                         for path in (root / folder).rglob('*') if path.is_file())
        return tuple(sorted(paths))
    if not metadata or metadata.get('info', {}).get('codebase_version') != 'v3.0':
        declared = (metadata or {}).get('manifest', {}).get('files')
        if isinstance(declared, list) and declared:
            paths = {entry['path'] for entry in declared
                     if isinstance(entry, dict) and isinstance(entry.get('path'), str)}
            return tuple(sorted(paths | {MANIFEST_NAME, DEMO_MANIFEST_NAME}))
        return (*SAMPLE_FILES, MANIFEST_NAME, DEMO_MANIFEST_NAME)
    paths = {'meta/info.json', 'meta/tasks.parquet', MANIFEST_NAME}
    for folder in ('meta', 'data', 'videos'):
        paths.update(p.relative_to(root).as_posix() for p in (root / folder).rglob('*') if p.is_file())
    for index in metadata['episode_indices']:
        paths.update(p.relative_to(root).as_posix() for p in episode_paths(root, metadata, index))
        paths.update(p.relative_to(root).as_posix() for p in episode_camera_paths(root, metadata, index).values())
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
