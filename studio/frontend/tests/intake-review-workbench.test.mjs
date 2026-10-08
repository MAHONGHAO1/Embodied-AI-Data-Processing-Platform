import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../js/intake-review-workbench.js', import.meta.url), 'utf8');

test('mounted workbench uses the lexical API client and saves before leaving', async () => {
  let component, cleanup, exposed, saved;
  const api = {
    async getDataPackage() { return { id: 4, workspace_id: 3, status: 'pending_intake_review', episodes: [{ id: 7, admission_status: 'passed', preview_available: false }] }; },
    async getIntakeReviewDraft() { return { data_package_id: 4, workspace_id: 3, draft_version: 0, source_fingerprint: 'a'.repeat(64), editable: true, draft: {} }; },
    async saveIntakeReviewDraft(id, body) { saved = { id, body }; return { draft_version: 1, source_fingerprint: 'a'.repeat(64), editable: true, draft: body.draft }; },
  };
  const sandbox = { __api: api, setTimeout, clearTimeout, structuredClone, Vue: {
    reactive: value => value, ref: value => ({ value }), computed: fn => ({ get value() { return fn(); } }),
    watch() {}, onMounted() {}, onBeforeUnmount(fn) { cleanup = fn; },
  } };
  vm.createContext(sandbox);
  vm.runInContext(`const QuicDataAPI = __api;\n${source}\nglobalThis.module = QuicDataIntakeReviewWorkbench;`, sandbox);
  assert.equal(sandbox.QuicDataAPI, undefined, 'API is a lexical global, as in the browser');
  sandbox.module.install({ component(name, value) { component = value; } });
  const view = component.setup({ workspaceId: 3, packageId: 4, locale: 'en-US' }, { emit() {}, expose(value) { exposed = value; } });
  await view.loadScope();
  assert.equal(view.state.loadError, '');
  assert.equal(view.state.episodes.length, 1);
  await view.selectEpisode(7);
  assert.equal(exposed.hasUnsaved(), true);
  assert.equal(await exposed.canLeave(), true);
  assert.equal(saved.id, 4);
  assert.equal(saved.body.base_version, 0);
  assert.deepEqual(Array.from(saved.body.draft.viewed_episode_ids), [7]);
  assert.equal(exposed.hasUnsaved(), false);
  cleanup();
});

