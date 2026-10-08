import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../js/workbench-shortcuts.js', import.meta.url), 'utf8');

function loadShortcuts() {
  const context = { Object, String, Array, Set };
  context.globalThis = context;
  vm.runInNewContext(`${source}\n;globalThis.__shortcuts = QuicDataWorkbenchShortcuts;`, context, {
    filename: 'workbench-shortcuts.js',
  });
  return context.__shortcuts;
}

function keyboardEvent(overrides = {}) {
  return { key: '', ctrlKey: false, metaKey: false, shiftKey: false, altKey: false, isComposing: false, ...overrides };
}

test('workbench shortcut registry filters commands by capability', () => {
  const shortcuts = loadShortcuts();
  const cut = shortcuts.commandsFor({ cut: true, annotation: false });
  const annotation = shortcuts.commandsFor({ cut: false, annotation: true });

  assert.ok(cut.some((item) => item.id === 'cut-add-boundary'));
  assert.ok(!cut.some((item) => item.id === 'annotation-add-segment'));
  assert.ok(annotation.some((item) => item.id === 'annotation-add-segment'));
  assert.ok(!annotation.some((item) => item.id === 'cut-add-boundary'));
});

test('shortcut resolution handles command chords and ignores editable targets', () => {
  const shortcuts = loadShortcuts();
  const capabilities = { cut: true, annotation: false };

  assert.equal(shortcuts.resolve(keyboardEvent({ key: ' ', }), capabilities)?.id, 'play-toggle');
  assert.equal(shortcuts.resolve(keyboardEvent({ key: 'z', metaKey: true }), capabilities)?.id, 'history-undo');
  assert.equal(shortcuts.resolve(keyboardEvent({ key: 'Z', metaKey: true, shiftKey: true }), capabilities)?.id, 'history-redo');
  assert.equal(shortcuts.resolve(keyboardEvent({ key: 'b' }), capabilities)?.id, 'cut-add-boundary');
  assert.equal(shortcuts.resolve(keyboardEvent({ key: '?' }), capabilities)?.id, 'help-toggle');
  assert.equal(shortcuts.shouldIgnoreTarget({ tagName: 'input' }), true);
  assert.equal(shortcuts.shouldIgnoreTarget({ isContentEditable: true }), true);
  assert.equal(shortcuts.shouldIgnoreTarget({ tagName: 'button' }), false);
});
