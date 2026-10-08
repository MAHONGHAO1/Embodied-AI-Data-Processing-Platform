import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../js/data-catalog.js', import.meta.url), 'utf8');
const stylesheet = readFileSync(new URL('../css/data-catalog.css', import.meta.url), 'utf8');

function loadCatalog(extra = {}) {
  const sandbox = { console, setTimeout, clearTimeout, Date, Intl, Promise, ...extra };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(`${source}\n;globalThis.__catalog = QuicDataCatalog;`, sandbox, {
    filename: 'data-catalog.js',
  });
  return { catalog: sandbox.__catalog, sandbox };
}

function json(value) { return JSON.parse(JSON.stringify(value)); }

test('permission gates follow the explicit matrix and current admin write gate', () => {
  const { catalog } = loadCatalog();
  assert.deepEqual(json(catalog.permissionSnapshot({ role: 'annotator', permissions: ['dataset:read', 'dataset:write', 'export:create', 'train:write'] })), {
    read: true, write: false, export: false, train: false,
  });
  assert.deepEqual(json(catalog.permissionSnapshot({ role: 'admin', permissions: ['*'] })), {
    read: true, write: true, export: true, train: true,
  });
  assert.equal(catalog.hasPermission({ permissions: ['dataset:*'] }, 'dataset:read'), true);
  assert.equal(catalog.hasPermission({ permissions: ['dataset:read'] }, 'export:create'), false);
});

test('asset details preserve exact snapshot intervals and never invent a duration', () => {
  const { catalog } = loadCatalog();
  const asset = catalog.normalizeAsset({
    id: 31,
    data_batch_id: 4,
    workspace_id: 8,
    governed_valid_duration_hours: null,
    episode_ids_json: [100, 101],
    source_snapshot_json: {
      episodes: [
        {
          episode_id: 100,
          annotation_submission_id: 77,
          annotation_conclusion: 'segments',
          effective_segments: [
            { id: 'a', start_ns: '9007199254740993000', end_ns: '9007199254740993123', description: '抓取' },
            { id: 'b', start_ns: '9007199254740994000', end_ns: '9007199254740994500', description: '放置' },
          ],
        },
        { episode_id: 101, annotation_conclusion: 'no_valid_segments', effective_segments: [] },
      ],
    },
  });
  const rows = catalog.assetEpisodeRows(asset);
  assert.equal(rows[0].segments[0].start_ns, '9007199254740993000');
  assert.equal(rows[0].segments[1].description, '放置');
  assert.equal(rows[1].conclusion, 'no_valid_segments');
  assert.equal(catalog.assetEffectiveDurationSeconds(asset), 0.000000623);
  assert.equal(catalog.formatDuration(0.000000623), '623 ns');
  assert.equal(catalog.assetEffectiveDurationSeconds({ episode_ids_json: [1] }), null);
});

test('catalog export and training payloads require real stable artifacts', () => {
  const { catalog } = loadCatalog();
  const first = catalog.buildExportBody('9', 'lerobot_3_0');
  const second = catalog.buildExportBody('9', 'lerobot_3_0');
  assert.equal(first.format, 'lerobot_3_0');
  assert.equal(second.format, 'lerobot_3_0');
  assert.match(first.idempotency_key, /^catalog:9:lerobot_3_0:/);
  assert.match(second.idempotency_key, /^catalog:9:lerobot_3_0:/);
  assert.notEqual(first.idempotency_key, second.idempotency_key, 'each explicit export action needs a new idempotency key');
  assert.deepEqual(json(catalog.buildExportBody('9', 'lerobot_3_0', first.idempotency_key)), json(first), 'a retry can reuse the action key');
  assert.equal(catalog.buildExportBody(9, 'unsupported'), null);
  assert.deepEqual(json(catalog.buildDeliveryRequest(12)), {
    url_ttl_seconds: 3600, oss_network: 'internal',
  });
  assert.equal(catalog.stableOssUri({ oss_uri: 'https://download.example/file' }), '');
  const exportRow = { id: 12, format: 'lerobot_3_0', status: 'succeeded', oss_uri: 'oss://bucket/catalog/12' };
  assert.equal(catalog.canRegisterExport(exportRow, { role: 'admin', permissions: ['*'] }), true);
  assert.equal(catalog.canRegisterExport({ ...exportRow, status: 'running' }, { role: 'admin', permissions: ['*'] }), false);
  assert.equal(catalog.canRegisterExport(exportRow, { role: 'annotator', permissions: ['train:write'] }), false);
});

