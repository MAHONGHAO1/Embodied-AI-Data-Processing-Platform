import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../js/mining-console.js', import.meta.url), 'utf8');
const utilsSource = readFileSync(new URL('../js/mining-utils.js', import.meta.url), 'utf8');
const appSource = readFileSync(new URL('../js/app.js', import.meta.url), 'utf8');

function loadConsole() {
  const context = {};
  context.globalThis = context;
  vm.runInNewContext(`${utilsSource}\n${source}\n;globalThis.__mining = QuicDataMining;`, context);
  return context.__mining;
}

function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

test('taskProgress aggregates batches and computes the delivery gap', () => {
  const mining = loadConsole();
  const task = {
    target_episodes: 1000,
    batches: [
      { target: 100, raw: 108, checked: 108, valid: 96 },
      { target: 100, raw: 96, checked: 96, valid: 88 },
    ],
  };
  const progress = plain(mining.taskProgress(task));
  assert.equal(progress.target, 1000);
  assert.equal(progress.raw, 204);
  assert.equal(progress.checked, 204);
  assert.equal(progress.valid, 184);
  assert.equal(progress.gap, 816);
  assert.equal(progress.progress, 0.184);
  assert.ok(Math.abs(progress.valid_rate - 184 / 204) < 1e-9);
});

test('taskProgress tolerates empty tasks', () => {
  const mining = loadConsole();
  const progress = plain(mining.taskProgress({}));
  assert.deepEqual(progress, {
    target: 0, raw: 0, checked: 0, valid: 0, valid_rate: 0, gap: 0, progress: 0,
    completed_batches: 0, batch_count: 0,
  });
});

test('splitPlan distributes the remainder across the first batches', () => {
  const mining = loadConsole();
  const plan = plain(mining.splitPlan(1000, 10, 100));
  assert.equal(plan.length, 10);
  assert.equal(plan.reduce((sum, item) => sum + item.target, 0), 1000);
  assert.equal(plan[0].seq, 1);

  const uneven = plain(mining.splitPlan(250, 0, 100));
  assert.equal(uneven.length, 3);
  assert.deepEqual(uneven.map((item) => item.target), [84, 83, 83]);
  assert.equal(uneven.reduce((sum, item) => sum + item.target, 0), 250);
});

test('splitPlan guards invalid targets', () => {
  const mining = loadConsole();
  assert.deepEqual(plain(mining.splitPlan(0, 4, 100)), []);
  assert.deepEqual(plain(mining.splitPlan(-10, 4, 100)), []);
});

test('applyAssignment only mutates the targeted batch and promotes planned status', () => {
  const mining = loadConsole();
  const batches = [
    { id: 'a', status: 'planned', assignees: [] },
    { id: 'b', status: 'done', assignees: [{ collector_id: 1 }] },
  ];
  const next = plain(mining.applyAssignment(batches, 'a', { assignees: [{ collector_id: 9, device_id: 3 }], window: '08-01 ~ 08-03' }));
  assert.deepEqual(next[0].assignees, [{ collector_id: 9, device_id: 3 }]);
  assert.equal(next[0].status, 'collecting');
  assert.equal(next[0].window, '08-01 ~ 08-03');
  assert.deepEqual(next[1], { id: 'b', status: 'done', assignees: [{ collector_id: 1 }] });
  assert.equal(batches[0].status, 'planned');
});

test('aggregateFailures ranks reasons by count', () => {
  const mining = loadConsole();
  const rows = plain(mining.aggregateFailures([
    { failures: [{ key: 'off_task', count: 3 }, { key: 'depth_abnormal', count: 8 }] },
    { failures: [{ key: 'off_task', count: 2 }, { key: 'unknown_reason', count: 5 }] },
  ]));
  assert.deepEqual(rows.map((row) => row.key), ['depth_abnormal', 'off_task']);
  assert.equal(rows[0].count, 8);
  assert.equal(rows[1].count, 5);
  assert.equal(mining.failureReasonLabel('depth_abnormal'), 'Depth 异常');
});

