/* QuicTrain V1 console, rendered with the QuicStudio Element Plus shell. */
var QuicTrainConsole = (() => {
  const VIEWS = ['trainDash', 'trainJobs', 'trainNew', 'trainDatasets', 'trainModels', 'trainResources', 'trainSystem'];
  const STEPS = ['数据', '模型', '配置', '资源', '确认'];
  const STEP_LABELS = Object.freeze({
    数据: '数据 / Dataset',
    模型: '模型 / Model',
    配置: '配置 / Configuration',
    资源: '资源 / Resources',
    确认: '确认 / Confirm',
  });

  const ACTIVE_JOB_STATES = new Set(['QUEUED', 'VALIDATING', 'SUBMITTING', 'PROVISIONING', 'RUNNING', 'CANCELLING']);
  const TERMINAL_JOB_STATES = new Set(['SUCCEEDED', 'FAILED', 'CANCELLED', 'ORPHANED']);

  function defaultWizardForm() {
    return {
      dataset_version_id: '',
      model_version_id: '',
      recipe_id: 'fine_tune',
      steps: 1000,
      batch_size: 1,
      display_name: '',
      profile: '',
    };
  }

  function progressPercent(state) {
    const value = String(state || '').toUpperCase();
    if (value === 'SUCCEEDED') return 100;
    if (value === 'FAILED' || value === 'CANCELLED' || value === 'ORPHANED') return 100;
    if (value === 'RUNNING') return 55;
    if (value === 'PROVISIONING' || value === 'SUBMITTING') return 35;
    if (value === 'QUEUED' || value === 'VALIDATING') return 15;
    if (value === 'CANCELLING') return 80;
    return 5;
  }

  function formatResourceOption(profile) {
    if (!profile) return '';
    const gpus = Array.isArray(profile.gpu_models) ? profile.gpu_models.join('/') : '';
    const count = profile.gpu_count != null ? `×${profile.gpu_count}` : '';
    const vram = profile.vram_gb_min ? `${profile.vram_gb_min}G` : '';
    const bits = [profile.name || profile.id, gpus && count ? `${gpus}${count}` : '', vram].filter(Boolean);
    return bits.join(' · ');
  }

  function profilesForSelection(allProfiles, model, recipeId) {
    const items = Array.isArray(allProfiles) ? allProfiles : [];
    const selectable = items.filter((item) => item && item.selectable !== false && item.enabled !== false);
    if (!model) return selectable;
    const recipes = Array.isArray(model.recipes) ? model.recipes : [];
    const recipe = recipes.find((item) => (typeof item === 'string' ? item : item?.id) === recipeId) || recipes[0];
    const recipeProfiles = recipe && typeof recipe === 'object' && Array.isArray(recipe.resource_profiles)
      ? recipe.resource_profiles.filter((item) => item && item.selectable !== false)
      : [];
    if (recipeProfiles.length) {
      const byId = new Map(selectable.map((item) => [item.id, item]));
      return recipeProfiles.map((item) => ({ ...item, ...(byId.get(item.id) || {}) }));
    }
    const versionId = model.version_id || model.id;
    const modelId = model.id || '';
    const matched = selectable.filter((item) => (
      item.model_version_id === versionId
      || String(item.id || '').startsWith(`${modelId}-`)
    ));
    return matched.length ? matched : selectable;
  }

  function defaultProfileId(model, profiles) {
    const items = Array.isArray(profiles) ? profiles : [];
    if (!items.length) return '';
    const modelId = model?.id || '';
    const preferred = [
      `${modelId}-4090-standard`,
      `${modelId}-4090-x2`,
      `${modelId}-4090-x4`,
      `${modelId}-local-sim`,
    ];
    for (const id of preferred) {
      const hit = items.find((item) => item.id === id);
      if (hit) return hit.id;
    }
    const cloud4090 = items.find((item) => String(item.id || '').includes('4090') && item.selectable !== false);
    if (cloud4090) return cloud4090.id;
    return items[0].id;
  }

  function wizardStepGate(step, form, datasets) {
    if (step === 0) {
      if (!form.dataset_version_id) return { ok: false, message: '请先选择数据集版本 / Select a dataset version first.' };
      const ds = (datasets || []).find((item) => item.id === form.dataset_version_id);
      if (ds && String(ds.status || '').toUpperCase() !== 'READY') {
        return { ok: false, message: '仅 READY 数据集可进入下一步 / Only READY datasets can proceed.' };
      }
      return { ok: true, message: '' };
    }
    if (step === 1) {
      if (!form.model_version_id) return { ok: false, message: '请先选择模型 / Select a model first.' };
      if (!form.recipe_id) return { ok: false, message: '请先选择 Recipe / Select a recipe first.' };
      return { ok: true, message: '' };
    }
    if (step === 2) {
      if (!form.steps || form.steps < 1) return { ok: false, message: '训练步数无效 / Invalid training steps.' };
      if (!form.batch_size || form.batch_size < 1) return { ok: false, message: '批大小无效 / Invalid batch size.' };
      return { ok: true, message: '' };
    }
    if (step === 3) {
      if (!form.profile) return { ok: false, message: '请选择资源规格（含 4090 多卡） / Select a resource profile.' };
      return { ok: true, message: '' };
    }
    return { ok: true, message: '' };
  }

  const WRITABLE_PROJECT_ROLES = new Set(['admin', 'operator']);
  const DATASET_URI_PREFIXES = Object.freeze(['oss://', 'bmcpfs://', 'cpfs://', 'file://', 'hf://']);
  const READY_URI_PREFIXES = Object.freeze(['oss://', 'bmcpfs://', 'cpfs://', 'file://']);
  const DATASET_STATUS_LABELS = Object.freeze({
    'zh-CN': Object.freeze({
      REGISTERED: '已登记 · 尚未就绪',
      MATERIALIZING: '物化中 · 暂不可训练',
      READY: '已就绪 · 待兼容校验',
      FAILED: '物化失败',
      DEPRECATED: '已弃用',
      INVALID: '无效',
    }),
    'en-US': Object.freeze({
      REGISTERED: 'Registered · not ready',
      MATERIALIZING: 'Materializing · not schedulable',
      READY: 'Ready · compatibility check required',
      FAILED: 'Materialization failed',
      DEPRECATED: 'Deprecated',
      INVALID: 'Invalid',
    }),
  });
  const CONSOLE_COPY = Object.freeze({
    'zh-CN': Object.freeze({
      eyebrow: '训练 / Training',
      newTraining: '新建训练',
      refresh: '刷新',
      datasetTitle: '训练数据集',
      datasetSubtitle: '登记数据源并查看物化状态；登记状态不能直接训练',
      manualRegisterTitle: '登记数据源',
      manualRegisterDescription: '只填写已知事实；未知的 episode、帧率、动作或状态维度保持为空',
      metadataOptional: '已验证元数据（可选）',
      project: '训练项目',
      projectRole: '项目角色',
      projectContextNote: '此训练项目来自训练控制面，与 Studio workspace 独立。',
      readOnly: '只读',
      writeAllowed: '可登记',
      displayName: '名称',
      datasetId: '数据集 ID',
      version: '版本',
      uri: 'URI',
      format: '格式',
      formatVersion: '格式版本',
      checksum: '校验和（可选）',
      status: '登记状态',
      registered: '已登记（推荐）',
      ready: '已就绪（需确认）',
      registeredHelp: 'REGISTERED 只表示来源已登记，达到 READY 并通过模型兼容校验前不可训练。',
      readyHelp: 'READY 仅适用于已验证可直接挂载、且下方关键元数据完整的数据源；仍需模型兼容校验。',
      readyConfirm: '我确认该 URI 已可直接挂载，且下方元数据均来自真实数据。',
      episodes: 'Episode 数（可选）',
      frames: '帧数（可选）',
      durationHours: '时长（小时，可选）',
      fps: '帧率（可选）',
      robotType: '机器人类型（可选）',
      cameraKeys: '相机字段（可选，逗号分隔）',
      actionDim: '动作维度（可选）',
      stateDim: '状态维度（可选）',
      languageTasks: '语言任务（可选）',
      unknown: '未知',
      yes: '是',
      no: '否',
      register: '登记',
      datasetsTitle: '已登记数据集',
      emptyDatasets: '还没有已登记的数据集',
      nameColumn: '名称',
      versionColumn: '版本',
      statusColumn: '状态',
      uriColumn: 'URI',
      registrationSucceeded: '数据源已登记',
      registrationId: '数据版本 ID',
      metadata: '已提供元数据',
      metadataUnknown: '未提供额外元数据；未推断默认值',
      requiredFields: '请填写名称和 URI。',
      invalidUri: 'URI 必须以 oss://、bmcpfs://、cpfs://、file:// 或 hf:// 开头。',
      invalidMetadata: '已填写的数值元数据必须有效；FPS 必须大于 0。',
      readyFactsRequired: 'READY 需要直接挂载确认，以及真实的 episode、帧数、正数帧率、相机字段和动作/状态维度（可为 0）。',
      readyArchiveUri: '归档文件不能直接标记 READY，请先登记为 REGISTERED。',
      projectUnavailable: '训练项目上下文不可用。',
      noWritePermission: '当前项目为只读，无法登记数据源。',
      registrationFailed: '数据源登记失败',
    }),
    'en-US': Object.freeze({
      eyebrow: 'Training',
      newTraining: 'New training',
      refresh: 'Refresh',
      datasetTitle: 'Training datasets',
      datasetSubtitle: 'Register sources and inspect materialization; registration alone is not trainable',
      manualRegisterTitle: 'Register a data source',
      manualRegisterDescription: 'Enter known facts only; leave unknown episode, FPS, action, or state dimensions blank',
      metadataOptional: 'Verified metadata (optional)',
      project: 'Training project',
      projectRole: 'Project role',
      projectContextNote: 'This project comes from the training control plane and is independent of the Studio workspace.',
      readOnly: 'Read only',
      writeAllowed: 'Registration allowed',
      displayName: 'Name',
      datasetId: 'Dataset ID',
      version: 'Version',
      uri: 'URI',
      format: 'Format',
      formatVersion: 'Format version',
      checksum: 'Checksum (optional)',
      status: 'Registration state',
      registered: 'Registered (recommended)',
      ready: 'Ready (confirm)',
      registeredHelp: 'REGISTERED records the source only; it is not trainable until READY and model compatibility checks pass.',
      readyHelp: 'READY is only for a verified directly mountable source with complete key metadata below; model compatibility is still required.',
      readyConfirm: 'I confirm this URI is directly mountable and all metadata below comes from the real data.',
      episodes: 'Episodes (optional)',
      frames: 'Frames (optional)',
      durationHours: 'Duration in hours (optional)',
      fps: 'FPS (optional)',
      robotType: 'Robot type (optional)',
      cameraKeys: 'Camera keys (optional, comma separated)',
      actionDim: 'Action dimension (optional)',
      stateDim: 'State dimension (optional)',
      languageTasks: 'Language tasks (optional)',
      unknown: 'Unknown',
      yes: 'Yes',
      no: 'No',
      register: 'Register',
      datasetsTitle: 'Registered datasets',
      emptyDatasets: 'No registered datasets yet',
      nameColumn: 'Name',
      versionColumn: 'Version',
      statusColumn: 'Status',
      uriColumn: 'URI',
      registrationSucceeded: 'Data source registered',
      registrationId: 'Dataset version ID',
      metadata: 'Metadata provided',
      metadataUnknown: 'No additional metadata provided; no defaults were inferred',
      requiredFields: 'Enter a name and URI.',
      invalidUri: 'URI must start with oss://, bmcpfs://, cpfs://, file://, or hf://.',
      invalidMetadata: 'Every provided numeric metadata value must be valid; FPS must be greater than 0.',
      readyFactsRequired: 'READY requires direct-mount confirmation plus real episodes, frames, positive FPS, camera keys, and action/state dimensions (zero is valid).',
      readyArchiveUri: 'An archive cannot be marked READY directly; register it as REGISTERED first.',
      projectUnavailable: 'The training project context is unavailable.',
      noWritePermission: 'This project is read only; data sources cannot be registered.',
      registrationFailed: 'Data source registration failed',
    }),
  });

  function isEnglishLocale(locale) {
    return String(locale || '').toLowerCase().startsWith('en');
  }

  function localeKey(locale) {
    return isEnglishLocale(locale) ? 'en-US' : 'zh-CN';
  }

  function consoleCopy(locale, key) {
    return CONSOLE_COPY[localeKey(locale)][key] || CONSOLE_COPY['zh-CN'][key] || key;
  }

  function datasetStatusLabel(status, locale = 'zh-CN') {
    const normalized = String(status || '').trim().toUpperCase();
    return DATASET_STATUS_LABELS[localeKey(locale)][normalized] || normalized || '—';
  }

  function hasValue(value) {
    return value !== null && value !== undefined && String(value).trim() !== '';
  }

  function optionalNumber(value, integer = false) {
    if (!hasValue(value)) return null;
    const number = Number(value);
    if (!Number.isFinite(number) || number < 0 || (integer && !Number.isInteger(number))) return null;
    return number;
  }

  function splitCameraKeys(value) {
    if (Array.isArray(value)) return value.map((item) => String(item).trim()).filter(Boolean);
    return String(value || '').split(',').map((item) => item.trim()).filter(Boolean);
  }

  function normalizedUriPrefix(uri, prefixes = DATASET_URI_PREFIXES) {
    const value = String(uri || '').trim().toLowerCase();
    return prefixes.find((prefix) => value.startsWith(prefix)) || '';
  }

  function isDatasetArchiveUri(uri) {
    const path = String(uri || '').trim().toLowerCase().split(/[?#]/, 1)[0];
    return /\.(?:tar\.gz|tgz|tar|zip)$/.test(path);
  }

  function readyRegistrationAllowed(form = {}) {
    const directUri = normalizedUriPrefix(form.uri, READY_URI_PREFIXES);
    const episodes = optionalNumber(form.episodes, true);
    const frames = optionalNumber(form.frames, true);
    const fps = optionalNumber(form.fps);
    const cameraKeys = splitCameraKeys(form.camera_keys);
    const actionDim = optionalNumber(form.action_dim, true);
    const stateDim = optionalNumber(form.state_dim, true);
    return Boolean(
      form.status === 'READY'
      && form.ready_confirmed
      && directUri
      && !isDatasetArchiveUri(form.uri)
      && episodes !== null
      && frames !== null
      && fps !== null
      && fps > 0
      && cameraKeys.length > 0
      && actionDim !== null
      && stateDim !== null,
    );
  }

  function invalidNumericMetadataField(form = {}) {
    const fields = [
      ['episodes', true, false],
      ['frames', true, false],
      ['duration_hours', false, false],
      ['fps', false, true],
      ['action_dim', true, false],
      ['state_dim', true, false],
    ];
    for (const [field, integer, positive] of fields) {
      if (!hasValue(form[field])) continue;
      const parsed = optionalNumber(form[field], integer);
      if (parsed === null || (positive && parsed <= 0)) return field;
    }
    return '';
  }

  function validateRegistrationForm(form = {}) {
    if (![form.display_name, form.uri].every(hasValue)) {
      return { ok: false, code: 'required' };
    }
    if (!normalizedUriPrefix(form.uri)) return { ok: false, code: 'invalidUri' };
    const invalidNumericField = invalidNumericMetadataField(form);
    if (invalidNumericField) return { ok: false, code: 'invalidMetadata', field: invalidNumericField };
    if (form.status === 'READY' && isDatasetArchiveUri(form.uri)) {
      return { ok: false, code: 'readyArchiveUri' };
    }
    if (form.status === 'READY' && !readyRegistrationAllowed(form)) {
      return { ok: false, code: 'readyFactsRequired' };
    }
    return { ok: true, code: '' };
  }

  function buildDatasetRegistrationBody(form = {}, requestId = '') {
    const body = {
      client_request_id: requestId,
      display_name: String(form.display_name || '').trim(),
      dataset_id: String(form.dataset_id || '').trim() || undefined,
      version: String(form.version || '').trim() || undefined,
      uri: String(form.uri || '').trim(),
      status: form.status === 'READY' ? 'READY' : 'REGISTERED',
      format: String(form.format || 'lerobot').trim() || 'lerobot',
      format_version: String(form.format_version || '3.0').trim() || '3.0',
      checksum: String(form.checksum || '').trim() || undefined,
    };
    const integerFields = [['episodes', form.episodes], ['frames', form.frames], ['action_dim', form.action_dim], ['state_dim', form.state_dim]];
    integerFields.forEach(([key, value]) => {
      const parsed = optionalNumber(value, true);
      if (parsed !== null) body[key] = parsed;
    });
    const duration = optionalNumber(form.duration_hours);
    if (duration !== null) body.duration_hours = duration;
    const fps = optionalNumber(form.fps);
    if (fps !== null) body.fps = fps;
    const robotType = String(form.robot_type || '').trim();
    if (robotType) body.robot_type = robotType;
    const cameraKeys = splitCameraKeys(form.camera_keys);
    if (cameraKeys.length) body.camera_keys = cameraKeys;
    if (form.language_tasks === true || form.language_tasks === 'true') body.language_tasks = true;
    if (form.language_tasks === false || form.language_tasks === 'false') body.language_tasks = false;
    Object.keys(body).forEach((key) => {
      if (body[key] === undefined) delete body[key];
    });
    return body;
  }

  function metadataFactSummary(form = {}, locale = 'zh-CN') {
    const items = [];
    const copy = (key) => consoleCopy(locale, key);
    if (hasValue(form.episodes)) items.push(`${copy('episodes')}: ${form.episodes}`);
    if (hasValue(form.frames)) items.push(`${copy('frames')}: ${form.frames}`);
    if (hasValue(form.duration_hours)) items.push(`${copy('durationHours')}: ${form.duration_hours}`);
    if (hasValue(form.fps)) items.push(`${copy('fps')}: ${form.fps}`);
    if (hasValue(form.robot_type)) items.push(`${copy('robotType')}: ${String(form.robot_type).trim()}`);
    const cameraKeys = splitCameraKeys(form.camera_keys);
    if (cameraKeys.length) items.push(`${copy('cameraKeys')}: ${cameraKeys.join(', ')}`);
    if (hasValue(form.action_dim)) items.push(`${copy('actionDim')}: ${form.action_dim}`);
    if (hasValue(form.state_dim)) items.push(`${copy('stateDim')}: ${form.state_dim}`);
    if (form.language_tasks === true || form.language_tasks === 'true') items.push(`${copy('languageTasks')}: ${copy('yes')}`);
    if (form.language_tasks === false || form.language_tasks === 'false') items.push(`${copy('languageTasks')}: ${copy('no')}`);
    return items.join(' · ');
  }

  function stableOssUri(exp) {
    if (!exp || typeof exp !== 'object') return '';
    const detail = exp.detail_json && typeof exp.detail_json === 'object' ? exp.detail_json : {};
    const raw = exp.oss_uri || detail.oss_uri;
    const uri = typeof raw === 'string' ? raw.trim() : '';
    return uri.startsWith('oss://') && uri !== 'oss://' ? uri : '';
  }

  function registrationBlockReason(exp) {
    return stableOssUri(exp) ? '' : '导出尚未产生稳定的 oss:// URI，不能登记到训练。';
  }

  function clientRequestId() {
    const raw = `studio-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
    return raw.length >= 8 ? raw : `${raw}-train`;
  }

  function normalizeTrainContext(raw) {
    const source = raw && typeof raw === 'object' ? raw : {};
    const projectId = typeof source.project_id === 'string' ? source.project_id.trim() : '';
    const projects = Array.isArray(source.projects) ? source.projects : [];
    const membership = projects.find((item) => String(item?.project_id || '').trim() === projectId) || {};
    const projectRole = String(source.project_role || membership.role || '').trim();
    const projectName = String(
      source.project_name
      || source.project?.name
      || membership.project_name
      || membership.name
      || projectId,
    ).trim();
    return {
      project_id: projectId,
      project_name: projectName,
      project_role: projectRole,
      can_write: Boolean(projectId && WRITABLE_PROJECT_ROLES.has(projectRole)),
    };
  }

  function stepLabel(label) {
    return STEP_LABELS[label] || label;
  }

  function requestFingerprint(body) {
    return JSON.stringify(body);
  }

  function install(app) {
    app.component('train-console', {
      props: {
        view: { type: String, default: 'trainDash' },
        locale: { type: String, default: 'zh-CN' },
      },
      template: `
        <section class="view-stack train-console" v-loading="loading">
          <div class="page-heading">
            <div>
              <p class="eyebrow">{{ eyebrow }}</p>
              <h1>{{ title }}</h1>
              <span>{{ subtitle }}</span>
            </div>
            <div>
              <el-button v-if="view !== 'trainNew'" type="primary" @click="go('trainNew')">{{ copy('newTraining') }}</el-button>
              <el-button @click="reload">{{ copy('refresh') }}</el-button>
            </div>
          </div>
          <el-alert v-if="error" type="error" :closable="false" :title="error" show-icon />

          <template v-if="view === 'trainDash'">
            <div class="metric-grid">
              <article class="metric-card"><span>运行中</span><strong>{{ jobs.running || 0 }}</strong></article>
              <article class="metric-card"><span>排队</span><strong>{{ jobs.queued || 0 }}</strong></article>
              <article class="metric-card"><span>失败</span><strong>{{ jobs.failed || 0 }}</strong></article>
              <article class="metric-card"><span>成功</span><strong>{{ jobs.succeeded || 0 }}</strong></article>
            </div>
            <section class="surface-panel table-panel">
              <div class="panel-heading"><div><h2>最近任务</h2></div></div>
              <el-table :data="recent" class="data-table" @row-click="openJob">
                <el-table-column prop="display_name" label="名称" min-width="180" />
                <el-table-column prop="state" label="状态" width="120" />
                <el-table-column prop="id" label="任务 ID" min-width="160" />
              </el-table>
              <el-empty v-if="!recent.length" description="还没有训练任务" :image-size="64" />
            </section>
          </template>

          <template v-else-if="view === 'trainJobs'">
            <section class="surface-panel table-panel">
              <el-table :data="jobItems" class="data-table" @row-click="openJob">
                <el-table-column prop="display_name" label="名称" min-width="180" />
                <el-table-column prop="state" label="状态" width="120" />
                <el-table-column prop="stage" label="阶段" width="120" />
                <el-table-column label="Loss" width="100"><template #default="scope">{{ metricLoss(scope.row) }}</template></el-table-column>
                <el-table-column label="模型" min-width="120"><template #default="scope">{{ scope.row.model?.model_id || scope.row.model_version_id || '—' }}</template></el-table-column>
                <el-table-column prop="dataset_version_id" label="数据版本" min-width="160" />
              </el-table>
              <el-empty v-if="!jobItems.length" description="还没有训练任务" :image-size="64" />
            </section>
            <section v-if="selected" class="surface-panel train-job-detail">
              <div class="panel-heading">
                <div>
                  <h2>{{ selected.display_name || selected.id }}</h2>
                  <span>{{ selected.state }} · {{ selected.stage || '—' }} · {{ selected.resource?.profile || selected.resource_profile_id || '—' }}</span>
                </div>
                <div>
                  <el-button @click="act('cancel')">取消</el-button>
                  <el-button @click="act('retry')">重试</el-button>
                  <el-button @click="act('clone')">克隆</el-button>
                </div>
              </div>
              <div class="train-job-overview">
                <el-progress :percentage="jobProgress" :status="jobProgressStatus" />
                <div class="train-job-facts">
                  <span>Loss {{ selected.latest_metrics?.['train/loss'] ?? selected.latest_metrics?.loss ?? '—' }}</span>
                  <span v-if="selected.queue_reason">排队原因 {{ selected.queue_reason }}</span>
                  <span v-else-if="isQueuedNoExternal">排队中：等待 Quota4090/DLC 空闲卡（无卡时保持排队，有卡后自动下发）</span>
                  <span v-if="selected.current_attempt">Attempt #{{ selected.current_attempt.number || 1 }} · {{ selected.current_attempt.provider || '—' }} · {{ selected.current_attempt.provider_state || selected.current_attempt.state || '—' }}</span>
                  <span v-if="selected.current_attempt?.external_job_id">外部任务 {{ selected.current_attempt.external_job_id }}</span>
                  <a v-if="selected.links?.provider_dashboard" :href="selected.links.provider_dashboard" target="_blank" rel="noopener">Provider 控制台</a>
                </div>
                <el-table v-if="(selected.attempts || []).length" :data="selected.attempts" size="small" class="data-table" style="margin-top: 12px;">
                  <el-table-column prop="number" label="#" width="50" />
                  <el-table-column prop="state" label="状态" width="110" />
                  <el-table-column prop="provider" label="Provider" width="120" />
                  <el-table-column prop="external_job_id" label="外部 ID" min-width="160" show-overflow-tooltip />
                  <el-table-column prop="provider_state" label="Provider 状态" width="130" />
                </el-table>
              </div>
              <div class="train-log-toolbar">
                <strong>训练日志</strong>
                <span>{{ logPolling ? '自动刷新中' : '已暂停' }}</span>
                <el-button size="small" @click="toggleLogPolling">{{ logPolling ? '暂停' : '继续' }}</el-button>
              </div>
              <pre class="train-log">{{ logText }}</pre>
            </section>
          </template>

          <template v-else-if="view === 'trainNew'">
            <section class="surface-panel">
              <el-steps :active="step" finish-status="success" align-center>
                <el-step v-for="label in steps" :key="label" :title="stepLabel(label)" />
              </el-steps>
              <div v-if="step === 0" style="margin-top: 16px;">
                <p class="form-help">仅 READY 数据集可训练；每次新建从第 1 步开始。</p>
                <el-select v-model="form.dataset_version_id" placeholder="选择数据集版本 / Select dataset version" filterable style="width: 100%;">
                  <el-option
                    v-for="item in readyDatasets"
                    :key="item.id"
                    :label="item.name + ' · ' + item.version + ' · ' + (item.status || '')"
                    :value="item.id"
                  />
                </el-select>
                <el-empty v-if="!readyDatasets.length" description="没有 READY 数据集，请先在「训练数据集」登记可挂载目录" :image-size="56" />
              </div>
              <div v-else-if="step === 1" style="margin-top: 16px;">
                <el-select v-model="form.model_version_id" placeholder="选择模型 / Select model" filterable style="width: 100%;" @change="pickModel">
                  <el-option
                    v-for="item in selectableModels"
                    :key="item.version_id"
                    :label="item.name + ' ' + item.version"
                    :value="item.version_id"
                  />
                </el-select>
                <el-select v-model="form.recipe_id" placeholder="Recipe" style="width: 100%; margin-top: 12px;" @change="syncResourceDefault">
                  <el-option v-for="item in recipes" :key="item.id" :label="item.name || item.id" :value="item.id" />
                </el-select>
              </div>
              <div v-else-if="step === 2" style="margin-top: 16px;">
                <el-form label-position="top">
                  <el-form-item label="训练步数 / Training steps"><el-input-number v-model="form.steps" :min="1" /></el-form-item>
                  <el-form-item label="批大小 / Batch size"><el-input-number v-model="form.batch_size" :min="1" /></el-form-item>
                  <el-form-item label="任务名称 / Job name"><el-input v-model="form.display_name" placeholder="例如 / e.g. desk-grasp-pi05" /></el-form-item>
                </el-form>
              </div>
              <div v-else-if="step === 3" style="margin-top: 16px;">
                <p class="form-help">π0.5 / ACT 可选用 Quota4090_48G 的 ×1 / ×2 / ×4；本地模拟仅用于联调。</p>
                <el-select v-model="form.profile" placeholder="资源规格（含 4090 多卡）" filterable style="width: 100%;">
                  <el-option
                    v-for="item in modelProfiles"
                    :key="item.id"
                    :label="formatResourceOption(item)"
                    :value="item.id"
                  />
                </el-select>
                <div v-if="selectedProfile" class="train-resource-hint">
                  <div>{{ selectedProfile.warning || '—' }}</div>
                  <div>Provider {{ selectedProfile.provider_id || '—' }} · Pool {{ selectedProfile.pool_id || '—' }} · VRAM ≥ {{ selectedProfile.vram_gb_min || 0 }}G</div>
                </div>
              </div>
              <div v-else style="margin-top: 16px;">
                <p>项目 / Project {{ projectContext?.project_name || projectContext?.project_id || '—' }}</p>
                <p>数据 / Dataset {{ form.dataset_version_id || '—' }} · 模型 / Model {{ form.model_version_id || '—' }} · {{ form.recipe_id }}</p>
                <p>步数 / Steps {{ form.steps }} · batch {{ form.batch_size }} · 资源 / Resource {{ formatResourceOption(selectedProfile) || form.profile || '—' }}</p>
              </div>
              <el-alert v-if="validation" :type="validation.ok ? 'success' : 'warning'" :closable="false" :title="validation.message" style="margin-top: 16px;" />
              <div style="margin-top: 16px;">
                <el-button :disabled="step === 0" @click="step -= 1">上一步</el-button>
                <el-button v-if="step < 4" type="primary" :disabled="!readyDatasets.length && step === 0" @click="nextStep">下一步 / Next</el-button>
                <el-button v-else type="primary" :loading="saving" @click="submitJob">提交训练 / Submit training</el-button>
              </div>
            </section>
          </template>

          <template v-else-if="view === 'trainDatasets'">
            <section class="surface-panel train-dataset-context">
              <div class="panel-heading">
                <div>
                  <h2>{{ copy('project') }}</h2>
                  <span>{{ copy('datasetSubtitle') }}</span>
                </div>
                <el-tag :type="projectContext?.can_write ? 'success' : 'info'">
                  {{ projectContext?.can_write ? copy('writeAllowed') : copy('readOnly') }}
                </el-tag>
              </div>
              <div class="train-project-facts">
                <span v-if="projectContext?.project_name && projectContext.project_name !== projectContext.project_id">{{ projectContext.project_name }}</span>
                <code>{{ projectContext?.project_id || '—' }}</code>
                <span>{{ copy('projectRole') }}: {{ projectContext?.project_role || '—' }}</span>
              </div>
              <small class="form-help">{{ copy('projectContextNote') }}</small>
            </section>
            <el-alert v-if="registrationNotice" type="success" :closable="false" show-icon>
              <template #title>{{ registrationNoticeTitle }}</template>
              <template #default>{{ registrationNoticeDescription }}</template>
            </el-alert>
            <section class="surface-panel">
              <div class="panel-heading">
                <div>
                  <h2>{{ copy('manualRegisterTitle') }}</h2>
                  <span>{{ copy('manualRegisterDescription') }}</span>
                </div>
              </div>
              <el-form label-position="top" class="train-dataset-register-form" :disabled="saving">
                <el-form-item :label="copy('displayName')" required><el-input v-model="registerForm.display_name" /></el-form-item>
                <el-form-item :label="copy('uri')" required><el-input v-model="registerForm.uri" placeholder="oss://, bmcpfs://, cpfs://, file://, or hf://" /></el-form-item>
                <el-form-item :label="copy('status')">
                  <el-radio-group v-model="registerForm.status">
                    <el-radio value="REGISTERED">{{ copy('registered') }}</el-radio>
                    <el-radio value="READY">{{ copy('ready') }}</el-radio>
                  </el-radio-group>
                  <small class="form-help">{{ registerForm.status === 'READY' ? copy('readyHelp') : copy('registeredHelp') }}</small>
                </el-form-item>
                <template v-if="registerForm.status === 'READY'">
                  <el-form-item>
                    <el-checkbox v-model="registerForm.ready_confirmed">{{ copy('readyConfirm') }}</el-checkbox>
                  </el-form-item>
                </template>
                <el-collapse v-model="metadataOpen" class="train-dataset-metadata">
                  <el-collapse-item name="facts" :title="copy('metadataOptional')">
                    <el-form-item :label="copy('datasetId')"><el-input v-model="registerForm.dataset_id" /></el-form-item>
                    <el-form-item :label="copy('version')"><el-input v-model="registerForm.version" placeholder="v1" /></el-form-item>
                    <el-form-item :label="copy('format')"><el-input v-model="registerForm.format" /></el-form-item>
                    <el-form-item :label="copy('formatVersion')"><el-input v-model="registerForm.format_version" /></el-form-item>
                    <el-form-item :label="copy('checksum')"><el-input v-model="registerForm.checksum" /></el-form-item>
                    <el-form-item :label="copy('episodes')"><el-input v-model="registerForm.episodes" inputmode="numeric" /></el-form-item>
                    <el-form-item :label="copy('frames')"><el-input v-model="registerForm.frames" inputmode="numeric" /></el-form-item>
                    <el-form-item :label="copy('durationHours')"><el-input v-model="registerForm.duration_hours" inputmode="decimal" /></el-form-item>
                    <el-form-item :label="copy('fps')"><el-input v-model="registerForm.fps" inputmode="decimal" /></el-form-item>
                    <el-form-item :label="copy('robotType')"><el-input v-model="registerForm.robot_type" /></el-form-item>
                    <el-form-item :label="copy('cameraKeys')"><el-input v-model="registerForm.camera_keys" /></el-form-item>
                    <el-form-item :label="copy('actionDim')"><el-input v-model="registerForm.action_dim" inputmode="numeric" /></el-form-item>
                    <el-form-item :label="copy('stateDim')"><el-input v-model="registerForm.state_dim" inputmode="numeric" /></el-form-item>
                    <el-form-item :label="copy('languageTasks')">
                      <el-select v-model="registerForm.language_tasks" :placeholder="copy('unknown')" clearable>
                        <el-option value="true" :label="copy('yes')" />
                        <el-option value="false" :label="copy('no')" />
                      </el-select>
                    </el-form-item>
                  </el-collapse-item>
                </el-collapse>
                <el-form-item>
                  <el-button type="primary" :loading="saving" :disabled="!projectContext?.can_write" @click="registerDataset">{{ copy('register') }}</el-button>
                </el-form-item>
              </el-form>
            </section>
            <section class="surface-panel table-panel">
              <div class="panel-heading"><div><h2>{{ copy('datasetsTitle') }}</h2></div></div>
              <el-table :data="datasets" class="data-table" :empty-text="copy('emptyDatasets')">
                <el-table-column prop="name" :label="copy('nameColumn')" min-width="160" />
                <el-table-column prop="version" :label="copy('versionColumn')" width="120" />
                <el-table-column :label="copy('statusColumn')" width="250">
                  <template #default="scope"><el-tag :type="scope.row.status === 'READY' ? 'success' : scope.row.status === 'FAILED' ? 'danger' : 'info'">{{ datasetStatusLabel(scope.row.status, locale) }}</el-tag></template>
                </el-table-column>
                <el-table-column prop="uri" :label="copy('uriColumn')" min-width="240" show-overflow-tooltip />
              </el-table>
            </section>
          </template>

          <template v-else-if="view === 'trainModels'">
            <section class="surface-panel table-panel">
              <el-table :data="models" class="data-table">
                <el-table-column prop="name" label="模型" min-width="140" />
                <el-table-column prop="version" label="版本" width="120" />
                <el-table-column prop="maturity" label="成熟度" width="120" />
                <el-table-column prop="description" label="说明" min-width="240" show-overflow-tooltip />
              </el-table>
            </section>
          </template>

          <template v-else-if="view === 'trainResources'">
            <section class="surface-panel table-panel">
              <el-table :data="profiles" class="data-table">
                <el-table-column prop="name" label="规格" min-width="180" />
                <el-table-column prop="gpu_count" label="卡数" width="80" />
                <el-table-column prop="vram_gb_min" label="VRAM≥" width="90" />
                <el-table-column prop="provider_id" label="Provider" width="140" />
                <el-table-column prop="pool_id" label="池" min-width="140" />
                <el-table-column prop="calibration_status" label="校准" width="140" />
              </el-table>
            </section>
          </template>

          <template v-else>
            <section class="surface-panel">
              <pre class="train-log">{{ systemText }}</pre>
            </section>
          </template>
        </section>
      `,
      setup(props) {
        const loading = Vue.ref(false);
        const saving = Vue.ref(false);
        const error = Vue.ref('');
        const step = Vue.ref(0);
        const dashboard = Vue.ref(null);
        const jobItems = Vue.ref([]);
        const selected = Vue.ref(null);
        const logText = Vue.ref('');
        const logPolling = Vue.ref(false);
        const logCursor = Vue.ref(0);
        let jobPollTimer = null;
        let logPollTimer = null;
        const datasets = Vue.ref([]);
        const models = Vue.ref([]);
        const profiles = Vue.ref([]);
        const systemText = Vue.ref('');
        const validation = Vue.ref(null);
        const projectContext = Vue.ref(null);
        const form = Vue.reactive(defaultWizardForm());
        const registerForm = Vue.reactive({
          display_name: '',
          dataset_id: '',
          version: '',
          uri: '',
          format: 'lerobot',
          format_version: '3.0',
          checksum: '',
          status: 'REGISTERED',
          ready_confirmed: false,
          episodes: '',
          frames: '',
          duration_hours: '',
          fps: '',
          robot_type: '',
          camera_keys: '',
          action_dim: '',
          state_dim: '',
          language_tasks: '',
        });
        const registrationNotice = Vue.ref(null);
        const metadataOpen = Vue.ref([]);
        const titles = {
          trainDash: ['训练总览', '从数据版本到训练任务'],
          trainJobs: ['训练任务', '排队、运行、失败与日志'],
          trainNew: ['新建训练 / New training', '数据 / 模型 / 配置 / 资源 / 确认 · Dataset / model / configuration / resources / confirmation'],
          trainDatasets: ['训练数据集', '登记数据源并查看物化状态；登记状态不能直接训练'],
          trainModels: ['模型与 Recipe', 'ACT 与 π0.5'],
          trainResources: ['训练资源', '资源池与模型硬约束'],
          trainSystem: ['系统健康', '运行模式与集成状态'],
        };

        function copy(key) {
          return consoleCopy(props.locale, key);
        }

        function resetRegisterForm() {
          Object.assign(registerForm, {
            display_name: '',
            dataset_id: '',
            version: '',
            uri: '',
            format: 'lerobot',
            format_version: '3.0',
            checksum: '',
            status: 'REGISTERED',
            ready_confirmed: false,
            episodes: '',
            frames: '',
            duration_hours: '',
            fps: '',
            robot_type: '',
            camera_keys: '',
            action_dim: '',
            state_dim: '',
            language_tasks: '',
          });
        }

        function resetWizard() {
          Object.assign(form, defaultWizardForm());
          step.value = 0;
          validation.value = null;
          error.value = '';
        }

        function stopJobPolling() {
          if (jobPollTimer) {
            clearInterval(jobPollTimer);
            jobPollTimer = null;
          }
          if (logPollTimer) {
            clearInterval(logPollTimer);
            logPollTimer = null;
          }
          logPolling.value = false;
        }

        function go(view) {
          if (view === 'trainNew') resetWizard();
          if (window.location) window.location.hash = `#/${view}`;
        }

        const eyebrow = Vue.computed(() => copy('eyebrow'));
        const registrationNoticeTitle = Vue.computed(() => {
          const notice = registrationNotice.value;
          if (!notice) return '';
          return `${copy('registrationSucceeded')} · ${datasetStatusLabel(notice.status, props.locale)}`;
        });
        const registrationNoticeDescription = Vue.computed(() => {
          const notice = registrationNotice.value;
          if (!notice) return '';
          const id = notice.id ? `${copy('registrationId')}: ${notice.id}` : '';
          const metadata = notice.metadata || copy('metadataUnknown');
          return [id, `${copy('metadata')}: ${metadata}`].filter(Boolean).join(' · ');
        });

        const recipes = Vue.computed(() => {
          const model = models.value.find((item) => item.version_id === form.model_version_id);
          const items = Array.isArray(model?.recipes) ? model.recipes : [];
          return items.map((item) => (typeof item === 'string' ? { id: item, name: item } : item)).filter((item) => item && item.id);
        });
        const readyDatasets = Vue.computed(() => (datasets.value || []).filter((item) => String(item.status || '').toUpperCase() === 'READY'));
        const selectableModels = Vue.computed(() => (models.value || []).filter((item) => item && item.selectable !== false));
        const selectedModel = Vue.computed(() => (models.value || []).find((item) => item.version_id === form.model_version_id) || null);
        const modelProfiles = Vue.computed(() => profilesForSelection(profiles.value, selectedModel.value, form.recipe_id));
        const selectedProfile = Vue.computed(() => (modelProfiles.value || []).find((item) => item.id === form.profile) || null);
        const jobProgress = Vue.computed(() => progressPercent(selected.value?.state));
        const jobProgressStatus = Vue.computed(() => {
          const state = String(selected.value?.state || '').toUpperCase();
          if (state === 'SUCCEEDED') return 'success';
          if (state === 'FAILED' || state === 'ORPHANED') return 'exception';
          if (state === 'CANCELLED') return 'warning';
          return undefined;
        });
        const isQueuedNoExternal = Vue.computed(() => {
          const state = String(selected.value?.state || '').toUpperCase();
          const attemptExt = (selected.value?.current_attempt || {}).external_job_id
            || ((selected.value?.attempts || [])[0] || {}).external_job_id;
          return (state === 'QUEUED' || state === 'SUBMITTING' || state === 'PROVISIONING') && !attemptExt;
        });

        function metricLoss(row) {
          const metrics = row?.latest_metrics || {};
          const value = metrics['train/loss'] ?? metrics.loss;
          return value == null || value === '' ? '—' : value;
        }

        function syncResourceDefault() {
          const next = defaultProfileId(selectedModel.value, modelProfiles.value);
          if (next) form.profile = next;
        }

        async function reload() {
          if (!QuicDataAPI.trainRequest) return;
          loading.value = true;
          error.value = '';
          try {
            if (props.view === 'trainDash') {
              dashboard.value = await QuicDataAPI.trainRequest('GET', '/dashboard');
            } else if (props.view === 'trainJobs') {
              jobItems.value = (await QuicDataAPI.trainRequest('GET', '/jobs?limit=50')).items || [];
            } else if (props.view === 'trainNew' || props.view === 'trainDatasets') {
              projectContext.value = normalizeTrainContext(await QuicDataAPI.trainRequest('GET', '/auth/me'));
              datasets.value = (await QuicDataAPI.trainRequest('GET', '/datasets')).items || [];
              if (props.view === 'trainNew') {
                if (!projectContext.value.project_id || !projectContext.value.can_write) {
                  throw new Error('训练项目上下文不可用或没有 train:write 权限 / A writable training project is unavailable.');
                }
                models.value = (await QuicDataAPI.trainRequest('GET', '/models')).items || [];
                profiles.value = (await QuicDataAPI.trainRequest('GET', '/resources')).profiles || [];
                if (!form.profile) syncResourceDefault();
              }
            } else if (props.view === 'trainModels') {
              models.value = (await QuicDataAPI.trainRequest('GET', '/models')).items || [];
            } else if (props.view === 'trainResources') {
              profiles.value = (await QuicDataAPI.trainRequest('GET', '/resources')).profiles || [];
            } else {
              systemText.value = JSON.stringify(await QuicDataAPI.trainRequest('GET', '/ops/status'), null, 2);
            }
          } catch (err) {
            error.value = err && err.message ? err.message : '训练控制面请求失败';
          } finally {
            loading.value = false;
          }
        }

        async function refreshSelectedJob() {
          if (!selected.value?.id || !QuicDataAPI.trainRequest) return;
          try {
            selected.value = await QuicDataAPI.trainRequest('GET', `/jobs/${encodeURIComponent(selected.value.id)}`);
            if (TERMINAL_JOB_STATES.has(String(selected.value.state || '').toUpperCase())) {
              stopJobPolling();
              await fetchJobLogs(true);
            }
          } catch (err) {
            error.value = err.message;
          }
        }

        async function fetchJobLogs(reset = false) {
          if (!selected.value?.id || !QuicDataAPI.trainRequest) return;
          try {
            const cursorQuery = reset || !logCursor.value ? '' : `?cursor=${encodeURIComponent(logCursor.value)}`;
            const logs = await QuicDataAPI.trainRequest('GET', `/jobs/${encodeURIComponent(selected.value.id)}/logs${cursorQuery}`);
            const lines = Array.isArray(logs.lines) ? logs.lines : [];
            const rendered = lines.map((line) => line.message || line.text || JSON.stringify(line)).join('\n');
            if (reset || !logCursor.value) {
              logText.value = rendered || '暂无日志';
            } else if (rendered) {
              logText.value = `${logText.value ? `${logText.value}\n` : ''}${rendered}`;
            }
            if (logs.next_cursor != null) logCursor.value = logs.next_cursor;
            else if (lines.length) logCursor.value = (logCursor.value || 0) + lines.length;
          } catch (err) {
            error.value = err.message;
          }
        }

        function startJobPolling() {
          stopJobPolling();
          if (!selected.value?.id) return;
          logPolling.value = true;
          jobPollTimer = setInterval(refreshSelectedJob, 10000);
          logPollTimer = setInterval(() => fetchJobLogs(false), 5000);
          if (typeof jobPollTimer?.unref === 'function') jobPollTimer.unref();
          if (typeof logPollTimer?.unref === 'function') logPollTimer.unref();
        }

        function toggleLogPolling() {
          if (logPolling.value) {
            stopJobPolling();
            return;
          }
          startJobPolling();
        }

        async function openJob(row) {
          stopJobPolling();
          selected.value = row;
          logText.value = '';
          logCursor.value = 0;
          if (props.view !== 'trainJobs') go('trainJobs');
          try {
            selected.value = await QuicDataAPI.trainRequest('GET', `/jobs/${encodeURIComponent(row.id)}`);
            await fetchJobLogs(true);
            if (ACTIVE_JOB_STATES.has(String(selected.value.state || '').toUpperCase())) {
              startJobPolling();
            }
          } catch (err) {
            error.value = err.message;
          }
        }

        async function act(name) {
          if (!selected.value) return;
          try {
            selected.value = await QuicDataAPI.trainRequest('POST', `/jobs/${encodeURIComponent(selected.value.id)}/${name}`);
            await reload();
            if (name === 'clone' && selected.value?.id) {
              // keep viewing cloned job progress
              await openJob({ id: selected.value.id });
            }
          } catch (err) {
            error.value = err.message;
          }
        }

        function pickModel() {
          const first = recipes.value[0];
          form.recipe_id = first ? first.id : 'fine_tune';
          syncResourceDefault();
        }

        function jobBody() {
          const overrides = { 'training.steps': form.steps, 'training.batch_size': form.batch_size };
          if (form.steps && form.steps <= 500) {
            overrides['optimizer.warmup_steps'] = Math.max(0, Math.min(10, Math.floor(form.steps / 2)));
          }
          return {
            project_id: projectContext.value?.project_id || '',
            dataset_version_id: form.dataset_version_id,
            model_version_id: form.model_version_id,
            recipe_id: form.recipe_id || 'fine_tune',
            config_overrides: overrides,
            resource_selection: { mode: form.profile ? 'MANUAL' : 'AUTO', profile: form.profile || null },
          };
        }

        async function nextStep() {
          const gate = wizardStepGate(step.value, form, datasets.value);
          if (!gate.ok) {
            validation.value = { ok: false, message: gate.message };
            return;
          }
          if (step.value === 3) {
            if (!projectContext.value?.project_id || !projectContext.value.can_write) {
              validation.value = {
                ok: false,
                message: '训练项目上下文不可用或没有 train:write 权限 / A writable training project is unavailable.',
              };
              return;
            }
            try {
              const body = jobBody();
              const result = await QuicDataAPI.trainRequest('POST', '/jobs/validate', { body });
              const blockers = (result?.issues || []).filter((item) => item.severity === 'BLOCKER');
              const reportedValid = result && typeof result.valid === 'boolean'
                ? result.valid
                : result && typeof result.ok === 'boolean' ? result.ok : !blockers.length;
              validation.value = {
                ok: Boolean(reportedValid) && !blockers.length,
                message: blockers.length ? blockers.map((item) => item.message).join('；') : '校验通过',
                request_fingerprint: requestFingerprint(body),
              };
            } catch (err) {
              validation.value = { ok: false, message: err.message };
            }
            if (!validation.value.ok) return;
          } else {
            validation.value = null;
          }
          step.value = Math.min(4, step.value + 1);
        }

        async function submitJob() {
          const body = jobBody();
          const currentFingerprint = requestFingerprint(body);
          if (
            step.value !== 4
            || !validation.value?.ok
            || validation.value.request_fingerprint !== currentFingerprint
            || !projectContext.value?.project_id
            || !projectContext.value.can_write
          ) {
            error.value = '请先完成当前参数的训练前校验 / Complete preflight validation for the current parameters first.';
            return;
          }
          saving.value = true;
          error.value = '';
          try {
            const created = await QuicDataAPI.trainRequest('POST', '/jobs', {
              body: { ...body, client_request_id: clientRequestId(), display_name: form.display_name || null },
            });
            const jobId = created && (created.job_id || created.id);
            resetWizard();
            go('trainJobs');
            if (jobId) openJob({ id: jobId });
          } catch (err) {
            error.value = err.message;
          } finally {
            saving.value = false;
          }
        }

        async function registerDataset() {
          if (!projectContext.value?.project_id) {
            error.value = copy('projectUnavailable');
            return;
          }
          if (!projectContext.value.can_write) {
            error.value = copy('noWritePermission');
            return;
          }
          const validationResult = validateRegistrationForm(registerForm);
          if (!validationResult.ok) {
            error.value = validationResult.code === 'invalidUri'
              ? copy('invalidUri')
              : validationResult.code === 'invalidMetadata'
                ? copy('invalidMetadata')
                : validationResult.code === 'readyArchiveUri'
                  ? copy('readyArchiveUri')
                  : validationResult.code === 'readyFactsRequired'
                    ? copy('readyFactsRequired')
                    : copy('requiredFields');
            return;
          }
          const body = Object.freeze(buildDatasetRegistrationBody(registerForm, clientRequestId()));
          const submittedMetadata = metadataFactSummary(body, props.locale);
          saving.value = true;
          error.value = '';
          try {
            const result = await QuicDataAPI.trainRequest('POST', '/datasets', { body });
            registrationNotice.value = {
              status: String(result?.status || body.status || 'REGISTERED').toUpperCase(),
              id: result?.dataset_version_id || result?.id || '',
              metadata: submittedMetadata,
            };
            resetRegisterForm();
            await reload();
          } catch (err) {
            error.value = err && err.message ? err.message : copy('registrationFailed');
          } finally {
            saving.value = false;
          }
        }

        Vue.onMounted(() => {
          if (props.view === 'trainNew') resetWizard();
          return reload();
        });
        Vue.watch(() => props.view, (view) => {
          stopJobPolling();
          if (view === 'trainNew') resetWizard();
          return reload();
        });
        Vue.onUnmounted?.(() => stopJobPolling());
        const pair = titles[props.view] || titles.trainDash;
        return {
          loading, saving, error, step, steps: STEPS, stepLabel, form, registerForm, datasets, models, profiles, projectContext,
          jobItems, selected, logText, logPolling, systemText, validation, recipes,
          readyDatasets, selectableModels, modelProfiles, selectedProfile, jobProgress, jobProgressStatus,
          isQueuedNoExternal,
          registrationNotice, registrationNoticeTitle, registrationNoticeDescription,
          metadataOpen, copy, eyebrow, datasetStatusLabel, formatResourceOption, metricLoss,
          jobs: Vue.computed(() => (dashboard.value && dashboard.value.jobs) || {}),
          recent: Vue.computed(() => (dashboard.value && dashboard.value.recent_jobs) || []),
          title: Vue.computed(() => props.view === 'trainDatasets' ? copy('datasetTitle') : (titles[props.view] || pair)[0]),
          subtitle: Vue.computed(() => props.view === 'trainDatasets' ? copy('datasetSubtitle') : (titles[props.view] || pair)[1]),
          locale: Vue.computed(() => props.locale),
          reload, go, openJob, act, pickModel, syncResourceDefault, nextStep, submitJob, registerDataset,
          toggleLogPolling, resetWizard,
        };
      },
    });
  }

  return {
    VIEWS,
    STEPS,
    ACTIVE_JOB_STATES,
    TERMINAL_JOB_STATES,
    defaultWizardForm,
    progressPercent,
    formatResourceOption,
    profilesForSelection,
    defaultProfileId,
    wizardStepGate,
    stableOssUri,
    registrationBlockReason,
    clientRequestId,
    normalizeTrainContext,
    stepLabel,
    isEnglishLocale,
    localeKey,
    consoleCopy,
    datasetStatusLabel,
    isDatasetArchiveUri,
    readyRegistrationAllowed,
    validateRegistrationForm,
    buildDatasetRegistrationBody,
    metadataFactSummary,
    install,
  };
})();
if (typeof window !== 'undefined') {
  window.QuicTrainConsole = QuicTrainConsole;
}
