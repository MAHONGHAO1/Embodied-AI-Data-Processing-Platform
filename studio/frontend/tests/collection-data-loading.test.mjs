import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
const app = readFileSync(new URL('../js/app.js', import.meta.url), 'utf8');
const source = app.slice(app.indexOf('      let miningDataGeneration = 0;'), app.indexOf('      async function loadMiningConfigData('));
function setup(api) {
  const errors = [];
  const context = {
    demoMode: { value: false }, selectedWorkspaceId: { value: 3 },
    selectedCollectionProjectId: { value: null }, collectionProjectOptions: { value: [] },
    miningSelectedTaskId: { value: '' },
    QuicDataAPI: api, errorMessage: e => errors.push(e.message),
    QuicDataMining: new Proxy({}, { get() { throw new Error('production accessed demo store'); } }),
  };
  for (const key of ['miningTasks','miningDictionaries','collectionLabels','collectionProjects','miningPipelines','miningTransfers']) context[key] = { value: [{ id: 'old' }] };
  vm.runInNewContext(source + ';globalThis.load=loadMiningData;', context);
  return { context, errors };
}
const emptyApi = () => ({ listCollectionProjects: async () => ({items:[]}), listCollectionLabels: async () => ({items:[]}), listCollectionTasks: async () => ({items:[]}) });

test('empty production dictionaries have no seeded fallback and failures stay visible', async () => {
  const { context, errors } = setup(emptyApi());
  await context.load();
  assert.equal(context.miningDictionaries.value.every(row => row.items.length === 0), true);
  assert.equal(context.miningTransfers.value.length, 0);
  context.QuicDataAPI.listCollectionLabels = async () => { throw new Error('dictionary offline'); };
  await context.load();
  assert.deepEqual(errors, ['dictionary offline']);
  assert.equal(context.collectionLabels.value.length, 0);
  assert.equal(context.miningTasks.value.length, 0);
});

test('space change invalidates late projects and clears data when no space is selected', async () => {
  let resolve;
  const api = emptyApi();
  api.listCollectionProjects = () => new Promise(r => { resolve = r; });
  const { context } = setup(api);
  const previous = context.load();
  context.selectedWorkspaceId.value = null;
  await context.load();
  resolve({items:[{id:3,name:'stale'}]});
  await previous;
  assert.equal(context.collectionProjects.value.length, 0);
  assert.equal(context.miningTasks.value.length, 0);
});
