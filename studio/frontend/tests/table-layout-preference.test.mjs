import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../js/table-layout-preference.js', import.meta.url), 'utf8');

function memoryStorage() {
  const entries = new Map();
  return {
    getItem(key) { return entries.get(key) || null; },
    setItem(key, value) { entries.set(key, String(value)); },
    removeItem(key) { entries.delete(key); },
  };
}

function loadTableLayout() {
  const context = { JSON, Number, String, Object, Math };
  context.globalThis = context;
  vm.runInNewContext(`${source}\n;globalThis.__tableLayout = QuicDataTableLayoutPreference;`, context, {
    filename: 'table-layout-preference.js',
  });
  return context.__tableLayout;
}

test('column widths are user, workspace, and table scoped', () => {
  const preference = loadTableLayout();
  const storage = memoryStorage();
  const owner = { userId: 7, workspaceId: 12, tableKey: 'work-queue' };

  assert.equal(preference.writeColumn(storage, owner, 'updated_at', 242), true);
  assert.equal(preference.resolveWidth(storage, owner, 'updated_at', 166), 242);
  assert.equal(
    preference.resolveWidth(storage, { ...owner, workspaceId: 13 }, 'updated_at', 166),
    166,
  );
  assert.equal(
    preference.resolveWidth(storage, { ...owner, userId: 8 }, 'updated_at', 166),
    166,
  );
});

test('column preferences discard invalid JSON and clamp widths', () => {
  const preference = loadTableLayout();
  const storage = memoryStorage();
  const scope = { userId: 7, workspaceId: 12, tableKey: 'dataset-view' };
  const key = preference.storageKey(scope);

  storage.setItem(key, '{not json');
  assert.equal(preference.resolveWidth(storage, scope, 'created_at', 180), 180);
  assert.equal(preference.writeColumn(storage, scope, 'created_at', 9), true);
  assert.equal(preference.resolveWidth(storage, scope, 'created_at', 180), 64);
  assert.equal(preference.writeColumn(storage, scope, 'created_at', 9999), true);
  assert.equal(preference.resolveWidth(storage, scope, 'created_at', 180), 720);
});

test('column preferences can be reset and never persist without a valid scope', () => {
  const preference = loadTableLayout();
  const storage = memoryStorage();
  const scope = { userId: 7, workspaceId: 12, tableKey: 'import-candidates' };

  assert.equal(preference.writeColumn(storage, scope, 'collector', 212), true);
  assert.equal(preference.reset(storage, scope), true);
  assert.equal(preference.resolveWidth(storage, scope, 'collector', 160), 160);
  assert.equal(
    preference.writeColumn(storage, { userId: null, workspaceId: 12, tableKey: 'import-candidates' }, 'collector', 212),
    false,
  );
});
