import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const appSource = readFileSync(new URL('../js/app.js', import.meta.url), 'utf8');
const apiSource = readFileSync(new URL('../js/api.js', import.meta.url), 'utf8');
const queueSource = readFileSync(new URL('../js/work-queue.js', import.meta.url), 'utf8');

function loadQueueHelpers() {
  const context = {};
  context.globalThis = context;
  vm.runInNewContext(`${queueSource}\n;globalThis.__queue = QuicDataWorkQueue;`, context);
  return context.__queue;
}

test('work queue request identity includes filters and stable server sorting', () => {
  const queue = loadQueueHelpers();
  const request = queue.requestSnapshot({
    workspaceId: 3,
    taskSetId: 9,
    taskLabelId: 7,
    status: 'pending',
    assigneeUserId: 5,
    updatedFrom: '2026-08-01T00:00:00Z',
    updatedTo: '2026-08-24T23:59:59Z',
    episodeKeyword: 'drv29',
    sortBy: 'updated_at',
    sortOrder: 'desc',
    stage: 'review',
    reviewTargetKind: 'annotation',
    page: 2,
    pageSize: 100,
  }, [50, 100]);

  assert.deepEqual(JSON.parse(JSON.stringify(request)), {
    workspaceId: 3,
    taskSetId: 9,
    taskLabelId: 7,
    status: 'pending',
    assigneeUserId: 5,
    updatedFrom: '2026-08-01T00:00:00Z',
    updatedTo: '2026-08-24T23:59:59Z',
    episodeKeyword: 'drv29',
    sortBy: 'updated_at',
    sortOrder: 'desc',
    stage: 'review',
    reviewTargetKind: 'annotation',
    page: 2,
    pageSize: 100,
  });
  assert.deepEqual(JSON.parse(JSON.stringify(queue.requestQuery(request, [50, 100]))), {
    workspace_id: 3,
    task_set_id: 9,
    task_label_id: 7,
    status: 'pending',
    assignee_user_id: 5,
    updated_from: '2026-08-01T00:00:00Z',
    updated_to: '2026-08-24T23:59:59Z',
    episode_keyword: 'drv29',
    sort_by: 'updated_at',
    sort_order: 'desc',
    stage: 'review',
    review_target_kind: 'annotation',
    limit: 100,
    offset: 100,
  });
  assert.equal(queue.sameRequest(request, { ...request, status: 'assigned' }), false);
  assert.equal(queue.sameRequest(request, { ...request, episodeKeyword: 'drv30' }), false);
});

test('review target kind is omitted outside the review stage', () => {
  const queue = loadQueueHelpers();
  const request = queue.requestSnapshot({
    workspaceId: 3,
    stage: 'cut',
    reviewTargetKind: 'annotation',
    page: 1,
    pageSize: 50,
  }, [50, 100]);

  assert.equal(request.reviewTargetKind, undefined);
  assert.equal(queue.requestQuery(request, [50, 100]).review_target_kind, undefined);
  assert.equal(queue.sameRequest(
    { ...request, stage: 'review', reviewTargetKind: 'cut' },
    { ...request, stage: 'review', reviewTargetKind: 'annotation' },
  ), false);
});