test('pipelineStageRows fills missing stages with a queued projection', () => {
  const mining = loadConsole();
  const rows = plain(mining.pipelineStageRows([{ id: 'p1', name: 'EGO', stages: [{ key: 'quality', status: 'running', progress: 40 }] }]));
  assert.equal(rows.length, 1);
  assert.equal(rows[0].stages.length, mining.PIPELINE_STAGES.length);
  assert.deepEqual(rows[0].stages.map((stage) => stage.key), ['split', 'integrity', 'quality', 'desensitize', 'process']);
  const quality = rows[0].stages.find((stage) => stage.key === 'quality');
  assert.equal(quality.status, 'running');
  assert.equal(quality.progress, 40);
  assert.equal(rows[0].stages[0].status, 'queued');
});

test('transferSummary counts states and total bytes', () => {
  const mining = loadConsole();
  const summary = plain(mining.transferSummary([
    { status: 'succeeded', size_bytes: 100 },
    { status: 'running', size_bytes: 50 },
    { status: 'failed', size_bytes: 30 },
    { status: 'queued', size_bytes: 20 },
  ]));
  assert.deepEqual(summary, { total: 4, running: 1, succeeded: 1, failed: 1, bytes: 200 });
});

test('store creates a task and splits it into batches', () => {
  const mining = loadConsole();
  const task = mining.createTask({ name: '新任务', target_episodes: 300, workspace_id: 1, task_set_id: 12 });
  assert.equal(task.batches.length, 0);
  const created = plain(mining.splitTaskBatches(task.id, { batch_count: 3, per_batch: 100 }));
  assert.equal(created.length, 3);
  assert.equal(mining.findTask(task.id).batches.length, 3);
  assert.equal(mining.taskProgress(mining.findTask(task.id)).target, 300);
});

test('store assigns a batch and filters tasks by scope', () => {
  const mining = loadConsole();
  const scoped = plain(mining.listTasks({ workspace_id: 1, task_set_id: 12 }));
  assert.ok(scoped.length >= 2);
  const task = scoped[0];
  const target = task.batches[0];
  mining.assignTaskBatch(task.id, target.id, { assignees: [{ collector_id: 7, device_id: 8 }] });
  const updated = plain(mining.findTask(task.id).batches.find((batch) => batch.id === target.id));
  assert.deepEqual(updated.assignees, [{ collector_id: 7, device_id: 8 }]);
  assert.equal(mining.listTasks({ task_set_id: 11 }).length, 1);
});

test('cutSummary counts valid and invalid segments with total duration', () => {
  const mining = loadConsole();
  const summary = plain(mining.cutSummary({
    segments: [
      { start_s: 0, end_s: 35, valid: true },
      { start_s: 35, end_s: 75, valid: true },
      { start_s: 75, end_s: 125, valid: false },
    ],
  }));
  assert.deepEqual(summary, { segment_count: 3, valid_count: 2, invalid_count: 1, total_s: 125 });
  assert.deepEqual(plain(mining.cutSummary({})), { segment_count: 0, valid_count: 0, invalid_count: 0, total_s: 0 });
});

test('filterCutEpisodes narrows by task name and keyword', () => {
  const mining = loadConsole();
  const rows = mining.listCutEpisodes();
  assert.ok(rows.length >= 5);
  const byTask = plain(mining.filterCutEpisodes(rows, { task_name: '水杯与托盘动作补采' }));
  assert.equal(byTask.length, 1);
  const byKeyword = plain(mining.filterCutEpisodes(rows, { keyword: '曾琳' }));
  assert.equal(byKeyword.length, 1);
  assert.deepEqual(plain(mining.filterCutEpisodes(rows, { keyword: '不存在的关键词' })), []);
  assert.equal(mining.filterCutEpisodes(rows, {}).length, rows.length);
});

test('activeCutSegment finds the segment covering the playhead', () => {
  const mining = loadConsole();
  const episode = mining.listCutEpisodes()[0];
  assert.equal(mining.activeCutSegment(episode, 10).id, episode.segments[0].id);
  assert.equal(mining.activeCutSegment(episode, 40).id, episode.segments[1].id);
  assert.equal(mining.activeCutSegment(episode, 500), null);
  assert.equal(mining.activeCutSegment(episode, 0), null);
});

test('formatCutRange renders zero-padded ranges', () => {
  const mining = loadConsole();
  assert.equal(mining.formatCutRange({ start_s: 0, end_s: 35 }), '00:00 - 00:35');
  assert.equal(mining.formatCutRange({ start_s: 75, end_s: 125 }), '01:15 - 02:05');
});

