"""Copy local inputs into a managed batch, then build preview and inventory.

The format-specific work lives in ``robodata.adapters``. This module is the
stable orchestration layer: it owns the managed directory, the input
fingerprint, and the business-stage entry points.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import uuid
from typing import BinaryIO

from .adapters import get_adapter
from .adapters.support import (digest as _digest, file_records as _files,
                               preview_worker as _preview_worker, read_json as _json,
                               write_json as _write)
from .matching import SOURCE_KIND as MATCHING_SOURCE_KIND
from .matching import assert_source_unchanged as assert_matching_source_unchanged
from .source import file_sha256
from .sources import HDF5_SHA256


def _upload_limit() -> int:
    """可用 ROBODATA_MAX_UPLOAD_MB 调整；默认 2 GiB，防止无界写满本机磁盘。"""
    try:
        value = int(os.environ.get('ROBODATA_MAX_UPLOAD_MB', '2048'))
    except ValueError as exc:
        raise ValueError('ROBODATA_MAX_UPLOAD_MB 必须是正整数') from exc
    if value <= 0:
        raise ValueError('ROBODATA_MAX_UPLOAD_MB 必须是正整数')
    return value * 1024 * 1024


def save_upload(data: bytes | BinaryIO, filename: str) -> Path:
    if Path(filename).suffix.lower() not in {'.hdf5', '.h5'}:
        raise ValueError('当前上传入口只接受已验证配置的 HDF5 文件')
    limit = _upload_limit()
    root = Path(os.environ.get('ROBODATA_BUSINESS_ROOT', Path(__file__).resolve().parents[1] / 'work/business')) / 'uploads' / uuid.uuid4().hex
    root.mkdir(parents=True)
    path = root / 'uploaded.hdf5'
    try:
        with path.open('wb') as destination:
            if isinstance(data, bytes):
                if len(data) > limit:
                    raise ValueError(f'上传文件超过本机上限 {limit // (1024 * 1024)} MiB')
                destination.write(data)
            else:
                written = 0
                while True:
                    chunk = data.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > limit:
                        raise ValueError(f'上传文件超过本机上限 {limit // (1024 * 1024)} MiB')
                    destination.write(chunk)
    except Exception:
        path.unlink(missing_ok=True)
        root.rmdir()
        raise
    return path


def input_fingerprint(source: dict) -> str:
    manifest = _json(source['manifest_path'])
    raw = Path(source['managed_path']) / 'source'
    actual = _files(raw, [item['path'] for item in manifest['files']])
    return _digest({'files': actual, 'interpretation': manifest['interpretation']})


def assert_input_unchanged(source: dict):
    if source.get('kind') == MATCHING_SOURCE_KIND:
        assert_matching_source_unchanged(source)
        return
    if input_fingerprint(source) != source['input_fingerprint']:
        raise ValueError('批次管理副本的输入文件已变化，请重新导入；当前检查和产物不可继续使用')


def verify_managed_source(source: dict) -> str:
    assert_input_unchanged(source)
    return source['input_fingerprint']


def _copy(request, run):
    adapter = get_adapter(request['source_kind'])
    managed = Path(request['managed_dir']).resolve()
    raw = managed / 'source'
    if raw.exists():
        raise ValueError('本次导入暂存目录已存在，不能覆盖既有数据')
    if request['source_kind'] not in {'aloha', MATCHING_SOURCE_KIND}:
        source = Path(request['input_path']).resolve()
        if source == managed or managed.is_relative_to(source):
            raise ValueError('导入目标不能位于输入目录内')
    raw.mkdir(parents=True)
    prepared = adapter.prepare(request, raw, managed)
    fingerprint = _digest({'files': prepared.files, 'interpretation': prepared.interpretation})
    _write(managed / 'input_manifest.json',
           {'schema_version': 1, 'files': prepared.files, 'interpretation': prepared.interpretation,
            'input_fingerprint': fingerprint})
    return {'managed_path': str(managed), 'input_fingerprint': fingerprint,
            'manifest_path': str(managed / 'input_manifest.json')}


def _preview(request, run):
    adapter = get_adapter(request['source_kind'])
    return adapter.preview(request, Path(request['managed_dir']), run)


def _inventory(request, run):
    adapter = get_adapter(request['source_kind'])
    result = adapter.inventory(request, Path(request['managed_dir']), run)
    assert_input_unchanged(result['source'])
    return result


def _quality(request, run):
    from .business import get_store
    from .quality import run_batch, json_safe
    from .reports import write_report
    from .config import APP_VERSION
    batch = get_store(request['business_root']).get_batch(request['batch_id'])
    source = request['source']
    inventory = request['inventory']
    assert_input_unchanged(source)
    if source['kind'] == 'hdf5':
        fresh = run / 'quality_preview'
        _preview_worker(Path(source['hdf5_path']), fresh, source.get('test_label'))
        report = _json(fresh / 'quality_report.json')
        actual_sha = file_sha256(Path(source['hdf5_path']))
        if report.get('source', {}).get('sha256') != actual_sha:
            raise ValueError('原始 HDF5 质检报告与本次输入 SHA-256 不一致')
        for result in report['episodes']:
            result.setdefault('row_count', next(e['row_count'] for e in inventory if e['episode_index'] == result['episode_index']))
            result.setdefault('expected_length', result['row_count'])
            result.setdefault('video', {'status': 'not_applicable', 'note': '原始 HDF5 图像数组，无视频编码'})
    else:
        report = run_batch(Path(source['data_root']), episode_indices=[e['episode_index'] for e in inventory],
                           source_kind='injected_test' if source.get('test_label') else 'original_public')
    if report.get('rule_version') != request['rule_version']:
        raise ValueError('质检解析器规则版本与冻结请求不一致')
    report['dataset_input_fingerprint'] = report.get('input_fingerprint')
    report['input_fingerprint'] = source['input_fingerprint']
    report.update(run_id=run.name, batch_id=batch['batch_id'], app_version=APP_VERSION,
                  episode_indices=[e['episode_index'] for e in inventory])
    report['source'] = {**report.get('source', {}), **source,
                        'source_kind': 'injected_test' if source.get('test_label') else 'original_public'}
    if source['kind'] == 'hdf5':
        report['source'].update(
            integrity_status='differs_from_manifest' if source.get('test_label') else 'matches_manifest',
            integrity_note='管理副本已重新计算 SHA-256；故障副本保留独立测试标记，原件未修改',
            input_files=[{'path': 'test.hdf5', 'status': 'present',
                          'size_bytes': Path(source['hdf5_path']).stat().st_size,
                          'sha256': actual_sha, 'hash_matches_manifest': actual_sha == HDF5_SHA256}])
    report.setdefault('params', {})
    report.setdefault('limitations', ['HDF5 时间和索引为派生预览字段，不代表源硬件同步'])
    report.setdefault('started_at_utc', __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat())
    report.setdefault('runtime_seconds', 0)
    report['issues'] = [issue for ep in report['episodes'] for issue in ep['issues']]
    coverage = [c for ep in report['episodes'] for c in ep['coverage']]
    report['summary'] = {'episode_count': len(report['episodes']),
                         'row_count': sum(e['row_count'] for e in report['episodes']),
                         'issue_count': len(report['issues']),
                         'data_issue_count': sum(i.get('category') == 'data_issue' for i in report['issues']),
                         'load_failure_count': sum(i.get('category') == 'load_failure' for i in report['issues']),
                         'not_checked_count': sum(i.get('category') == 'not_checked' for i in report['issues']),
                         'passed_episode_count': sum(e['status'] == 'passed' for e in report['episodes']),
                         'coverage_total': len(coverage),
                         'coverage_performed': sum(c['status'] == 'performed' for c in coverage)}
    assert_input_unchanged(source)
    report = json_safe(report)
    report['result_digest'] = _digest({k: v for k, v in report.items()
                                       if k not in {'run_id', 'started_at_utc', 'runtime_seconds',
                                                    'dataset_input_fingerprint'}})
    paths = write_report(report, run / 'reports')
    return {'report_path': str(paths['json']), 'input_fingerprint': source['input_fingerprint'],
            'required_rules': source['required_rules']}


def preview_batch(batch: dict, episode_index: int, row: int = 0, camera: str | None = None) -> dict:
    import numpy as np
    import pandas as pd
    source = batch['source']
    assert_input_unchanged(source)
    if source['kind'] in {'so100', 'aloha', 'lerobot_v21'}:
        from .dataset import load_episode
        from .video import episode_camera, read_video_frame
        episode = load_episode(Path(source['data_root']), episode_index)
        row = max(0, min(row, len(episode.table) - 1))
        camera_names = list(episode.camera_paths or {episode.source.get('camera', 'camera'): episode.video_path})
        camera = camera if camera in camera_names else camera_names[0]
        selected = episode_camera(episode, camera)
        image = None
        issue = []
        try:
            timestamp = float(episode.table.iloc[row]['timestamp']) + selected.video_start_time
            image = read_video_frame(selected.video_path, timestamp)['image']
        except Exception as exc:
            issue = [{'message': str(exc), 'field': 'video', 'camera': camera}]
        return {'image': image, 'table': episode.table, 'state_names': episode.state_names,
                'action_names': episode.action_names, 'fps': episode.fps, 'issues': issue,
                'source': episode.source, 'row_index': row, 'camera': camera,
                'cameras': camera_names, 'camera_metadata': episode.source.get('camera_metadata', {})}
    root = Path(source['preview_root'])
    expected = _json(Path(source['managed_path']) / 'preview_manifest.json')['files']
    if _files(root, [e['path'] for e in expected]) != expected:
        raise ValueError('原始预览缓存已变化，请重新导入；不能用变化后的缓存代表输入')
    metadata = _json(root / 'metadata.json')
    episode = next(e for e in metadata['episodes'] if e['episode_index'] == episode_index)
    table = pd.read_parquet(root / episode['table_path']) if episode.get('table_path') else pd.DataFrame()
    image = None
    if episode.get('images_path'):
        with np.load(root / episode['images_path'], allow_pickle=False) as archive:
            images = archive['images']
            if 0 <= row < len(images):
                image = images[row]
    return {'image': image, 'table': table, 'state_names': metadata['state_names'],
            'action_names': metadata['action_names'], 'fps': metadata['fps'],
            'issues': episode['issues'], 'source': source, 'row_index': row}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--stage', choices=['copy', 'preview', 'inventory', 'quality'], required=True)
    parser.add_argument('--run-dir', type=Path, required=True)
    args = parser.parse_args()
    run = args.run_dir.resolve()
    request = _json(run / 'request.json')
    try:
        details = globals()['_' + args.stage](request, run)
        _write(run / 'steps' / f'{args.stage}.json', {'stage': args.stage, 'status': 'passed', 'details': details})
        return 0
    except Exception as exc:
        from .errors import explain_error, error_details
        diagnostic = {'stage': args.stage, 'message_zh': explain_error(exc),
                      'input_path': request.get('input_path'),
                      **error_details(exc, include_traceback=True), **getattr(exc, 'details', {})}
        _write(run / 'diagnostic.json', diagnostic)
        _write(run / 'steps' / f'{args.stage}.json', {'stage': args.stage, 'status': 'failed',
                                                      'diagnostic': diagnostic})
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
