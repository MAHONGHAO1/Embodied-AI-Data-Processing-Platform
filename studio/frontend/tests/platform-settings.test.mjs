import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const apiSource = readFileSync(new URL('../js/api.js', import.meta.url), 'utf8');
const appSource = readFileSync(new URL('../js/app.js', import.meta.url), 'utf8');
const policySource = readFileSync(new URL('../js/access-policy.js', import.meta.url), 'utf8');

test('platform settings are an administrator-only primary view', () => {
  assert.match(appSource, /VIEWS = new Set\(\[[^\]]*'settings'/);
  assert.match(appSource, /data-view="settings"/);
  assert.match(appSource, /activeView === 'settings'/);
  assert.match(policySource, /settings: \['\*'\]/);
});

test('obsolete AI runtime settings and OSS scopes are removed from API and UI', () => {
  for (const method of [
    'updatePlatformAiSettings',
    'createOssImportScope',
    'updateOssImportScope',
  ]) {
    assert.doesNotMatch(apiSource, new RegExp(`\\b${method}\\s*\\(`));
  }
  assert.doesNotMatch(appSource, /<h2>\{\{\s*t\('aiRuntimeSettings'\)\s*\}\}<\/h2>/);
  assert.doesNotMatch(appSource, /<h2>\{\{\s*t\('ossImportScopes'\)\s*\}\}<\/h2>/);
  assert.doesNotMatch(appSource, /openOssScopeDialog/);
  assert.doesNotMatch(appSource, /v-model="showOssScopeDialog"/);
});

test('settings center exposes collection projects and label dictionaries APIs', () => {
  for (const method of [
    'listCollectionProjects',
    'createCollectionProject',
    'updateCollectionProject',
    'archiveCollectionProject',
    'listCollectionLabels',
    'createCollectionLabel',
    'deactivateCollectionLabel',
  ]) {
    assert.match(apiSource, new RegExp(`\\b${method}\\s*\\(`));
  }
  assert.match(appSource, /collectionProjects/);
  assert.match(appSource, /collectionLabels/);
});