test('catalog cards contain long export URIs within narrow layouts', () => {
  assert.match(stylesheet, /\.data-catalog-export-card\s*\{[^}]*min-width:\s*0;/);
  assert.match(stylesheet, /\.data-catalog-export-card code\s*\{[^}]*overflow-wrap:\s*anywhere;/);
  assert.match(stylesheet, /\.data-catalog-export-card code\s*\{[^}]*word-break:\s*break-word;/);
});

test('catalog removes the descriptive hero and keeps refresh in each toolbar', () => {
  assert.doesNotMatch(source, /class="data-catalog-header"/);
  assert.doesNotMatch(source, /固定来源、批次与标注快照/);
  assert.doesNotMatch(source, /不可变版本、真实导出与训练登记/);
  assert.doesNotMatch(source, /重新读取/);
  assert.match(source, /mode === 'assets'[\s\S]*?data-catalog-toolbar-refresh[\s\S]*?@click="refreshCurrent"/);
  assert.match(source, /openDatasetDialog[\s\S]*?data-catalog-toolbar-refresh[\s\S]*?@click="refreshCurrent"/);
  assert.match(stylesheet, /\.data-catalog-toolbar-refresh\s*\{[^}]*margin-left:\s*auto;/);
});

test('paged loaders send bounded server pagination and discard an older scope response', async () => {
  const { catalog } = loadCatalog();
  const pending = [];
  const calls = [];
  const api = {
    listDataAssets(params) {
      calls.push(params);
      return new Promise((resolve) => pending.push({ params, resolve }));
    },
    listCatalogDatasets(params) {
      calls.push({ datasets: params });
      return Promise.resolve({ items: [{ id: 2, name: 'real' }], total: 1, limit: 20, offset: 0 });
    },
    listCatalogVersions(id, params) {
      calls.push({ versions: id, params });
      return Promise.resolve({ items: [{ id: 3, dataset_id: id, version: 1, data_asset_ids: [31] }], total: 1, limit: 20, offset: 0 });
    },
  };
  const loader = catalog.createCatalogLoaders(api);
  const first = loader.loadAssets({ sourceWorkspaceId: 8, page: 2, limit: 20 });
  const second = loader.loadAssets({ sourceWorkspaceId: 9, page: 1, limit: 20 });
  await Promise.resolve();
  assert.deepEqual(json(calls.slice(0, 2)), [
    { source_workspace_id: 8, limit: 20, offset: 20 },
    { source_workspace_id: 9, limit: 20, offset: 0 },
  ]);
  pending[0].resolve({ items: [{ id: 1 }] });
  assert.equal((await first).stale, true);
  pending[1].resolve({ items: [{ id: 2 }], total: 41, limit: 20, offset: 0 });
  assert.deepEqual(json(await second), { stale: false, items: [{ id: 2, data_batch_id: null, workspace_id: null, governed_valid_duration_hours: null, stage_snapshot_json: {}, episode_ids_json: [], source_json: {}, source_snapshot_json: {}, source_snapshot_id: '' }], total: 41, limit: 20, offset: 0, serverPaged: true });
  const datasets = await loader.loadDatasets();
  assert.equal(datasets.serverPaged, true);
  const versions = await loader.loadVersions(2);
  assert.equal(versions.items[0].id, 3);
  assert.deepEqual(json(calls.at(-2)), { datasets: { limit: 20, offset: 0 } });
  assert.deepEqual(json(calls.at(-1)), { versions: 2, params: { limit: 20, offset: 0 } });
});

