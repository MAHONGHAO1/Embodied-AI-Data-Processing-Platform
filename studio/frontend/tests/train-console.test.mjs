import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../js/train-console.js', import.meta.url), 'utf8');

function load({ api = null, Vue = null, window = null } = {}) {
  const context = { __api: api };
  if (Vue) context.Vue = Vue;
  if (window) context.window = window;
  context.globalThis = context;
  vm.runInNewContext(`const QuicDataAPI = globalThis.__api;\n${source}\n;globalThis.__train = QuicTrainConsole;`, context);
  return context.__train;
}

function createVueShim() {
  const mounted = [];
  return {
    reactive(value) { return value; },
    ref(value) { return { value }; },
    computed(getter) { return { get value() { return getter(); } }; },
    onMounted(callback) { mounted.push(callback); },
    watch() {},
    async mount() {
      for (const callback of mounted) await callback();
    },
  };
}

async function settle() {
  for (let index = 0; index < 6; index += 1) {
    await Promise.resolve();
    await new Promise((resolve) => setImmediate(resolve));
  }
}

function createTrainComponent({ api, props = { view: 'trainNew' } } = {}) {
  const Vue = createVueShim();
  const window = { location: { hash: '' } };
  const train = load({ api, Vue, window });
  let definition = null;
  train.install({ component(name, value) { definition = { name, value }; } });
  const instance = definition.value.setup(props);
  return { train, Vue, window, props, definition, instance };
}

test('catalog export can be registered only with a stable oss uri', () => {
  const train = load();
  assert.equal(train.registrationBlockReason({ checksum: 'abc', detail_json: {} }), '导出尚未产生稳定的 oss:// URI，不能登记到训练。');
  assert.equal(train.registrationBlockReason({ oss_uri: 'https://example.invalid/ds' }), '导出尚未产生稳定的 oss:// URI，不能登记到训练。');
  assert.equal(train.stableOssUri({ detail_json: { oss_uri: 'oss://bucket/datasets/v1' } }), 'oss://bucket/datasets/v1');
  assert.equal(train.registrationBlockReason({ oss_uri: 'oss://bucket/datasets/v1' }), '');
});

test('manual dataset registration preserves unknown metadata and defaults to non-trainable REGISTERED', () => {
  const train = load();
  const body = train.buildDatasetRegistrationBody({
    display_name: 'ego_rgb',
    uri: 'oss://bucket/ego_rgb',
  }, 'request-1');
  assert.equal(body.status, 'REGISTERED');
  assert.equal(body.display_name, 'ego_rgb');
  assert.equal(body.uri, 'oss://bucket/ego_rgb');
  for (const field of ['episodes', 'frames', 'duration_hours', 'fps', 'robot_type', 'camera_keys', 'action_dim', 'state_dim', 'language_tasks']) {
    assert.equal(Object.hasOwn(body, field), false, `unexpected inferred ${field}`);
  }
  assert.match(train.datasetStatusLabel('REGISTERED', 'en-US'), /Registered.*not ready/);
  assert.match(train.datasetStatusLabel('READY', 'en-US'), /Ready.*compatibility/);
  assert.equal(train.validateRegistrationForm({ display_name: 'ego_rgb', uri: 'hf://org/ego_rgb', status: 'REGISTERED' }).ok, true);
  const invalidFps = train.validateRegistrationForm({ display_name: 'ego_rgb', uri: 'hf://org/ego_rgb', status: 'REGISTERED', fps: 'abc' });
  assert.equal(invalidFps.ok, false);
  assert.equal(invalidFps.code, 'invalidMetadata');
  const zeroFps = train.validateRegistrationForm({ display_name: 'ego_rgb', uri: 'hf://org/ego_rgb', status: 'REGISTERED', fps: '0' });
  assert.equal(zeroFps.ok, false);
  assert.equal(zeroFps.code, 'invalidMetadata');
});

