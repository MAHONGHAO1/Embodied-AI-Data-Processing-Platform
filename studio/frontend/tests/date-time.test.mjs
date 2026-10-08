import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../js/date-time.js', import.meta.url), 'utf8');

function loadDateTime() {
  const context = { Intl, Date };
  context.globalThis = context;
  vm.runInNewContext(`${source}\n;globalThis.__dateTime = QuicDataDateTime;`, context, {
    filename: 'date-time.js',
  });
  return context.__dateTime;
}

test('date-time formatting keeps compact cells and a complete accessible value', () => {
  const dateTime = loadDateTime();
  const value = '2026-08-26T07:09:03Z';

  assert.equal(
    dateTime.compact(value, { locale: 'en-US', timeZone: 'Asia/Shanghai' }),
    '2026-08-26 15:09',
  );
  assert.equal(
    dateTime.full(value, { locale: 'en-US', timeZone: 'Asia/Shanghai' }),
    '2026-08-26 15:09:03 GMT+8',
  );
  assert.equal(dateTime.compact('not-a-date'), '');
  assert.equal(dateTime.full(null), '');
});

test('date-time formats arbitrary IANA zones without changing the instant', () => {
  const dateTime = loadDateTime();
  const value = '2026-08-26T07:09:03Z';

  assert.equal(
    dateTime.compact(value, { locale: 'en-US', timeZone: 'UTC' }),
    '2026-08-26 07:09',
  );
});
