"""Shared-video offsets and file-local/global index boundaries are independent."""
import json
from fractions import Fraction
import av
import numpy as np
import pandas as pd
import pytest
from robodata.dataset import load_episode, dataset_fingerprint
from robodata.quality import run_batch
from robodata.sources import V3_CAMERA
from robodata.video import read_video_frame


def v3_fixture(root):
    for p in ['meta/episodes/chunk-000', 'data/chunk-002', f'videos/{V3_CAMERA}/chunk-001']:
        (root / p).mkdir(parents=True)
    info = {'codebase_version': 'v3.0', 'fps': 20,
            'features': {'observation.state': {'shape': [9], 'names': [f's{i}' for i in range(9)]},
                         'action': {'shape': [7], 'names': [f'a{i}' for i in range(7)]},
                         V3_CAMERA: {'dtype': 'video'}},
            'data_path': 'data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet',
            'video_path': 'videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4'}
    (root / 'meta/info.json').write_text(json.dumps(info), encoding='utf-8')
    pd.DataFrame({'task_index': [0]}, index=['Lift']).to_parquet(root / 'meta/tasks.parquet')
    entries, records = [], []
    for ep in range(3):
        entries.append({'episode_index': ep, 'length': 2, 'tasks': ['Lift'],
            'data/chunk_index': 2, 'data/file_index': 5,
            'dataset_from_index': 100 + ep*2, 'dataset_to_index': 102 + ep*2,
            f'videos/{V3_CAMERA}/chunk_index': 1, f'videos/{V3_CAMERA}/file_index': 4,
            f'videos/{V3_CAMERA}/from_timestamp': ep/10, f'videos/{V3_CAMERA}/to_timestamp': (ep+1)/10})
        for frame in range(2):
            records.append({'index': 100+ep*2+frame, 'episode_index': ep, 'frame_index': frame,
                            'timestamp': frame/20, 'task_index': 0,
                            'observation.state': [float(ep)]*9, 'action': [float(frame)]*7})
    pd.DataFrame(entries).to_parquet(root / 'meta/episodes/chunk-000/file-000.parquet')
    pd.DataFrame(records).to_parquet(root / 'data/chunk-002/file-005.parquet')
    video = root / f'videos/{V3_CAMERA}/chunk-001/file-004.mp4'
    with av.open(str(video), 'w') as container:
        stream = container.add_stream('mpeg4', rate=20)
        stream.width = stream.height = 32
        stream.pix_fmt = 'yuv420p'
        for index in range(6):
            frame = av.VideoFrame.from_ndarray(np.full((32,32,3), index*35, np.uint8), format='rgb24')
            frame.pts, frame.time_base = index, Fraction(1,20)
            for packet in stream.encode(frame): container.mux(packet)
        for packet in stream.encode(): container.mux(packet)
    return video


def test_shared_video_segments_and_separate_dimensions(tmp_path):
    v3_fixture(tmp_path)
    report = run_batch(tmp_path)
    assert report['summary']['episode_count'] == 3
    assert report['summary']['coverage_performed'] == 33
    assert report['summary']['issue_count'] == 0
    for ep in range(3):
        episode = load_episode(tmp_path, ep)
        assert episode.table['index'].tolist() == [100+ep*2,101+ep*2]
        assert len(episode.state_names) == 9 and len(episode.action_names) == 7
        frame = read_video_frame(episode.video_path, episode.video_start_time+0.05)
        assert frame['frame_index'] == ep*2+1
        assert abs(float(frame['image'].mean())-(ep*2+1)*35) < 8
        assert report['episodes'][ep]['video']['frame_count'] == 2
        assert report['episodes'][ep]['video']['file_frame_count'] == 6


def test_shared_video_missing_keeps_all_tables(tmp_path):
    video = v3_fixture(tmp_path)
    before = dataset_fingerprint(tmp_path)
    video.unlink()
    assert dataset_fingerprint(tmp_path) != before
    result = run_batch(tmp_path)
    assert result['summary']['row_count'] == 6
    assert result['summary']['load_failure_count'] == 3
    assert result['summary']['coverage_performed'] == 24


def test_unsupported_format_and_path_escape_rejected(tmp_path):
    v3_fixture(tmp_path)
    path = tmp_path / 'meta/info.json'
    info = json.loads(path.read_text())
    info['data_path'] = '../outside.parquet'
    path.write_text(json.dumps(info))
    with pytest.raises(ValueError, match='超出数据目录'):
        load_episode(tmp_path, 0)
    info['codebase_version'] = 'v4.0'
    path.write_text(json.dumps(info))
    with pytest.raises(ValueError, match='仅验证'):
        load_episode(tmp_path, 0)