test('READY registration requires direct mount confirmation and complete real metadata', () => {
  const train = load();
  const base = {
    display_name: 'ego_rgb',
    uri: 'oss://bucket/ego_rgb',
    status: 'READY',
    ready_confirmed: true,
  };
  const incomplete = train.validateRegistrationForm(base);
  assert.equal(incomplete.ok, false);
  assert.equal(incomplete.code, 'readyFactsRequired');
  const complete = {
    ...base,
    episodes: '2',
    frames: '20',
    duration_hours: '0.1',
    fps: '29.97',
    robot_type: 'arm',
    camera_keys: 'observation.images.rgb',
    action_dim: '0',
    state_dim: '0',
  };
  const valid = train.validateRegistrationForm(complete);
  assert.equal(valid.ok, true);
  assert.equal(valid.code, '');
  const body = train.buildDatasetRegistrationBody(complete, 'request-ready');
  assert.equal(body.status, 'READY');
  assert.equal(body.fps, 29.97);
  assert.equal(body.action_dim, 0);
  assert.equal(body.state_dim, 0);
  assert.equal(train.validateRegistrationForm({ ...complete, uri: 'oss://bucket/ego_rgb.tar.gz' }).code, 'readyArchiveUri');
});

test('wizard covers the five training steps', () => {
  const train = load();
  assert.equal(JSON.stringify(train.STEPS), JSON.stringify(['数据', '模型', '配置', '资源', '确认']));
  assert.ok(train.clientRequestId().length >= 8);
  assert.equal(train.stepLabel('数据'), '数据 / Dataset');
  const context = train.normalizeTrainContext({
    project_id: 'project-real',
    project_name: 'Robot Lab',
    project_role: 'operator',
  });
  assert.equal(JSON.stringify(context), JSON.stringify({
    project_id: 'project-real',
    project_name: 'Robot Lab',
    project_role: 'operator',
    can_write: true,
  }));
  assert.equal(train.normalizeTrainContext({ project_id: 'project-real', project_role: 'viewer' }).can_write, false);
});

test('trainNew keeps the wizard on a failed preflight and submits only after a successful gate', async () => {
  const calls = [];
  let validationPasses = false;
  const api = {
    async trainRequest(method, path, options = {}) {
      calls.push({ method, path, body: options.body });
      if (method === 'GET' && path === '/datasets') return { items: [{ id: 'dataset-1', name: 'dataset', version: 'v1', status: 'READY' }] };
      if (method === 'GET' && path === '/auth/me') return {
        project_id: 'project-real',
        project_name: 'Robot Lab',
        project_role: 'operator',
        projects: [{ project_id: 'project-real', role: 'operator' }],
      };
      if (method === 'GET' && path === '/models') return {
        items: [{
          id: 'act',
          version_id: 'model-1',
          name: 'ACT',
          version: 'v1',
          recipes: [{
            id: 'fine_tune',
            name: 'Fine-tune',
            resource_profiles: [
              { id: 'act-4090-standard', name: '4090 ×1', gpu_count: 1, vram_gb_min: 48, selectable: true, provider_id: 'aliyun_dlc' },
              { id: 'act-4090-x2', name: '4090 ×2', gpu_count: 2, vram_gb_min: 48, selectable: true, provider_id: 'aliyun_dlc' },
            ],
          }],
        }],
      };
      if (method === 'GET' && path === '/resources') {
        return {
          profiles: [
            { id: 'act-4090-standard', name: '4090 ×1', gpu_count: 1, vram_gb_min: 48, selectable: true, provider_id: 'aliyun_dlc' },
            { id: 'act-4090-x2', name: '4090 ×2', gpu_count: 2, vram_gb_min: 48, selectable: true, provider_id: 'aliyun_dlc' },
          ],
        };
      }
      if (method === 'POST' && path === '/jobs/validate') {
        return validationPasses
          ? { valid: true, issues: [] }
          : { valid: false, issues: [{ severity: 'BLOCKER', message: 'dataset is not ready' }] };
      }
      if (method === 'POST' && path === '/jobs') return { job_id: 'job-1' };
      if (method === 'GET' && path === '/jobs/job-1') return { id: 'job-1', state: 'QUEUED', stage: 'QUEUE', attempts: [], latest_metrics: {} };
      if (method === 'GET' && path.startsWith('/jobs/job-1/logs')) return { lines: [], next_cursor: 0 };
      throw new Error(`unexpected ${method} ${path}`);
    },
  };
  const harness = createTrainComponent({ api });
  await harness.Vue.mount();
  harness.instance.form.dataset_version_id = 'dataset-1';
  harness.instance.form.model_version_id = 'model-1';
  harness.instance.form.profile = 'act-4090-x2';
  harness.instance.step.value = 3;

  await harness.instance.nextStep();
  assert.equal(harness.instance.step.value, 3);
  assert.equal(harness.instance.validation.value.ok, false);
  assert.equal(calls.filter((call) => call.path === '/jobs').length, 0);

  validationPasses = true;
  await harness.instance.nextStep();
  assert.equal(harness.instance.step.value, 4);
  assert.equal(harness.instance.validation.value.ok, true);
  harness.instance.form.steps = 2000;
  await harness.instance.submitJob();
  assert.match(harness.instance.error.value, /当前参数|current parameters/);
  assert.equal(calls.filter((call) => call.path === '/jobs').length, 0);
  harness.instance.form.steps = 1000;
  await harness.instance.submitJob();
  const jobCall = calls.find((call) => call.method === 'POST' && call.path === '/jobs');
  assert.equal(jobCall.body.project_id, 'project-real');
  assert.equal(jobCall.body.resource_selection.profile, 'act-4090-x2');
  assert.equal(harness.window.location.hash, '#/trainJobs');
  await settle();
});

