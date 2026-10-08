import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const apiSource = readFileSync(new URL('../js/api.js', import.meta.url), 'utf8');
const appSource = readFileSync(new URL('../js/app.js', import.meta.url), 'utf8');

function loadApi(responses = []) {
  const calls = [];
  const session = new Map();
  const context = {
    URL,
    window: { location: { origin: 'http://localhost:8010' } },
    sessionStorage: {
      getItem(key) { return session.get(key) || null; },
      setItem(key, value) { session.set(key, String(value)); },
      removeItem(key) { session.delete(key); },
    },
    async fetch(url, options = {}) {
      calls.push({ url: String(url), options });
      const payload = responses.shift() || { code: 200, data: {} };
      return { ok: true, status: 200, async json() { return payload; } };
    },
  };
  context.globalThis = context;
  vm.runInNewContext(`${apiSource}\n;globalThis.__api = QuicDataAPI;`, context, { filename: 'api.js' });
  return { api: context.__api, calls };
}

test('annotation and review work-item APIs support list submit return and reassign', async () => {
  for (const method of [
    'listAnnotationWorkItems',
    'submitAnnotationWorkItem',
    'reassignAnnotationWorkItem',
    'listReviewWorkItems',
    'approveReviewWorkItem',
    'returnReviewWorkItem',
    'reassignReviewWorkItem',
  ]) {
    assert.match(apiSource, new RegExp(`\\b${method}\\s*\\(`));
  }

  const { api, calls } = loadApi([
    { code: 200, data: { token: 't', userInfo: { id: 1 } } },
    { code: 200, data: { items: [] } },
    { code: 200, data: { id: 1 } },
    { code: 200, data: { items: [] } },
    { code: 200, data: { id: 2 } },
    { code: 200, data: { id: 2 } },
  ]);
  await api.login('a@b.c', 'x');
  await api.listAnnotationWorkItems({ workspace_id: 4 });
  await api.reassignAnnotationWorkItem(11, { workspace_id: 4, to_user_id: 8, reason: 'handoff' });
  await api.listReviewWorkItems({ workspace_id: 4 });
  await api.returnReviewWorkItem(22, { workspace_id: 4, reason: 'fix' });
  await api.reassignReviewWorkItem(22, { workspace_id: 4, to_user_id: 9, reason: 'load' });

  assert.match(calls[1].url, /\/annotation-work-items\?workspace_id=4/);
  assert.match(calls[2].url, /\/annotation-work-items\/11\/reassign/);
  assert.match(calls[3].url, /\/review-work-items\?workspace_id=4/);
  assert.match(calls[4].url, /\/review-work-items\/22\/return/);
  assert.match(calls[5].url, /\/review-work-items\/22\/reassign/);
});

test('annotation queue drops claim/release and shows assignee return reason and reassign', () => {
  assert.match(appSource, /listAnnotationWorkItems|loadAnnotationWorkItems/);
  assert.match(appSource, /listReviewWorkItems|loadReviewWorkItems/);
  assert.match(appSource, /reassignAnnotationWorkItem|reassignReviewWorkItem/);
  assert.match(appSource, /@click.stop="openPackageWorkbench\(scope\.row\)"/);
  assert.doesNotMatch(appSource, /submitGovernanceAnnotationItem|approveGovernanceReviewItem|returnGovernanceReviewItem/);
  assert.match(appSource, /assignee_user_id/);
  assert.match(appSource, /return_reason:\s*item\.return_reason/);
  assert.match(appSource, /reassign_reason|\.reason/);
  assert.match(appSource, /governanceAnnotationQueueRows/);
  assert.match(appSource, /actionReassign/);
  assert.match(appSource, /returnReason|return_reason/);
  assert.match(appSource, /queueStage === 'annotation' \|\| queueStage === 'review'/);
  assert.match(appSource, /openQueueStage\('review'\)/);
  assert.doesNotMatch(appSource, /function toggleImportAnnotation\(/);
  assert.doesNotMatch(appSource, /toggleImportAnnotation\(scope\.row\)/);
  assert.doesNotMatch(
    appSource,
    /governanceAnnotationQueueRows[\s\S]{0,1200}actionClaim|governanceAnnotationQueueRows[\s\S]{0,1200}actionRelease/,
  );
});

test('data annotation stage switch hides the split button in every mode', () => {
  const options = appSource.match(/const queueStageOptions = computed\(\(\) => \[([\s\S]*?)\]\);/)?.[1] || '';
  assert.doesNotMatch(options, /value: 'cut'/);
  assert.match(options, /value: 'annotation'/);
  assert.match(options, /value: 'review'/);
});

test('review queue surfaces algorithm source and low-confidence filter', () => {
  assert.match(appSource, /reviewFilterState/);
  assert.match(appSource, /source\.kind === 'algorithm'/);
  assert.match(appSource, /confidence.*threshold/);
  assert.match(appSource, /lowConfidence/);
});
