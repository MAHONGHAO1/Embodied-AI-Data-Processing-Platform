/** QuicStudio frontend global API client, encapsulating authentication, request interception, error handling, and business endpoints. */
const QuicDataAPI = (() => {
  const base = '/api/v1';
  const localPreviewHost = typeof window !== 'undefined' && ['localhost', '127.0.0.1'].includes(String(window.location?.hostname || ''));
  const urlParams = localPreviewHost && typeof window !== 'undefined' && window.location && typeof URLSearchParams !== 'undefined'
    ? new URLSearchParams(window.location.search || '')
    : null;
  const isMockRequested = Boolean(
    localPreviewHost &&
    urlParams &&
    (urlParams.get('demo') === '1' || urlParams.get('mock') === '1')
  );
  let demoMode = isMockRequested;
  let accessToken = '';
  let currentUserInfo = null;
  let refreshPromise = null;

  function rememberAuth(payload) {
    accessToken = payload.token || payload.access_token || '';
    currentUserInfo = payload.userInfo || null;
  }

  function clearAuth() {
    accessToken = '';
    currentUserInfo = null;
  }

  function currentUser() {
    return currentUserInfo;
  }

  function setQuery(url, params) {
    Object.entries(params || {}).forEach(([key, value]) => {
      if (value === undefined || value === null || value === '') return;
      if (Array.isArray(value)) {
        value.forEach((item) => {
          if (item !== undefined && item !== null && item !== '') {
            url.searchParams.append(key, String(item));
          }
        });
        return;
      }
      url.searchParams.set(key, String(value));
    });
  }

  function errorMessage(payload, fallback) {
    const detail = payload && payload.detail;
    if (typeof detail === 'string' && detail.trim()) return detail;
    if (Array.isArray(detail)) {
      const messages = detail
        .map((item) => {
          if (typeof item === 'string') return item;
          return item && typeof item.msg === 'string' ? item.msg : '';
        })
        .filter(Boolean);
      if (messages.length) return messages.join('；');
    }
    if (detail && typeof detail.message === 'string' && detail.message.trim()) return detail.message;
    if (typeof payload?.message === 'string' && payload.message.trim()) return payload.message;
    return fallback;
  }

  function responseError(response, payload, fallback) {
    const error = new Error(errorMessage(payload, fallback));
    error.status = response.status;
    error.detail = payload?.detail;
    error.code = payload?.detail?.code;
    return error;
  }

  async function refresh() {
    if (demoMode) {
      rememberAuth({ token: 'local-demo-token', userInfo: QuicDataDemo.user });
      return true;
    }
    if (refreshPromise) return refreshPromise;
    refreshPromise = (async () => {
      try {
        const response = await fetch(`${base}/auth/refresh`, {
          method: 'POST',
          credentials: 'same-origin',
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok || payload.code !== 200) {
          clearAuth();
          return false;
        }
        rememberAuth(payload.data || {});
        return true;
      } catch {
        clearAuth();
        return false;
      } finally {
        refreshPromise = null;
      }
    })();
    return refreshPromise;
  }

  async function request(method, path, { params, body, retried = false, timeoutMs } = {}) {
    if (demoMode) {
      return method === 'GET'
        ? QuicDataDemo.read(path, params)
        : QuicDataDemo.write(method, path, body);
    }
    const url = new URL(`${base}${path}`, window.location.origin);
    setQuery(url, params);
    const headers = {};
    if (accessToken) headers.Authorization = `Bearer ${accessToken}`;
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    const controller = typeof AbortController !== 'undefined' && timeoutMs ? new AbortController() : null;
    const timer = controller
      ? setTimeout(() => controller.abort(), Math.max(1, Number(timeoutMs) || 0))
      : null;
    let response;
    try {
       response = await fetch(url, {
         method,
         headers,
         credentials: 'same-origin',
         body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller ? controller.signal : undefined,
      });
    } catch (error) {
      if (controller && error && error.name === 'AbortError') {
        throw new Error('请求超时，请稍后重试');
      }
      throw error;
     } finally {
       if (timer) clearTimeout(timer);
     }
    const payload = await response.json().catch(() => ({}));
    if (response.status === 401 && !retried && await refresh()) {
      return request(method, path, { params, body, retried: true, timeoutMs });
    }
    if (response.status === 401) {
      clearAuth();
      throw responseError(response, payload, '登录已失效');
    }
    if (!response.ok || (payload.code !== undefined && payload.code !== 200)) {
      throw responseError(response, payload, `请求失败 (${response.status})`);
    }
    return payload.data;
  }

  async function download(path, params, retried = false) {
    if (demoMode) {
      const text = await QuicDataDemo.read(path, params);
      return new Blob([String(text)], { type: 'text/csv;charset=utf-8' });
    }
    const url = new URL(`${base}${path}`, window.location.origin);
    setQuery(url, params);
    const headers = {};
    if (accessToken) headers.Authorization = `Bearer ${accessToken}`;
    const response = await fetch(url, { method: 'GET', headers, credentials: 'same-origin' });
    if (response.status === 401 && !retried && await refresh()) return download(path, params, true);
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      if (response.status === 401) clearAuth();
      throw responseError(response, payload, `导出失败 (${response.status})`);
    }
    return response.blob();
  }

  async function trainRequest(method, path, { body, retried = false } = {}) {
    if (demoMode) {
      return method === 'GET'
        ? QuicDataDemo.trainRead(path)
        : QuicDataDemo.trainWrite(method, path, body);
    }
    const url = new URL(`/api/train/api/v1${path}`, window.location.origin);
    const headers = {};
    if (accessToken) headers.Authorization = `Bearer ${accessToken}`;
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    if (body && body.client_request_id) headers['Idempotency-Key'] = body.client_request_id;
    const response = await fetch(url, {
      method,
      headers,
      credentials: 'same-origin',
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const payload = await response.json().catch(() => ({}));
    if (response.status === 401 && !retried && await refresh()) {
      return trainRequest(method, path, { body, retried: true });
    }
    if (!response.ok) {
      const message = payload?.error?.message || payload?.detail || `训练请求失败 (${response.status})`;
      const error = new Error(typeof message === 'string' ? message : '训练请求失败');
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  return {
    isLocalPreview() {
      return Boolean(localPreviewHost && (demoMode || isMockRequested));
    },
    isDemoMode() {
      return demoMode;
    },
    enableDemoMode() {
      if (!localPreviewHost || typeof QuicDataDemo === 'undefined') return false;
      demoMode = true;
      rememberAuth({ token: 'local-demo-token', userInfo: QuicDataDemo.user });
      return true;
    },
    currentUser,
    getAccessToken() {
      return accessToken;
    },
    clearAuth,
    async restoreSession() {
      return refresh();
    },
    async getProductVersion() {
      // Public, unauthenticated; the local demo has no backend version.
      if (demoMode) return null;
      const response = await fetch(new URL(`${base}/version`, window.location.origin), { credentials: 'same-origin' });
      if (!response.ok) return null;
      const payload = await response.json();
      return typeof payload?.data?.version === 'string' ? payload.data.version : null;
    },
    async login(email, password) {
      if (demoMode) {
        rememberAuth({ token: 'local-demo-token', userInfo: QuicDataDemo.user });
        return QuicDataDemo.user;
      }
      const response = await fetch(`${base}/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({ email, password }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || payload.code !== 200) throw new Error(errorMessage(payload, '登录失败'));
      rememberAuth(payload.data || {});
      return payload.data;
    },
    async logout() {
      if (demoMode) {
        demoMode = false;
        clearAuth();
        return;
      }
      const headers = {};
      if (accessToken) headers.Authorization = `Bearer ${accessToken}`;
      let response;
      try {
        response = await fetch(`${base}/auth/logout`, {
          method: 'POST',
          headers,
          credentials: 'same-origin',
        });
      } catch {
        throw new Error('注销未完成，请检查网络后重试');
      }
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || payload.code !== 200) {
        throw new Error(errorMessage(payload, '注销未完成，请重试'));
      }
      clearAuth();
    },
    changePassword(body) {
      return request('POST', '/auth/change-password', { body });
    },
    listApiTokens() {
      return request('GET', '/tokens');
    },
    createApiToken(body) {
      return request('POST', '/tokens', { body });
    },
    rotateApiToken(id) {
      return request('POST', `/tokens/${encodeURIComponent(String(id))}/rotate`);
    },
    revokeApiToken(id) {
      return request('DELETE', `/tokens/${encodeURIComponent(String(id))}`);
    },
    listUsers() {
      return request('GET', '/auth/users');
    },
    listRoles() {
      return request('GET', '/auth/roles');
    },
    createManagedUser(body) {
      return request('POST', '/auth/users', { body });
    },
    updateUserRole(userId, role) {
      return request('PUT', `/auth/users/${encodeURIComponent(String(userId))}/role`, { body: { role } });
    },
    updateUserStatus(userId, isActive) {
      return request('PATCH', `/auth/users/${encodeURIComponent(String(userId))}/status`, { body: { is_active: isActive } });
    },
    resetUserPassword(userId) {
      return request('POST', `/auth/users/${encodeURIComponent(String(userId))}/reset-password`);
    },
    get(path, params) {
      return request('GET', path, { params });
    },
    post(path, body) {
      return request('POST', path, { body });
    },
    listWorkspaces(params = {}) {
      return request('GET', '/workspace/options', { params });
    },
    createWorkspace(body) {
      return request('POST', '/workspace/create', { body });
    },
    listTaskSets(workspaceId) {
      // Keep the old console state shape while reading the current project
      // resource. No request is sent to the retired task-set endpoint.
      return request('GET', '/collection-projects', { params: { workspace_id: workspaceId } })
        .then((payload) => ({ list: Array.isArray(payload?.items) ? payload.items : [] }));
    },
    createTaskSet(body) {
      return request('POST', '/collection-projects', { body: { workspace_id: body.workspace_id, name: body.name, description: body.description || '' } });
    },
    listWorkspaceMembers(workspaceId) {
      return request('GET', `/workspace/${encodeURIComponent(String(workspaceId))}/members`);
    },
    grantWorkspaceMember(workspaceId, body) {
      return request('POST', `/workspace/${encodeURIComponent(String(workspaceId))}/members`, { body });
    },
    revokeWorkspaceMember(workspaceId, userId) {
      return request(
        'DELETE',
        `/workspace/${encodeURIComponent(String(workspaceId))}/members/${encodeURIComponent(String(userId))}`,
      );
    },
    getPlatformSettings() {
      return request('GET', '/platform-settings');
    },
    getJob(jobId) {
      return request('GET', `/jobs/${encodeURIComponent(String(jobId))}`);
    },
    retryJob(jobId) {
      return request('POST', `/jobs/${encodeURIComponent(String(jobId))}/retry`);
    },
    listTaskLabels(params) {
      return request('GET', '/task-labels', { params });
    },
    createTaskLabel(body) {
      return request('POST', '/task-labels', { body });
    },
    listEpisodes(params) {
      return request('GET', '/episodes', { params });
    },
    listEpisodeAssets(params) {
      return request('GET', '/episodes/assets', { params });
    },
    getEpisode(episodeId) {
      return request('GET', `/episodes/${encodeURIComponent(String(episodeId))}`);
    },
    getEpisodePreviewUrl(episodeId) {
      return request('GET', `/episodes/${encodeURIComponent(String(episodeId))}/preview-url`);
    },
    getEpisodeRawSourceDownloads(episodeId) {
      return request('GET', `/episodes/${encodeURIComponent(String(episodeId))}/raw-source/downloads`);
    },
    getEpisodeWorkbench(episodeId, workItemId, options = {}) {
      return request('GET', `/episodes/${encodeURIComponent(String(episodeId))}/workbench`, {
        params: { work_item_id: workItemId },
        timeoutMs: options.timeoutMs,
      });
    },
    getEpisodeTimeline(episodeId, options = {}) {
      return request('GET', `/episodes/${encodeURIComponent(String(episodeId))}/timeline`, {
        timeoutMs: options.timeoutMs,
      });
    },
    getEpisodeMultimodalSession(episodeId) {
      return request('GET', `/episodes/${encodeURIComponent(String(episodeId))}/multimodal/session`);
    },
    getEpisodeAssets(episodeId) {
      return request('GET', `/episodes/${encodeURIComponent(String(episodeId))}/assets`);
    },
    listCollectorProfiles(workspaceId, includeInactive = false) {
      return request('GET', '/collector-profiles', { params: { workspace_id: workspaceId, include_inactive: includeInactive || undefined } });
    },
    createCollectorProfile(body) {
      return request('POST', '/collector-profiles', { body });
    },
    updateCollectorProfile(profileId, body) {
      return request('PATCH', `/collector-profiles/${encodeURIComponent(String(profileId))}`, { body });
    },
    listAvailableCollectorProfiles(workspaceId, query = '') {
      return request('GET', '/collector-profiles/available', { params: { workspace_id: workspaceId, query: query || undefined } });
    },
    grantCollectorMembership(workspaceId, profileId) {
      return request('POST', '/collector-profiles/memberships', { body: { workspace_id: workspaceId, personnel_profile_id: profileId } });
    },
    revokeCollectorMembership(workspaceId, profileId) {
      return request('DELETE', `/collector-profiles/${encodeURIComponent(String(profileId))}/memberships`, { params: { workspace_id: workspaceId } });
    },
    listCollectionDevices(workspaceId, includeInactive = false) {
      return request('GET', '/collection-devices', { params: { workspace_id: workspaceId, include_inactive: includeInactive || undefined } });
    },
    createCollectionDevice(body) {
      return request('POST', '/collection-devices', { body });
    },
    updateCollectionDevice(deviceId, body) {
      return request('PATCH', `/collection-devices/${encodeURIComponent(String(deviceId))}`, { body });
    },
    getEpisodeAiSuggestions(episodeId) {
      return request('GET', `/episodes/${encodeURIComponent(String(episodeId))}/ai-suggestions`);
    },
    createEpisodeAiSuggestions(episodeId, body) {
      return request('POST', `/episodes/${encodeURIComponent(String(episodeId))}/ai-suggestions`, { body });
    },
    retryEpisodeAiSuggestions(episodeId, jobId) {
      return request(
        'POST',
        `/episodes/${encodeURIComponent(String(episodeId))}/ai-suggestions/${encodeURIComponent(String(jobId))}/retry`,
      );
    },
    listDatasets(params) {
      return request('GET', '/datasets', { params });
    },
    listDatasetCatalog(params) {
      return request('GET', '/datasets/catalog', { params });
    },
    listNativeLerobotDatasets(params) {
      return request('GET', '/native-lerobot-datasets', { params });
    },
    getNativeLerobotDataset(datasetId) {
      return request('GET', `/native-lerobot-datasets/${encodeURIComponent(String(datasetId))}`);
    },
    getNativeLerobotDatasetOssUri(datasetId) {
      return request('GET', `/native-lerobot-datasets/${encodeURIComponent(String(datasetId))}/oss-uri`);
    },
    retryNativeLerobotCopy(datasetId) {
      return request('POST', `/native-lerobot-datasets/${encodeURIComponent(String(datasetId))}/copy/retry`);
    },
    reauthorizeNativeLerobotSource(datasetId, body) {
      return request(
        'POST',
        `/native-lerobot-datasets/${encodeURIComponent(String(datasetId))}/source-reauthorization`,
        { body },
      );
    },
    requestNativeLerobotBundle(datasetId) {
      return request('POST', `/native-lerobot-datasets/${encodeURIComponent(String(datasetId))}/bundles`);
    },
    getNativeLerobotBundle(datasetId, bundleId) {
      return request(
        'GET',
        `/native-lerobot-datasets/${encodeURIComponent(String(datasetId))}/bundles/${encodeURIComponent(String(bundleId))}`,
      );
    },
    getNativeLerobotBundleDownload(datasetId, bundleId) {
      return request(
        'GET',
        `/native-lerobot-datasets/${encodeURIComponent(String(datasetId))}/bundles/${encodeURIComponent(String(bundleId))}/download`,
      );
    },
    updateNativeLerobotDataset(datasetId, body) {
      return request('PATCH', `/native-lerobot-datasets/${encodeURIComponent(String(datasetId))}`, { body });
    },
    createDataset(body) {
      return request('POST', '/datasets', { body });
    },
    getDataset(datasetId) {
      return request('GET', `/datasets/${encodeURIComponent(String(datasetId))}`);
    },
    listDatasetRevisions(datasetId) {
      return request('GET', `/datasets/${encodeURIComponent(String(datasetId))}/revisions`);
    },
    listDatasetRevisionCandidates(datasetId, taskSetId = null) {
      return request('GET', `/datasets/${encodeURIComponent(String(datasetId))}/revision-candidates`, {
        params: { task_set_id: taskSetId || undefined },
      });
    },
    createDatasetRevision(datasetId, body) {
      return request('POST', `/datasets/${encodeURIComponent(String(datasetId))}/revisions`, { body });
    },
    getDatasetRevision(revisionId) {
      return request('GET', `/dataset-revisions/${encodeURIComponent(String(revisionId))}`);
    },
    exportDatasetRevision(revisionId, body = { export_profile: 'lerobot' }) {
      return request('POST', `/dataset-revisions/${encodeURIComponent(String(revisionId))}/exports`, { body });
    },
    getExport(jobId) {
      return request('GET', `/exports/${encodeURIComponent(String(jobId))}`);
    },
    getExportDownloadUrl(jobId) {
      return request('GET', `/exports/${encodeURIComponent(String(jobId))}/download-url`);
    },
    getExportDelivery(jobId) {
      return request('GET', `/exports/${encodeURIComponent(String(jobId))}/delivery`);
    },
    retryExport(jobId) {
      return request('POST', `/exports/${encodeURIComponent(String(jobId))}/retry`);
    },
    listWorkQueue(params) {
      return request('GET', '/work-queue', { params });
    },
    getDashboardOverview(scope = {}) {
      return request('GET', '/dashboard/overview', { params: scope });
    },
    refreshDashboard(scope = {}) {
      return request('POST', '/dashboard/refresh', { body: scope });
    },
    workQueueAction(workItemId, action, body) {
      const supported = new Set(['claim', 'continue', 'release', 'submit', 'review']);
      if (!supported.has(action)) throw new Error('不支持的工作项操作');
      return request('POST', `/work-queue/items/${encodeURIComponent(String(workItemId))}/${action}`, { body });
    },
    saveWorkbenchDraft(workItemId, body) {
      return request('PUT', `/work-queue/items/${encodeURIComponent(String(workItemId))}/draft`, { body });
    },
    listDataBatchCandidates(params) {
      return request('GET', '/data-batches/candidates', { params });
    },
    listDataBatches(params) {
      return request('GET', '/data-batches', { params });
    },
    getDataBatch(dataBatchId, params) {
      return request('GET', `/data-batches/${encodeURIComponent(String(dataBatchId))}`, { params });
    },
    getDataBatchGovernanceReport(dataBatchId, workspaceId) {
      return request('GET', `/data-batches/${encodeURIComponent(String(dataBatchId))}/governance-report`, {
        params: { workspace_id: workspaceId },
      });
    },
    createDataBatch(body) {
      return request('POST', '/data-batches', { body });
    },
    retryDataBatchGovernance(dataBatchId, body) {
      return request('POST', `/data-batches/${encodeURIComponent(String(dataBatchId))}/governance/retry`, { body });
    },
    listAnnotationWorkItems(params) {
      return request('GET', '/annotation-work-items', { params });
    },
    getPackageAnnotationWorkbench(itemId, workspaceId, mode = 'annotation') {
      if (!['annotation', 'review'].includes(mode)) return Promise.reject(new Error('Invalid workbench mode'));
      return request('GET', `/${mode === 'review' ? 'review' : 'annotation'}-work-items/${encodeURIComponent(String(itemId))}/workbench`, { params: { workspace_id: workspaceId } });
    },
    getPackageAnnotationEpisode(itemId, episodeId, workspaceId, mode = 'annotation') {
      if (!['annotation', 'review'].includes(mode)) return Promise.reject(new Error('Invalid workbench mode'));
      return request('GET', `/${mode === 'review' ? 'review' : 'annotation'}-work-items/${encodeURIComponent(String(itemId))}/episodes/${encodeURIComponent(String(episodeId))}/workbench`, { params: { workspace_id: workspaceId } });
    },
    saveAnnotationWorkItem(itemId, body) {
      return request('PATCH', `/annotation-work-items/${encodeURIComponent(String(itemId))}`, { body });
    },
    saveAnnotationDraft(itemId, body) {
      return request('PATCH', `/annotation-work-items/${encodeURIComponent(String(itemId))}`, { body });
    },
    submitAnnotationWorkItem(itemId, body) {
      return request('POST', `/annotation-work-items/${encodeURIComponent(String(itemId))}/submit`, { body });
    },
    reassignAnnotationWorkItem(itemId, body) {
      return request('POST', `/annotation-work-items/${encodeURIComponent(String(itemId))}/reassign`, { body });
    },
    listReviewWorkItems(params) {
      return request('GET', '/review-work-items', { params });
    },
    approveReviewWorkItem(itemId, body) {
      return request('POST', `/review-work-items/${encodeURIComponent(String(itemId))}/approve`, { body });
    },
    returnReviewWorkItem(itemId, body) {
      return request('POST', `/review-work-items/${encodeURIComponent(String(itemId))}/return`, { body });
    },
    reassignReviewWorkItem(itemId, body) {
      return request('POST', `/review-work-items/${encodeURIComponent(String(itemId))}/reassign`, { body });
    },
    listDataAssets(params = {}) {
      return request('GET', '/data-assets', { params });
    },
    getDataAsset(assetId) {
      return request('GET', `/data-assets/${encodeURIComponent(String(assetId))}`);
    },
    listCatalogDatasets(params = {}) {
      return request('GET', '/catalog-datasets', { params });
    },
    createCatalogDataset(body) {
      return request('POST', '/catalog-datasets', { body });
    },
    getCatalogDataset(datasetId) {
      return request('GET', `/catalog-datasets/${encodeURIComponent(String(datasetId))}`);
    },
    listCatalogVersions(datasetId, params = {}) {
      return request('GET', `/catalog-datasets/${encodeURIComponent(String(datasetId))}/versions`, { params });
    },
    getCatalogVersion(versionId) {
      return request('GET', `/catalog-datasets/versions/${encodeURIComponent(String(versionId))}`);
    },
    getCatalogExport(exportId) {
      return request('GET', `/catalog-datasets/exports/${encodeURIComponent(String(exportId))}`);
    },
    getCatalogExportDelivery(exportId, params = {}) {
      return request('GET', `/catalog-datasets/exports/${encodeURIComponent(String(exportId))}/download`, { params });
    },
    retryCatalogExport(exportId) {
      return request('POST', `/catalog-datasets/exports/${encodeURIComponent(String(exportId))}/retry`);
    },
    recoverCatalogExport(exportId) {
      return request('POST', `/catalog-datasets/exports/${encodeURIComponent(String(exportId))}/recover`);
    },
    createCatalogVersion(datasetId, body) {
      return request('POST', `/catalog-datasets/${encodeURIComponent(String(datasetId))}/versions`, { body });
    },
    exportCatalogVersion(versionId, body = { format: 'lerobot_3_0' }) {
      return request('POST', `/catalog-datasets/versions/${encodeURIComponent(String(versionId))}/export`, { body });
    },
    importLerobotCatalogDataset(body) {
      return request('POST', '/catalog-datasets/lerobot-imports', { body });
    },
    trainRequest,
    registerCatalogExport(versionId) {
      return request('POST', '/train/catalog-registrations', { body: { version_id: Number(versionId) } });
    },
    listCollectionProjects(workspaceId) {
      return request('GET', '/collection-projects', { params: { workspace_id: workspaceId } });
    },
    getCollectionProject(projectId, workspaceId) {
      return request('GET', `/collection-projects/${encodeURIComponent(String(projectId))}`, {
        params: { workspace_id: workspaceId },
      });
    },
    createCollectionProject(body) {
      return request('POST', '/collection-projects', { body });
    },
    updateCollectionProject(projectId, body) {
      return request('PATCH', `/collection-projects/${encodeURIComponent(String(projectId))}`, { body });
    },
    archiveCollectionProject(projectId, workspaceId) {
      return request('POST', `/collection-projects/${encodeURIComponent(String(projectId))}/archive`, {
        body: { workspace_id: workspaceId },
      });
    },
    listCollectionTasks(workspaceId, collectionProjectId = null) {
      const params = { workspace_id: workspaceId };
      if (collectionProjectId) params.collection_project_id = collectionProjectId;
      return request('GET', '/collection-tasks', { params });
    },
    getCollectionTask(taskId, workspaceId) {
      return request('GET', `/collection-tasks/${encodeURIComponent(String(taskId))}`, {
        params: { workspace_id: workspaceId },
      });
    },
    createCollectionTask(body) {
      return request('POST', '/collection-tasks', { body });
    },
    async listTaskPackages(taskId, workspaceId) {
      try {
        return await request('GET', `/collection-tasks/${encodeURIComponent(String(taskId))}/packages`, {
          params: { workspace_id: workspaceId },
        });
      } catch (err) {
        if (err?.status === 404) {
          return request('GET', '/data-packages', {
            params: { workspace_id: workspaceId, collection_task_id: taskId },
          });
        }
        throw err;
      }
    },
    listDataPackages(params) {
      return request('GET', '/data-packages', { params });
    },
    listDataPackageCreators(workspaceId) {
      return request('GET', '/data-packages/creators', { params: { workspace_id: workspaceId } });
    },
    getDataPackage(packageId, workspaceId) {
      return request('GET', `/data-packages/${encodeURIComponent(String(packageId))}`, {
        params: { workspace_id: workspaceId },
      });
    },
    getDataPackageEpisodePreviewUrls(packageId, episodeId, workspaceId) {
      return request(
        'GET',
        `/data-packages/${encodeURIComponent(String(packageId))}/episodes/${encodeURIComponent(String(episodeId))}/preview-urls`,
        { params: { workspace_id: workspaceId } },
      );
    },
    adjustDataPackages(body) {
      return request('POST', '/data-packages/adjust', { body });
    },
    createSupplementPackage(packageId, body) {
      return request('POST', `/data-packages/${encodeURIComponent(String(packageId))}/supplements`, { body });
    },
    batchAssignDataPackages(body) {
      return request('POST', '/data-packages/batch-assign', { body });
    },
    assignDataPackage(packageId, body) {
      return request('POST', `/data-packages/${encodeURIComponent(String(packageId))}/assign`, { body });
    },
    voidDataPackage(packageId, body) {
      return request('POST', `/data-packages/${encodeURIComponent(String(packageId))}/void`, { body });
    },
    getTaskOfflineManifest(taskId, workspaceId) {
      return request('GET', `/collection-tasks/${encodeURIComponent(String(taskId))}/offline-manifest`, {
        params: { workspace_id: workspaceId },
      });
    },
    getOfflineManifest(packageId, workspaceId) {
      return request('GET', `/data-packages/${encodeURIComponent(String(packageId))}/offline-manifest`, {
        params: { workspace_id: workspaceId },
      });
    },
    reviewIntakePackage(packageId, body) {
      return request('POST', `/data-packages/${encodeURIComponent(String(packageId))}/intake-review`, { body });
    },
    getIntakeReviewDraft(packageId, workspaceId) {
      return request('GET', `/data-packages/${encodeURIComponent(String(packageId))}/intake-review-draft`, {
        params: { workspace_id: workspaceId },
      });
    },
    saveIntakeReviewDraft(packageId, body) {
      return request('PATCH', `/data-packages/${encodeURIComponent(String(packageId))}/intake-review-draft`, { body });
    },
    bulkApproveIntakePackages(body) {
      return request('POST', '/data-packages/intake-review/bulk-approve', { body });
    },
    listCollectionLabels(workspaceIdOrParams, category = null, includeInactive = false) {
      let params;
      if (typeof workspaceIdOrParams === 'object' && workspaceIdOrParams !== null) {
        params = { ...workspaceIdOrParams };
      } else {
        params = { workspace_id: workspaceIdOrParams };
      }
      if (category) params.category = category;
      if (includeInactive) params.include_inactive = true;
      return request('GET', '/collection-labels', { params });
    },
    createCollectionLabel(body) {
      return request('POST', '/collection-labels', { body });
    },
    deactivateCollectionLabel(labelId, workspaceId) {
      return request('POST', `/collection-labels/${encodeURIComponent(String(labelId))}/deactivate`, {
        body: { workspace_id: workspaceId },
      });
    },
    getCollectionOverview(workspaceId, collectionProjectId = null) {
      const params = { workspace_id: workspaceId };
      if (collectionProjectId) params.collection_project_id = collectionProjectId;
      return request('GET', '/collection-overview', { params });
    },
    getCollectionDashboardData(params) {
      return request('GET', '/collection-dashboard/data', { params });
    },
    getCollectionDashboardCapacity(params) {
      return request('GET', '/collection-dashboard/capacity', { params });
    },
    getCollectionDashboardEfficiency(params) {
      return request('GET', '/collection-dashboard/efficiency', { params });
    },
    downloadCollectionDashboardCsv(params) {
      return download('/collection-dashboard/data', { ...params, format: 'csv' });
    },
  };
})();
