import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../js/preview-timeline.js', import.meta.url), 'utf8');

function loadTimeline() {
  const context = {};
  context.globalThis = context;
  vm.runInNewContext(`${source}\n;globalThis.__timeline = QuicDataPreviewTimeline;`, context);
  return context.__timeline;
}

test('playback seconds map through the exact source frame timeline', () => {
  const timeline = loadTimeline();
  const preview = {
    encoded_fps: 2,
    frame_timestamps_ns: ['1000000000', '1100000000', '4000000000', '9000000000'],
  };

  assert.equal(timeline.sourceTimestampAt(0, preview), '1000000000');
  assert.equal(timeline.sourceTimestampAt(0.5, preview), '1100000000');
  assert.equal(timeline.sourceTimestampAt(1, preview), '4000000000');
  assert.equal(timeline.sourceTimestampAt(1.5, preview), '9000000000');
  assert.equal(timeline.sourceTimestampAt(999, preview), '9000000000');
});

test('invalid or missing preview mappings fail closed', () => {
  const timeline = loadTimeline();

  assert.equal(timeline.sourceTimestampAt(1, null), null);
  assert.equal(timeline.sourceTimestampAt(1, { encoded_fps: 0, frame_timestamps_ns: ['1'] }), null);
  assert.equal(timeline.sourceTimestampAt(1, { encoded_fps: 15, frame_timestamps_ns: [] }), null);
  assert.equal(
    timeline.sourceTimestampAt(1, { encoded_fps: 15, frame_timestamps_ns: ['2', '1'] }),
    null,
  );
});

test('preview timeline exposes no automatic cut partitioning policy', () => {
  const timeline = loadTimeline();
  assert.equal(typeof timeline.partitionBounds, 'undefined');
  assert.equal(typeof timeline.isValidPartition, 'undefined');
});

test('source timestamps can seek the compressed constant-fps preview', () => {
  const timeline = loadTimeline();
  const preview = {
    encoded_fps: 2,
    frame_timestamps_ns: ['1000000000', '1100000000', '4000000000', '9000000000'],
  };

  assert.equal(timeline.playbackSecondsAt('4000000000', preview), 1);
  assert.equal(timeline.playbackSecondsAt('8000000000', preview), 1.5);
  assert.equal(timeline.nearestSourceTimestamp('7000000000', preview, '4000000000', '9000000000'), '9000000000');
});

test('preview mapping is reused for the same preview object', () => {
  const timeline = loadTimeline();
  const timestamps = [];
  for (let index = 0; index < 8000; index += 1) timestamps.push(String(1_000_000_000 + index * 10_000_000));
  const preview = { encoded_fps: 30, encoded_duration_s: 8, frame_timestamps_ns: timestamps };

  assert.equal(timeline.sourceTimestampAt(0, preview), timestamps[0]);
  assert.equal(timeline.playbackSecondsAt(timestamps[4000], preview), 4000 / 7999 * 8);
  assert.equal(timeline.nearestSourceTimestamp(timestamps[1234], preview, timestamps[1000], timestamps[2000]), timestamps[1234]);
  assert.equal(timeline.sourceTimestampAt(4, preview), timeline.sourceTimestampAt(4, preview));
});
