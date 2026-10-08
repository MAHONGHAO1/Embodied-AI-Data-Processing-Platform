import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const workbenchSource = readFileSync(new URL('../js/intake-review-workbench.js', import.meta.url), 'utf8');
const appSource = readFileSync(new URL('../js/app.js', import.meta.url), 'utf8');
const cssSource = readFileSync(new URL('../css/app.css', import.meta.url), 'utf8');
const policySource = readFileSync(new URL('../js/access-policy.js', import.meta.url), 'utf8');
const demoSource = readFileSync(new URL('../js/demo-data.js', import.meta.url), 'utf8');
const apiSource = readFileSync(new URL('../js/api.js', import.meta.url), 'utf8');

test('access policy authorizes the intake-review view', () => {
  assert.match(policySource, /['"]?intake-review['"]?\s*:\s*\[/);
});

test('app.js registers the intake-review view and review interaction', () => {
  assert.match(appSource, /'intake-review'/);
  assert.match(appSource, /openIntakeReview\(/);
  assert.match(appSource, /rejectedEpisodeIds/);
  assert.match(appSource, /submitIntakeApprove\(/);
  assert.match(workbenchSource, /admission_reason/);
  assert.match(workbenchSource, /privacy_sensitive/);
});

test('entry buttons call openIntakeReview from the review queue and the package drawer', () => {
  const matches = appSource.match(/@click="openIntakeReview\(/g) || [];
  assert.ok(matches.length >= 2, `expected >=2 @click="openIntakeReview( bindings, got ${matches.length}`);
  assert.match(appSource, /enterIntakeReviewAction/);
});

test('approve reloads the package instead of shallow-merging the verdict response', () => {
  const submitBlock = appSource.match(/async function submitIntakeApprove\(\)[\s\S]*?\n      async function backFromIntakeReview/)?.[0] || '';
  assert.match(submitBlock, /loadIntakeReviewPackage\(pkg\.id\)/);
  assert.doesNotMatch(submitBlock, /intakeReviewPackage\.value = \{ \.\.\.intakeReviewPackage\.value, \.\.\.res \}/);
});

test('app.js renders the full screen intake review section', () => {
  assert.match(appSource, /activeView === 'intake-review'/);
  assert.match(appSource, /openIntakeReview\(/);
  assert.match(appSource, /toggleEpisodeRejected\(/);
  assert.match(appSource, /submitIntakeApprove\(/);
  assert.match(appSource, /intakeApprovePreview/);
});

test('hash navigation into intake-review loads the package from the route query', () => {
  assert.match(appSource, /nextView === 'intake-review'/);
  assert.match(appSource, /route\.query\.package_id \|\| intakeReviewPackageId\.value/);
});

test('backToBatchesList keeps the batches back label while intake-review keeps backToBatches', () => {
  assert.match(appSource, /backToBatchesList: '[^']*返回数据包列表/);
  assert.match(appSource, /backToBatchesList: 'Back to batches'/);
  assert.match(appSource, /backToBatches: '[^']*返回审核队列/);
  assert.match(appSource, /backToBatches: 'Back to review queue'/);
  assert.match(appSource, /t\('backToBatchesList'\)/);
});

test('setAuthorizedView keeps the package id in the intake-review hash', () => {
  assert.match(appSource, /nextView === 'intake-review'/);
  assert.match(appSource, /intake-review\?package_id=\$\{packageId\}/);
});

test('intake review keeps the collection-review navigation selected', () => {
  assert.match(appSource, /\['batches', 'intake-review'\]\.includes\(activeView\)/);
});

test('returning to the review queue temporarily highlights the reviewed package row', () => {
  const backBlock = appSource.match(/async function backFromIntakeReview\(\)[\s\S]*?\n      const intakeReviewCounts/)?.[0] || '';
  assert.match(backBlock, /highlightReturnedReviewPackage\(packageId\)/);
  assert.match(appSource, /:row-class-name="reviewPackageRowClass"/);
  assert.match(appSource, /review-package-return-highlight/);
  assert.match(appSource, /}, 5000\)/);
  assert.match(cssSource, /\.review-package-return-highlight > td\.el-table__cell/);
});

function createDemoApi() {
  const sandbox = {
    window: { location: { hostname: '127.0.0.1', origin: 'http://127.0.0.1:8090', search: '?demo=1' } },
    URL, URLSearchParams, AbortController, console, structuredClone, setTimeout, clearTimeout,
    fetch: () => { throw new Error('demo mode must not call the network'); },
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(`${demoSource}\n;globalThis.QuicDataDemo = QuicDataDemo;`, sandbox, { filename: 'demo-data.js' });
  vm.runInContext(`${apiSource}\n;globalThis.QuicDataAPI = QuicDataAPI;`, sandbox, { filename: 'api.js' });
  return sandbox.QuicDataAPI;
}

test('demo package detail carries admission grouping for review', async () => {
  const api = createDemoApi();
  await api.login('demo@local.preview', 'x');
  const detail = await api.getDataPackage(5102, 1);
  assert.ok(detail.admission_counts, 'admission_counts missing');
  assert.ok(detail.admission_counts.failed > 0, 'demo needs a failed episode for review');
  assert.ok(Array.isArray(detail.episodes) && detail.episodes.length >= 3);
  const failed = detail.episodes.find((item) => item.admission_status === 'failed');
  assert.ok(failed && failed.admission_reason, 'failed episode must carry a reason');
  const sensitive = detail.episodes.find((item) => item.privacy_sensitive === true);
  assert.ok(sensitive, 'demo needs a privacy-sensitive episode');
  assert.ok(detail.intake_review === null || detail.intake_review.verdict, 'intake_review shape ok');
});

test('demo intake review accepts rejected episode ids', async () => {
  const api = createDemoApi();
  await api.login('demo@local.preview', 'x');
  const detail = await api.getDataPackage(5102, 1);
  const rejected = Array.from(detail.episodes.slice(0, 1)).map((item) => item.id);
  const result = await api.reviewIntakePackage(5102, { workspace_id: 1, verdict: 'approved', rejected_episode_ids: rejected });
  assert.equal(result.status, 'intake_approved');
  assert.deepEqual(Array.from(result.rejected_episode_ids || []).map(Number).sort(), rejected.map(Number).sort());
});

test('openPackageIntakeReview routes to the full-screen review page instead of the drawer', () => {
  assert.match(appSource, /function openPackageIntakeReview\(row\) \{[\s\S]*openIntakeReview\(row\);/);
  assert.doesNotMatch(appSource, /function openPackageIntakeReview\(row\) \{[\s\S]*openDataPackageDrawer\(target\);/);
  assert.match(workbenchSource, /state\.package\.canReview|canReview/);
  assert.match(appSource, /intakeReviewNotReady: '该数据包尚未进入可审核状态/);
  assert.match(appSource, /intakeReviewNotReady: 'This data package is not reviewable yet/);
});
