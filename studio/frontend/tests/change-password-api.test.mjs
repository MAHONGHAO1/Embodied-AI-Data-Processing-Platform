import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const apiSource = readFileSync(new URL('../js/api.js', import.meta.url), 'utf8');

test('password changes use the authenticated same-origin auth endpoint', async () => {
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
      const data = calls.length === 1
        ? { token: 'access-token', userInfo: { id: 7, must_change_password: true } }
        : { ok: true };
      return { ok: true, status: 200, async json() { return { code: 200, data }; } };
    },
  };
  context.globalThis = context;
  vm.runInNewContext(`${apiSource}\n;globalThis.__api = QuicDataAPI;`, context, { filename: 'api.js' });

  await context.__api.login('uat-admin@example.com', 'temporary-password');
  await context.__api.changePassword({ old_password: 'temporary-password', new_password: 'a-new-password' });

  assert.equal(calls[1].url, 'http://localhost:8010/api/v1/auth/change-password');
  assert.equal(calls[1].options.headers.Authorization, 'Bearer access-token');
  assert.deepEqual(JSON.parse(calls[1].options.body), {
    old_password: 'temporary-password',
    new_password: 'a-new-password',
  });
});

test('structured validation errors retain their human-readable server message', async () => {
  const context = {
    URL,
    window: { location: { origin: 'http://localhost:8010' } },
    sessionStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
    async fetch() {
      return {
        ok: false,
        status: 422,
        async json() {
          return {
            detail: [{ type: 'string_too_short', msg: 'String should have at least 10 characters' }],
          };
        },
      };
    },
  };
  context.globalThis = context;
  vm.runInNewContext(`${apiSource}\n;globalThis.__api = QuicDataAPI;`, context, { filename: 'api.js' });

  await assert.rejects(
    () => context.__api.changePassword({ old_password: 'old-password', new_password: '123456789' }),
    /String should have at least 10 characters/,
  );
});

test('logout keeps local authentication when the server does not confirm revocation', async () => {
  const calls = [];
  const context = {
    URL,
    window: { location: { origin: 'http://localhost:8010' } },
    async fetch(url) {
      calls.push(String(url));
      if (calls.length === 1) {
        return {
          ok: true,
          status: 200,
          async json() {
            return { code: 200, data: { token: 'access-token', userInfo: { id: 7 } } };
          },
        };
      }
      throw new Error('network unavailable');
    },
  };
  context.globalThis = context;
  vm.runInNewContext(`${apiSource}\n;globalThis.__api = QuicDataAPI;`, context, { filename: 'api.js' });
  await context.__api.login('uat-admin@example.com', 'temporary-password');

  await assert.rejects(() => context.__api.logout(), /注销未完成/);
  assert.equal(context.__api.currentUser().id, 7);
  assert.equal(context.__api.getAccessToken(), 'access-token');
});
