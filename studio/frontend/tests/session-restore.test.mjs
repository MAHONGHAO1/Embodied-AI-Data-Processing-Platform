import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const appSource = readFileSync(new URL('../js/app.js', import.meta.url), 'utf8');
const apiSource = readFileSync(new URL('../js/api.js', import.meta.url), 'utf8');
const accessPolicySource = readFileSync(new URL('../js/access-policy.js', import.meta.url), 'utf8');
const episodeDetailSource = readFileSync(new URL('../js/episode-detail.js', import.meta.url), 'utf8');
const workQueueSource = readFileSync(new URL('../js/work-queue.js', import.meta.url), 'utf8');
const scopePreferenceSource = readFileSync(new URL('../js/scope-preference.js', import.meta.url), 'utf8');

function loadAccessPolicy() {
  const context = {};
  context.globalThis = context;
  vm.runInNewContext(`${accessPolicySource}\n;globalThis.__policy = QuicDataAccessPolicy;`, context);
  return context.__policy;
}

function loadWorkQueue() {
  const context = {};
  context.globalThis = context;
  vm.runInNewContext(`${workQueueSource}\n;globalThis.__queue = QuicDataWorkQueue;`, context);
  return context.__queue;
}

function loadEpisodeDetail() {
  const context = {};
  context.globalThis = context;
  vm.runInNewContext(`${episodeDetailSource}\n;globalThis.__episodeDetail = QuicDataEpisodeDetail;`, context);
  return context.__episodeDetail;
}

function loadScopePreference() {
  const context = {};
  context.globalThis = context;
  vm.runInNewContext(`${scopePreferenceSource}\n;globalThis.__scopePreference = QuicDataScopePreference;`, context);
  return context.__scopePreference;
}

test('login markup contains one native form and no nested Element form', () => {
  const loginMarkup = appSource.match(/<section v-else-if="!user"[\s\S]*?<\/section>/)?.[0] || '';

  assert.equal((loginMarkup.match(/<form\b/g) || []).length, 1);
  assert.doesNotMatch(loginMarkup, /<el-form\b/);
});

test('session restoration has an explicit first-render state before the login form', () => {
  assert.match(appSource, /const sessionRestoring = ref\(true\)/);
  assert.match(appSource, /<section v-if="sessionRestoring" class="session-restore-view"/);
  assert.match(appSource, /<section v-else-if="!user" class="login-view"/);
});

