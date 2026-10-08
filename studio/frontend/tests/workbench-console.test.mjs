import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const apiSource = readFileSync(new URL('../js/api.js', import.meta.url), 'utf8');
const appSource = readFileSync(new URL('../js/app.js', import.meta.url), 'utf8');
const cssSource = readFileSync(new URL('../css/app.css', import.meta.url), 'utf8');
const demoSource = readFileSync(new URL('../js/demo-data.js', import.meta.url), 'utf8');
const indexSource = readFileSync(new URL('../index.html', import.meta.url), 'utf8');
const workQueueSource = readFileSync(new URL('../js/work-queue.js', import.meta.url), 'utf8');
const shortcutsSource = readFileSync(new URL('../js/workbench-shortcuts.js', import.meta.url), 'utf8');

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

test('workbench API uses the authorized Batch/Episode contracts', async () => {
  const { api, calls } = loadApi([
    { code: 200, data: { token: 'access-token', userInfo: { id: 1 } } },
    { code: 200, data: { work_item: { id: 9 } } },
    { code: 200, data: { draft: { version: 2 } } },
    { code: 200, data: { start_ns: '1', end_ns: '2' } },
    { code: 200, data: { items: [] } },
  ]);

  assert.equal(typeof api.getEpisodeWorkbench, 'function');
  assert.equal(typeof api.saveWorkbenchDraft, 'function');
  assert.equal(typeof api.getEpisodeTimeline, 'function');
  assert.equal(typeof api.getEpisodeAssets, 'function');

  await api.login('admin@quicdata.local', 'password');
  await api.getEpisodeWorkbench(42, 9);
  await api.saveWorkbenchDraft(9, { base_version: 1, payload: { segments: [] } });
  await api.getEpisodeTimeline(42);
  await api.getEpisodeAssets(42);

  assert.equal(calls[1].url, 'http://localhost:8010/api/v1/episodes/42/workbench?work_item_id=9');
  assert.equal(calls[2].url, 'http://localhost:8010/api/v1/work-queue/items/9/draft');
  assert.equal(calls[2].options.method, 'PUT');
  assert.deepEqual(JSON.parse(calls[2].options.body), { base_version: 1, payload: { segments: [] } });
  assert.equal(calls[3].url, 'http://localhost:8010/api/v1/episodes/42/timeline');
  assert.equal(calls[4].url, 'http://localhost:8010/api/v1/episodes/42/assets');
});

