import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const apiSource = readFileSync(new URL('../js/api.js', import.meta.url), 'utf8');
const appSource = readFileSync(new URL('../js/app.js', import.meta.url), 'utf8');

test('frontend retires task set endpoints in favor of collection projects', () => {
  assert.match(apiSource, /listTaskSets\(workspaceId\)/);
  assert.doesNotMatch(apiSource, /\/workspace\/task-set\/options/);
  assert.match(apiSource, /createTaskSet\(body\)/);
  assert.doesNotMatch(apiSource, /\/workspace\/task-set\/create/);
  assert.match(apiSource, /\/collection-projects/);
  assert.doesNotMatch(apiSource, /\/workspace\/project\//);
});

test('runtime scope and payloads use required task set IDs', () => {
  assert.match(appSource, /const selectedTaskSetId = ref\(null\)/);
  assert.match(appSource, /task_set_id: selectedTaskSetId\.value/);
  assert.doesNotMatch(appSource, /ossScopeForm/);
  assert.doesNotMatch(appSource, /\bproject_id\b/);
});

test('collection project language is distinct from collection task labels', () => {
  // The legacy task set naming is deprecated: the scope level reads 采集项目.
  assert.match(appSource, /taskSet: '采集项目'/);
  assert.match(appSource, /collectionTask: '采集任务'/);
  assert.match(appSource, /taskSet: 'Collection project'/);
  assert.match(appSource, /collectionTask: 'Collection task'/);
  assert.match(appSource, /selectedCollectionProjectId/);
  assert.doesNotMatch(appSource, /taskSet: '任务集'/);
});

test('external project is exposed as a read-only projection only', () => {
  assert.match(appSource, /externalProjectStatus/);
  assert.match(appSource, /const externalProject = taskSet\?\.external_project/);
  assert.match(appSource, /externalProjectStatus\(currentTaskSet\)/);
  assert.match(appSource, /externalProjectUnbound/);
  assert.doesNotMatch(appSource, /external_project_ref_id/);
  assert.doesNotMatch(appSource, /bindExternalProject/);
});

test('collection task creation generates the backend key from the name', () => {
  assert.match(appSource, /function generateTaskLabelKey\(name\)/);
  assert.match(appSource, /const key = generateTaskLabelKey\(taskLabelForm\.name\)/);
  assert.match(appSource, /createTaskLabel\(\{ key, name: taskLabelForm\.name, description: taskLabelForm\.description \}\)/);
  assert.doesNotMatch(appSource, /v-model="taskLabelForm\.key"/);
  assert.match(appSource, /:disabled="!taskLabelForm\.name\.trim\(\)"/);
});
