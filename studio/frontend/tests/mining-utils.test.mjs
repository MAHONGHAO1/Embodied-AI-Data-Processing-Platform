import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../js/mining-utils.js', import.meta.url), 'utf8');

function loadUtils() {
  const context = { globalThis: {} };
  context.globalThis = context;
  vm.runInNewContext(`${source}\n;globalThis.__utils = QuicDataMiningUtils;`, context, {
    filename: 'mining-utils.js',
  });
  return context.__utils;
}

function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

test('production mining helper module has no demo store or timer dependency', () => {
  assert.doesNotMatch(source, /seedTasks|seedPipelines|seedTransfers|localStorage|sessionStorage|setTimeout/);
  const utils = loadUtils();
  assert.equal(typeof utils.taskProgress, 'function');
  assert.equal(typeof utils.cutSummary, 'function');
  assert.equal(typeof utils.batchStageRows, 'function');
  assert.equal(utils.listTasks, undefined);
  assert.equal(utils.listCutEpisodes, undefined);
});

test('production mining helpers preserve the API projections used by app', () => {
  const utils = loadUtils();
  assert.deepEqual(plain(utils.taskProgress({
    target_episodes: 10,
    batches: [{ target: 10, raw: 8, checked: 8, valid: 6 }],
  })), {
    target: 10,
    raw: 8,
    checked: 8,
    valid: 6,
    valid_rate: 0.75,
    gap: 4,
    progress: 0.6,
    completed_batches: 0,
    batch_count: 1,
  });
  assert.deepEqual(plain(utils.cutSummary({
    segments: [
      { start_s: 0, end_s: 10, valid: true },
      { start_s: 10, end_s: 25, valid: false },
    ],
  })), { segment_count: 2, valid_count: 1, invalid_count: 1, total_s: 25 });
  assert.equal(utils.activeCutSegment({ segments: [{ id: 'a', start_s: 0, end_s: 10 }] }, 5).id, 'a');
  assert.equal(utils.formatCutRange({ start_s: 75, end_s: 125 }), '01:15 - 02:05');
  assert.equal(utils.batchStage({ stages: [{ key: 'integrity', status: 'succeeded' }] }, 'integrity').status, 'succeeded');
});

test('production helper is exposed under the legacy name without demo-only methods', () => {
  const context = { globalThis: {} };
  context.globalThis = context;
  vm.runInNewContext(`${source}\n;globalThis.__legacy = globalThis.QuicDataMining;`, context);
  assert.equal(typeof context.__legacy.taskProgress, 'function');
  assert.equal(context.__legacy.listTasks, undefined);
  assert.equal(context.__legacy.listBatchCutEpisodes, undefined);
});

test('pending package assignment and count preview use backend rules', () => {
  const utils = loadUtils();
  assert.equal(utils.canAssignPackage({ status: 'pending_assignment' }), true);
  assert.equal(utils.canAssignPackage({ raw_status: 'assigned', status: 'collecting' }), false);
  assert.equal(utils.taskAssignDone({ package_count: 3, pending_assignment_count: 0 }), true);
  assert.equal(utils.taskAssignDone({ package_count: 3 }), false);
  assert.equal(utils.packageCountPreview(5, 2), 3);
  assert.equal(utils.packageCountPreview(1, 0), null);
});
