/* Shared helpers for collection dashboards. */
const QuicStudioDashboardCharts = (() => {
  const known = (value) => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value)) && Number(value) >= 0;
  function formatDuration(value) {
    if (!known(value)) return '—';
    const total = Math.round(Number(value));
    return `${Math.floor(total / 3600)}:${String(Math.floor(total / 60) % 60).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}`;
  }
  const UNITS = ['B', 'KB', 'MB', 'GB', 'TB'];
  function formatBytes(value) {
    if (!known(value)) return '—';
    let size = Number(value); let unit = 0;
    while (size >= 1024 && unit < UNITS.length - 1) { size /= 1024; unit += 1; }
    return `${unit === 0 ? size : size.toFixed(2)} ${UNITS[unit]}`;
  }
  async function ensureEcharts(loader) {
    loader = loader || (typeof QuicStudioPageAssets !== 'undefined' ? QuicStudioPageAssets : globalThis.QuicStudioPageAssets);
    if (typeof globalThis.echarts !== 'undefined') return globalThis.echarts;
    if (!loader) throw new Error('图表组件不可用 / Charts are unavailable');
    await loader.load('charts');
    if (typeof globalThis.echarts === 'undefined') throw new Error('图表组件加载失败 / Charts failed to load');
    return globalThis.echarts;
  }
  function lineOption(points, { color = '#3568d4', formatter = (value) => String(value) } = {}) {
    const list = Array.isArray(points) ? points : [];
    return { color: [color], tooltip: { trigger: 'axis', valueFormatter: formatter }, grid: { left: 64, right: 16, top: 24, bottom: 28 }, xAxis: { type: 'category', boundaryGap: false, data: list.map((point) => point.bucket) }, yAxis: { type: 'value', minInterval: 1, axisLabel: { formatter } }, series: [{ type: 'line', smooth: true, showSymbol: list.length <= 31, areaStyle: { opacity: 0.12 }, data: list.map((point) => Number(point.value) || 0) }] };
  }
  function createRegistry(echartsLib) {
    const charts = new Map();
    return {
      render(key, el, option) { if (!el || !echartsLib) return null; let chart = charts.get(key); if (chart && chart.getDom() !== el) { chart.dispose(); chart = null; } if (!chart) { chart = echartsLib.init(el); charts.set(key, chart); } chart.setOption(option, true); return chart; },
      resize() { charts.forEach((chart) => chart.resize()); },
      dispose() { charts.forEach((chart) => chart.dispose()); charts.clear(); },
      size() { return charts.size; },
    };
  }
  return { formatDuration, formatBytes, ensureEcharts, lineOption, createRegistry };
})();