test('legacy review target URL state remains compatible without a cut/annotation selector in the batch review queue', () => {
  assert.match(appSource, /const queueReviewTargetKind = ref\(/);
  assert.match(appSource, /reviewTargetKind: queueStage\.value === 'review'/);
  assert.match(appSource, /add\('review_target_kind', queueStage\.value === 'review'/);
  assert.match(
    appSource,
    /queueReviewTargetKind\.value = queueStage\.value === 'review'[\s\S]*?includes\(query\.review_target_kind\)/,
  );
  // Plan 4 reviews assigned data-batch work items by default, while the
  // Episode work-item queue stays reachable through the explicit queue mode.
  assert.doesNotMatch(appSource, /v-model="queueReviewTargetKind"/);
  assert.match(appSource, /const governanceQueueActive = computed\(\(\) => \(/);
  assert.match(appSource, /queueStage\.value === 'annotation' \|\| queueStage\.value === 'review'/);
  assert.match(
    appSource,
    /if \(governanceQueueActive\.value\) \{[\s\S]*?await loadAnnotationWorkItems\(\);[\s\S]*?await loadReviewWorkItems\(\);/,
  );
  assert.match(appSource, /key: 'review_target_kind'/);
  assert.match(appSource, /queueReviewTargetKind\.value = '';/);
  assert.match(appSource, /if \(key === 'review_target_kind'\) queueReviewTargetKind\.value = '';/);
  assert.match(appSource, /if \(stage !== 'review'\) queueReviewTargetKind\.value = '';/);
  assert.match(appSource, /queueReviewTargetKind, queueUpdatedRange/);
});

test('every queue stage transition clears an inapplicable review target through one setter', () => {
  assert.match(
    appSource,
    /function setQueueStageValue\(stage\) \{[\s\S]*?queueStage\.value = stage;[\s\S]*?if \(stage !== 'review'\) queueReviewTargetKind\.value = '';/,
  );
  assert.equal((appSource.match(/queueStage\.value\s*=(?!=)/g) || []).length, 1);
});

test('catalog screens own their requests and no longer preload legacy workspace lists', () => {
  assert.match(appSource, /<data-catalog[^>]*:mode="activeView"/);
  const boot = appSource.match(/async function loadAuthorizedConsoleData\(\) \{([\s\S]*?)\n      \}/)[1];
  assert.doesNotMatch(boot, /loadDataAssets|loadCatalogDatasets|loadEpisodes|loadDatasets/);
  const catalogSource = readFileSync(new URL('../js/data-catalog.js', import.meta.url), 'utf8');
  assert.match(catalogSource, /source_workspace_id/);
  assert.doesNotMatch(catalogSource, /currentTaskSet|task_set_id/);
});

test('work queue task-set filtering is explicit and tables expose operation-time sorting', () => {
  assert.match(appSource, /const queueTaskSetId = ref\(/);
  assert.match(appSource, /v-model="queueTaskSetId"/);
  assert.match(appSource, /@sort-change="changeWorkQueueSort"/);
  assert.match(appSource, /prop="updated_at"[\s\S]*sortable="custom"/);
  assert.match(appSource, /work_item\.updated_at/);
});

test('switching the top task set does not silently rescope workspace lists', () => {
  const match = appSource.match(/async function switchTaskSet\(\) \{([\s\S]*?)\n      \}\n\n      async function switchQueueStage/);
  assert.ok(match);
  assert.doesNotMatch(match[1], /loadWorkQueue\(/);
  assert.doesNotMatch(match[1], /loadDatasets\(/);
  assert.doesNotMatch(match[1], /loadEpisodes\(/);
});

test('collection tasks, package review and overview own their server project filters', () => {
  // The task list retains its shared mining filter; paginated review uses an ID filter.
  assert.match(appSource, /const miningProjectScope = computed\(\{/);
  assert.match(appSource, /get: \(\) => miningDashFilters\.project/);
  assert.match(
    appSource,
    /set: \(value\) => \{ miningDashFilters\.project = Array\.isArray\(value\) \? value : \[\]; \}/,
  );
  assert.match(appSource, /const miningProjectOptions = computed\(\(\) => \{/);
  assert.match(
    appSource,
    /miningProjectScope\.value\.length && !miningProjectScope\.value\.includes\(String\(row\.tags\?\.project \|\| ''\)\)/,
  );
  assert.match(appSource, /const selectedCollectionProjectIds = computed/);
  // Tasks, package review and resources share the multi-project scope; the overview keeps its own project filter.
  const bindings = appSource.match(/v-model="miningProjectScope"/g) || [];
  assert.equal(bindings.length, 3);
  assert.match(appSource, /params\.collection_project_id = selectedCollectionProjectIds\.value/);
  assert.match(appSource, /const params = \{ workspace_id: workspaceId, page, size: 50 \}/);
  assert.match(appSource, /<collection-overview[^>]*:workspace-id="selectedWorkspaceId"[^>]*:projects="collectionProjects"/);
  assert.match(appSource, /miningDashFilters\.project = \[\];/);
});
