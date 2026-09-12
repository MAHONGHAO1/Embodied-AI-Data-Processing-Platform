"""Verify the actual registered export with the light reader and official pixels."""
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
from robodata.config import PROJECT_ROOT, APP_VERSION
from robodata.conversion import latest_export
from robodata.dataset import load_metadata, load_episode, dataset_fingerprint
from robodata.quality import run_batch
from robodata.sources import HDF5_PATH, HDF5_SHA256
from robodata.source import file_sha256
from robodata.video import read_video_frame


def main():
    output = latest_export()
    assert output, '没有已登记的有效输出'
    root = Path(output['output_path'])
    run = PROJECT_ROOT / 'work/runs' / output['run_id']
    official = json.loads((run / 'steps/official_verify.json').read_text(encoding='utf-8'))
    assert official['status'] == 'passed'
    assert official['details']['offline']
    assert official['details']['checked_numeric_frames'] == 174
    assert official['details']['batch_size'] == 2
    decoded = np.load(run / 'decoded_samples.npz')
    assert len(decoded['images']) == 9
    metadata = load_metadata(root)
    assert metadata['episode_indices'] == [0,1,2]
    assert metadata['info']['total_frames'] == 174
    assert metadata['info']['splits'] == {'train': '0:3'}
    assert metadata['tasks'][0]['task'] == 'Lift'
    checks = []
    for index, (ep_index, row) in enumerate(zip(decoded['episode_indices'], decoded['frame_indices'])):
        ep = load_episode(root, int(ep_index))
        assert ep.expected_length == (59,58,57)[int(ep_index)]
        assert len(ep.state_names) == 9 and len(ep.action_names) == 7
        timestamp = float(ep.table.iloc[int(row)]['timestamp'])
        frame = read_video_frame(ep.video_path, timestamp + ep.video_start_time)
        # Both decoders read the same encoded frame; this is not a source-losslessness test.
        np.testing.assert_array_equal(frame['image'], decoded['images'][index])
        assert frame['frame_index'] == int(decoded['global_indices'][index])
        checks.append({'episode':int(ep_index), 'row':int(row), 'video_start_time':ep.video_start_time,
                       'absolute_pts':frame['pts_time'], 'official_pixels_equal':True})
    first = run_batch(root)
    second = run_batch(root)
    assert first['summary']['coverage_performed'] == 33
    assert first['summary']['issue_count'] == 0
    assert first['result_digest'] == second['result_digest']
    assert dataset_fingerprint(root) == output['fingerprint']
    assert file_sha256(HDF5_PATH) == HDF5_SHA256
    evidence = {'status':'passed', 'app_version':APP_VERSION,'run_id':output['run_id'],
                'output_path':str(root), 'official_result':str(run/'steps/official_verify.json'),
                'summary':first['summary'], 'frame_checks':checks, 'source_sha256':HDF5_SHA256,
                'result_digest':first['result_digest'], 'repeat_equal':True, 'raw_unchanged':True}
    path = PROJECT_ROOT/'work/v03-acceptance.json'
    path.write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
    print('PASS:', path)

if __name__ == '__main__':
    main()
