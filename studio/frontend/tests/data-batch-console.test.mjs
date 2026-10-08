import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const apiSource = readFileSync(new URL('../js/api.js', import.meta.url), 'utf8');
const appSource = readFileSync(new URL('../js/app.js', import.meta.url), 'utf8');

test('governance retry posts the batch, workspace and actual stage to the server', async () => {
  const { api, calls } = loadApi();
  await api.retryDataBatchGovernance(12, { workspace_id: 4, stage: 'compliance' });
  assert.equal(calls[0].url, 'http://localhost:8010/api/v1/data-batches/12/governance/retry');
  assert.equal(calls[0].options.method, 'POST');
  assert.deepEqual(JSON.parse(calls[0].options.body), { workspace_id: 4, stage: 'compliance' });
});

test('failed governance requests never show completion and repeated clicks share one operation', async () => {
  const source = appSource.match(/async function rerunOverviewStage\(item, stage\) \{([\s\S]*?)\n      \}\n/)[1];
  const pending = [];
  const messages = [];
  const context = {
    selectedWorkspaceId: { value: 4 },
    hasPermission: () => true,
    overviewStageRerunOverrides: {},
    QuicDataAPI: { retryDataBatchGovernance: (...args) => new Promise((resolve, reject) => pending.push({ args, resolve, reject })) },
    loadDataBatches: async () => messages.push('reload'),
    ElMessage: { success: (message) => messages.push(message), warning: (message) => messages.push(message) },
    errorMessage: (error) => messages.push(error.message),
    t: (key) => key,
  };
  vm.runInNewContext(`globalThis.retry = async function(item, stage) {${source}\n}`, context);
  const first = context.retry({ batch_id: 12 }, 'desensitize');
  await context.retry({ batch_id: 12 }, 'desensitize');
  assert.equal(pending.length, 1);
  assert.equal(pending[0].args[1].stage, 'compliance');
  assert.deepEqual(messages, []);
  pending[0].reject(new Error('worker unavailable'));
  await first;
  assert.deepEqual(messages, ['worker unavailable']);
  const second = context.retry({ batch_id: 12 }, 'desensitize');
  pending[1].resolve({ status: 'passed' });
  await second;
  assert.deepEqual(messages, ['worker unavailable', 'reload', 'stageRerunDone']);
  const late = context.retry({ batch_id: 12 }, 'quality');
  context.selectedWorkspaceId.value = 9;
  pending[2].resolve({ status: 'passed' });
  await late;
  assert.equal(messages.length, 3);
});

function loadApi(responses = []) {
  const calls = [];
  const session = new Map();
  const context = {
    URL,
    window: { location: { origin: 'http://localhost:8010' } },
    sessionStorage: {
      getItem(key) { return session.get(key) || null; },
      setItem(key, value) { session.set(key, String(value)); },
      removeItem(key) { session.delete(key); },
    },
    async fetch(url, options = {}) {
      calls.push({ url: String(url), options });
      const payload = responses.shift() || { code: 200, data: {} };
      return { ok: true, status: 200, async json() { return payload; } };
    },
  };
  context.globalThis = context;
  vm.runInNewContext(`${apiSource}\n;globalThis.__api = QuicDataAPI;`, context, { filename: 'api.js' });
  return { api: context.__api, calls };
}

