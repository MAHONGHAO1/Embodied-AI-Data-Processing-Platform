import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const appSource = readFileSync(new URL('../js/app.js', import.meta.url), 'utf8');

const miningStart = appSource.indexOf('function miningDialogScopeMatches');
const miningEnd = appSource.indexOf('\n      async function splitMiningTask', miningStart);
assert.ok(miningStart >= 0 && miningEnd > miningStart, 'mining task scope functions must remain named');
const miningSource = appSource.slice(miningStart, miningEnd);

const buildStart = appSource.indexOf('function exitOverviewBuildMode');
const buildEnd = appSource.indexOf('\n      async function loadDataBatchCandidates', buildStart);
assert.ok(buildStart >= 0 && buildEnd > buildStart, 'overview build functions must remain named');
const buildSource = appSource.slice(buildStart, buildEnd);

const dashboardStart = appSource.indexOf('function dashboardScope');
const dashboardEnd = appSource.indexOf('\n      watch(activeView', dashboardStart);
assert.ok(dashboardStart >= 0 && dashboardEnd > dashboardStart, 'dashboard refresh functions must remain named');
const dashboardSource = appSource.slice(dashboardStart, dashboardEnd);

test('mining UI leaves absent package targets unknown and rejects an explicit zero task target', () => {
  assert.doesNotMatch(appSource, /target:\s*Number\(pkg\.target_duration_hours\)\s*\|\|\s*2/);
  assert.doesNotMatch(appSource, /target:\s*Number\(pkg\.target_duration_hours\)\s*\|\|\s*100/);
  assert.match(appSource, /Number\.isFinite\(packageTargetHours\)\s*&&\s*packageTargetHours\s*>\s*0/);
  assert.match(appSource, /if \(!Number\.isFinite\(targetHours\) \|\| targetHours < 0\.01\)/);
});

function ref(value) {
  return { value };
}

function flush() {
  return new Promise((resolve) => setImmediate(resolve));
}

test('a project lookup that started in workspace A cannot create a task in workspace B', async () => {
  const pending = [];
  const calls = [];
  const context = {
    selectedWorkspaceId: ref(1),
    demoMode: ref(false),
    showMiningTaskDialog: ref(false),
    showMiningSplitDialog: ref(false),
    showMiningAssignDialog: ref(false),
    miningTaskForm: {
      name: '', modality: 'ego', sop: '', target_episodes: 1000, target_duration_hours: 100,
      min_valid_rate: 0.9, due_date: '', project: '', purpose: '', scene: '', train: '', owner: '',
    },
    miningSplitForm: { batch_count: 10, per_batch: 100 },
    miningAssignForm: { batch_id: '', collector_ids: [], device_id: '', window: '', mode: 'even' },
    selectedTaskSetId: ref(null),
    collectionProjects: ref([]),
    collectionLabels: ref([]),
    miningSelectedTaskId: ref(''),
    saving: ref(false),
    locale: ref('zh-CN'),
    QuicDataAPI: {
      listCollectionProjects(workspaceId) {
        calls.push(['list-projects', workspaceId]);
        return new Promise((resolve) => pending.push({ workspaceId, resolve }));
      },
      createCollectionProject(body) {
        calls.push(['create-project', body]);
        return Promise.resolve({ id: 21, name: body.name });
      },
      createCollectionTask(body) {
        calls.push(['create-task', body]);
        return Promise.resolve({ id: 101 });
      },
    },
    ElMessage: { warning() {}, success() {} },
    t(key) { return key; },
    errorMessage() { throw new Error('stale project lookup must not surface an error'); },
    loadMiningData: async () => {},
  };
  vm.createContext(context);
  vm.runInContext(
    `let miningTaskDialogWorkspaceId = 0;
     let miningSplitDialogWorkspaceId = 0;
     let miningAssignDialogWorkspaceId = 0;
     let miningAssignTaskId = '';
     let miningWriteGeneration = 0;
     ${miningSource}
     globalThis.openTask = openMiningTaskDialog;
     globalThis.resetTask = resetMiningTaskDialogState;
     globalThis.saveTask = createMiningTask;`,
    context,
    { filename: 'app-mining-scope.js' },
  );

  context.openTask();
  context.miningTaskForm.name = 'A task';
  const saveA = context.saveTask();
  assert.deepEqual(calls, [['list-projects', 1]]);

  // This is the switchWorkspace invalidation and the user reopening the form
  // in B while A's project lookup is still unresolved.
  context.selectedWorkspaceId.value = 2;
  context.resetTask();
  context.openTask();
  context.miningTaskForm.name = 'B task';
  pending[0].resolve({ items: [] });
  await saveA;

  assert.deepEqual(calls, [['list-projects', 1]], 'late A lookup must not issue a project/task write for B');
  assert.equal(context.showMiningTaskDialog.value, true, 'the newer B form remains open');
  assert.equal(context.miningTaskForm.name, 'B task', 'the newer form remains intact');
});

