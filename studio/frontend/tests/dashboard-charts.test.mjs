import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../js/dashboard-charts.js', import.meta.url), 'utf8');
const load = (extra = {}) => { const context = vm.createContext({ ...extra }); vm.runInContext(`${source}\nglobalThis.charts = QuicStudioDashboardCharts;`, context); return context.charts; };

test('formats duration, bytes, and unknown values', () => {
  const charts = load();
  assert.equal(charts.formatDuration(3661.4), '1:01:01');
  assert.equal(charts.formatDuration(null), '—');
  assert.equal(charts.formatBytes(1536), '1.50 KB');
  assert.equal(charts.formatBytes(undefined), '—');
});

test('line option maps buckets and registry lifecycle', () => {
  const charts = load();
  const option = charts.lineOption([{ bucket: 'a', value: 3 }]);
  assert.deepEqual(JSON.parse(JSON.stringify(option.xAxis.data)), ['a']);
  const created = []; const lib = { init(el) { const chart = { getDom: () => el, setOption() {}, resize() {}, dispose() { this.disposed = true; } }; created.push(chart); return chart; } };
  const registry = charts.createRegistry(lib); const el = {};
  registry.render('a', el, {}); registry.render('a', el, {}); assert.equal(created.length, 1); registry.dispose(); assert.equal(registry.size(), 0);
});

test('loads charts through the page asset binding', async () => {
  let loads = 0;
  const fake = { version: 'test' };
  const context = vm.createContext({ QuicStudioPageAssets: { load: async (name) => { assert.equal(name, 'charts'); loads += 1; context.echarts = fake; } } });
  vm.runInContext(`${source}\nglobalThis.charts = QuicStudioDashboardCharts;`, context);
  assert.equal(await context.charts.ensureEcharts(), fake);
  assert.equal(loads, 1);
});
