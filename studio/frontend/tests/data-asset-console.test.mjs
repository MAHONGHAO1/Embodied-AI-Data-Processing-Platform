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

test('data-asset API lists global assets with optional source workspace filter', async () => {
  assert.match(apiSource, /\blistDataAssets\s*\(/);
  assert.match(apiSource, /\bgetDataAsset\s*\(/);

  const { api, calls } = loadApi([
    { code: 200, data: { token: 't', userInfo: { id: 1 } } },
    { code: 200, data: { items: [] } },
    { code: 200, data: { id: 3 } },
  ]);
  await api.login('a@b.c', 'x');
  await api.listDataAssets({ source_workspace_id: 5 });
  await api.getDataAsset(3);

  assert.match(calls[1].url, /\/api\/v1\/data-assets\?source_workspace_id=5/);
  assert.equal(calls[2].url, 'http://localhost:8010/api/v1/data-assets/3');
});

test('catalog assets show frozen batch evidence rather than current Episode state', () => {
  const catalogSource = readFileSync(new URL('../js/data-catalog.js', import.meta.url), 'utf8');
  assert.match(appSource, /<data-catalog/);
  assert.match(catalogSource, /getDataAsset/);
  assert.match(catalogSource, /effective_duration_ns/);
  assert.match(catalogSource, /annotation_submission_id/);
  assert.match(catalogSource, /source_workspace_id/);
  assert.doesNotMatch(catalogSource, /listEpisodeAssets|claimWorkItem|releaseWorkItem/);
});

test('resources, settings, assets and datasets expose the current workspace scope', () => {
  assert.match(appSource, /viewRequiresWorkspace|scopeWorkspaceRequired/);
  assert.match(appSource, /!selectedWorkspaceId && (?:viewRequiresWorkspace|scopeWorkspaceRequired)\(activeView\)/);
  assert.match(appSource, /class="page-actions settings-center-actions collection-scope-actions"/);
  assert.match(appSource, /activeView === 'work-queue'/);
  assert.match(appSource, /activeView === 'overview'/);
  assert.match(appSource, /<data-catalog[^>]*:workspaces="workspaces"/);
});
