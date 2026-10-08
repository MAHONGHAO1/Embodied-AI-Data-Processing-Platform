import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const apiSource = readFileSync(new URL('../js/api.js', import.meta.url), 'utf8');
const appSource = readFileSync(new URL('../js/app.js', import.meta.url), 'utf8');

test('API errors retain structured business error codes', () => {
  assert.match(apiSource, /error\.detail\s*=\s*payload\?\.detail/);
  assert.match(apiSource, /error\.code\s*=\s*payload\?\.detail\?\.code/);
});

test('resource creation maps stable conflict codes instead of English messages', () => {
  for (const code of [
    'workspace_name_exists',
    'task_set_name_exists',
    'dataset_name_exists',
    'collector_name_exists',
    'device_serial_exists',
  ]) {
    assert.match(appSource, new RegExp(code));
  }
  assert.doesNotMatch(appSource, /error\?\.message === 'dataset name already exists in the workspace'/);
});
