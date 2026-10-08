import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const apiSource = readFileSync(new URL('../js/api.js', import.meta.url), 'utf8');
const appSource = readFileSync(new URL('../js/app.js', import.meta.url), 'utf8');

test('collector membership API methods are defined on QuicDataAPI', () => {
  assert.match(apiSource, /listAvailableCollectorProfiles\(workspaceId,\s*query\s*=\s*''\)/);
  assert.match(apiSource, /\/collector-profiles\/available/);
  assert.match(apiSource, /grantCollectorMembership\(workspaceId,\s*profileId\)/);
  assert.match(apiSource, /\/collector-profiles\/memberships/);
  assert.match(apiSource, /revokeCollectorMembership\(workspaceId,\s*profileId\)/);
  assert.match(apiSource, /DELETE[\s\S]*\/collector-profiles\/.*\/memberships/);
});

test('collector dialog supports dual modes (create new vs select existing)', () => {
  assert.match(appSource, /const collectorDialogMode = ref\('existing'\)/);
  assert.match(appSource, /const availableCollectors = ref\(\[\]\)/);
  assert.match(appSource, /const selectedExistingCollectorId = ref\(null\)/);
  assert.match(appSource, /async function openCollectorDialog\(\)/);
  assert.match(appSource, /async function loadAvailableCollectors\(\)/);
  assert.match(appSource, /async function handleCollectorDialogSubmit\(\)/);
  assert.match(appSource, /async function handleRevokeCollector\(profile\)/);
  assert.match(appSource, /el-radio-group v-model="collectorDialogMode"/);
  assert.match(appSource, /collectorModeExisting/);
  assert.match(appSource, /collectorModeCreate/);
  assert.match(appSource, /selectExistingCollector/);
});

test('resources view exposes revoke collector action', () => {
  assert.match(appSource, /handleRevokeCollector\(scope\.row\)/);
  assert.match(appSource, /revokeCollectorMember/);
});
