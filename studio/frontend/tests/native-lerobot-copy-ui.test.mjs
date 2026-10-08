import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const apiSource = readFileSync(new URL('../js/api.js', import.meta.url), 'utf8');
const appSource = readFileSync(new URL('../js/app.js', import.meta.url), 'utf8');
const cssSource = readFileSync(new URL('../css/app.css', import.meta.url), 'utf8');
const indexSource = readFileSync(new URL('../index.html', import.meta.url), 'utf8');

test('native LeRobot delivery is unavailable until the platform copy succeeds', () => {
  assert.match(apiSource, /retryNativeLerobotCopy/);
  assert.match(apiSource, /reauthorizeNativeLerobotSource/);
  assert.match(apiSource, /requestNativeLerobotBundle/);
  assert.match(apiSource, /getNativeLerobotBundleDownload/);
  assert.match(appSource, /nativeCopyStatusLabel/);
  assert.match(appSource, /nativeCopyErrorLabel/);
  assert.match(appSource, /oss_uri_available/);
  assert.match(appSource, /requestNativeLerobotBundle/);
  assert.match(appSource, /downloadNativeLerobotBundle/);
  assert.match(appSource, /subscribeNativeLerobotCopyJob/);
  assert.match(appSource, /selectedNativeLerobotDataset\?\.completed_at/);
  assert.match(appSource, /formatDateFull\(candidate\?\.completed_at\)/);
  assert.doesNotMatch(appSource, /refreshNativeLerobotDataset\(dataset\.id\)\.catch\(\(\) => \{\}\)/);
  assert.doesNotMatch(appSource, /refreshNativeLerobotBundle\(dataset\.id, bundle\.id\)\.catch\(\(\) => \{\}\)/);
  assert.doesNotMatch(appSource, /source_oss_uri/);
  assert.doesNotMatch(apiSource, /source_oss_uri/);
});

test('native records never enter QRDF dataset building and keep dense table controls readable', () => {
  assert.match(appSource, /row\?\.type === 'native_lerobot' && Number\(row\.batch_id\) === Number\(batch\.id\)/);
  assert.match(appSource, /<time class="compact-date-time"/);
  assert.match(appSource, /@header-dragend=/);
  assert.match(cssSource, /\.native-lerobot-copy-state/);
  assert.match(cssSource, /\.dataset-catalog-table \{ min-width: 980px; \}/);
  assert.match(indexSource, /\/js\/api\.js\?v=\d+/);
  assert.doesNotMatch(indexSource, /src="\/js\/train-console\.js/);
  assert.match(indexSource, /\/js\/app-bootstrap\.js\?v=\d+/);
});

test('native LeRobot datasets come from the authorized registry instead of retired batches', () => {
  assert.match(appSource, /const nativeLerobotBatchDatasets = ref\(\[\]\)/);
  assert.match(appSource, /async function loadNativeLerobotBatchDatasets\(\)/);
  assert.match(appSource, /QuicDataAPI\.listNativeLerobotDatasets\(\{\s*workspace_id: batch\.workspace_id,\s*task_set_id: batch\.task_set_id,/);
  assert.doesNotMatch(appSource, /loadNativeLerobotImportSessions/);
  assert.doesNotMatch(appSource, /loadDatasetCatalog\(\)/);
  assert.match(appSource, /loadNativeLerobotBatchDatasets\(\)/);
});
