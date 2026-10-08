import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const savePath = new URL('../js/workbench-save.js', import.meta.url);

function flushPromises() {
  return new Promise((resolve) => setImmediate(resolve));
}

function fakeClock() {
  let time = 0;
  let nextId = 1;
  const timers = new Map();

  return {
    now: () => time,
    setTimeout(callback, delay) {
      const id = nextId;
      nextId += 1;
      timers.set(id, { at: time + Math.max(0, Number(delay) || 0), callback });
      return id;
    },
    clearTimeout(id) {
      timers.delete(id);
    },
    advance(milliseconds) {
      const target = time + milliseconds;
      while (true) {
        const due = [...timers.entries()]
          .filter(([, timer]) => timer.at <= target)
          .sort((left, right) => left[1].at - right[1].at)[0];
        if (!due) break;
        timers.delete(due[0]);
        time = due[1].at;
        due[1].callback();
      }
      time = target;
    },
  };
}

function loadSaveModule() {
  assert.equal(existsSync(savePath), true, 'expected workbench-save.js');
  const context = {
    console,
    setTimeout,
    clearTimeout,
  };
  context.globalThis = context;
  vm.runInNewContext(
    `${readFileSync(savePath, 'utf8')}\n;globalThis.__save = QuicDataWorkbenchSave;`,
    context,
    { filename: 'workbench-save.js' },
  );
  return context.__save;
}

function createCoordinator({ clock, save, onSaved = () => {}, onFailed = () => {} }) {
  const module = loadSaveModule();
  return module.create({
    capture: (revision) => ({ revision }),
    save,
    onSaved,
    onFailed,
    setTimer: clock.setTimeout,
    clearTimer: clock.clearTimeout,
    now: clock.now,
    debounceMs: 5000,
    maxWaitMs: 30000,
  });
}

test('autosave waits for idle time and never overlaps an in-flight save', async () => {
  const clock = fakeClock();
  const pending = [];
  const coordinator = createCoordinator({
    clock,
    save: (snapshot) => new Promise((resolve) => pending.push({ snapshot, resolve })),
  });

  coordinator.markDirty();
  clock.advance(4999);
  await flushPromises();
  assert.equal(pending.length, 0);

  clock.advance(1);
  await flushPromises();
  assert.equal(pending.length, 1);
  assert.equal(pending[0].snapshot.revision, 1);

  coordinator.markDirty();
  assert.equal(pending.length, 1);
  pending[0].resolve({ draft: { version: 1 } });
  await flushPromises();
  assert.equal(coordinator.state().dirty, true);

  clock.advance(5000);
  await flushPromises();
  assert.equal(pending.length, 2);
  assert.equal(pending[1].snapshot.revision, 2);
});

test('continuous edits cannot postpone the maximum wait checkpoint', async () => {
  const clock = fakeClock();
  const requests = [];
  const coordinator = createCoordinator({
    clock,
    save: async (snapshot) => {
      requests.push(snapshot);
      return { draft: { version: snapshot.revision } };
    },
  });

  coordinator.markDirty();
  for (let index = 0; index < 7; index += 1) {
    clock.advance(4000);
    coordinator.markDirty();
  }
  clock.advance(2000);
  await flushPromises();

  assert.equal(requests.length, 1);
  assert.equal(requests[0].revision, 8);
});

test('a newer edit remains dirty when an older request resolves', async () => {
  const clock = fakeClock();
  const pending = [];
  const coordinator = createCoordinator({
    clock,
    save: (snapshot) => new Promise((resolve) => pending.push({ snapshot, resolve })),
  });

  coordinator.markDirty();
  clock.advance(5000);
  await flushPromises();
  coordinator.markDirty();
  pending[0].resolve({ draft: { version: 1 } });
  await flushPromises();

  assert.equal(coordinator.state().dirty, true);
  assert.equal(coordinator.state().savedRevision, 1);
  assert.equal(coordinator.state().localRevision, 2);
});

test('paused work does not autosave, while an explicit manual drain still saves', async () => {
  const clock = fakeClock();
  const requests = [];
  const coordinator = createCoordinator({
    clock,
    save: async (snapshot) => {
      requests.push(snapshot);
      return { draft: { version: snapshot.revision } };
    },
  });

  coordinator.markDirty();
  coordinator.pause();
  clock.advance(30000);
  await flushPromises();
  assert.equal(requests.length, 0);

  assert.equal(await coordinator.flush({ drain: true, reason: 'manual' }), true);
  assert.equal(requests.length, 1);
  assert.equal(coordinator.state().dirty, false);
});

test('drain saves edits made while the first manual request is in flight', async () => {
  const clock = fakeClock();
  const pending = [];
  const coordinator = createCoordinator({
    clock,
    save: (snapshot) => new Promise((resolve) => pending.push({ snapshot, resolve })),
  });

  coordinator.markDirty();
  const draining = coordinator.flush({ drain: true, reason: 'manual' });
  await flushPromises();
  assert.equal(pending.length, 1);

  coordinator.markDirty();
  pending[0].resolve({ draft: { version: 1 } });
  await flushPromises();
  assert.equal(pending.length, 2);
  pending[1].resolve({ draft: { version: 2 } });

  assert.equal(await draining, true);
  assert.equal(coordinator.state().dirty, false);
});
