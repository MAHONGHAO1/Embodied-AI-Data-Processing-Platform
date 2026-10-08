import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../js/package-annotation-workbench.js', import.meta.url), 'utf8');

function loadWorkbench({ api = null, Vue = null, window = null } = {}) {
  const sandbox = {
    console,
    setTimeout,
    clearTimeout,
    structuredClone,
    Date,
    Promise,
    __lexicalApi: api,
  };
  if (Vue) sandbox.Vue = Vue;
  if (window) sandbox.window = window;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(`const QuicDataAPI = globalThis.__lexicalApi;\n${source}\n;globalThis.__workbench = QuicDataPackageAnnotationWorkbench;`, sandbox, {
    filename: 'package-annotation-workbench.js',
  });
  return sandbox.__workbench;
}

function createVueShim() {
  const watches = [];
  const mounted = [];
  const unmounted = [];
  const same = (left, right) => Array.isArray(left) && Array.isArray(right)
    ? left.length === right.length && left.every((value, index) => value === right[index])
    : left === right;
  return {
    reactive(value) { return value; },
    ref(value) { return { value }; },
    computed(getter) { return { get value() { return getter(); } }; },
    watch(sourceGetter, callback, options = {}) {
      const watcher = { sourceGetter, callback, value: sourceGetter() };
      watches.push(watcher);
      if (options.immediate) callback(watcher.value, undefined);
      return () => {
        const index = watches.indexOf(watcher);
        if (index >= 0) watches.splice(index, 1);
      };
    },
    onMounted(callback) { mounted.push(callback); },
    onBeforeUnmount(callback) { unmounted.push(callback); },
    async flushWatchers() {
      for (const watcher of watches) {
        const next = watcher.sourceGetter();
        if (!same(next, watcher.value)) {
          const previous = watcher.value;
          watcher.value = next;
          await watcher.callback(next, previous);
        }
      }
    },
    mount() { mounted.forEach((callback) => callback()); },
    unmount() { unmounted.splice(0).forEach((callback) => callback()); },
  };
}

function createWindowShim() {
  const listeners = new Map();
  return {
    confirm: () => true,
    addEventListener(name, callback) { listeners.set(name, callback); },
    removeEventListener(name, callback) {
      if (listeners.get(name) === callback) listeners.delete(name);
    },
  };
}

function createComponentHarness({ api, props = {} } = {}) {
  const Vue = createVueShim();
  const window = createWindowShim();
  const workbench = loadWorkbench({ api, Vue, window });
  let definition = null;
  workbench.install({ component(name, value) { definition = { name, value }; } });
  const events = [];
  const exposed = {};
  const resolvedProps = {
    workspaceId: 7,
    workItemId: 10,
    mode: 'annotation',
    locale: 'en-US',
    ...props,
  };
  const instance = definition.value.setup(resolvedProps, {
    emit(name, payload) { events.push({ name, payload }); },
    expose(value) { Object.assign(exposed, value); },
  });
  return {
    Vue,
    window,
    props: resolvedProps,
    events,
    exposed,
    instance,
    unmount() { Vue.unmount(); },
  };
}

async function settle() {
  for (let index = 0; index < 8; index += 1) {
    await Promise.resolve();
    await new Promise((resolve) => setImmediate(resolve));
  }
}

const DRAFT_SCHEMA = 'quicstudio.package-annotation-draft.v1';

function packageSummary({
  itemId = 10,
  workspaceId = 7,
  status = 'in_progress',
  returnReason = '',
  episodes = [1],
  draftEntries = null,
  draftVersion = 1,
  generation = 2,
  capabilities = {},
} = {}) {
  const entries = draftEntries || Object.fromEntries(episodes.map((id) => [String(id), {
    conclusion: 'segments',
    segments: [{ id: `segment-${id}`, start_ns: '1000000000', end_ns: '1500000000', description: 'pick up' }],
  }]));
  return {
    item: { id: itemId, workspace_id: workspaceId, status, return_reason: returnReason },
    package: { id: 21, package_uid: `package-${itemId}` },
    episodes: episodes.map((id, index) => ({ id, episode_uid: `episode-${id}`, index })),
    draft_json: { schema: DRAFT_SCHEMA, episodes: entries },
    draft_version: draftVersion,
    generation,
    capabilities: {
      edit: true,
      save: true,
      submit: true,
      approve: false,
      return: false,
      read_only: false,
      ...capabilities,
    },
  };
}