test('new training always resets to step 1 and gates READY + 4090 profiles', () => {
  const train = load();
  assert.equal(train.defaultWizardForm().dataset_version_id, '');
  assert.equal(train.progressPercent('RUNNING'), 55);
  assert.match(train.formatResourceOption({ name: '4090 ×2', gpu_models: ['RTX 4090'], gpu_count: 2, vram_gb_min: 48 }), /4090/);
  const profiles = [
    { id: 'pi05-local-sim', name: '本地模拟', selectable: true },
    { id: 'pi05-4090-standard', name: '4090 ×1', selectable: true, gpu_count: 1, vram_gb_min: 48 },
    { id: 'pi05-4090-x4', name: '4090 ×4', selectable: true, gpu_count: 4, vram_gb_min: 48 },
  ];
  const model = {
    id: 'pi05',
    version_id: 'mv_pi05',
    recipes: [{ id: 'fine_tune', resource_profiles: profiles }],
  };
  assert.equal(train.defaultProfileId(model, profiles), 'pi05-4090-standard');
  assert.equal(train.wizardStepGate(0, { dataset_version_id: '' }, []).ok, false);
  assert.equal(train.wizardStepGate(0, { dataset_version_id: 'dsv-1' }, [{ id: 'dsv-1', status: 'REGISTERED' }]).ok, false);
  assert.equal(train.wizardStepGate(0, { dataset_version_id: 'dsv-1' }, [{ id: 'dsv-1', status: 'READY' }]).ok, true);
  assert.equal(train.wizardStepGate(3, { profile: '' }, []).ok, false);
  assert.equal(train.wizardStepGate(3, { profile: 'pi05-4090-x4' }, []).ok, true);
});

test('trainNew refuses preflight and submit when the server context has no writable project', async () => {
  const calls = [];
  const api = {
    async trainRequest(method, path, options = {}) {
      calls.push({ method, path, body: options.body });
      if (method === 'GET' && path === '/datasets') return { items: [] };
      if (method === 'GET' && path === '/auth/me') return {
        project_id: 'project-readonly',
        project_name: 'Read only',
        project_role: 'viewer',
      };
      if (method === 'GET' && path === '/models') return { items: [] };
      if (method === 'GET' && path === '/resources') return { profiles: [] };
      throw new Error(`unexpected ${method} ${path}`);
    },
  };
  const harness = createTrainComponent({ api });
  await harness.Vue.mount();
  harness.instance.form.profile = 'act-local-sim';
  harness.instance.step.value = 3;
  await harness.instance.nextStep();
  assert.equal(harness.instance.step.value, 3);
  assert.equal(harness.instance.validation.value.ok, false);
  harness.instance.step.value = 4;
  await harness.instance.submitJob();
  assert.match(harness.instance.error.value, /preflight|校验|不可用|unavailable|只读|read only/);
  assert.equal(calls.some((call) => call.method === 'POST'), false);
});

