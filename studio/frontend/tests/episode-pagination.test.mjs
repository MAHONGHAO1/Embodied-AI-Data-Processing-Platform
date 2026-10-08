import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { performance } from 'node:perf_hooks';
import test from 'node:test';
import vm from 'node:vm';

const appSource = readFileSync(new URL('../js/app.js', import.meta.url), 'utf8');
const indexSource = readFileSync(new URL('../index.html', import.meta.url), 'utf8');
const paginationSource = readFileSync(new URL('../js/work-queue.js', import.meta.url), 'utf8');

function loadPagination() {
  const context = {};
  context.globalThis = context;
  vm.runInNewContext(
    `${paginationSource}\n;globalThis.__pagination = QuicDataWorkQueue;`,
    context,
    { filename: 'work-queue.js' },
  );
  return context.__pagination;
}

test('shared pagination helper normalizes page requests and bounded responses', () => {
  const pagination = loadPagination();
  assert.deepEqual(
    JSON.parse(JSON.stringify(pagination.pageQuery({ page: 3, pageSize: 50 }))),
    { limit: 50, offset: 100 },
  );
  assert.deepEqual(
    JSON.parse(JSON.stringify(pagination.pageResult({ items: [{ id: 1 }], total: 101 }, 3, 50))),
    { items: [{ id: 1 }], total: 101, page: 3, pageSize: 50, lastPage: 3, needsReload: false },
  );
  assert.equal(pagination.pageResult({ items: [], total: 50 }, 2, 50).needsReload, true);
});

test('work queue helper owns request snapshots, queries, and current-request comparisons', () => {
  const queue = loadPagination();
  const request = queue.requestSnapshot({
    workspaceId: 3,
    taskSetId: 8,
    stage: 'annotation',
    page: '2',
    pageSize: '100',
  }, [50, 100]);

  assert.deepEqual(JSON.parse(JSON.stringify(request)), {
    workspaceId: 3,
    taskSetId: 8,
    stage: 'annotation',
    page: 2,
    pageSize: 100,
  });
  assert.deepEqual(JSON.parse(JSON.stringify(queue.requestQuery(request, [50, 100]))), {
    workspace_id: 3,
    task_set_id: 8,
    stage: 'annotation',
    limit: 100,
    offset: 100,
  });
  assert.equal(queue.sameRequest(request, { ...request }), true);
  assert.equal(queue.sameRequest(request, { ...request, stage: 'review' }), false);
});

test('200 visible work items are patched locally without changing order or scanning server pages', () => {
  const queue = loadPagination();
  const rows = Array.from({ length: 200 }, (_, index) => ({
    episode: { id: index + 1 },
    work_item: { id: index + 1, kind: 'review', status: 'in_progress' },
  }));
  const startedAt = performance.now();
  const updated = queue.applyActionRow(
    rows,
    200,
    { episode: { id: 100 }, work_item: { id: 100, kind: 'review', status: 'accepted' } },
    'review',
  );
  const elapsedMs = performance.now() - startedAt;

  assert.equal(updated.items.length, 199);
  assert.equal(updated.total, 199);
  assert.equal(updated.items[98].work_item.id, 99);
  assert.equal(updated.items[99].work_item.id, 101);
  assert.equal(elapsedMs < 100, true, `local queue patch took ${elapsedMs.toFixed(1)}ms`);
});

test('terminal reviews leave review while rejected annotations remain claimable rework', () => {
  const queue = loadPagination();
  assert.equal(queue.rowMatchesStage({
    work_item: { id: 1, kind: 'review', review_target_kind: 'annotation', status: 'rejected' },
  }, 'review'), false);
  assert.equal(queue.rowMatchesStage({
    work_item: { id: 2, kind: 'annotation', status: 'rejected' },
  }, 'annotation'), true);
  assert.equal(queue.rowMatchesStage({
    work_item: { id: 3, kind: 'review', review_target_kind: 'annotation', status: 'accepted' },
  }, 'completed'), true);
  assert.equal(queue.rowMatchesStage({
    work_item: { id: 4, kind: 'review', review_target_kind: 'cut', status: 'accepted' },
  }, 'completed'), false);
});

