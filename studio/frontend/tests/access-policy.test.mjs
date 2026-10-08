import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../js/access-policy.js', import.meta.url), 'utf8');

function loadPolicy() {
  const context = {};
  context.globalThis = context;
  vm.runInNewContext(`${source}\n;globalThis.__policy = QuicDataAccessPolicy;`, context);
  return context.__policy;
}

function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

const users = {
  admin: { role: 'admin', permissions: ['*'] },
  operator: {
    role: 'operator',
    permissions: [
      'workspace:read', 'workspace:write', 'batch:*', 'import:*',
      'episode:*', 'dataset:*', 'export:*', 'train:*',
    ],
  },
  annotator: {
    role: 'annotator',
    permissions: ['workspace:read', 'batch:read', 'import:read', 'episode:read', 'episode:annotate'],
  },
  auditor: {
    role: 'auditor',
    permissions: ['workspace:read', 'batch:read', 'import:read', 'episode:read', 'episode:review'],
  },
  viewer: {
    role: 'viewer',
    permissions: ['workspace:read', 'batch:read', 'import:read', 'episode:read', 'dataset:read'],
  },
};

test('primary navigation follows endpoint permissions and collection admin constraints', () => {
  const policy = loadPolicy();

  assert.deepEqual(plain(policy.visibleViews(users.admin)), [
    'overview', 'intake', 'batches', 'work-queue', 'resources', 'assets', 'datasets',
    'trainDash', 'trainJobs', 'trainNew', 'trainDatasets', 'trainModels', 'trainResources', 'trainSystem',
    'admin', 'settings',
  ]);
  assert.deepEqual(plain(policy.visibleViews(users.operator)), [
    'overview', 'work-queue', 'resources', 'assets', 'datasets',
    'trainDash', 'trainJobs', 'trainNew', 'trainDatasets', 'trainModels', 'trainResources', 'trainSystem',
  ]);
  assert.deepEqual(plain(policy.visibleViews(users.annotator)), ['overview', 'work-queue', 'resources']);
  assert.deepEqual(plain(policy.visibleViews(users.auditor)), ['overview', 'work-queue', 'resources']);
  assert.deepEqual(plain(policy.visibleViews(users.viewer)), ['overview', 'resources', 'assets', 'datasets']);
  for (const user of [users.operator, users.annotator, users.auditor, users.viewer]) {
    for (const view of ['intake', 'batches', 'intake-review', 'miningTasks', 'miningDash']) assert.equal(policy.canView(user, view), false);
  }
  assert.equal(policy.canView(users.annotator, 'workbench'), true);
  assert.equal(policy.canView(users.annotator, 'datasets'), false);
  assert.equal(policy.canView(users.viewer, 'trainDash'), false);
  assert.equal(policy.canView(users.operator, 'trainDash'), true);
  assert.equal(policy.canView(users.operator, 'settings'), false);
  assert.equal(policy.canView(users.admin, 'settings'), true);
});

test('read access does not imply write access', () => {
  const policy = loadPolicy();

  assert.equal(policy.hasPermission(users.viewer, 'dataset:read'), true);
  assert.equal(policy.hasPermission(users.viewer, 'dataset:write'), false);
  assert.equal(policy.hasPermission(users.operator, 'dataset:write'), true);
  assert.equal(policy.hasPermission(users.admin, 'workspace:write'), true);
});

test('collection resources require the same episode read permission as the backend API', () => {
  const policy = loadPolicy();

  assert.equal(policy.canView({ permissions: ['workspace:read'] }, 'resources'), false);
  assert.equal(
    policy.canView({ permissions: ['workspace:read', 'episode:read'] }, 'resources'),
    true,
  );
});

test('chooses a capability-based landing view and queue stage', () => {
  const policy = loadPolicy();

  assert.equal(policy.defaultView(users.admin), 'overview');
  assert.equal(policy.defaultView(users.operator), 'overview');
  assert.equal(policy.defaultView(users.annotator), 'work-queue');
  assert.equal(policy.defaultQueueStage(users.annotator), 'annotation');
  assert.equal(policy.defaultView(users.auditor), 'work-queue');
  assert.equal(policy.defaultQueueStage(users.auditor), 'review');
  assert.equal(policy.defaultView(users.viewer), 'overview');
  assert.equal(policy.defaultView({ role: 'empty', permissions: [] }), null);
});

test('localizes stable roles and safely falls back for custom roles', () => {
  const policy = loadPolicy();

  assert.equal(policy.roleLabel('admin', 'zh-CN'), '管理员');
  assert.equal(policy.roleLabel('operator', 'zh-CN'), '数据运维');
  assert.equal(policy.roleLabel('annotator', 'zh-CN'), '标注员');
  assert.equal(policy.roleLabel('auditor', 'zh-CN'), '审核员');
  assert.equal(policy.roleLabel('viewer', 'zh-CN'), '查看者');
  assert.equal(policy.roleLabel('admin', 'en-US'), 'Administrator');
  assert.equal(policy.roleLabel('operator', 'en-US'), 'Data Operator');
  assert.equal(policy.roleLabel('annotator', 'en-US'), 'Annotator');
  assert.equal(policy.roleLabel('auditor', 'en-US'), 'Reviewer');
  assert.equal(policy.roleLabel('viewer', 'en-US'), 'Viewer');
  assert.equal(policy.roleLabel('curator', 'zh-CN', '数据策展'), '数据策展');
  assert.equal(policy.roleLabel('curator', 'en-US'), 'curator');
});