test('tag dictionaries support add and remove with dedupe', () => {
  const mining = loadConsole();
  assert.equal(mining.addDictItem('scene', '家居 · 卧室'), true);
  assert.equal(mining.addDictItem('scene', '家居 · 卧室'), false);
  const scene = mining.listDictionaries().find((entry) => entry.kind === 'scene');
  assert.ok(scene.items.includes('家居 · 卧室'));
  assert.equal(mining.removeDictItem('scene', '家居 · 卧室'), true);
  assert.equal(mining.removeDictItem('scene', '不存在的标签'), false);
  assert.equal(mining.addDictItem('unknown_kind', 'x'), false);
});

test('filterCutEpisodes narrows by tag dimensions', () => {
  const mining = loadConsole();
  const rows = mining.listCutEpisodes();
  const target = rows[0];
  const filters = {
    project: target.tags.project,
    scene: target.tags.scene,
    purpose: target.tags.purpose,
    train: target.tags.train,
  };
  const matched = mining.filterCutEpisodes(rows, filters);
  assert.ok(matched.length >= 1);
  matched.forEach((row) => {
    assert.equal(row.tags.project, filters.project);
    assert.equal(row.tags.scene, filters.scene);
  });
  assert.equal(mining.filterCutEpisodes(rows, { ...filters, project: '不存在的项目' }).length, 0);
});

test('dashboards expose capacity, collection and efficiency projections', () => {
  const mining = loadConsole();
  const capacity = plain(mining.capacityDashboard());
  assert.ok(capacity.kpis.plan_target > 0);
  assert.equal(capacity.daily.length, 7);
  const collection = plain(mining.collectionDashboard());
  assert.ok(collection.by_scene.length >= 3);
  assert.ok(collection.by_purpose.length >= 2);
  const efficiency = plain(mining.efficiencyDashboard());
  assert.ok(efficiency.collectors.length >= 3);
  assert.ok(efficiency.devices.length >= 2);
  const rates = efficiency.collectors.map((row) => row.rate);
  assert.deepEqual(rates, [...rates].sort((a, b) => b - a));
});

test('collection config can be updated and reset', () => {
  const mining = loadConsole();
  mining.updateCollectionConfig({ min_valid_rate: 0.95, batch_size: 200 });
  assert.equal(mining.getCollectionConfig().min_valid_rate, 0.95);
  assert.equal(mining.getCollectionConfig().batch_size, 200);
  mining.reset();
  assert.equal(mining.getCollectionConfig().min_valid_rate, 0.9);
});

test('batchStageRows projects the four stages with queued defaults', () => {
  const mining = loadConsole();
  const rows = plain(mining.batchStageRows({ stages: [{ key: 'split', status: 'succeeded', progress: 100 }] }));
  assert.deepEqual(rows.map((row) => row.key), ['integrity', 'split', 'quality', 'desensitize']);
  assert.equal(rows[0].status, 'queued');
  assert.equal(rows[1].status, 'succeeded');
});

test('canRunStage gates a stage on its predecessor and current status', () => {
  const mining = loadConsole();
  const batch = { id: 'x', stages: mining.batchStageRows({}) };
  assert.equal(mining.canRunStage(batch, 'integrity'), true);
  assert.equal(mining.canRunStage(batch, 'split'), false);
  const running = plain(mining.setBatchStage([batch], 'x', 'integrity', { status: 'running' }))[0];
  assert.equal(mining.canRunStage(running, 'integrity'), false);
  const done = plain(mining.setBatchStage([batch], 'x', 'integrity', { status: 'succeeded' }))[0];
  assert.equal(mining.canRunStage(done, 'split'), true);
});

test('setBatchStage only patches the targeted batch', () => {
  const mining = loadConsole();
  const batches = [
    { id: 'b1', stages: mining.batchStageRows({}) },
    { id: 'b2', stages: mining.batchStageRows({}) },
  ];
  const next = plain(mining.setBatchStage(batches, 'b2', 'integrity', { status: 'running', progress: 10 }));
  assert.equal(next[0].stages[0].status, 'queued');
  assert.equal(next[1].stages[0].status, 'running');
  assert.equal(batches[1].stages[0].status, 'queued');
});

