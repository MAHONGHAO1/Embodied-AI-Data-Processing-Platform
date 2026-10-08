import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const appSource = readFileSync(new URL('../js/app.js', import.meta.url), 'utf8');
const loaderStart = appSource.indexOf('async function loadSettingsCenterData()');
const loaderEnd = appSource.indexOf('\n      async function loadPlatformSettings', loaderStart);
assert.ok(loaderStart >= 0 && loaderEnd > loaderStart, 'settings loader must remain a named function');
const loaderSource = appSource.slice(loaderStart, loaderEnd);
const resourceLoaderStart = appSource.indexOf('async function loadCollectionResources()');
const resourceLoaderEnd = appSource.indexOf('\n      async function openCollectorDialog', resourceLoaderStart);
assert.ok(resourceLoaderStart >= 0 && resourceLoaderEnd > resourceLoaderStart, 'resource loader must remain a named function');
const resourceLoaderSource = appSource.slice(resourceLoaderStart, resourceLoaderEnd);
const collectorScopeStart = appSource.indexOf('function collectorDialogScopeMatches');
const collectorScopeEnd = appSource.indexOf('\n      async function handleRevokeCollector', collectorScopeStart);
assert.ok(collectorScopeStart >= 0 && collectorScopeEnd > collectorScopeStart, 'collector scope helpers must remain available');
const collectorScopeSource = appSource.slice(collectorScopeStart, collectorScopeEnd);
const projectScopeStart = appSource.indexOf('function resetCollectionProjectDialogState');
const projectScopeEnd = appSource.indexOf('\n      async function handleArchiveCollectionProject', projectScopeStart);
assert.ok(projectScopeStart >= 0 && projectScopeEnd > projectScopeStart, 'project dialog scope helpers must remain available');
const projectScopeSource = appSource.slice(projectScopeStart, projectScopeEnd);

test('settings loads expose rejected sections instead of turning them into empty data', () => {
  assert.match(loaderSource, /Promise\.allSettled/);
  assert.match(loaderSource, /settingsCenterLoadFailures\.value/);
  assert.match(loaderSource, /status !== 'fulfilled'/);
  assert.match(appSource, /settingsCenterError/);
  assert.match(appSource, /settingsLoadRetry/);
  assert.doesNotMatch(loaderSource, /\.catch\s*\(\s*\(\)\s*=>\s*\(\{\s*items:\s*\[\]\s*\}\)\s*\)/);
});

test('settings responses are invalidated by workspace and request generation', () => {
  assert.match(loaderSource, /settingsCenterGeneration/);
  assert.match(loaderSource, /isSameRequest/);
  assert.match(loaderSource, /workspaceId === \(Number\(selectedWorkspaceId\.value\) \|\| 0\)/);
  assert.match(appSource, /settingsCenterGeneration \+= 1/);
  assert.match(appSource, /Number\(settingsCenterLoadedWorkspaceId\.value\) === Number\(selectedWorkspaceId\.value\)/);
});

test('settings project module keeps project management separate from workspace creation', () => {
  const requiresWorkspace = appSource.match(/function viewRequiresWorkspace\(view\) \{([\s\S]*?)\n      \}/)?.[1] || '';
  assert.doesNotMatch(requiresWorkspace, /'settings'|'resources'/);
  assert.match(appSource, /membershipScopedViews = new Set\(\[\.\.\.COLLECTION_VIEWS, 'settings'\]\)/);
  const header = appSource.match(/<header class="console-header">([\s\S]*?)<\/header>/)?.[1] || '';
  assert.doesNotMatch(header, /'settings'/);
  const toolbar = appSource.match(/<div class="page-actions settings-center-actions collection-scope-actions">([\s\S]*?)<\/div>\s*<el-alert/)?.[1] || '';
  // Projects are created in the selected workspace, so the page picks it; workspace creation stays in 工作空间.
  assert.match(toolbar, /<label><span>\{\{ t\('workspace'\) \}\}<\/span><el-select v-model="selectedWorkspaceId"[^>]*@change="switchWorkspace"/);
  assert.doesNotMatch(toolbar, /settings-project-scope|selectedCollectionProjectId|settingsQuickCreate|handleSettingsCreateCommand|createWorkspace/);
  assert.match(toolbar, /loadSettingsCenterData[\s\S]*?v-if="settingsActiveTab === 'projects'" type="primary"[\s\S]*?openCreateCollectionProjectDialog/);
  assert.match(appSource, /activeView === 'admin'[\s\S]*?t\('createManagedUser'\)[\s\S]*?t\('createWorkspace'\)/);
  assert.match(appSource, /createCollectionProject: '新建采集项目'/);
  assert.match(appSource, /const settingsProjectRows = computed/);
  assert.match(appSource, /:data="settingsProjectRows"/);
});

