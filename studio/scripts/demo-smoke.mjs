#!/usr/bin/env node
// Pre-demo smoke check: starts the local mock preview, walks every console view
// in headless Chrome, and fails when a view renders an empty state or logs an
// error. Run with `make demo-check`.

import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import net from 'node:net';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { setTimeout as sleep } from 'node:timers/promises';

const ROOT = resolve(new URL('..', import.meta.url).pathname);
const CHROME_CANDIDATES = [
  process.env.CHROME_BIN,
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/Applications/Chromium.app/Contents/MacOS/Chromium',
  '/usr/bin/google-chrome',
  '/usr/bin/chromium',
  '/usr/bin/chromium-browser',
].filter(Boolean);

const VIEWS = [
  'overview', 'intake', 'batches', 'work-queue', 'intake-review', 'resources', 'assets', 'datasets',
  'miningDash', 'miningTasks', 'miningConfig',
  'trainDash', 'trainJobs', 'trainNew', 'trainDatasets', 'trainModels', 'trainResources', 'trainSystem',
  'admin', 'settings',
];

function findChrome() {
  const found = CHROME_CANDIDATES.find((candidate) => existsSync(candidate));
  if (!found) {
    console.error('demo-smoke: no Chrome/Chromium binary found; set CHROME_BIN=<path>');
    process.exit(2);
  }
  return found;
}

async function freePort() {
  return new Promise((resolvePort) => {
    const server = net.createServer();
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address();
      server.close(() => resolvePort(port));
    });
  });
}

async function waitForPort(port, timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      await fetch(`http://127.0.0.1:${port}/`);
      return true;
    } catch {
      await sleep(150);
    }
  }
  return false;
}

async function chromeTarget(port) {
  const deadline = Date.now() + 20000;
  while (Date.now() < deadline) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
      const page = list.find((target) => target.type === 'page');
      if (page) return page;
    } catch {
      // keep waiting for the devtools endpoint
    }
    await sleep(200);
  }
  throw new Error('devtools endpoint did not become available');
}

const previewPort = await freePort();
const debugPort = await freePort();
const profileDir = join(tmpdir(), `quicstudio-demo-smoke-${Date.now()}`);

const preview = spawn('python3', [
  'frontend/serve_preview.py',
  '--host', '127.0.0.1',
  '--port', String(previewPort),
  '--backend', 'http://127.0.0.1:8000',
  '--mock',
], { cwd: ROOT, stdio: 'ignore' });

const chrome = spawn(findChrome(), [
  '--headless=new',
  `--remote-debugging-port=${debugPort}`,
  `--user-data-dir=${profileDir}`,
  '--no-first-run',
  '--no-default-browser-check',
  '--disable-gpu',
  '--window-size=1600,1000',
  'about:blank',
], { stdio: 'ignore' });

let socket;
let nextId = 1;
const pending = new Map();
const consoleErrors = [];

function send(method, params = {}) {
  const id = nextId += 1;
  return new Promise((resolveCall) => {
    pending.set(id, resolveCall);
    socket.send(JSON.stringify({ id, method, params }));
  });
}

async function evaluate(expression) {
  const result = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
  return result.result?.result?.value;
}

function cleanup() {
  try { socket?.close(); } catch { /* ignore */ }
  try { chrome.kill(); } catch { /* ignore */ }
  try { preview.kill(); } catch { /* ignore */ }
}

try {
  if (!await waitForPort(previewPort)) throw new Error('preview server did not start');
  const page = await chromeTarget(debugPort);
  socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolveSocket, rejectSocket) => {
    socket.onopen = resolveSocket;
    socket.onerror = rejectSocket;
  });
  socket.onmessage = (event) => {
    const message = JSON.parse(event.data);
    if (message.id && pending.has(message.id)) {
      pending.get(message.id)(message);
      pending.delete(message.id);
      return;
    }
    if (message.method === 'Runtime.consoleAPICalled' && message.params.type === 'error') {
      consoleErrors.push(message.params.args.map((arg) => arg.value ?? arg.description ?? arg.type).join(' ').slice(0, 200));
    }
    if (message.method === 'Runtime.exceptionThrown') {
      consoleErrors.push(`EXCEPTION ${message.params.exceptionDetails.text}`.slice(0, 200));
    }
  };

  await send('Runtime.enable');
  await send('Page.enable');

  const failures = [];
  for (const view of VIEWS) {
    consoleErrors.length = 0;
    const target = view === 'intake-review' ? '#/intake-review?package_id=5102' : `#/${view}`;
    await send('Page.navigate', { url: `http://127.0.0.1:${previewPort}/?demo=1${target}` });
    await sleep(2200);
    const probe = JSON.parse(await evaluate(`(() => {
      const empties = [...document.querySelectorAll('.el-empty__description')].map((node) => node.textContent.trim());
      return JSON.stringify({
        rows: document.querySelectorAll('.el-table__row').length,
        cards: document.querySelectorAll('.metric-card').length,
        empties: [...new Set(empties)],
      });
    })()`));
    const empty = probe.empties.length > 0;
    const bare = !probe.rows && !probe.cards;
    const errored = consoleErrors.length > 0;
    const status = empty || errored ? 'FAIL' : (bare ? 'WARN' : 'ok');
    if (status === 'FAIL') {
      failures.push(`${view}: ${empty ? `empty state ${JSON.stringify(probe.empties)}` : ''}${errored ? ` console ${JSON.stringify(consoleErrors.slice(0, 2))}` : ''}`);
    }
    console.log(`${status.padEnd(4)} ${view.padEnd(14)} rows=${probe.rows} cards=${probe.cards}${probe.empties.length ? ` empties=${JSON.stringify(probe.empties)}` : ''}`);
  }

  if (failures.length) {
    console.error(`\ndemo-smoke: ${failures.length} view(s) failed`);
    for (const failure of failures) console.error(`  - ${failure}`);
    cleanup();
    process.exit(1);
  }
  console.log('\ndemo-smoke: all views rendered demo data');
  cleanup();
} catch (error) {
  console.error(`demo-smoke: ${error.message}`);
  cleanup();
  process.exit(1);
}