function episodePayload({ itemId = 10, episodeId = 1, workspaceId = 7, mapping = true, entry = null } = {}) {
  const playbackTimeline = mapping ? {
    kind: 'qrdf_preview_timeline',
    topic: '/camera/head/rgb',
    entries: [
      { kind: 'frame', frame_index: 0, timestamp_ns: '1000000000', video_pts_us: '0' },
      { kind: 'frame', frame_index: 1, timestamp_ns: '1500000000', video_pts_us: '500000' },
      { kind: 'frame', frame_index: 2, timestamp_ns: '2000000000', video_pts_us: '1000000' },
    ],
  } : null;
  return {
    item: { id: itemId, workspace_id: workspaceId },
    episode: { id: episodeId, episode_uid: `episode-${episodeId}` },
    source_binding: { start_ns: '1000000000', end_ns: '2000000000' },
    timeline: { start_ns: '1000000000', end_ns: '2000000000', duration_s: 1 },
    media: {
      preview: {
        available: true,
        url: `https://media.example/${episodeId}.mp4`,
        mapping_available: mapping,
        playback_timeline: playbackTimeline,
        disabled_reason: mapping ? '' : 'annotation_preview_mapping_unavailable',
      },
    },
    entry,
    draft_version: 1,
    generation: 2,
    capabilities: { edit: mapping, read_only: !mapping, mapping_available: mapping },
  };
}

function apiError(status, code) {
  const error = new Error(code);
  error.status = status;
  error.code = code;
  return error;
}

function json(value) {
  return JSON.parse(JSON.stringify(value));
}

test('preview timeline maps relative playback to exact frame ns without source-duration interpolation', () => {
  const workbench = loadWorkbench();
  const timeline = workbench.normalizePlaybackTimeline({
    kind: 'qrdf_preview_timeline',
    topic: '/camera/head/rgb',
    entries: [
      { kind: 'frame', frame_index: 0, timestamp_ns: '9007199254740993000', video_pts_us: '1000000' },
      { kind: 'frame', frame_index: 1, timestamp_ns: '9007199254740993123', video_pts_us: '1250000' },
      { kind: 'frame', frame_index: 2, timestamp_ns: '9007199254740993999', video_pts_us: '2000000' },
    ],
  });
  assert.ok(timeline);
  assert.equal(workbench.sourceTimestampAtPlaybackSeconds(0.24, timeline), '9007199254740993123');
  assert.equal(workbench.sourceTimestampAtPlaybackRatio(.5, timeline), '9007199254740993123');
  assert.equal(workbench.playbackSecondsAtSourceTimestamp('9007199254740993999', timeline), 1);
  assert.equal(workbench.sourceTimestampAtPlaybackSeconds(0.5, { entries: [] }), null);
});

test('draft normalization preserves partial entries and completion requires every member', () => {
  const workbench = loadWorkbench();
  const draft = workbench.normalizeDraftJson({
    schema: 'quicstudio.package-annotation-draft.v1',
    episodes: {
      '10': { conclusion: 'segments', segments: [{ id: 'local-1', start_ns: '100', end_ns: '200', description: '' }] },
      '11': { conclusion: 'no_valid_segments', reason: '' },
    },
  }, new Set(['10', '11', '12']));
  assert.equal(draft.episodes['10'].segments[0].start_ns, '100');
  assert.equal(draft.episodes['11'].reason, '');
  const incomplete = workbench.validateDraftForSubmit(draft, ['10', '11', '12']);
  assert.equal(incomplete.valid, false);
  assert.deepEqual(Array.from(incomplete.missing), ['12']);
  const complete = workbench.validateDraftForSubmit({
    schema: workbench.DRAFT_SCHEMA,
    episodes: {
      '10': { conclusion: 'segments', segments: [{ id: 'local-1', start_ns: '100', end_ns: '200', description: 'pick up' }] },
      '11': { conclusion: 'no_valid_segments', reason: 'occluded' },
      '12': { conclusion: 'segments', segments: [{ id: 'local-2', start_ns: '300', end_ns: '400', description: 'place down' }] },
    },
  }, ['10', '11', '12']);
  assert.equal(complete.valid, true);
});