test('resource responses are invalidated when the workspace changes', () => {
  assert.match(resourceLoaderSource, /collectionResourcesGeneration/);
  assert.match(resourceLoaderSource, /workspaceId = Number\(selectedWorkspaceId\.value\)/);
  assert.match(resourceLoaderSource, /generation === collectionResourcesGeneration/);
  assert.match(resourceLoaderSource, /if \(!isCurrent\(\)\) return;/);
  assert.match(appSource, /collectionResourcesGeneration \+= 1;/);
});

test('deferred resource responses cannot repopulate a changed or signed-out scope', async () => {
  const pending = [];
  const api = {
    listCollectorProfiles(workspaceId) {
      return new Promise((resolve) => pending.push({ kind: 'collector', workspaceId, resolve }));
    },
    listCollectionDevices(workspaceId) {
      return new Promise((resolve) => pending.push({ kind: 'device', workspaceId, resolve }));
    },
  };
  const context = {
    QuicDataAPI: api,
    selectedWorkspaceId: { value: 1 },
    collectorProfiles: { value: [{ id: 'initial' }] },
    collectionDevices: { value: [{ id: 'initial' }] },
    loading: { resources: false },
    errors: [],
    errorMessage(error) { this.errors.push(error); },
  };
  vm.createContext(context);
  vm.runInContext(
    `let collectionResourcesGeneration = 0;\n${resourceLoaderSource}\n` +
      'globalThis.runResourceLoad = loadCollectionResources;\n' +
      'globalThis.invalidateResourceLoads = () => { collectionResourcesGeneration += 1; };',
    context,
    { filename: 'app-resource-loader.js' },
  );

  const firstLoad = context.runResourceLoad();
  assert.deepEqual(pending.map((item) => [item.kind, item.workspaceId]), [['collector', 1], ['device', 1]]);
  context.selectedWorkspaceId.value = 2;
  context.invalidateResourceLoads();
  const secondLoad = context.runResourceLoad();
  const secondRequests = pending.filter((item) => item.workspaceId === 2);
  secondRequests.find((item) => item.kind === 'collector').resolve({ items: [{ id: 'workspace-2-collector' }] });
  secondRequests.find((item) => item.kind === 'device').resolve({ items: [{ id: 'workspace-2-device' }] });
  await secondLoad;
  assert.deepEqual(context.collectorProfiles.value, [{ id: 'workspace-2-collector' }]);
  assert.deepEqual(context.collectionDevices.value, [{ id: 'workspace-2-device' }]);

  pending.filter((item) => item.workspaceId === 1).forEach((item) => item.resolve({ items: [{ id: 'stale' }] }));
  await firstLoad;
  assert.deepEqual(context.collectorProfiles.value, [{ id: 'workspace-2-collector' }]);
  assert.deepEqual(context.collectionDevices.value, [{ id: 'workspace-2-device' }]);

  const signOutLoad = context.runResourceLoad();
  context.invalidateResourceLoads();
  context.selectedWorkspaceId.value = 0;
  context.collectorProfiles.value = [];
  context.collectionDevices.value = [];
  pending.filter((item) => item.workspaceId === 2).forEach((item) => item.resolve({ items: [{ id: 'signed-out-stale' }] }));
  await signOutLoad;
  assert.deepEqual(context.collectorProfiles.value, []);
  assert.deepEqual(context.collectionDevices.value, []);
  assert.deepEqual(context.errors, []);
});

test('settings writes require a successfully loaded current workspace', () => {
  assert.match(appSource, /function ensureSettingsCenterWritable\(\)/);
  for (const functionName of [
    'openEditCollectionProjectDialog',
    'saveCollectionProject',
    'handleArchiveCollectionProject',
    'handleCreateCollectionLabel',
    'handleDeactivateCollectionLabel',
  ]) {
    const start = appSource.indexOf(`function ${functionName}`);
    assert.ok(start >= 0, `${functionName} must remain available`);
    const nextFunction = appSource.indexOf('\n      function ', start + 10);
    const nextAsyncFunction = appSource.indexOf('\n      async function ', start + 10);
    const candidates = [nextFunction, nextAsyncFunction].filter((index) => index >= 0);
    const end = candidates.length ? Math.min(...candidates) : appSource.length;
    assert.match(appSource.slice(start, end), /ensureSettingsCenterWritable\(\)/, `${functionName} must guard writes`);
  }
  assert.match(appSource, /function openCreateCollectionProjectDialog\(\)[\s\S]*?ensureCollectionProjectCreateWritable\(\)/);
  assert.match(appSource, /:disabled="!settingsCenterWritable"/);
  assert.match(appSource, /settingsUnavailable/);
});