test('work queue renders product labels for rework and publication progress', () => {
  assert.match(appSource, /function workItemStatusLabel\(status\)/);
  assert.match(appSource, /rejected: t\('workNeedsRework'\)/);
  assert.match(appSource, /function publicationJobStatusLabel\(status\)/);
  assert.match(appSource, /queued: t\('publicationQueued'\)/);
  assert.match(appSource, /publicationJobStatusLabel\(scope\.row\.publication\?\.status\)/);
});

test('a 200-item review batch can be applied locally without issuing 200 refreshes', () => {
  const queue = loadPagination();
  let rows = Array.from({ length: 200 }, (_, index) => ({
    episode: { id: index + 1 },
    work_item: { id: index + 1, kind: 'review', status: 'in_progress' },
  }));
  let total = rows.length;
  const startedAt = performance.now();
  for (let id = 1; id <= 200; id += 1) {
    const result = queue.applyActionRow(
      rows,
      total,
      { episode: { id }, work_item: { id, kind: 'review', status: 'accepted' } },
      'review',
    );
    rows = result.items;
    total = result.total;
  }
  const elapsedMs = performance.now() - startedAt;
  assert.equal(rows.length, 0);
  assert.equal(total, 0);
  assert.equal(elapsedMs < 100, true, `review batch patch took ${elapsedMs.toFixed(1)}ms`);
});

test('refresh coordinator coalesces notification bursts into one refresh plus one trailing refresh', async () => {
  const queue = loadPagination();
  const timers = [];
  let firstResolve;
  let refreshCount = 0;
  const coordinator = queue.createRefreshCoordinator({
    refresh() {
      refreshCount += 1;
      if (refreshCount === 1) return new Promise((resolve) => { firstResolve = resolve; });
      return Promise.resolve();
    },
    setTimer(callback) { timers.push(callback); return timers.length; },
    clearTimer() {},
    delayMs: 250,
  });

  for (let index = 0; index < 200; index += 1) coordinator.schedule();
  assert.equal(timers.length, 1);
  timers.shift()();
  await Promise.resolve();
  assert.equal(refreshCount, 1);

  coordinator.schedule();
  coordinator.schedule();
  firstResolve();
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(refreshCount, 2);
});

test('catalog asset requests carry bounded server pagination rather than legacy Episode totals', () => {
  const catalogSource = readFileSync(new URL('../js/data-catalog.js', import.meta.url), 'utf8');
  assert.match(indexSource, /\/js\/page-components\.js/);
  assert.match(catalogSource, /limit: boundedLimit/);
  assert.match(catalogSource, /offset: \(boundedPage - 1\) \* boundedLimit/);
  assert.match(catalogSource, /source\.total/);
});

test('submitting and reviewing patch the visible page before a coalesced reconciliation', () => {
  const submit = appSource.match(
    /async function submitWorkbenchDraft\(\) \{([\s\S]*?)\n      \}\n\n      async function submitReviewDecision/,
  );
  const review = appSource.match(
    /async function submitReviewDecision\(decision\) \{([\s\S]*?)\n      \}\n\n      async function releaseWorkbench/,
  );
  assert.ok(submit);
  assert.ok(review);
  assert.doesNotMatch(submit[1], /loadEpisodes\(\)/);
  assert.doesNotMatch(review[1], /loadEpisodes\(\)/);
  assert.match(submit[1], /applyWorkQueueMutation\(result\.work_item\)/);
  assert.match(review[1], /applyWorkQueueMutation\(result\.work_item\)/);
  assert.match(submit[1], /navigate\('work-queue', \{ refreshQueue: false \}\)/);
  assert.match(review[1], /navigate\('work-queue', \{ refreshQueue: false \}\)/);
  assert.match(submit[1], /scheduleWorkQueueRefresh\(\)/);
  assert.match(review[1], /scheduleWorkQueueRefresh\(\)/);
});