test('a stale batch build completion cannot close or clear a newer workspace form', async () => {
  const pending = [];
  const calls = [];
  const context = {
    selectedWorkspaceId: ref(1),
    overviewBuildMode: ref(false),
    overviewBuildSelection: ref(new Set()),
    overviewBuildDialogVisible: ref(false),
    overviewBuildForm: {
      name: '', governance: { integrity: false, quality: false, compliance: false }, annotation: false,
      annotators: [], reviewers: [], reviewMode: 'single', sceneLabelIds: [], purposeLabelIds: [],
      modalityLabelIds: [], trainLabelIds: [],
    },
    overviewBuildSelectedInfo: ref({ scenes: [], purposes: [], modalities: [], trains: [] }),
    overviewImportExpandedGroupIds: ref(new Set()),
    overviewImportGroups: ref([]),
    pendingBuildPackageIds: ref([]),
    collectionLabels: ref([]),
    managedUsers: ref([]),
    annotatorUserOptions: ref([]),
    reviewerUserOptions: ref([]),
    saving: ref(false),
    dataOverviewRefreshKey: ref(0),
    locale: ref('zh-CN'),
    QuicDataAPI: {
      createDataBatch(body) {
        calls.push(body);
        return new Promise((resolve) => pending.push(resolve));
      },
    },
    loadCollectionLabels: async () => {},
    loadAdmin: async () => {},
    loadWorkspaceMembers: async () => {},
    ElMessage: { warning() {}, success() {} },
    t(key) { return key; },
    errorMessage() { throw new Error('stale batch completion must not surface an error'); },
  };
  vm.createContext(context);
  vm.runInContext(
    `let overviewBuildWorkspaceId = 0;
     let overviewBuildWriteGeneration = 0;
     ${buildSource}
     globalThis.openBatch = openPackageBatchDialog;
     globalThis.closeBatch = exitOverviewBuildMode;
     globalThis.submitBatch = submitOverviewBuild;`,
    context,
    { filename: 'app-overview-build-scope.js' },
  );

  context.openBatch([2001]);
  context.overviewBuildForm.name = 'A batch';
  const saveA = context.submitBatch();
  assert.deepEqual(JSON.parse(JSON.stringify(calls)), [{
    workspace_id: 1,
    name: 'A batch',
    data_package_ids: [2001],
    label_ids: [],
    integrity_check_enabled: false,
    quality_check_enabled: false,
    compliance_check_enabled: false,
    annotation_enabled: false,
    annotator_user_ids: [],
    reviewer_user_id: null,
    review_mode: 'single',
  }]);

  context.selectedWorkspaceId.value = 2;
  context.closeBatch();
  context.openBatch([3001]);
  context.overviewBuildForm.name = 'B batch';
  assert.equal(context.overviewBuildDialogVisible.value, true);
  assert.deepEqual(Array.from(context.pendingBuildPackageIds.value), [3001]);
  pending[0]({ id: 77 });
  await saveA;

  assert.equal(context.overviewBuildDialogVisible.value, true, 'A completion must not close B dialog');
  assert.equal(context.overviewBuildMode.value, true);
  assert.deepEqual(Array.from(context.pendingBuildPackageIds.value), [3001]);
  assert.equal(context.overviewBuildForm.name, 'B batch', 'A completion must not clear B form');
  assert.equal(context.dataOverviewRefreshKey.value, 0);
  assert.equal(context.saving.value, false);
});

