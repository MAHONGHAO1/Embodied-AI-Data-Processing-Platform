import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../js/episode-detail.js', import.meta.url), 'utf8');

function loadEpisodeDetail() {
  const context = {};
  context.globalThis = context;
  vm.runInNewContext(`${source}\n;globalThis.__episodeDetail = QuicDataEpisodeDetail;`, context);
  return context.__episodeDetail;
}

function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

test('normalizes plain Episodes and work-queue rows into read-only drawer contexts', () => {
  const detail = loadEpisodeDetail();
  const plainContext = plain(detail.normalizeContext(
    { id: 7, episode_uid: 'src_7', kind: 'source' },
    { source: 'assets' },
  ));
  const queuedContext = plain(detail.normalizeContext({
    episode: { id: 8, episode_uid: 'drv_8', kind: 'derived' },
    work_item: {
      id: 91,
      kind: 'annotation',
      status: 'pending',
      updated_at: '2026-08-24T10:00:00Z',
    },
    preview_status: 'queued',
  }, { source: 'work-queue', queueStage: 'annotation' }));

  assert.deepEqual(plainContext, {
    source: 'assets',
    episodeId: 7,
    episode: { id: 7, episode_uid: 'src_7', kind: 'source' },
    workItem: null,
    queueStage: '',
    previewStatus: '',
  });
  assert.deepEqual(queuedContext, {
    source: 'work-queue',
    episodeId: 8,
    episode: { id: 8, episode_uid: 'drv_8', kind: 'derived' },
    workItem: {
      id: 91,
      kind: 'annotation',
      status: 'pending',
      updated_at: '2026-08-24T10:00:00Z',
    },
    queueStage: 'annotation',
    previewStatus: 'queued',
  });
});

test('normalization copies only drawer-safe summary fields without mutating input', () => {
  const detail = loadEpisodeDetail();
  const row = {
    episode: {
      id: 12,
      episode_uid: 'drv_12',
      published_at: '2026-08-24T11:00:00Z',
      storage_uri: 'oss://private/object',
      metadata_json: { secret: 'value' },
    },
    work_item: {
      id: 101,
      kind: 'review',
      status: 'assigned',
      updated_at: '2026-08-24T11:01:00Z',
      available_actions: ['review', 'release'],
    },
  };
  const before = JSON.stringify(row);

  const context = plain(detail.normalizeContext(row, {
    source: 'work-queue',
    queueStage: 'review',
  }));

  assert.equal(JSON.stringify(row), before);
  assert.equal(context.episode.published_at, '2026-08-24T11:00:00Z');
  assert.equal('storage_uri' in context.episode, false);
  assert.equal('metadata_json' in context.episode, false);
  assert.equal('available_actions' in context.workItem, false);
});

test('merges a full detail without losing list-only publication time or mutating sources', () => {
  const detail = loadEpisodeDetail();
  const context = detail.normalizeContext({
    id: 15,
    episode_uid: 'drv_15',
    published_at: '2026-08-24T12:00:00Z',
    publication: { category: 'published', status: 'succeeded' },
  }, { source: 'assets' });
  const response = {
    id: 15,
    episode_uid: 'drv_15',
    kind: 'derived',
    published_at: null,
    publication: { status: 'succeeded' },
    artifacts: [{ artifact_type: 'official_qrdf' }],
  };
  const beforeContext = JSON.stringify(context);
  const beforeResponse = JSON.stringify(response);

  const merged = plain(detail.mergeEpisodeDetail(context, response));

  assert.equal(merged.published_at, '2026-08-24T12:00:00Z');
  assert.deepEqual(merged.publication, { category: 'published', status: 'succeeded' });
  assert.deepEqual(merged.artifacts, [{ artifact_type: 'official_qrdf' }]);
  assert.equal(JSON.stringify(context), beforeContext);
  assert.equal(JSON.stringify(response), beforeResponse);
});

test('request gate rejects stale responses after switching Episodes or closing the drawer', () => {
  const detail = loadEpisodeDetail();
  const gate = detail.createRequestGate();

  const first = gate.begin(21);
  assert.equal(gate.isCurrent(first, 21), true);

  const second = gate.begin(22);
  assert.equal(gate.isCurrent(first, 21), false);
  assert.equal(gate.isCurrent(second, 22), true);
  assert.equal(gate.isCurrent(second, 21), false);

  gate.invalidate();
  assert.equal(gate.isCurrent(second, 22), false);
});