test('403 loader errors are surfaced and never converted into an empty success list', async () => {
  const { catalog } = loadCatalog();
  const error = Object.assign(new Error('forbidden'), { status: 403 });
  const loader = catalog.createCatalogLoaders({ listDataAssets: async () => { throw error; } });
  await assert.rejects(loader.loadAssets(), (received) => received === error);
  assert.match(catalog.errorMessage(error), /没有数据集权限/);
});

test('durable version refresh uses a real detail endpoint when the API exposes it', async () => {
  const { catalog } = loadCatalog();
  const calls = [];
  const loader = catalog.createCatalogLoaders({
    getCatalogVersion(id) {
      calls.push(id);
      return Promise.resolve({ version: { id, dataset_id: 2, version: 3, data_asset_ids: [31], exports: [{ id: 55, version_id: id, format: 'lerobot_3_0', status: 'running' }] } });
    },
  });
  const result = await loader.loadVersionDetail(9);
  assert.equal(result.detail.exports.lerobot_3_0.status, 'running');
  assert.deepEqual(calls, [9]);
});

test('each successful export action creates a new attempt key while a duplicate click stays single-flight', async () => {
  const exportCalls = [];
  let resolveFirst;
  const api = {
    exportCatalogVersion(id, body) {
      exportCalls.push({ id, body });
      if (exportCalls.length === 1) return new Promise((resolve) => { resolveFirst = resolve; });
      return Promise.resolve({ export_id: 56, status: 'queued' });
    },
    getCatalogVersion(id) {
      return Promise.resolve({ id, dataset_id: 2, version: 1, data_asset_ids: [31], exports: [] });
    },
  };
  const fakeVue = {
    reactive: value => value,
    ref: value => ({ value }),
    computed: fn => ({ get value() { return fn(); } }),
    watch() {},
    onMounted() {},
    onBeforeUnmount() {},
  };
  const { catalog } = loadCatalog({ QuicDataAPI: api, Vue: fakeVue });
  let component;
  catalog.install({ component(name, value) { component = value; } });
  const view = component.setup({ mode: 'datasets', locale: 'en-US', user: { role: 'admin', permissions: ['*'] }, workspaces: [] }, { emit() {} });
  view.state.selectedDataset = { id: 2, source_kind: 'qrdf_assets' };
  view.state.selectedDatasetId = 2;
  view.state.selectedVersion = { id: 9, dataset_id: 2, version: 1, data_asset_ids: [31], exports: {} };
  view.state.selectedVersionId = 9;

  const first = view.exportVersion('lerobot_3_0');
  await Promise.resolve();
  assert.equal(view.state.actionLoading, true);
  const duplicate = view.exportVersion('lerobot_3_0');
  assert.equal(await duplicate, undefined, 'a second click while the first request is pending is ignored');
  assert.equal(exportCalls.length, 1);
  resolveFirst({ export_id: 55, status: 'queued' });
  await first;

  await view.exportVersion('lerobot_3_0');
  assert.equal(exportCalls.length, 2);
  assert.equal(exportCalls[0].id, 9);
  assert.equal(exportCalls[1].id, 9);
  assert.notEqual(exportCalls[0].body.idempotency_key, exportCalls[1].body.idempotency_key, 'a later explicit export gets a new attempt key');
  assert.equal(view.state.selectedVersion.id, 9, 'the immutable version selection is unchanged');
});

