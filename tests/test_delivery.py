"""Delivery verifies portable bytes; it never infers official loader success."""
import json
from pathlib import Path
import zipfile

import pytest

from robodata.delivery import _file_records, _validate_payload, verify, verified_delivery, _portable
from robodata.source import file_sha256


def payload(tmp_path):
    root = tmp_path / 'payload'
    (root / 'dataset/meta').mkdir(parents=True)
    (root / 'dataset/meta/info.json').write_text(json.dumps({'codebase_version':'v3.0', 'total_episodes':2, 'total_frames':116}))
    (root / 'dataset/meta/manifest.json').write_text('{}')
    manifest = {'schema_version':1,'format_version':'v3.0','snapshot_id':'frozen',
                'episode_count':2,'row_count':116,'files':_file_records(root)}
    (root / 'manifest.json').write_text(json.dumps(manifest))
    return root


def archive(root, path):
    with zipfile.ZipFile(path,'w') as z:
        for p in root.rglob('*'):
            if p.is_file(): z.write(p,p.relative_to(root).as_posix())
    return path


def test_independent_unpack_and_manifest_hash(tmp_path):
    root = payload(tmp_path)
    z = archive(root, tmp_path/'input.zip')
    run = tmp_path/'run'; run.mkdir()
    result = verify({'archive_path':str(z)},run)
    assert result['manifest_verified'] and result['row_count']==116
    assert result['zip_sha256']==file_sha256(z)
    assert (run/'archive.sha256.json').is_file()
    # Nested manifests are real payload files, only the root manifest excludes itself.
    (Path(result['dataset_path'])/'meta/manifest.json').write_text('{"changed":true}')
    with pytest.raises(ValueError,match='SHA-256'):
        _validate_payload(run/'unpacked')


def test_changed_zip_and_wrong_snapshot_are_rejected(tmp_path):
    root = payload(tmp_path)
    with pytest.raises(ValueError,match='快照'):
        _validate_payload(root,'different')
    z = archive(root,tmp_path/'input.zip')
    run = tmp_path/'run'; (run/'steps').mkdir(parents=True)
    (run/'steps/package.json').write_text(json.dumps({'details':{'zip_path':str(z),'zip_sha256':'0'*64}}))
    with pytest.raises(ValueError,match='整包'):
        verify({},run)


@pytest.mark.parametrize('name',['../escape.txt','C:/escape.txt','folder\\escape.txt','/escape.txt','a//b'])
def test_unsafe_archive_paths_never_extract(tmp_path,name):
    z = tmp_path/'input.zip'
    with zipfile.ZipFile(z,'w') as out: out.writestr(name,'untrusted')
    run = tmp_path/'run'; run.mkdir()
    with pytest.raises(ValueError): verify({'archive_path':str(z)},run)
    assert not (tmp_path/'escape.txt').exists()


def test_missing_changed_or_stale_download_is_not_current(tmp_path):
    z = tmp_path/'real.zip'; z.write_bytes(b'actual bytes')
    item = {'delivery_id':'d','zip_path':str(z),'zip_sha256':file_sha256(z),
            'content_version':3,'current':True,'episodes':[{'row_count':59}]}
    batch = {'content_version':3,'deliveries':[item]}
    assert verified_delivery(batch,'d')['size_bytes']==12
    z.write_bytes(b'changed')
    with pytest.raises(ValueError,match='字节'): verified_delivery(batch,'d')
    batch['content_version']=4
    with pytest.raises(ValueError,match='版本'): verified_delivery(batch,'d')


def test_diagnostics_are_portable():
    value=_portable({'file':r'C:\private\input.hdf5','managed_path':'private',
                     'traceback':'private traceback','source':{'path':'test/test.hdf5'}})
    assert 'private' not in json.dumps(value)
    assert value['source']['path']=='test/test.hdf5'
