import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../js/scope-preference.js', import.meta.url), 'utf8');

function loadPreference() {
  const context = {};
  context.globalThis = context;
  vm.runInNewContext(`${source}\n;globalThis.__preference = QuicDataScopePreference;`, context);
  return context.__preference;
}

function memoryStorage() {
  const values = new Map();
  return {
    getItem(key) { return values.get(key) ?? null; },
    setItem(key, value) { values.set(key, String(value)); },
    removeItem(key) { values.delete(key); },
  };
}

test('scope preference is versioned, user-isolated, and only resolves accessible IDs', () => {
  const preference = loadPreference();
  const storage = memoryStorage();

  preference.write(storage, 7, { workspace_id: 3, task_set_id: 12 });
  assert.deepEqual(JSON.parse(JSON.stringify(preference.read(storage, 7))), {
    version: 1,
    workspace_id: 3,
    task_set_id: 12,
  });
  assert.equal(preference.read(storage, 8), null);

  assert.deepEqual(
    JSON.parse(JSON.stringify(preference.resolve({
      workspaceItems: [{ id: 1 }, { id: 3 }],
      taskSetItems: [{ id: 11 }, { id: 12 }],
      saved: preference.read(storage, 7),
      fallbackWorkspaceId: 1,
      fallbackTaskSetId: 11,
    }))),
    { workspace_id: 3, task_set_id: 12 },
  );

  assert.deepEqual(
    JSON.parse(JSON.stringify(preference.resolve({
      workspaceItems: [{ id: 1 }],
      taskSetItems: [{ id: 11 }],
      saved: preference.read(storage, 7),
      fallbackWorkspaceId: 1,
      fallbackTaskSetId: 11,
    }))),
    { workspace_id: 1, task_set_id: 11 },
  );
});

test('scope preference ignores malformed or mismatched local data', () => {
  const preference = loadPreference();
  const storage = memoryStorage();
  storage.setItem(preference.storageKey(7), '{bad json');
  assert.equal(preference.read(storage, 7), null);
  storage.setItem(preference.storageKey(7), JSON.stringify({ version: 2, workspace_id: 3, task_set_id: 12 }));
  assert.equal(preference.read(storage, 7), null);
});