test('an unconfirmed export failure retries with the same version-format key', async () => {
  const exportCalls = [];
  let callCount = 0;
  const api = {
    exportCatalogVersion(id, body) {
      exportCalls.push({ id, body });
      callCount += 1;
      return callCount === 1
        ? Promise.reject(Object.assign(new Error('network timeout'), { status: 503 }))
        : Promise.resolve({ export_id: 57, status: 'queued' });
    },
  };
  const fakeVue = {
    reactive: value => value,
    ref: value => ({ value }),
    computed: fn => ({ get value() { return fn(); } }),
    watch() {},
    onMounted() {},
    onBeforeUnmount() {},
  };
  const { catalog } = loadCatalog({ QuicDataAPI: api, Vue: fakeVue });
  let component;
  catalog.install({ component(name, value) { component = value; } });
  const view = component.setup({ mode: 'datasets', locale: 'en-US', user: { role: 'admin', permissions: ['*'] }, workspaces: [] }, { emit() {} });
  view.state.selectedDataset = { id: 2, source_kind: 'qrdf_assets' };
  view.state.selectedDatasetId = 2;
  view.state.selectedVersion = { id: 9, dataset_id: 2, version: 1, data_asset_ids: [31], exports: {} };
  view.state.selectedVersionId = 9;

  await view.exportVersion('qrdf_0_2');
  assert.equal(view.state.actionLoading, false);
  assert.match(view.state.actionError, /network timeout/);
  await view.exportVersion('qrdf_0_2');
  assert.equal(exportCalls.length, 2);
  assert.equal(exportCalls[0].id, 9);
  assert.equal(exportCalls[1].id, 9);
  assert.equal(exportCalls[0].body.idempotency_key, exportCalls[1].body.idempotency_key);
});

test('component setup trial uses lexical QuicDataAPI and skips requests without read permission', async () => {
  let registered;
  let calls = 0;
  const api = {
    async listDataAssets(params) {
      calls += 1;
      assert.deepEqual(params, { limit: 20, offset: 0 });
      return { items: [] };
    },
  };
  const fakeVue = {
    reactive(value) { return value; },
    ref(value) { return { value }; },
    computed(fn) { return { get value() { return fn(); } }; },
    watch() {},
    onMounted() {},
    onBeforeUnmount() {},
  };
  const sandbox = { console, setTimeout, clearTimeout, Date, Intl, Promise, __api: api, Vue: fakeVue };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  // Evaluate with a lexical API binding, matching api.js's browser shape.
  vm.runInContext(`const QuicDataAPI = __api;${source}\n;QuicDataCatalog.install({ component(name, definition) { globalThis.__registered = definition; } });`, sandbox, { filename: 'data-catalog-component.js' });
  registered = sandbox.__registered;
  const user = { role: 'annotator', permissions: ['dataset:read'] };
  const view = registered.setup({ mode: 'assets', locale: 'en-US', user, workspaces: [] }, { emit() {} });
  await view.loadScope();
  assert.equal(calls, 1);
  const denied = registered.setup({ mode: 'assets', locale: 'zh-CN', user: { role: 'viewer', permissions: [] }, workspaces: [] }, { emit() {} });
  await denied.loadScope();
  assert.equal(calls, 1);
});