test('work queue can enter a real workbench without interval polling', () => {
  assert.match(appSource, /VIEWS = new Set\(\[[^\]]*'workbench'/);
  assert.match(appSource, /function openWorkbench\(/);
  assert.match(appSource, /function enterWorkbench\(/);
  assert.match(appSource, /#\/workbench\//);
  assert.match(appSource, /activeView === 'workbench'/);
  assert.match(appSource, /getEpisodeWorkbench/);
  assert.match(appSource, /saveWorkbenchDraft/);
  assert.match(appSource, /getEpisodeTimeline/);
  const intervalCalls = appSource.match(/(?:window\.)?setInterval\s*\(/g) || [];
  const timeoutCalls = appSource.match(/(?:window\.)?setTimeout\s*\(/g) || [];
  assert.equal(intervalCalls.length, 0);
  assert.ok(timeoutCalls.length <= 3, 'only bounded history, queue, and return-highlight timers remain');
  assert.match(appSource, /window\.setTimeout\(capture, WORKBENCH_HISTORY_DEBOUNCE_MS\)/);
  assert.match(workQueueSource, /function createRefreshCoordinator/);
  assert.match(indexSource, /\/js\/workbench-shortcuts\.js\?v=1[\s\S]*\/js\/workbench-save\.js\?v=1[\s\S]*\/js\/episode-detail\.js\?v=1[\s\S]*\/js\/app-bootstrap\.js\?v=\d+/);
  assert.doesNotMatch(indexSource, /dataset-revision-builder\.js/);
});

test('leaving a workbench or another console view preserves its queue stage', () => {
  assert.match(appSource, /let queueStageInitialized = initialRoute\.view === 'work-queue'/);
  assert.match(appSource, /nextView === 'work-queue' && \(previousView !== 'work-queue' \|\| !queueStageInitialized\)/);
  assert.match(appSource, /const nextQueueStageOverride = queueStageOverride\s*\|\|\s*\(\s*view === 'work-queue' && queueStageInitialized \? queueStage\.value : null\s*\)/);
  assert.match(appSource, /queueStageInitialized = true/);
  assert.match(appSource, /queueStageOverride: nextQueueStageOverride,[\s\S]*preserveAuthorizedHash: view === 'work-queue'/);
});

test('sidebar queue stages push a complete route without falling back to cut', () => {
  const routeSync = appSource.match(/function syncListRouteState\([\s\S]*?\n      \}/);
  assert.ok(routeSync, 'expected list route synchronization');
  assert.match(routeSync[0], /\{ push = false \} = \{\}/);
  assert.match(routeSync[0], /if \(push && window\.location\.hash !== hash\)/);
  assert.match(routeSync[0], /window\.location\.hash = hash/);

  const openStage = appSource.match(/function openQueueStage\(stage\) \{[\s\S]*?\n      \}/);
  assert.ok(openStage, 'expected sidebar stage navigation');
  assert.match(openStage[0], /queueStatus\.value = ''/);
  assert.match(openStage[0], /navigate\('work-queue', \{ queueStageOverride: stage \}\)/);

  const navigation = appSource.match(/async function navigate\(view,[\s\S]*?\n      \}/);
  assert.ok(navigation, 'expected console navigation');
  assert.match(navigation[0], /preserveAuthorizedHash: view === 'work-queue'/);
  assert.match(navigation[0], /syncListRouteState\(\{ push: true \}\)/);

  const replaced = [];
  const context = {
    URLSearchParams,
    activeView: { value: 'work-queue' },
    queueStage: { value: 'annotation' },
    queuePage: { value: 1 },
    queuePageSize: { value: 50 },
    queueSortBy: { value: '' },
    queueSortOrder: { value: '' },
    queueTaskSetId: { value: '' },
    queueTaskLabelId: { value: '' },
    queueStatus: { value: '' },
    queueEpisodeKeyword: { value: '' },
    queueUpdatedRange: { value: [] },
    normalizedWorkQueuePage: (value) => value,
    normalizedWorkQueuePageSize: (value) => value,
    window: {
      location: { hash: '#/work-queue?stage=cut&page=3&page_size=50' },
      history: { replaceState(...args) { replaced.push(args); } },
    },
  };
  const syncListRouteState = vm.runInNewContext(`(${routeSync[0]})`, context);

  assert.equal(syncListRouteState({ push: true }), true);
  assert.equal(context.window.location.hash, '#/work-queue?stage=annotation&page=1&page_size=50');
  assert.equal(syncListRouteState({ push: true }), false);
  assert.equal(replaced.length, 1);
});

test('switching work items discards a dirty draft even when draft versions match', () => {
  const helperSource = appSource.match(
    /function shouldReplaceWorkbenchDraft\([\s\S]*?\n  \}/,
  );
  assert.ok(helperSource, 'expected a work-item-aware draft replacement helper');

  const shouldReplaceWorkbenchDraft = vm.runInNewContext(`(${helperSource[0]})`);
  const cutSnapshot = { work_item: { id: 3 }, draft: { version: 1 } };
  const annotationSnapshot = { work_item: { id: 5 }, draft: { version: 1 } };

  assert.equal(
    shouldReplaceWorkbenchDraft(cutSnapshot, annotationSnapshot, true),
    true,
  );
  assert.equal(
    shouldReplaceWorkbenchDraft(annotationSnapshot, annotationSnapshot, true),
    false,
  );
  assert.equal(
    shouldReplaceWorkbenchDraft(
      annotationSnapshot,
      { work_item: { id: 5 }, draft: { version: 2 } },
      true,
    ),
    false,
  );
});

test('cut workbench uses one global editor timeline without automatic partitioning', () => {
  assert.match(appSource, /class="cut-timeline-track"/);
  assert.match(appSource, /class="timeline-ruler"/);
  assert.match(appSource, /v-for="tick in timelineTicks\(\)"/);
  assert.doesNotMatch(appSource, /class="cut-local-timeline-track"/);
  assert.doesNotMatch(appSource, /v-for="boundary in cutLocalBoundaries\(\)"/);
  assert.match(appSource, /class="cut-boundary"/);
  assert.match(appSource, /startCutBoundaryDrag/);
  assert.match(appSource, /dragBoundary/);
  assert.match(appSource, /cutDraftSaveValid/);
  assert.match(appSource, /cutDraftSubmitValid/);
  assert.match(appSource, /is-too-long/);
  assert.match(appSource, /addCutBoundary/);
  assert.match(appSource, /deleteSelectedCutBoundary/);
  assert.match(appSource, /restoreSelectedQrBoundary/);
  assert.match(appSource, /setCutEligibility/);
  assert.match(appSource, /QuicDataCutWorkbench\.initialize/);
  assert.doesNotMatch(appSource, /resetAutomaticCut/);
  assert.doesNotMatch(appSource, /autoPartition/);
  assert.doesNotMatch(appSource, /partitionBounds/);
  assert.match(appSource, /playback_timeline/);
  assert.match(appSource, /QuicDataPreviewTimeline\.sourceTimestampAt/);
  assert.match(appSource, /payload\.mode = workbenchDraft\.value\.mode/);
  assert.match(appSource, /@click="selectCutSegment\((?:index|row\.index), \{ seek: true \}\)"/);
  assert.match(appSource, /if \(seek\) seekWorkbenchToTimestamp\(workbenchDraft\.value\.segments\[normalized\]\.start_ns\)/);

  const cutSection = appSource.match(/<section v-if="activeWorkbench\.capabilities\?\.cut"([\s\S]*?)<section v-else-if="activeWorkbench\.capabilities\?\.annotation"/);
  assert.ok(cutSection, 'expected a dedicated cut workbench section');
  assert.match(cutSection[1], /cut-boundary-origin/);
  assert.match(appSource, /cutSegmentBoundaryLabel/);
  assert.doesNotMatch(cutSection[1], />V ·/);
  assert.doesNotMatch(cutSection[1], />X ·/);
  assert.match(cutSection[1], /cut-segment-eligibility/);
  assert.doesNotMatch(cutSection[1], /cut-shortcut-hint/);
  assert.match(appSource, /v-if="activeWorkbench\.capabilities\?\.cut"[\s\S]*timelineIcon\('add'\)/);
  assert.match(appSource, /v-if="activeWorkbench\.capabilities\?\.cut"[\s\S]*timelineIcon\('trash'\)/);
  assert.doesNotMatch(cutSection[1], /v-model="segment\.(?:start_ns|end_ns)"/);
  assert.doesNotMatch(cutSection[1], /setCutBoundary/);
  assert.match(appSource, /activeWorkbench\.review_target\.payload\?\.segments[\s\S]*segment\.eligibility/);
  assert.match(appSource, /exclusionReasonLabel/);
});

test('cut workbench uses a fixed 4:3 preview frame without cropping and supports safe keyboard operation', () => {
  assert.match(appSource, /class="workbench-video-stage"/);
  assert.match(appSource, /@loadedmetadata="syncWorkbenchPlayhead"/);
  assert.doesNotMatch(appSource, /workbenchVideoAspect/);
  assert.match(cssSource, /\.workbench-video-stage[^}]*aspect-ratio:\s*4\s*\/\s*3/);
  assert.match(cssSource, /\.workbench-video-stage[^}]*width:\s*min\(100cqw,\s*133\.333cqh\)/);
  assert.match(cssSource, /\.workbench-video[^}]*object-fit:\s*contain/);
  assert.match(shortcutsSource, /id: 'cut-add-boundary'[\s\S]*key: 'b'/);
  assert.match(shortcutsSource, /id: 'cut-restore-qr'[\s\S]*key: 'r'/);
  assert.match(appSource, /function restoreSelectedQrBoundary\(\)[\s\S]*selectedCutBoundary\.value === null/);
  assert.match(shortcutsSource, /id: 'cut-include'[\s\S]*key: 'v'/);
  assert.match(shortcutsSource, /id: 'cut-exclude'[\s\S]*key: 'x'/);
  assert.match(shortcutsSource, /id: 'cut-delete-boundary'[\s\S]*key: 'Delete'/);
  assert.match(shortcutsSource, /id: 'cut-select-previous'[\s\S]*key: 'ArrowUp'/);
  assert.match(shortcutsSource, /id: 'cut-select-next'[\s\S]*key: 'ArrowDown'/);
  assert.match(shortcutsSource, /id: 'step-backward'[\s\S]*key: 'ArrowLeft'/);
  assert.match(shortcutsSource, /id: 'step-forward'[\s\S]*key: 'ArrowRight'/);
  assert.match(appSource, /QuicDataWorkbenchShortcuts\.shouldIgnoreTarget\(event\.target\)/);
});

test('an assignee can re-enter an in-progress work item after leaving the workbench', () => {
  const canEnter = appSource.match(/function canEnterWorkbench\(row\) \{([\s\S]*?)\n      \}\n\n      function visibleQueueActions/);
  assert.ok(canEnter, 'expected the workbench entry guard');
  assert.match(canEnter[1], /item\?\.status === 'in_progress'/);
  assert.match(canEnter[1], /actions\.includes\('save_draft'\)/);
  assert.match(canEnter[1], /actions\.includes\('submit'\)/);
  assert.match(canEnter[1], /actions\.includes\('review'\)/);
});

test('batch import history uses a dedicated fixed actions column', () => {
  const importHistory = appSource.match(/<section class="surface-panel table-panel"><div class="panel-heading"><div><h2>\{\{ t\('episodesDetail'\) \}\}<\/h2>[\s\S]*?<\/section>/);
  assert.ok(importHistory, 'expected the batch import history table');
  assert.doesNotMatch(importHistory[0], /:label="t\('actions'\)" fixed="right"/);
  assert.doesNotMatch(importHistory[0], /t\('importState'\)/);
  assert.doesNotMatch(importHistory[0], /t\('manualReview'\)/);
  assert.doesNotMatch(importHistory[0], /t\('stageSplitStatus'\)/);
  assert.doesNotMatch(importHistory[0], /t\('qcStatus'\)/);
  assert.doesNotMatch(importHistory[0], /t\('importState'\)/);
  assert.doesNotMatch(importHistory[0], /<el-table-column :label="t\('status'\)" width="180"/);
});

test('Vue templates expose every authentication action from setup', () => {
  const returnBlock = appSource.match(/\n      return \{([\s\S]*?)\n      \};\n    \},\n    template:/);
  assert.ok(returnBlock, 'expected the Vue setup return block');

  for (const handler of [
    'signIn',
    'cancelPasswordChange',
    'submitPasswordChange',
  ]) {
    assert.match(appSource, new RegExp(`[@]\\w+(?:\\.\\w+)*="${handler}"`));
    assert.match(returnBlock[1], new RegExp(`\\b${handler}\\b`));
  }
  assert.match(appSource, /<el-dropdown[^>]*@command="handleAccountCommand"/);
  assert.match(appSource, /command="change-password"/);
  assert.match(appSource, /command="sign-out"/);
  assert.match(appSource, /function handleAccountCommand\(command\)[\s\S]*openChangePassword\(\)[\s\S]*signOut\(\)/);
  for (const handler of ['signOut', 'openChangePassword', 'handleAccountCommand']) {
    assert.match(returnBlock[1], new RegExp(`\\b${handler}\\b`));
  }
});

test('annotation workbench uses AI projections without reviving old EGO routes', () => {
  for (const method of [
    'getEpisodeAiSuggestions',
    'createEpisodeAiSuggestions',
    'retryEpisodeAiSuggestions',
  ]) {
    assert.match(apiSource, new RegExp(`\\b${method}\\s*\\(`));
  }
  assert.match(apiSource, /\/ai-suggestions/);
  assert.match(appSource, /activeWorkbench\.capabilities\?\.annotation/);
  assert.match(appSource, /aiSuggestions/);
  assert.match(appSource, /applyAiSuggestion/);
  assert.match(appSource, /:disabled="!aiSuggestions\.capability\?\.eligible"/);
  assert.doesNotMatch(appSource, /data-view="ego"/);
  assert.doesNotMatch(appSource, /\/ego\//);
});

test('annotation workbench uses layered editor timeline controls and safe shortcuts', () => {
  assert.doesNotMatch(appSource, /class="workbench-video" controls/);
  assert.match(appSource, /class="surface-panel studio-timeline"/);
  assert.match(appSource, /--workbench-editor-width': workbenchEditorWidth \+ 'px'/);
  assert.match(appSource, /class="workbench-split-handle"/);
  assert.match(appSource, /function startWorkbenchSplitResize/);
  assert.match(cssSource, /grid-template-columns:\s*minmax\(360px, 1fr\) 10px minmax\(320px, var\(--workbench-editor-width, 420px\)\)/);
  assert.match(cssSource, /\.workbench-split-handle::before/);
  assert.match(cssSource, /\.workbench-media, \.workbench-editor[^}]*height:\s*calc\(100dvh - var\(--timeline-panel-height, 160px\) - 148px\)/);
  assert.match(appSource, /activeWorkbench\.capabilities\?\.annotation[\s\S]*class="annotation-window"/);
  assert.match(appSource, /annotationDetailOpen/);
  assert.match(appSource, /annotation-detail-drawer/);
  assert.match(appSource, /<label class="annotation-description" @click\.stop>/);
  assert.match(appSource, /toggleAnnotationDetail/);
  assert.match(appSource, /selectAnnotationSegment\(index\)/);
  assert.match(cssSource, /\.annotation-segment-list[^}]*flex:\s*1 1 auto/);
  assert.match(cssSource, /\.annotation-detail-body[^}]*max-height:\s*min\(42vh, 360px\)/);
  const annotationDrawerKeyframes = cssSource.match(/@keyframes annotationDrawerIn \{ from \{[^}]+\} to \{[^}]+\} \}/);
  assert.ok(annotationDrawerKeyframes, 'expected annotation drawer keyframes');
  assert.match(annotationDrawerKeyframes[0], /transform: translateY\(14px\)/);
  assert.doesNotMatch(annotationDrawerKeyframes[0], /max-height/);
  assert.doesNotMatch(appSource, /custom-media-controls/);
  assert.doesNotMatch(appSource, /class="cut-local-adjustment"/);
  assert.match(appSource, /class="timeline-ruler"/);
  assert.match(appSource, /@click="seekWorkbenchFromTimelineEvent\(\$event, \$event\.currentTarget\.parentElement\)"/);
  assert.doesNotMatch(appSource, /<h2>\{\{ t\('timeline'\) \}\}<\/h2>/);
  assert.doesNotMatch(appSource, /\{\{ t\('currentPlayhead'\) \}\}/);
  assert.match(appSource, /class="timeline-timecode"/);
  assert.match(appSource, /class="cut-progress-line"/);
  assert.match(appSource, /class="cut-playhead-pin"/);
  assert.match(appSource, /function timelineTicks/);
  assert.match(appSource, /timelineTickStep/);
  assert.match(appSource, /class="timeline-icon-button"/);
  assert.match(appSource, /stepWorkbenchPlayback\(-1, 'seconds5'\)/);
  assert.match(appSource, /stepWorkbenchPlayback\(-1, 'seconds1'\)/);
  assert.match(appSource, /stepWorkbenchPlayback\(1, 'seconds1'\)/);
  assert.match(appSource, /stepWorkbenchPlayback\(1, 'seconds5'\)/);
  assert.match(appSource, /function startPlayheadDrag/);
  assert.match(appSource, /class="cut-playhead-pin"[\s\S]*@pointerdown\.stop\.prevent="startPlayheadDrag"/);
  assert.match(appSource, /class="cut-timeline-canvas"/);
  assert.match(appSource, /visibleCutTrackWindows/);
  assert.match(appSource, /visibleCutList/);
  assert.match(appSource, /function applyPlayheadDom/);
  assert.match(appSource, /function scheduleCutTimelinePaint/);
  assert.match(cssSource, /\.cut-window-list-spacer/);
  assert.match(cssSource, /\.cut-timeline-canvas/);
  assert.match(appSource, /const deltaNs = BigInt\(Math\.round\(\(amount === 'seconds5' \? 5 : 1\) \* 1e9\)\)/);
  assert.match(appSource, /seekWorkbenchToTimestamp\(next\.toString\(\)\)/);
  assert.match(shortcutsSource, /id: 'shuttle-backward'[\s\S]*key: 'j'/);
  assert.match(shortcutsSource, /id: 'shuttle-pause'[\s\S]*key: 'k'/);
  assert.match(shortcutsSource, /id: 'shuttle-forward'[\s\S]*key: 'l'/);
  assert.match(shortcutsSource, /id: 'boundary-previous'[\s\S]*key: '\['/);
  assert.match(shortcutsSource, /id: 'boundary-next'[\s\S]*key: '\]'/);
  assert.match(shortcutsSource, /id: 'timeline-zoom-in'[\s\S]*key: '\+'/);
  assert.match(shortcutsSource, /id: 'timeline-snap'[\s\S]*key: 's'/);
  assert.match(appSource, /timelineSnapEnabled\.value = !timelineSnapEnabled\.value/);
  assert.match(appSource, /event\.shiftKey[\s\S]*event\.currentTarget\.scrollLeft/);
  assert.match(appSource, /class="timeline-volume-popover"/);
  assert.match(cssSource, /\.timeline-volume-popover[\s\S]*opacity: 0/);
  assert.match(cssSource, /\.timeline-volume-control:hover \.timeline-volume-popover/);
  assert.match(cssSource, /\.timeline-hover-preview \{ position: fixed; z-index: 80/);
  assert.match(cssSource, /\.timeline-scroll-viewport[^}]*min-height:\s*86px/);
  assert.match(appSource, /const anchorRatio = \(viewport\.scrollLeft \+ anchorX\) \/ Math\.max\(1, viewport\.scrollWidth\)/);
  assert.match(appSource, /viewport\.scrollLeft = Math\.max\(0, anchorRatio \* viewport\.scrollWidth - anchorX\)/);
  assert.match(appSource, /function handleWorkbenchShortcut/);
  assert.match(appSource, /QuicDataWorkbenchShortcuts\.resolve\(event, activeWorkbench\.value\.capabilities \|\| \{\}\)/);
  assert.match(shortcutsSource, /event\.isComposing/);
  assert.match(shortcutsSource, /id: 'annotation-set-start'[\s\S]*key: 'i'/);
  assert.match(shortcutsSource, /id: 'annotation-set-end'[\s\S]*key: 'o'/);
  assert.match(shortcutsSource, /id: 'annotation-add-segment'[\s\S]*key: 'n'/);
  assert.match(shortcutsSource, /id: 'play-toggle'[\s\S]*key: ' '/);
  assert.match(shortcutsSource, /INPUT', 'TEXTAREA', 'SELECT/);
});

