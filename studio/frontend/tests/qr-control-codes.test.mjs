import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const vendorSource = readFileSync(new URL('../vendor/qrcode-generator/1.5.0/qrcode.js', import.meta.url), 'utf8');
const source = readFileSync(new URL('../js/qr-control-codes.js', import.meta.url), 'utf8');

function loadQrCodes() {
  const context = { JSON, String, Number, Object, Array, RegExp, encodeURIComponent };
  context.globalThis = context;
  vm.runInNewContext(vendorSource, context, { filename: 'qrcode.js' });
  vm.runInNewContext(`${source}\n;globalThis.__qrCodes = QuicDataQrControlCodes;`, context, {
    filename: 'qr-control-codes.js',
  });
  return context.__qrCodes;
}

test('QR control payloads exactly match the quic_ego decoder contract', () => {
  const qrCodes = loadQrCodes();
  const codes = qrCodes.forCollector({ profile_key: '0042', name: 'Zhao Yang' });

  assert.deepEqual(JSON.parse(codes.control.payload), {
    v: 1,
    kind: 'control',
    collector_id: '0042',
  });
  assert.deepEqual(JSON.parse(codes.segment.payload), {
    v: 1,
    kind: 'segment',
    collector_id: '0042',
    segment_id: 'manual_boundary',
  });
  assert.equal(codes.segment.segment_id, 'manual_boundary');
});

test('QR control codes reject missing or non-numeric collector identifiers', () => {
  const qrCodes = loadQrCodes();

  assert.equal(qrCodes.forCollector({ profile_key: '' }), null);
  assert.equal(qrCodes.forCollector({ profile_key: '000x' }), null);
  assert.equal(qrCodes.forCollector({ profile_key: 'abc' }), null);
  assert.ok(qrCodes.forCollector({ profile_key: '42' }), 'short numeric IDs should be accepted');
  assert.ok(qrCodes.forCollector({ profile_key: '12345' }), 'long numeric IDs should be accepted');
});

test('QR rendering is delegated to the pinned encoder and is scan-ready SVG', () => {
  const qrCodes = loadQrCodes();
  const codes = qrCodes.forCollector({ profile_key: '0042' });
  const svg = qrCodes.svg(codes.control.payload, { alt: 'control code', title: 'Start or stop' });

  assert.match(svg, /^<svg\b/);
  assert.match(svg, /<path\b/);
  assert.match(svg, /viewBox=/);
});
