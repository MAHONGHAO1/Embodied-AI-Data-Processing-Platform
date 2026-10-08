import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../js/dashboard-state.js', import.meta.url), 'utf8');
const context = {};
context.globalThis = context;
vm.runInNewContext(`${source}\n;globalThis.__dashboard = QuicDataDashboardState;`, context);
const dashboard = context.__dashboard;

test('ordinary users never produce a global dashboard scope', () => {
  assert.deepEqual(
    structuredClone(dashboard.resolveScope({ role: 'operator' }, 3, null)),
    { workspace_id: 3, task_set_id: null },
  );
  assert.throws(() => dashboard.resolveScope({ role: 'operator' }, null, null), /工作空间/);
});

test('admin can explicitly select global, workspace, or task-set scope', () => {
  assert.deepEqual(structuredClone(dashboard.resolveScope({ role: 'admin' }, null, null)), {
    workspace_id: null,
    task_set_id: null,
  });
  assert.deepEqual(structuredClone(dashboard.resolveScope({ role: 'admin' }, 3, 8)), {
    workspace_id: 3,
    task_set_id: 8,
  });
});

test('scope keys match the backend dashboard scope contract', () => {
  assert.equal(dashboard.scopeKey({}), 'global');
  assert.equal(dashboard.scopeKey({ workspace_id: 7 }), 'workspace:7');
  assert.equal(dashboard.scopeKey({ workspace_id: 7, task_set_id: 12 }), 'task-set:12');
});

test('refresh accepts only its exact job or a snapshot newer than its baseline', () => {
  const receipt = {
    job_id: 'job-2',
    scope_key: 'workspace:3',
    baseline_computed_at: '2026-08-13T10:00:00Z',
  };
  const oldSnapshot = {
    scope: { key: 'workspace:3' },
    etl_job_id: 'job-1',
    computed_at: '2026-08-13T10:00:00Z',
  };

  assert.equal(dashboard.isFreshSnapshot(oldSnapshot, receipt), false);
  assert.equal(dashboard.isFreshSnapshot({ ...oldSnapshot, etl_job_id: 'job-2' }, receipt), true);
  assert.equal(
    dashboard.isFreshSnapshot({ ...oldSnapshot, computed_at: '2026-08-13T10:00:01Z' }, receipt),
    true,
  );
  assert.equal(
    dashboard.isFreshSnapshot({ ...oldSnapshot, scope: { key: 'workspace:4' }, etl_job_id: 'job-2' }, receipt),
    false,
  );
  assert.equal(
    dashboard.isFreshSnapshot({ ...oldSnapshot, source: 'live', etl_job_id: 'job-1' }, receipt),
    true,
  );
});

test('job status helpers recognize success and terminal failures', () => {
  assert.equal(dashboard.jobSucceeded({ status: 'succeeded' }), true);
  assert.equal(dashboard.jobSucceeded({ status: 'running' }), false);
  assert.equal(dashboard.jobFailed({ status: 'failed' }), true);
  assert.equal(dashboard.jobFailed({ status: 'cancelled' }), true);
  assert.equal(dashboard.jobFailed({ status: 'queued' }), false);
});

test('today queues are projected from funnel stages and cannot keep a stale queue total', () => {
  const payload = {
    kpis: { total_episodes: 5 },
    pipeline_funnel: {
      stages: [
        { key: 'intake', buckets: { lt_30s: 0, bt_30_60s: 0, gt_60s: 0, unknown: 0 } },
        { key: 'collected', buckets: { lt_30s: 1, bt_30_60s: 4, gt_60s: 0, unknown: 0 } },
        { key: 'separated', buckets: { lt_30s: 0, bt_30_60s: 0, gt_60s: 0, unknown: 0 } },
        { key: 'annotated', buckets: { lt_30s: 0, bt_30_60s: 0, gt_60s: 0, unknown: 0 } },
        { key: 'stored', buckets: { lt_30s: 0, bt_30_60s: 0, gt_60s: 0, unknown: 0 } },
      ],
    },
    today_queues: [{ key: 'pending_process', count: 4, share: 1, pending: 4 }],
  };
  const rows = dashboard.todayQueuesFromFunnel(payload, 'zh-CN');
  const collected = rows.find((row) => row.key === 'collected');
  assert.equal(collected.count, 5);
  assert.equal(collected.pending, 5);
  assert.equal(collected.share, 1);
  assert.equal(collected.label, '待处理');
  assert.equal(rows.reduce((sum, row) => sum + row.count, 0), 5);
});
