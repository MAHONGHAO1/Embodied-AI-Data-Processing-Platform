import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const read = (name) => readFileSync(new URL(`../js/${name}`, import.meta.url), 'utf8');
function installOverview() {
  const context = vm.createContext({});
  vm.runInContext(`${read('dashboard-charts.js')}\n${read('collection-dashboard-filters.js')}\n${read('collection-data-board.js')}\n${read('collection-capacity-board.js')}\n${read('collection-efficiency-board.js')}\n${read('collection-overview.js')}\nglobalThis.overview = QuicStudioCollectionOverview;`, context);
  let registered = null;
  context.overview.install({ component(name, definition) { registered = { name, definition }; } });
  return registered;
}
test('overview shell registers three dashboard tabs with shared components', () => {
  const { name, definition } = installOverview();
  assert.equal(name, 'collection-overview');
  assert.ok(definition.components['collection-dashboard-filters']);
  assert.ok(definition.components['collection-data-board']);
  assert.ok(definition.components['collection-capacity-board']);
  assert.ok(definition.components['collection-efficiency-board']);
  for (const tab of ['产能看板', '数采看板', '人效看板']) assert.match(definition.template, new RegExp(tab));
  assert.match(definition.template, /<collection-data-board v-else-if="board === 'data'"/);
});
test('overview source does not touch dependencies at load time', () => {
  const context = vm.createContext({});
  assert.doesNotThrow(() => vm.runInContext(read('collection-overview.js'), context));
});
test('page loader fetches overview dependencies before page script', () => {
  const source = read('page-components.js');
  assert.match(source, /'collection-overview': \{ deps: \['\/js\/dashboard-charts\.js\?v=2', '\/js\/collection-dashboard-filters\.js\?v=1', '\/js\/collection-data-board\.js\?v=3', '\/js\/collection-capacity-board\.js\?v=4', '\/js\/collection-efficiency-board\.js\?v=2'\]/);
  assert.match(source, /page\.deps/);
});

test('collection overview renders the page workspace scope beside its own filters', () => {
  const source = readFileSync(new URL('../js/collection-overview.js', import.meta.url), 'utf8');
  assert.match(source, /<div v-if="\$slots\.scope" class="page-actions collection-scope-actions"><slot name="scope" \/><\/div>/);
  assert.match(source, /<collection-dashboard-filters v-model="filters" :projects="projects"/);
});
