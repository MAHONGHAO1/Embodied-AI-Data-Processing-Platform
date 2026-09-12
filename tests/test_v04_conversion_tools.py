"""Business selection and pre-conversion HDF5 evidence without another scheduler."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess

import pytest

from robodata.config import PROJECT_ROOT
from robodata.conversion import CONVERSION_PYTHON
from robodata.sources import HDF5_PATH


def worker_module():
    spec = importlib.util.spec_from_file_location("conversion_worker_test", PROJECT_ROOT / "tools/conversion/worker.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def selection():
    value = {"batch_id": "batch-test", "content_version": 3, "input_fingerprint": "fixed-input",
             "episodes": [{"source_episode_index": source, "output_episode_index": target,
                           "row_count": [59, 58, 57][source], "annotation": {"task": task, "outcome": "success", "tags": ["fixture"], "notes": "人工审核测试"},
                           "quality_run_id": "quality-fixture"} for target, (source, task) in enumerate([(0, "拿起物体"), (2, "抬起方块")])]}
    value["snapshot_id"] = hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    return value


def test_spec_preserves_noncontinuous_source_mapping_and_annotation(tmp_path):
    module = worker_module()
    path = tmp_path / "conversion-spec.json"
    value = selection()
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    assert module.load_conversion_spec(path) == value
    assert module.load_conversion_spec(None) is None
    value["episodes"][1]["annotation"]["task"] = "被修改但未更新快照"
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(module.ConversionError, match="快照摘要不一致"):
        module.load_conversion_spec(path)


@pytest.mark.parametrize("mutation", ["duplicate", "renumber", "empty"])
def test_spec_rejects_invalid_selection(tmp_path, mutation):
    module = worker_module()
    value = selection()
    if mutation == "duplicate":
        value["episodes"][1]["source_episode_index"] = 0
    elif mutation == "renumber":
        value["episodes"][1]["output_episode_index"] = 2
    else:
        value["episodes"] = []
    path = tmp_path / "invalid-spec.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(module.ConversionError):
        module.load_conversion_spec(path)


def conversion_process(code, *arguments):
    if not CONVERSION_PYTHON.is_file() or not HDF5_PATH.is_file():
        pytest.skip("requires pinned HDF5 and separate conversion environment")
    env = {**os.environ, "PYTHONUTF8": "1", "HF_HUB_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1"}
    result = subprocess.run([str(CONVERSION_PYTHON), "-c", code, *map(str, arguments)], cwd=PROJECT_ROOT,
                            env=env, capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


def test_preview_real_source_and_selection_mapping_without_conversion(tmp_path):
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(selection(), ensure_ascii=False), encoding="utf-8")
    conversion_process("""
import sys, json
from pathlib import Path
sys.path.insert(0, 'tools/conversion')
from preview import build_preview
from worker import sha256, load_conversion_spec, inspect_hdf5, map_arrays
source, out, spec_path = map(Path, sys.argv[1:])
before = sha256(source)
meta = build_preview(source, out)
assert meta['source_statistics'] == {'declared_total':9666,'actual_total':531,'demo_count':10}
report = json.loads((out/'quality_report.json').read_text(encoding='utf-8'))
assert report['summary']['coverage_performed'] == report['summary']['coverage_total'] == 30
assert report['summary']['row_count'] == 174 and not report['issues']
assert all(e['status']=='passed' for e in meta['episodes'])
mapping = inspect_hdf5(source, load_conversion_spec(spec_path))
assert [e['source_demo'] for e in mapping['episodes']] == ['demo_0','demo_2']
assert [e['episode_index'] for e in mapping['episodes']] == [0,1]
assert [e['task'] for e in mapping['episodes']] == ['拿起物体','抬起方块']
assert mapping['selected_frames'] == 116 and mapping['selected_episodes'] == 2
arrays = map_arrays(source, mapping)
assert arrays[1]['state'].shape == (57,9) and arrays[1]['action'].shape == (57,7)
assert sha256(source) == before
assert not (out/'meta/info.json').exists()
""", HDF5_PATH, tmp_path / "preview", spec_path)


def test_preview_nan_missing_and_length_errors_isolate_bad_episodes(tmp_path):
    conversion_process("""
import sys, shutil, json
from pathlib import Path
import h5py
import numpy as np
import pandas as pd
sys.path.insert(0, 'tools/conversion')
from preview import build_preview
from worker import sha256
source, temp = map(Path, sys.argv[1:])
before = sha256(source)
copy = temp/'explicit-three-errors.hdf5'
shutil.copy2(source, copy)
with h5py.File(copy,'r+') as f:
    f['data/demo_0/actions'][10,2] = np.nan
    del f['data/demo_1/obs/robot0_eef_pos']
    images = f['data/demo_2/obs/agentview_image'][:-1]
    del f['data/demo_2/obs/agentview_image']
    f['data/demo_2/obs'].create_dataset('agentview_image',data=images)
meta = build_preview(copy,temp/'fault-preview','NaN、缺字段、图像长度测试')
assert [e['status'] for e in meta['episodes']] == ['issues','incomplete','issues']
issues = [i for e in meta['episodes'] for i in e['issues']]
assert any(i['rule']=='numeric_finite' and i['row_index']==10 and i['episode_index']==0 for i in issues)
assert any(i['rule']=='required_fields' and i['episode_index']==1 for i in issues)
assert any(i['rule']=='image_frame_count' and i['episode_index']==2 for i in issues)
table = pd.read_parquet(temp/'fault-preview'/meta['episodes'][1]['table_path'])
assert 'observation.state' not in table and 'action' in table
assert all(e['table_path'] for e in meta['episodes'])
assert meta['source']['matches_fixed_source'] is False and meta['source']['test_mode']
assert sha256(source)==before
""", HDF5_PATH, tmp_path)


def test_preview_one_bad_task_does_not_hide_other_tasks(tmp_path):
    conversion_process("""
import sys, shutil
from pathlib import Path
import h5py
sys.path.insert(0,'tools/conversion')
from preview import build_preview
source,temp=map(Path,sys.argv[1:])
copy=temp/'explicit-missing-task.hdf5'
shutil.copy2(source,copy)
with h5py.File(copy,'r+') as f: del f['data/demo_0']
meta=build_preview(copy,temp/'preview','缺任务测试')
assert [e['status'] for e in meta['episodes']] == ['load_failed','passed','passed']
assert len(meta['episodes'][0]['coverage'])==10
assert meta['episodes'][1]['images_path']
""", HDF5_PATH, tmp_path)