test('data-batch console API covers candidates create and label dictionary', async () => {
  for (const method of [
    'listDataBatchCandidates',
    'listDataBatches',
    'getDataBatch',
    'getDataBatchGovernanceReport',
    'createDataBatch',
    'listCollectionLabels',
  ]) {
    assert.match(apiSource, new RegExp(`\\b${method}\\s*\\(`));
  }

  const { api, calls } = loadApi([
    { code: 200, data: { token: 't', userInfo: { id: 1 } } },
    { code: 200, data: { items: [] } },
    { code: 200, data: { items: [] } },
    { code: 200, data: { id: 9 } },
    { code: 200, data: { items: [] } },
  ]);
  await api.login('admin@quicdata.local', 'password');
  await api.listDataBatchCandidates({ workspace_id: 3, collection_project_id: 2 });
  await api.getDataBatchGovernanceReport(9, 3);
  await api.createDataBatch({
    workspace_id: 3,
    name: 'batch-1',
    data_package_ids: [10],
    label_ids: [1, 2],
    integrity_check_enabled: true,
    quality_check_enabled: false,
    compliance_check_enabled: false,
    annotation_enabled: true,
    annotator_user_ids: [5],
    reviewer_user_id: 7,
    review_mode: 'single',
  });
  await api.listCollectionLabels({ workspace_id: 3, category: 'scene' });

  assert.match(calls[1].url, /\/api\/v1\/data-batches\/candidates\?/);
  assert.match(calls[1].url, /workspace_id=3/);
  assert.match(calls[2].url, /\/api\/v1\/data-batches\/9\/governance-report\?/);
  assert.match(calls[2].url, /workspace_id=3/);
  assert.equal(calls[3].url, 'http://localhost:8010/api/v1/data-batches');
  assert.equal(calls[3].options.method, 'POST');
  assert.deepEqual(JSON.parse(calls[3].options.body).label_ids, [1, 2]);
  assert.match(calls[4].url, /\/api\/v1\/collection-labels\?/);
});

test('build-batch dialog loads candidates and submits label ids from the dictionary', () => {
  assert.match(appSource, /listDataBatchCandidates/);
  assert.match(appSource, /createDataBatch/);
  assert.match(appSource, /listCollectionLabels/);
  assert.match(appSource, /loadDataBatchCandidates/);
  assert.match(appSource, /label_ids:/);
  assert.match(appSource, /params\.label_ids|label_ids:\s*\[\.\.\.new Set\(labelIds\)\]/);
  assert.match(appSource, /data_package_ids:/);
  assert.match(appSource, /annotator_user_ids:/);
  assert.match(appSource, /reviewer_user_id:/);
  assert.match(appSource, /user\.role === 'annotator' && user\.is_active !== false/);
  assert.match(appSource, /user\.role === 'auditor' && user\.is_active !== false/);
  assert.match(appSource, /请选择有效的标注员和审核员/);
  assert.match(appSource, /annotatorRoleHint/);
  assert.doesNotMatch(appSource, /user\.role === 'annotator' \|\| user\.role === 'admin'/);
  assert.match(appSource, /integrity_check_enabled:/);
  assert.doesNotMatch(appSource, /overviewBuildForm\.scene\.join/);
  const dialog = appSource.match(/<el-dialog v-model="overviewBuildDialogVisible"([\s\S]*?)<\/el-dialog>/);
  assert.ok(dialog, 'expected build-batch dialog');
  assert.doesNotMatch(dialog[1], /allow-create/);
  assert.match(dialog[1], /collectionLabelOptions|labelOptions|sceneLabelOptions/);
});

test('empty batch candidates never fall back to mock package names or governanceMocks', () => {
  assert.match(appSource, /const overviewImportRows = computed\(\(\) => batchCandidates\.value\.map/);
  assert.doesNotMatch(appSource, /governanceMocks/);
  assert.doesNotMatch(appSource, /ego_station_a_0811\.zip/);
  assert.doesNotMatch(appSource, /现场采集 OSS 扫描/);
  assert.doesNotMatch(appSource, /lerobot_run_001\.tar/);
  assert.doesNotMatch(appSource, /ds-mock-/);
  assert.match(appSource, /async function loadDataBatches\(/);
  assert.match(appSource, /listDataBatches/);
  assert.match(appSource, /getDataBatchGovernanceReport/);
  assert.match(appSource, /governanceBackendStageKey/);
  assert.match(appSource, /governanceBackendIssueItems/);
  assert.match(appSource, /if \(!demoMode\.value\) return \{ status: 'queued'/);
});
