import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../js/collection-dashboard-filters.js', import.meta.url), 'utf8');
const context = vm.createContext({}); vm.runInContext(`${source}\nglobalThis.filters = QuicStudioCollectionDashboardFilters;`, context); const f = context.filters;
const plain = (value) => JSON.parse(JSON.stringify(value));
test('default filters and params', () => {
  assert.deepEqual(plain(f.defaultFilters(new Date(2026, 8, 29))), { dateRange: ['2026-08-31', '2026-09-29'], projectIds: [], taskIds: [], sceneLabelIds: [], purposeLabelIds: [] });
  assert.deepEqual(plain(f.toParams({ dateRange: ['2026-09-01', '2026-09-02'], projectIds: ['3'], taskIds: [5], sceneLabelIds: [], purposeLabelIds: [9] })), { start_date: '2026-09-01', end_date: '2026-09-02', project_ids: [3], task_ids: [5], scene_label_ids: [], purpose_label_ids: [9] });
});
test('tasks narrow to projects and stale picks are pruned', () => {
  const tasks = [{ id: 1, collection_project_id: 10 }, { id: 2, project_id: 20 }, { id: 3, collection_project_id: 20 }];
  assert.deepEqual(plain(f.visibleTasks(tasks, [20]).map((t) => t.id)), [2, 3]); assert.deepEqual(plain(f.pruneTasks([1, 3], tasks, [20])), [3]);
});
