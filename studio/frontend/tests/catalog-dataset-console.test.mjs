import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const apiSource = readFileSync(new URL('../js/api.js', import.meta.url), 'utf8');
const appSource = readFileSync(new URL('../js/app.js', import.meta.url), 'utf8');

function loadApi(responses = []) {
  const calls = [];
  const session = new Map();
  const context = {
    URL,
    window: { location: { origin: 'http://localhost:8010' } },
    sessionStorage: {
      getItem(key) { return session.get(key) || null; },
      setItem(key, value) { session.set(key, String(value)); },
      removeItem(key) { session.delete(key); },
    },
    async fetch(url, options = {}) {
      calls.push({ url: String(url), options });
      const payload = responses.shift() || { code: 200, data: {} };
      return { ok: true, status: 200, async json() { return payload; } };
    },
  };
  context.globalThis = context;
  vm.runInNewContext(`${apiSource}\n;globalThis.__api = QuicDataAPI;`, context, { filename: 'api.js' });
  return { api: context.__api, calls };
}

test('catalog dataset API covers dataset version export and lerobot import', async () => {
  for (const method of [
    'listCatalogDatasets',
    'createCatalogDataset',
    'listCatalogVersions',
    'createCatalogVersion',
    'exportCatalogVersion',
    'importLerobotCatalogDataset',
  ]) {
    assert.match(apiSource, new RegExp(`\\b${method}\\s*\\(`));
  }

  const { api, calls } = loadApi([
    { code: 200, data: { token: 't', userInfo: { id: 1 } } },
    { code: 200, data: { items: [] } },
    { code: 200, data: { items: [] } },
    { code: 200, data: { id: 1 } },
    { code: 200, data: { id: 2 } },
  ]);
  await api.login('a@b.c', 'x');
  await api.listCatalogDatasets();
  await api.listCatalogVersions(7);
  await api.exportCatalogVersion(9, { format: 'lerobot_3_0' });
  await api.importLerobotCatalogDataset({
    name: 'lr-1',
    native_lerobot_dataset_id: 3,
  });

  assert.equal(calls[1].url, 'http://localhost:8010/api/v1/catalog-datasets');
  assert.match(calls[2].url, /\/catalog-datasets\/7\/versions/);
  assert.match(calls[3].url, /\/catalog-datasets\/versions\/9\/export/);
  assert.deepEqual(JSON.parse(calls[3].options.body), { format: 'lerobot_3_0' });
  assert.match(calls[4].url, /\/catalog-datasets\/lerobot-imports/);
});

test('catalog dataset console delegates to the standalone catalog and has no retired import UI', () => {
  assert.match(appSource, /<data-catalog\s+v-else-if="activeView === 'assets' \|\| activeView === 'datasets'/);
  assert.doesNotMatch(appSource, /listCatalogDatasets|loadCatalogDatasets|listCatalogVersions|loadCatalogVersions/);
  assert.doesNotMatch(appSource, /createCatalogDataset|createCatalogVersion|exportCatalogVersion/);
  assert.doesNotMatch(appSource, /importLerobotCatalogDataset|submitAssetDatasetBuild/);
  assert.doesNotMatch(appSource, /showLerobotCatalogImportDialog|lerobotCatalogImportForm/);
  assert.doesNotMatch(appSource, /assetDatasetDialogVisible|assetDatasetForm|dataAssets/);
  assert.doesNotMatch(appSource, /ds-mock-/);
});
