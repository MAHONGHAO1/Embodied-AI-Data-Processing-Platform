import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../js/cut-workbench.js', import.meta.url), 'utf8');

function loadCutWorkbench() {
  const context = {};
  context.globalThis = context;
  vm.runInNewContext(`${source}\n;globalThis.__cut = QuicDataCutWorkbench;`, context);
  return context.__cut;
}

function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

test('initialization uses QR suggestions or one full included source window', () => {
  const cut = loadCutWorkbench();
  const bounds = { start_ns: '1000000000', end_ns: '100000000000' };

  assert.deepEqual(plain(cut.initialize(bounds, [])), [
    {
      id: 'cut-1',
      start_ns: '1000000000',
      end_ns: '100000000000',
      eligibility: 'included',
    },
  ]);
  assert.deepEqual(plain(cut.initialize(bounds, [
    {
      timestamp_ns: '30000000000',
      segment_id_hint: 'qr-1',
      segment_index: 1,
      protocol_version: 1,
    },
    {
      timestamp_ns: '70000000000',
      segment_id_hint: 'qr-2',
      segment_index: 2,
      protocol_version: 1,
    },
  ])), [
    {
      id: 'cut-1',
      start_ns: '1000000000',
      end_ns: '30000000000',
      eligibility: 'included',
      boundary_after: {
        origin: 'qr_event',
        suggested_timestamp_ns: '30000000000',
        adjusted: false,
        segment_id_hint: 'qr-1',
        segment_index: 1,
        protocol_version: 1,
      },
    },
    {
      id: 'cut-2',
      start_ns: '30000000000',
      end_ns: '70000000000',
      eligibility: 'included',
      boundary_after: {
        origin: 'qr_event',
        suggested_timestamp_ns: '70000000000',
        adjusted: false,
        segment_id_hint: 'qr-2',
        segment_index: 2,
        protocol_version: 1,
      },
    },
    {
      id: 'cut-3',
      start_ns: '70000000000',
      end_ns: '100000000000',
      eligibility: 'included',
    },
  ]);
});

test('QR boundaries are movable and only the selected QR boundary can be restored', () => {
  const cut = loadCutWorkbench();
  const initial = cut.initialize(
    { start_ns: '0', end_ns: '60000000000' },
    [{
      timestamp_ns: '30000000000',
      segment_id_hint: 'qr-1',
      segment_index: 1,
      protocol_version: 1,
    }],
  );

  const moved = cut.moveBoundary(initial, 0, '35000000000');
  assert.equal(moved[0].end_ns, '35000000000');
  assert.equal(moved[1].start_ns, '35000000000');
  assert.equal(moved[0].boundary_after.adjusted, true);

  const restored = cut.restoreQrBoundary(moved, 0);
  assert.equal(restored[0].end_ns, '30000000000');
  assert.equal(restored[0].boundary_after.adjusted, false);

  const inserted = cut.insertBoundary(restored, '45000000000');
  assert.equal(inserted[1].boundary_after.origin, 'human');
  assert.equal(cut.restoreQrBoundary(inserted, 1), null);
  assert.equal(cut.restoreQrBoundary(inserted, 0)[0].end_ns, '30000000000');
});

test('inserting and removing human boundaries preserves a contiguous auditable plan', () => {
  const cut = loadCutWorkbench();
  const original = cut.initialize({ start_ns: '0', end_ns: '60000000000' }, []);
  const inserted = cut.insertBoundary(original, '20000000000');

  assert.deepEqual(plain(inserted), [
    {
      id: 'cut-1',
      start_ns: '0',
      end_ns: '20000000000',
      eligibility: 'included',
      boundary_after: { origin: 'human' },
    },
    {
      id: 'cut-2',
      start_ns: '20000000000',
      end_ns: '60000000000',
      eligibility: 'included',
    },
  ]);
  assert.deepEqual(plain(cut.removeBoundary(inserted, 0)), plain(original));
});

