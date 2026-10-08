import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../js/asset-lineage.js', import.meta.url), 'utf8');

function loadLineage() {
  const context = {};
  context.globalThis = context;
  vm.runInNewContext(`${source}\n;globalThis.__lineage = QuicDataAssetLineage;`, context);
  return context.__lineage;
}

function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

function episode(id, kind, parentId, overrides = {}) {
  return {
    id,
    episode_uid: `${kind}-${id}`,
    kind,
    parent_episode_id: parentId,
    quality: { status: 'passed' },
    annotation_status: kind === 'source' ? 'pending' : 'submitted',
    review_status: kind === 'source' ? 'not_applicable' : 'pending',
    publication: { category: 'unpublished' },
    ...overrides,
  };
}

test('groups derived Episodes under their source and summarizes the complete snapshot', () => {
  const lineage = loadLineage();
  const items = [
    episode(1, 'source', null),
    episode(2, 'derived', 1, {
      review_status: 'accepted',
      publication: { category: 'published' },
    }),
    episode(3, 'derived', 1),
    episode(4, 'derived', 1, {
      quality: { status: 'failed' },
      review_status: 'rejected',
      publication: { category: 'failed' },
    }),
  ];

  const groups = lineage.buildGroups(items);

  assert.equal(groups.length, 1);
  assert.equal(groups[0].root.episode_uid, 'source-1');
  assert.deepEqual(plain(groups[0].summary), {
    descendants: 3,
    quality_failed: 1,
    review: { accepted: 1, pending: 1, rejected: 1 },
    publication: { failed: 1, published: 1, unpublished: 1 },
  });
  assert.deepEqual(plain(groups[0].descendants.map((row) => row.episode_uid)), [
    'derived-2',
    'derived-3',
    'derived-4',
  ]);
});

test('source roots are collapsed by default and expand without mutating input', () => {
  const lineage = loadLineage();
  const items = [episode(1, 'source', null), episode(2, 'derived', 1)];
  const original = JSON.stringify(items);
  const groups = lineage.buildGroups(items);

  assert.deepEqual(plain(lineage.visibleRows(groups, new Set()).map((row) => row.id)), [1]);
  assert.deepEqual(plain(lineage.visibleRows(groups, new Set([1])).map((row) => row.id)), [1, 2]);
  assert.equal(JSON.stringify(items), original);
});

test('a matching derived filter keeps and auto-expands its source context', () => {
  const lineage = loadLineage();
  const groups = lineage.buildGroups([
    episode(1, 'source', null),
    episode(2, 'derived', 1, { review_status: 'accepted' }),
    episode(3, 'derived', 1, { review_status: 'rejected' }),
  ], { review_status: 'accepted' });

  assert.equal(groups.length, 1);
  assert.equal(groups[0].root.id, 1);
  assert.equal(groups[0].auto_expand, true);
  assert.deepEqual(plain(groups[0].descendants.map((row) => row.id)), [2]);
  assert.deepEqual(plain(lineage.visibleRows(groups, new Set()).map((row) => row.id)), [1, 2]);
  assert.deepEqual(plain(groups[0].summary.review), { accepted: 1, rejected: 1 });
});

test('multi-level lineage preserves matching ancestor context and bounded expansion', () => {
  const lineage = loadLineage();
  const groups = lineage.buildGroups([
    episode(1, 'source', null),
    episode(2, 'derived', 1, { review_status: 'pending' }),
    episode(3, 'derived', 2, {
      review_status: 'accepted',
      publication: { category: 'published' },
    }),
  ], { publication_status: 'published' });

  assert.deepEqual(plain(groups[0].descendants.map((row) => row.id)), [2, 3]);
  assert.deepEqual(plain(lineage.visibleRows(groups, new Set()).map((row) => [row.id, row.asset_depth])), [
    [1, 0],
    [2, 1],
    [3, 2],
  ]);
});

test('orphaned and cyclic derived Episodes remain observable as flagged roots', () => {
  const lineage = loadLineage();
  const groups = lineage.buildGroups([
    episode(7, 'derived', 999),
    episode(8, 'derived', 9),
    episode(9, 'derived', 8),
  ]);

  assert.deepEqual(plain(groups.map((group) => [group.root.id, group.lineage_issue])), [
    [7, 'missing_parent'],
    [8, 'cycle'],
    [9, 'cycle'],
  ]);
  assert.deepEqual(plain(lineage.visibleRows(groups, new Set()).map((row) => row.id)), [7, 8, 9]);
});

test('unknown filters fail closed instead of broadening the asset snapshot', () => {
  const lineage = loadLineage();
  const items = [episode(1, 'source', null), episode(2, 'derived', 1)];

  assert.deepEqual(plain(lineage.buildGroups(items, { review_status: 'deleted' })), []);
  assert.deepEqual(plain(lineage.buildGroups(items, { kind: 'archive' })), []);
});

test('lineage deeper than the bounded limit is detached but remains observable', () => {
  const lineage = loadLineage();
  const items = [episode(1, 'source', null)];
  for (let id = 2; id <= 40; id += 1) items.push(episode(id, 'derived', id - 1));

  const groups = lineage.buildGroups(items);

  assert.equal(groups[0].summary.descendants, 32);
  assert.ok(groups.some((group) => group.lineage_issue === 'depth_exceeded'));
  assert.equal(groups.reduce((total, group) => total + group.summary.descendants + 1, 0), 40);
});

test('dataset selection preview keeps selected Episodes and their ancestor hierarchy', () => {
  const lineage = loadLineage();
  const items = [
    episode(1, 'source', null),
    episode(2, 'derived', 1),
    episode(3, 'derived', 2),
    episode(4, 'derived', 1),
  ];

  const rows = plain(lineage.selectionRows(items, [3, 4]));

  assert.deepEqual(rows.map((row) => [row.id, row.asset_depth, row.asset_selected]), [
    [1, 0, false],
    [2, 1, false],
    [3, 2, true],
    [4, 1, true],
  ]);
});