test('trainDatasets localizes the catalog, loads the real training project, and keeps a sparse registration factual', async () => {
  const calls = [];
  const api = {
    async trainRequest(method, path, options = {}) {
      calls.push({ method, path, body: options.body });
      if (method === 'GET' && path === '/datasets') return { items: [] };
      if (method === 'GET' && path === '/auth/me') return {
        project_id: 'prj_other',
        project_name: 'Training control plane',
        project_role: 'admin',
      };
      if (method === 'POST' && path === '/datasets') return {
        id: 'dsv-ego-rgb-v1',
        status: 'REGISTERED',
      };
      throw new Error(`unexpected ${method} ${path}`);
    },
  };
  const harness = createTrainComponent({ api, props: { view: 'trainDatasets', locale: 'en-US' } });
  await harness.Vue.mount();
  assert.equal(harness.instance.copy('datasetTitle'), 'Training datasets');
  assert.equal(harness.instance.copy('projectContextNote'), 'This project comes from the training control plane and is independent of the Studio workspace.');
  assert.equal(harness.instance.projectContext.value.project_id, 'prj_other');
  assert.equal(harness.instance.projectContext.value.can_write, true);

  harness.instance.registerForm.display_name = 'ego_rgb';
  harness.instance.registerForm.uri = 'oss://uat/ego_rgb';
  await harness.instance.registerDataset();
  const registerCall = calls.find((call) => call.method === 'POST' && call.path === '/datasets');
  assert.equal(registerCall.body.status, 'REGISTERED');
  assert.equal(registerCall.body.display_name, 'ego_rgb');
  assert.equal(Object.hasOwn(registerCall.body, 'fps'), false);
  assert.equal(Object.hasOwn(registerCall.body, 'action_dim'), false);
  assert.equal(harness.instance.registrationNotice.value.status, 'REGISTERED');
  assert.match(harness.instance.registrationNoticeTitle.value, /Data source registered.*Registered/);
  assert.match(harness.instance.registrationNoticeDescription.value, /No additional metadata provided/);
  assert.equal(harness.instance.registerForm.display_name, '');
  assert.equal(calls.filter((call) => call.method === 'GET' && call.path === '/auth/me').length, 2);
});

test('trainDatasets prevents registration for a read-only training project', async () => {
  const calls = [];
  const api = {
    async trainRequest(method, path, options = {}) {
      calls.push({ method, path, body: options.body });
      if (method === 'GET' && path === '/datasets') return { items: [] };
      if (method === 'GET' && path === '/auth/me') return {
        project_id: 'prj_other',
        project_name: 'Training control plane',
        project_role: 'viewer',
      };
      throw new Error(`unexpected ${method} ${path}`);
    },
  };
  const harness = createTrainComponent({ api, props: { view: 'trainDatasets', locale: 'en-US' } });
  await harness.Vue.mount();
  harness.instance.registerForm.display_name = 'ego_rgb';
  harness.instance.registerForm.uri = 'oss://uat/ego_rgb';
  await harness.instance.registerDataset();
  assert.match(harness.instance.error.value, /read only/);
  assert.equal(calls.some((call) => call.method === 'POST'), false);
});

test('trainDatasets freezes the submitted metadata snapshot and disables edits while registering', async () => {
  let resolvePost;
  let postBody = null;
  const api = {
    async trainRequest(method, path, options = {}) {
      if (method === 'GET' && path === '/datasets') return { items: [] };
      if (method === 'GET' && path === '/auth/me') return {
        project_id: 'prj_other',
        project_name: 'Training control plane',
        project_role: 'admin',
      };
      if (method === 'POST' && path === '/datasets') {
        postBody = options.body;
        return new Promise((resolve) => { resolvePost = resolve; });
      }
      throw new Error(`unexpected ${method} ${path}`);
    },
  };
  const harness = createTrainComponent({ api, props: { view: 'trainDatasets', locale: 'en-US' } });
  await harness.Vue.mount();
  harness.instance.registerForm.display_name = 'ego_rgb';
  harness.instance.registerForm.uri = 'oss://uat/ego_rgb';
  harness.instance.registerForm.fps = '10';
  const pending = harness.instance.registerDataset();
  await settle();
  assert.equal(harness.instance.saving.value, true);
  assert.match(harness.definition.value.template, /:disabled="saving"/);
  harness.instance.registerForm.fps = '30';
  resolvePost({ status: 'REGISTERED', dataset_version_id: 'dsv-ego-rgb-v1' });
  await pending;
  assert.equal(postBody.fps, 10);
  assert.match(harness.instance.registrationNoticeDescription.value, /FPS[^:]*:\s*10/);
  assert.doesNotMatch(harness.instance.registrationNoticeDescription.value, /FPS[^:]*:\s*30/);
});