test('a stale project form cannot issue a write after the workspace changes', async () => {
  const calls = [];
  const context = {
    selectedWorkspaceId: { value: 1 },
    settingsCenterWritable: { value: true },
    showCollectionProjectDialog: { value: false },
    editingCollectionProject: { value: null },
    collectionProjectForm: { name: '', description: '' },
    savingCollectionProject: { value: false },
    QuicDataAPI: {
      updateCollectionProject(...args) { calls.push(['update', ...args]); return Promise.resolve({}); },
      createCollectionProject(...args) { calls.push(['create', ...args]); return Promise.resolve({}); },
    },
    canView() { return true; },
    activeView: { value: 'settings' },
    ElMessage: { warning() {}, success() {} },
    t(key) { return key; },
    errorMessage() {},
    loadSettingsCenterData: async () => {},
    loadCollectionProjectOptions: async () => {},
    collectionProjects: { value: [] },
  };
  vm.createContext(context);
  vm.runInContext(
    `let collectionProjectWriteGeneration = 0;\nconst ensureSettingsCenterWritable = () => settingsCenterWritable.value;\n${projectScopeSource}\n` +
      'globalThis.openProject = openEditCollectionProjectDialog; globalThis.saveProject = saveCollectionProject;',
    context,
    { filename: 'app-project-scope.js' },
  );
  context.openProject({ id: 91, name: 'workspace A project' });
  context.selectedWorkspaceId.value = 2;
  await context.saveProject();
  assert.deepEqual(calls, [], 'the old form must not send a workspace B mutation for workspace A');
  assert.equal(context.showCollectionProjectDialog.value, false);
});

test('late available-collector results cannot repopulate a changed dialog scope', async () => {
  const pending = [];
  const context = {
    selectedWorkspaceId: { value: 1 },
    showCollectorDialog: { value: false },
    collectorDialogWorkspaceId: 0,
    availableCollectors: { value: [] },
    selectedExistingCollectorId: { value: null },
    loadingAvailableCollectors: { value: false },
    collectorDialogMode: { value: 'existing' },
    collectorForm: { name: '', profile_key: '' },
    saving: { value: false },
    canManageWorkspace: { value: true },
    QuicDataAPI: {
      listAvailableCollectorProfiles(workspaceId) {
        return new Promise((resolve) => pending.push({ workspaceId, resolve }));
      },
      grantCollectorMembership() { throw new Error('stale dialog must not grant membership'); },
    },
    errorMessage() { throw new Error('stale request must not surface an error'); },
    ElMessage: { warning() {}, success() {} },
    t(key) { return key; },
    locale: { value: 'zh-CN' },
    collectorDisplayLabel(item) { return item.name || String(item.id); },
  };
  vm.createContext(context);
  vm.runInContext(
    `let collectorDialogWorkspaceId = 0;\nlet availableCollectorsGeneration = 0;\nlet collectorDialogWriteGeneration = 0;\n${collectorScopeSource}\n` +
      'globalThis.openCollector = openCollectorDialog; globalThis.closeCollector = closeCollectorDialog; globalThis.submitCollector = handleCollectorDialogSubmit;',
    context,
    { filename: 'app-collector-scope.js' },
  );
  const first = context.openCollector();
  assert.deepEqual(pending.map((item) => item.workspaceId), [1]);
  context.selectedWorkspaceId.value = 2;
  context.closeCollector();
  pending[0].resolve({ items: [{ id: 10, name: 'A only' }] });
  await first;
  assert.deepEqual(Array.from(context.availableCollectors.value), []);

  context.selectedWorkspaceId.value = 1;
  const second = context.openCollector();
  context.collectorDialogMode.value = 'existing';
  context.selectedExistingCollectorId.value = 10;
  context.selectedWorkspaceId.value = 2;
  await context.submitCollector();
  assert.equal(context.showCollectorDialog.value, false);
  assert.deepEqual(Array.from(context.availableCollectors.value), []);
  pending[1].resolve({ items: [{ id: 10, name: 'A only' }] });
  await second;
});
