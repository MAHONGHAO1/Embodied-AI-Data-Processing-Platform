import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const apiSource = readFileSync(new URL('../js/api.js', import.meta.url), 'utf8');
const demoSource = readFileSync(new URL('../js/demo-data.js', import.meta.url), 'utf8');

function createDemoApi() {
  const sandbox = {
    window: { location: { hostname: '127.0.0.1', origin: 'http://127.0.0.1:8090', search: '?demo=1' } },
    URL,
    URLSearchParams,
    AbortController,
    console,
    structuredClone,
    setTimeout,
    clearTimeout,
    fetch: () => { throw new Error('demo mode must not call the network'); },
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(`${demoSource}\n;globalThis.QuicDataDemo = QuicDataDemo;`, sandbox, { filename: 'demo-data.js' });
  vm.runInContext(`${apiSource}\n;globalThis.QuicDataAPI = QuicDataAPI;`, sandbox, { filename: 'api.js' });
  return sandbox.QuicDataAPI;
}

test('api.js exposes the four token endpoints', () => {
  for (const method of ['listApiTokens', 'createApiToken', 'rotateApiToken', 'revokeApiToken']) {
    assert.match(apiSource, new RegExp(`\\b${method}\\s*\\(`));
  }
});

test('demo mode issues, lists without secret, rotates, and revokes tokens', async () => {
  const api = createDemoApi();
  await api.login('demo@local.preview', 'x');
  const created = await api.createApiToken({ name: 'duance-运维机', expires_in_days: 90 });
  assert.match(created.secret, /^qs_[A-Za-z0-9]+_[A-Za-z0-9]+$/);
  const listed = await api.listApiTokens();
  assert.ok(listed.items.length >= 1);
  assert.equal('secret' in listed.items[0], false);
  const rotated = await api.rotateApiToken(created.id);
  assert.match(rotated.secret, /^qs_/);
  assert.notEqual(rotated.secret, created.secret);
  const revoked = await api.revokeApiToken(created.id);
  assert.equal(revoked.revoked_at != null, true);
  assert.equal('secret' in revoked, false);
});

const appSource = readFileSync(new URL('../js/app.js', import.meta.url), 'utf8');

test('app.js wires the account menu to a full-screen token view', () => {
  assert.match(appSource, /command="api-tokens"/);
  assert.match(appSource, /showApiTokens/);
  assert.match(appSource, /openApiTokens\(/);
  assert.match(appSource, /loadApiTokens\(/);
  assert.match(appSource, /apiTokens:/);
});