test('restoreBatchToRaw sends split to manual and resets downstream stages', () => {
  const mining = loadConsole();
  const batches = [{
    id: 'b1',
    stages: [
      { key: 'integrity', status: 'succeeded', progress: 100 },
      { key: 'split', status: 'pending_review', progress: 100 },
      { key: 'quality', status: 'succeeded', progress: 100 },
      { key: 'desensitize', status: 'succeeded', progress: 100 },
    ],
  }];
  const next = plain(mining.restoreBatchToRaw(batches, 'b1'))[0];
  assert.equal(next.stages.find((stage) => stage.key === 'split').status, 'manual');
  assert.equal(next.stages.find((stage) => stage.key === 'quality').status, 'queued');
  assert.equal(next.stages.find((stage) => stage.key === 'desensitize').status, 'queued');
  assert.equal(next.stages.find((stage) => stage.key === 'integrity').status, 'succeeded');
  assert.equal(next.split_mode, 'manual');
  assert.equal(next.restored_to_raw, true);
});

test('store review and restore ops update the seeded batches', () => {
  const mining = loadConsole();
  const task = mining.listTasks()[0];
  const batch = task.batches.find((item) => item.stages.find((stage) => stage.key === 'split').status === 'pending_review');
  assert.ok(batch, 'seed should contain a batch pending split review');
  mining.reviewTaskBatchSplit(task.id, batch.id, true);
  assert.equal(mining.findTask(task.id).batches.find((item) => item.id === batch.id).stages.find((stage) => stage.key === 'split').status, 'succeeded');
  mining.restoreTaskBatchToManual(task.id, batch.id);
  const restored = mining.findTask(task.id).batches.find((item) => item.id === batch.id);
  assert.equal(restored.stages.find((stage) => stage.key === 'split').status, 'manual');
  assert.equal(restored.split_mode, 'manual');
});

test('seeded batches all carry the four-stage pipeline', () => {
  const mining = loadConsole();
  mining.listTasks().forEach((task) => {
    plain(task.batches).forEach((batch) => {
      assert.deepEqual(batch.stages.map((stage) => stage.key), ['integrity', 'split', 'quality', 'desensitize']);
    });
  });
});

test('listBatchCutEpisodes returns seeded episodes for known batches', () => {
  const mining = loadConsole();
  const rows = plain(mining.listBatchCutEpisodes(9001, '901-1', '桌面物体整理 · 8 月采集'));
  assert.equal(rows.length, 2);
  rows.forEach((row) => {
    assert.equal(row.batch_id, '901-1');
    assert.equal(row.task_name, '桌面物体整理 · 8 月采集');
    assert.ok(row.segments.length >= 2);
    assert.ok(row.preview_url.startsWith('https://'));
  });
});

test('listBatchCutEpisodes generates deterministic episodes for unknown batches', () => {
  const mining = loadConsole();
  const first = plain(mining.listBatchCutEpisodes(9001, '901-5', '桌面物体整理'));
  const second = plain(mining.listBatchCutEpisodes(9001, '901-5', '桌面物体整理'));
  assert.deepEqual(first, second);
  assert.equal(first.length >= 3, true);
  first.forEach((row, index) => {
    assert.equal(row.episode_uid, `AUTO-901-5-E${index + 1}`);
    assert.equal(row.batch_id, '901-5');
    assert.ok(row.segments.length >= 2);
    let cursor = 0;
    row.segments.forEach((segment) => {
      assert.equal(segment.start_s, cursor);
      cursor = segment.end_s;
    });
    assert.equal(row.duration_s, cursor);
  });
});

test('collection tasks can be distinguished by collection project and created from the empty state', () => {
  assert.match(appSource, /v-if="!miningTaskRows\.length"[\s\S]*@click="openCreateCollectionProjectDialog"/);
  assert.match(appSource, /v-if="collectionProjects\.length"[\s\S]*@click="openMiningTaskDialog"/);
  assert.match(appSource, /miningProjectOptions = computed\(\(\) =>/);
  assert.match(appSource, /scope\.row\.tags\?\.project/);
  assert.match(appSource, /loadCollectionProjectOptions\(\);/);
  assert.doesNotMatch(appSource, /selectedWorkspaceId"[\s\S]{0,400}text type="primary"/);
});