test('non-contiguous segments are valid while overlap, empty descriptions and bad bounds are rejected', () => {
  const workbench = loadWorkbench();
  const bounds = { start_ns: '1000', end_ns: '10000' };
  assert.equal(workbench.validateEntry({ conclusion: 'segments', segments: [
    { id: 'a', start_ns: '1000', end_ns: '2000', description: '' },
    { id: 'b', start_ns: '4000', end_ns: '5000', description: 'action' },
  ] }, bounds).valid, true);
  assert.equal(workbench.validateEntry({ conclusion: 'segments', segments: [
    { id: 'a', start_ns: '1000', end_ns: '5000', description: 'one' },
    { id: 'b', start_ns: '4000', end_ns: '6000', description: 'two' },
  ] }, bounds).code, 'overlap');
  assert.equal(workbench.validateEntry({ conclusion: 'segments', segments: [
    { id: 'a', start_ns: '999', end_ns: '2000', description: 'bad' },
  ] }, bounds).code, 'bounds');
  assert.equal(workbench.validateEntry({ conclusion: 'segments', segments: [
    { id: 'a', start_ns: '1000', end_ns: '2000', description: '' },
  ] }, bounds, { complete: true }).code, 'description');
});

test('save, submit, and review payloads carry the required version gates', () => {
  const workbench = loadWorkbench();
  assert.deepEqual(json(workbench.buildSavePayload({
    workspaceId: '7',
    baseVersion: 3,
    expectedGeneration: 4,
    draft: { episodes: { '10': { conclusion: 'no_valid_segments', reason: 'no usable view', segments: [] } } },
  })), {
    workspace_id: 7,
    base_version: 3,
    expected_generation: 4,
    draft_json: {
      schema: workbench.DRAFT_SCHEMA,
      episodes: { '10': { conclusion: 'no_valid_segments', reason: 'no usable view', segments: [] } },
    },
  });
  assert.deepEqual(json(workbench.buildSubmitPayload({ workspaceId: 7, baseVersion: 3, expectedGeneration: 4 })), {
    workspace_id: 7, base_version: 3, expected_generation: 4,
  });
  assert.deepEqual(json(workbench.buildReviewDecisionPayload({ workspaceId: 7, submissionId: 91, generation: 4 })), { workspace_id: 7, expected_submission_id: 91, expected_generation: 4 });
  assert.deepEqual(json(workbench.buildReviewDecisionPayload({ workspaceId: 7, submissionId: 91, generation: 4, reason: 'needs clarification' })), {
    workspace_id: 7, expected_submission_id: 91, expected_generation: 4, reason: 'needs clarification',
  });
});

test('workbench loader discards responses from a previous item scope', async () => {
  const workbench = loadWorkbench();
  const pending = [];
  const api = {
    getPackageAnnotationWorkbench(id) {
      return new Promise((resolve) => pending.push({ id, resolve }));
    },
  };
  const loader = workbench.createWorkbenchLoader(api);
  const first = loader.load(10, 7, 'annotation');
  const second = loader.load(11, 7, 'annotation');
  await Promise.resolve();
  pending.find((item) => item.id === 10).resolve({ item: { id: 10, workspace_id: 7 }, episodes: [], draft_json: {} });
  assert.equal((await first).stale, true);
  pending.find((item) => item.id === 11).resolve({ item: { id: 11, workspace_id: 7 }, episodes: [], draft_json: {} });
  const current = await second;
  assert.equal(current.stale, false);
  assert.equal(current.summary.item.id, 11);
});

