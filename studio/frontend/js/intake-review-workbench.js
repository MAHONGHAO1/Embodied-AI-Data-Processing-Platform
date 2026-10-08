/**
 * Package intake review workbench.
 *
 * This module deliberately owns only the package review surface.  The host
 * application supplies workspaceId/packageId and mounts the component through
 * `QuicDataIntakeReviewWorkbench.install(app)`.
 */
const QuicDataIntakeReviewWorkbench = (() => {
  const EMPTY_DRAFT = Object.freeze({
    viewed_episode_ids: [],
    rejected_episodes: {},
    last_episode_id: null,
  });
  const ELIGIBLE_STATUSES = new Set(['passed', 'accepted', 'ready', 'eligible', 'success', 'valid']);
  const BUSY_PACKAGE_STATUSES = new Set(['pending_upload', 'uploading', 'parsing', 'ingested']);
  const FINAL_PACKAGE_STATUS = 'pending_intake_review';
  const MAX_REASON_LENGTH = 1000;

  function own(value, key) {
    return Boolean(value && Object.prototype.hasOwnProperty.call(value, key));
  }

  function clone(value) {
    if (value === null || value === undefined) return value;
    if (typeof structuredClone === 'function') {
      try { return structuredClone(value); } catch { /* fall through */ }
    }
    if (Array.isArray(value)) return value.map((item) => clone(item));
    if (typeof value === 'object') {
      return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, clone(item)]));
    }
    return value;
  }

  function numberId(value) {
    if (value === null || value === undefined || value === '') return null;
    const number = Number(value);
    return Number.isSafeInteger(number) && number > 0 ? number : null;
  }

  function keyId(value) {
    const id = numberId(value);
    return id === null ? '' : String(id);
  }

  function asCount(value, fallback = 0) {
    const number = Number(value);
    return Number.isFinite(number) && number >= 0 ? Math.floor(number) : fallback;
  }

  function textReason(value) {
    return typeof value === 'string' ? value.slice(0, MAX_REASON_LENGTH) : '';
  }

  function episodeIdOf(episode) {
    return numberId(episode?.id ?? episode?.episode_id);
  }

  function isEpisodeEligible(episode) {
    if (!episode || episode.system_excluded === true || episode.excluded === true) return false;
    if (episode.admission_eligible === true || episode.eligible === true) return true;
    const status = String(episode.admission_status ?? episode.admissionStatus ?? '').toLowerCase();
    return ELIGIBLE_STATUSES.has(status);
  }

  function episodeExclusionReason(episode) {
    return episode?.admission_reason
      || episode?.exclusion_reason
      || episode?.excluded_reason
      || episode?.reason
      || '';
  }

  function normalizeEpisode(episode, index = 0) {
    const source = episode && typeof episode === 'object' ? episode : {};
    const id = episodeIdOf(source);
    const eligible = isEpisodeEligible(source);
    return {
      ...source,
      id,
      episode_id: id ?? source.episode_id,
      index,
      eligible: Boolean(id !== null && eligible),
      systemExcluded: Boolean(id === null || !eligible),
      exclusionReason: episodeExclusionReason(source),
      label: source.episode_uid || source.uid || (id === null ? `Episode ${index + 1}` : `Episode ${id}`),
    };
  }

  function normalizeEpisodes(payload) {
    const source = payload?.episodes;
    if (!Array.isArray(source)) return [];
    return source.map((episode, index) => normalizeEpisode(episode, index));
  }

  function deriveAdmissionCounts(payload, episodes = normalizeEpisodes(payload)) {
    const supplied = payload?.admission_counts && typeof payload.admission_counts === 'object'
      ? payload.admission_counts
      : {};
    const readyFallback = episodes.filter((episode) => episode.eligible).length;
    const runningFallback = episodes.filter((episode) => {
      const status = String(episode.admission_status || '').toLowerCase();
      return status === 'running' || status === 'processing' || status === 'pending';
    }).length;
    const failedFallback = episodes.filter((episode) => {
      const status = String(episode.admission_status || '').toLowerCase();
      return status === 'failed' || status === 'error';
    }).length;
    return {
      ready: asCount(supplied.ready, readyFallback),
      running: asCount(supplied.running, runningFallback),
      failed: asCount(supplied.failed, failedFallback),
      total: asCount(supplied.total, episodes.length),
    };
  }

  function normalizePackage(payload) {
    const source = payload?.data && typeof payload.data === 'object' && !Array.isArray(payload.data)
      ? payload.data
      : (payload && typeof payload === 'object' ? payload : {});
    const episodes = normalizeEpisodes(source);
    const status = String(source.status ?? source.raw_status ?? '');
    const admissionCounts = deriveAdmissionCounts(source, episodes);
    const uploadingOrParsing = BUSY_PACKAGE_STATUSES.has(status)
      || status === 'uploading'
      || status === 'parsing';
    const canReview = status === FINAL_PACKAGE_STATUS
      && admissionCounts.running === 0
      && !uploadingOrParsing;
    return {
      ...source,
      id: numberId(source.id ?? source.data_package_id),
      workspace_id: numberId(source.workspace_id),
      status,
      episodes,
      admission_counts: admissionCounts,
      uploadingOrParsing,
      canReview,
    };
  }

  function normalizeDraft(value, episodeIds = null) {
    const source = value && typeof value === 'object' ? value : EMPTY_DRAFT;
    const allowed = episodeIds instanceof Set
      ? episodeIds
      : (Array.isArray(episodeIds) ? new Set(episodeIds.map(keyId).filter(Boolean)) : null);
    const viewed = Array.isArray(source.viewed_episode_ids)
      ? source.viewed_episode_ids.map(numberId).filter((id) => id !== null)
      : [];
    const rejectedSource = source.rejected_episodes && typeof source.rejected_episodes === 'object'
      ? source.rejected_episodes
      : {};
    const rejected = {};
    Object.entries(rejectedSource).forEach(([rawId, reason]) => {
      const id = keyId(rawId);
      if (!id || (allowed && !allowed.has(id))) return;
      rejected[id] = textReason(reason);
    });
    const uniqueViewed = Array.from(new Set(viewed.filter((id) => !allowed || allowed.has(String(id))))).sort((a, b) => a - b);
    const last = numberId(source.last_episode_id);
    return {
      viewed_episode_ids: uniqueViewed,
      rejected_episodes: rejected,
      last_episode_id: last !== null && (!allowed || allowed.has(String(last))) ? last : null,
    };
  }

  function normalizeDraftResponse(value, episodeIds = null) {
    const source = value && typeof value === 'object' ? value : {};
    return {
      ...source,
      draft_version: asCount(source.draft_version, 0),
      source_fingerprint: typeof source.source_fingerprint === 'string' ? source.source_fingerprint : '',
      saved_source_fingerprint: typeof source.saved_source_fingerprint === 'string'
        ? source.saved_source_fingerprint
        : '',
      source_changed: Boolean(source.source_changed),
      editable: source.editable !== false,
      draft: normalizeDraft(source.draft, episodeIds),
    };
  }

  function draftFingerprint(value) {
    return typeof value === 'string' ? value : '';
  }

  function currentDraftSnapshot({ viewedEpisodeIds = [], rejectedEpisodes = {}, lastEpisodeId = null } = {}) {
    const viewed = Array.from(new Set((Array.isArray(viewedEpisodeIds) ? viewedEpisodeIds : [])
      .map(numberId)
      .filter((id) => id !== null))).sort((a, b) => a - b);
    const rejected = {};
    Object.keys(rejectedEpisodes && typeof rejectedEpisodes === 'object' ? rejectedEpisodes : {})
      .map(keyId)
      .filter(Boolean)
      .sort((a, b) => Number(a) - Number(b))
      .forEach((id) => { rejected[id] = textReason(rejectedEpisodes[id]); });
    const last = numberId(lastEpisodeId);
    return {
      viewed_episode_ids: viewed,
      rejected_episodes: rejected,
      last_episode_id: last,
    };
  }

  function buildDraftPayload({ workspaceId, baseVersion, sourceFingerprint, viewedEpisodeIds, rejectedEpisodes, lastEpisodeId }) {
    return {
      workspace_id: numberId(workspaceId),
      base_version: Math.max(0, asCount(baseVersion, 0)),
      source_fingerprint: draftFingerprint(sourceFingerprint),
      draft: currentDraftSnapshot({ viewedEpisodeIds, rejectedEpisodes, lastEpisodeId }),
    };
  }

  function buildReviewPayload({
    workspaceId,
    verdict,
    wholeReason = '',
    baseVersion,
    sourceFingerprint,
    rejectedEpisodes = {},
  }) {
    const episodeReasons = currentDraftSnapshot({ rejectedEpisodes }).rejected_episodes;
    return {
      workspace_id: numberId(workspaceId),
      verdict: verdict === 'rejected' ? 'rejected' : 'approved',
      rejected_episode_ids: Object.keys(episodeReasons).map(Number),
      reason: typeof wholeReason === 'string' ? wholeReason.trim() : '',
      base_version: Math.max(0, asCount(baseVersion, 0)),
      source_fingerprint: draftFingerprint(sourceFingerprint),
      episode_reasons: episodeReasons,
    };
  }

  function reviewCounts({ packageData, episodes = [], viewedEpisodeIds = [], rejectedEpisodes = {} } = {}) {
    const rows = Array.isArray(episodes) ? episodes : [];
    const viewed = new Set((Array.isArray(viewedEpisodeIds) ? viewedEpisodeIds : []).map(keyId).filter(Boolean));
    const rejected = new Set(Object.keys(rejectedEpisodes && typeof rejectedEpisodes === 'object' ? rejectedEpisodes : {}).map(keyId).filter(Boolean));
    const eligible = rows.filter((episode) => episode.eligible);
    const excluded = rows.filter((episode) => !episode.eligible);
    const viewedEligible = eligible.filter((episode) => viewed.has(keyId(episode.id))).length;
    return {
      total: rows.length,
      eligible: eligible.length,
      accepted: Math.max(0, eligible.length - rejected.size),
      rejected: rejected.size,
      excluded: excluded.length,
      viewed: rows.filter((episode) => viewed.has(keyId(episode.id))).length,
      viewedEligible,
      unviewed: Math.max(0, rows.length - viewed.size),
      unviewedEligible: Math.max(0, eligible.length - viewedEligible),
      ready: asCount(packageData?.admission_counts?.ready, eligible.length),
      running: asCount(packageData?.admission_counts?.running, rows.filter((episode) => !episode.eligible && String(episode.admission_status || '').toLowerCase() === 'running').length),
      failed: asCount(packageData?.admission_counts?.failed, excluded.length),
    };
  }

  function scopeKey(packageId, workspaceId) {
    return `${numberId(workspaceId) || ''}:${numberId(packageId) || ''}`;
  }

  function scopeMatches(value, { packageId, workspaceId, episodeId } = {}) {
    if (!value || typeof value !== 'object') return true;
    const expectedPackageId = numberId(packageId);
    const expectedWorkspaceId = numberId(workspaceId);
    const packageFields = ['id', 'package_id', 'data_package_id'];
    const workspaceFields = ['workspace_id', 'workspaceId'];
    const episodeFields = ['episode_id', 'episodeId'];
    for (const field of packageFields) {
      if (own(value, field) && value[field] !== null && numberId(value[field]) !== expectedPackageId) return false;
    }
    for (const field of workspaceFields) {
      if (own(value, field) && value[field] !== null && numberId(value[field]) !== expectedWorkspaceId) return false;
    }
    const expectedEpisodeId = numberId(episodeId);
    if (expectedEpisodeId !== null) {
      for (const field of episodeFields) {
        if (own(value, field) && value[field] !== null && numberId(value[field]) !== expectedEpisodeId) return false;
      }
    }
    return true;
  }

  function scopeMismatchError(kind, expected, actual) {
    const error = new Error(kind === 'media'
      ? 'The preview response belongs to a different Episode or package.'
      : 'The package response belongs to a different workspace or package.');
    error.code = kind === 'media' ? 'stale_media_response' : 'stale_scope_response';
    error.stale = true;
    error.expected = expected;
    error.actual = actual;
    return error;
  }

  function assertScope(value, expected, kind = 'scope') {
    if (!scopeMatches(value, expected)) throw scopeMismatchError(kind, expected, value);
  }

  function normalizePreviewStreams(payload) {
    const source = Array.isArray(payload)
      ? payload
      : (Array.isArray(payload?.streams) ? payload.streams : []);
    return source.map((stream, index) => {
      const value = typeof stream === 'string' ? { video_url: stream } : (stream || {});
      return {
        ...value,
        index,
        url: value.video_url || value.preview_url || value.stream_url || value.url || '',
        label: value.topic || value.name || value.camera || `Stream ${index + 1}`,
      };
    }).filter((stream) => typeof stream.url === 'string' && stream.url.trim());
  }

  /**
   * Scope aware package + draft loader.  A response from a previous package is
   * returned as stale and never treated as current data by the component.
   */
  function createLoader(api = typeof QuicDataAPI === 'undefined' ? null : QuicDataAPI) {
    let generation = 0;
    let disposed = false;
    let pending = null;

    function invalidate() {
      generation += 1;
      pending = null;
    }

    function load(packageId, workspaceId) {
      const key = scopeKey(packageId, workspaceId);
      if (pending && pending.key === key) return pending.promise;
      invalidate();
      const token = generation;
      if (!numberId(packageId) || !numberId(workspaceId)) return Promise.resolve({ stale: false, empty: true });
      const promise = Promise.all([
        api.getDataPackage(packageId, workspaceId),
        api.getIntakeReviewDraft(packageId, workspaceId),
      ]).then(([packagePayload, draftPayload]) => {
        if (disposed || token !== generation) return { stale: true };
        assertScope(packagePayload, { packageId, workspaceId }, 'scope');
        assertScope(draftPayload, { packageId, workspaceId }, 'scope');
        const packageData = normalizePackage(packagePayload);
        const episodeIds = new Set(packageData.episodes.map((episode) => keyId(episode.id)).filter(Boolean));
        return {
          stale: false,
          package: packageData,
          draft: normalizeDraftResponse(draftPayload, episodeIds),
        };
      }).finally(() => {
        if (pending?.promise === promise) pending = null;
      });
      pending = { key, promise };
      return promise;
    }

    function dispose() {
      disposed = true;
      invalidate();
    }

    return { load, invalidate, dispose };
  }

  function createPreviewLoader(api = typeof QuicDataAPI === 'undefined' ? null : QuicDataAPI) {
    let generation = 0;
    let disposed = false;

    function load(packageId, episodeId, workspaceId) {
      const token = ++generation;
      if (!numberId(packageId) || !numberId(episodeId) || !numberId(workspaceId)) {
        return Promise.resolve({ stale: false, streams: [] });
      }
      return Promise.resolve()
        .then(() => api.getDataPackageEpisodePreviewUrls(packageId, episodeId, workspaceId))
        .then((payload) => {
          if (disposed || token !== generation) return { stale: true };
          assertScope(payload, { packageId, workspaceId, episodeId }, 'media');
          if (own(payload, 'episode_id') && numberId(payload.episode_id) !== numberId(episodeId)) {
            throw scopeMismatchError('media', { packageId, episodeId, workspaceId }, payload);
          }
          return { stale: false, streams: normalizePreviewStreams(payload), payload };
        });
    }

    function invalidate() {
      generation += 1;
    }

    function dispose() {
      disposed = true;
      invalidate();
    }

    return { load, invalidate, dispose };
  }

  /**
   * Debounced, single flight save coordinator.  It captures each revision at
   * request start and drains edits made while that request is in flight.
   */
  function createAutosaveCoordinator({
    capture,
    save,
    onSaved = () => {},
    onFailed = () => {},
    onState = () => {},
    setTimer = (...args) => globalThis.setTimeout(...args),
    clearTimer = (...args) => globalThis.clearTimeout(...args),
    now = () => Date.now(),
    debounceMs = 850,
    maxWaitMs = 5000,
  } = {}) {
    if (typeof capture !== 'function' || typeof save !== 'function') {
      throw new TypeError('capture and save are required');
    }
    let localRevision = 0;
    let savedRevision = 0;
    let inFlight = null;
    let dirtySince = null;
    let debounceTimer = null;
    let maxWaitTimer = null;
    let paused = false;
    let disposed = false;
    let epoch = 0;
    let failed = false;

    function state() {
      return {
        dirty: localRevision > savedRevision,
        saving: Boolean(inFlight),
        failed,
        localRevision,
        savedRevision,
      };
    }

    function publish() { onState(state()); }

    function clearTimers() {
      if (debounceTimer !== null) clearTimer(debounceTimer);
      if (maxWaitTimer !== null) clearTimer(maxWaitTimer);
      debounceTimer = null;
      maxWaitTimer = null;
    }

    function schedule({ resetDebounce = false } = {}) {
      if (disposed || paused || inFlight || localRevision <= savedRevision) return;
      if (resetDebounce && debounceTimer !== null) {
        clearTimer(debounceTimer);
        debounceTimer = null;
      }
      if (debounceTimer === null) {
        debounceTimer = setTimer(() => {
          debounceTimer = null;
          void flush({ reason: 'auto' });
        }, Math.max(0, debounceMs));
      }
      if (maxWaitTimer === null) {
        const delay = Math.max(0, maxWaitMs - (now() - (dirtySince ?? now())));
        maxWaitTimer = setTimer(() => {
          maxWaitTimer = null;
          void flush({ reason: 'auto' });
        }, delay);
      }
    }

    function markDirty() {
      if (disposed) return localRevision;
      localRevision += 1;
      if (dirtySince === null) dirtySince = now();
      failed = false;
      schedule({ resetDebounce: true });
      publish();
      return localRevision;
    }

    async function saveOnce(reason) {
      clearTimers();
      const revision = localRevision;
      const epochAtStart = epoch;
      let snapshot;
      try {
        snapshot = capture(revision);
      } catch (error) {
        failed = true;
        onFailed(error, { revision, reason, snapshot: null });
        publish();
        return false;
      }
      if (!snapshot) return false;
      const request = Promise.resolve().then(() => save(snapshot, { revision, reason }));
      inFlight = request;
      publish();
      try {
        const result = await request;
        // A scope reset can happen while the old request is still resolving.
        // Do not let its version acknowledge edits in the new scope.
        if (epochAtStart !== epoch) return true;
        savedRevision = Math.max(savedRevision, revision);
        if (savedRevision >= localRevision) dirtySince = null;
        failed = false;
        onSaved(result, { revision, reason, snapshot });
        return true;
      } catch (error) {
        if (epochAtStart === epoch) {
          failed = true;
          onFailed(error, { revision, reason, snapshot });
        }
        return false;
      } finally {
        if (inFlight === request) inFlight = null;
        publish();
        if (epochAtStart === epoch && localRevision > savedRevision) schedule();
        if (epochAtStart !== epoch && localRevision > savedRevision) schedule();
      }
    }

    async function flush({ drain = false, reason = 'manual' } = {}) {
      if (disposed || (paused && reason === 'auto')) return false;
      if (inFlight) {
        let previousSucceeded = true;
        try { await inFlight; } catch { previousSucceeded = false; }
        if (!previousSucceeded) return false;
        return drain && localRevision > savedRevision
          ? flush({ drain, reason })
          : true;
      }
      if (localRevision <= savedRevision) return true;
      const saved = await saveOnce(reason);
      if (!saved) return false;
      if (drain && localRevision > savedRevision) return flush({ drain, reason });
      schedule();
      return true;
    }

    function pause() { clearTimers(); paused = true; publish(); }

    function resume() { if (!disposed) { paused = false; schedule(); publish(); } }

    function reset() {
      clearTimers();
      epoch += 1;
      localRevision = 0;
      savedRevision = 0;
      dirtySince = null;
      paused = false;
      failed = false;
      publish();
    }

    function dispose() {
      clearTimers();
      epoch += 1;
      disposed = true;
      paused = true;
      publish();
    }

    publish();
    return { markDirty, flush, pause, resume, reset, dispose, state };
  }

  function errorMessage(error, fallback = 'Request failed.') {
    if (error?.stale) return error.message;
    if (error?.status === 401) return '登录已失效，请重新登录。';
    if (error?.status === 403) return '没有权限继续操作。';
    if (error?.status === 404) return '数据包不存在或已无法访问。';
    if (error?.status === 409) return '数据包来源或草稿版本已变化，请重新加载后核对。';
    if (error?.status === 422) return error?.message || '提交内容未通过校验。';
    return error?.message || fallback;
  }

  function isConflict(error) {
    return error?.status === 409 || ['intake_sources_changed', 'intake_draft_version_conflict', 'intake_draft_decisions_changed', 'package_intake_finalized_or_unavailable'].includes(error?.code);
  }

  function install(app) {
    if (!app || typeof app.component !== 'function') throw new TypeError('Vue app is required');
    app.component('intake-review-workbench', {
      name: 'IntakeReviewWorkbench',
      props: {
        workspaceId: { type: [Number, String], default: null },
        packageId: { type: [Number, String], default: null },
        locale: { type: String, default: 'zh-CN' },
      },
      emits: ['back', 'completed'],
      setup(props, { emit, expose }) {
        const state = Vue.reactive({
          loading: false,
          mediaLoading: false,
          submitting: false,
          package: null,
          episodes: [],
          selectedEpisodeId: null,
          viewedEpisodeIds: [],
          rejectedEpisodes: {},
          draftVersion: 0,
          sourceFingerprint: '',
          savedSourceFingerprint: '',
          sourceChanged: false,
          editable: false,
          loadError: '',
          mediaError: '',
          saveError: '',
          finalError: '',
          saveState: 'saved',
          conflict: false,
          finalAction: '',
          finalReason: '',
          mediaStreams: [],
          selectedStreamIndex: 0,
          completed: false,
          scopeToken: 0,
          localRevision: 0,
          savedRevision: 0,
          saving: false,
        });
        const packageLoader = createLoader(QuicDataAPI);
        const previewLoader = createPreviewLoader(QuicDataAPI);
        const videoElement = Vue.ref(null);
        let scopeToken = 0;
        let disposed = false;

        const english = Vue.computed(() => String(props.locale || '').toLowerCase().startsWith('en'));
        const text = (zh, en) => (english.value ? en : zh);
        const packageData = Vue.computed(() => state.package);
        const selectedEpisode = Vue.computed(() => state.episodes.find((episode) => Number(episode.id) === Number(state.selectedEpisodeId)) || null);
        const selectedStream = Vue.computed(() => state.mediaStreams[state.selectedStreamIndex] || state.mediaStreams[0] || null);
        const counts = Vue.computed(() => reviewCounts({
          packageData: state.package,
          episodes: state.episodes,
          viewedEpisodeIds: state.viewedEpisodeIds,
          rejectedEpisodes: state.rejectedEpisodes,
        }));
        const rejectedReasonMissing = Vue.computed(() => Object.entries(state.rejectedEpisodes)
          .some(([, reason]) => !String(reason || '').trim()));
        const baseFinalAllowed = Vue.computed(() => Boolean(
          state.package
          && state.package.canReview
          && state.editable
          && !state.sourceChanged
          && !state.conflict
          && !state.submitting
          && !state.completed
          && !state.loading
          && !state.mediaLoading
        ));
        const canFinalize = Vue.computed(() => baseFinalAllowed.value && state.draftVersion >= 1 && !rejectedReasonMissing.value);
        const finalDisabledReason = Vue.computed(() => {
          if (!state.package) return text('等待数据包加载。', 'Waiting for the package to load.');
          if (state.package.uploadingOrParsing || counts.value.running > 0) return text('上传或解析仍在进行，完成后才能提交终审。', 'Upload or parsing is still running; final review is disabled.');
          if (!state.editable || state.package.status !== FINAL_PACKAGE_STATUS) return text('该数据包当前不可编辑。', 'This package is not editable in its current state.');
          if (state.sourceChanged) return text('来源清单已变化，请按当前来源重新检查。旧草稿仍保留。', 'The sources changed. Restart the review for the current sources. The previous draft is preserved.');
          if (state.conflict) return text('草稿版本冲突，请重新加载后继续。', 'The draft has a version conflict. Reload before continuing.');
          if (rejectedReasonMissing.value) return text('每条不合格 Episode 都需要填写原因。', 'Each rejected Episode needs a reason.');
          if (state.draftVersion < 1) return text('草稿尚未保存，请先保存后提交。', 'The draft must be saved before final review.');
          return '';
        });

        function clearVideo() {
          const video = videoElement.value;
          if (!video) return;
          try {
            video.pause();
            video.removeAttribute('src');
            video.load();
          } catch { /* media cleanup is best effort */ }
          videoElement.value = null;
        }

        function clearMedia() {
          previewLoader.invalidate();
          clearVideo();
          state.mediaStreams = [];
          state.selectedStreamIndex = 0;
          state.mediaLoading = false;
          state.mediaError = '';
        }

        function resetLocalDraft(draftResponse) {
          const draft = draftResponse.draft || EMPTY_DRAFT;
          state.viewedEpisodeIds = draft.viewed_episode_ids.slice();
          state.rejectedEpisodes = { ...draft.rejected_episodes };
          state.selectedEpisodeId = draft.last_episode_id
            || state.episodes[0]?.id
            || null;
        }

        function applyLoadedData(result, token) {
          if (disposed || token !== scopeToken || result?.stale) return false;
          state.package = result.package;
          state.completed = Boolean(result.package.intake_review);
          state.episodes = result.package.episodes;
          state.draftVersion = result.draft.draft_version;
          state.sourceFingerprint = result.draft.source_fingerprint;
          state.savedSourceFingerprint = result.draft.saved_source_fingerprint;
          state.sourceChanged = result.draft.source_changed;
          state.editable = result.draft.editable && result.package.canReview;
          resetLocalDraft(result.draft);
          state.scopeToken = token;
          state.conflict = false;
          state.saveError = '';
          state.finalError = '';
          state.finalAction = '';
          state.saveState = state.draftVersion > 0 ? 'saved' : 'unsaved';
          state.localRevision = 0;
          state.savedRevision = 0;
          return true;
        }

        function currentDraftPayload() {
          return buildDraftPayload({
            workspaceId: props.workspaceId,
            baseVersion: state.draftVersion,
            sourceFingerprint: state.sourceFingerprint,
            viewedEpisodeIds: state.viewedEpisodeIds,
            rejectedEpisodes: state.rejectedEpisodes,
            lastEpisodeId: state.selectedEpisodeId,
          });
        }

        const autosave = createAutosaveCoordinator({
          capture: () => ({ ...currentDraftPayload(), __scopeToken: scopeToken }),
          save: (payload) => {
            const { __scopeToken, ...body } = payload;
            if (__scopeToken !== scopeToken || disposed) return Promise.resolve(null);
            if (!QuicDataAPI || typeof QuicDataAPI.saveIntakeReviewDraft !== 'function') {
              throw new Error('Draft save API is unavailable.');
            }
            return QuicDataAPI.saveIntakeReviewDraft(props.packageId, body);
          },
          onState: (snapshot) => {
            state.saving = snapshot.saving;
            state.localRevision = snapshot.localRevision;
            state.savedRevision = snapshot.savedRevision;
            if (snapshot.saving) state.saveState = 'saving';
            else if (snapshot.dirty) state.saveState = snapshot.failed ? 'failed' : 'unsaved';
            else if (!state.conflict && !state.saveError) state.saveState = 'saved';
          },
          onSaved: (response, meta) => {
            if (meta?.snapshot?.__scopeToken !== scopeToken || disposed) return;
            const normalized = normalizeDraftResponse(response, new Set(state.episodes.map((episode) => keyId(episode.id))));
            state.draftVersion = normalized.draft_version;
            state.sourceFingerprint = normalized.source_fingerprint || state.sourceFingerprint;
            state.savedSourceFingerprint = normalized.saved_source_fingerprint || state.savedSourceFingerprint;
            state.sourceChanged = normalized.source_changed;
            state.editable = normalized.editable && Boolean(state.package?.canReview);
            state.saveError = '';
            if (state.sourceChanged) {
              state.conflict = true;
              state.saveState = 'conflict';
              autosave.pause();
            }
          },
          onFailed: (error, meta) => {
            if (meta?.snapshot?.__scopeToken !== scopeToken || disposed) return;
            state.saveError = errorMessage(error, text('草稿保存失败，请重试。', 'Draft save failed. Try again.'));
            state.saveState = isConflict(error) ? 'conflict' : 'failed';
            if (isConflict(error)) {
              state.conflict = true;
              autosave.pause();
            }
          },
        });

        function markDirty() {
          if (!state.editable || state.conflict || disposed) return;
          state.saveError = '';
          state.finalError = '';
          autosave.markDirty();
        }

        async function loadScope({ explicitReload = false } = {}) {
          const token = ++scopeToken;
          state.scopeToken = token;
          state.loading = true;
          state.loadError = '';
          state.finalError = '';
          state.mediaError = '';
          clearMedia();
          // Stop timers while the new scope is loading.  Keeping the current
          // coordinator state until a successful response is important: a
          // failed explicit reload must not make local edits look saved.
          autosave.pause();
          if (!numberId(props.packageId) || !numberId(props.workspaceId)) {
            state.package = null;
            state.episodes = [];
            state.loading = false;
            state.editable = false;
            autosave.reset();
            return;
          }
          try {
            const result = await packageLoader.load(props.packageId, props.workspaceId);
            if (!applyLoadedData(result, token)) return;
            autosave.reset();
            state.loading = false;
            if (state.selectedEpisodeId) await selectEpisode(state.selectedEpisodeId, { markViewed: false });
          } catch (error) {
            if (token !== scopeToken || disposed || error?.stale) return;
            state.loading = false;
            state.loadError = errorMessage(error, text('数据包加载失败，请重试。', 'Package loading failed. Try again.'));
            if (!explicitReload) {
              state.package = null;
              state.episodes = [];
              state.editable = false;
              autosave.reset();
            } else {
              // Keep the current draft and conflict marker visible.  The
              // reviewer can retry without losing edits made before reload.
              autosave.resume();
            }
          }
        }

        async function selectEpisode(episodeOrId, { markViewed = true } = {}) {
          const id = numberId(typeof episodeOrId === 'object' ? episodeOrId.id : episodeOrId);
          const episode = state.episodes.find((item) => Number(item.id) === Number(id));
          if (!episode || id === null) return;
          state.selectedEpisodeId = id;
          state.finalError = '';
          if (markViewed && state.editable && !state.viewedEpisodeIds.some((item) => Number(item) === id)) {
            state.viewedEpisodeIds = [...state.viewedEpisodeIds, id];
            markDirty();
          }
          clearMedia();
          if (episode.preview_available === false) {
            state.mediaError = episode.exclusionReason || text('该 Episode 没有可用预览。', 'No preview is available for this Episode.');
            return;
          }
          state.mediaLoading = true;
          const token = scopeToken;
          try {
            const result = await previewLoader.load(props.packageId, id, props.workspaceId);
            if (token !== scopeToken || result?.stale || Number(state.selectedEpisodeId) !== Number(id)) return;
            state.mediaStreams = result.streams || [];
            state.selectedStreamIndex = 0;
            if (!state.mediaStreams.length) state.mediaError = text('没有可播放的视频预览。', 'No playable video preview was returned.');
          } catch (error) {
            if (token !== scopeToken || error?.stale) return;
            state.mediaError = errorMessage(error, text('视频预览加载失败，请重试。', 'Video preview failed to load. Try again.'));
          } finally {
            if (token === scopeToken && Number(state.selectedEpisodeId) === Number(id)) state.mediaLoading = false;
          }
        }

        function updateReason(value) {
          const episode = selectedEpisode.value;
          if (!episode?.eligible || !Object.prototype.hasOwnProperty.call(state.rejectedEpisodes, keyId(episode.id))) return;
          state.rejectedEpisodes = { ...state.rejectedEpisodes, [keyId(episode.id)]: textReason(value) };
          markDirty();
        }

        function toggleRejected(episode) {
          if (!episode?.eligible || !state.editable || state.conflict) return;
          const key = keyId(episode.id);
          const next = { ...state.rejectedEpisodes };
          if (Object.prototype.hasOwnProperty.call(next, key)) delete next[key];
          else next[key] = '';
          state.rejectedEpisodes = next;
          if (!state.viewedEpisodeIds.some((item) => Number(item) === Number(episode.id))) {
            state.viewedEpisodeIds = [...state.viewedEpisodeIds, episode.id];
          }
          state.selectedEpisodeId = episode.id;
          markDirty();
        }

        function markViewed(episode) {
          if (!episode || !state.editable || state.conflict) return;
          if (state.viewedEpisodeIds.some((item) => Number(item) === Number(episode.id))) return;
          state.viewedEpisodeIds = [...state.viewedEpisodeIds, episode.id];
          markDirty();
        }

        function hasUnsaved() {
          const snapshot = autosave.state();
          return snapshot.dirty || snapshot.saving || state.saveState === 'failed' || state.conflict;
        }

        async function canLeave() {
          if (!hasUnsaved()) return true;
          if (state.conflict) {
            if (typeof window === 'undefined' || typeof window.confirm !== 'function') return false;
            return window.confirm(text(
              '草稿版本已冲突，离开将放弃当前未保存编辑。确定离开吗？',
              'The draft has a version conflict. Leaving will discard the unsaved edits. Leave anyway?',
            ));
          }
          const flushed = await autosave.flush({ drain: true, reason: 'leave' });
          return Boolean(flushed && !hasUnsaved());
        }

        async function requestBack() {
          if (!(await canLeave())) return;
          clearMedia();
          emit('back');
        }

        function beforeUnload(event) {
          if (!hasUnsaved()) return;
          event.preventDefault();
          event.returnValue = '';
        }

        function openFinal(verdict) {
          state.finalError = '';
          if (!baseFinalAllowed.value) {
            state.finalError = finalDisabledReason.value;
            return;
          }
          if (rejectedReasonMissing.value) {
            state.finalError = finalDisabledReason.value;
            return;
          }
          state.finalAction = verdict === 'rejected' ? 'rejected' : 'approved';
          if (verdict !== 'rejected') state.finalReason = '';
        }

        async function ensureDraftSaved() {
          if (state.conflict || state.sourceChanged) return false;
          if (state.draftVersion < 1 && autosave.state().localRevision <= autosave.state().savedRevision) {
            markDirty();
          }
          const success = await autosave.flush({ drain: true, reason: 'final' });
          if (!success || state.conflict || state.sourceChanged || state.draftVersion < 1) {
            if (!state.finalError) state.finalError = state.saveError || finalDisabledReason.value;
            return false;
          }
          return true;
        }

        async function confirmFinal() {
          const verdict = state.finalAction;
          if (!verdict || state.submitting) return;
          const finalToken = scopeToken;
          if (verdict === 'rejected' && !String(state.finalReason || '').trim()) {
            state.finalError = text('整包驳回必须填写原因。', 'A reason is required to reject the whole package.');
            return;
          }
          if (!baseFinalAllowed.value || rejectedReasonMissing.value) {
            state.finalError = finalDisabledReason.value;
            return;
          }
          state.submitting = true;
          state.finalError = '';
          try {
            if (!(await ensureDraftSaved())) return;
            if (finalToken !== scopeToken || disposed) return;
            const body = buildReviewPayload({
              workspaceId: props.workspaceId,
              verdict,
              wholeReason: state.finalReason,
              baseVersion: state.draftVersion,
              sourceFingerprint: state.sourceFingerprint,
              rejectedEpisodes: state.rejectedEpisodes,
            });
            if (body.base_version < 1) throw new Error(text('草稿尚未保存。', 'The draft has not been saved.'));
            const result = await QuicDataAPI.reviewIntakePackage(props.packageId, body);
            if (disposed || finalToken !== scopeToken) return;
            state.completed = true;
            state.editable = false;
            state.finalAction = '';
            await loadScope();
            emit('completed', result);
          } catch (error) {
            if (isConflict(error)) {
              state.conflict = true;
              state.saveState = 'conflict';
              autosave.pause();
            }
            state.finalError = errorMessage(error, text('终审提交失败，请重试。', 'Final review failed. Try again.'));
          } finally {
            state.submitting = false;
          }
        }

        async function saveDraft() {
          if (!state.editable || state.conflict || state.sourceChanged) return;
          if (state.draftVersion < 1) markDirty();
          await autosave.flush({ drain: true, reason: 'manual' });
        }

        async function restartSourceReview() {
          if (!state.sourceChanged || !state.editable) return;
          state.viewedEpisodeIds = [];
          state.rejectedEpisodes = {};
          state.sourceChanged = false;
          state.conflict = false;
          autosave.resume();
          markDirty();
          await saveDraft();
        }

        async function reload() {
          state.saveError = '';
          state.finalError = '';
          await loadScope({ explicitReload: true });
        }

        function episodeStatus(episode) {
          const key = keyId(episode?.id);
          if (!episode?.eligible) return 'excluded';
          if (Object.prototype.hasOwnProperty.call(state.rejectedEpisodes, key)) return 'rejected';
          if (state.viewedEpisodeIds.some((id) => keyId(id) === key)) return 'viewed';
          return 'pending';
        }

        function statusLabel(status) {
          return {
            pending: text('待查看', 'To review'),
            viewed: text('已查看', 'Viewed'),
            rejected: text('不合格', 'Rejected'),
            excluded: text('系统排除', 'System excluded'),
          }[status] || status;
        }

        function packageStatusLabel(status) {
          return {
            pending_upload: text('待上传', 'Awaiting upload'),
            uploading: text('上传中', 'Uploading'),
            parsing: text('解析中', 'Parsing'),
            ingested: text('已入库', 'Ingested'),
            pending_intake_review: text('待数采审核', 'Pending intake review'),
            intake_approved: text('审核通过', 'Intake approved'),
            voided: text('已作废', 'Voided'),
          }[status] || status || '—';
        }

        function saveLabel() {
          if (state.conflict) return text('版本冲突', 'Version conflict');
          if (state.saving) return text('保存中…', 'Saving…');
          if (state.saveState === 'failed') return text('保存失败', 'Save failed');
          if (state.saveState === 'unsaved') return text('有未保存更改', 'Unsaved changes');
          if (state.draftVersion < 1 && !state.completed) return text('尚未保存草稿', 'No saved draft');
          return text('已保存', 'Saved');
        }

        function confirmationSummary() {
          return text(
            `本次提交包含 ${counts.value.accepted} 条可通过、${counts.value.rejected} 条不合格、${counts.value.excluded} 条系统排除、${counts.value.unviewed} 条尚未查看。`,
            `This submits ${counts.value.accepted} eligible, ${counts.value.rejected} rejected, ${counts.value.excluded} system excluded, and ${counts.value.unviewed} not viewed.`,
          );
        }

        function attachListeners() {
          if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') window.addEventListener('beforeunload', beforeUnload);
        }

        Vue.watch(() => [props.workspaceId, props.packageId], () => { void loadScope(); }, { immediate: true });
        Vue.onMounted(attachListeners);
        Vue.onBeforeUnmount(() => {
          disposed = true;
          if (typeof window !== 'undefined' && typeof window.removeEventListener === 'function') window.removeEventListener('beforeunload', beforeUnload);
          clearMedia();
          packageLoader.dispose();
          previewLoader.dispose();
          autosave.dispose();
        });

        if (typeof expose === 'function') expose({ canLeave, hasUnsaved, reload });

        return {
          state,
          packageData,
          selectedEpisode,
          selectedStream,
          counts,
          rejectedReasonMissing,
          baseFinalAllowed,
          canFinalize,
          finalDisabledReason,
          text,
          loadScope,
          saveDraft,
          restartSourceReview,
          reload,
          selectEpisode,
          updateReason,
          toggleRejected,
          markViewed,
          requestBack,
          openFinal,
          confirmFinal,
          episodeStatus,
          statusLabel,
          packageStatusLabel,
          saveLabel,
          confirmationSummary,
          setVideoElement: (element) => { videoElement.value = element; },
          streamUrl: (stream) => stream?.url || '',
          isRejected: (episode) => Object.prototype.hasOwnProperty.call(state.rejectedEpisodes, keyId(episode?.id)),
          isViewed: (episode) => state.viewedEpisodeIds.some((id) => Number(id) === Number(episode?.id)),
          isEnglish: english,
          formatDuration: (value) => {
            const seconds = Number(value);
            if (!Number.isFinite(seconds) || seconds < 0) return '—';
            const rounded = Math.floor(seconds);
            const hours = Math.floor(rounded / 3600);
            const minutes = Math.floor((rounded % 3600) / 60);
            const rest = rounded % 60;
            return hours ? `${hours}:${String(minutes).padStart(2, '0')}:${String(rest).padStart(2, '0')}` : `${minutes}:${String(rest).padStart(2, '0')}`;
          },
        };
      },
      template: `
        <section class="intake-review-workbench" :class="{ 'is-readonly': !state.editable, 'is-conflict': state.conflict, 'is-finalized': state.completed }" :aria-busy="state.loading || state.submitting">
          <header class="intake-review-header">
            <button class="intake-review-back" type="button" @click="requestBack">‹ {{ text('返回审核队列', 'Back to review queue') }}</button>
            <div class="intake-review-title">
              <p class="intake-review-eyebrow">{{ text('上传后数采审核', 'Uploaded package intake review') }}</p>
              <h1>{{ packageData?.package_uid || text('数据包审核', 'Package review') }}</h1>
              <span v-if="packageData?.collection_task_name || packageData?.collection_project_name">{{ packageData.collection_project_name || '—' }} · {{ packageData.collection_task_name || '—' }}</span>
            </div>
            <div class="intake-review-header-meta">
              <span v-if="packageData" class="intake-review-status" :class="'status-' + (packageData.status || 'unknown')">{{ packageStatusLabel(packageData.status) }}</span>
              <span class="intake-review-save-state" :class="'save-' + state.saveState">{{ saveLabel() }}</span>
              <button v-if="state.editable" type="button" class="intake-review-button secondary" :disabled="state.saving || state.conflict || state.sourceChanged" @click="saveDraft">{{ text('保存草稿', 'Save draft') }}</button>
            </div>
          </header>

          <div v-if="state.loading" class="intake-review-state-panel"><span class="intake-review-spinner"></span><p>{{ text('正在加载数据包和审核草稿…', 'Loading package and review draft…') }}</p></div>
          <div v-else-if="state.loadError" class="intake-review-state-panel intake-review-error-panel">
            <p>{{ state.loadError }}</p><button type="button" class="intake-review-button secondary" @click="reload">{{ text('重新加载', 'Reload') }}</button>
          </div>
          <div v-else-if="packageData" class="intake-review-layout">
            <aside class="intake-review-episodes" aria-label="Episode navigator">
              <div class="intake-review-panel-heading"><div><h2>{{ text('Episode', 'Episodes') }}</h2><span>{{ state.episodes.length }} {{ text('条', 'total') }}</span></div><span class="intake-review-progress">{{ counts.viewed }}/{{ counts.total }}</span></div>
              <div class="intake-review-counts compact"><span class="count-viewed">{{ text('已查看', 'Viewed') }} {{ counts.viewed }}</span><span class="count-rejected">{{ text('不合格', 'Rejected') }} {{ counts.rejected }}</span><span class="count-excluded">{{ text('排除', 'Excluded') }} {{ counts.excluded }}</span></div>
              <div v-if="!state.episodes.length" class="intake-review-empty">{{ text('该数据包没有 Episode。', 'This package has no Episodes.') }}</div>
              <nav v-else class="intake-review-episode-list">
                <button v-for="episode in state.episodes" :key="episode.id || episode.index" type="button" class="intake-review-episode" :class="['episode-' + episodeStatus(episode), { selected: Number(state.selectedEpisodeId) === Number(episode.id) }]" @click="selectEpisode(episode)">
                  <span class="episode-index">{{ episode.index + 1 }}</span><span class="episode-main"><strong>{{ episode.label }}</strong><small v-if="episode.duration_s != null">{{ formatDuration(episode.duration_s) }}</small><small v-if="!episode.eligible" class="episode-reason">{{ episode.exclusionReason || text('系统准入检查未通过', 'Admission check did not pass') }}</small></span><span class="episode-status-dot" :title="statusLabel(episodeStatus(episode))"></span>
                </button>
              </nav>
            </aside>

            <main class="intake-review-media-column">
              <div class="intake-review-media-heading"><div><p class="intake-review-eyebrow">{{ text('当前 Episode', 'Current Episode') }}</p><h2>{{ selectedEpisode?.label || '—' }}</h2></div><div v-if="selectedEpisode" class="intake-review-media-meta"><span>{{ selectedEpisode.modality || '—' }}</span><span v-if="selectedEpisode.duration_s != null">{{ formatDuration(selectedEpisode.duration_s) }}</span></div></div>
              <div class="intake-review-video-frame" :class="{ 'is-loading': state.mediaLoading }">
                <div v-if="state.mediaLoading" class="intake-review-video-placeholder"><span class="intake-review-spinner"></span><p>{{ text('正在获取视频预览…', 'Loading video preview…') }}</p></div>
                <video v-else-if="selectedStream" :ref="setVideoElement" class="intake-review-video" :src="streamUrl(selectedStream)" controls playsinline preload="metadata" @error="state.mediaError = text('视频播放失败，请重试预览。', 'The video could not be played. Retry the preview.')"></video>
                <div v-else class="intake-review-video-placeholder"><span class="intake-review-video-icon">▶</span><p>{{ state.mediaError || text('选择 Episode 后将在此处播放实际视频。', 'Select an Episode to play its actual video here.') }}</p></div>
              </div>
              <div v-if="state.mediaStreams.length > 1" class="intake-review-stream-tabs" role="tablist">
                <button v-for="(stream, index) in state.mediaStreams" :key="stream.url + index" type="button" :class="{ active: state.selectedStreamIndex === index }" @click="state.selectedStreamIndex = index">{{ stream.label }}</button>
              </div>
              <div v-if="state.mediaError && !state.mediaLoading && selectedStream" class="intake-review-inline-error">{{ state.mediaError }}</div>
              <div v-if="selectedEpisode?.privacy_sensitive" class="intake-review-excluded-notice">{{ text('该 Episode 被标记为隐私敏感，请核对采集内容。', 'This Episode is marked privacy-sensitive. Check the recorded content.') }}</div>
            </main>

            <aside class="intake-review-decision-column">
              <section class="intake-review-card current-decision">
                <div class="intake-review-panel-heading"><div><h2>{{ text('检查结果', 'Review decision') }}</h2><span>{{ selectedEpisode?.label || '—' }}</span></div><span v-if="selectedEpisode" class="decision-badge" :class="'badge-' + episodeStatus(selectedEpisode)">{{ statusLabel(episodeStatus(selectedEpisode)) }}</span></div>
                <div v-if="selectedEpisode && !selectedEpisode.eligible" class="intake-review-excluded-notice"><strong>{{ text('系统排除', 'System excluded') }}</strong><p>{{ selectedEpisode.exclusionReason || text('该 Episode 未通过系统准入检查。', 'This Episode did not pass system admission checks.') }}</p><small>{{ text('系统排除项不能被标记为合格。', 'System exclusions cannot be marked eligible by a reviewer.') }}</small></div>
                <template v-else-if="selectedEpisode">
                  <label class="intake-review-toggle"><input type="checkbox" :checked="isRejected(selectedEpisode)" :disabled="!state.editable || state.conflict" @change="toggleRejected(selectedEpisode)" /><span><strong>{{ text('标记为不合格', 'Mark as rejected') }}</strong><small>{{ text('此标记会随审核草稿保存。', 'This decision is saved with the review draft.') }}</small></span></label>
                  <label v-if="isRejected(selectedEpisode)" class="intake-review-reason-field"><span>{{ text('不合格原因', 'Rejection reason') }} <em>*</em></span><textarea :value="state.rejectedEpisodes[String(selectedEpisode.id)] || ''" maxlength="1000" :disabled="!state.editable || state.conflict" :placeholder="text('可先留空暂存，提交终审前必须填写。', 'Can be empty while saving; required before final review.')" @input="updateReason($event.target.value)"></textarea><small>{{ (state.rejectedEpisodes[String(selectedEpisode.id)] || '').length }}/1000</small></label>
                </template>
                <button v-if="selectedEpisode && !isViewed(selectedEpisode)" type="button" class="intake-review-button subtle" :disabled="!state.editable || state.conflict" @click="markViewed(selectedEpisode)">{{ text('标记为已查看', 'Mark as viewed') }}</button>
              </section>

              <section class="intake-review-card intake-review-summary">
                <div class="intake-review-panel-heading"><div><h2>{{ text('整包汇总', 'Package summary') }}</h2><span>{{ text('最终结论作用于整个数据包', 'Final decision applies to the whole package') }}</span></div></div>
                <div class="intake-review-summary-grid"><div><strong>{{ counts.accepted }}</strong><span>{{ text('可通过', 'Eligible') }}</span></div><div class="danger"><strong>{{ counts.rejected }}</strong><span>{{ text('不合格', 'Rejected') }}</span></div><div class="muted"><strong>{{ counts.excluded }}</strong><span>{{ text('系统排除', 'System excluded') }}</span></div><div class="warning"><strong>{{ counts.unviewed }}</strong><span>{{ text('尚未查看', 'Not viewed') }}</span></div></div>
                <div class="intake-review-progress-bar"><span :style="{ width: counts.total ? ((counts.viewed / counts.total) * 100) + '%' : '0%' }"></span></div>
                <p class="intake-review-progress-label">{{ text('查看进度', 'Viewed progress') }} {{ counts.viewed }}/{{ counts.total }}</p>
              </section>

              <section class="intake-review-card final-review-card">
                <div class="intake-review-panel-heading"><div><h2>{{ text('提交整包结论', 'Submit package verdict') }}</h2><span>{{ text('请在确认区核对完整数量。', 'Check the complete counts in the confirmation step.') }}</span></div></div>
                <p v-if="finalDisabledReason" class="intake-review-disabled-reason">{{ finalDisabledReason }}</p>
                <div v-if="state.saveError" class="intake-review-inline-error">{{ state.saveError }}</div>
                <button v-if="state.sourceChanged && state.editable" type="button" class="intake-review-button secondary" @click="restartSourceReview">{{ text('按当前来源重新检查', 'Restart for current sources') }}</button>
                <div v-if="state.finalError" class="intake-review-inline-error">{{ state.finalError }}</div>
                <div v-if="state.conflict" class="intake-review-conflict"><strong>{{ text('草稿版本冲突', 'Draft version conflict') }}</strong><p>{{ text('当前编辑仍保留在页面中。重新加载会以服务端草稿为准。', 'Your edits remain on this page. Reloading will use the server draft.') }}</p><button type="button" class="intake-review-button secondary" @click="reload">{{ text('重新加载草稿', 'Reload draft') }}</button></div>
                <template v-if="state.finalAction">
                  <div class="intake-review-confirmation"><strong>{{ state.finalAction === 'rejected' ? text('确认整包驳回？', 'Reject the whole package?') : text('确认整包通过？', 'Approve the whole package?') }}</strong><p>{{ confirmationSummary() }}</p><label v-if="state.finalAction === 'rejected'" class="intake-review-reason-field"><span>{{ text('整包驳回原因', 'Whole-package rejection reason') }} <em>*</em></span><textarea v-model="state.finalReason" maxlength="1000" :placeholder="text('请输入整包驳回原因。', 'Enter a reason for rejecting the whole package.')"></textarea></label><div class="intake-review-confirm-actions"><button type="button" class="intake-review-button secondary" :disabled="state.submitting" @click="state.finalAction = ''">{{ text('返回修改', 'Back to edits') }}</button><button type="button" class="intake-review-button" :class="state.finalAction === 'rejected' ? 'danger' : 'primary'" :disabled="state.submitting || !baseFinalAllowed || rejectedReasonMissing" @click="confirmFinal">{{ state.submitting ? text('提交中…', 'Submitting…') : (state.finalAction === 'rejected' ? text('确认驳回', 'Confirm rejection') : text('确认通过', 'Confirm approval')) }}</button></div></div>
                </template>
                <div v-else class="intake-review-final-actions"><button type="button" class="intake-review-button primary" :disabled="!baseFinalAllowed || rejectedReasonMissing || state.completed" @click="openFinal('approved')">{{ text('通过数据包', 'Approve package') }}</button><button type="button" class="intake-review-button danger" :disabled="!baseFinalAllowed || rejectedReasonMissing || state.completed" @click="openFinal('rejected')">{{ text('驳回数据包', 'Reject package') }}</button></div>
              </section>
            </aside>
          </div>
        </section>`,
    });
    return app;
  }

  return {
    EMPTY_DRAFT,
    normalizePackage,
    normalizeEpisode,
    normalizeDraft,
    normalizeDraftResponse,
    currentDraftSnapshot,
    buildDraftPayload,
    buildReviewPayload,
    reviewCounts,
    scopeMatches,
    normalizePreviewStreams,
    createLoader,
    createPreviewLoader,
    createAutosaveCoordinator,
    errorMessage,
    isConflict,
    install,
  };
})();
