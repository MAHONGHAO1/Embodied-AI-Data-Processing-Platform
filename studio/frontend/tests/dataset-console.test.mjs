import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const appSource = readFileSync(new URL('../js/app.js', import.meta.url), 'utf8');
const catalogSource = readFileSync(new URL('../js/data-catalog.js', import.meta.url), 'utf8');

test('assets and datasets routes are owned by the standalone catalog', () => {
  assert.match(appSource, /<data-catalog\s+v-else-if="activeView === 'assets' \|\| activeView === 'datasets'/);
  assert.doesNotMatch(appSource, /showDatasetDialog|showDatasetRevisionDialog/);
  assert.doesNotMatch(appSource, /QuicDataDatasetRevisionBuilder|datasetRevision(?:Groups|Candidates|Dialog|Timeline)/);
  assert.doesNotMatch(appSource, /\bloadDatasets\s*\(/);
  assert.doesNotMatch(appSource, /\bopenDataset(?:CatalogRow|RevisionDialog)?\s*\(/);
  assert.match(catalogSource, /listCatalogDatasets/);
  assert.match(catalogSource, /listCatalogVersions/);
  assert.match(catalogSource, /createCatalogVersion/);
});

test('native LeRobot remains an independent batches flow', () => {
  assert.match(appSource, /openNativeLerobotDataset/);
  assert.match(appSource, /loadNativeLerobotBatchDatasets/);
  assert.match(appSource, /requestNativeLerobotBundle/);
  assert.doesNotMatch(appSource, /listDatasetRevisionCandidates\s*\(/);
  assert.doesNotMatch(appSource, /createDatasetRevision\s*\(/);
});

test('collection vocabulary still drives the intake guide', () => {
  assert.match(appSource, /collectionTask:/);
  assert.doesNotMatch(appSource, /v-model="importTaskLabelId"/);
  assert.doesNotMatch(appSource, /task_label_id:\s*taskLabelId/);
  assert.match(appSource, /showTaskLabelDialog/);
  assert.match(appSource, /intakeGuideStepManifest/);
});