test('episode loader rejects an exact stale Episode response', async () => {
  const workbench = loadWorkbench();
  let resolve;
  const api = {
    getPackageAnnotationEpisode() { return new Promise((done) => { resolve = done; }); },
  };
  const loader = workbench.createEpisodeLoader(api);
  const request = loader.load(10, 4, 7, 'annotation');
  await Promise.resolve();
  resolve({ item: { id: 10, workspace_id: 7 }, episode: { id: 5 }, timeline: { start_ns: '0', end_ns: '100' }, media: { preview: {} } });
  await assert.rejects(request, (error) => error.code === 'stale_episode_response' && error.stale === true);
  assert.equal(workbench.scopeMatches({ item: { id: 11, workspace_id: 7 } }, { itemId: 10, workspaceId: 7 }), false);
});

test('autosave remains single-flight and drains edits made while saving', async () => {
  const workbench = loadWorkbench();
  const requests = [];
  const pending = [];
  const coordinator = workbench.createAutosaveCoordinator({
    capture: (revision) => ({ revision }),
    save: (snapshot) => { requests.push(snapshot.revision); return new Promise((resolve) => pending.push(resolve)); },
    debounceMs: 999999,
    maxWaitMs: 999999,
  });
  coordinator.markDirty();
  const flushing = coordinator.flush({ drain: true, reason: 'test' });
  await Promise.resolve();
  assert.deepEqual(Array.from(requests), [1]);
  coordinator.markDirty();
  pending.shift()({ draft_version: 1 });
  await new Promise((done) => setImmediate(done));
  assert.deepEqual(Array.from(requests), [1, 2]);
  pending.shift()({ draft_version: 2 });
  assert.equal(await flushing, true);
  assert.equal(coordinator.state().dirty, false);
});

test('install registers the standalone component contract', () => {
  const workbench = loadWorkbench();
  let registered = null;
  workbench.install({ component(name, definition) { registered = { name, definition }; } });
  assert.equal(registered.name, 'package-annotation-workbench');
  assert.equal(registered.definition.props.workspaceId.type.length, 2);
  assert.equal(registered.definition.props.workItemId.type.length, 2);
  assert.deepEqual(Array.from(registered.definition.emits), ['back', 'completed']);
});

test('component setup loads lexical API data, maps video refs, autosaves, and submits the saved version', async () => {
  const calls = [];
  let packageLoads = 0;
  const api = {
    getPackageAnnotationWorkbench() {
      packageLoads += 1;
      return packageSummary({
        draftVersion: packageLoads === 1 ? 1 : 2,
        capabilities: packageLoads === 1
          ? {}
          : { edit: false, save: false, submit: false, read_only: true },
      });
    },
    getPackageAnnotationEpisode(itemId, episodeId, workspaceId) {
      calls.push({ method: 'episode', itemId, episodeId, workspaceId });
      return episodePayload({ itemId, episodeId, workspaceId });
    },
    saveAnnotationWorkItem(itemId, body) {
      calls.push({ method: 'save', itemId, body });
      return { id: itemId, draft_version: 2, generation: 2 };
    },
    submitAnnotationWorkItem(itemId, body) {
      calls.push({ method: 'submit', itemId, body });
      return { id: itemId, status: 'submitted', draft_version: 2, generation: 2 };
    },
  };
  const harness = createComponentHarness({ api });
  await settle();

  assert.equal(packageLoads, 1);
  assert.equal(harness.instance.state.packageSummary.item.id, 10);
  const video = { currentTime: 0 };
  harness.instance.setVideoElement(video);
  harness.instance.handleTimelineClick({
    clientX: 50,
    currentTarget: { getBoundingClientRect: () => ({ left: 0, width: 100 }) },
  });
  assert.equal(harness.instance.state.pendingTimestampNs, '1500000000');
  assert.equal(video.currentTime, 0.5);

  harness.instance.updateSegment('segment-1', 'description', 'updated description');
  harness.instance.openSubmitConfirmation();
  assert.equal(harness.instance.state.finalAction, 'submit');
  await harness.instance.confirmFinalAction();

  const save = calls.find((call) => call.method === 'save');
  assert.deepEqual(json(save.body), {
    workspace_id: 7,
    base_version: 1,
    expected_generation: 2,
    draft_json: {
      schema: DRAFT_SCHEMA,
      episodes: {
        '1': {
          conclusion: 'segments',
          segments: [{ id: 'segment-1', start_ns: '1000000000', end_ns: '1500000000', description: 'updated description' }],
        },
      },
    },
  });
  const submit = calls.find((call) => call.method === 'submit');
  assert.deepEqual(json(submit.body), {
    workspace_id: 7,
    base_version: 2,
    expected_generation: 2,
  });
  assert.equal(packageLoads, 2);
  assert.equal(harness.instance.state.capabilities.read_only, true);
  assert.equal(harness.instance.canEditCurrent.value, false);
  assert.equal(harness.instance.canSubmit.value, false);
  assert.equal(harness.events.filter((event) => event.name === 'completed').length, 1);
  harness.unmount();
});

