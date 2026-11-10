from fastapi.testclient import TestClient

from robodata.importing import save_upload
from robodata.web import create_app


def test_health_and_loopback_origin_guard():
    client = TestClient(create_app())
    assert client.get('/api/health').json()['scope'] == 'loopback'
    denied = client.get('/api/health', headers={'Origin': 'https://example.invalid'})
    assert denied.status_code == 403


def test_batch_list_does_not_expose_local_filesystem_paths(monkeypatch):
    batch = {
        'batch_id': 'batch_' + 'a' * 32,
        'source': {'kind': 'aloha', 'data_root': 'C:/private/data', 'input_fingerprint': 'abc'},
        'episodes': [],
        'metadata': {'archive_path': 'C:/private/archive.zip'},
    }
    monkeypatch.setattr('robodata.web.business.safe_list_batches', lambda: {'batches': [batch], 'errors': []})
    result = TestClient(create_app()).get('/api/batches').json()['batches'][0]
    assert result['source'] == {'kind': 'aloha', 'input_fingerprint': 'abc'}
    assert 'archive_path' not in result['metadata']


def test_start_run_returns_accepted_run_id(monkeypatch):
    monkeypatch.setattr('robodata.web._start_request', lambda request: 'run_demo')
    response = TestClient(create_app()).post('/api/runs', json={'operation': 'verify_delivery', 'archive_path': 'demo.zip'})
    assert response.status_code == 202
    assert response.json() == {'run_id': 'run_demo', 'status': 'queued'}


def test_hdf5_upload_copies_from_stream(monkeypatch, tmp_path):
    from io import BytesIO

    monkeypatch.setenv('ROBODATA_BUSINESS_ROOT', str(tmp_path / 'business'))
    payload = b'hdf5-upload-fixture'
    saved = save_upload(BytesIO(payload), 'dataset.h5')
    assert saved.read_bytes() == payload
    assert saved.name == 'uploaded.hdf5'