test('datasets component restores persisted export status after re-entry', async () => {
  const calls = [];
  let openCalls = 0;
  let deliveryRequest;
  const api = {
    async listCatalogDatasets() { calls.push('datasets'); return { items: [{ id: 2, name: 'catalog', source_kind: 'qrdf_assets' }] }; },
    async listCatalogVersions(id) { calls.push(`versions:${id}`); return { items: [{ id: 9, dataset_id: id, version: 1, status: 'active', data_asset_ids: [31] }] }; },
    async getCatalogVersion(id) {
      calls.push(`version:${id}`);
      return { id, dataset_id: 2, version: 1, status: 'active', data_asset_ids: [31], exports: [{ id: 55, version_id: id, format: 'lerobot_3_0', status: 'running', attempt: 2 }] };
    },
    async getCatalogExportDelivery(id, params) {
      deliveryRequest = { id, params };
      return { url: 'https://download.example/catalog.tar.gz' };
    },
  };
  const fakeVue = {
    reactive(value) { return value; },
    ref(value) { return { value }; },
    computed(fn) { return { get value() { return fn(); } }; },
    watch() {},
    onMounted() {},
    onBeforeUnmount() {},
  };
  const sandbox = { console, setTimeout, clearTimeout, Date, Intl, Promise, __api: api, Vue: fakeVue, window: { open() { openCalls += 1; } } };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(`const QuicDataAPI = __api;${source}\n;QuicDataCatalog.install({ component(name, definition) { globalThis.__registered = definition; } });`, sandbox, { filename: 'data-catalog-persisted-component.js' });
  const view = sandbox.__registered.setup({ mode: 'datasets', locale: 'zh-CN', user: { role: 'admin', permissions: ['*'] }, workspaces: [] }, { emit() {} });
  await view.loadScope();
  await view.selectDataset({ id: 2, name: 'catalog', source_kind: 'qrdf_assets' });
  assert.equal(view.state.selectedVersion.exports.lerobot_3_0.status, 'running');
  assert.deepEqual(calls, ['datasets', 'versions:2', 'version:9']);
  view.state.selectedVersion.exports.lerobot_3_0 = { id: 55, version_id: 9, format: 'lerobot_3_0', status: 'succeeded', oss_uri: 'oss://bucket/55' };
  await view.deliverExport('lerobot_3_0');
  assert.deepEqual(JSON.parse(JSON.stringify(deliveryRequest)), { id: 55, params: { url_ttl_seconds: 3600, oss_network: 'internal' } });
  assert.equal(view.state.deliveryUrl, 'https://download.example/catalog.tar.gz');
  assert.equal(openCalls, 0, 'internal/public delivery is opened by the explicit link only');
});

test('component registration keeps the standalone mode and event contract', () => {
  const { catalog } = loadCatalog();
  let registration;
  catalog.install({ component(name, definition) { registration = { name, definition }; } });
  assert.equal(registration.name, 'data-catalog');
  assert.equal(registration.definition.props.mode.default, 'assets');
  assert.deepEqual(Array.from(registration.definition.emits), ['view-episode', 'registered', 'dataset-created', 'version-created']);
});

test('delivery links follow the latest version and network selection, including late responses', async () => {
  const pending = [];
  const watchers = [];
  const api = { getCatalogExportDelivery() { return new Promise((resolve, reject) => pending.push({ resolve, reject })); } };
  const fakeVue = {
    reactive: value => value,
    ref: value => ({ value }),
    computed: fn => ({ get value() { return fn(); } }),
    watch(get, callback, options) { watchers.push({ get, callback, options }); },
    onMounted() {}, onBeforeUnmount() {},
  };
  const { catalog } = loadCatalog({ QuicDataAPI: api, Vue: fakeVue });
  let component;
  catalog.install({ component(name, value) { component = value; } });
  const view = component.setup({ mode: 'datasets', locale: 'en-US', user: { id: 5, role: 'admin', permissions: ['*'] }, workspaces: [] }, { emit() {} });
  view.state.selectedDatasetId = 2;
  view.state.selectedVersionId = 9;
  view.state.selectedVersion = { id: 9, exports: {
    qrdf_0_2: { id: 55, format: 'qrdf_0_2', status: 'succeeded' },
  } };
  const stale = view.deliverExport('qrdf_0_2');
  view.state.deliveryNetwork = 'public';
  const latest = view.deliverExport('qrdf_0_2');
  pending[1].resolve({ url: 'https://public.example/current' });
  await latest;
  pending[0].resolve({ url: 'https://internal.example/stale' });
  await stale;
  assert.equal(view.state.deliveryUrl, 'https://public.example/current');
  const invalidator = watchers.find(watcher => watcher.options?.flush === 'sync');
  assert.ok(invalidator, 'selection changes must immediately clear the previous link');
  view.state.deliveryTtlSeconds = 600;
  invalidator.callback();
  assert.equal(view.state.deliveryUrl, '');
  const oldVersion = view.deliverExport('qrdf_0_2');
  view.state.selectedVersionId = 10;
  pending[2].reject(new Error('old version failure'));
  await oldVersion;
  assert.equal(view.state.deliveryError, '');
  assert.equal(view.state.deliveryUrl, '');
});