test('empty segment drafts are pending until a described segment or reason is complete', async () => {
  const api = {
    getPackageAnnotationWorkbench: () => packageSummary({
      draftEntries: { '1': { conclusion: 'segments', segments: [] } },
    }),
    getPackageAnnotationEpisode: () => episodePayload(),
  };
  const harness = createComponentHarness({ api });
  await settle();

  assert.equal(harness.instance.packageProgress.value.processed, 0);
  assert.equal(harness.instance.packageProgress.value.pending, 1);
  assert.equal(harness.instance.statusLabel(harness.instance.state.episodes[0]), 'Needs segments');
  assert.equal(harness.instance.statusClass(harness.instance.state.episodes[0]), 'pending');
  assert.equal(harness.instance.draftComplete.value.valid, false);
  assert.match(harness.instance.finalDisabledReason.value, /Episode entries are incomplete/);
  harness.unmount();
});

test('new segment boundary flow exposes the current phase and selected in point', async () => {
  const api = {
    getPackageAnnotationWorkbench: () => packageSummary({
      draftEntries: { '1': { conclusion: 'segments', segments: [] } },
    }),
    getPackageAnnotationEpisode: () => episodePayload(),
  };
  const harness = createComponentHarness({ api });
  await settle();
  const video = { currentTime: 0 };
  harness.instance.setVideoElement(video);

  harness.instance.newSegment();
  assert.equal(harness.instance.state.boundaryMode, 'new');
  assert.match(harness.instance.boundaryPrompt(), /click the timeline to choose an in point/i);
  harness.instance.handleTimelineClick({
    clientX: 25,
    currentTarget: { getBoundingClientRect: () => ({ left: 0, width: 100 }) },
  });
  harness.instance.setInPoint();
  assert.equal(harness.instance.state.pendingStartNs, '1000000000');
  assert.match(harness.instance.boundaryPrompt(), /In point selected/i);
  harness.instance.handleTimelineClick({
    clientX: 75,
    currentTarget: { getBoundingClientRect: () => ({ left: 0, width: 100 }) },
  });
  harness.instance.setOutPoint();
  assert.equal(harness.instance.state.draftJson.episodes['1'].segments.length, 1);
  assert.equal(harness.instance.state.boundaryMode, 'idle');
  assert.equal(harness.instance.state.pendingStartNs, '');
  harness.unmount();
});

