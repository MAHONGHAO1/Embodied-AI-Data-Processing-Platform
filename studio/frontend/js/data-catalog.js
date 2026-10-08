/**
 * Data asset and catalog dataset pages.
 *
 * This file is intentionally independent from app.js.  The host registers it
 * with `QuicDataCatalog.install(app)` and passes the current user/workspaces.
 * Every read and write goes through the real QuicDataAPI methods; this module
 * does not synthesize assets, versions, exports, or success states.
 */
const QuicDataCatalog = (() => {
  const PAGE_SIZE = 20;
  const MAX_PAGE_SIZE = 100;
  const EXPORT_FORMATS = Object.freeze(['lerobot_3_0', 'qrdf_0_2']);
  const PROCESSING_EXPORT_STATUSES = new Set(['queued', 'retry_pending', 'running', 'processing']);

  function runtimeApi() {
    // api.js declares this as a top-level const.  Prefer that lexical binding
    // and do not depend on a window/global property in the production page.
    if (typeof QuicDataAPI !== 'undefined') return QuicDataAPI;
    return null;
  }

  function idNumber(value) {
    if (value === null || value === undefined || value === '') return null;
    const number = Number(value);
    return Number.isSafeInteger(number) && number > 0 ? number : null;
  }

  function stringValue(value, fallback = '') {
    return typeof value === 'string' ? value : fallback;
  }

  function finiteNumber(value) {
    if (value === null || value === undefined || value === '') return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function unwrap(value) {
    if (value?.data && typeof value.data === 'object' && !Array.isArray(value.data)) return value.data;
    return value && typeof value === 'object' ? value : {};
  }

  function itemsOf(value) {
    const source = unwrap(value);
    if (Array.isArray(source.items)) return source.items;
    if (Array.isArray(source.list)) return source.list;
    if (Array.isArray(value)) return value;
    return [];
  }

  function hasPermission(user, permission) {
    if (!permission || typeof permission !== 'string') return false;
    const permissions = Array.isArray(user?.permissions)
      ? user.permissions.filter((item) => typeof item === 'string')
      : [];
    if (permissions.includes('*') || permissions.includes(permission)) return true;
    const separator = permission.indexOf(':');
    return separator > 0 && permissions.includes(`${permission.slice(0, separator)}:*`);
  }

  function isCatalogAdmin(user) {
    return user?.role === 'admin';
  }

  function permissionSnapshot(user) {
    // The current catalog router keeps writes/export/registration behind the
    // administrator gate in addition to the explicit permission matrix.  Keep
    // both checks in the UI so an operator with a future broad permission does
    // not see controls that the current server will reject.
    const isAdmin = isCatalogAdmin(user);
    return {
      read: hasPermission(user, 'dataset:read'),
      write: isAdmin && hasPermission(user, 'dataset:write'),
      export: isAdmin && (hasPermission(user, 'export:create') || hasPermission(user, 'export:write')),
      train: isAdmin && hasPermission(user, 'train:write'),
    };
  }

  function normalizeWorkspace(raw) {
    const source = raw && typeof raw === 'object' ? raw : {};
    return {
      ...source,
      id: idNumber(source.id ?? source.workspace_id),
      name: stringValue(source.name || source.workspace_name || source.label, ''),
    };
  }

  function workspaceName(workspaces, workspaceId) {
    const id = idNumber(workspaceId);
    if (id === null) return '—';
    const match = (Array.isArray(workspaces) ? workspaces : [])
      .map(normalizeWorkspace)
      .find((workspace) => workspace.id === id);
    return match?.name || `#${id}`;
  }

  function normalizeAsset(raw) {
    const source = raw && typeof raw === 'object' ? raw : {};
    return {
      ...source,
      id: idNumber(source.id ?? source.asset_id),
      data_batch_id: idNumber(source.data_batch_id ?? source.batch_id),
      workspace_id: idNumber(source.workspace_id ?? source.source_workspace_id),
      governed_valid_duration_hours: source.governed_valid_duration_hours ?? null,
      stage_snapshot_json: source.stage_snapshot_json && typeof source.stage_snapshot_json === 'object' ? source.stage_snapshot_json : {},
      episode_ids_json: Array.isArray(source.episode_ids_json) ? source.episode_ids_json : [],
      source_json: source.source_json && typeof source.source_json === 'object' ? source.source_json : {},
      source_snapshot_json: source.source_snapshot_json && typeof source.source_snapshot_json === 'object' ? source.source_snapshot_json : {},
      source_snapshot_id: stringValue(source.source_snapshot_id, ''),
    };
  }

  function normalizeExport(raw) {
    const source = raw?.job && typeof raw.job === 'object'
      ? raw.job
      : (raw?.export && typeof raw.export === 'object' ? raw.export : (raw && typeof raw === 'object' ? raw : {}));
    const detail = source.detail_json && typeof source.detail_json === 'object' ? source.detail_json : {};
    return {
      ...source,
      id: idNumber(source.id ?? source.export_id),
      version_id: idNumber(source.version_id),
      format: stringValue(source.format || source.export_profile, ''),
      status: stringValue(source.status, 'unknown').toLowerCase(),
      oss_uri: stringValue(source.oss_uri || detail.oss_uri, ''),
      checksum: stringValue(source.checksum || source.sha256, ''),
      size_bytes: finiteNumber(source.size_bytes),
      detail_json: detail,
      error_code: stringValue(source.error_code, ''),
      error_message: stringValue(source.error_message, ''),
      attempt: finiteNumber(source.attempt),
    };
  }

  function exportMapOf(raw) {
    const values = [];
    const source = raw && typeof raw === 'object' ? raw : {};
    if (Array.isArray(source.exports)) values.push(...source.exports);
    else if (source.exports && typeof source.exports === 'object') values.push(...Object.values(source.exports));
    if (Array.isArray(source.export_jobs)) values.push(...source.export_jobs);
    if (source.latest_export) values.push(source.latest_export);
    const result = {};
    values.forEach((item) => {
      const normalized = normalizeExport(item);
      if (!normalized.format) return;
      const previous = result[normalized.format];
      if (!previous || (normalized.id || 0) >= (previous.id || 0)) result[normalized.format] = normalized;
    });
    return result;
  }

  function normalizeVersion(raw) {
    const source = raw?.version && typeof raw.version === 'object' ? raw.version : (raw || {});
    const exports = exportMapOf(source);
    return {
      ...source,
      id: idNumber(source.id ?? source.version_id),
      dataset_id: idNumber(source.dataset_id),
      version: finiteNumber(source.version),
      status: stringValue(source.status, 'unknown').toLowerCase(),
      data_asset_ids: Array.isArray(source.data_asset_ids)
        ? source.data_asset_ids.map(idNumber).filter((id) => id !== null)
        : [],
      asset_snapshots: Array.isArray(source.asset_snapshots) ? source.asset_snapshots : [],
      source_snapshot_json: source.source_snapshot_json && typeof source.source_snapshot_json === 'object' ? source.source_snapshot_json : {},
      source_snapshot_id: stringValue(source.source_snapshot_id, ''),
      exports,
      latest_export: source.latest_export ? normalizeExport(source.latest_export) : null,
    };
  }

  function normalizeDataset(raw) {
    const source = raw?.dataset && typeof raw.dataset === 'object' ? raw.dataset : (raw || {});
    return {
      ...source,
      id: idNumber(source.id ?? source.dataset_id),
      name: stringValue(source.name || source.dataset_name, '—'),
      description: stringValue(source.description, ''),
      status: stringValue(source.status, 'unknown').toLowerCase(),
      source_kind: stringValue(source.source_kind, 'qrdf_assets'),
      created_at: stringValue(source.created_at, ''),
      updated_at: stringValue(source.updated_at, ''),
      native_lerobot_dataset_id: idNumber(source.native_lerobot_dataset_id),
    };
  }

  function normalizeAssetDetail(raw) {
    return normalizeAsset(unwrap(raw));
  }

  function canonicalNs(value) {
    if (typeof value === 'bigint') return value.toString();
    const normalized = String(value ?? '').trim();
    if (!/^(?:0|[1-9][0-9]{0,39})$/.test(normalized)) return '';
    try { return BigInt(normalized).toString(); } catch { return ''; }
  }

  function sumNs(segments) {
    let total = 0n;
    for (const segment of segments || []) {
      const start = canonicalNs(segment?.start_ns);
      const end = canonicalNs(segment?.end_ns);
      if (!start || !end || BigInt(end) <= BigInt(start)) continue;
      total += BigInt(end) - BigInt(start);
    }
    return total.toString();
  }

  function assetEpisodeRows(asset) {
    const source = normalizeAsset(asset);
    const snapshot = source.source_snapshot_json || {};
    const episodes = Array.isArray(snapshot.episodes) ? snapshot.episodes : [];
    if (episodes.length) {
      return episodes.map((episode, index) => {
        const segments = Array.isArray(episode.effective_segments) ? episode.effective_segments : [];
        const durationNs = canonicalNs(episode.effective_duration_ns) || sumNs(segments);
        return {
          ...episode,
          index,
          episode_id: idNumber(episode.episode_id),
          admission_attempt: idNumber(episode.admission_attempt),
          segments,
          duration_ns: durationNs,
          duration_s: durationNs ? Number(durationNs) / 1_000_000_000 : null,
          submission_id: idNumber(episode.annotation_submission_id || episode.submission_id),
          conclusion: stringValue(episode.annotation_conclusion || episode.conclusion, segments.length ? 'segments' : ''),
        };
      });
    }
    return source.episode_ids_json.map((id, index) => ({ episode_id: idNumber(id), index, segments: [], duration_ns: '', duration_s: null, conclusion: '' }));
  }

  function assetEffectiveDurationSeconds(asset) {
    const source = normalizeAsset(asset);
    const rows = assetEpisodeRows(source);
    const ns = rows.reduce((total, row) => total + (canonicalNs(row.duration_ns) ? BigInt(row.duration_ns) : 0n), 0n);
    // A published annotated asset's effective duration is the exact sum of
    // its frozen intervals.  The legacy governed-hours field is a fallback
    // only for unannotated or historical snapshots without interval data.
    const hasSnapshotDuration = rows.some((row) => canonicalNs(row.duration_ns) !== '' || row.segments.length > 0);
    if (hasSnapshotDuration) return Number(ns) / 1_000_000_000;
    const direct = finiteNumber(source.governed_valid_duration_hours);
    return direct === null ? null : direct * 3600;
  }

  function pageSlice(items, page = 1, pageSize = PAGE_SIZE) {
    const list = Array.isArray(items) ? items : [];
    const size = Math.max(1, Math.min(MAX_PAGE_SIZE, Number(pageSize) || PAGE_SIZE));
    const total = list.length;
    const pages = Math.max(1, Math.ceil(total / size));
    const current = Math.max(1, Math.min(pages, Number(page) || 1));
    return { items: list.slice((current - 1) * size, current * size), total, pages, page: current, pageSize: size };
  }

  function formatDuration(seconds) {
    const value = finiteNumber(seconds);
    if (value === null || value < 0) return '—';
    if (value > 0 && value < 0.001) return `${Math.round(value * 1_000_000_000)} ns`;
    if (value > 0 && value < 1) return `${value.toFixed(3)} s`;
    const total = Math.floor(value);
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const rest = total % 60;
    return hours
      ? `${hours}:${String(minutes).padStart(2, '0')}:${String(rest).padStart(2, '0')}`
      : `${minutes}:${String(rest).padStart(2, '0')}`;
  }

  function formatBytes(bytes) {
    const value = finiteNumber(bytes);
    if (value === null || value < 0) return '—';
    if (value < 1024) return `${value} B`;
    const units = ['KB', 'MB', 'GB', 'TB'];
    let number = value;
    let index = -1;
    while (number >= 1024 && index < units.length - 1) { number /= 1024; index += 1; }
    return `${number >= 10 ? number.toFixed(0) : number.toFixed(1)} ${units[index]}`;
  }

  function formatDate(value, locale = 'zh-CN') {
    if (!value) return '—';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    try { return new Intl.DateTimeFormat(locale, { dateStyle: 'medium', timeStyle: 'short' }).format(date); } catch { return String(value); }
  }

  function exportIsProcessing(exportRow) {
    return PROCESSING_EXPORT_STATUSES.has(String(exportRow?.status || '').toLowerCase());
  }

  function stableOssUri(exportRow) {
    const value = stringValue(exportRow?.oss_uri || exportRow?.detail_json?.oss_uri, '').trim();
    return value.startsWith('oss://') && value !== 'oss://' ? value : '';
  }

  function canRegisterExport(exportRow, user) {
    return Boolean(
      isCatalogAdmin(user)
      && hasPermission(user, 'train:write')
      && exportRow?.status === 'succeeded'
      && exportRow?.format === 'lerobot_3_0'
      && stableOssUri(exportRow),
    );
  }

  let exportActionSequence = 0;

  function newExportIdempotencyKey(versionId, format) {
    const id = idNumber(versionId);
    if (id === null || !EXPORT_FORMATS.includes(format)) return '';
    exportActionSequence += 1;
    const entropy = Math.random().toString(36).slice(2, 10) || '0';
    return `catalog:${id}:${format}:${Date.now().toString(36)}-${exportActionSequence}-${entropy}`;
  }

  function buildExportBody(versionId, format, idempotencyKey = '') {
    const id = idNumber(versionId);
    if (id === null || !EXPORT_FORMATS.includes(format)) return null;
    const key = String(idempotencyKey || '').trim() || newExportIdempotencyKey(id, format);
    return { format, idempotency_key: key };
  }

  function buildDeliveryRequest(exportId, { ttlSeconds = 3600, network = 'internal' } = {}) {
    const id = idNumber(exportId);
    if (id === null) return null;
    return { url_ttl_seconds: Math.max(60, Math.min(3600, Number(ttlSeconds) || 3600)), oss_network: network === 'public' ? 'public' : 'internal' };
  }

  function apiMethod(api, method) {
    const active = api || runtimeApi();
    if (!active || typeof active[method] !== 'function') {
      const error = new Error(`QuicDataAPI.${method} is unavailable.`);
      error.code = 'api_method_unavailable';
      throw error;
    }
    return active[method].bind(active);
  }

  function errorMessage(error, fallback = 'Request failed.') {
    if (error?.code === 'api_method_unavailable') return '当前操作暂不可用，请稍后重试。';
    if (error?.status === 401) return '登录已失效，请重新登录。';
    if (error?.status === 403) return '没有数据集权限，无法读取或执行此操作。';
    if (error?.status === 404) return '请求的资产、数据集或导出不存在。';
    if (error?.status === 409) return error?.message || '数据状态已变化，请刷新后重试。';
    return error?.message || fallback;
  }

  function createCatalogLoaders(api = null) {
    let assetsGeneration = 0;
    let datasetsGeneration = 0;
    let versionsGeneration = 0;
    let detailGeneration = 0;
    let disposed = false;
    function loadAssets({ sourceWorkspaceId = null, page = 1, limit = PAGE_SIZE } = {}) {
      const token = ++assetsGeneration;
      const params = {};
      const workspaceId = idNumber(sourceWorkspaceId);
      if (workspaceId !== null) params.source_workspace_id = workspaceId;
      const boundedLimit = Math.max(1, Math.min(MAX_PAGE_SIZE, Number(limit) || PAGE_SIZE));
      const boundedPage = Math.max(1, Number(page) || 1);
      params.limit = boundedLimit;
      params.offset = (boundedPage - 1) * boundedLimit;
      return Promise.resolve().then(() => apiMethod(api, 'listDataAssets')(params)).then((payload) => {
        if (disposed || token !== assetsGeneration) return { stale: true };
        const source = unwrap(payload);
        const items = itemsOf(payload).map(normalizeAsset);
        const hasTotal = source.total !== null && source.total !== undefined && Number.isFinite(Number(source.total));
        return {
          stale: false,
          items,
          total: hasTotal ? Math.max(0, Number(source.total)) : items.length,
          limit: Number(source.limit) || boundedLimit,
          offset: Number(source.offset) || params.offset,
          serverPaged: hasTotal,
        };
      });
    }
    function loadDatasets({ page = 1, limit = PAGE_SIZE } = {}) {
      const token = ++datasetsGeneration;
      const boundedLimit = Math.max(1, Math.min(MAX_PAGE_SIZE, Number(limit) || PAGE_SIZE));
      const boundedPage = Math.max(1, Number(page) || 1);
      const params = { limit: boundedLimit, offset: (boundedPage - 1) * boundedLimit };
      return Promise.resolve().then(() => apiMethod(api, 'listCatalogDatasets')(params)).then((payload) => {
        if (disposed || token !== datasetsGeneration) return { stale: true };
        const source = unwrap(payload);
        const items = itemsOf(payload).map(normalizeDataset);
        const hasTotal = source.total !== null && source.total !== undefined && Number.isFinite(Number(source.total));
        return {
          stale: false,
          items,
          total: hasTotal ? Math.max(0, Number(source.total)) : items.length,
          limit: Number(source.limit) || boundedLimit,
          offset: Number(source.offset) || params.offset,
          serverPaged: hasTotal,
        };
      });
    }
    function loadVersions(datasetId, { page = 1, limit = PAGE_SIZE } = {}) {
      const token = ++versionsGeneration;
      const id = idNumber(datasetId);
      if (id === null) return Promise.resolve({ stale: false, items: [] });
      const boundedLimit = Math.max(1, Math.min(MAX_PAGE_SIZE, Number(limit) || PAGE_SIZE));
      const boundedPage = Math.max(1, Number(page) || 1);
      const params = { limit: boundedLimit, offset: (boundedPage - 1) * boundedLimit };
      return Promise.resolve().then(() => apiMethod(api, 'listCatalogVersions')(id, params)).then((payload) => {
        if (disposed || token !== versionsGeneration) return { stale: true };
        const source = unwrap(payload);
        const items = itemsOf(payload).map(normalizeVersion);
        const hasTotal = source.total !== null && source.total !== undefined && Number.isFinite(Number(source.total));
        return {
          stale: false,
          items,
          total: hasTotal ? Math.max(0, Number(source.total)) : items.length,
          limit: Number(source.limit) || boundedLimit,
          offset: Number(source.offset) || params.offset,
          serverPaged: hasTotal,
        };
      });
    }
    function loadAssetDetail(assetId) {
      const token = ++detailGeneration;
      const id = idNumber(assetId);
      if (id === null) return Promise.resolve({ stale: false, detail: null });
      return Promise.resolve().then(() => apiMethod(api, 'getDataAsset')(id)).then((payload) => {
        if (disposed || token !== detailGeneration) return { stale: true };
        const detail = normalizeAssetDetail(payload);
        if (detail.id !== null && detail.id !== id) {
          const error = new Error('The asset detail response belongs to another asset.');
          error.code = 'stale_asset_response';
          error.stale = true;
          throw error;
        }
        return { stale: false, detail };
      });
    }
    function loadVersionDetail(versionId) {
      const token = ++detailGeneration;
      const id = idNumber(versionId);
      if (id === null) return Promise.resolve({ stale: false, detail: null, unsupported: true });
      const active = api || runtimeApi();
      if (active && typeof active.getCatalogVersion === 'function') {
        return Promise.resolve().then(() => active.getCatalogVersion(id)).then((payload) => {
          if (disposed || token !== detailGeneration) return { stale: true };
          return { stale: false, detail: normalizeVersion(unwrap(payload)), unsupported: false };
        });
      }
      if (active && typeof active.listCatalogVersionExports === 'function') {
        return Promise.resolve().then(() => active.listCatalogVersionExports(id)).then((payload) => {
          if (disposed || token !== detailGeneration) return { stale: true };
          return { stale: false, detail: { id, exports: exportMapOf(payload) }, unsupported: false };
        });
      }
      return Promise.resolve({ stale: false, detail: null, unsupported: true });
    }
    function invalidate() {
      assetsGeneration += 1;
      datasetsGeneration += 1;
      versionsGeneration += 1;
      detailGeneration += 1;
    }
    function dispose() { disposed = true; invalidate(); }
    return { loadAssets, loadDatasets, loadVersions, loadAssetDetail, loadVersionDetail, invalidate, dispose };
  }

  function install(app) {
    if (!app || typeof app.component !== 'function') throw new TypeError('Vue app is required');
    app.component('data-catalog', {
      name: 'DataCatalog',
      props: {
        mode: { type: String, default: 'assets' },
        locale: { type: String, default: 'zh-CN' },
        user: { type: Object, default: null },
        workspaces: { type: Array, default: () => [] },
      },
      emits: ['view-episode', 'registered', 'dataset-created', 'version-created'],
      setup(props, { emit }) {
        const state = Vue.reactive({
          loading: false,
          detailLoading: false,
          versionsLoading: false,
          actionLoading: false,
          error: '',
          detailError: '',
          versionsError: '',
          actionError: '',
          assets: [],
          assetTotal: 0,
          assetServerPaged: false,
          assetWorkspaceId: '',
          assetPage: 1,
          assetPageSize: PAGE_SIZE,
          selectedAssetId: null,
          selectedAsset: null,
          datasets: [],
          datasetTotal: 0,
          datasetServerPaged: false,
          datasetPage: 1,
          datasetPageSize: PAGE_SIZE,
          selectedDatasetId: null,
          selectedDataset: null,
          versions: [],
          versionTotal: 0,
          versionServerPaged: false,
          versionPage: 1,
          versionPageSize: PAGE_SIZE,
          selectedVersionId: null,
          selectedVersion: null,
          candidateAssets: [],
          candidateAssetTotal: 0,
          candidateAssetServerPaged: false,
          candidateAssetPageNumber: 1,
          candidateAssetPageSize: PAGE_SIZE,
          candidateAssetsLoaded: false,
          candidateAssetsLoading: false,
          versionSelectedAssetIds: [],
          showDatasetDialog: false,
          showVersionDialog: false,
          datasetName: '',
          datasetDescription: '',
          deliveryNetwork: 'internal',
          deliveryTtlSeconds: 3600,
          deliveryError: '',
          deliveryUrl: '',
          registrationError: '',
          mounted: false,
        });
        const loaders = createCatalogLoaders();
        let disposed = false;
        let pollTimer = null;
        let loadGeneration = 0;
        let deliveryGeneration = 0;
        const exportAttemptKeys = new Map();

        const english = Vue.computed(() => String(props.locale || '').toLowerCase().startsWith('en'));
        const text = (zh, en) => (english.value ? en : zh);
        const mode = Vue.computed(() => props.mode === 'datasets' ? 'datasets' : 'assets');
        const permissions = Vue.computed(() => permissionSnapshot(props.user));
        const canRead = Vue.computed(() => permissions.value.read);
        const canWrite = Vue.computed(() => permissions.value.write);
        const canExport = Vue.computed(() => permissions.value.export);
        const canTrain = Vue.computed(() => permissions.value.train);
        const workspaceOptions = Vue.computed(() => (Array.isArray(props.workspaces) ? props.workspaces : []).map(normalizeWorkspace).filter((item) => item.id !== null));
        const assetPage = Vue.computed(() => {
          if (state.assetServerPaged) {
            const pageSize = Math.max(1, Number(state.assetPageSize) || PAGE_SIZE);
            const total = Math.max(state.assetTotal, state.assets.length);
            return {
              items: state.assets,
              total,
              pages: Math.max(1, Math.ceil(total / pageSize)),
              page: Math.max(1, state.assetPage),
              pageSize,
            };
          }
          return pageSlice(state.assets, state.assetPage, state.assetPageSize);
        });
        const datasetPage = Vue.computed(() => {
          if (state.datasetServerPaged) {
            const pageSize = Math.max(1, Number(state.datasetPageSize) || PAGE_SIZE);
            const total = Math.max(state.datasetTotal, state.datasets.length);
            return {
              items: state.datasets,
              total,
              pages: Math.max(1, Math.ceil(total / pageSize)),
              page: Math.max(1, state.datasetPage),
              pageSize,
            };
          }
          return pageSlice(state.datasets, state.datasetPage, state.datasetPageSize);
        });
        const selectedVersionExports = Vue.computed(() => state.selectedVersion?.exports || {});
        const selectedVersionProcessing = Vue.computed(() => Object.values(selectedVersionExports.value).some(exportIsProcessing));
        const candidateAssetPage = Vue.computed(() => {
          if (state.candidateAssetServerPaged) {
            const pageSize = Math.max(1, Number(state.candidateAssetPageSize) || PAGE_SIZE);
            const total = Math.max(state.candidateAssetTotal, state.candidateAssets.length);
            return {
              items: state.candidateAssets,
              total,
              pages: Math.max(1, Math.ceil(total / pageSize)),
              page: Math.max(1, state.candidateAssetPageNumber),
              pageSize,
            };
          }
          return pageSlice(state.candidateAssets, state.candidateAssetPageNumber, state.candidateAssetPageSize);
        });
        const versionPage = Vue.computed(() => {
          if (state.versionServerPaged) {
            const pageSize = Math.max(1, Number(state.versionPageSize) || PAGE_SIZE);
            return {
              items: state.versions,
              total: Math.max(state.versionTotal, state.versions.length),
              pages: Math.max(1, Math.ceil(Math.max(state.versionTotal, state.versions.length) / pageSize)),
              page: Math.max(1, state.versionPage),
              pageSize,
            };
          }
          return pageSlice(state.versions, state.versionPage, state.versionPageSize);
        });

        function assetWorkspaceName(asset) { return workspaceName(props.workspaces, asset?.workspace_id); }
        function episodeRows(asset) { return assetEpisodeRows(asset); }
        function effectiveDuration(asset) { return assetEffectiveDurationSeconds(asset); }
        function statusLabel(status) {
          return {
            active: text('有效', 'Active'), archived: text('已归档', 'Archived'),
            not_started: text('未生成', 'Not generated'),
            queued: text('排队中', 'Queued'), retry_pending: text('等待重试', 'Retry pending'),
            running: text('处理中', 'Running'), processing: text('处理中', 'Processing'),
            succeeded: text('已完成', 'Succeeded'), failed: text('失败', 'Failed'), cancelled: text('已取消', 'Cancelled'),
          }[String(status || '').toLowerCase()] || (status || '—');
        }
        function formatExportName(format) { return format === 'qrdf_0_2' ? 'QRDF 0.2' : 'LeRobot 3.0'; }
        function sourceKindLabel(kind) { return kind === 'lerobot_direct' ? text('原生 LeRobot', 'Native LeRobot') : text('QRDF 资产', 'QRDF assets'); }
        function selectedVersionAssetIds(version = state.selectedVersion) {
          return Array.isArray(version?.data_asset_ids) ? version.data_asset_ids : [];
        }
        function exportFor(format) { return selectedVersionExports.value[format] || null; }
        function exportRegisterReason(exportRow) {
          if (!canTrain.value) return text('需要 train:write 权限。', 'train:write permission is required.');
          if (!exportRow || exportRow.format !== 'lerobot_3_0') return text('只能登记 LeRobot 3.0 导出。', 'Only a LeRobot 3.0 export can be registered.');
          if (exportRow.status !== 'succeeded') return text('导出完成后才能登记。', 'The export must succeed before registration.');
          if (!stableOssUri(exportRow)) return text('导出尚未产生稳定的 oss:// URI。', 'The export has no stable oss:// URI yet.');
          return '';
        }

        function clearPoll() {
          if (pollTimer !== null && typeof clearTimeout === 'function') clearTimeout(pollTimer);
          pollTimer = null;
        }
        function documentVisible() { return typeof document === 'undefined' || document.visibilityState !== 'hidden'; }
        function schedulePoll() {
          clearPoll();
          if (disposed || !state.mounted || mode.value !== 'datasets' || !state.selectedVersionId || !selectedVersionProcessing.value || !documentVisible()) return;
          pollTimer = setTimeout(async () => {
            pollTimer = null;
            if (disposed || !state.mounted || !documentVisible()) return;
            await refreshSelectedVersion();
            schedulePoll();
          }, 10000);
        }

        async function loadAssets({ preserveSelection = false } = {}) {
          const token = ++loadGeneration;
          state.loading = true;
          state.error = '';
          state.assets = [];
          state.assetTotal = 0;
          state.assetServerPaged = false;
          if (!preserveSelection) {
            state.selectedAsset = null;
            state.selectedAssetId = null;
            state.selectedDataset = null;
            state.selectedDatasetId = null;
            state.versions = [];
            state.selectedVersion = null;
            state.selectedVersionId = null;
            clearPoll();
          }
          try {
            const result = await loaders.loadAssets({ sourceWorkspaceId: state.assetWorkspaceId || null, page: state.assetPage, limit: state.assetPageSize });
            if (disposed || token !== loadGeneration || result.stale) return;
            state.assets = result.items;
            state.assetTotal = result.total;
            state.assetServerPaged = Boolean(result.serverPaged);
            if (result.limit) state.assetPageSize = result.limit;
          } catch (error) {
            if (!disposed && token === loadGeneration) state.error = errorMessage(error, text('资产列表加载失败，请重试。', 'Failed to load assets.'));
          } finally {
            if (!disposed && token === loadGeneration) state.loading = false;
          }
        }

        async function loadDatasets({ preserveSelection = false } = {}) {
          const token = ++loadGeneration;
          state.loading = true;
          state.error = '';
          state.datasets = [];
          state.datasetTotal = 0;
          state.datasetServerPaged = false;
          if (!preserveSelection) {
            state.selectedDataset = null;
            state.selectedDatasetId = null;
            state.versions = [];
            state.selectedVersion = null;
            state.selectedVersionId = null;
            clearPoll();
          }
          try {
            const result = await loaders.loadDatasets({ page: state.datasetPage, limit: state.datasetPageSize });
            if (disposed || token !== loadGeneration || result.stale) return;
            state.datasets = result.items;
            state.datasetTotal = result.total;
            state.datasetServerPaged = Boolean(result.serverPaged);
            if (result.limit) state.datasetPageSize = result.limit;
          } catch (error) {
            if (!disposed && token === loadGeneration) state.error = errorMessage(error, text('数据集列表加载失败，请重试。', 'Failed to load datasets.'));
          } finally {
            if (!disposed && token === loadGeneration) state.loading = false;
          }
        }

        async function loadScope() {
          if (!canRead.value) {
            ++loadGeneration;
            loaders.invalidate();
            state.error = text('当前账号没有 dataset:read 权限。', 'This account does not have dataset:read permission.');
            state.loading = false;
            state.assets = [];
            state.datasets = [];
            state.assetTotal = 0;
            state.datasetTotal = 0;
            state.assetServerPaged = false;
            state.datasetServerPaged = false;
            return;
          }
          if (mode.value === 'assets') await loadAssets();
          else await loadDatasets();
        }

        async function selectAsset(asset) {
          const id = idNumber(asset?.id);
          if (id === null) return;
          state.selectedAssetId = id;
          state.selectedAsset = null;
          state.detailError = '';
          state.detailLoading = true;
          try {
            const result = await loaders.loadAssetDetail(id);
            if (disposed || result.stale || state.selectedAssetId !== id) return;
            state.selectedAsset = result.detail;
          } catch (error) {
            if (!disposed && state.selectedAssetId === id) state.detailError = errorMessage(error, text('资产详情加载失败。', 'Failed to load asset detail.'));
          } finally {
            if (!disposed && state.selectedAssetId === id) state.detailLoading = false;
          }
        }

        function viewEpisode(asset, episode) {
          const episodeId = idNumber(episode?.episode_id);
          if (episodeId === null) return;
          emit('view-episode', { asset_id: idNumber(asset?.id), episode_id: episodeId, workspace_id: idNumber(asset?.workspace_id), asset, episode });
        }

        async function selectDataset(dataset) {
          const id = idNumber(dataset?.id);
          if (id === null) return;
          state.selectedDatasetId = id;
          state.selectedDataset = dataset;
          state.versions = [];
          state.versionTotal = 0;
          state.versionServerPaged = false;
          state.versionPage = 1;
          state.selectedVersion = null;
          state.selectedVersionId = null;
          state.versionsError = '';
          state.versionsLoading = true;
          clearPoll();
          try {
            const result = await loaders.loadVersions(id, { page: 1, limit: state.datasetPageSize });
            if (disposed || result.stale || state.selectedDatasetId !== id) return;
            state.versions = result.items;
            state.versionTotal = result.total;
            state.versionServerPaged = Boolean(result.serverPaged);
            state.versionPage = 1;
            if (result.limit) state.versionPageSize = result.limit;
            // With a paged response, the first page is not necessarily the
            // newest version (the service keeps version numbers ascending).
            // Let the reviewer choose a page before loading a version that is
            // not visible; legacy unpaged responses retain the convenient
            // latest-version selection.
            if (state.versions.length && (!result.serverPaged || result.total <= result.items.length)) {
              await selectVersion(state.versions[state.versions.length - 1]);
            }
          } catch (error) {
            if (!disposed && state.selectedDatasetId === id) state.versionsError = errorMessage(error, text('版本列表加载失败。', 'Failed to load versions.'));
          } finally {
            if (!disposed && state.selectedDatasetId === id) state.versionsLoading = false;
          }
        }

        async function selectVersion(version) {
          const id = idNumber(version?.id);
          if (id === null) return;
          state.selectedVersionId = id;
          state.selectedVersion = normalizeVersion(version);
          state.deliveryError = '';
          state.deliveryUrl = '';
          await refreshSelectedVersion();
        }

        function mergeVersion(next) {
          const normalized = normalizeVersion(next);
          if (normalized.id === null || normalized.id !== state.selectedVersionId) return;
          const merged = {
            ...(state.selectedVersion || {}),
            ...normalized,
            exports: { ...(state.selectedVersion?.exports || {}), ...(normalized.exports || {}) },
          };
          state.selectedVersion = merged;
          state.versions = state.versions.map((item) => item.id === merged.id ? merged : item);
        }

        async function refreshSelectedVersion() {
          const id = idNumber(state.selectedVersionId);
          if (id === null || disposed) return;
          try {
            const result = await loaders.loadVersionDetail(id);
            if (disposed || result.stale || state.selectedVersionId !== id) return;
            if (result.unsupported) {
              clearPoll();
              state.versionsError = text('版本导出状态暂不可读取，请刷新重试。', 'Export status is temporarily unavailable. Refresh and try again.');
              return;
            }
            if (result.detail) mergeVersion({ ...(state.selectedVersion || {}), ...result.detail, id });
            schedulePoll();
          } catch (error) {
            if (!disposed) state.versionsError = errorMessage(error, text('版本状态刷新失败。', 'Failed to refresh version status.'));
          }
        }

        async function ensureCandidateAssets() {
          if (state.candidateAssetsLoaded || state.candidateAssetsLoading) return;
          state.candidateAssetsLoading = true;
          state.actionError = '';
          try {
            const result = await loaders.loadAssets({ page: 1, limit: MAX_PAGE_SIZE });
            if (disposed || result.stale) return;
            state.candidateAssets = result.items;
            state.candidateAssetTotal = result.total;
            state.candidateAssetServerPaged = Boolean(result.serverPaged);
            state.candidateAssetPageNumber = 1;
            if (result.limit) state.candidateAssetPageSize = result.limit;
            state.candidateAssetsLoaded = true;
          } catch (error) {
            if (!disposed) state.actionError = errorMessage(error, text('可用资产加载失败。', 'Failed to load candidate assets.'));
          } finally {
            if (!disposed) state.candidateAssetsLoading = false;
          }
        }

        function openDatasetDialog() {
          if (!canWrite.value) return;
          state.actionError = '';
          state.datasetName = '';
          state.datasetDescription = '';
          state.showDatasetDialog = true;
        }
        async function createDataset() {
          const name = String(state.datasetName || '').trim();
          if (!canWrite.value || !name || state.actionLoading) return;
          state.actionLoading = true;
          state.actionError = '';
          try {
            const result = await apiMethod(null, 'createCatalogDataset')({ name, description: String(state.datasetDescription || '').trim(), source_kind: 'qrdf_assets' });
            const dataset = normalizeDataset(unwrap(result));
            state.showDatasetDialog = false;
            emit('dataset-created', dataset);
            await loadDatasets();
            if (dataset.id) await selectDataset(dataset);
          } catch (error) {
            state.actionError = errorMessage(error, text('数据集创建失败。', 'Failed to create dataset.'));
          } finally { state.actionLoading = false; }
        }

        async function openVersionDialog() {
          if (!canWrite.value || !state.selectedDatasetId) return;
          state.versionSelectedAssetIds = [];
          state.actionError = '';
          state.showVersionDialog = true;
          await ensureCandidateAssets();
        }
        function toggleCandidateAsset(assetId) {
          const id = idNumber(assetId);
          if (id === null) return;
          const selected = new Set(state.versionSelectedAssetIds);
          if (selected.has(id)) selected.delete(id); else selected.add(id);
          state.versionSelectedAssetIds = [...selected];
        }
        async function createVersion() {
          if (!canWrite.value || !state.selectedDatasetId || !state.versionSelectedAssetIds.length || state.actionLoading) return;
          state.actionLoading = true;
          state.actionError = '';
          try {
            const result = await apiMethod(null, 'createCatalogVersion')(state.selectedDatasetId, { data_asset_ids: state.versionSelectedAssetIds });
            const version = normalizeVersion(unwrap(result));
            state.showVersionDialog = false;
            emit('version-created', { dataset: state.selectedDataset, version });
            await selectDataset(state.selectedDataset);
            if (version.id) await selectVersion(version);
          } catch (error) {
            state.actionError = errorMessage(error, text('数据集版本创建失败。', 'Failed to create dataset version.'));
          } finally { state.actionLoading = false; }
        }

        async function exportVersion(format) {
          const version = state.selectedVersion;
          const versionId = idNumber(version?.id);
          if (versionId === null || !EXPORT_FORMATS.includes(format) || !canExport.value || state.actionLoading) return;
          if (state.selectedDataset?.source_kind === 'lerobot_direct' && format === 'qrdf_0_2') return;
          const attemptScope = `${versionId}:${format}`;
          const body = buildExportBody(versionId, format, exportAttemptKeys.get(attemptScope));
          if (!body) return;
          exportAttemptKeys.set(attemptScope, body.idempotency_key);
          state.actionLoading = true;
          state.actionError = '';
          try {
            await apiMethod(null, 'exportCatalogVersion')(versionId, body);
            // A resolved API call confirms that the server accepted this
            // action.  A later refresh failure must not cause a duplicate
            // submission, so clear the key before refreshing the detail.
            exportAttemptKeys.delete(attemptScope);
            await refreshSelectedVersion();
          } catch (error) {
            // Keep the key when the request is not confirmed.  The next
            // explicit click is a retry of this action, not a new export.
            state.actionError = errorMessage(error, text('导出请求失败，未显示成功状态。', 'Export request failed; no success state was shown.'));
          } finally { state.actionLoading = false; }
        }

        async function retryExport(format) {
          const exportRow = exportFor(format);
          if (!exportRow?.id || !canExport.value || state.actionLoading) return;
          state.actionLoading = true;
          state.actionError = '';
          try {
            const active = runtimeApi();
            const retry = active?.retryCatalogExport || active?.retryCatalogExportJob;
            if (typeof retry !== 'function') throw Object.assign(new Error('QuicDataAPI.retryCatalogExport is unavailable.'), { code: 'api_method_unavailable' });
            await retry.call(active, exportRow.id);
            await refreshSelectedVersion();
          } catch (error) {
            state.actionError = errorMessage(error, text('导出重试失败。', 'Export retry failed.'));
          } finally { state.actionLoading = false; }
        }

        async function deliverExport(format) {
          const exportRow = exportFor(format);
          if (!exportRow?.id || exportRow.status !== 'succeeded') return;
          const generation = ++deliveryGeneration;
          const context = deliveryContext();
          const isCurrent = () => !disposed && generation === deliveryGeneration && context === deliveryContext();
          state.deliveryError = '';
          state.deliveryUrl = '';
          try {
            const active = runtimeApi();
            const deliver = active?.getCatalogExportDownloadUrl || active?.getCatalogExportDelivery || active?.getCatalogExportDownload;
            if (typeof deliver === 'function') {
              const request = buildDeliveryRequest(exportRow.id, { ttlSeconds: state.deliveryTtlSeconds, network: state.deliveryNetwork });
              const result = await deliver.call(active, exportRow.id, request);
              if (!isCurrent()) return;
              const descriptor = unwrap(result);
              const url = stringValue(descriptor.url || descriptor.download_url, '');
              if (!url) throw new Error(text('服务端没有返回下载地址。', 'The server returned no download URL.'));
              state.deliveryUrl = url;
              return;
            }
            const uri = stableOssUri(exportRow);
            if (/^https?:\/\//.test(uri)) {
              state.deliveryUrl = uri;
              return;
            }
            throw new Error(text('当前导出尚无可用的下载地址，请稍后重试。', 'This export does not have a delivery URL yet. Please try again later.'));
          } catch (error) { if (isCurrent()) state.deliveryError = errorMessage(error, text('下载地址获取失败。', 'Failed to obtain a delivery URL.')); }
        }

        function deliveryContext() {
          return JSON.stringify([props.user, mode.value, state.selectedDatasetId, state.selectedVersionId, state.deliveryNetwork, state.deliveryTtlSeconds]);
        }

        function invalidateDelivery() {
          deliveryGeneration += 1;
          state.deliveryUrl = '';
          state.deliveryError = '';
        }

        async function copyExportUri(format) {
          const uri = stableOssUri(exportFor(format));
          if (!uri) return;
          try {
            if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) await navigator.clipboard.writeText(uri);
            else if (typeof document !== 'undefined' && typeof document.execCommand === 'function') {
              const input = document.createElement('textarea'); input.value = uri; input.setAttribute('readonly', ''); input.style.position = 'fixed'; document.body.appendChild(input); input.select(); document.execCommand('copy'); input.remove();
            }
            state.deliveryError = text('已复制稳定 URI。', 'Stable URI copied.');
          } catch (error) { state.deliveryError = errorMessage(error, text('复制失败。', 'Copy failed.')); }
        }

        async function registerExport() {
          const version = state.selectedVersion;
          const exportRow = exportFor('lerobot_3_0');
          const reason = exportRegisterReason(exportRow);
          if (reason || !version?.id || state.actionLoading) { state.registrationError = reason; return; }
          state.actionLoading = true;
          state.registrationError = '';
          try {
            const result = await apiMethod(null, 'registerCatalogExport')(version.id);
            emit('registered', { version, export: exportRow, result });
          } catch (error) { state.registrationError = errorMessage(error, text('登记训练数据集失败。', 'Training dataset registration failed.')); }
          finally { state.actionLoading = false; }
        }

        function refreshCurrent() { return mode.value === 'assets' ? loadAssets() : loadDatasets(); }
        function setAssetPage(page) {
          const next = Math.max(1, Number(page) || 1);
          const changed = next !== state.assetPage;
          state.assetPage = next;
          if (changed && state.assetServerPaged) void loadAssets({ preserveSelection: true });
        }
        function setDatasetPage(page) {
          const next = Math.max(1, Number(page) || 1);
          const changed = next !== state.datasetPage;
          state.datasetPage = next;
          if (changed && state.datasetServerPaged) void loadDatasets({ preserveSelection: true });
        }
        async function setCandidateAssetPage(page) {
          const next = Math.max(1, Number(page) || 1);
          if (next === state.candidateAssetPageNumber || !state.candidateAssetServerPaged) {
            state.candidateAssetPageNumber = next;
            return;
          }
          state.candidateAssetPageNumber = next;
          state.candidateAssetsLoading = true;
          state.actionError = '';
          try {
            const result = await loaders.loadAssets({ page: next, limit: state.candidateAssetPageSize });
            if (disposed || result.stale || state.candidateAssetPageNumber !== next) return;
            state.candidateAssets = result.items;
            state.candidateAssetTotal = result.total;
            state.candidateAssetServerPaged = Boolean(result.serverPaged);
            if (result.limit) state.candidateAssetPageSize = result.limit;
          } catch (error) {
            if (!disposed) state.actionError = errorMessage(error, text('可用资产加载失败。', 'Failed to load candidate assets.'));
          } finally {
            if (!disposed) state.candidateAssetsLoading = false;
          }
        }
        async function setVersionPage(page) {
          const next = Math.max(1, Number(page) || 1);
          if (next === state.versionPage || !state.selectedDatasetId || !state.versionServerPaged) {
            state.versionPage = next;
            return;
          }
          state.versionPage = next;
          const datasetId = state.selectedDatasetId;
          state.versionsLoading = true;
          state.versionsError = '';
          try {
            const result = await loaders.loadVersions(datasetId, { page: next, limit: state.versionPageSize });
            if (disposed || result.stale || state.selectedDatasetId !== datasetId || state.versionPage !== next) return;
            state.versions = result.items;
            state.versionTotal = result.total;
            state.versionServerPaged = Boolean(result.serverPaged);
            if (result.limit) state.versionPageSize = result.limit;
          } catch (error) {
            if (!disposed && state.selectedDatasetId === datasetId) state.versionsError = errorMessage(error, text('版本列表加载失败。', 'Failed to load versions.'));
          } finally {
            if (!disposed && state.selectedDatasetId === datasetId) state.versionsLoading = false;
          }
        }
        function toggleVersionAsset(assetId) { toggleCandidateAsset(assetId); }

        function visibilityChanged() { if (documentVisible()) schedulePoll(); else clearPoll(); }
        Vue.watch(() => [mode.value, props.user, state.assetWorkspaceId], () => { if (!disposed) void loadScope(); }, { immediate: true });
        Vue.watch(deliveryContext, invalidateDelivery, { flush: 'sync' });
        Vue.onMounted(() => {
          state.mounted = true;
          if (typeof document !== 'undefined') document.addEventListener('visibilitychange', visibilityChanged);
          schedulePoll();
        });
        Vue.onBeforeUnmount(() => {
          disposed = true;
          state.mounted = false;
          clearPoll();
          if (typeof document !== 'undefined') document.removeEventListener('visibilitychange', visibilityChanged);
          loaders.dispose();
        });

        return {
          state, mode, english, text, permissions, canRead, canWrite, canExport, canTrain,
          workspaceOptions, assetPage, datasetPage,
          selectedVersionExports, selectedVersionProcessing, candidateAssetPage, versionPage,
          assetWorkspaceName, episodeRows, effectiveDuration, statusLabel, formatExportName, sourceKindLabel,
          selectedVersionAssetIds, exportFor, exportIsProcessing, exportRegisterReason, stableOssUri,
          loadScope, loadAssets, loadDatasets, refreshCurrent, selectAsset, viewEpisode, selectDataset, selectVersion,
          setAssetPage, setDatasetPage, setVersionPage, setCandidateAssetPage,
          openDatasetDialog, createDataset, openVersionDialog, toggleVersionAsset, createVersion,
          exportVersion, retryExport, deliverExport, copyExportUri, registerExport,
          formatDuration, formatBytes, formatDate,
        };
      },
      template: `
        <section class="data-catalog" :class="'mode-' + mode" :aria-busy="state.loading || state.actionLoading">
          <div v-if="!canRead" class="data-catalog-state is-error"><strong>{{ text('没有 dataset:read 权限', 'dataset:read permission required') }}</strong><p>{{ text('服务端不会返回资产或数据集列表。', 'The server list is not requested without read permission.') }}</p></div>
          <template v-else-if="mode === 'assets'">
            <div class="data-catalog-toolbar"><label><span>{{ text('来源数采工作空间', 'Source collection workspace') }}</span><select v-model="state.assetWorkspaceId" :disabled="state.loading"><option value="">{{ text('全部数采工作空间', 'All collection workspaces') }}</option><option v-for="workspace in workspaceOptions" :key="workspace.id" :value="workspace.id">{{ workspace.name || ('#' + workspace.id) }}</option></select></label><button type="button" class="data-catalog-button secondary data-catalog-toolbar-refresh" :disabled="state.loading" @click="refreshCurrent">{{ text('刷新', 'Refresh') }}</button></div>
            <div v-if="state.error" class="data-catalog-alert error">{{ state.error }}</div>
            <div v-if="state.loading" class="data-catalog-state"><span class="data-catalog-spinner"></span><p>{{ text('正在读取资产…', 'Loading assets…') }}</p></div>
            <div v-else class="data-catalog-columns asset-columns"><section class="data-catalog-card data-catalog-list-card"><div class="data-catalog-card-heading"><div><h2>{{ text('资产清单', 'Asset list') }}</h2><span>{{ assetPage.total }} {{ text('条', 'items') }}</span></div></div><div v-if="assetPage.items.length" class="data-catalog-table-wrap"><table class="data-catalog-table"><thead><tr><th>ID</th><th>{{ text('数据批', 'Batch') }}</th><th>{{ text('数采工作空间', 'Collection workspace') }}</th><th>{{ text('有效时长', 'Effective duration') }}</th><th>{{ text('创建时间', 'Created') }}</th></tr></thead><tbody><tr v-for="asset in assetPage.items" :key="asset.id" :class="{ selected: state.selectedAssetId === asset.id }" @click="selectAsset(asset)"><td>#{{ asset.id }}</td><td>#{{ asset.data_batch_id || '—' }}</td><td>{{ assetWorkspaceName(asset) }}</td><td>{{ formatDuration(effectiveDuration(asset)) }}</td><td>{{ formatDate(asset.created_at, locale) }}</td></tr></tbody></table></div><div v-else class="data-catalog-empty">{{ text('没有可查看的逻辑资产。', 'No logical assets are available.') }}</div><div v-if="assetPage.pages > 1" class="data-catalog-pagination"><button type="button" :disabled="assetPage.page <= 1" @click="setAssetPage(assetPage.page - 1)">‹</button><span>{{ assetPage.page }} / {{ assetPage.pages }}</span><button type="button" :disabled="assetPage.page >= assetPage.pages" @click="setAssetPage(assetPage.page + 1)">›</button></div></section><aside class="data-catalog-card data-catalog-detail-card"><div v-if="state.detailLoading" class="data-catalog-state compact"><span class="data-catalog-spinner"></span><p>{{ text('正在读取冻结快照…', 'Loading frozen snapshot…') }}</p></div><div v-else-if="state.detailError" class="data-catalog-alert error">{{ state.detailError }}</div><template v-else-if="state.selectedAsset"><div class="data-catalog-card-heading"><div><h2>#{{ state.selectedAsset.id }}</h2><span>{{ text('冻结资产详情', 'Frozen asset detail') }}</span></div><code v-if="state.selectedAsset.source_snapshot_id">{{ state.selectedAsset.source_snapshot_id.slice(0, 12) }}…</code></div><dl class="data-catalog-facts"><div><dt>{{ text('数据批', 'Batch') }}</dt><dd>#{{ state.selectedAsset.data_batch_id || '—' }}</dd></div><div><dt>{{ text('数采工作空间', 'Collection workspace') }}</dt><dd>{{ assetWorkspaceName(state.selectedAsset) }}</dd></div><div><dt>{{ text('有效时长', 'Effective duration') }}</dt><dd>{{ formatDuration(effectiveDuration(state.selectedAsset)) }}</dd></div><div><dt>{{ text('Episode', 'Episodes') }}</dt><dd>{{ episodeRows(state.selectedAsset).length }}</dd></div></dl><div class="data-catalog-episode-detail"><h3>{{ text('冻结 Episode 与标注区间', 'Frozen Episodes and annotation ranges') }}</h3><div v-for="episode in episodeRows(state.selectedAsset)" :key="episode.episode_id + '-' + (episode.admission_attempt || '')" class="data-catalog-episode-row"><div class="data-catalog-episode-heading"><strong>#{{ episode.episode_id }}</strong><span>{{ episode.duration_s === null ? '—' : formatDuration(episode.duration_s) }}</span><button type="button" class="link-button" @click="viewEpisode(state.selectedAsset, episode)">{{ text('查看', 'View') }}</button></div><small v-if="episode.submission_id">{{ text('提交', 'Submission') }} #{{ episode.submission_id }} · {{ text('准入 attempt', 'Admission attempt') }} {{ episode.admission_attempt || '—' }}</small><div v-if="episode.segments.length" class="data-catalog-segments"><div v-for="segment in episode.segments" :key="segment.id || (segment.start_ns + '-' + segment.end_ns)"><code>{{ segment.start_ns }} → {{ segment.end_ns }}</code><span>{{ segment.description || text('无描述', 'No description') }}</span></div></div><p v-else class="data-catalog-muted">{{ episode.conclusion === 'no_valid_segments' ? text('本 Episode 无有效片段。', 'No valid segments for this Episode.') : text('此快照没有区间明细。', 'No interval details in this snapshot.') }}</p></div></div></template><div v-else class="data-catalog-empty">{{ text('选择资产查看冻结提交和有效区间。', 'Select an asset to inspect its frozen submission and ranges.') }}</div></aside></div>
          </template>
          <template v-else>
            <div class="data-catalog-toolbar"><button v-if="canWrite" type="button" class="data-catalog-button primary" @click="openDatasetDialog">{{ text('新建数据集', 'New dataset') }}</button><button type="button" class="data-catalog-button secondary data-catalog-toolbar-refresh" :disabled="state.loading" @click="refreshCurrent">{{ text('刷新', 'Refresh') }}</button></div>
            <div v-if="state.error" class="data-catalog-alert error">{{ state.error }}</div>
            <div v-if="state.actionError" class="data-catalog-alert error">{{ state.actionError }}</div>
            <div v-if="state.loading" class="data-catalog-state"><span class="data-catalog-spinner"></span><p>{{ text('正在读取数据集目录…', 'Loading dataset catalog…') }}</p></div>
            <div v-else class="data-catalog-columns dataset-columns"><section class="data-catalog-card data-catalog-list-card"><div class="data-catalog-card-heading"><div><h2>{{ text('数据集', 'Datasets') }}</h2><span>{{ datasetPage.total }} {{ text('条', 'items') }}</span></div></div><div v-if="datasetPage.items.length" class="data-catalog-table-wrap"><table class="data-catalog-table"><thead><tr><th>ID</th><th>{{ text('名称', 'Name') }}</th><th>{{ text('来源', 'Source') }}</th><th>{{ text('状态', 'Status') }}</th><th>{{ text('更新时间', 'Updated') }}</th></tr></thead><tbody><tr v-for="dataset in datasetPage.items" :key="dataset.id" :class="{ selected: state.selectedDatasetId === dataset.id }" @click="selectDataset(dataset)"><td>#{{ dataset.id }}</td><td><strong>{{ dataset.name }}</strong><small>{{ dataset.description || '—' }}</small></td><td>{{ sourceKindLabel(dataset.source_kind) }}</td><td><span class="data-catalog-status">{{ statusLabel(dataset.status) }}</span></td><td>{{ formatDate(dataset.updated_at || dataset.created_at, locale) }}</td></tr></tbody></table></div><div v-else class="data-catalog-empty">{{ text('没有数据集。', 'No datasets are available.') }}</div><div v-if="datasetPage.pages > 1" class="data-catalog-pagination"><button type="button" :disabled="datasetPage.page <= 1" @click="setDatasetPage(datasetPage.page - 1)">‹</button><span>{{ datasetPage.page }} / {{ datasetPage.pages }}</span><button type="button" :disabled="datasetPage.page >= datasetPage.pages" @click="setDatasetPage(datasetPage.page + 1)">›</button></div></section><aside class="data-catalog-card data-catalog-version-card"><div v-if="!state.selectedDataset" class="data-catalog-empty">{{ text('选择数据集查看固定版本。', 'Select a dataset to inspect immutable versions.') }}</div><template v-else><div class="data-catalog-card-heading"><div><h2>{{ state.selectedDataset.name }}</h2><span>{{ sourceKindLabel(state.selectedDataset.source_kind) }} · #{{ state.selectedDataset.id }}</span></div><button v-if="canWrite && state.selectedDataset.source_kind === 'qrdf_assets'" type="button" class="data-catalog-button primary small" @click="openVersionDialog">{{ text('创建版本', 'Create version') }}</button></div><div v-if="state.versionsError" class="data-catalog-alert error">{{ state.versionsError }}</div><div v-if="state.versionsLoading" class="data-catalog-state compact"><span class="data-catalog-spinner"></span><p>{{ text('正在读取版本…', 'Loading versions…') }}</p></div><div v-else-if="!state.versions.length" class="data-catalog-empty">{{ text('当前数据集没有版本。', 'This dataset has no versions.') }}</div><div v-else class="data-catalog-version-list"><button v-for="version in versionPage.items" :key="version.id" type="button" :class="['data-catalog-version', { selected: state.selectedVersionId === version.id }]" @click="selectVersion(version)"><span>v{{ version.version || '—' }}</span><small>{{ statusLabel(version.status) }} · {{ version.data_asset_ids.length }} {{ text('项资产', 'assets') }}</small></button></div><div v-if="versionPage.pages > 1" class="data-catalog-pagination"><button type="button" :disabled="versionPage.page <= 1 || state.versionsLoading" @click="setVersionPage(versionPage.page - 1)">‹</button><span>{{ versionPage.page }} / {{ versionPage.pages }}</span><button type="button" :disabled="versionPage.page >= versionPage.pages || state.versionsLoading" @click="setVersionPage(versionPage.page + 1)">›</button></div><template v-if="state.selectedVersion"><div class="data-catalog-version-detail"><div class="data-catalog-card-heading"><div><h3>v{{ state.selectedVersion.version || '—' }}</h3><span>{{ state.selectedVersion.data_asset_ids.length }} {{ text('项冻结资产', 'frozen assets') }} · {{ statusLabel(state.selectedVersion.status) }}</span></div><span v-if="selectedVersionProcessing" class="data-catalog-status is-processing">{{ text('导出处理中', 'Exporting') }}</span></div><div class="data-catalog-version-assets"><h4>{{ text('资产清单', 'Asset list') }}</h4><button v-for="assetId in selectedVersionAssetIds()" :key="assetId" type="button" class="data-catalog-asset-chip" @click="selectAsset({ id: assetId })">#{{ assetId }}</button></div><div class="data-catalog-delivery-settings"><label><span>{{ text('交付网络', 'Delivery network') }}</span><select v-model="state.deliveryNetwork"><option value="internal">{{ text('内网', 'Internal') }}</option><option value="public">{{ text('公网', 'Public') }}</option></select></label><label><span>{{ text('地址有效期（秒）', 'URL TTL (seconds)') }}</span><input v-model.number="state.deliveryTtlSeconds" type="number" min="60" max="3600" step="60" /></label></div><div class="data-catalog-export-grid"><article v-for="format in ['lerobot_3_0', 'qrdf_0_2']" :key="format" class="data-catalog-export-card" :class="{ disabled: state.selectedDataset.source_kind === 'lerobot_direct' && format === 'qrdf_0_2' }"><div><strong>{{ formatExportName(format) }}</strong><span>{{ statusLabel(exportFor(format)?.status || 'not_started') }}</span></div><p v-if="exportFor(format)?.error_message" class="data-catalog-alert error">{{ exportFor(format).error_message }}</p><code v-if="stableOssUri(exportFor(format))">{{ stableOssUri(exportFor(format)) }}</code><div class="data-catalog-export-actions"><button type="button" class="data-catalog-button primary small" :disabled="!canExport || state.actionLoading || exportIsProcessing(exportFor(format)) || (state.selectedDataset.source_kind === 'lerobot_direct' && format === 'qrdf_0_2')" @click="exportVersion(format)">{{ exportFor(format) ? text('重新导出', 'Export again') : text('生成导出', 'Create export') }}</button><button v-if="exportFor(format)?.status === 'failed'" type="button" class="data-catalog-button secondary small" :disabled="!canExport || state.actionLoading" @click="retryExport(format)">{{ text('重试', 'Retry') }}</button><button v-if="exportFor(format)?.status === 'succeeded'" type="button" class="data-catalog-button secondary small" @click="deliverExport(format)">{{ text('下载 / 交付', 'Download / deliver') }}</button><button v-if="stableOssUri(exportFor(format))" type="button" class="link-button" @click="copyExportUri(format)">{{ text('复制 URI', 'Copy URI') }}</button></div><button v-if="format === 'lerobot_3_0'" type="button" class="data-catalog-register" :disabled="Boolean(exportRegisterReason(exportFor(format))) || state.actionLoading" :title="exportRegisterReason(exportFor(format))" @click="registerExport">{{ text('登记到训练', 'Register to training') }}</button></article></div><div v-if="state.deliveryError" class="data-catalog-alert info">{{ state.deliveryError }}</div><a v-if="state.deliveryUrl" class="data-catalog-delivery-link" :href="state.deliveryUrl" target="_blank" rel="noopener">{{ text('打开交付地址', 'Open delivery URL') }}</a><p v-if="state.registrationError" class="data-catalog-alert error">{{ state.registrationError }}</p><p class="data-catalog-muted">{{ text('导出状态来自服务端持久记录；离开页面后重新进入仍会恢复。', 'Export status is read from durable server records and restored after re-entry.') }}</p></div></template></template></aside></div>
            <div v-if="state.showDatasetDialog" class="data-catalog-modal-backdrop"><div class="data-catalog-modal" role="dialog" aria-modal="true"><h2>{{ text('新建数据集', 'New dataset') }}</h2><label><span>{{ text('名称', 'Name') }}</span><input v-model="state.datasetName" maxlength="128" /></label><label><span>{{ text('描述', 'Description') }}</span><textarea v-model="state.datasetDescription" maxlength="4000"></textarea></label><div class="data-catalog-modal-actions"><button type="button" class="data-catalog-button secondary" :disabled="state.actionLoading" @click="state.showDatasetDialog = false">{{ text('取消', 'Cancel') }}</button><button type="button" class="data-catalog-button primary" :disabled="state.actionLoading || !state.datasetName.trim()" @click="createDataset">{{ text('创建', 'Create') }}</button></div></div></div>
                        <div v-if="state.showVersionDialog" class="data-catalog-modal-backdrop"><div class="data-catalog-modal wide" role="dialog" aria-modal="true"><h2>{{ text('创建数据集版本', 'Create dataset version') }}</h2><p class="data-catalog-muted">{{ text('选择已冻结资产；版本只保存资产快照引用。', 'Select frozen assets; the version stores immutable asset references.') }}</p><div v-if="state.candidateAssetsLoading" class="data-catalog-state compact"><span class="data-catalog-spinner"></span><p>{{ text('读取资产…', 'Loading assets…') }}</p></div><div v-else class="data-catalog-candidate-list"><label v-for="asset in candidateAssetPage.items" :key="asset.id" class="data-catalog-candidate"><input type="checkbox" :checked="state.versionSelectedAssetIds.includes(asset.id)" @change="toggleVersionAsset(asset.id)" /><span><strong>#{{ asset.id }}</strong><small>{{ assetWorkspaceName(asset) }} · {{ formatDuration(effectiveDuration(asset)) }} · batch #{{ asset.data_batch_id || '—' }}</small></span></label><p v-if="!candidateAssetPage.items.length" class="data-catalog-muted">{{ text('没有可选择的资产。', 'No candidate assets are available.') }}</p></div><div v-if="candidateAssetPage.pages > 1" class="data-catalog-pagination"><button type="button" :disabled="candidateAssetPage.page <= 1 || state.candidateAssetsLoading" @click="setCandidateAssetPage(candidateAssetPage.page - 1)">‹</button><span>{{ candidateAssetPage.page }} / {{ candidateAssetPage.pages }}</span><button type="button" :disabled="candidateAssetPage.page >= candidateAssetPage.pages || state.candidateAssetsLoading" @click="setCandidateAssetPage(candidateAssetPage.page + 1)">›</button></div><div class="data-catalog-modal-actions"><span>{{ state.versionSelectedAssetIds.length }} {{ text('项已选', 'selected') }}</span><button type="button" class="data-catalog-button secondary" :disabled="state.actionLoading" @click="state.showVersionDialog = false">{{ text('取消', 'Cancel') }}</button><button type="button" class="data-catalog-button primary" :disabled="state.actionLoading || !state.versionSelectedAssetIds.length" @click="createVersion">{{ text('创建版本', 'Create version') }}</button></div></div></div>
          </template>
        </section>`,
    });
    return app;
  }

  return {
    PAGE_SIZE,
    EXPORT_FORMATS,
    hasPermission,
    permissionSnapshot,
    normalizeWorkspace,
    workspaceName,
    normalizeAsset,
    normalizeAssetDetail,
    normalizeExport,
    normalizeVersion,
    normalizeDataset,
    assetEpisodeRows,
    assetEffectiveDurationSeconds,
    pageSlice,
    formatDuration,
    formatBytes,
    formatDate,
    exportIsProcessing,
    stableOssUri,
    canRegisterExport,
    newExportIdempotencyKey,
    buildExportBody,
    buildDeliveryRequest,
    createCatalogLoaders,
    errorMessage,
    install,
  };
})();
