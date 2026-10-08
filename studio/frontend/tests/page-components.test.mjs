import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../js/page-components.js', import.meta.url), 'utf8');
function setup() {
  const elements = [], registered = new Map();
  const definition = { name: 'Catalog' };
  const context = {
    Vue: { defineAsyncComponent: value => value },
    QuicDataCatalog: { install(app) { app.component('data-catalog', definition); } },
    QuicTrainConsole: { install(app) { app.component('train-console', definition); } },
    document: { createElement(tag) { return { tag, remove() { this.removed = true; } }; }, head: { appendChild(element) { elements.push(element); } } },
  };
  vm.runInNewContext(source + ';globalThis.pages=QuicStudioPageComponents;', context);
  context.pages.install({ component(name, value) { registered.set(name, value); } });
  return { elements, registered, definition, load: context.pages.load };
}

test('page registration does not load page code on login', () => {
  const { elements, registered } = setup();
  assert.equal(elements.length, 0);
  assert.equal(registered.size, 6);
  const html = readFileSync(new URL('../index.html', import.meta.url), 'utf8');
  assert.doesNotMatch(html, /src="\/js\/(train-console|data-catalog|package-annotation-workbench|intake-review-workbench|demo-data|mining-console)\.js/);
});

test('training code and styles load only when the training route mounts', async () => {
  const { elements, registered, definition } = setup();
  assert.equal(elements.length, 0);
  const first = registered.get('train-console').loader();
  assert.equal(elements.length, 2);
  assert.match(elements[0].src, /\/js\/train-console\.js/);
  assert.match(elements[1].href, /\/css\/train-console\.css/);
  elements.forEach(element => element.onload());
  assert.equal(await first, definition);
  assert.equal(await registered.get('train-console').loader(), definition);
  assert.equal(elements.length, 2);
});

test('concurrent page mounts share scripts and styles and reuse the compiled definition', async () => {
  const { elements, registered, definition } = setup();
  const loader = registered.get('data-catalog').loader;
  const first = loader(), second = loader();
  assert.equal(first, second);
  assert.equal(elements.length, 2);
  elements.forEach(element => element.onload());
  assert.equal(await first, definition);
  assert.equal(await loader(), definition);
  assert.equal(elements.length, 2);
});

test('a failed stylesheet retries without executing a successfully loaded script twice', async () => {
  const { elements, load } = setup();
  const first = load('data-catalog');
  elements.find(element => element.tag === 'script').onload();
  elements.find(element => element.tag === 'link').onerror();
  await assert.rejects(first, /Page load failed/);
  const retry = load('data-catalog');
  assert.equal(elements.filter(element => element.tag === 'script').length, 1);
  assert.equal(elements.length, 3);
  elements.at(-1).onload();
  await retry;
  await assert.rejects(load('https://example.invalid'), /Unknown page/);
});