test('returned reason is exposed and submitted annotation status replaces the submit action', async () => {
  const returned = createComponentHarness({
    api: {
      getPackageAnnotationWorkbench: () => packageSummary({ status: 'returned', returnReason: 'Please describe the handoff action.' }),
      getPackageAnnotationEpisode: () => episodePayload(),
    },
  });
  await settle();
  assert.equal(returned.instance.workItem.value.return_reason, 'Please describe the handoff action.');
  assert.equal(returned.instance.workItemStatusLabel(), 'Returned for changes');
  assert.equal(returned.instance.annotationFinalized.value, false);
  returned.unmount();

  const submitted = createComponentHarness({
    api: {
      getPackageAnnotationWorkbench: () => packageSummary({
        status: 'submitted',
        capabilities: { edit: false, save: false, submit: false, read_only: true },
      }),
      getPackageAnnotationEpisode: () => episodePayload(),
    },
  });
  await settle();
  assert.equal(submitted.instance.annotationFinalized.value, true);
  assert.match(submitted.instance.finalDisabledReason.value, /submitted/i);
  submitted.unmount();
});

test('mapping-unavailable Episodes still allow no-valid conclusions while blocking boundary edits', async () => {
  const api = {
    getPackageAnnotationWorkbench: () => packageSummary({
      draftEntries: { '1': { conclusion: 'segments', segments: [] } },
    }),
    getPackageAnnotationEpisode: () => episodePayload({ mapping: false }),
    saveAnnotationWorkItem: () => ({ id: 10, draft_version: 2, generation: 2 }),
  };
  const harness = createComponentHarness({ api });
  await settle();

  assert.equal(harness.instance.canEditCurrent.value, true);
  assert.equal(harness.instance.canEditBounds.value, false);
  harness.instance.setConclusion('no_valid_segments');
  harness.instance.updateNoValidReason('camera occluded');
  assert.equal(harness.instance.state.draftJson.episodes['1'].conclusion, 'no_valid_segments');
  assert.equal(harness.instance.state.draftJson.episodes['1'].reason, 'camera occluded');
  harness.instance.setConclusion('segments');
  assert.equal(harness.instance.state.draftJson.episodes['1'].conclusion, 'no_valid_segments');
  harness.instance.newSegment();
  assert.equal(harness.instance.state.draftJson.episodes['1'].segments.length, 0);
  harness.unmount();
});

test('review terminal states use the package API approved and returned statuses', async () => {
  for (const status of ['approved', 'returned']) {
    const harness = createComponentHarness({
      props: { mode: 'review' },
      api: {
        getPackageAnnotationWorkbench: () => packageSummary({ status, capabilities: { read_only: true } }),
        getPackageAnnotationEpisode: () => episodePayload(),
      },
    });
    await settle();
    assert.equal(harness.instance.reviewFinalized.value, true);
    assert.notEqual(harness.instance.workItemStatusLabel(), 'Pending');
    assert.match(harness.instance.finalDisabledReason.value, new RegExp(status));
    harness.unmount();
  }
});

test('a save 409 preserves edits and protects navigation until the user confirms abandoning them', async () => {
  let saveCalls = 0;
  const api = {
    getPackageAnnotationWorkbench: () => packageSummary(),
    getPackageAnnotationEpisode: () => episodePayload(),
    saveAnnotationWorkItem: () => {
      saveCalls += 1;
      return Promise.reject(apiError(409, 'annotation_draft_version_conflict'));
    },
  };
  const harness = createComponentHarness({ api });
  await settle();
  harness.instance.updateSegment('segment-1', 'description', 'local edit survives conflict');
  await harness.instance.requestBack();

  assert.equal(saveCalls, 1);
  assert.equal(harness.instance.state.conflict, true);
  assert.equal(harness.instance.state.draftJson.episodes['1'].segments[0].description, 'local edit survives conflict');
  assert.equal(harness.events.some((event) => event.name === 'back'), false);

  harness.window.confirm = () => false;
  await harness.instance.requestBack();
  assert.equal(harness.events.some((event) => event.name === 'back'), false);
  harness.window.confirm = () => true;
  await harness.instance.requestBack();
  assert.equal(harness.events.filter((event) => event.name === 'back').length, 1);
  harness.unmount();
});

