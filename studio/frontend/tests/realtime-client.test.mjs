import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const clientPath = new URL('../js/realtime.js', import.meta.url);

function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

class FakeSocket {
  constructor() {
    this.connected = false;
    this.listeners = new Map();
    this.emitted = [];
  }

  on(event, listener) {
    this.listeners.set(event, listener);
  }

  emit(event, payload) {
    this.emitted.push([event, payload]);
  }

  disconnect() {
    this.connected = false;
  }

  trigger(event, payload) {
    this.listeners.get(event)?.(payload);
  }
}

function flushPromises() {
  return new Promise((resolve) => setImmediate(resolve));
}

test('realtime client reconnects with a one-time snapshot and ignores stale resource events', async () => {
  assert.equal(existsSync(clientPath), true);
  if (!existsSync(clientPath)) return;

  const socket = new FakeSocket();
  let connection;
  const context = {
    window: { location: { origin: 'http://localhost:8010' } },
    io(origin, options) {
      connection = { origin, options };
      return socket;
    },
  };
  context.globalThis = context;
  const source = readFileSync(clientPath, 'utf8');
  vm.runInNewContext(`${source}\n;globalThis.__realtime = QuicDataRealtime;`, context, { filename: 'realtime.js' });

  const updates = [];
  let snapshots = 0;
  context.__realtime.connect({ accessToken: 'in-memory-token' });
  context.__realtime.subscribe({
    resourceType: 'import_session',
    resourceId: 'import-1',
    realtimeVersion: 1,
    async refresh() {
      snapshots += 1;
      return { id: 'import-1', status: 'uploading', realtime_version: 1 };
    },
    onUpdate(resource) { updates.push(resource); },
  });

  socket.connected = true;
  socket.trigger('connect');
  await flushPromises();

  assert.equal(connection.origin, 'http://localhost:8010');
  assert.equal(connection.options.transports.length, 1);
  assert.equal(connection.options.transports[0], 'websocket');
  assert.deepEqual(plain(connection.options.auth), { access_token: 'in-memory-token' });
  assert.equal(snapshots, 1);
  assert.equal(socket.emitted.length, 1);
  assert.equal(socket.emitted[0][0], 'subscribe');
  assert.deepEqual({ ...socket.emitted[0][1] }, { resource_type: 'import_session', resource_id: 'import-1' });

  socket.trigger('import_session.updated', {
    resource_type: 'import_session',
    resource_id: 'import-1',
    resource_version: 2,
    resource: { id: 'import-1', status: 'parsing' },
  });
  socket.trigger('import_session.updated', {
    resource_type: 'import_session',
    resource_id: 'import-1',
    resource_version: 2,
    resource: { id: 'import-1', status: 'stale' },
  });

  assert.deepEqual(plain(updates), [
    { id: 'import-1', status: 'uploading', realtime_version: 1 },
    { id: 'import-1', status: 'parsing', realtime_version: 2 },
  ]);
});

test('workspace queue subscriptions refresh once after reconnect without a resource snapshot version', async () => {
  const socket = new FakeSocket();
  const context = {
    window: { location: { origin: 'http://localhost:8010' } },
    io() { return socket; },
  };
  context.globalThis = context;
  const source = readFileSync(clientPath, 'utf8');
  vm.runInNewContext(`${source}\n;globalThis.__realtime = QuicDataRealtime;`, context, { filename: 'realtime.js' });

  const updates = [];
  let snapshots = 0;
  context.__realtime.connect({ accessToken: 'in-memory-token' });
  context.__realtime.subscribe({
    resourceType: 'work_queue',
    resourceId: '12',
    refreshAlways: true,
    async refresh() {
      snapshots += 1;
      return { items: [{ id: snapshots }] };
    },
    onUpdate(snapshot) { updates.push(snapshot); },
  });

  socket.connected = true;
  socket.trigger('connect');
  await flushPromises();
  socket.connected = false;
  socket.connected = true;
  socket.trigger('connect');
  await flushPromises();

  assert.equal(snapshots, 2);
  assert.deepEqual(plain(updates), [
    { items: [{ id: 1 }] },
    { items: [{ id: 2 }] },
  ]);
  assert.equal(socket.emitted.filter(([event]) => event === 'subscribe').length, 2);
});