function loadWorkbench() {
  const sandbox = {
    console,
    setTimeout,
    clearTimeout,
    structuredClone,
    Date,
    Promise,
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(`${source}\n;globalThis.__workbench = QuicDataIntakeReviewWorkbench;`, sandbox, {
    filename: 'intake-review-workbench.js',
  });
  return sandbox.__workbench;
}

test('package normalization separates eligible Episodes from system exclusions and blocks running packages', () => {
  const workbench = loadWorkbench();
  const packageData = workbench.normalizePackage({
    id: 42,
    workspace_id: 7,
    status: 'pending_intake_review',
    admission_counts: { ready: 1, running: 1, failed: 1 },
    episodes: [
      { id: 100, episode_uid: 'ok', admission_status: 'passed', duration_s: 12 },
      { id: 101, episode_uid: 'bad', admission_status: 'failed', admission_reason: 'integrity_failed' },
      { id: 102, episode_uid: 'running', admission_status: 'running', admission_reason: 'preview_pending' },
    ],
  });

  assert.equal(packageData.episodes[0].eligible, true);
  assert.equal(packageData.episodes[1].eligible, false);
  assert.equal(packageData.episodes[1].exclusionReason, 'integrity_failed');
  assert.equal(packageData.admission_counts.ready, 1);
  assert.equal(packageData.admission_counts.running, 1);
  assert.equal(packageData.canReview, false);
  assert.equal(workbench.reviewCounts({ packageData, episodes: packageData.episodes }).excluded, 2);
});

test('draft and final payloads preserve the current rejected map while applying API shapes', () => {
  const workbench = loadWorkbench();
  const draft = workbench.buildDraftPayload({
    workspaceId: '7',
    baseVersion: 0,
    sourceFingerprint: 'a'.repeat(64),
    viewedEpisodeIds: [3, '2', 2, null],
    rejectedEpisodes: { 3: '  camera blocked  ', 2: '' },
    lastEpisodeId: '3',
  });
  assert.deepEqual(JSON.parse(JSON.stringify(draft)), {
    workspace_id: 7,
    base_version: 0,
    source_fingerprint: 'a'.repeat(64),
    draft: {
      viewed_episode_ids: [2, 3],
      rejected_episodes: { 2: '', 3: '  camera blocked  ' },
      last_episode_id: 3,
    },
  });

  const final = workbench.buildReviewPayload({
    workspaceId: 7,
    verdict: 'approved',
    baseVersion: 4,
    sourceFingerprint: 'b'.repeat(64),
    rejectedEpisodes: { 3: ' camera blocked ', 2: '' },
  });
  assert.deepEqual(JSON.parse(JSON.stringify(final)), {
    workspace_id: 7,
    verdict: 'approved',
    rejected_episode_ids: [2, 3],
    reason: '',
    base_version: 4,
    source_fingerprint: 'b'.repeat(64),
    episode_reasons: { 2: '', 3: ' camera blocked ' },
  });
});

test('scope aware package loader ignores late responses from an older package', async () => {
  const workbench = loadWorkbench();
  const pending = [];
  const api = {
    getDataPackage(packageId) {
      return new Promise((resolve) => pending.push({ kind: 'package', packageId, resolve }));
    },
    getIntakeReviewDraft(packageId) {
      return new Promise((resolve) => pending.push({ kind: 'draft', packageId, resolve }));
    },
  };
  const loader = workbench.createLoader(api);
  const first = loader.load(10, 7);
  const second = loader.load(11, 7);

  for (const request of pending.filter((item) => item.packageId === 10)) {
    request.resolve(request.kind === 'package'
      ? { id: 10, workspace_id: 7, status: 'pending_intake_review', episodes: [] }
      : { data_package_id: 10, workspace_id: 7, source_fingerprint: 'a'.repeat(64), draft: {} });
  }
  const stale = await first;
  assert.equal(stale.stale, true);

  for (const request of pending.filter((item) => item.packageId === 11)) {
    request.resolve(request.kind === 'package'
      ? { id: 11, workspace_id: 7, status: 'pending_intake_review', episodes: [] }
      : { data_package_id: 11, workspace_id: 7, source_fingerprint: 'b'.repeat(64), draft: {} });
  }
  const current = await second;
  assert.equal(current.stale, false);
  assert.equal(current.package.id, 11);
  assert.equal(current.draft.source_fingerprint, 'b'.repeat(64));
});

test('preview loader rejects a response for the wrong Episode and keeps stale media out', async () => {
  const workbench = loadWorkbench();
  let resolve;
  const api = {
    getDataPackageEpisodePreviewUrls() {
      return new Promise((done) => { resolve = done; });
    },
  };
  const loader = workbench.createPreviewLoader(api);
  const request = loader.load(10, 4, 7);
  await Promise.resolve();
  resolve({ data_package_id: 10, workspace_id: 7, episode_id: 5, streams: [{ video_url: 'wrong.mp4' }] });
  await assert.rejects(request, (error) => error.code === 'stale_media_response' && error.stale === true);
  assert.equal(workbench.scopeMatches({ episode_id: 5 }, { episodeId: 4 }), false);
});

test('autosave is single flight and drains edits made while the first save is pending', async () => {
  const workbench = loadWorkbench();
  const requests = [];
  const pending = [];
  const coordinator = workbench.createAutosaveCoordinator({
    capture: (revision) => ({ revision }),
    save: (snapshot) => {
      requests.push(snapshot.revision);
      return new Promise((resolve) => pending.push(resolve));
    },
    debounceMs: 999999,
    maxWaitMs: 999999,
  });
  coordinator.markDirty();
  const firstFlush = coordinator.flush({ drain: true, reason: 'test' });
  await Promise.resolve();
  assert.deepEqual(Array.from(requests), [1]);
  coordinator.markDirty();
  pending.shift()({ draft_version: 1 });
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(Array.from(requests), [1, 2]);
  pending.shift()({ draft_version: 2 });
  assert.equal(await firstFlush, true);
  assert.equal(coordinator.state().dirty, false);
});

test('install registers a component without requiring app.js state', () => {
  const workbench = loadWorkbench();
  let registered = null;
  const app = { component(name, definition) { registered = { name, definition }; } };
  workbench.install(app);
  assert.equal(registered.name, 'intake-review-workbench');
  assert.equal(registered.definition.props.workspaceId.type.length, 2);
  assert.equal(registered.definition.props.workspaceId.type[0].name, 'Number');
  assert.equal(registered.definition.props.workspaceId.type[1].name, 'String');
  assert.deepEqual(Array.from(registered.definition.emits), ['back', 'completed']);
});