test('eligibility shortcuts are idempotent and excluded windows may exceed five minutes', () => {
  const cut = loadCutWorkbench();
  const bounds = { start_ns: '0', end_ns: '600000000000' };
  const original = cut.initialize(bounds, []);

  const excluded = cut.setEligibility(original, 0, 'excluded');
  assert.deepEqual(plain(cut.setEligibility(excluded, 0, 'excluded')), plain(excluded));
  assert.equal(cut.validateForSave(excluded, bounds), true);
  assert.equal(cut.validateForSubmit(excluded, bounds), true);

  const included = cut.setEligibility(excluded, 0, 'included');
  assert.deepEqual(plain(cut.setEligibility(included, 0, 'included')), plain(included));
  assert.equal(cut.validateForSave(included, bounds), true);
  assert.equal(cut.validateForSubmit(included, bounds), false);
});

test('local precision range stays within fifteen seconds of the selected boundary', () => {
  const cut = loadCutWorkbench();
  const segments = cut.initialize(
    { start_ns: '1000000000', end_ns: '100000000000' },
    [{
      timestamp_ns: '10000000000',
      segment_id_hint: 'qr-1',
      segment_index: 1,
      protocol_version: 1,
    }],
  );

  assert.deepEqual(plain(cut.localRange(
    segments,
    0,
    { start_ns: '1000000000', end_ns: '100000000000' },
    15,
  )), {
    start_ns: '1000000000',
    end_ns: '25000000000',
    boundary_ns: '10000000000',
  });
  assert.equal(cut.localRange(segments, 1, { start_ns: '0', end_ns: '10' }, 15), null);
});

test('local precision range exposes every boundary in the visible window', () => {
  const cut = loadCutWorkbench();
  const bounds = { start_ns: '0', end_ns: '30000000000' };
  let segments = cut.initialize(bounds, [{
    timestamp_ns: '5000000000',
    segment_id_hint: 'qr-1',
    segment_index: 1,
    protocol_version: 1,
  }]);
  segments = cut.insertBoundary(segments, '9000000000');
  segments = cut.insertBoundary(segments, '18000000000');
  const range = cut.localRange(segments, 1, bounds, 15);

  assert.deepEqual(plain(cut.localBoundaries(segments, range, 1)), [
    {
      index: 0,
      timestamp_ns: '5000000000',
      selected: false,
      origin: 'qr_event',
      adjusted: false,
    },
    {
      index: 1,
      timestamp_ns: '9000000000',
      selected: true,
      origin: 'human',
      adjusted: false,
    },
    {
      index: 2,
      timestamp_ns: '18000000000',
      selected: false,
      origin: 'human',
      adjusted: false,
    },
  ]);
  assert.deepEqual(plain(cut.localBoundaries(segments, { start_ns: '6000000000', end_ns: '12000000000' }, 1)), [
    {
      index: 1,
      timestamp_ns: '9000000000',
      selected: true,
      origin: 'human',
      adjusted: false,
    },
  ]);
  assert.deepEqual(plain(cut.localBoundaries(segments, null, 1)), []);
});

test('overlappingRange and indexContaining use binary search over contiguous windows', () => {
  const cut = loadCutWorkbench();
  const segments = [];
  for (let index = 0; index < 404; index += 1) {
    const start = 1_000_000_000 + index * 4_000_000_000;
    segments.push({
      id: `cut-${index + 1}`,
      start_ns: String(start),
      end_ns: String(start + 4_000_000_000),
      eligibility: 'included',
      ...(index < 403 ? { boundary_after: { origin: 'human' } } : {}),
    });
  }

  const firstWindow = cut.overlappingRange(segments, '1000000000', '9000000000', 0);
  assert.equal(firstWindow.startIndex, 0);
  assert.equal(firstWindow.endIndex, 2);
  const midWindow = cut.overlappingRange(segments, '810000000000', '821000000000', 1);
  assert.equal(midWindow.startIndex, 201);
  assert.equal(midWindow.endIndex, 206);
  assert.equal(cut.indexContaining(segments, '1000000000'), 0);
  assert.equal(cut.indexContaining(segments, '5000000000'), 1);
  assert.equal(cut.indexContaining(segments, segments[403].end_ns), 403);
  assert.equal(cut.nearestBoundaryIndex(segments, '810000000000'), 201);
  assert.equal(cut.overlappingRange([], '1', '2').endIndex, 0);
});