test('a subscribe-only resource registers on connect and reconnect without fetching a snapshot', async () => {
  const socket = new FakeSocket();
  const context = {
    window: { location: { origin: 'http://localhost:8010' } },
    io() { return socket; },
  };
  context.globalThis = context;
  const source = readFileSync(clientPath, 'utf8');
  vm.runInNewContext(`${source}\n;globalThis.__realtime = QuicDataRealtime;`, context, { filename: 'realtime.js' });

  let snapshots = 0;
  const updates = [];
  context.__realtime.connect({ accessToken: 'in-memory-token' });
  context.__realtime.subscribe({
    resourceType: 'work_item',
    resourceId: '42',
    realtimeVersion: 7,
    refreshOnSubscribe: false,
    refreshOnReconnect: false,
    async refresh() {
      snapshots += 1;
      return { id: 42, realtime_version: 7 };
    },
    onUpdate(resource) { updates.push(resource); },
  });

  socket.connected = true;
  socket.trigger('connect');
  await flushPromises();
  socket.connected = false;
  socket.connected = true;
  socket.trigger('connect');
  await flushPromises();

  assert.equal(snapshots, 0);
  assert.deepEqual(plain(updates), []);
  assert.equal(socket.emitted.filter(([event]) => event === 'subscribe').length, 2);
});

test('acknowledging a resource version suppresses the matching event but keeps newer events', async () => {
  const socket = new FakeSocket();
  const context = {
    window: { location: { origin: 'http://localhost:8010' } },
    io() { return socket; },
  };
  context.globalThis = context;
  const source = readFileSync(clientPath, 'utf8');
  vm.runInNewContext(`${source}\n;globalThis.__realtime = QuicDataRealtime;`, context, { filename: 'realtime.js' });

  const updates = [];
  context.__realtime.connect({ accessToken: 'in-memory-token' });
  socket.connected = true;
  socket.trigger('connect');
  context.__realtime.subscribe({
    resourceType: 'work_item',
    resourceId: '42',
    realtimeVersion: 1,
    refreshOnSubscribe: false,
    refreshOnReconnect: false,
    onUpdate(resource) { updates.push(resource); },
  });
  context.__realtime.acknowledge({ resourceType: 'work_item', resourceId: '42', realtimeVersion: 3 });

  socket.trigger('work_item.updated', {
    resource_type: 'work_item',
    resource_id: '42',
    resource_version: 3,
    resource: { id: 42, status: 'in_progress' },
  });
  socket.trigger('work_item.updated', {
    resource_type: 'work_item',
    resource_id: '42',
    resource_version: 4,
    resource: { id: 42, status: 'submitted' },
  });

  assert.deepEqual(plain(updates), [
    { id: 42, status: 'submitted', realtime_version: 4 },
  ]);
});

test('deferred resource events let a local mutation acknowledge itself before external updates resume', async () => {
  const socket = new FakeSocket();
  const context = {
    window: { location: { origin: 'http://localhost:8010' } },
    io() { return socket; },
  };
  context.globalThis = context;
  const source = readFileSync(clientPath, 'utf8');
  vm.runInNewContext(`${source}\n;globalThis.__realtime = QuicDataRealtime;`, context, { filename: 'realtime.js' });

  const updates = [];
  context.__realtime.connect({ accessToken: 'in-memory-token' });
  socket.connected = true;
  socket.trigger('connect');
  context.__realtime.subscribe({
    resourceType: 'work_item',
    resourceId: '42',
    realtimeVersion: 1,
    refreshOnSubscribe: false,
    refreshOnReconnect: false,
    onUpdate(resource) { updates.push(resource); },
  });
  const resume = context.__realtime.deferEvents({ resourceType: 'work_item', resourceId: '42' });

  socket.trigger('work_item.updated', {
    resource_type: 'work_item',
    resource_id: '42',
    resource_version: 2,
    resource: { id: 42, status: 'submitted' },
  });
  assert.deepEqual(plain(updates), []);

  context.__realtime.acknowledge({ resourceType: 'work_item', resourceId: '42', realtimeVersion: 2 });
  resume();
  assert.deepEqual(plain(updates), []);

  socket.trigger('work_item.updated', {
    resource_type: 'work_item',
    resource_id: '42',
    resource_version: 3,
    resource: { id: 42, status: 'assigned', assignee_user_id: 99 },
  });
  assert.deepEqual(plain(updates), [
    { id: 42, status: 'assigned', assignee_user_id: 99, realtime_version: 3 },
  ]);
});
