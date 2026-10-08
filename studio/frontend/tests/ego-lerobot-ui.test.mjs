import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const appSource = readFileSync(new URL('../js/app.js', import.meta.url), 'utf8');
const apiSource = readFileSync(new URL('../js/api.js', import.meta.url), 'utf8');
const cssSource = readFileSync(new URL('../css/app.css', import.meta.url), 'utf8');

test('legacy LeRobot scan, session and batch creation paths are fully removed', () => {
  assert.doesNotMatch(appSource, /<el-dialog v-model="showBatchDialog"/);
  for (const retired of [
    'requestNativeLerobotScan',
    'createNativeLerobotImportSession',
    'submitNativeLerobotImportSession',
    'listNativeLerobotScanCandidates',
    'getCurrentNativeLerobotScanSnapshot',
    'listNativeLerobotCandidates',
    'createNativeLerobotBatch',
  ]) {
    assert.doesNotMatch(appSource, new RegExp(`\\b${retired}\\b`));
    assert.doesNotMatch(apiSource, new RegExp(`\\b${retired}\\b`));
  }
  // The retained native LeRobot path is the authorized dataset registry.
  assert.match(apiSource, /\blistNativeLerobotDatasets\s*\(/);
  assert.match(appSource, /openNativeLerobotDataset/);
  assert.match(appSource, /retryNativeLerobotCopy/);
  assert.match(appSource, /requestNativeLerobotBundle/);
  assert.doesNotMatch(appSource, /v-model="(?:lerobot|native).*oss_uri"/);
});

test('dataset browsing delegates to the catalog while native LeRobot stays in batches', () => {
  assert.match(appSource, /<data-catalog[^>]*activeView === 'assets' \|\| activeView === 'datasets'/);
  assert.doesNotMatch(appSource, /assetDatasetBuildMode|trainDatasetTreeRows|builtTrainDatasets/);
  assert.doesNotMatch(appSource, /listDatasetCatalog|datasetCatalogPage|changeDatasetCatalogPage/);
  assert.match(readFileSync(new URL('../js/data-catalog.js', import.meta.url), 'utf8'), /listCatalogDatasets/);
  assert.doesNotMatch(appSource, /QuicDataDatasetRevisionBuilder|datasetRevision(?:Groups|Candidates|Dialog|Timeline)/);
  assert.match(appSource, /openNativeLerobotDataset/);
  assert.match(appSource, /copyNativeLerobotOssUri/);
  assert.match(appSource, /v-model="showNativeLerobotDialog"/);
});

test('collector QR controls and package attribution are visible without exposing QR payloads', () => {
  assert.match(appSource, /v-model="showCollectorQrDialog"/);
  assert.match(appSource, /openCollectorQrDialog\(scope\.row\)/);
  assert.match(appSource, /collectorNumber/);
  assert.match(appSource, /defaultCollector/);
  assert.match(appSource, /collectorAttributionLabel/);
  assert.match(appSource, /dataPackageStatusLabel\(scope\.row\.status\)/);
  assert.doesNotMatch(appSource, /\{\{\s*card\.payload\s*\}\}/);
});

test('dense tables preserve full timestamps and allow per-user column sizing without a reset button', () => {
  assert.match(appSource, /@header-dragend=/);
  assert.match(appSource, /tableColumnWidth\('work-queue', 'updated_at', 190\)/);
  assert.match(appSource, /formatDateFull\(scope\.row\.work_item\.updated_at\)/);
  assert.doesNotMatch(appSource, /resetTableColumnWidths\('work-queue'\)/);
  assert.match(cssSource, /\.table-panel \{ min-width: 0; overflow-x: auto; \}/);
});

test('workbench shortcut commands have a discoverable help surface', () => {
  assert.match(appSource, /v-model="showShortcutHelp"/);
  assert.match(appSource, /workbenchShortcutCommands/);
  assert.match(appSource, /timelineIcon\('keyboard'\)/);
});

test('dataset building uses the asset catalog and the retired build view is absent', () => {
  assert.doesNotMatch(appSource, /activeView === 'buildData'/);
  assert.match(appSource, /<data-catalog/);
  const catalogSource = readFileSync(new URL('../js/data-catalog.js', import.meta.url), 'utf8');
  assert.match(catalogSource, /createCatalogVersion/);
  assert.match(catalogSource, /data_asset_ids/);
});
test('LeRobot submission uses a valid UUID request id outside secure contexts', () => {
  const requestIdFactory = appSource.match(
    /function nativeLerobotSessionRequestId\(\) \{([\s\S]*?)\n      \}/,
  );

  assert.ok(requestIdFactory, 'expected the LeRobot request id factory');
  assert.match(
    requestIdFactory[1],
    /createUuidRequestId\(globalThis\.crypto\)/,
  );
  assert.doesNotMatch(requestIdFactory[1], /Date\.now\(\)|Math\.random\(\)|QuicDataDatasetRevisionBuilder/);
});
