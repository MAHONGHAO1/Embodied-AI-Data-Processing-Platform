import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const appSource = readFileSync(new URL('../js/app.js', import.meta.url), 'utf8');
const helperStart = appSource.indexOf('function dashboardMetricValue');
const helperEnd = appSource.indexOf('\n      function disposeDashboardCharts', helperStart);
assert.ok(helperStart >= 0 && helperEnd > helperStart, 'dashboard unknown-state helpers must remain named functions');
const helperSource = appSource.slice(helperStart, helperEnd);

test('dashboard helpers preserve real zero while rendering unavailable values as unknown', () => {
  const context = {
    formatPercent(value) {
      const number = Number(value || 0);
      return `${(number * 100).toFixed(1)}%`;
    },
    formatDelta(value) {
      const number = Number(value || 0);
      return { text: `${(number * 100).toFixed(1)}% vs yesterday`, tone: number ? 'up' : 'flat' };
    },
  };
  vm.createContext(context);
  vm.runInContext(`${helperSource}\nglobalThis.metric=dashboardMetricValue; globalThis.percent=dashboardPercent; globalThis.delta=dashboardDelta;`, context);
  assert.equal(context.metric(null), '—');
  assert.equal(context.metric(undefined), '—');
  assert.equal(context.metric(0), 0);
  assert.equal(context.percent(null), '—');
  assert.equal(context.percent(0), '0.0%');
  assert.deepEqual({ ...context.delta(null) }, { text: '—', tone: 'flat' });
  assert.deepEqual({ ...context.delta(0) }, { text: '0.0% vs yesterday', tone: 'flat' });
});

test('dashboard failure is visible and does not use zero fallbacks for unknown KPIs', () => {
  const metricBlock = appSource.match(/class="metric-grid is-dashboard"[\s\S]*?<\/div>\s*<div class="dashboard-grid">/)?.[0] || '';
  assert.match(metricBlock, /dashboardMetricValue\(dashboardOverview\?\.kpis/);
  assert.match(metricBlock, /dashboardDelta\(dashboardOverview\?\.kpis/);
  assert.doesNotMatch(metricBlock, /\?\?\s*0/);
  assert.match(appSource, /dashboardOverviewError\.value = error\?\.message \|\| t\('dashboardLoadFailed'\)/);
  assert.match(appSource, /<el-alert v-if="dashboardOverviewError" type="error"/);
  assert.match(appSource, /dashboardOverviewGeneration/);
});