test('refresh dashboard does not issue a B refresh after an A overview load becomes stale', async () => {
  const pendingOverview = [];
  const calls = [];
  const context = {
    user: ref({ id: 1 }),
    selectedWorkspaceId: ref(1),
    canViewDashboard: ref(true),
    demoMode: ref(false),
    QuicDataRealtime: {},
    loading: { dashboard: false },
    dashboardOverview: ref(null),
    dashboardOverviewError: ref(''),
    QuicDataDashboardState: undefined,
    QuicDataAPI: {
      getDashboardOverview(scope) {
        calls.push(['get', scope.workspace_id]);
        return new Promise((resolve) => pendingOverview.push(resolve));
      },
      refreshDashboard(scope) {
        calls.push(['refresh', scope.workspace_id]);
        return Promise.resolve({ job_id: 'unexpected-B', state: 'queued', scope_key: JSON.stringify(scope) });
      },
    },
    scheduleDashboardCharts: async () => {},
    disposeDashboardCharts() {},
    errorMessage() {},
    ElMessage: { error() {} },
    t(key) { return key; },
    ensureRealtimeConnection() {},
    replaceSubscription() {},
    removeSubscription() {},
  };
  context.QuicDataDashboardState = {
    resolveScope(_user, workspaceId) { return { workspace_id: Number(workspaceId), task_set_id: null }; },
    scopeKey(scope) { return JSON.stringify(scope); },
    jobFailed(job) { return job?.state === 'failed'; },
    jobSucceeded(job) { return job?.state === 'succeeded'; },
    isFreshSnapshot() { return false; },
  };
  vm.createContext(context);
  vm.runInContext(
    `let dashboardOverviewGeneration = 0;
     ${dashboardSource}
     globalThis.runRefresh = refreshDashboardData;
     globalThis.invalidateDashboardScope = () => {
       dashboardOverviewGeneration += 1;
       activeDashboardRefreshJobId = null;
       loading.dashboard = false;
     };`,
    context,
    { filename: 'app-dashboard-refresh-scope.js' },
  );

  const refreshA = context.runRefresh();
  assert.deepEqual(calls, [['get', 1]]);
  context.selectedWorkspaceId.value = 2;
  context.invalidateDashboardScope();
  pendingOverview[0]({ kpis: { total: 1 } });
  await refreshA;

  assert.deepEqual(calls, [['get', 1]], 'stale A overview load must not trigger a refresh for B');
  assert.equal(context.dashboardOverview.value, null);
  assert.equal(context.dashboardOverviewError.value, '');
  assert.equal(context.loading.dashboard, false);
});

