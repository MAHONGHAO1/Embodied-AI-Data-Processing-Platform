import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const appSource = readFileSync(new URL('../js/app.js', import.meta.url), 'utf8');

test('collector labels show names while stable workspace numbers stay internal', () => {
  const helperSource = appSource.match(/function collectorDisplayLabel\([\s\S]*?\n      \}/);
  assert.ok(helperSource, 'expected a collector display-label helper');
  const collectorDisplayLabel = vm.runInNewContext(`(${helperSource[0]})`);

  assert.equal(
    collectorDisplayLabel({ name: '小王', profile_key: '0042', display_label: '小王#0042' }),
    '小王',
  );
  assert.equal(collectorDisplayLabel({ name: '历史人员' }), '历史人员');
  assert.equal(collectorDisplayLabel(null), '');

  assert.match(appSource, /collectorForm = reactive\(\{ name: '', profile_key: '' \}\)/);
  assert.match(appSource, /profile_key: collectorForm\.profile_key\.trim\(\) \|\| undefined/);
  assert.match(appSource, /:label="collectorDisplayLabel\(profile\)"/);
  assert.doesNotMatch(appSource, /:label="profile\.name"/);
});

test('collection devices can be deactivated and reactivated without changing history', () => {
  const helperSource = appSource.match(/async function setCollectionDeviceActive\([\s\S]*?\n      \}/);
  assert.ok(helperSource, 'expected one reversible device state helper');
  assert.match(helperSource[0], /updateCollectionDevice\(device\.id, \{ is_active: active \}\)/);
  assert.match(appSource, /setCollectionDeviceActive\(scope\.row, !scope\.row\.is_active\)/);
  assert.match(appSource, /scope\.row\.is_active \? t\('deactivate'\) : t\('activate'\)/);
  assert.doesNotMatch(appSource, /async function deactivateCollectionDevice/);
});

test('collector profiles can be deactivated and reactivated without changing history', () => {
  const helperSource = appSource.match(/async function setCollectorProfileActive\([\s\S]*?\n      \}/);
  assert.ok(helperSource, 'expected one reversible collector state helper');
  assert.match(helperSource[0], /updateCollectorProfile\(profile\.id, \{ is_active: active \}\)/);
  assert.match(appSource, /setCollectorProfileActive\(scope\.row, !scope\.row\.is_active\)/);
  assert.match(appSource, /scope\.row\.is_active \? t\('deactivate'\) : t\('activate'\)/);
  assert.match(appSource, /activate: '启用'/);
  assert.match(appSource, /activate: 'Activate'/);
  assert.match(appSource, /collectorProfiles\.filter\(\(item\) => item\.is_active\)/);
});