test('annotation and review use a source workspace scope without collection-project controls', () => {
  const header = appSource.match(/<header class="console-header">([\s\S]*?)<\/header>/);
  assert.ok(header, 'expected the global console header');
  assert.match(header[1], /activeView === 'work-queue'/);
  assert.doesNotMatch(header[1], /v-model="selectedWorkspaceId"|v-model="selectedCollectionProjectId"|t\('sourceWorkspace'\)|t\('collectProject'\)/);
  assert.match(appSource, /class="page-actions work-queue-scope-actions"[\s\S]*?t\('sourceWorkspace'\)[\s\S]*?queueStageOptions[\s\S]*?loadWorkQueue/);
  assert.doesNotMatch(header[1], /settingsQuickCreate|handleSettingsCreateCommand/);
  const collectionScopeBindings = appSource.match(/class="page-actions collection-scope-actions"[\s\S]*?v-model="miningProjectScope"[\s\S]*?multiple/g) || [];
  assert.equal(collectionScopeBindings.length, 3);
  assert.match(appSource, /<collection-overview[^>]*>\s*<template #scope>[\s\S]*?v-model="selectedWorkspaceId"[\s\S]*?<\/template>\s*<\/collection-overview>/);
  const scope = appSource.match(/<div[^>]*class="scope-toolbar page-scope-toolbar"([\s\S]*?)<\/div>/);
  assert.ok(scope, 'expected a page-level scope toolbar');
  assert.match(scope[1], /v-model="selectedWorkspaceId"/);
  assert.match(appSource, /activeView === 'overview'/);
});

test('overview renders the tenant dashboard without synthetic production data', () => {
  assert.match(appSource, /class="metric-grid is-dashboard"/);
  assert.match(appSource, /function dashboardScope\(\)/);
  assert.match(appSource, /QuicDataAPI\.getDashboardOverview\(dashboardScope\(\)\)/);
  assert.match(appSource, /QuicDataAPI\.refreshDashboard\(scope\)/);
  assert.match(appSource, /kpiTotalDuration: 'Episode 总时长（含派生）'/);
  assert.match(appSource, /await loadDashboardOverview\(\{ manageLoading: false, generation: requestGeneration, workspaceId, scope \}\)/);
  assert.match(appSource, /receipt\.payload && QuicDataDashboardState\.isFreshSnapshot\(receipt\.payload, receipt\)/);
  assert.match(appSource, /const assetView = activeView\.value === 'assets'/);
  assert.match(appSource, /batchId: assetView \? null/);
  assert.match(appSource, /batch_id: request\.batchId \|\| undefined/);
  assert.match(appSource, /function workQueueQuery/);
  assert.match(appSource, /taskSetId: queueTaskSetId\.value \|\| null/);
  assert.match(workQueueSource, /task_set_id: snapshot\.taskSetId \|\| undefined/);
  assert.match(appSource, /function dashboardChartInstance\(/);
  assert.match(appSource, /dashboardTodayQueues/);
  assert.match(appSource, /todayQueuesFromFunnel/);
  assert.match(appSource, /async function scheduleDashboardCharts\(/);
  assert.match(appSource, /watch\(activeView,/);
  assert.match(appSource, /disposeDashboardCharts\(\)/);
  assert.match(appSource, /durationUnknown: '未知'/);
  assert.match(appSource, /const bucketKeys = \['lt_30s', 'bt_30_60s', 'gt_60s', 'unknown'\]/);
  assert.doesNotMatch(appSource, /function buildDashboardMockOverview/);
  const headerTitle = appSource.match(/<div class="header-title">([\s\S]*?)<\/div>/);
  assert.ok(headerTitle);
  assert.doesNotMatch(headerTitle[1], /currentTaskSet/);
});

test('destructive row actions are bordered buttons and tags share one typography rule', () => {
  assert.match(appSource, /plain size="small" type="danger"[^>]*revokeWorkspaceMember/);
  assert.match(appSource, /plain size="small" :type="scope\.row\.is_active \? 'danger' : 'primary'"/);
  assert.doesNotMatch(appSource, /el-button[^>]*link[^>]*type="danger"/);
  assert.match(cssSource, /\.el-tag\s*\{[^}]*font-size:\s*12px !important;[^}]*font-weight:\s*600/);
  assert.match(cssSource, /\.el-button\.el-button--primary:not\(\.is-link\):not\(\.is-plain\)[^}]*color:\s*#fff/);
});

test('local demo keeps draft writes in memory without changing the production API path', () => {
  assert.match(apiSource, /\['localhost', '127\.0\.0\.1'\]/);
  assert.match(apiSource, /new URLSearchParams\(window\.location\.search/);
  assert.match(apiSource, /get\('demo'\) === '1'/);
  assert.match(apiSource, /enableDemoMode\(\)/);
  assert.match(apiSource, /if \(!localPreviewHost \|\| typeof QuicDataDemo === 'undefined'\) return false/);
  assert.match(apiSource, /method === 'GET'[\s\S]*QuicDataDemo\.read\(path, params\)[\s\S]*QuicDataDemo\.write\(method, path, body\)/);
  assert.match(apiSource, /const base = '\/api\/v1'/);
  // The workbench projection exposes no queue actions; only the queue rows do.
  assert.match(demoSource, /kind: 'cut'[^\n]*available_actions: \[\]/);
  assert.match(demoSource, /kind: 'annotation'[^\n]*available_actions: \[\]/);
  assert.match(demoSource, /kind: 'review'[^\n]*available_actions: \[\]/);
  assert.match(appSource, /本地演示/);
  assert.match(appSource, /class="demo-entry-button"[^>]*@click="enterLocalDemo"/);
  assert.match(demoSource, /https:\/\/vjs\.zencdn\.net\/v\/oceans\.mp4/);
  assert.match(demoSource, /duration_s:\s*125/);
  assert.match(demoSource, /encoded_duration_s:\s*46\.612/);
  assert.match(demoSource, /reference_frame_count:\s*126/);
  assert.match(demoSource, /const demoDrafts = new Map\(\)/);
  assert.match(demoSource, /method === 'PUT' && draftMatch/);
  assert.doesNotMatch(appSource, /quicdata_demo_workbench_draft/);
  assert.match(demoSource, /role: 'admin'/);
  assert.match(demoSource, /permissions: \['\*'\]/);
  assert.match(demoSource, /path === '\/auth\/users'/);
  assert.match(demoSource, /path === '\/platform-settings'/);
  assert.match(demoSource, /path === '\/batches'/);
  assert.match(demoSource, /path === '\/datasets'/);
  assert.match(demoSource, /path === '\/datasets\/701\/revision-candidates'/);
  assert.match(demoSource, /path === '\/dashboard\/overview'/);
  assert.match(demoSource, /\/batches\\\/\\d\+\\\/imports/);
  for (const stage of ['cut', 'annotation', 'review', 'completed']) {
    assert.match(demoSource, new RegExp(`${stage}: \\[`));
    assert.match(demoSource, /queueRow\(episode, \{ id: 9001, kind: 'cut'/);
    assert.match(demoSource, /queueRow\(derivedClip, \{ id: 9002, kind: 'annotation'/);
    assert.match(demoSource, /queueRow\(derivedClip, \{ id: 9003, kind: 'review'/);
  }
  // Queue rows mirror the backend projections so the workbench entry renders.
  assert.match(demoSource, /id: 9001, kind: 'cut'[^\n]*available_actions: \['save_draft', 'submit', 'release'\]/);
  assert.match(demoSource, /id: 9002, kind: 'annotation'[^\n]*available_actions: \['save_draft', 'submit', 'release'\]/);
  assert.match(demoSource, /id: 9003, kind: 'review'[^\n]*available_actions: \['review'\]/);
  assert.match(appSource, /function enterLocalDemo\(\)/);
  assert.match(appSource, /demoMode\.value = true/);
  assert.match(appSource, /const preserveAuthorizedRoute = \(/);
  assert.match(appSource, /activeView\.value === 'workbench'/);
  assert.match(appSource, /preserveAuthorizedHash: preserveAuthorizedRoute/);
});

test('normal entry does not enable local preview demo while explicit demo parameter does', () => {
  function makeApiContext(search = '', hostname = 'localhost') {
    const context = {
      window: {
        location: {
          hostname,
          origin: `http://${hostname}:8090`,
          search,
        },
      },
      URLSearchParams,
    };
    context.globalThis = context;
    vm.runInNewContext(`${apiSource}\n;globalThis.__api = QuicDataAPI;`, context);
    return context.__api;
  }

  const normalApi = makeApiContext('');
  assert.equal(normalApi.isDemoMode(), false);
  assert.equal(normalApi.isLocalPreview(), false);

  const demoApi = makeApiContext('?demo=1');
  assert.equal(demoApi.isDemoMode(), true);
  assert.equal(demoApi.isLocalPreview(), true);

  const mockApi = makeApiContext('?mock=1');
  assert.equal(mockApi.isDemoMode(), true);
  assert.equal(mockApi.isLocalPreview(), true);

  const prodApi = makeApiContext('?demo=1', 'quicdata.com');
  assert.equal(prodApi.isDemoMode(), false);
  assert.equal(prodApi.isLocalPreview(), false);
});

test('deep-linked workbenches synchronize the queue navigation stage', () => {
  assert.match(appSource, /\['cut', 'annotation', 'review'\]\.includes\(snapshot\?\.work_item\?\.kind\)/);
  assert.match(appSource, /setQueueStageValue\(snapshot\.work_item\.kind\)/);
});

test('workbench submit and review refresh only their current work queue page', () => {
  const submit = appSource.match(/async function submitWorkbenchDraft\(\) \{([\s\S]*?)\n      \}\n\n      async function submitReviewDecision/);
  const review = appSource.match(/async function submitReviewDecision\(decision\) \{([\s\S]*?)\n      \}\n\n      async function releaseWorkbench/);
  assert.ok(submit, 'expected workbench submit action');
  assert.ok(review, 'expected workbench review action');
  assert.doesNotMatch(submit[1], /Promise\.all\(\[loadWorkQueue\(\), loadEpisodes\(\)\]\)/);
  assert.doesNotMatch(review[1], /Promise\.all\(\[loadWorkQueue\(\), loadEpisodes\(\)\]\)/);
  assert.doesNotMatch(submit[1], /loadEpisodes\(\)/);
  assert.doesNotMatch(review[1], /loadEpisodes\(\)/);
  assert.match(submit[1], /applyWorkQueueMutation\(result\.work_item\)/);
  assert.match(review[1], /applyWorkQueueMutation\(result\.work_item\)/);
  assert.match(submit[1], /scheduleWorkQueueRefresh\(\)/);
  assert.match(review[1], /scheduleWorkQueueRefresh\(\)/);
  assert.match(submit[1], /navigate\('work-queue', \{ refreshQueue: false \}\)/);
  assert.match(review[1], /navigate\('work-queue', \{ refreshQueue: false \}\)/);
});

test('sidebar uses icons and animates between expanded and collapsed states', () => {
  assert.match(appSource, /const sidebarIcons = \{/);
  assert.match(appSource, /class="nav-mark" v-html="sidebarIcons\['work-queue'\]"/);
  assert.match(appSource, /class="nav-mark" v-html="sidebarIcons\.settings"/);
  assert.doesNotMatch(appSource, /<span class="nav-mark">[OIBWRFDAS]<\/span>/);
  assert.match(appSource, /class="sidebar-nav-group"/);
  assert.match(appSource, /workbenchQueueOpen/);
  assert.match(appSource, /function openQueueStage\(stage\)/);
  assert.match(appSource, /setQueueStageValue\(stage\);[\s\S]*navigate\('work-queue'\)/);
  assert.doesNotMatch(appSource, /<el-tabs :model-value="queueStage"/);
  assert.match(appSource, /class="sidebar-collapse"[^>]*@click="toggleSidebar"/);
  assert.match(cssSource, /transition:\s*grid-template-columns \.4s/);
  assert.match(cssSource, /\.is-collapsed \.sidebar-collapse svg[^}]*rotate\(180deg\)/);
  assert.match(cssSource, /\.sidebar-nav button:active[^}]*scale\(\.97\)/);
  assert.match(cssSource, /\.is-collapsed \.sidebar-nav button[^}]*gap:\s*0/);
  assert.match(cssSource, /\.is-collapsed \.sidebar-nav button span:not\(\.nav-mark\)[^}]*display:\s*none/);
});

test('console header remains fixed while page content scrolls', () => {
  assert.match(cssSource, /\.console-header[^}]*position:\s*sticky/);
  assert.match(cssSource, /\.console-header[^}]*top:\s*0/);
  assert.match(cssSource, /\.console-header[^}]*z-index:\s*30/);
});

test('all workbench kinds share a resizable zoomable bottom timeline with preview', () => {
  assert.match(appSource, /class="timeline-resize-handle"/);
  assert.match(appSource, /@wheel="handleTimelineWheel"/);
  assert.match(appSource, /timelineZoom/);
  assert.match(appSource, /timelineWheelZoomDebt/);
  assert.match(appSource, /Math\.abs\(timelineWheelZoomDebt\) < threshold/);
  assert.match(appSource, /class="timeline-hover-preview"/);
  assert.match(appSource, /ref="timelineHoverVideo"/);
  assert.match(appSource, /activeWorkbench\.capabilities\?\.review/);
  assert.match(appSource, /activeWorkbench\.capabilities\?\.annotation/);
  assert.match(cssSource, /\.studio-timeline/);
  assert.match(cssSource, /\.studio-timeline[^}]*position:\s*fixed/);
  assert.match(cssSource, /\.studio-timeline[^}]*bottom:\s*0/);
  assert.match(cssSource, /\.timeline-scroll-viewport[^}]*overflow-x:\s*auto/);
  assert.match(appSource, /Math\.abs\(event\.deltaX\) > Math\.abs\(event\.deltaY\)/);
});

test('workbench edit history supports copy paste undo and redo without hijacking text inputs', () => {
  assert.match(appSource, /function undoWorkbench\(/);
  assert.match(appSource, /function redoWorkbench\(/);
  assert.match(appSource, /function copyWorkbenchSelection\(/);
  assert.match(appSource, /function pasteWorkbenchSelection\(/);
  assert.match(shortcutsSource, /id: 'history-undo'[\s\S]*key: 'z', command: true/);
  assert.match(shortcutsSource, /id: 'history-redo'[\s\S]*key: 'y', command: true/);
  assert.match(shortcutsSource, /id: 'annotation-copy'[\s\S]*key: 'c', command: true/);
  assert.match(shortcutsSource, /id: 'annotation-paste'[\s\S]*key: 'v', command: true/);
  assert.match(appSource, /QuicDataWorkbenchShortcuts\.shouldIgnoreTarget\(event\.target\)/);
});

test('dense workbench edits debounce full-draft history snapshots without weakening undo', () => {
  assert.match(appSource, /const WORKBENCH_HISTORY_DEBOUNCE_MS = 250/);
  assert.match(appSource, /function scheduleWorkbenchHistory\(/);
  assert.match(appSource, /function flushWorkbenchHistory\(/);
  const markDirty = appSource.match(/function markWorkbenchDraftDirty\([\s\S]*?\n      \}/);
  assert.ok(markDirty, 'expected dirty draft marker');
  assert.match(markDirty[0], /workbenchDraft\.value\.segments\?\.length \|\| 0\) > CUT_TIMELINE_DOM_LIMIT/);
  assert.match(markDirty[0], /scheduleWorkbenchHistory\(\)/);
  const undo = appSource.match(/function undoWorkbench\([\s\S]*?\n      \}/);
  const redo = appSource.match(/function redoWorkbench\([\s\S]*?\n      \}/);
  assert.ok(undo && redo, 'expected undo and redo helpers');
  assert.match(undo[0], /flushWorkbenchHistory\(\)/);
  assert.match(redo[0], /flushWorkbenchHistory\(\)/);
});

test('dense timeline canvas paints only the binary-searched visible window', () => {
  const paint = appSource.match(/function paintCutTimelineCanvas\([\s\S]*?\n      \}/);
  assert.ok(paint, 'expected dense timeline canvas painter');
  assert.match(paint[0], /const visibleRange = timelineVisibleNsRange\(\)/);
  assert.match(paint[0], /QuicDataCutWorkbench\.overlappingRange\(/);
  assert.match(paint[0], /for \(let index = startIndex; index < endIndex; index \+= 1\)/);
});

test('workbench autosave is debounced, serialized, and does not reload media state', () => {
  assert.match(appSource, /let workbenchSaveCoordinator = null/);
  assert.match(appSource, /QuicDataWorkbenchSave\.create\(/);
  assert.match(appSource, /debounceMs:\s*5000/);
  assert.match(appSource, /maxWaitMs:\s*30000/);
  const markDirty = appSource.match(/function markWorkbenchDraftDirty\([\s\S]*?\n      \}/);
  assert.ok(markDirty, 'expected the workbench dirty marker');
  assert.match(markDirty[0], /workbenchSaveCoordinator\?\.markDirty\(\)/);
  assert.match(appSource, /await workbenchSaveCoordinator\.flush\(\{ drain: true, reason: 'manual' \}\)/);
  assert.match(appSource, /const savedItem = result\?\.work_item\?\.work_item/);
  assert.match(appSource, /QuicDataRealtime\.acknowledge\(/);
  const persistence = appSource.match(/async function persistWorkbenchDraft\([\s\S]*?\n      \}/);
  assert.ok(persistence, 'expected isolated draft persistence');
  assert.doesNotMatch(persistence[0], /normalizedWorkbenchDraft|loadWorkbench/);
  const capture = appSource.match(/function captureWorkbenchDraft\([\s\S]*?\n      \}/);
  assert.ok(capture, 'expected draft capture guard');
  assert.match(capture[0], /draggingCutBoundary \|\| draggingAnnotationBoundary \|\| draggingAnnotationSegment/);
});

test('an invalidated workbench ignores stale actions from an in-flight draft save', () => {
  const applyResult = appSource.match(/function applyWorkbenchSaveResult\([\s\S]*?\n      \}/);
  assert.ok(applyResult, 'expected the draft save response handler');
  assert.match(
    applyResult[0],
    /const savedItemPatch = workbenchInvalidationNotified \? \{\} : \(savedItem \|\| \{\}\)/,
  );
  assert.match(applyResult[0], /work_item: \{ \.\.\.current\.work_item, \.\.\.savedItemPatch, \.\.\.versionPatch \}/);
});

test('workbench realtime events subscribe without snapshots and never trigger a full workbench reload', () => {
  const subscribe = appSource.match(/function subscribeWorkbench\(snapshot\) \{([\s\S]*?)\n      \}\n\n      async function loadWorkbench/);
  assert.ok(subscribe, 'expected workbench subscriptions');
  assert.match(subscribe[1], /refreshOnSubscribe:\s*false/);
  assert.match(subscribe[1], /refreshOnReconnect:\s*false/);
  assert.match(subscribe[1], /handleWorkbenchItemUpdate/);
  assert.match(subscribe[1], /handleWorkbenchEpisodeUpdate/);
  assert.doesNotMatch(subscribe[1], /loadWorkbench|getEpisodeWorkbench|getEpisode\(/);
});

test('cut and annotation editors expose manual draft save controls', () => {
  assert.match(appSource, /activeWorkbench\.capabilities\?\.cut[\s\S]*saveWorkbenchDraft/);
  assert.match(appSource, /activeWorkbench\.capabilities\?\.annotation[\s\S]*saveWorkbenchDraft/);
  assert.match(appSource, /demoMode \|\| activeWorkbench\.work_item\.available_actions\?\.includes\('save_draft'\)/);
});

test('new annotation segments are long enough to drag by default', () => {
  assert.match(appSource, /const defaultDuration = 5000000000n/);
  assert.match(appSource, /endNs = start !== null \? start \+ defaultDuration : null/);
  assert.match(appSource, /startNs = endNs - defaultDuration > bounds\.start \? endNs - defaultDuration : bounds\.start/);
});

test('v2.1 workbench keeps partial save state and distinguishes draft from submission completeness', () => {
  assert.match(appSource, /draft: \{ \.\.\.current\.draft, \.\.\.\(result\?\.draft \|\| \{\}\) \}/);
  assert.match(appSource, /work_item: \{ \.\.\.current\.work_item, \.\.\.savedItemPatch, \.\.\.versionPatch \}/);
  assert.match(appSource, /function annotationDraftSaveValid\(/);
  assert.match(appSource, /function annotationDraftSubmitValid\(/);
  assert.match(appSource, /payload\.note = String\(workbenchDraft\.value\.note \|\| ''\)\.trim\(\)/);
  assert.match(appSource, /activeWorkbench\.capabilities\?\.annotation \? !annotationDraftSaveValid\(\)/);
  assert.match(appSource, /activeWorkbench\.capabilities\?\.annotation \? !annotationDraftSubmitValid\(\)/);
  const annotationSubmitValid = appSource.match(/function annotationDraftSubmitValid\([\s\S]*?\n      \}/);
  assert.ok(annotationSubmitValid, 'expected annotation submit validation');
  assert.doesNotMatch(annotationSubmitValid[0], /workbenchNoteComplete/);
  const submitWorkbench = appSource.match(/async function submitWorkbenchDraft\([\s\S]*?\n      \}/);
  assert.ok(submitWorkbench, 'expected workbench submission handler');
  assert.doesNotMatch(submitWorkbench[0], /workbenchNoteComplete/);
  assert.match(submitWorkbench[0], /deferWorkbenchItemEvents/);
  assert.match(submitWorkbench[0], /acknowledgeWorkbenchItemEvent/);
  assert.match(appSource, /function deferWorkbenchItemEvents\([\s\S]*?QuicDataRealtime\.deferEvents/);
  assert.match(appSource, /function workbenchItemRealtimeVersion\([\s\S]*?work_item_realtime_version/);
});

test('v2.1 workbench keeps navigation and annotation interactions tied to the selected segment start', () => {
  const reviewSelector = appSource.match(/function selectReviewSegment\([\s\S]*?\n      \}/);
  assert.ok(reviewSelector, 'expected review segment selection helper');
  assert.match(reviewSelector[0], /seekWorkbenchToTimestamp\(segments\[normalized\]\.start_ns\)/);
  assert.match(appSource, /function startAnnotationSegmentDrag\(/);
  assert.match(appSource, /function stopAnnotationSegmentDrag\(/);
  assert.match(appSource, /@pointerdown\.stop\.prevent="startAnnotationSegmentDrag\(item\.index, \$event\)"/);
  assert.match(appSource, /timelineHover\.left =/);
  assert.match(appSource, /class="timeline-hover-line"/);
  assert.match(appSource, /class="workbench-list-note"/);
  assert.match(cssSource, /\.workbench-list-note/);
});

test('v2.1 keeps cut editing free of copy paste controls and defers annotation drag persistence', () => {
  assert.doesNotMatch(appSource, /activeWorkbench\.capabilities\?\.cut \? selectedCutBoundary === null/);
  assert.match(appSource, /<el-tooltip v-if="activeWorkbench\.capabilities\?\.annotation" :content="t\('copy'\)/);
  const dragStop = appSource.match(/function stopAnnotationSegmentDrag\([\s\S]*?\n      \}/);
  assert.ok(dragStop, 'expected annotation segment drag stop helper');
  assert.match(dragStop[0], /markWorkbenchDraftDirty\(\)/);
  assert.doesNotMatch(dragStop[0], /saveWorkbenchDraft\(/);
});

test('dense annotation timelines virtualize DOM windows and keep canvas selections reachable', () => {
  const densePredicate = appSource.match(/function shouldPaintCutTimelineCanvas\([\s\S]*?\n      \}/);
  assert.ok(densePredicate, 'expected dense timeline render predicate');
  assert.match(densePredicate[0], /return cutTimelineVisibleCount\(\) > CUT_TIMELINE_DOM_LIMIT/);
  assert.doesNotMatch(densePredicate[0], /capabilities\?\.annotation\) return false/);
  assert.match(appSource, /function annotationSegmentIndexAtPointer\(/);
  assert.match(appSource, /QuicDataCutWorkbench\.indexContaining\(workbenchDraft\.value\.segments/);
  const annotationTrack = appSource.match(/<template v-else-if="activeWorkbench\.capabilities\?\.annotation">([\s\S]*?)<\/template>/);
  assert.ok(annotationTrack, 'expected annotation timeline template');
  assert.match(annotationTrack[1], /v-for="item in visibleCutTrackWindows"/);
  assert.doesNotMatch(annotationTrack[1], /v-for="\(segment, index\) in workbenchDraft\.segments"/);
  const annotationEditor = appSource.match(/<section v-else-if="activeWorkbench\.capabilities\?\.annotation" class="surface-panel workbench-editor">([\s\S]*?)<section v-else-if="activeWorkbench\.capabilities\?\.review"/);
  assert.ok(annotationEditor, 'expected annotation editor template');
  assert.match(annotationEditor[1], /ref="annotationListViewport"/);
  assert.match(annotationEditor[1], /@scroll\.passive="onAnnotationListScroll"/);
  assert.match(annotationEditor[1], /v-for="row in visibleAnnotationList\.items"/);
  assert.doesNotMatch(annotationEditor[1], /v-for="\(segment, index\) in workbenchDraft\.segments"/);
  assert.match(appSource, /function scrollAnnotationListToIndex\(/);
  assert.match(cssSource, /\.annotation-virtual-list \{/);
  assert.match(cssSource, /\.review-segment-row \{ content-visibility: auto;/);
});

test('dense tables keep compact cells while preserving horizontal access to resized columns', () => {
  assert.match(appSource, /class="data-table work-queue-table" fit @header-dragend=/);
  assert.match(cssSource, /\.table-panel[^}]*overflow-x:\s*auto/);
  assert.match(cssSource, /\.work-queue-table, \.import-candidate-table[^}]*min-width:\s*1080px/);
  assert.match(cssSource, /\.dataset-catalog-table \{ min-width: 980px; \}/);
  assert.match(cssSource, /\.data-table[^}]*table-layout:\s*fixed/);
  assert.match(cssSource, /\.data-table \.cell[^}]*text-overflow:\s*ellipsis/);
  assert.match(cssSource, /\.data-table \.el-table th\.el-table__cell, \.data-table \.el-table td\.el-table__cell[^}]*padding:\s*7px 0/);
});

test('annotation and review rely on automatic collection attribution', () => {
  const payload = appSource.match(/function workbenchDraftPayload\([\s\S]*?\n      \}/);
  assert.ok(payload, 'expected the workbench draft payload builder');
  assert.doesNotMatch(payload[0], /payload\.collector_profile_id/);
  assert.doesNotMatch(payload[0], /payload\.collection_device_id/);
  const annotationEditor = appSource.match(/<section v-else-if="activeWorkbench\.capabilities\?\.annotation" class="surface-panel workbench-editor">([\s\S]*?)<section v-else-if="activeWorkbench\.capabilities\?\.review"/);
  assert.ok(annotationEditor, 'expected annotation editor template');
  assert.doesNotMatch(annotationEditor[1], /workbenchDraft\.collector_profile_id/);
  assert.doesNotMatch(annotationEditor[1], /workbenchDraft\.collection_device_id/);
  assert.doesNotMatch(appSource, /reviewForm\.device_choice/);
  assert.doesNotMatch(appSource, /reviewForm\.collector_choice/);
  assert.doesNotMatch(appSource, /body\.collection_device_id/);
  assert.doesNotMatch(appSource, /body\.collector_profile_id/);
  assert.doesNotMatch(appSource, /v-model="reviewForm\.device_choice"/);
  assert.doesNotMatch(appSource, /v-model="reviewForm\.collector_choice"/);
  assert.match(appSource, /v-model="workbenchDraft\.rating"/);
  assert.match(appSource, /v-model="reviewForm\.rating"/);
  assert.match(appSource, /function outcomeLabel\(/);
  assert.match(appSource, /outcomeLabel\(activeWorkbench\.review_target\.payload\?\.outcome\)/);
  assert.match(appSource, /reviewDetailOpen/);
  assert.match(appSource, /review-detail-drawer/);
  assert.match(appSource, /toggleReviewDetail/);
  assert.match(appSource, /selectReviewSegment\(index\)/);
  assert.match(appSource, /@click\.stop="selectReviewSegment\((?:index|item\.index)\)"/);
  assert.match(cssSource, /\.review-segment-list[^}]*flex:\s*1 1 auto/);
  assert.match(cssSource, /\.review-detail-body[^}]*max-height:\s*min\(42vh, 360px\)/);
});

test('annotation marks clip content required while notes remain optional', () => {
  assert.match(appSource, /class="required-mark"[^>]*>\*<\/span>/);
  assert.match(appSource, /segmentContent/);
  assert.doesNotMatch(appSource, /annotationNote[^\n]*required-mark/);
});

test('review and asset views consume only safe Episode projections', () => {
  assert.match(appSource, /VIEWS = new Set\(\[[^\]]*'assets'/);
  assert.match(appSource, /data-view="assets"/);
  assert.match(appSource, /activeWorkbench\.capabilities\?\.review/);
  assert.match(appSource, /review_target/);
  assert.match(appSource, /function submitReviewDecision\(/);
  assert.match(appSource, /function openEpisodeDetail\(/);
  assert.doesNotMatch(appSource, /function openEpisodeAssets\(/);
  assert.match(apiSource, /getEpisodeAssets/);
  assert.doesNotMatch(appSource, /storage_uri/);
  assert.doesNotMatch(appSource, /checksum_sha256/);
});

test('retired batch activity feed is absent and collection review owns package status', async () => {
  const { api, calls } = loadApi([
    { code: 200, data: { token: 'access-token', userInfo: { id: 1 } } },
    { code: 200, data: { items: [], total: 0, limit: 100 } },
  ]);

  assert.equal(typeof api.getBatchActivity, 'undefined');
  await api.login('admin@quicdata.local', 'password');

  assert.equal(calls.length, 1); // only the login request remains
  assert.match(appSource, /reviewPackageRowsFiltered/);
  assert.match(appSource, /dataPackageStatusLabel/);
  assert.doesNotMatch(appSource, /QuicDataAPI\.getBatchActivity/);
  assert.match(appSource, /async function loadBatchActivity\(\) \{\s*batchActivity\.value = \[\];/);
  assert.doesNotMatch(appSource, /storage_uri/);
  assert.doesNotMatch(appSource, /checksum_sha256/);
});