test('browser authentication keeps tokens and user state in memory only', () => {
  assert.doesNotMatch(apiSource, /sessionStorage|localStorage|quicdata_refresh|refresh_token/);
  assert.match(apiSource, /let currentUserInfo = null/);
  assert.match(apiSource, /credentials: 'same-origin'/);
  assert.match(apiSource, /fetch\(`\$\{base\}\/auth\/refresh`, \{[\s\S]*?method: 'POST'/);
  assert.doesNotMatch(apiSource, /body: JSON\.stringify\(\{ refresh_token/);
});

function mountApp({
  storedUser,
  restoreResult,
  loginResult = null,
  initialHash = '',
  workspaceList = [],
  taskSetList = [],
  scopeStorage = null,
}) {
  let component;
  let mounted;
  let hashchange;
  let restoreCalls = 0;
  let workspaceCalls = 0;
  let workQueueCalls = 0;
  let annotationCalls = 0;
  let reviewCalls = 0;
  let datasetCalls = 0;
  let warningCalls = 0;

  const context = {
    URLSearchParams,
    Vue: {
      defineAsyncComponent(options) { return options; },
      createApp(options) {
        component = options;
        return {
          use() { return this; },
          component() { return this; },
          mount() {},
        };
      },
      computed(getter) {
        return { get value() { return getter(); } };
      },
      onMounted(callback) {
        mounted = callback;
      },
      reactive(value) { return value; },
      ref(value) { return { value }; },
    },
    window: {
      location: { hash: initialHash },
      setTimeout() { return 1; },
      clearTimeout() {},
      addEventListener(event, listener) {
        if (event === 'hashchange') hashchange = listener;
      },
    },
    ElementPlus: { ElMessage: { error() {}, success() {}, warning() { warningCalls += 1; } } },
    QuicDataAccessPolicy: loadAccessPolicy(),
    QuicDataEpisodeDetail: loadEpisodeDetail(),
    QuicDataWorkQueue: loadWorkQueue(),
    QuicDataScopePreference: loadScopePreference(),
    localStorage: scopeStorage,
    QuicDataAPI: {
      currentUser() { return storedUser; },
      async restoreSession() {
        restoreCalls += 1;
        return typeof restoreResult === 'function' ? restoreResult() : restoreResult;
      },
      async login() { return loginResult; },
      async listWorkspaces() {
        workspaceCalls += 1;
        return { list: workspaceList };
      },
      async listTaskSets() { return { list: taskSetList }; },
      async listBatches() { return { items: [] }; },
      async listEpisodes() { return { items: [] }; },
      async listWorkQueue() {
        workQueueCalls += 1;
        return { items: [], total: 0 };
      },
      async listAnnotationWorkItems() {
        annotationCalls += 1;
        return { items: [] };
      },
      async listReviewWorkItems() {
        reviewCalls += 1;
        return { items: [] };
      },
      async listDatasets() {
        datasetCalls += 1;
        return { items: [] };
      },
      async listCollectorProfiles() { return { items: [] }; },
      async listCollectionDevices() { return { items: [] }; },
      async listTaskLabels() { return { items: [] }; },
    },
  };
  const overviewSource = readFileSync(new URL('../js/collection-overview.js', import.meta.url), 'utf8');
  const dataOverviewSource = readFileSync(new URL('../js/data-overview.js', import.meta.url), 'utf8');
  const intakeReviewSource = readFileSync(new URL('../js/intake-review-workbench.js', import.meta.url), 'utf8');
  const packageAnnotationSource = readFileSync(new URL('../js/package-annotation-workbench.js', import.meta.url), 'utf8');
  const catalogSource = readFileSync(new URL('../js/data-catalog.js', import.meta.url), 'utf8');
  const pageComponentsSource = readFileSync(new URL('../js/page-components.js', import.meta.url), 'utf8');
  vm.runInNewContext(`${pageComponentsSource}\n${catalogSource}\n${packageAnnotationSource}\n${overviewSource}\n${dataOverviewSource}\n${intakeReviewSource}\n${appSource}`, context, { filename: 'app.js' });
  const view = component.setup();
  return {
    view,
    async mount() { await mounted(); },
    restoreCalls: () => restoreCalls,
    workspaceCalls: () => workspaceCalls,
    workQueueCalls: () => workQueueCalls,
    annotationCalls: () => annotationCalls,
    reviewCalls: () => reviewCalls,
    datasetCalls: () => datasetCalls,
    warningCalls: () => warningCalls,
    triggerHashChange(hash) {
      context.window.location.hash = hash;
      hashchange?.();
    },
  };
}

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

test('expired saved session returns to the login view after a development reset', async () => {
  const app = mountApp({
    storedUser: { id: 1, email: 'admin@quicdata.com', role: 'admin' },
    restoreResult: false,
  });

  await app.mount();

  assert.equal(app.restoreCalls(), 1);
  assert.equal(app.workspaceCalls(), 0);
  assert.equal(app.view.user.value, null);
  assert.equal(app.view.ready.value, true);
  assert.equal(app.view.sessionRestoring.value, false);
});

test('login remains hidden while the refresh request is unresolved', async () => {
  const savedSession = deferred();
  const app = mountApp({
    storedUser: { id: 1, email: 'admin@quicdata.com', role: 'admin' },
    restoreResult: () => savedSession.promise,
  });

  const mounting = app.mount();
  await Promise.resolve();

  assert.equal(app.view.sessionRestoring.value, true);
  assert.equal(app.view.user.value, null);
  assert.equal(app.view.ready.value, false);

  savedSession.resolve(false);
  await mounting;
  assert.equal(app.view.sessionRestoring.value, false);
});

test('a successful explicit login wins over a late failed session restore', async () => {
  const savedSession = deferred();
  const signedInUser = { id: 1, email: 'admin@quicdata.com', role: 'admin' };
  const app = mountApp({
    storedUser: signedInUser,
    restoreResult: () => savedSession.promise,
    loginResult: { userInfo: signedInUser },
  });

  const mounting = app.mount();
  await Promise.resolve();
  await app.view.signIn();
  savedSession.resolve(false);
  await mounting;

  assert.equal(app.view.user.value, signedInUser);
});

test('a bootstrap account enters password change without loading the console', async () => {
  const bootstrapUser = {
    id: 7,
    email: 'uat-admin@example.com',
    role: 'admin',
    must_change_password: true,
  };
  const app = mountApp({
    storedUser: bootstrapUser,
    restoreResult: true,
  });

  await app.mount();

  assert.equal(app.view.user.value, bootstrapUser);
  assert.equal(app.view.showChangePassword?.value, true);
  assert.equal(app.workspaceCalls(), 0);
});

test('a bootstrap account ignores console hash navigation before changing its password', async () => {
  const app = mountApp({
    storedUser: { id: 7, email: 'uat-admin@example.com', role: 'admin', must_change_password: true },
    restoreResult: true,
  });

  await app.mount();
  app.view.selectedWorkspaceId.value = 3;
  app.triggerHashChange('#/work-queue');
  await Promise.resolve();

  assert.equal(app.workQueueCalls(), 0);
});

test('password change form reports a short new password before submitting it', () => {
  assert.match(appSource, /passwordTooShort:/);
  assert.match(appSource, /passwordForm\.newPassword\.length < 10/);
});

test('annotator lands on annotation work without preloading an unauthorized dataset', async () => {
  const annotator = {
    id: 8,
    email: 'annotator@quicdata.local',
    role: 'annotator',
    permissions: ['workspace:read', 'batch:read', 'import:read', 'episode:read', 'episode:annotate'],
  };
  const app = mountApp({
    storedUser: annotator,
    restoreResult: true,
    workspaceList: [{ id: 3, name: 'UAT' }],
    taskSetList: [{ id: 5, name: 'Test' }],
  });

  await app.mount();

  assert.equal(app.view.activeView.value, 'work-queue');
  assert.equal(app.view.queueStage.value, 'annotation');
  assert.equal(app.annotationCalls(), 1);
  assert.equal(app.reviewCalls(), 0);
  assert.equal(app.workQueueCalls(), 0);
  assert.equal(app.datasetCalls(), 0);

  await app.view.switchWorkspace();
  assert.equal(app.annotationCalls(), 2);
  assert.equal(app.datasetCalls(), 0);
});

test('auditor lands on the review queue', async () => {
  const auditor = {
    id: 9,
    email: 'auditor@quicdata.local',
    role: 'auditor',
    permissions: ['workspace:read', 'batch:read', 'import:read', 'episode:read', 'episode:review'],
  };
  const app = mountApp({
    storedUser: auditor,
    restoreResult: true,
    workspaceList: [{ id: 3, name: 'UAT' }],
    taskSetList: [{ id: 5, name: 'Test' }],
  });

  await app.mount();

  assert.equal(app.view.activeView.value, 'work-queue');
  assert.equal(app.view.queueStage.value, 'review');
  assert.equal(app.reviewCalls(), 1);
  assert.equal(app.annotationCalls(), 0);
  assert.equal(app.workQueueCalls(), 0);
  assert.equal(app.datasetCalls(), 0);
});

test('console restores the current user\'s valid workspace and task set preference', async () => {
  const values = new Map();
  const storage = {
    getItem(key) { return values.get(key) || null; },
    setItem(key, value) { values.set(key, String(value)); },
  };
  storage.setItem('quicdata.scope-preference.v1.7', JSON.stringify({
    version: 1,
    workspace_id: 1,
    task_set_id: 11,
  }));
  const app = mountApp({
    storedUser: {
      id: 7,
      email: 'admin@quicdata.local',
      role: 'admin',
      permissions: ['workspace:read', 'batch:read', 'import:read', 'episode:read', 'episode:cut'],
    },
    restoreResult: true,
    workspaceList: [
      { id: 1, name: 'remembered', created_at: '2026-08-01T00:00:00Z' },
      { id: 3, name: 'latest fallback', created_at: '2026-08-02T00:00:00Z' },
    ],
    taskSetList: [
      { id: 11, name: 'remembered task', created_at: '2026-08-01T00:00:00Z' },
      { id: 12, name: 'latest task', created_at: '2026-08-02T00:00:00Z' },
    ],
    scopeStorage: storage,
  });

  await app.mount();

  assert.equal(app.view.selectedWorkspaceId.value, 1);
  assert.equal(app.view.selectedTaskSetId.value, 11);
});

test('an unauthorized dataset deep link redirects before its loader and warns only once', async () => {
  const annotator = {
    id: 8,
    email: 'annotator@quicdata.local',
    role: 'annotator',
    permissions: ['workspace:read', 'batch:read', 'import:read', 'episode:read', 'episode:annotate'],
  };
  const app = mountApp({
    storedUser: annotator,
    restoreResult: true,
    initialHash: '#/datasets',
    workspaceList: [{ id: 3, name: 'UAT' }],
    taskSetList: [{ id: 5, name: 'Test' }],
  });

  await app.mount();
  app.triggerHashChange('#/datasets');
  await Promise.resolve();

  assert.equal(app.view.activeView.value, 'work-queue');
  assert.equal(app.datasetCalls(), 0);
  assert.equal(app.warningCalls(), 1);
});

test('package workbench refresh restores its own workspace and item without preloading another queue', async () => {
  const app = mountApp({
    storedUser: { id: 7, role: 'annotator', permissions: ['workspace:read', 'episode:read', 'episode:annotate'] },
    restoreResult: true,
    initialHash: '#/package-workbench?item_id=12&workspace_id=3&mode=annotation',
    workspaceList: [{ id: 3, name: 'Annotation' }, { id: 4, name: 'Another workspace' }],
  });
  await app.mount();
  assert.equal(app.view.activeView.value, 'package-workbench');
  // The workbench uses the item's workspace; the collection scope is left alone.
  assert.equal(app.view.packageWorkbenchRoute.workspaceId, 3);
  assert.equal(app.view.packageWorkbenchRoute.workItemId, 12);
  assert.equal(app.view.packageWorkbenchRoute.mode, 'annotation');
  assert.equal(app.annotationCalls(), 0);
});

test('package workbench opens a deep link outside member workspaces; assignment authorizes it', async () => {
  const app = mountApp({
    storedUser: { id: 7, role: 'annotator', permissions: ['workspace:read', 'episode:read', 'episode:annotate'] },
    restoreResult: true,
    initialHash: '#/package-workbench?item_id=12&workspace_id=99&mode=annotation',
    workspaceList: [{ id: 3, name: 'Annotation' }],
  });
  await app.mount();
  assert.equal(app.view.activeView.value, 'package-workbench');
  assert.equal(app.view.packageWorkbenchRoute.workspaceId, 99);
  assert.equal(app.warningCalls(), 0);
});

test('package workbench rejects a malformed deep link', async () => {
  const app = mountApp({
    storedUser: { id: 7, role: 'annotator', permissions: ['workspace:read', 'episode:read', 'episode:annotate'] },
    restoreResult: true,
    initialHash: '#/package-workbench?item_id=12&workspace_id=0&mode=annotation',
    workspaceList: [{ id: 3, name: 'Annotation' }],
  });
  await app.mount();
  assert.equal(app.view.activeView.value, 'work-queue');
  assert.equal(app.warningCalls(), 1);
});
