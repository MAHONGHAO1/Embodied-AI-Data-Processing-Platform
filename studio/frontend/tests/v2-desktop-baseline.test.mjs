import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const appSource = readFileSync(new URL('../js/app.js', import.meta.url), 'utf8');
const cssSource = readFileSync(new URL('../css/app.css', import.meta.url), 'utf8');

test('v2 desktop containers use border-box sizing without page overflow', () => {
  assert.match(cssSource, /\*, \*::before, \*::after\s*\{[^}]*box-sizing:\s*border-box/);
  assert.match(cssSource, /\.view-stack\s*\{[^}]*width:\s*100%/);
  assert.doesNotMatch(cssSource, /dataset-revision-builder|dataset-revision-/);
});

test('v2 keeps business scope below the desktop header', () => {
  const header = appSource.match(/<header class="console-header">([\s\S]*?)<\/header>/);
  assert.ok(header, 'expected the global console header');
  assert.doesNotMatch(header[1], /v-model="selectedWorkspaceId"/);
  assert.match(header[1], /class="header-actions"/);
  assert.match(appSource, /class="scope-toolbar page-scope-toolbar"/);
});

test('v2 desktop workbench supports resizable editor and fixed timeline', () => {
  assert.match(appSource, /--workbench-editor-width': workbenchEditorWidth \+ 'px'/);
  assert.match(appSource, /class="workbench-split-handle"/);
  assert.match(appSource, /function startWorkbenchSplitResize/);
  assert.match(appSource, /class="surface-panel studio-timeline"/);
  assert.match(appSource, /class="timeline-ruler"/);
  assert.match(appSource, /function startPlayheadDrag/);
  assert.match(
    cssSource,
    /grid-template-columns:\s*minmax\(360px, 1fr\) 10px minmax\(320px, var\(--workbench-editor-width, 420px\)\)/,
  );
  assert.match(cssSource, /\.studio-timeline[^}]*position:\s*fixed/);
});

test('v2 desktop media stage is a fixed 4:3 frame and preserves the complete source frame', () => {
  assert.match(cssSource, /\.workbench-media-body[^}]*container-type:\s*size/);
  assert.match(cssSource, /\.workbench-video-stage[^}]*width:\s*min\(100cqw,\s*133\.333cqh\)/);
  assert.match(cssSource, /\.workbench-video-stage[^}]*aspect-ratio:\s*4\s*\/\s*3/);
  assert.match(cssSource, /\.workbench-video[^}]*object-fit:\s*contain/);
});

test('v2 data intake is an operational flow distinct from batch management', () => {
  const intake = appSource.match(
    /<section v-else-if="activeView === 'intake'"([\s\S]*?)<section v-else-if="activeView === 'batches'"/,
  );
  assert.ok(intake, 'expected a distinct intake section');
  assert.match(intake[1], /<data-overview/);
  const overview = readFileSync(new URL('../js/data-overview.js', import.meta.url), 'utf8');
  assert.match(overview, /intake-tabs-bar/);
  assert.match(overview, /name="packages"/);
  assert.match(overview, /name="batches"/);
  assert.match(intake[1], /openPackageBatchDialog/);
  assert.match(overview, /state\.items/);
  assert.doesNotMatch(appSource, /function selectIntakeBatch\(/);
  assert.doesNotMatch(appSource, /function startIntake\(/);
});
