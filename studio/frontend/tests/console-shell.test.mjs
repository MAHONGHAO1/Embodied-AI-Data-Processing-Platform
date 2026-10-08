import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const appSource = readFileSync(new URL('../js/app.js', import.meta.url), 'utf8');
const cssSource = readFileSync(new URL('../css/app.css', import.meta.url), 'utf8');
const indexSource = readFileSync(new URL('../index.html', import.meta.url), 'utf8');
const workQueueSource = readFileSync(new URL('../js/work-queue.js', import.meta.url), 'utf8');

test('console has one Batch/Episode navigation shell without duplicate Episode or EGO primary areas', () => {
  assert.match(appSource, /<aside class="sidebar"/);
  assert.match(appSource, /data-view="work-queue"/);
  assert.match(appSource, /data-view="batches"/);
  assert.match(appSource, /data-view="resources"/);
  assert.match(appSource, /data-view="assets"/);
  assert.doesNotMatch(appSource, /data-view="episodes"/);
  assert.doesNotMatch(appSource, /data-view="ego"/);
  assert.doesNotMatch(appSource, /device_capture|device_direct/);
});

test('every primary navigation entry is projected from backend permissions', () => {
  for (const view of ['work-queue', 'batches', 'resources', 'assets', 'datasets', 'admin', 'settings']) {
    const escapedView = view.replace('-', '\\-');
    assert.match(appSource, new RegExp(`v-if="canView\\('${escapedView}'\\)"[\\s\\S]*?data-view="${escapedView}"`));
  }
});

test('authorized workbench hashes keep their item and Episode query parameters', () => {
  const routeSync = appSource.match(/function syncRoute\(\) \{([\s\S]*?)\n      \}\n\n      async function navigate/);
  assert.ok(routeSync, 'expected route synchronization');
  assert.match(routeSync[1], /preserveAuthorizedHash: true/);
});

test('legacy Episode hash is redirected into the unified data-assets view', () => {
  assert.match(appSource, /requestedView === 'episodes' \? 'assets' : requestedView/);
  assert.match(appSource, /window\.location\.hash = '#\/assets'/);
});