test('a late package response from a previous scope cannot overwrite the current setup state', async () => {
  const pending = [];
  const api = {
    getPackageAnnotationWorkbench(itemId, workspaceId) {
      return new Promise((resolve) => pending.push({ itemId, workspaceId, resolve }));
    },
  };
  const harness = createComponentHarness({ api });
  await settle();
  assert.deepEqual(pending.map((request) => request.itemId), [10]);

  harness.props.workItemId = 11;
  await harness.Vue.flushWatchers();
  await settle();
  assert.deepEqual(pending.map((request) => request.itemId), [10, 11]);

  pending[1].resolve(packageSummary({ itemId: 11, episodes: [] }));
  await settle();
  assert.equal(harness.instance.state.packageSummary.item.id, 11);
  pending[0].resolve(packageSummary({ itemId: 10, episodes: [] }));
  await settle();
  assert.equal(harness.instance.state.packageSummary.item.id, 11);
  harness.unmount();
});

test('a late Episode response from a previous scope cannot overwrite the selected video state', async () => {
  const packagePending = [];
  const episodePending = [];
  const api = {
    getPackageAnnotationWorkbench(itemId, workspaceId) {
      return new Promise((resolve) => packagePending.push({ itemId, workspaceId, resolve }));
    },
    getPackageAnnotationEpisode(itemId, episodeId, workspaceId) {
      return new Promise((resolve) => episodePending.push({ itemId, episodeId, workspaceId, resolve }));
    },
  };
  const harness = createComponentHarness({ api });
  await settle();
  packagePending[0].resolve(packageSummary({ itemId: 10, episodes: [1] }));
  await settle();
  assert.deepEqual(episodePending.map((request) => request.itemId), [10]);

  harness.props.workItemId = 11;
  await harness.Vue.flushWatchers();
  await settle();
  packagePending[1].resolve(packageSummary({ itemId: 11, episodes: [2] }));
  await settle();
  assert.deepEqual(episodePending.map((request) => `${request.itemId}:${request.episodeId}`), ['10:1', '11:2']);

  episodePending[1].resolve(episodePayload({ itemId: 11, episodeId: 2 }));
  await settle();
  assert.equal(harness.instance.state.episodeData.episode.id, 2);
  episodePending[0].resolve(episodePayload({ itemId: 10, episodeId: 1 }));
  await settle();
  assert.equal(harness.instance.state.episodeData.episode.id, 2);
  harness.unmount();
});

test('large package rendering is bounded while completion, saving and navigation cover all entries', async () => {
  const ids = Array.from({ length: 1001 }, (_, index) => index + 1);
  const entries = Object.fromEntries(ids.map(id => [String(id), { conclusion: 'no_valid_segments', reason: 'No usable frames', segments: [] }]));
  entries['1001'] = { conclusion: 'no_valid_segments', reason: '', segments: [] };
  entries['1'] = { conclusion: 'segments', segments: Array.from({ length: 101 }, (_, index) => ({ id: `many-${index}`, start_ns: String(1000000000 + index * 1000000), end_ns: String(1000000000 + (index + 1) * 1000000), description: 'Movement' })) };
  let saved;
  const harness = createComponentHarness({ api: {
    getPackageAnnotationWorkbench: async () => packageSummary({ episodes: ids, draftEntries: entries }),
    getPackageAnnotationEpisode: async (_item, id) => episodePayload({ episodeId: id }),
    saveAnnotationWorkItem: async (_item, payload) => { saved = payload; return { draft_version: 2, generation: 2 }; },
  } });
  try {
    await settle();
    const view = harness.instance;
    assert.equal(view.episodePage.value.items.length, 40);
    assert.equal(view.segmentPage.value.items.length, 20);
    assert.equal(view.packageProgress.value.total, 1001);
    assert.equal(view.packageProgress.value.pending, 1);
    assert.equal(view.draftComplete.value.valid, false, 'offscreen missing conclusion must block submit');
    view.selectSegment('many-100');
    assert.equal(view.segmentPage.value.page, 6);
    assert.equal(view.segmentPage.value.items[0].id, 'many-100');
    await view.selectEpisode(1001);
    assert.equal(view.episodePage.value.page, 26);
    assert.equal(view.episodePage.value.items[0].id, 1001);
    assert.equal(view.segmentPage.value.page, 1);
    view.updateNoValidReason('No usable frames');
    assert.equal(view.draftComplete.value.valid, true);
    await view.requestBack();
    assert.equal(Object.keys(saved.draft_json.episodes).length, 1001);
    assert.equal(saved.draft_json.episodes['1'].segments.length, 101);
  } finally { harness.unmount(); }
});

