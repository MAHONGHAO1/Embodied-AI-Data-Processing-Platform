import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const appSource = readFileSync(new URL('../js/app.js', import.meta.url), 'utf8');

function functionBody(name) {
  const start = appSource.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `expected ${name}`);
  return appSource.slice(start, appSource.indexOf('\n      }\n', start));
}

test('annotation and review lists span every workspace unless one is picked', () => {
  for (const name of ['loadAnnotationWorkItems', 'loadReviewWorkItems']) {
    const body = functionBody(name);
    assert.doesNotMatch(body, /selectedWorkspaceId/);
    assert.doesNotMatch(body, /if \(!workspaceId\) return;/);
    assert.match(body, /if \(workspaceId\) params\.workspace_id = Number\(workspaceId\);/);
    assert.match(body, /workQueueWorkspaceOptions\.value = Array\.isArray\(data\?\.workspaces\)/);
  }
  const loadQueue = functionBody('loadWorkQueue');
  assert.ok(loadQueue.indexOf('governanceQueueActive.value') < loadQueue.indexOf('if (!selectedWorkspaceId.value) return;'));
  assert.match(appSource, /const workQueueWorkspaceId = ref\(''\);/);
  assert.match(appSource, /<el-select v-model="workQueueWorkspaceId"[^>]*@change="switchWorkQueueWorkspace"[^>]*><el-option :label="t\('allWorkspaces'\)" value="" \/>/);
  assert.match(appSource, /\{\{ scope\.row\.workspace_name \|\| \('#' \+ scope\.row\.workspace_id\) \}\}/);
});

test('work items open, reassign and assign in their own workspace without membership', () => {
  assert.match(functionBody('openPackageWorkbench'), /packageWorkbenchRoute\.workspaceId = Number\(item\.workspace_id\);/);
  assert.doesNotMatch(functionBody('applyPackageWorkbenchRoute'), /workspaces\.value|selectedWorkspaceId/);
  assert.match(appSource, /reassignDialog\.workspaceId = item\?\.workspace_id \|\| null;/);
  assert.match(functionBody('submitReassignDialog'), /const body = \{ workspace_id: workspaceId, to_user_id: toUserId, reason \};/);
  assert.match(appSource, /const annotatorUserOptions = computed\(\(\) => \(managedUsers\.value \|\| \[\]\)\.filter\(\(user\) => user\.role === 'annotator' && user\.is_active !== false\)\);/);
  assert.match(appSource, /const reviewerUserOptions = computed\(\(\) => \(managedUsers\.value \|\| \[\]\)\.filter\(\(user\) => user\.role === 'auditor' && user\.is_active !== false\)\);/);
  assert.match(functionBody('viewRequiresWorkspace'), /'work-queue', 'package-workbench'/);
});
