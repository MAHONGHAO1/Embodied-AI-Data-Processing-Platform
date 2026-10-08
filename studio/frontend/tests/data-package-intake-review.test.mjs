import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const apiSource = readFileSync(new URL('../js/api.js', import.meta.url), 'utf8');
const workbenchSource = readFileSync(new URL('../js/intake-review-workbench.js', import.meta.url), 'utf8');
const appSource = readFileSync(new URL('../js/app.js', import.meta.url), 'utf8');

function loadApi(responses = []) {
  const calls = [];
  const session = new Map();
  const context = {
    URL,
    window: { location: { origin: 'http://localhost:8000' } },
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

test('QuicDataAPI exposes getOfflineManifest, reviewIntakePackage, and bulkApproveIntakePackages', async () => {
  for (const method of [
    'getOfflineManifest',
    'reviewIntakePackage',
    'bulkApproveIntakePackages',
    'getDataPackage',
    'getDataPackageEpisodePreviewUrls',
    'listDataPackages',
  ]) {
    assert.match(apiSource, new RegExp(`\\b${method}\\s*\\(`));
  }

  const { api, calls } = loadApi([
    { code: 200, data: { token: 'jwt-token', userInfo: { id: 1 } } },
    { code: 200, data: { schema_version: 1, package_uid: 'pkg-001', target_duration_hours: '2.00' } },
    { code: 200, data: { id: 42, status: 'intake_approved' } },
    { code: 200, data: { approved_count: 2, packages: [] } },
    { code: 200, data: { streams: [{ topic: '/camera/head/rgb', video_url: 'https://storage.test/video.mp4' }] } },
  ]);

  await api.login('admin@quicdata.local', 'password');
  await api.getOfflineManifest(42, 3);
  await api.reviewIntakePackage(42, { workspace_id: 3, verdict: 'approved' });
  await api.bulkApproveIntakePackages({ workspace_id: 3, data_package_ids: [42, 43] });
  await api.getDataPackageEpisodePreviewUrls(42, 7, 3);

  assert.match(calls[1].url, /\/api\/v1\/data-packages\/42\/offline-manifest\?/);
  assert.match(calls[1].url, /workspace_id=3/);
  assert.equal(calls[1].options.method, 'GET');

  assert.equal(calls[2].url, 'http://localhost:8000/api/v1/data-packages/42/intake-review');
  assert.equal(calls[2].options.method, 'POST');
  assert.deepEqual(JSON.parse(calls[2].options.body), { workspace_id: 3, verdict: 'approved' });

  assert.equal(calls[3].url, 'http://localhost:8000/api/v1/data-packages/intake-review/bulk-approve');
  assert.equal(calls[3].options.method, 'POST');
  assert.deepEqual(JSON.parse(calls[3].options.body), { workspace_id: 3, data_package_ids: [42, 43] });

  assert.match(calls[4].url, /\/api\/v1\/data-packages\/42\/episodes\/7\/preview-urls\?/);
  assert.match(calls[4].url, /workspace_id=3/);
  assert.equal(calls[4].options.method, 'GET');
});

test('buildCsvContent generates RFC 4180 CSV with UTF-8 BOM and escaped fields', () => {
  const sandbox = {};
  sandbox.globalThis = sandbox;
  vm.runInNewContext(
    `function extractCsvBuilder() {\n${appSource}\n};\nglobalThis.__appSource = true;`,
    sandbox,
    { filename: 'app.js' }
  );

  // Match the buildCsvContent implementation from app.js
  const match = appSource.match(/function buildCsvContent\(headers, rows\) \{([\s\S]*?)\n      \}/);
  assert.ok(match, 'buildCsvContent should be defined in app.js');

  const buildCsvContent = new Function('headers', 'rows', match[1]);

  const headers = ['schema_version', 'package_uid', 'target_duration_hours', 'notes'];
  const rows = [
    { schema_version: 1, package_uid: 'pkg-001', target_duration_hours: '2.50', notes: 'normal' },
    { schema_version: 1, package_uid: 'pkg-002', target_duration_hours: '3.00', notes: 'has,comma and "quotes"\nand newline' },
  ];

  const csv = buildCsvContent(headers, rows);
  assert.ok(csv.startsWith('\uFEFF'), 'CSV must include UTF-8 BOM for spreadsheet compatibility');

  const lines = csv.slice(1).split('\r\n');
  assert.equal(lines[0], 'schema_version,package_uid,target_duration_hours,notes');
  assert.equal(lines[1], '1,pkg-001,2.50,normal');
  assert.equal(lines[2], '1,pkg-002,3.00,"has,comma and ""quotes""\nand newline"');
});

test('app.js integrates data package drawer, intake review dialog, and bulk approval', () => {
  // Drawer markup and state
  assert.match(appSource, /<el-drawer v-model="dataPackageDrawerVisible"/);
  assert.match(appSource, /dataPackageDrawerVisible = ref\(false\)/);
  assert.match(appSource, /openDataPackageDrawer/);
  assert.match(appSource, /downloadPackageOfflineManifest/);

  // Offline manifest actions
  assert.match(appSource, /downloadManifestCsv/);
  assert.match(appSource, /downloadManifestJson/);
  assert.match(appSource, /isManifestUnavailable/);

  // Intake review actions
  assert.match(appSource, /approvePackageIntake/);
  assert.match(appSource, /openRejectPackageDialog/);
  assert.match(appSource, /submitRejectPackageIntake/);
  assert.match(appSource, /<el-dialog v-model="rejectDialogVisible"/);
  assert.match(appSource, /rejectReason/);

  // Intake review episode preview
  assert.match(appSource, /getDataPackageEpisodePreviewUrls/);
  assert.match(appSource, /openIntakeEpisodePreview/);
  assert.match(appSource, /<intake-review-workbench/);
  assert.match(workbenchSource, /<video/);
  assert.match(workbenchSource, /selectedStream/);

  // Package identity remains separate from individual Episode detail/preview.
  assert.match(appSource, /return openDataPackageDrawer\(\{ \.\.\.item, id: item\.data_package_id \|\| item\.id \}\)/);
  assert.match(appSource, /openEpisodeDetail\(scope\.row, \{ source: 'package' \}\)/);
  assert.doesNotMatch(appSource, /function importDetailArtifacts|<dd>30 Hz<\/dd>/);

  // Bulk approval
  assert.match(appSource, /bulkApproveSelectedPackages/);
  assert.match(appSource, /selectedMiningPackages/);
  assert.match(appSource, /handleMiningPackageSelectionChange/);

  // Ensure batch creation is wired to candidate packages (intake_approved)
  assert.match(appSource, /loadDataBatchCandidates/);
  assert.match(appSource, /createDataBatch/);
});

test('miningBatchReviewStatus correctly maps all data package raw statuses', () => {
  const match = appSource.match(/function miningBatchReviewStatus\(batch\) \{([\s\S]*?)\n      \}/);
  assert.ok(match, 'miningBatchReviewStatus should be defined in app.js');

  const fakeT = (key) => key;
  const stageOf = () => ({ status: 'queued' });
  const fn = new Function('batch', 't', 'stageOf', match[1]);

  assert.equal(fn({ raw_status: 'intake_approved' }, fakeT, stageOf).type, 'success');
  assert.equal(fn({ raw_status: 'voided' }, fakeT, stageOf).type, 'info');
  assert.equal(fn({ raw_status: 'pending_intake_review' }, fakeT, stageOf).type, 'warning');
  assert.equal(fn({ raw_status: 'ingested' }, fakeT, stageOf).type, 'warning');
  assert.equal(fn({ raw_status: 'parsing' }, fakeT, stageOf).type, 'warning');
  assert.equal(fn({ raw_status: 'assigned' }, fakeT, stageOf).type, 'primary');
  assert.equal(fn({ raw_status: 'pending_assignment' }, fakeT, stageOf).type, 'info');
  assert.equal(fn({ raw_status: 'batched' }, fakeT, stageOf).type, 'success');
  assert.equal(fn({ raw_status: 'parse_failed' }, fakeT, stageOf).type, 'danger');
});

test('app.js exposes collection task offline manifest download and retires web file upload', () => {
  // Task-level manifest actions
  assert.match(appSource, /downloadTaskOfflineManifest/);
  assert.match(appSource, /handleTaskRowCommand/);
  assert.match(appSource, /downloadTaskManifest/);

  // Manifest download dropdown on task table row and task detail header
  assert.match(appSource, /handleTaskRowCommand\(cmd, scope\.row\)/);
  assert.match(appSource, /handleTaskRowCommand\(cmd, miningSelectedTask\)/);

  // Browser file upload is retired from import dialog
  assert.doesNotMatch(appSource, /<el-upload[^>]*selectImportFile/);
  assert.doesNotMatch(appSource, /<el-radio-button label="upload"/);
});

test('miningBatchTargetHoursText and duration text functions correctly display hours for real data packages', () => {
  const targetMatch = appSource.match(/function miningBatchTargetHoursText\(batch\) \{([\s\S]*?)\n      \}/);
  assert.ok(targetMatch, 'miningBatchTargetHoursText should be defined');
  const fakeT = (key) => key === 'dashHour' ? '小时' : key;
  const demoMode = { value: false };
  const targetFn = new Function('batch', 't', 'demoMode', targetMatch[1]);

  // Real package with 2.00 hours target duration should format to expected duration string, not "0.0"
  assert.equal(targetFn({ target_duration_hours: '2.00' }, fakeT, demoMode), '2.0 小时');
  assert.equal(targetFn({ target: 2 }, fakeT, demoMode), '2.0 小时');
  assert.equal(targetFn({ target_duration_hours: '0.00' }, fakeT, demoMode), '—');

  // Assignee devices filter out null / empty device_id
  const devMatch = appSource.match(/function miningAssigneeDevices\(batch\) \{([\s\S]*?)\n      \}/);
  assert.ok(devMatch, 'miningAssigneeDevices should be defined');
  const collectionDevices = { value: [{ id: 1, name: 'CamA', serial_number: 'SN1', software_number: 'SW1' }] };
  const devFn = new Function('batch', 'collectionDevices', devMatch[1]);

  // When device_id is null, it should NOT return [{ name: '#null', meta: '' }]
  const resNull = devFn({ assignees: [{ collector_id: 1, device_id: null }] }, collectionDevices);
  assert.deepEqual(resNull, [], 'null device_id should be filtered out to prevent #null');

  const resValid = devFn({ assignees: [{ collector_id: 1, device_id: 1 }] }, collectionDevices);
  assert.equal(resValid.length, 1);
  assert.equal(resValid[0].name, 'CamA');
});

test('collection review lists data packages instead of legacy batches', () => {
  assert.match(appSource, /reviewPackagesTitle: '数据包审核'/);
  assert.match(appSource, /dataPackageStatusLabel: '数据包状态'/);
  assert.match(appSource, /function loadReviewPackages\(\) \{/);
  assert.match(appSource, /QuicDataAPI\.listDataPackages\(params\)/);
  assert.match(appSource, /@selection-change="onReviewPackageSelectionChange"/);
  assert.match(appSource, /@click="openIntakeReview\(scope\.row\)"/);
  assert.doesNotMatch(appSource, /@click\.stop="openRejectPackageDialog\(scope\.row\)"/);
  const reviewTableSource = appSource.slice(
    appSource.indexOf('<template v-else>\n                  <div class="page-actions collection-scope-actions">'),
    appSource.indexOf('<section v-else-if="activeView === \'workbench\'"'),
  );
  assert.match(reviewTableSource, /:placeholder="t\('dataPackageStatusLabel'\)"/);
  assert.match(reviewTableSource, /:label="t\('dataPackageStatusLabel'\)"/);
  assert.match(
    reviewTableSource,
    /:label="t\('uploadEndTime'\)"[\s\S]*?:label="t\('collectionTimeLabel'\)"[^>]*>\s*<template #default="scope">\{\{ miningBatchCollectionTimeText\(scope\.row\) \}\}/,
  );
  assert.doesNotMatch(reviewTableSource, /class="table-action-create-supplement"/);
  assert.doesNotMatch(reviewTableSource, /createPackageSupplement\(scope\.row\)/);
  assert.match(reviewTableSource, /class="review-package-actions"/);
  assert.match(reviewTableSource, /link size="small" type="primary" class="table-action-view-detail" @click\.stop="openDataPackageDrawer\(scope\.row\)"/);
  assert.doesNotMatch(reviewTableSource, /class="table-action-more"/);
  assert.doesNotMatch(reviewTableSource, /handlePackageRowCommand\(cmd, scope\.row\)/);
  assert.doesNotMatch(reviewTableSource, /t\('reviewStatusLabel'\)/);
  assert.doesNotMatch(reviewTableSource, /t\('dataPackageName'\)/);
  // Review verdicts follow the backend contract for data packages.
  assert.match(
    appSource,
    /function canReviewPackageRow\(row\) \{[\s\S]*?status === 'pending_intake_review'/,
  );
  // The review view no longer pages over legacy batches.
  assert.doesNotMatch(appSource, /openBatchWithMiningMock\(batches\.value\[0\]\)/);
});

test('supplement package action is shown after a final intake verdict', () => {
  const helperMatch = appSource.match(/function canCreatePackageSupplement\(pkg\) \{([\s\S]*?)\n      \}/);
  assert.ok(helperMatch, 'canCreatePackageSupplement should be defined');
  const canCreate = new Function('pkg', helperMatch[1]);

  assert.equal(canCreate({ id: 1, status: 'intake_approved' }), true);
  assert.equal(canCreate({ id: 2, raw_status: 'intake_approved', status: '已审核' }), true);
  assert.equal(canCreate({ id: 3, status: 'pending_intake_review' }), false);
  assert.equal(canCreate({ id: 4, status: 'voided', intake_review: { verdict: 'rejected' } }), true);
  assert.equal(canCreate({ id: 5, status: 'batched', intake_review: { verdict: 'approved' } }), true);
  assert.equal(canCreate({ id: 6, status: 'voided' }), false);
  assert.equal(canCreate({ id: 7, status: 'assigned' }), false);

  assert.doesNotMatch(appSource, /createPackageSupplement\(scope\.row\)/);
  assert.match(appSource, /v-if="canCreatePackageSupplement\(packageDetail\)"[^>]*@click="createPackageSupplement\(packageDetail\)"/);
  assert.match(appSource, /v-if="canCreatePackageSupplement\(packageDetail\)"[^>]*type="warning" class="table-action-create-supplement"/);
});

test('a package row can jump straight into the collection review queue', () => {
  assert.match(appSource, /@click\.stop="openPackageIntakeReview\(scope\.row\)"/);
  assert.match(appSource, /\{\{ t\('openIntakeReview'\) \}\}/);
  assert.match(appSource, /openIntakeReview: '数采审核'/);
  assert.match(appSource, /function openPackageIntakeReview\(row\)/);
  // The jump scopes the review to the package project and opens the full-screen review page.
  assert.match(
    appSource,
    /function openPackageIntakeReview\(row\) \{[\s\S]*?miningProjectScope\.value = \[projectName\][\s\S]*?openIntakeReview\(row\);/,
  );
  assert.doesNotMatch(appSource, /function openPackageIntakeReview\(row\) \{[\s\S]*?openDataPackageDrawer\(target\)/);
  assert.match(appSource, /showMiningTaskPackagesPage\.value = false;/);
});

test('collection task packages open as a routed page and hide the data package name column', () => {
  const packageListStart = appSource.indexOf('<div v-if="showMiningTaskPackagesPage" class="mining-task-packages-page">');
  const packageListSource = appSource.slice(packageListStart, appSource.indexOf('<collection-overview', packageListStart));
  assert.ok(packageListStart >= 0, 'expected the collection task package page');
  assert.doesNotMatch(appSource, /v-model="showMiningTaskPackagesPage"[^>]*custom-class="mining-task-drawer"/);
  assert.match(appSource, /target\.hash = `#\/miningTasks\?task_id=\$\{encodeURIComponent\(row\.id\)\}`/);
  // Same-tab SPA navigation keeps the signed-in session.
  assert.match(appSource, /window\.location\.assign\(target\.toString\(\)\)/);
  assert.match(appSource, /function closeMiningTaskPackagesPage\(\)[\s\S]*?window\.location\.hash = '#\/miningTasks'/);
  assert.match(appSource, /const taskId = route\.query\.task_id \|\| ''/);
  assert.match(appSource, /'is-task-packages-focus': miningTaskPackagesFocusMode/);
  assert.match(appSource, /activeView\.value === 'miningTasks' && showMiningTaskPackagesPage\.value/);
  assert.match(packageListSource, /@click="closeMiningTaskPackagesPage">← \{\{ t\('miningTaskList'\) \}\}/);
  assert.doesNotMatch(packageListSource, /<el-table-column :label="t\('dataPackageName'\)"/);
  assert.match(packageListSource, /:expand-row-keys="miningInlinePackageExpandedKeys"/);
  assert.match(packageListSource, /@click="openMiningInlinePackageDetail\(scope\.row\)"/);
  assert.doesNotMatch(packageListSource, /@click="openDataPackageDrawer\(scope\.row\)"/);
  assert.match(packageListSource, /class="mining-inline-package-detail"/);
  assert.match(packageListSource, /class="mining-inline-video-frame"/);
  assert.match(packageListSource, /@click="miningInlineExtraInfoVisible = true">\{\{ t\('extraInfo'\) \}\}/);
  assert.match(packageListSource, /@click="closeMiningInlinePackageDetail">\{\{ t\('closePreview'\) \}\}/);
  assert.match(appSource, /:label="t\('collectPeriodLabel'\)"[^>]*>\s*<template #default="scope">\{\{ miningTaskCollectionPeriodText\(scope\.row\) \}\}/);
  assert.match(
    packageListSource,
    /:label="t\('uploadEndTime'\)"[\s\S]*?:label="t\('collectionTimeLabel'\)"[^>]*>\s*<template #default="scope">\{\{ miningBatchCollectionTimeText\(scope\.row\) \}\}/,
  );
  assert.match(packageListSource, /<el-dialog v-model="miningInlineExtraInfoVisible"[^>]*append-to-body[^>]*class="mining-inline-extra-dialog">/);
  assert.match(packageListSource, /<div class="mining-inline-package-header">/);
  assert.match(packageListSource, /<div class="mining-inline-package-grid">/);
  assert.match(packageListSource, /<section class="mining-inline-package-section mining-inline-episodes">/);
  assert.match(appSource, /const miningInlineExtraInfoVisible = ref\(false\)/);
  assert.ok(
    packageListSource.indexOf('class="mining-inline-video-section"') < packageListSource.indexOf('class="mining-inline-package-grid"'),
    'media preview should appear before the optional package metadata',
  );
  assert.match(packageListSource, /@click="prevMiningInlineEpisodeVideo"/);
  assert.match(packageListSource, /@click="nextMiningInlineEpisodeVideo"/);
  assert.match(packageListSource, /:src="miningInlinePreviewStream\.url"/);
  assert.match(appSource, /QuicDataAPI\.getDataPackageEpisodePreviewUrls\(packageId, episode\.id, workspaceId\)/);
  assert.doesNotMatch(packageListSource, /class="table-action-create-supplement"/);
  assert.doesNotMatch(packageListSource, /createPackageSupplement\(scope\.row\)/);
  assert.doesNotMatch(packageListSource, /createPackageSupplement\(miningInlinePackageDetail\)/);
});

test('task manifest uses server contract and never downloads after request failure or scope change', async () => {
  const source = appSource.slice(appSource.indexOf('      async function downloadTaskOfflineManifest('), appSource.indexOf('      function handleTaskRowCommand('));
  const downloads = [], messages = [];
  const workspace = { value: 3 };
  let response = { items: [{ package_uid: 'pkg-real', manifest_revision: '2:revision' }], manifest_revision: '2:revision' };
  const context = {
    miningSelectedTask: { value: null }, selectedWorkspaceId: workspace,
    QuicDataAPI: { async getTaskOfflineManifest(id, scope) { assert.equal(id, 2); assert.equal(scope, 3); if (response instanceof Error) throw response; return response; } },
    downloadCsvFile(...args) { downloads.push(args); }, downloadJsonFile(...args) { downloads.push(args); },
    ElMessage: { success: (s) => messages.push(['success', s]), error: (s) => messages.push(['error', s]), warning: (s) => messages.push(['warning', s]) },
    t: (s) => s,
  };
  vm.runInNewContext(source + ';globalThis.run = downloadTaskOfflineManifest;', context);
  await context.run({ id: 2, name: 'real task' }, 'json');
  assert.equal(downloads.length, 1);
  assert.equal(downloads[0][1].manifest_revision, '2:revision');
  response = new Error('offline');
  await context.run({ id: 2 }, 'csv');
  assert.equal(downloads.length, 1);
  assert.deepEqual(messages.at(-1), ['error', 'offline']);
  response = { items: [] };
  await context.run({ id: 2 });
  assert.equal(downloads.length, 1);
  assert.equal(messages.at(-1)[0], 'warning');
  context.QuicDataAPI.getTaskOfflineManifest = async () => { workspace.value = 4; return { items: [{package_uid:'stale'}] }; };
  await context.run({ id: 2 });
  assert.equal(downloads.length, 1);
});

test('task manifest API passes task and workspace; completion time never uses edit time', async () => {
  const { api, calls } = loadApi([{code:200,data:{items:[],manifest_revision:''}}]);
  await api.getTaskOfflineManifest(2, 3);
  assert.match(calls[0].url, /\/collection-tasks\/2\/offline-manifest\?workspace_id=3/);
  const source = appSource.slice(appSource.indexOf('      function miningBatchUploadEndText('), appSource.indexOf('      function miningBatchUnassigned('));
  const context = { formatDate: (v) => 'formatted:' + v };
  vm.runInNewContext(source + ';globalThis.run = miningBatchUploadEndText;', context);
  assert.equal(context.run({ updated_at: 'today', window: 'yesterday~today' }), '—');
  assert.equal(context.run({ upload_completed_at: 'actual' }), 'formatted:actual');
});

test('collection task period and package collection time come from package capture facts only', () => {
  const source = appSource.slice(appSource.indexOf('      function miningTaskCollectionPeriodText('), appSource.indexOf('      function miningBatchUnassigned('));
  const context = { formatDate: (value) => String(value) };
  vm.runInNewContext(`${source};globalThis.period = miningTaskCollectionPeriodText; globalThis.collectionTime = miningBatchCollectionTimeText;`, context);
  assert.equal(context.period({ captured_started_from: '2026-08-01 09:00', captured_started_to: '2026-08-03 18:00' }), '2026-08-01 ~ 2026-08-03');
  assert.equal(context.period({ captured_started_from: '2026-08-01 09:00', captured_started_to: '2026-08-01 18:00' }), '2026-08-01');
  // Lifecycle timestamps are not collection time.
  assert.equal(context.period({ created_at: '2026-08-01 09:00', updated_at: '2026-08-03 18:00' }), '—');
  assert.equal(context.collectionTime({ captured_started_at: '2026-08-05 09:00' }), '2026-08-05 09:00');
  assert.equal(context.collectionTime({ window: '08-05 ~ 08-07', assigned_at: '2026-08-05 09:00' }), '—');
  assert.equal(context.collectionTime({}), '—');
});
