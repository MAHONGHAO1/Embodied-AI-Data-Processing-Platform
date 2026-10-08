import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../js/app-bootstrap.js', import.meta.url), 'utf8');

async function boot(hostname, search = '', fail = false) {
  const requests = [], root = { textContent: '' };
  const context = {
    URLSearchParams,
    window: { location: { hostname, search } },
    document: {
      createElement: () => ({}), getElementById: () => root,
      head: { appendChild(script) { requests.push(script.src); queueMicrotask(() => fail ? script.onerror() : script.onload()); } },
    },
  };
  vm.runInNewContext(source, context);
  await new Promise(resolve => setImmediate(resolve));
  return { requests, root };
}

test('production never requests demo fixtures even with a forged demo query', async () => {
  for (const host of ['studio-uat.quicrobot.xyz', 'studio.quicrobot.xyz']) {
    const { requests } = await boot(host, '?demo=1&mock=1');
    assert.equal(requests.length, 1);
    assert.match(requests[0], /^\/js\/app\.js\?v=/);
  }
});

test('local preview explicitly loads its stores before mounting the app', async () => {
  assert.equal((await boot('localhost')).requests.length, 1);
  const { requests } = await boot('127.0.0.1', '?demo=1');
  assert.deepEqual(requests.map(src => src.split('?')[0]), ['/js/demo-data.js', '/js/mining-console.js', '/js/app.js']);
});

test('failed startup renders an actionable error instead of an empty page', async () => {
  const { requests, root } = await boot('localhost', '?demo=1', true);
  assert.equal(requests.length, 1);
  assert.match(root.textContent, /Page load failed/);
});