test('an A refresh completion cannot install its job or error state into workspace B', async () => {
  const pendingRefresh = [];
  const calls = [];
  const context = {
    user: ref({ id: 1 }),
    selectedWorkspaceId: ref(1),
    canViewDashboard: ref(true),
    demoMode: ref(false),
    QuicDataRealtime: {},
    loading: { dashboard: false },
    dashboardOverview: ref(null),
    dashboardOverviewError: ref(''),
    QuicDataAPI: {
      getDashboardOverview(scope) {
        calls.push(['get', scope.workspace_id]);
        return Promise.resolve({ kpis: { total: scope.workspace_id } });
      },
      refreshDashboard(scope) {
        calls.push(['refresh', scope.workspace_id]);
        return new Promise((resolve) => pendingRefresh.push(resolve));
      },
    },
    scheduleDashboardCharts: async () => {},
    disposeDashboardCharts() {},
    errorMessage() { throw new Error('stale refresh must not surface an error'); },
    ElMessage: { error() { throw new Error('stale refresh must not toast'); } },
    t(key) { return key; },
    ensureRealtimeConnection() {},
    replaceSubscription() {},
    removeSubscription() {},
  };
  context.QuicDataDashboardState = {
    resolveScope(_user, workspaceId) { return { workspace_id: Number(workspaceId), task_set_id: null }; },
    scopeKey(scope) { return JSON.stringify(scope); },
    jobFailed(job) { return job?.state === 'failed'; },
    jobSucceeded(job) { return job?.state === 'succeeded'; },
    isFreshSnapshot() { return false; },
  };
  vm.createContext(context);
  vm.runInContext(
    `let dashboardOverviewGeneration = 0;
     ${dashboardSource}
     globalThis.runRefresh = refreshDashboardData;
     globalThis.invalidateDashboardScope = () => {
       dashboardOverviewGeneration += 1;
       activeDashboardRefreshJobId = null;
       loading.dashboard = false;
       dashboardOverview.value = null;
       dashboardOverviewError.value = '';
     };`,
    context,
    { filename: 'app-dashboard-refresh-completion-scope.js' },
  );

  const refreshA = context.runRefresh();
  await flush();
  await flush();
  assert.deepEqual(calls, [['get', 1], ['refresh', 1]]);
  context.selectedWorkspaceId.value = 2;
  context.invalidateDashboardScope();
  pendingRefresh[0]({ job_id: 'job-A', state: 'failed', error_message: 'A failed', scope_key: JSON.stringify({ workspace_id: 1, task_set_id: null }) });
  await refreshA;

  assert.deepEqual(calls, [['get', 1], ['refresh', 1]], 'a stale completion must not start a second refresh for B');
  assert.equal(context.dashboardOverview.value, null);
  assert.equal(context.dashboardOverviewError.value, '');
  assert.equal(context.loading.dashboard, false);
});

test('delayed dialog closed callbacks preserve a form that was reopened before transition end', () => {
  const dialogPairs = [
    ['showCollectionProjectDialog', 'onCollectionProjectDialogClosed', 'resetCollectionProjectDialogState'],
    ['showMiningTaskDialog', 'onMiningTaskDialogClosed', 'resetMiningTaskDialogState'],
    ['showMiningSplitDialog', 'onMiningSplitDialogClosed', 'resetMiningSplitDialogState'],
    ['showMiningAssignDialog', 'onMiningAssignDialogClosed', 'resetMiningAssignDialogState'],
    ['showCollectorDialog', 'onCollectorDialogClosed', 'resetCollectorDialogState'],
    ['showDeviceDialog', 'onDeviceDialogClosed', 'resetDeviceDialogState'],
    ['overviewBuildDialogVisible', 'onOverviewBuildDialogClosed', 'exitOverviewBuildMode'],
  ];
  for (const [visible, handler, reset] of dialogPairs) {
    assert.match(appSource, new RegExp(`@closed="${handler}"`));
    const start = appSource.indexOf(`function ${handler}`);
    const end = appSource.indexOf('\n      }', start) + '\n      }'.length;
    assert.ok(start >= 0 && end > start, `${handler} must remain a named close guard`);
    const body = appSource.slice(start, end);
    assert.match(body, new RegExp(`if \\(${visible}\\.value\\) return`));
    assert.match(body, new RegExp(`${reset}\\(\\)`));
  }
});

test('permission-center member selection is independent of batch assignment membership', async () => {
  const start = appSource.indexOf('let workspaceMembersGeneration = 0;');
  const end = appSource.indexOf('async function loadSettingsCenterData()', start);
  const calls = [];
  const context = {
    selectedWorkspaceId: ref(2), managementWorkspaceId: ref(4),
    workspaceMembers: ref([]), managementMembers: ref([]),
    canManageUsers: ref(true), loading: { members: false },
    errorMessage(error) { throw error; },
    QuicDataAPI: { async listWorkspaceMembers(id) {
      calls.push(id); return { list: [{ user_id: id * 10 }] };
    } },
  };
  vm.createContext(context);
  vm.runInContext(appSource.slice(start, end), context);
  await context.loadWorkspaceMembers();
  await context.loadManagementWorkspaceMembers();
  assert.deepEqual(calls, [2, 4]);
  assert.equal(context.workspaceMembers.value[0].user_id, 20);
  assert.equal(context.managementMembers.value[0].user_id, 40);
  assert.equal(context.selectedWorkspaceId.value, 2);
});