test('data assets and datasets mount one independent catalog without the retired build view', () => {
  assert.match(appSource, /<data-catalog v-else-if="activeView === 'assets' \|\| activeView === 'datasets'"/);
  assert.match(appSource, /:mode="activeView" :locale="locale" :user="user" :workspaces="workspaces"/);
  assert.doesNotMatch(appSource, /<section v-else-if="activeView === 'assets'|activeView === 'buildData'/);
});

test('all Episode list entries share a read-only detail opener', () => {
  const opener = appSource.match(
    /async function openEpisodeDetail\(row, options = \{\}\) \{([\s\S]*?)\n      \}\n\n      function closeEpisodeDetail/,
  );
  assert.ok(opener, 'expected one shared Episode detail opener');
  assert.match(opener[1], /QuicDataEpisodeDetail\.normalizeContext/);
  assert.match(opener[1], /episodeDetailRequestGate\.begin/);
  assert.match(opener[1], /showEpisodeDrawer\.value = true/);
  assert.match(opener[1], /QuicDataAPI\.getEpisode\(/);
  assert.match(opener[1], /QuicDataAPI\.getEpisodePreviewUrl\(/);
  assert.match(opener[1], /episodeDetailRequestGate\.isCurrent/);
  assert.doesNotMatch(opener[1], /enterWorkbench|runWorkAction|workQueueAction/);
  assert.doesNotMatch(appSource, /function inspectEpisode\(/);
  assert.doesNotMatch(appSource, /function openEpisodeAssets\(/);
  assert.match(appSource, /function openWorkQueueEpisodeDetail\(/);
  assert.match(appSource, /@row-click="openWorkQueueEpisodeDetail"/);
  assert.match(appSource, /openEpisodeDetail\(row\.episode, \{ source: 'overview' \}\)/);
  assert.match(appSource, /openEpisodeDetail\(\{ id: row\.episode_id, episode_uid: row\.episode_uid \}, \{ source: 'batch' \}\)/);
  assert.doesNotMatch(appSource, /source: 'dataset-builder'/);
  assert.match(appSource, /@click\.stop="runWorkActionById\(/);
});

test('Episode detail loading keeps detail, preview, and raw download failures independent', () => {
  assert.match(appSource, /episodeDrawerLoading = reactive\(\{ detail: false, preview: false, rawSource: false \}\)/);
  assert.match(appSource, /episodeDrawerError = reactive\(\{ detail: '', preview: '' \}\)/);
  assert.match(appSource, /QuicDataEpisodeDetail\.mergeEpisodeDetail/);
  assert.match(appSource, /function retryEpisodeDetail\(/);
  assert.match(appSource, /function closeEpisodeDetail\(/);
  assert.match(appSource, /episodeDetailRequestGate\.invalidate\(\)/);
  assert.match(appSource, /async function switchWorkspace\(\) \{[\s\S]*?closeEpisodeDetail\(\)/);
  assert.match(appSource, /async function switchTaskSet\(\) \{[\s\S]*?closeEpisodeDetail\(\)/);
  assert.match(appSource, /async function signOut\(\) \{[\s\S]*?closeEpisodeDetail\(\)/);
  assert.match(appSource, /v-else-if="episodePreview\?\.available"/);
  assert.doesNotMatch(appSource, /v-else-if="!episodeDrawerError\.detail && episodePreview\?\.available"/);
  assert.match(
    appSource,
    /mergeEpisodeDetail\(\s*\{ episode: selectedEpisode\.value \},\s*next,?\s*\)/,
  );
});

test('shared Episode drawer is read-only and organized into three tabs', () => {
  const drawer = appSource.match(
    /<el-drawer v-model="showEpisodeDrawer"([\s\S]*?)<\/el-drawer>/,
  );
  assert.ok(drawer, 'expected the shared Episode detail drawer');
  assert.match(drawer[0], /class="episode-detail-drawer"/);
  assert.match(drawer[1], /<el-tabs[^>]*v-model="episodeDrawerTab"[^>]*class="episode-detail-tabs">/);
  assert.match(drawer[1], /<el-tab-pane[^>]*name="overview"/);
  assert.match(drawer[1], /<el-tab-pane[^>]*name="artifacts"/);
  assert.match(drawer[1], /<el-tab-pane[^>]*name="packet-metadata"/);
  assert.match(drawer[1], /episodeDrawerContext\?\.workItem/);
  assert.match(drawer[1], /episode-technical-collapse/);
  assert.doesNotMatch(drawer[1], /retryEpisodeQuality|runWorkAction|enterWorkbench|openWorkbench/);
  assert.doesNotMatch(appSource, /function retryEpisodeQuality\(/);
  assert.doesNotMatch(appSource, /const assetEpisode|const episodeAssets|const episodePacketMetadata/);
  assert.match(cssSource, /\.episode-detail-drawer/);
  assert.match(cssSource, /\.episode-detail-tabs/);
  assert.match(cssSource, /\.episode-detail-tabs > \.el-tabs__header \{[^}]*order: -1/);
  assert.match(cssSource, /\.episode-detail-grid/);
});

test('console maps quality state for users and permits only bounded local UI timers', () => {
  const intervalCalls = appSource.match(/(?:window\.)?setInterval\s*\(/g) || [];
  const timeoutCalls = appSource.match(/(?:window\.)?setTimeout\s*\(/g) || [];
  assert.match(appSource, /function qualityStatusLabel/);
  assert.match(appSource, /质检中/);
  assert.match(appSource, /质检通过/);
  assert.match(appSource, /已恢复，存在数据丢失/);
  assert.match(appSource, /质检失败/);
  assert.match(appSource, /quicdata_locale/);
  assert.equal(intervalCalls.length, 0);
  assert.ok(timeoutCalls.length <= 3, 'only bounded history, queue, and return-highlight timers remain');
  assert.match(appSource, /window\.setTimeout\(capture, WORKBENCH_HISTORY_DEBOUNCE_MS\)/);
  assert.match(workQueueSource, /function createRefreshCoordinator/);
  assert.match(indexSource, /\/css\/app\.css\?v=374/);
  assert.match(indexSource, /\/js\/api\.js\?v=\d+/);
  assert.doesNotMatch(indexSource, /src="\/js\/train-console\.js/);
  assert.match(indexSource, /\/js\/date-time\.js\?v=1[\s\S]*\/js\/table-layout-preference\.js\?v=1[\s\S]*\/js\/workbench-shortcuts\.js\?v=1[\s\S]*\/js\/qr-control-codes\.js\?v=1[\s\S]*\/js\/app-bootstrap\.js\?v=\d+/);
});

test('work queue uses stable WorkItem identities for fixed action columns', () => {
  const queueView = appSource.match(
    /<section v-else-if="activeView === 'work-queue'"([\s\S]*?)<\/section>\n            <\/template>/,
  );
  assert.ok(queueView, 'expected the work queue view');
  assert.match(queueView[1], /:row-key="queueRowKey"/);
  assert.match(queueView[1], /class="data-table work-queue-table"/);
  assert.match(queueView[1], /column-key="episode_uid"[\s\S]*tableColumnWidth\('work-queue', 'episode_uid', 190\)/);
  assert.match(queueView[1], /:label="t\('quality'\)" width="110"/);
  assert.match(queueView[1], /column-key="actions" :label="t\('status'\)" :width="tableColumnWidth\('work-queue', 'actions', 170\)"/);
  assert.doesNotMatch(queueView[1].match(/class="data-table work-queue-table"[\s\S]*?<\/el-table>/)[0], /fixed="right"/);
  assert.match(queueView[1], /:key="queueActionKey\(scope\.row, action\)"/);
  assert.match(queueView[1], /runWorkActionById\(scope\.row\.work_item\.id, action\)/);
  assert.match(appSource, /\['assigned', 'in_progress'\]\.includes\(item\.status\)/);
  assert.match(appSource, /const canResume = item\?\.status === 'in_progress'/);
  assert.match(appSource, /actions\.includes\('continue'\) \|\| canResume/);
  assert.match(appSource, /actions\.includes\('save_draft'\) \|\| actions\.includes\('submit'\) \|\| actions\.includes\('review'\)/);
  assert.match(appSource, /forceRelease && !note/);
});

test('work queue invalidations coalesce refreshes without clearing visible rows', () => {
  const loader = appSource.match(/async function loadWorkQueue\(\) \{([\s\S]*?)\n      \}\n\n      async function changeWorkQueuePage/);
  const subscription = appSource.match(/function subscribeWorkspaceQueue\(\) \{([\s\S]*?)\n      \}\n\n      function pickLatestScopeId/);
  assert.ok(loader, 'expected work queue loader');
  assert.ok(subscription, 'expected workspace queue subscription');
  assert.doesNotMatch(loader[1], /queueRows\.value = \[\]/);
  assert.doesNotMatch(loader[1], /queueTotal\.value = 0/);
  assert.match(appSource, /function scheduleWorkQueueRefresh\(/);
  assert.match(appSource, /QuicDataWorkQueue\.createRefreshCoordinator/);
  assert.match(workQueueSource, /let trailing = false/);
  assert.match(subscription[1], /scheduleWorkQueueRefresh\(\)/);
  assert.match(subscription[1], /refreshOnSubscribe: false/);
});

test('completed imports do not claim that their source Episodes are still quality checking', () => {
  assert.match(appSource, /importSucceeded: '导入完成'/);
  assert.match(appSource, /importSucceeded: 'Import complete'/);
  assert.doesNotMatch(appSource, /importSucceeded: '导入完成，质检中'/);
  assert.doesNotMatch(appSource, /importSucceeded: 'Imported, quality checking'/);
});

test('retired browser import console is replaced by the offline data package guide', () => {
  assert.doesNotMatch(appSource, /QuicDataAPI\.(listBatches|createImportSession|listBatchImportSessions|getBatchActivity|listImportCandidates|scanImportCandidates)/);
  assert.doesNotMatch(appSource, /function openImportDialog\(/);
  assert.doesNotMatch(appSource, /function startIntake\(/);
  assert.doesNotMatch(appSource, /function selectIntakeBatch\(/);
  assert.doesNotMatch(appSource, /js\/import-upload\.js/);
  assert.match(appSource, /const showIntakeGuide = ref\(false\)/);
  assert.match(appSource, /function openIntakeGuide\(/);
  assert.match(appSource, /intakeGuideStepUpload/);
  assert.match(appSource, /intakeGuideOfflineHint/);
});

test('mobile navigation keeps every primary entry visible without horizontal full-width buttons', () => {
  assert.match(
    cssSource,
    /\.sidebar-nav button, \.is-collapsed \.sidebar-nav button \{ flex: 0 0 38px; width: 38px;/,
  );
});

test('retired batch creation dialog is gone and the guide points at collection review', () => {
  assert.match(appSource, /description: '说明'/);
  assert.match(appSource, /description: 'Description'/);
  assert.match(appSource, /<el-form-item :label="t\('description'\)">/);
  assert.doesNotMatch(appSource, /<el-option :label="t\('sourceFilesystem'\)" value="filesystem_candidate" \/>/);
  assert.doesNotMatch(appSource, /<el-dialog v-model="showBatchDialog"/);
  assert.doesNotMatch(appSource, /<el-dialog v-model="showImportDialog"/);
  assert.doesNotMatch(appSource, /pipeline_key|source_type/);
  assert.match(appSource, /@click="showIntakeGuide = true"/);
});

test('collection review lists data packages and never opens a retired batch detail', () => {
  assert.doesNotMatch(appSource, /class="data-table batch-list-table"/);
  assert.doesNotMatch(appSource, /openBatchWithMiningMock\(batches\.value\[0\]\)/);
  // 数采审核 is the data package review queue: it owns the collected-data view.
  const batchesView = appSource.match(/<section v-else-if="activeView === 'batches'"([\s\S]*?)<section v-else-if="activeView === 'workbench'"/);
  assert.ok(batchesView, 'expected the collected-data view');
  assert.match(batchesView[1], /reviewPackageRowsFiltered/);
  assert.match(batchesView[1], /openDataPackageDrawer/);
  assert.match(batchesView[1], /openIntakeReview/);
  assert.match(batchesView[1], /enterIntakeReviewAction/);
  assert.match(appSource, /void loadReviewPackages\(\)/);
  assert.match(appSource, /data-package|listDataPackages/);
  assert.match(appSource, /function qualityProgressPercent\(/);
  assert.match(appSource, /reviewPackageCollectorName\(scope\.row\)/);
  assert.match(appSource, /reviewPackageDeviceName\(scope\.row\)/);
  assert.match(cssSource, /\.batch-progress-cell[^}]*grid-template-columns:\s*max-content minmax\(72px, 1fr\)/);
  assert.match(cssSource, /\.batch-progress-cell > span[^}]*white-space:\s*nowrap/);
  assert.ok((appSource.match(/:label="t\('collectionTask'\)"/g) || []).length >= 1);
  assert.match(appSource, /onReviewPackageSelectionChange/);
});

test('data intake is an operational flow distinct from batch management', () => {
  const intake = appSource.match(/<section v-else-if="activeView === 'intake'"([\s\S]*?)<section v-else-if="activeView === 'batches'"/);
  assert.ok(intake, 'expected a distinct intake section');
  assert.match(intake[1], /<data-overview/);
  const overview = readFileSync(new URL('../js/data-overview.js', import.meta.url), 'utf8');
  assert.match(overview, /intake-tabs-bar/);
  assert.match(overview, /name="packages"/);
  assert.match(overview, /name="batches"/);
  assert.match(intake[1], /openPackageBatchDialog/);
  assert.doesNotMatch(intake[1], /@row-click="openBatch"/);
  assert.match(appSource, /overviewImportRows/);
  assert.match(appSource, /builtImportBatches/);
  assert.doesNotMatch(appSource, /function selectIntakeBatch\(/);
});

test('collected-data review renders package collector and device evidence', () => {
  assert.match(appSource, /reviewPackageCollectorName/);
  assert.match(appSource, /reviewPackageDeviceName/);
  assert.match(appSource, /intake_valid_duration_hours/);
  assert.match(appSource, /formatAvailableDuration/);
});

test('collection resources and safe quality diagnostics are visible without exposing worker logs', () => {
  assert.match(appSource, /activeView === 'resources'/);
  assert.match(appSource, /collectionDevices/);
  assert.match(appSource, /collectorProfiles/);
  assert.match(appSource, /selectedEpisode\.quality_diagnostic/);
  assert.match(appSource, /qualityDiagnosticLabel/);
  assert.match(appSource, /reference_timeline_incomplete: t\('qualityReferenceTimelineIncomplete'\)/);
  assert.match(appSource, /metadata_invalid: t\('qualityMetadataInvalid'\)/);
  assert.doesNotMatch(appSource, /quality_diagnostic\.error_message/);
  assert.match(cssSource, /\.resource-directory-grid\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\)/);
  assert.match(cssSource, /\.resource-directory-grid \.table-panel\s*\{[^}]*overflow-x:\s*auto[^}]*overflow-y:\s*visible/);
});

test('Batch, Episode, and queue views render controlled task labels without metadata inference', () => {
  assert.match(appSource, /const episodeTaskLabelId = ref\(initialQueryNumber\('assets', 'task_label_id'\)\)/);
  assert.match(appSource, /taskLabelId: episodeTaskLabelId\.value \|\| ''/);
  assert.match(appSource, /task_label_id: request\.taskLabelId \|\| undefined/);
  assert.match(appSource, /scope\.row\.episode\.task_label\?\.name/);
  assert.doesNotMatch(appSource, /metadata_json\.task/);
});

test('Batch detail lists only source Episodes', () => {
  const batchDetail = appSource.match(
    /<section v-else-if="activeView === 'batches'"([\s\S]*?)<section v-else-if="activeView === 'workbench'"/,
  );
  assert.ok(batchDetail, 'expected the Batch detail section');
  assert.match(batchDetail[1], /episodesDetail/);
  assert.doesNotMatch(batchDetail[1], /miningStageSummaryText/);
  assert.doesNotMatch(batchDetail[1], /v-for="episode in episodes"/);
});

test('completed queue rows identify their human work type', () => {
  assert.match(appSource, /function workItemKindLabel\(/);
  assert.match(appSource, /t\('workType'\)/);
  assert.match(appSource, /workItemKindLabel\(scope\.row\.work_item\.kind\)/);
});

test('work queue ignores a stale response after switching stages', () => {
  const loadWorkQueue = appSource.match(
    /async function loadWorkQueue\(\) \{([\s\S]*?)\n      \}\n\n      async function switchWorkspace/,
  );
  assert.ok(loadWorkQueue, 'expected the work queue loader');
  assert.match(loadWorkQueue[1], /const requestedStage = queueStage\.value;/);
  assert.match(loadWorkQueue[1], /stage: requestedStage/);
  assert.match(loadWorkQueue[1], /if \(requestedStage !== queueStage\.value\) return;/);
});

test('work queue requests and renders server-backed pages beyond the first 50 items', () => {
  assert.match(appSource, /const queuePage = ref\(initialQueryNumber\('work-queue', 'page', 1\)\);/);
  assert.match(appSource, /const queuePageSize = ref\(initialQueryNumber\('work-queue', 'page_size', 50\)\);/);
  assert.match(appSource, /function resetWorkQueuePage\(\)/);
  assert.match(appSource, /QuicDataWorkQueue\.requestQuery\(request, WORK_QUEUE_PAGE_SIZES\)/);
  assert.match(workQueueSource, /offset: \(normalizedPage - 1\) \* normalizedPageSize/);
  assert.match(appSource, /function changeWorkQueuePage\(page\)/);
  assert.match(appSource, /function changeWorkQueuePageSize\(pageSize\)/);
  assert.match(appSource, /if \(matchesPage && !applyWorkQueueResponse\(snapshot, request\)\) \{\s*void scheduleWorkQueueRefresh\(\);/);
  assert.match(appSource, /async function switchWorkspace\(\) \{[\s\S]*?resetWorkQueuePage\(\)/);
  const switchTaskSet = appSource.match(
    /async function switchTaskSet\(\) \{([\s\S]*?)\n      \}\n\n      async function switchQueueStage/,
  );
  assert.ok(switchTaskSet, 'expected the task-set switch handler');
  assert.doesNotMatch(switchTaskSet[1], /resetWorkQueuePage|subscribeWorkspaceQueue|loadWorkQueue/);
  assert.match(appSource, /async function switchQueueStage\(stage\) \{[\s\S]*?resetWorkQueuePage\(\)/);

  const queueView = appSource.match(
    /<section v-else-if="activeView === 'work-queue'"([\s\S]*?)<\/section>\n            <\/template>/,
  );
  assert.ok(queueView, 'expected the work queue view');
  assert.match(queueView[1], /<el-pagination/);
  assert.match(queueView[1], /:current-page="queuePage"/);
  assert.match(queueView[1], /:page-size="queuePageSize"/);
  assert.match(queueView[1], /:total="queueTotal"/);
  assert.match(queueView[1], /@current-change="changeWorkQueuePage"/);
  assert.match(queueView[1], /@size-change="changeWorkQueuePageSize"/);
  assert.match(indexSource, /\/css\/app\.css\?v=374/);
  assert.match(indexSource, /\/js\/api\.js\?v=\d+/);
  assert.match(indexSource, /\/js\/app-bootstrap\.js\?v=\d+/);
});

test('queue stage selected from another view survives navigation', () => {
  assert.match(appSource, /function setAuthorizedView\(requestedView, \{[\s\S]*queueStageOverride/);
  assert.match(appSource, /setQueueStageValue\([\s\S]*queueStageOverride[\s\S]*queueStageInitialized \? queueStage\.value/);
  assert.match(appSource, /function navigate\(view, \{ queueStageOverride = null, refreshQueue = true \} = \{\}\)/);
  assert.match(appSource, /const nextQueueStageOverride = queueStageOverride[\s\S]*queueStageInitialized \? queueStage\.value/);
  assert.match(appSource, /queueStageOverride: nextQueueStageOverride,[\s\S]*preserveAuthorizedHash: view === 'work-queue'/);
  assert.match(appSource, /navigate\('work-queue', \{ queueStageOverride: stage \}\)/);
});

test('retired OSS candidate scan console is gone from the collected-data view', () => {
  assert.doesNotMatch(appSource, /const candidateDateRange = ref\(\[\]\)/);
  assert.doesNotMatch(appSource, /const selectedImportCandidateGroupKeys = ref\(\[\]\)/);
  assert.doesNotMatch(appSource, /QuicDataAPI\.listImportCandidates/);
  assert.doesNotMatch(appSource, /toggleImportCandidateSourceGroup/);
  assert.doesNotMatch(appSource, /@click="importSelectedCandidates"/);
  assert.match(appSource, /reviewPackageStatusOptions/);
  assert.match(appSource, /downloadTaskOfflineManifest/);
});

test('a user with no workspace can create one from the empty state and the scope toolbar', () => {
  assert.match(appSource, /t\('noWorkspace'\) \}\}<\/h1><el-button v-if="canManageWorkspace"[\s\S]*@click="showWorkspaceDialog = true"/);
  assert.match(appSource, /v-model="selectedWorkspaceId"[\s\S]*@click="showWorkspaceDialog = true"/);
  assert.match(appSource, /workspaceName'\)">/);
  assert.match(appSource, /workspaceName: '数采工作空间名称'/);
  assert.match(appSource, /workspaceName: 'Collection workspace name'/);
});
