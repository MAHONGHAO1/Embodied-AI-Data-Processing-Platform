import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../js/page-assets.js', import.meta.url), 'utf8');
function setup() {
  const scripts = [];
  const context = { document: {
    createElement() { return { remove() { this.removed = true; } }; },
    head: { appendChild(script) { scripts.push(script); } },
  } };
  vm.runInNewContext(source + ';globalThis.assets=QuicStudioPageAssets;', context);
  return { scripts, load: context.assets.load };
}

test('charts load once across callers and keep integrity protection', async () => {
  const { scripts, load } = setup();
  const first = load('charts');
  assert.equal(first, load('charts'));
  assert.equal(scripts.length, 1);
  assert.match(scripts[0].src, /^\/vendor\/echarts\//);
  assert.match(scripts[0].integrity, /^sha384-/);
  assert.equal(scripts[0].crossOrigin, 'anonymous');
  scripts[0].onload();
  await first;
  await load('charts');
  assert.equal(scripts.length, 1);
});

test('failed asset loads are removable and retryable; unknown paths cannot load', async () => {
  const { scripts, load } = setup();
  const first = load('charts');
  scripts[0].onerror();
  await assert.rejects(first, /加载失败/);
  assert.equal(scripts[0].removed, true);
  const retry = load('charts');
  assert.equal(scripts.length, 2);
  scripts[1].onload();
  await retry;
  await assert.rejects(load('https://untrusted.example'), /Unknown/);
  assert.equal(scripts.length, 2);
});

test('entry page excludes chart engine and loads the page dependency helper', () => {
  const html = readFileSync(new URL('../index.html', import.meta.url), 'utf8');
  assert.doesNotMatch(html, /<script[^>]+src="\/vendor\/echarts/);
  assert.doesNotMatch(html, /<script[^>]+src="\/vendor\/qrcode-generator/);
  assert.match(html, /src="\/js\/page-assets.js\?v=\d+"/);
});

test('QR controls load their engine once on demand with integrity protection', async () => {
  const { scripts, load } = setup();
  assert.equal(scripts.length, 0);
  const first = load('qr');
  assert.equal(first, load('qr'));
  assert.equal(scripts.length, 1);
  assert.match(scripts[0].src, /^\/vendor\/qrcode-generator\//);
  assert.match(scripts[0].integrity, /^sha384-/);
  scripts[0].onload();
  await first;
  await load('qr');
  assert.equal(scripts.length, 1);
});