test('selected segment can update its out point without starting a new segment', async () => {
  const harness = createComponentHarness({ api: {
    getPackageAnnotationWorkbench: async () => packageSummary(),
    getPackageAnnotationEpisode: async () => episodePayload(),
  } });
  try {
    await settle();
    const view = harness.instance;
    view.selectSegment('segment-1');
    view.state.pendingTimestampNs = '2000000000';
    assert.equal(view.state.pendingStartNs, '');
    assert.equal(view.canSetOutPoint.value, true);
    view.setOutPoint();
    assert.equal(view.currentSegments.value.length, 1);
    assert.equal(view.currentSegments.value[0].end_ns, '2000000000');
    view.newSegment();
    view.state.pendingTimestampNs = '2000000000';
    assert.equal(view.canSetOutPoint.value, false, 'new segment still needs an in point');
  } finally { harness.unmount(); }
});

test('subsecond segment labels retain milliseconds and exact nanosecond tooltips', async () => {
  const harness = createComponentHarness({ api: {
    getPackageAnnotationWorkbench: async () => packageSummary(),
    getPackageAnnotationEpisode: async () => episodePayload(),
  } });
  try {
    await settle();
    const segment = { start_ns: '1001000000', end_ns: '1033000000' };
    assert.equal(harness.instance.segmentRangeLabel(segment), '0:00.001 → 0:00.033');
    assert.match(harness.instance.exactRangeTitle(segment), /1001000000 → 1033000000 ns/);
  } finally { harness.unmount(); }
});

test('historical submission missing is not presented as a refreshable version conflict', async () => {
  // FastAPI string detail becomes message/detail, with no error.code.
  const error = Object.assign(new Error('annotation_historical_result_uncertain'), {
    status: 409, detail: 'annotation_historical_result_uncertain',
  });
  const harness = createComponentHarness({
    api: { getPackageAnnotationWorkbench: () => Promise.reject(error) },
  });
  await settle();
  assert.equal(harness.instance.state.historicalResultMissing, true);
  assert.match(harness.instance.state.loadError, /重新加载无法恢复/);
  assert.equal(harness.instance.saveLabel(), '');
  assert.equal(harness.instance.workItemStatusLabel(), '');
  await harness.instance.reload();
  assert.equal(harness.instance.state.historicalResultMissing, true);
  await harness.instance.requestBack();
  assert.equal(harness.events.filter(event => event.name === 'back').length, 1);
  harness.unmount();
});

test('transient workbench failures retain retry and clear after successful reload', async () => {
  let failing = true;
  const harness = createComponentHarness({
    api: {
      getPackageAnnotationWorkbench: () => failing
        ? Promise.reject(apiError(503, 'temporarily_unavailable')) : packageSummary(),
      getPackageAnnotationEpisode: () => episodePayload(),
    },
  });
  await settle();
  assert.equal(harness.instance.state.historicalResultMissing, false);
  assert.equal(harness.instance.saveLabel(), '');
  failing = false;
  await harness.instance.reload();
  assert.equal(harness.instance.state.loadError, '');
  assert.notEqual(harness.instance.saveLabel(), '');
  harness.unmount();
});
