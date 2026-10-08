/**
 * Package-level annotation and submitted-result workbench.
 *
 * The host application owns routing and API installation.  This file exposes
 * pure draft/timeline helpers for tests and registers the component through
 * `QuicDataPackageAnnotationWorkbench.install(app)`.
 */
const QuicDataPackageAnnotationWorkbench = (() => {
  const DRAFT_SCHEMA = 'quicstudio.package-annotation-draft.v1';
  const EMPTY_DRAFT = Object.freeze({ schema: DRAFT_SCHEMA, episodes: {} });
  const EPISODE_PAGE_SIZE = 40;
  const SEGMENT_PAGE_SIZE = 20;
  function pageOf(items, page, size) {
    const pages = Math.max(1, Math.ceil(items.length / size));
    const current = Math.max(1, Math.min(pages, Math.floor(Number(page) || 1)));
    const offset = (current - 1) * size;
    return { page: current, pages, offset, items: items.slice(offset, offset + size) };
  }

  const NS_PATTERN = /^(?:0|[1-9][0-9]{0,19})$/;

  // api.js declares QuicDataAPI as a top-level const.  Prefer that lexical
  // binding, while retaining a small fallback for isolated test hosts and
  // pages that explicitly install the API on globalThis.
  function runtimeApi() {
    if (typeof QuicDataAPI !== 'undefined') return QuicDataAPI;
    return globalThis?.QuicDataAPI;
  }

  function own(value, key) {
    return Boolean(value && Object.prototype.hasOwnProperty.call(value, key));
  }

  function idNumber(value) {
    if (value === null || value === undefined || value === '') return null;
    const number = Number(value);
    return Number.isSafeInteger(number) && number > 0 ? number : null;
  }

  function idKey(value) {
    const id = idNumber(value);
    return id === null ? '' : String(id);
  }

  function nsBigInt(value) {
    const normalized = typeof value === 'bigint' ? value.toString() : String(value ?? '').trim();
    if (!NS_PATTERN.test(normalized)) return null;
    try { return BigInt(normalized); } catch { return null; }
  }

  function canonicalNs(value) {
    const parsed = nsBigInt(value);
    return parsed === null ? null : parsed.toString();
  }

  function clone(value) {
    if (value === null || value === undefined) return value;
    if (typeof structuredClone === 'function') {
      try { return structuredClone(value); } catch { /* use the small recursive clone below */ }
    }
    if (Array.isArray(value)) return value.map((item) => clone(item));
    if (typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, clone(item)]));
    return value;
  }

  function reasonText(value) {
    return typeof value === 'string' ? value.slice(0, 2000) : '';
  }

  function descriptionText(value) {
    return typeof value === 'string' ? value.slice(0, 4000) : '';
  }

  function uniqueSegmentId(used = new Set(), prefix = 'annotation') {
    const random = typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
      ? crypto.randomUUID()
      : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
    let candidate = `${prefix}-${random}`;
    let index = 1;
    while (used.has(candidate)) candidate = `${prefix}-${random}-${index++}`;
    return candidate;
  }

  function normalizeSegment(raw, used = new Set()) {
    const source = raw && typeof raw === 'object' ? raw : {};
    const start = canonicalNs(source.start_ns);
    const end = canonicalNs(source.end_ns);
    const rawId = typeof source.id === 'string' && source.id.trim() ? source.id.trim().slice(0, 128) : '';
    const id = rawId && !used.has(rawId) ? rawId : uniqueSegmentId(used);
    used.add(id);
    return {
      id,
      start_ns: start || '',
      end_ns: end || '',
      description: descriptionText(source.description),
    };
  }

  function normalizeEntry(raw) {
    const source = raw && typeof raw === 'object' ? raw : {};
    const conclusion = source.conclusion === 'no_valid_segments' ? 'no_valid_segments' : 'segments';
    const used = new Set();
    const segments = Array.isArray(source.segments)
      ? source.segments.map((segment) => normalizeSegment(segment, used))
      : [];
    if (conclusion === 'no_valid_segments') {
      return { conclusion, reason: reasonText(source.reason), segments: [] };
    }
    return { conclusion, segments };
  }

  function normalizeDraftJson(raw, memberIds = null) {
    const source = raw && typeof raw === 'object' ? raw : EMPTY_DRAFT;
    const allowed = memberIds instanceof Set
      ? memberIds
      : (Array.isArray(memberIds) ? new Set(memberIds.map(idKey).filter(Boolean)) : null);
    const entriesSource = source.episodes && typeof source.episodes === 'object' ? source.episodes : {};
    const episodes = {};
    Object.entries(entriesSource).forEach(([rawId, entry]) => {
      const key = idKey(rawId);
      if (!key || (allowed && !allowed.has(key))) return;
      episodes[key] = normalizeEntry(entry);
    });
    return {
      schema: DRAFT_SCHEMA,
      episodes,
      ...(own(source, 'source') ? { source: clone(source.source) } : {}),
      ...(own(source, 'review_required') ? { review_required: Boolean(source.review_required) } : {}),
    };
  }

  function entryFor(draft, episodeId) {
    const key = idKey(episodeId);
    return draft?.episodes?.[key] || { conclusion: 'segments', segments: [] };
  }

  function episodeBounds(timelineOrEpisode) {
    const source = timelineOrEpisode?.timeline || timelineOrEpisode?.source_binding || timelineOrEpisode || {};
    const start = nsBigInt(source.start_ns);
    const end = nsBigInt(source.end_ns);
    return start !== null && end !== null && start < end
      ? {
        start,
        end,
        start_ns: start.toString(),
        end_ns: end.toString(),
        duration_s: Number.isFinite(Number(source.duration_s))
          ? Number(source.duration_s)
          : Number(end - start) / 1_000_000_000,
      }
      : null;
  }

  function normalizePlaybackTimeline(raw) {
    const source = raw && typeof raw === 'object' ? raw : {};
    const rawEntries = Array.isArray(source.entries) ? source.entries : [];
    if (source.kind && source.kind !== 'qrdf_preview_timeline') return null;
    const entries = [];
    let previousTimestamp = null;
    let previousPts = null;
    for (const rawEntry of rawEntries) {
      if (rawEntry?.kind === 'dropped') continue;
      if (!rawEntry || rawEntry.kind !== 'frame') return null;
      const timestamp = nsBigInt(rawEntry.timestamp_ns);
      const pts = nsBigInt(rawEntry.video_pts_us);
      const frameIndex = Number(rawEntry.frame_index);
      if (
        timestamp === null || pts === null
        || !Number.isSafeInteger(frameIndex)
        || frameIndex !== entries.length
        || (previousTimestamp !== null && timestamp <= previousTimestamp)
        || (previousPts !== null && pts <= previousPts)
      ) return null;
      entries.push({
        kind: 'frame',
        frame_index: frameIndex,
        timestamp_ns: timestamp.toString(),
        timestamp,
        video_pts_us: pts.toString(),
        videoPtsUs: pts,
      });
      previousTimestamp = timestamp;
      previousPts = pts;
    }
    if (!entries.length) return null;
    return {
      kind: 'qrdf_preview_timeline',
      topic: typeof source.topic === 'string' ? source.topic : '',
      entries,
      firstPtsUs: entries[0].videoPtsUs,
      lastPtsUs: entries[entries.length - 1].videoPtsUs,
      firstTimestampNs: entries[0].timestamp,
      lastTimestampNs: entries[entries.length - 1].timestamp,
    };
  }

  function lowerBound(entries, target, field) {
    let low = 0;
    let high = entries.length;
    while (low < high) {
      const middle = Math.floor((low + high) / 2);
      if (entries[middle][field] < target) low = middle + 1;
      else high = middle;
    }
    return low;
  }

  function nearestEntry(entries, target, field) {
    if (!entries.length) return null;
    const upper = lowerBound(entries, target, field);
    if (upper <= 0) return entries[0];
    if (upper >= entries.length) return entries[entries.length - 1];
    const before = entries[upper - 1];
    const after = entries[upper];
    return target - before[field] <= after[field] - target ? before : after;
  }

  function normalizedTimeline(timeline) {
    if (
      timeline?.entries?.length
      && typeof timeline.firstPtsUs === 'bigint'
      && typeof timeline.lastPtsUs === 'bigint'
    ) return timeline;
    return normalizePlaybackTimeline(timeline);
  }

  /** Map relative preview playback seconds to an exact frame timestamp. */
  function sourceTimestampAtPlaybackSeconds(playbackSeconds, timeline) {
    const normalized = normalizedTimeline(timeline);
    const seconds = Number(playbackSeconds);
    if (!normalized?.entries?.length || !Number.isFinite(seconds) || seconds < 0) return null;
    const targetPts = normalized.firstPtsUs + BigInt(Math.max(0, Math.round(seconds * 1_000_000)));
    return nearestEntry(normalized.entries, targetPts, 'videoPtsUs')?.timestamp_ns || null;
  }

  /** Map an exact source timestamp to relative preview seconds for seeking. */
  function playbackSecondsAtSourceTimestamp(sourceTimestampNs, timeline) {
    const normalized = normalizedTimeline(timeline);
    const target = nsBigInt(sourceTimestampNs);
    if (!normalized?.entries?.length || target === null) return null;
    const entry = nearestEntry(normalized.entries, target, 'timestamp');
    if (!entry) return null;
    return Number(entry.videoPtsUs - normalized.firstPtsUs) / 1_000_000;
  }

  /** Pointer mapping uses preview PTS, then chooses an actual frame. It never interpolates source ns. */
  function sourceTimestampAtPlaybackRatio(ratio, timeline) {
    const normalized = normalizedTimeline(timeline);
    const value = Number(ratio);
    if (!normalized?.entries?.length || !Number.isFinite(value)) return null;
    const clamped = Math.max(0, Math.min(1, value));
    const ptsSpan = normalized.lastPtsUs - normalized.firstPtsUs;
    const targetPts = normalized.firstPtsUs + BigInt(Math.round(Number(ptsSpan) * clamped));
    return nearestEntry(normalized.entries, targetPts, 'videoPtsUs')?.timestamp_ns || null;
  }

  function percentageForNs(value, bounds) {
    const point = nsBigInt(value);
    const normalized = episodeBounds(bounds);
    if (point === null || !normalized || point < normalized.start || point > normalized.end) return 0;
    // This number is used only for CSS layout; draft persistence always uses
    // the canonical BigInt string returned by the mapping helpers.
    return Math.max(0, Math.min(100, Number(point - normalized.start) / Number(normalized.end - normalized.start) * 100));
  }

  function validateSegment(segment, bounds = null) {
    const start = nsBigInt(segment?.start_ns);
    const end = nsBigInt(segment?.end_ns);
    if (start === null || end === null || start >= end) return { valid: false, code: 'range' };
    const normalizedBounds = episodeBounds(bounds);
    if (normalizedBounds && (start < normalizedBounds.start || end > normalizedBounds.end)) {
      return { valid: false, code: 'bounds' };
    }
    return { valid: true, start, end };
  }

  function validateEntry(entry, bounds = null, { complete = false } = {}) {
    const source = entry && typeof entry === 'object' ? entry : {};
    const conclusion = source.conclusion;
    if (!['segments', 'no_valid_segments'].includes(conclusion)) return { valid: false, code: 'conclusion' };
    if (conclusion === 'no_valid_segments') {
      return {
        valid: !complete || Boolean(String(source.reason || '').trim()),
        code: !complete || String(source.reason || '').trim() ? '' : 'reason',
      };
    }
    const segments = Array.isArray(source.segments) ? source.segments : [];
    if (complete && !segments.length) return { valid: false, code: 'segments' };
    const normalized = [];
    const ids = new Set();
    for (const segment of segments) {
      if (typeof segment?.id !== 'string' || !segment.id || ids.has(segment.id)) return { valid: false, code: 'segment_id' };
      ids.add(segment.id);
      const result = validateSegment(segment, bounds);
      if (!result.valid) return result;
      if (complete && !String(segment.description || '').trim()) return { valid: false, code: 'description' };
      normalized.push({ ...result, id: segment.id });
    }
    normalized.sort((left, right) => (left.start < right.start ? -1 : left.start > right.start ? 1 : 0));
    for (let index = 1; index < normalized.length; index += 1) {
      if (normalized[index].start < normalized[index - 1].end) return { valid: false, code: 'overlap' };
    }
    return { valid: true, code: '' };
  }

  function validateDraftForSubmit(draft, memberIds, boundsByEpisode = {}) {
    const expected = Array.from(memberIds instanceof Set ? memberIds : new Set(memberIds || []), idKey).filter(Boolean);
    const entries = draft?.episodes && typeof draft.episodes === 'object' ? draft.episodes : {};
    const missing = expected.filter((key) => !entries[key]);
    const issues = [];
    for (const key of expected) {
      if (!entries[key]) continue;
      const result = validateEntry(entries[key], boundsByEpisode[key], { complete: true });
      if (!result.valid) issues.push({ episode_id: Number(key), code: result.code });
    }
    return { valid: missing.length === 0 && issues.length === 0, missing, issues };
  }

  function normalizePackageSummary(payload, mode = 'annotation') {
    const source = payload?.data && typeof payload.data === 'object' && !Array.isArray(payload.data)
      ? payload.data
      : (payload && typeof payload === 'object' ? payload : {});
    const rawEpisodes = Array.isArray(source.episodes) ? source.episodes : [];
    const episodes = rawEpisodes.map((episode, index) => ({
      ...(episode || {}),
      id: idNumber(episode?.id ?? episode?.episode_id),
      index,
      label: episode?.episode_uid || episode?.uid || `Episode ${index + 1}`,
      processed: Boolean(episode?.processed || episode?.conclusion),
    })).filter((episode) => episode.id !== null);
    const item = source.item && typeof source.item === 'object' ? source.item : {};
    const capabilities = source.capabilities && typeof source.capabilities === 'object' ? source.capabilities : {};
    return {
      ...source,
      mode: mode === 'review' ? 'review' : 'annotation',
      item: { ...item, id: idNumber(item.id), workspace_id: idNumber(item.workspace_id ?? source.workspace_id) },
      package: source.package && typeof source.package === 'object' ? source.package : {},
      episodes,
      draft_json: normalizeDraftJson(source.draft_json, new Set(episodes.map((episode) => idKey(episode.id)))),
      draft_version: Number.isSafeInteger(Number(source.draft_version)) && Number(source.draft_version) >= 0 ? Number(source.draft_version) : 0,
      generation: Number.isSafeInteger(Number(source.generation)) && Number(source.generation) >= 1 ? Number(source.generation) : 1,
      submission_id: idNumber(source.submission_id ?? source.submission?.id),
      capabilities: {
        edit: capabilities.edit === true,
        save: capabilities.save === true,
        submit: capabilities.submit === true,
        approve: capabilities.approve === true,
        return: capabilities.return === true,
        read_only: capabilities.read_only === true || mode === 'review',
        mapping_available: capabilities.mapping_available !== false,
        disabled_reason: capabilities.disabled_reason || '',
      },
    };
  }

  function normalizeEpisodeResponse(payload, expected = {}) {
    const source = payload?.data && typeof payload.data === 'object' && !Array.isArray(payload.data)
      ? payload.data
      : (payload && typeof payload === 'object' ? payload : {});
    const episode = source.episode && typeof source.episode === 'object' ? source.episode : {};
    const timeline = episodeBounds(source.timeline || source.source_binding);
    const preview = source.media?.preview && typeof source.media.preview === 'object' ? source.media.preview : {};
    const playbackTimeline = normalizePlaybackTimeline(preview.playback_timeline);
    return {
      ...source,
      item: source.item || {},
      episode: { ...episode, id: idNumber(episode.id ?? episode.episode_id) },
      timeline,
      source_binding: source.source_binding || {},
      entry: source.entry ? normalizeEntry(source.entry) : null,
      media: {
        ...(source.media || {}),
        preview: {
          ...preview,
          available: preview.available === true,
          url: typeof preview.url === 'string' ? preview.url : '',
          mapping_available: preview.mapping_available === true && Boolean(playbackTimeline),
          playback_timeline: playbackTimeline,
          disabled_reason: preview.disabled_reason || '',
        },
      },
      draft_version: Number(source.draft_version) || 0,
      generation: Number(source.generation) || 1,
      expected,
    };
  }

  function buildDraftEnvelope(draft) {
    const normalized = normalizeDraftJson(draft);
    const entries = {};
    Object.entries(normalized.episodes).forEach(([key, entry]) => {
      if (entry.conclusion === 'no_valid_segments') {
        entries[key] = { conclusion: entry.conclusion, reason: entry.reason, segments: [] };
      } else {
        entries[key] = {
          conclusion: 'segments',
          segments: entry.segments.map((segment) => ({
            id: segment.id,
            start_ns: canonicalNs(segment.start_ns) || '',
            end_ns: canonicalNs(segment.end_ns) || '',
            description: segment.description,
          })),
        };
      }
    });
    return { schema: DRAFT_SCHEMA, episodes: entries };
  }

  function buildSavePayload({ workspaceId, baseVersion, expectedGeneration, draft }) {
    return {
      workspace_id: idNumber(workspaceId),
      base_version: Math.max(0, Number(baseVersion) || 0),
      expected_generation: Math.max(1, Number(expectedGeneration) || 1),
      draft_json: buildDraftEnvelope(draft),
    };
  }

  function buildSubmitPayload({ workspaceId, baseVersion, expectedGeneration }) {
    return {
      workspace_id: idNumber(workspaceId),
      base_version: Math.max(0, Number(baseVersion) || 0),
      expected_generation: Math.max(1, Number(expectedGeneration) || 1),
    };
  }

  function buildReviewDecisionPayload({ workspaceId, submissionId, generation, reason }) {
    return {
      workspace_id: idNumber(workspaceId),
      expected_submission_id: idNumber(submissionId),
      expected_generation: Math.max(1, Number(generation) || 1),
      ...(reason !== undefined ? { reason: String(reason).trim() } : {}),
    };
  }

  function scopeMatches(value, expected = {}) {
    if (!value || typeof value !== 'object') return true;
    const packageId = idNumber(expected.itemId ?? expected.workItemId);
    const workspaceId = idNumber(expected.workspaceId);
    const episodeId = idNumber(expected.episodeId);
    const item = value.item && typeof value.item === 'object' ? value.item : value;
    if (packageId !== null && own(item, 'id') && item.id !== null && idNumber(item.id) !== packageId) return false;
    if (workspaceId !== null && own(item, 'workspace_id') && item.workspace_id !== null && idNumber(item.workspace_id) !== workspaceId) return false;
    const episode = value.episode && typeof value.episode === 'object' ? value.episode : null;
    if (episodeId !== null && episode && own(episode, 'id') && episode.id !== null && idNumber(episode.id) !== episodeId) return false;
    return true;
  }

  function staleError(kind, expected, actual) {
    const error = new Error(kind === 'episode' ? 'The Episode response is stale.' : 'The workbench response is stale.');
    error.code = kind === 'episode' ? 'stale_episode_response' : 'stale_workbench_response';
    error.stale = true;
    error.expected = expected;
    error.actual = actual;
    return error;
  }

  function assertScope(value, expected, kind = 'workbench') {
    if (!scopeMatches(value, expected)) throw staleError(kind, expected, value);
  }

  function createWorkbenchLoader(api = null) {
    let generation = 0;
    let pending = null;
    let disposed = false;
    function load(workItemId, workspaceId, mode = 'annotation') {
      const key = `${idNumber(workspaceId) || ''}:${idNumber(workItemId) || ''}:${mode === 'review' ? 'review' : 'annotation'}`;
      if (pending?.key === key) return pending.promise;
      generation += 1;
      const token = generation;
      if (!idNumber(workItemId) || !idNumber(workspaceId)) return Promise.resolve({ stale: false, empty: true });
      const promise = Promise.resolve()
        .then(() => {
          const activeApi = api || runtimeApi();
          if (!activeApi || typeof activeApi.getPackageAnnotationWorkbench !== 'function') throw new Error('Annotation workbench API is unavailable.');
          return activeApi.getPackageAnnotationWorkbench(workItemId, workspaceId, mode);
        })
        .then((payload) => {
          if (disposed || token !== generation) return { stale: true };
          assertScope(payload, { itemId: workItemId, workspaceId }, 'workbench');
          return { stale: false, summary: normalizePackageSummary(payload, mode) };
        })
        .finally(() => { if (pending?.promise === promise) pending = null; });
      pending = { key, promise };
      return promise;
    }
    function invalidate() { generation += 1; pending = null; }
    function dispose() { disposed = true; invalidate(); }
    return { load, invalidate, dispose };
  }

  function createEpisodeLoader(api = null) {
    let generation = 0;
    let disposed = false;
    function load(workItemId, episodeId, workspaceId, mode = 'annotation') {
      const token = ++generation;
      if (!idNumber(workItemId) || !idNumber(episodeId) || !idNumber(workspaceId)) return Promise.resolve({ stale: false, empty: true });
      return Promise.resolve()
        .then(() => {
          const activeApi = api || runtimeApi();
          if (!activeApi || typeof activeApi.getPackageAnnotationEpisode !== 'function') throw new Error('Annotation Episode API is unavailable.');
          return activeApi.getPackageAnnotationEpisode(workItemId, episodeId, workspaceId, mode);
        })
        .then((payload) => {
          if (disposed || token !== generation) return { stale: true };
          assertScope(payload, { itemId: workItemId, episodeId, workspaceId }, 'episode');
          const response = normalizeEpisodeResponse(payload, { workItemId, episodeId, workspaceId, mode });
          if (response.episode.id !== null && response.episode.id !== idNumber(episodeId)) throw staleError('episode', { workItemId, episodeId, workspaceId }, payload);
          return { stale: false, episode: response };
        });
    }
    function invalidate() { generation += 1; }
    function dispose() { disposed = true; invalidate(); }
    return { load, invalidate, dispose };
  }

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
    if (typeof capture !== 'function' || typeof save !== 'function') throw new TypeError('capture and save are required');
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
      return { dirty: localRevision > savedRevision, saving: Boolean(inFlight), failed, localRevision, savedRevision };
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
      if (resetDebounce && debounceTimer !== null) { clearTimer(debounceTimer); debounceTimer = null; }
      if (debounceTimer === null) {
        debounceTimer = setTimer(() => { debounceTimer = null; void flush({ reason: 'auto' }); }, Math.max(0, debounceMs));
      }
      if (maxWaitTimer === null) {
        const delay = Math.max(0, maxWaitMs - (now() - (dirtySince ?? now())));
        maxWaitTimer = setTimer(() => { maxWaitTimer = null; void flush({ reason: 'auto' }); }, delay);
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
      try { snapshot = capture(revision); } catch (error) {
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
        if (epochAtStart !== epoch) return true;
        savedRevision = Math.max(savedRevision, revision);
        if (savedRevision >= localRevision) dirtySince = null;
        failed = false;
        onSaved(result, { revision, reason, snapshot });
        return true;
      } catch (error) {
        if (epochAtStart === epoch) { failed = true; onFailed(error, { revision, reason, snapshot }); }
        return false;
      } finally {
        if (inFlight === request) inFlight = null;
        publish();
        if (localRevision > savedRevision) schedule();
      }
    }
    async function flush({ drain = false, reason = 'manual' } = {}) {
      if (disposed || (paused && reason === 'auto')) return false;
      if (inFlight) {
        let previousSucceeded = true;
        try { await inFlight; } catch { previousSucceeded = false; }
        if (!previousSucceeded) return false;
        return drain && localRevision > savedRevision ? flush({ drain, reason }) : true;
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
    function reset() { clearTimers(); epoch += 1; localRevision = 0; savedRevision = 0; dirtySince = null; paused = false; failed = false; publish(); }
    function dispose() { clearTimers(); epoch += 1; disposed = true; paused = true; publish(); }
    publish();
    return { markDirty, flush, pause, resume, reset, dispose, state };
  }

  function historicalResultMissing(error) {
    return [error?.code, error?.detail?.code, error?.detail, error?.message]
      .includes('annotation_historical_result_uncertain');
  }

  function errorMessage(error, fallback = 'Request failed.') {
    if (error?.stale) return error.message;
    if (historicalResultMissing(error)) return '历史标注结果不完整，缺少可核对的固定提交记录。重新加载无法恢复，请返回工作项并联系管理员核查历史数据。';
    if (error?.status === 401) return '登录已失效，请重新登录。';
    if (error?.status === 403) return '没有权限继续操作。';
    if (error?.status === 404) return '工作项或 Episode 不存在。';
    if (error?.status === 409) return '版本、分配或提交来源已变化，请重新加载后核对。';
    if (error?.status === 422) return error?.message || '提交内容未通过校验。';
    return error?.message || fallback;
  }

  function isConflict(error) {
    return error?.status === 409 || [
      'annotation_draft_version_conflict',
      'annotation_generation_conflict',
      'annotation_submission_conflict',
      'annotation_frozen_source_changed',
      'annotation_frozen_source_unavailable',
      'annotation_historical_result_uncertain',
    ].includes(error?.code);
  }

  function install(app) {
    if (!app || typeof app.component !== 'function') throw new TypeError('Vue app is required');
    app.component('package-annotation-workbench', {
      name: 'PackageAnnotationWorkbench',
      props: {
        workspaceId: { type: [Number, String], default: null },
        workItemId: { type: [Number, String], default: null },
        mode: { type: String, default: 'annotation' },
        locale: { type: String, default: 'zh-CN' },
      },
      emits: ['back', 'completed'],
      setup(props, { emit, expose }) {
        const state = Vue.reactive({
          loading: false,
          episodeLoading: false,
          mediaError: '',
          loadError: '',
          historicalResultMissing: false,
          episodeError: '',
          saveError: '',
          finalError: '',
          packageSummary: null,
          episodes: [],
          episodePage: 1,
          segmentPage: 1,
          draftJson: clone(EMPTY_DRAFT),
          draftVersion: 0,
          generation: 1,
          capabilities: {},
          submissionId: null,
          submission: null,
          selectedEpisodeId: null,
          episodeData: null,
          selectedSegmentId: null,
          pendingTimestampNs: '',
          pendingStartNs: '',
          boundaryMode: 'idle',
          playbackSeconds: 0,
          playbackTimestampNs: '',
          saveState: 'saved',
          saving: false,
          localRevision: 0,
          savedRevision: 0,
          conflict: false,
          finalAction: '',
          returnReason: '',
          submitting: false,
          completed: false,
          scopeToken: 0,
        });
        const packageLoader = createWorkbenchLoader();
        const episodeLoader = createEpisodeLoader();
        const videoElement = Vue.ref(null);
        let disposed = false;
        let scopeToken = 0;
        let paintRaf = null;

        const english = Vue.computed(() => String(props.locale || '').toLowerCase().startsWith('en'));
        const text = (zh, en) => (english.value ? en : zh);
        const isReview = Vue.computed(() => props.mode === 'review');
        const packageData = Vue.computed(() => state.packageSummary?.package || {});
        const workItem = Vue.computed(() => state.packageSummary?.item || {});
        const workItemStatus = Vue.computed(() => String(workItem.value.status || '').toLowerCase());
        const selectedEpisodeSummary = Vue.computed(() => state.episodes.find((episode) => Number(episode.id) === Number(state.selectedEpisodeId)) || null);
        const currentEntry = Vue.computed(() => entryFor(state.draftJson, state.selectedEpisodeId));
        const currentTimeline = Vue.computed(() => state.episodeData?.media?.preview?.playback_timeline || null);
        const currentPreview = Vue.computed(() => state.episodeData?.media?.preview || null);
        const currentBounds = Vue.computed(() => state.episodeData?.timeline || null);
        const currentSegments = Vue.computed(() => currentEntry.value.conclusion === 'segments' ? currentEntry.value.segments : []);
        const episodePage = Vue.computed(() => pageOf(state.episodes, state.episodePage, EPISODE_PAGE_SIZE));
        const segmentPage = Vue.computed(() => pageOf(currentSegments.value, state.segmentPage, SEGMENT_PAGE_SIZE));
        const canSetOutPoint = Vue.computed(() => Boolean(canEditBounds.value && state.pendingTimestampNs && (state.selectedSegmentId || state.pendingStartNs)));
        const canEditCurrent = Vue.computed(() => Boolean(
          !isReview.value && !state.completed && !state.loading
          && state.capabilities.edit
          && state.episodeData
          && !state.conflict
        ));
        // The package draft endpoint remains writable when exact media mapping
        // is unavailable. That still permits a no-valid conclusion, while
        // boundary changes require the Episode-level mapping capability.
        const canEditBounds = Vue.computed(() => Boolean(
          canEditCurrent.value
          && state.episodeData?.capabilities?.edit
          && currentPreview.value?.mapping_available
        ));
        const draftComplete = Vue.computed(() => validateDraftForSubmit(state.draftJson, state.episodes.map((episode) => episode.id), {}));
        const annotationFinalized = Vue.computed(() => !isReview.value && ['submitted', 'done', 'accepted'].includes(workItemStatus.value));
        const reviewFinalized = Vue.computed(() => isReview.value && ['approved', 'returned', 'accepted', 'rejected'].includes(workItemStatus.value));
        const packageProgress = Vue.computed(() => {
          const entries = state.draftJson?.episodes || {};
          let processed = 0, noValid = 0, segmentCount = 0;
          for (const episode of state.episodes) {
            const entry = entries[idKey(episode.id)];
            segmentCount += entry?.segments?.length || 0;
            if (!validateEntry(entry, episodeBounds(episode), { complete: true }).valid) continue;
            processed += 1;
            if (entry.conclusion === 'no_valid_segments') noValid += 1;
          }
          const total = state.episodes.length;
          return { total, processed, pending: total - processed, noValid, segmentCount };
        });
        const currentValidity = Vue.computed(() => validateEntry(currentEntry.value, currentBounds.value, { complete: false }));
        const finalDisabledReason = Vue.computed(() => {
          if (isReview.value) {
            if (reviewFinalized.value) return ['approved', 'accepted'].includes(workItemStatus.value)
              ? text('此提交已通过。', 'This submission has already been approved.')
              : text('此提交已退回。', 'This submission has already been returned.');
            return state.capabilities.approve || state.capabilities.return
              ? ''
              : text('当前结果不可操作。', 'This result is not actionable.');
          }
          if (annotationFinalized.value) return workItemStatus.value === 'done' || workItemStatus.value === 'accepted'
            ? text('标注结果已完成。', 'This annotation result is complete.')
            : text('标注已提交，等待结果确认。', 'The annotation is submitted and awaiting result confirmation.');
          if (!state.capabilities.submit || !state.capabilities.edit) return text('当前工作项不可编辑。', 'This work item is not editable.');
          if (state.conflict) return text('草稿版本冲突，请重新加载。', 'The draft has a version conflict. Reload before continuing.');
          if (!draftComplete.value.valid) return text(`还有 ${draftComplete.value.missing.length + draftComplete.value.issues.length} 条 Episode 未完成。`, `${draftComplete.value.missing.length + draftComplete.value.issues.length} Episode entries are incomplete.`);
          return '';
        });
        const canSubmit = Vue.computed(() => !isReview.value && Boolean(state.capabilities.submit && !annotationFinalized.value && !state.conflict && !state.submitting && !state.completed && !state.loading));
        const canReviewApprove = Vue.computed(() => isReview.value && Boolean(state.capabilities.approve && !reviewFinalized.value && !state.submitting && !state.completed && !state.loading));
        const canReviewReturn = Vue.computed(() => isReview.value && Boolean(state.capabilities.return && !reviewFinalized.value && !state.submitting && !state.completed && !state.loading));

        function currentVideo() { return videoElement.value; }

        function clearVideo() {
          const video = currentVideo();
          if (video) {
            try { video.pause(); video.removeAttribute('src'); video.load(); } catch { /* media cleanup is best effort */ }
          }
          videoElement.value = null;
          if (paintRaf !== null && typeof cancelAnimationFrame === 'function') cancelAnimationFrame(paintRaf);
          paintRaf = null;
        }

        function clearEpisodeMedia() {
          episodeLoader.invalidate();
          clearVideo();
          state.episodeData = null;
          state.mediaError = '';
          state.episodeError = '';
          state.playbackSeconds = 0;
          state.playbackTimestampNs = '';
          state.pendingTimestampNs = '';
          state.pendingStartNs = '';
          state.boundaryMode = 'idle';
          state.selectedSegmentId = null;
          state.segmentPage = 1;
        }

        function applySummary(summary, token) {
          if (disposed || token !== scopeToken || !summary) return false;
          state.completed = false;
          state.packageSummary = summary;
          state.episodes = summary.episodes;
          state.draftJson = normalizeDraftJson(summary.draft_json, new Set(summary.episodes.map((episode) => idKey(episode.id))));
          state.draftVersion = summary.draft_version;
          state.generation = summary.generation;
          state.capabilities = summary.capabilities;
          state.submissionId = summary.submission_id;
          state.submission = summary.submission || null;
          state.selectedEpisodeId = state.episodes[0]?.id || null;
          state.conflict = false;
          state.saveError = '';
          state.finalError = '';
          state.finalAction = '';
          state.saveState = state.draftVersion > 0 ? 'saved' : 'unsaved';
          return true;
        }

        function currentDraftForSave() {
          return buildSavePayload({
            workspaceId: props.workspaceId,
            baseVersion: state.draftVersion,
            expectedGeneration: state.generation,
            draft: state.draftJson,
          });
        }

        const autosave = createAutosaveCoordinator({
          capture: () => ({ ...currentDraftForSave(), __scopeToken: scopeToken }),
          save: (payload) => {
            const { __scopeToken, ...body } = payload;
            if (__scopeToken !== scopeToken || disposed) return Promise.resolve(null);
            const api = runtimeApi();
            if (!api || typeof api.saveAnnotationWorkItem !== 'function') throw new Error('Annotation draft API is unavailable.');
            return api.saveAnnotationWorkItem(props.workItemId, body);
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
            const payload = response?.item && typeof response.item === 'object' ? response.item : response || {};
            if (Number.isFinite(Number(payload.draft_version))) state.draftVersion = Number(payload.draft_version);
            if (Number.isFinite(Number(payload.generation)) && Number(payload.generation) > 0) state.generation = Number(payload.generation);
            state.saveError = '';
          },
          onFailed: (error, meta) => {
            if (meta?.snapshot?.__scopeToken !== scopeToken || disposed) return;
            state.saveError = errorMessage(error, text('草稿保存失败，请重试。', 'Draft save failed. Try again.'));
            state.saveState = isConflict(error) ? 'conflict' : 'failed';
            if (isConflict(error)) { state.conflict = true; autosave.pause(); }
          },
        });

        function markDirty() {
          if (!canEditCurrent.value || state.conflict || disposed) return;
          state.saveError = '';
          state.finalError = '';
          autosave.markDirty();
        }

        async function loadScope({ explicitReload = false } = {}) {
          const token = ++scopeToken;
          state.scopeToken = token;
          state.loading = true;
          state.loadError = '';
          state.historicalResultMissing = false;
          state.finalError = '';
          clearEpisodeMedia();
          autosave.pause();
          if (!idNumber(props.workItemId) || !idNumber(props.workspaceId)) {
            state.packageSummary = null;
            state.episodes = [];
            state.loading = false;
            autosave.reset();
            return;
          }
          try {
            const result = await packageLoader.load(props.workItemId, props.workspaceId, props.mode);
            if (disposed || token !== scopeToken || result?.stale) return;
            if (!applySummary(result.summary, token)) return;
            autosave.reset();
            state.loading = false;
            if (state.selectedEpisodeId) await selectEpisode(state.selectedEpisodeId, { preserveDraft: true });
          } catch (error) {
            if (disposed || token !== scopeToken || error?.stale) return;
            state.loading = false;
            state.historicalResultMissing = historicalResultMissing(error);
            state.loadError = errorMessage(error, text('工作台加载失败，请重试。', 'Workbench loading failed. Try again.'));
            if (!explicitReload) {
              state.packageSummary = null;
              state.episodes = [];
              autosave.reset();
            } else {
              if (!state.historicalResultMissing) autosave.resume();
            }
          }
        }

        async function selectEpisode(episodeOrId, { preserveDraft = true } = {}) {
          const id = idNumber(typeof episodeOrId === 'object' ? episodeOrId.id : episodeOrId);
          const summary = state.episodes.find((episode) => Number(episode.id) === Number(id));
          if (!summary || id === null) return;
          state.selectedEpisodeId = id;
          state.episodePage = Math.floor(state.episodes.indexOf(summary) / EPISODE_PAGE_SIZE) + 1;
          clearEpisodeMedia();
          state.episodeLoading = true;
          const token = scopeToken;
          try {
            const result = await episodeLoader.load(props.workItemId, id, props.workspaceId, props.mode);
            if (disposed || token !== scopeToken || result?.stale || Number(state.selectedEpisodeId) !== Number(id)) return;
            state.episodeData = result.episode;
            const key = idKey(id);
            if (!preserveDraft && result.episode.entry) {
              state.draftJson = { ...state.draftJson, episodes: { ...state.draftJson.episodes, [key]: result.episode.entry } };
            } else if (!state.draftJson.episodes[key] && result.episode.entry) {
              state.draftJson = { ...state.draftJson, episodes: { ...state.draftJson.episodes, [key]: result.episode.entry } };
            }
            if (!state.draftJson.episodes[key]) {
              state.draftJson = { ...state.draftJson, episodes: { ...state.draftJson.episodes, [key]: { conclusion: 'segments', segments: [] } } };
            }
            state.episodeError = '';
            state.mediaError = result.episode.media?.preview?.available
              ? ''
              : (result.episode.media?.preview?.disabled_reason || text('预览视频不可用。', 'Preview video is unavailable.'));
          } catch (error) {
            if (disposed || token !== scopeToken || error?.stale) return;
            state.episodeError = errorMessage(error, text('Episode 工作台加载失败，请重试。', 'Episode workbench failed to load. Try again.'));
            state.mediaError = state.episodeError;
          } finally {
            if (token === scopeToken && Number(state.selectedEpisodeId) === Number(id)) state.episodeLoading = false;
          }
        }

        function replaceCurrentEntry(nextEntry) {
          const key = idKey(state.selectedEpisodeId);
          if (!key) return;
          state.draftJson = { ...state.draftJson, episodes: { ...state.draftJson.episodes, [key]: normalizeEntry(nextEntry) } };
          markDirty();
        }

        function setConclusion(conclusion) {
          if (!canEditCurrent.value) return;
          if (conclusion === 'segments' && !canEditBounds.value) return;
          const current = currentEntry.value;
          if (conclusion === 'no_valid_segments') replaceCurrentEntry({ conclusion, reason: current.reason || '', segments: [] });
          else replaceCurrentEntry({ conclusion: 'segments', segments: Array.isArray(current.segments) ? current.segments : [] });
        }

        function updateNoValidReason(value) {
          const current = currentEntry.value;
          if (current.conclusion !== 'no_valid_segments' || !canEditCurrent.value) return;
          replaceCurrentEntry({ ...current, reason: reasonText(value), segments: [] });
        }

        function updateSegment(segmentId, field, value) {
          if (!canEditCurrent.value || currentEntry.value.conclusion !== 'segments') return;
          if (field !== 'description' && !canEditBounds.value) return;
          const segments = currentEntry.value.segments.map((segment) => (
            segment.id === segmentId ? { ...segment, [field]: field === 'description' ? descriptionText(value) : canonicalNs(value) || '' } : segment
          ));
          replaceCurrentEntry({ conclusion: 'segments', segments });
        }

        function selectSegment(segmentId, { seek = false } = {}) {
          state.selectedSegmentId = segmentId;
          const index = currentSegments.value.findIndex((segment) => segment.id === segmentId);
          if (index >= 0) state.segmentPage = Math.floor(index / SEGMENT_PAGE_SIZE) + 1;
          if (seek) {
            const segment = currentSegments.value.find((item) => item.id === segmentId);
            const seconds = playbackSecondsAtSourceTimestamp(segment?.start_ns, currentTimeline.value);
            if (seconds !== null && currentVideo()) currentVideo().currentTime = seconds;
          }
        }

        function deleteSegment(segmentId) {
          if (!canEditBounds.value || currentEntry.value.conclusion !== 'segments') return;
          replaceCurrentEntry({ conclusion: 'segments', segments: currentSegments.value.filter((segment) => segment.id !== segmentId) });
          if (state.selectedSegmentId === segmentId) state.selectedSegmentId = null;
        }

        function clearPendingSelection() {
          state.pendingTimestampNs = '';
          state.pendingStartNs = '';
          state.boundaryMode = 'idle';
          state.selectedSegmentId = null;
        }

        function mappedTimestampFromPointer(event) {
          const track = event?.currentTarget;
          const timeline = currentTimeline.value;
          if (!track || !timeline) return null;
          const rect = track.getBoundingClientRect();
          if (!rect.width) return null;
          const ratio = Math.max(0, Math.min(1, (Number(event.clientX) - rect.left) / rect.width));
          return sourceTimestampAtPlaybackRatio(ratio, timeline);
        }

        function handleTimelineClick(event) {
          if (!currentPreview.value?.mapping_available || !currentTimeline.value) return;
          const timestamp = mappedTimestampFromPointer(event);
          if (!timestamp) return;
          state.pendingTimestampNs = timestamp;
          if (!state.selectedSegmentId) state.boundaryMode = 'new';
          const seconds = playbackSecondsAtSourceTimestamp(timestamp, currentTimeline.value);
          if (seconds !== null && currentVideo()) currentVideo().currentTime = seconds;
        }

        function setInPoint() {
          if (!canEditBounds.value || !state.pendingTimestampNs) return;
          const timestamp = state.pendingTimestampNs;
          const selected = currentSegments.value.find((segment) => segment.id === state.selectedSegmentId);
          if (selected) {
            const end = nsBigInt(selected.end_ns);
            const start = nsBigInt(timestamp);
            if (start !== null && end !== null && start < end) {
              updateSegment(selected.id, 'start_ns', timestamp);
              state.pendingTimestampNs = '';
              state.boundaryMode = 'idle';
            }
            return;
          }
          state.pendingStartNs = timestamp;
          state.boundaryMode = 'new-out';
        }

        function setOutPoint() {
          if (!canEditBounds.value || !state.pendingTimestampNs) return;
          const timestamp = state.pendingTimestampNs;
          const selected = currentSegments.value.find((segment) => segment.id === state.selectedSegmentId);
          if (selected) {
            const start = nsBigInt(selected.start_ns);
            const end = nsBigInt(timestamp);
            if (start !== null && end !== null && start < end) {
              updateSegment(selected.id, 'end_ns', timestamp);
              state.pendingTimestampNs = '';
              state.boundaryMode = 'idle';
            }
            return;
          }
          const start = nsBigInt(state.pendingStartNs);
          const end = nsBigInt(timestamp);
          if (start === null || end === null || start >= end) return;
          const used = new Set(currentSegments.value.map((segment) => segment.id));
          const segment = { id: uniqueSegmentId(used), start_ns: start.toString(), end_ns: end.toString(), description: '' };
          replaceCurrentEntry({ conclusion: 'segments', segments: [...currentSegments.value, segment] });
          selectSegment(segment.id);
          state.pendingTimestampNs = '';
          state.pendingStartNs = '';
          state.boundaryMode = 'idle';
        }

        function newSegment() {
          if (!canEditBounds.value) return;
          if (currentEntry.value.conclusion !== 'segments') setConclusion('segments');
          state.selectedSegmentId = null;
          state.pendingTimestampNs = '';
          state.pendingStartNs = '';
          state.boundaryMode = 'new';
        }

        function onVideoTimeUpdate() {
          const video = currentVideo();
          if (!video) return;
          state.playbackSeconds = Number.isFinite(Number(video.currentTime)) ? Number(video.currentTime) : 0;
          state.playbackTimestampNs = sourceTimestampAtPlaybackSeconds(state.playbackSeconds, currentTimeline.value) || '';
          if (paintRaf === null && typeof requestAnimationFrame === 'function') {
            paintRaf = requestAnimationFrame(() => { paintRaf = null; });
          }
        }

        function seekTimestamp(timestamp) {
          const seconds = playbackSecondsAtSourceTimestamp(timestamp, currentTimeline.value);
          if (seconds === null || !currentVideo()) return false;
          currentVideo().currentTime = seconds;
          onVideoTimeUpdate();
          return true;
        }

        function segmentStyle(segment) {
          const bounds = currentBounds.value;
          const left = percentageForNs(segment?.start_ns, bounds);
          const right = percentageForNs(segment?.end_ns, bounds);
          return { left: `${left}%`, width: `${Math.max(0, right - left)}%` };
        }

        function playheadStyle() {
          return { left: `${percentageForNs(state.playbackTimestampNs, currentBounds.value)}%` };
        }

        function segmentValidity(segment) {
          return validateSegment(segment, currentBounds.value).valid;
        }

        function markSubmitIssues() {
          if (draftComplete.value.valid) return true;
          state.finalError = text(
            `请补全 ${draftComplete.value.missing.length + draftComplete.value.issues.length} 条 Episode：每条需填写有效片段和描述，或填写无有效片段原因。`,
            `Complete ${draftComplete.value.missing.length + draftComplete.value.issues.length} Episode entries with described segments or a no-valid reason.`,
          );
          return false;
        }

        async function ensureSaved() {
          if (state.conflict) return false;
          if (state.draftVersion < 1 && autosave.state().localRevision <= autosave.state().savedRevision) autosave.markDirty();
          const result = await autosave.flush({ drain: true, reason: 'final' });
          if (!result || state.conflict || state.draftVersion < 1) {
            state.finalError = state.saveError || text('草稿尚未保存。', 'The draft has not been saved.');
            return false;
          }
          return true;
        }

        function openSubmitConfirmation() {
          state.finalError = '';
          if (!canSubmit.value || !markSubmitIssues()) return;
          state.finalAction = 'submit';
        }

        function openReviewAction(action) {
          state.finalError = '';
          if (action === 'approved' && !canReviewApprove.value) return;
          if (action === 'rejected' && !canReviewReturn.value) return;
          state.finalAction = action;
          if (action !== 'rejected') state.returnReason = '';
        }

        async function confirmFinalAction() {
          const action = state.finalAction;
          if (!action || state.submitting) return;
          if (action === 'rejected' && !String(state.returnReason || '').trim()) {
            state.finalError = text('退回必须填写原因。', 'A reason is required when returning the result.');
            return;
          }
          const finalToken = scopeToken;
          state.submitting = true;
          state.finalError = '';
          try {
            let result;
            if (action === 'submit') {
              if (!(await ensureSaved())) return;
              if (!validateDraftForSubmit(state.draftJson, state.episodes.map((episode) => episode.id), {}).valid) {
                markSubmitIssues();
                return;
              }
              const api = runtimeApi();
              if (!api || typeof api.submitAnnotationWorkItem !== 'function') throw new Error('Annotation submit API is unavailable.');
              result = await api.submitAnnotationWorkItem(props.workItemId, buildSubmitPayload({
                workspaceId: props.workspaceId,
                baseVersion: state.draftVersion,
                expectedGeneration: state.generation,
              }));
            } else if (action === 'approved') {
              const api = runtimeApi();
              if (!api || typeof api.approveReviewWorkItem !== 'function') throw new Error('Review approval API is unavailable.');
              result = await api.approveReviewWorkItem(props.workItemId, buildReviewDecisionPayload({
                workspaceId: props.workspaceId,
                submissionId: state.submissionId,
                generation: state.generation,
              }));
            } else {
              const api = runtimeApi();
              if (!api || typeof api.returnReviewWorkItem !== 'function') throw new Error('Review return API is unavailable.');
              result = await api.returnReviewWorkItem(props.workItemId, buildReviewDecisionPayload({
                workspaceId: props.workspaceId,
                submissionId: state.submissionId,
                generation: state.generation,
                reason: state.returnReason,
              }));
            }
            if (disposed || finalToken !== scopeToken) return;
            state.completed = true;
            state.capabilities = { edit: false, save: false, submit: false, approve: false, return: false, read_only: true };
            state.finalAction = '';
            await loadScope();
            emit('completed', result);
          } catch (error) {
            if (isConflict(error)) { state.conflict = true; state.saveState = 'conflict'; autosave.pause(); }
            state.finalError = errorMessage(error, text('提交失败，请重试。', 'Submission failed. Try again.'));
          } finally {
            state.submitting = false;
          }
        }

        function hasUnsaved() {
          const snapshot = autosave.state();
          return snapshot.dirty || snapshot.saving || state.saveState === 'failed' || state.conflict;
        }

        async function canLeave() {
          if (isReview.value || !hasUnsaved()) return true;
          if (state.conflict) {
            if (typeof window === 'undefined' || typeof window.confirm !== 'function') return false;
            return window.confirm(text('草稿有版本冲突，离开将放弃当前编辑。确定离开吗？', 'The draft has a version conflict. Leaving will discard the edits. Leave anyway?'));
          }
          const flushed = await autosave.flush({ drain: true, reason: 'leave' });
          return Boolean(flushed && !hasUnsaved());
        }

        async function requestBack() {
          if (!(await canLeave())) return;
          clearEpisodeMedia();
          emit('back');
        }

        function beforeUnload(event) {
          if (!hasUnsaved() || isReview.value) return;
          event.preventDefault();
          event.returnValue = '';
        }

        async function reload() {
          state.saveError = '';
          state.finalError = '';
          await loadScope({ explicitReload: true });
        }

        function saveLabel() {
          if (!state.packageSummary || state.loading || state.loadError) return '';
          if (state.conflict) return text('版本冲突', 'Version conflict');
          if (state.saving) return text('保存中…', 'Saving…');
          if (state.saveState === 'failed') return text('保存失败', 'Save failed');
          if (state.saveState === 'unsaved') return text('有未保存更改', 'Unsaved changes');
          return text('已保存', 'Saved');
        }

        function workItemStatusLabel() {
          if (!state.packageSummary || state.loading || state.loadError) return '';
          const labels = {
            assigned: text('待开始', 'Assigned'),
            in_progress: isReview.value ? text('待确认', 'Ready for review') : text('进行中', 'In progress'),
            returned: text('已退回，待修改', 'Returned for changes'),
            submitted: text('已提交，待确认', 'Submitted for review'),
            done: text('已完成', 'Completed'),
            accepted: text('已通过', 'Approved'),
            approved: text('已通过', 'Approved'),
            rejected: text('已退回', 'Returned'),
          };
          return labels[workItemStatus.value] || text('待处理', 'Pending');
        }

        function conclusionLabel(conclusion) {
          return conclusion === 'no_valid_segments' ? text('无有效片段', 'No valid segments') : text('有效片段', 'Segments');
        }

        function entryComplete(episode) {
          const key = idKey(episode?.id);
          const entry = key ? state.draftJson?.episodes?.[key] : null;
          return Boolean(entry && validateEntry(entry, episodeBounds(episode), { complete: true }).valid);
        }

        function statusLabel(episode) {
          const key = idKey(episode?.id);
          const entry = key ? state.draftJson?.episodes?.[key] : null;
          if (!entry || !entry.conclusion) return text('待处理', 'Pending');
          if (entryComplete(episode)) return entry.conclusion === 'no_valid_segments'
            ? text('无有效片段', 'No valid segments')
            : text('已标注', 'Annotated');
          if (entry.conclusion === 'no_valid_segments') return text('待补原因', 'Needs reason');
          if (!entry.segments?.length) return text('待补片段', 'Needs segments');
          const issue = validateEntry(entry, episodeBounds(episode), { complete: true }).code;
          if (issue === 'description') return text('待补描述', 'Needs description');
          return text('待完成', 'Incomplete');
        }

        function statusClass(episode) {
          const key = idKey(episode?.id);
          const entry = key ? state.draftJson?.episodes?.[key] : null;
          if (!entryComplete(episode)) return 'pending';
          return entry?.conclusion === 'no_valid_segments' ? 'no-valid' : 'complete';
        }

        function reasonForIssue(issue) {
          return {
            missing: text('尚未填写结论。', 'No conclusion yet.'),
            segments: text('至少添加一个有效片段。', 'Add at least one valid segment.'),
            description: text('每个片段都需要语言描述。', 'Every segment needs a description.'),
            reason: text('无有效片段需要原因。', 'A no-valid conclusion needs a reason.'),
            overlap: text('片段不能重叠。', 'Segments cannot overlap.'),
            bounds: text('片段必须在 Episode 范围内。', 'Segments must stay inside the Episode bounds.'),
          }[issue] || text('标注内容未完成。', 'Annotation is incomplete.');
        }

        function confirmationSummary() {
          if (isReview.value) {
            return text(
              `当前提交包含 ${packageProgress.value.segmentCount} 个有效片段、${packageProgress.value.noValid} 条无有效片段 Episode，共 ${packageProgress.value.total} 条。`,
              `This submission contains ${packageProgress.value.segmentCount} valid segments and ${packageProgress.value.noValid} no-valid Episodes across ${packageProgress.value.total} Episodes.`,
            );
          }
          return text(
            `当前包括 ${packageProgress.value.processed} 条已完成、${packageProgress.value.pending} 条待处理、${packageProgress.value.segmentCount} 个片段。`,
            `This includes ${packageProgress.value.processed} complete, ${packageProgress.value.pending} pending, and ${packageProgress.value.segmentCount} segments.`,
          );
        }

        function formatDurationValue(seconds) {
          const number = Number(seconds);
          if (!Number.isFinite(number) || number < 0) return '—';
          const total = Math.floor(number);
          const hours = Math.floor(total / 3600);
          const minutes = Math.floor((total % 3600) / 60);
          const rest = total % 60;
          return hours ? `${hours}:${String(minutes).padStart(2, '0')}:${String(rest).padStart(2, '0')}` : `${minutes}:${String(rest).padStart(2, '0')}`;
        }

        function sourceOffsetLabel(value, bounds = currentBounds.value) {
          const point = nsBigInt(value);
          const normalized = episodeBounds(bounds);
          if (point === null || !normalized || point < normalized.start || point > normalized.end) return '—';
          const offset = point - normalized.start;
          const milliseconds = String((offset % 1_000_000_000n) / 1_000_000n).padStart(3, '0').replace(/0+$/, '');
          return formatDurationValue(Number(offset / 1_000_000_000n)) + (milliseconds ? `.${milliseconds}` : '');
        }

        function exactTimestampTitle(value) {
          const timestamp = canonicalNs(value);
          return timestamp ? `${text('精确源时间戳', 'Exact source timestamp')}: ${timestamp} ns` : '';
        }

        function segmentRangeLabel(segment) {
          return `${sourceOffsetLabel(segment?.start_ns)} → ${sourceOffsetLabel(segment?.end_ns)}`;
        }

        function exactRangeTitle(range) {
          const start = canonicalNs(range?.start_ns);
          const end = canonicalNs(range?.end_ns);
          return start && end ? `${text('精确范围', 'Exact range')}: ${start} → ${end} ns` : '';
        }

        function boundaryPrompt() {
          if (!canEditBounds.value) return text('当前媒体没有精确映射，不能调整边界。', 'Exact media mapping is unavailable, so boundaries cannot be edited.');
          if (state.pendingStartNs) return text('入点已选择，请点击时间轴选择出点。', 'In point selected. Click the timeline to choose the out point.');
          if (state.pendingTimestampNs) return text('已选择时间，请点击“设为入点”。', 'Timestamp selected. Click “Set in point”.');
          if (state.boundaryMode === 'new') return text('新建片段：点击时间轴选择入点。', 'New segment: click the timeline to choose an in point.');
          if (state.selectedSegmentId) return text('已选中片段，可调整入点或出点。', 'Segment selected. Adjust its in or out point.');
          return text('点击“新建片段”开始标注。', 'Click “New segment” to start annotating.');
        }

        function attachListeners() {
          if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') window.addEventListener('beforeunload', beforeUnload);
        }

        Vue.watch(() => [props.workspaceId, props.workItemId, props.mode], () => { void loadScope(); }, { immediate: true });
        Vue.onMounted(attachListeners);
        Vue.onBeforeUnmount(() => {
          disposed = true;
          if (typeof window !== 'undefined' && typeof window.removeEventListener === 'function') window.removeEventListener('beforeunload', beforeUnload);
          clearEpisodeMedia();
          packageLoader.dispose();
          episodeLoader.dispose();
          autosave.dispose();
        });
        if (typeof expose === 'function') expose({ canLeave, hasUnsaved, reload });

        return {
          state,
          packageData,
          workItem,
          selectedEpisodeSummary,
          currentEntry,
          entryFor,
          currentPreview,
          currentBounds,
          currentTimeline,
          currentSegments,
          episodePage,
          segmentPage,
          canSetOutPoint,
          canEditCurrent,
          canEditBounds,
          currentValidity,
          packageProgress,
          draftComplete,
          finalDisabledReason,
          canSubmit,
          canReviewApprove,
          canReviewReturn,
          annotationFinalized,
          reviewFinalized,
          isReview,
          text,
          selectEpisode,
          setConclusion,
          updateNoValidReason,
          updateSegment,
          selectSegment,
          deleteSegment,
          newSegment,
          handleTimelineClick,
          setInPoint,
          setOutPoint,
          seekTimestamp,
          onVideoTimeUpdate,
          segmentStyle,
          playheadStyle,
          segmentValidity,
          openSubmitConfirmation,
          openReviewAction,
          confirmFinalAction,
          requestBack,
          reload,
          saveLabel,
          conclusionLabel,
          statusLabel,
          statusClass,
          workItemStatusLabel,
          reasonForIssue,
          confirmationSummary,
          boundaryPrompt,
          sourceOffsetLabel,
          segmentRangeLabel,
          exactTimestampTitle,
          exactRangeTitle,
          entryComplete,
          setVideoElement: (element) => { videoElement.value = element; },
          isSelectedSegment: (segment) => segment?.id === state.selectedSegmentId,
          formatNs: (value) => canonicalNs(value) || '—',
          formatDuration: formatDurationValue,
        };
      },
      template: `
        <section class="package-annotation-workbench" :class="{ 'is-review': isReview, 'is-readonly': !state.capabilities.edit, 'is-conflict': state.conflict, 'is-completed': state.completed }" :aria-busy="state.loading || state.submitting">
          <header class="package-annotation-header">
            <button type="button" class="package-annotation-back" @click="requestBack">‹ {{ text('返回工作项', 'Back to work items') }}</button>
            <div class="package-annotation-title"><p>{{ isReview ? text('标注结果', 'Annotation result') : text('标注', 'Annotation') }}</p><h1>{{ packageData.package_uid || text('包级工作台', 'Package workbench') }}</h1><span v-if="packageData.task_name || packageData.batch_name">{{ packageData.task_name || '—' }} · {{ packageData.batch_name || '—' }}</span><small v-if="workItemStatusLabel()" class="package-annotation-work-item-status">{{ workItemStatusLabel() }}</small></div>
            <div v-if="saveLabel()" class="package-annotation-header-meta"><span class="package-annotation-save" :class="'save-' + state.saveState">{{ saveLabel() }}</span></div>
          </header>

          <div v-if="state.loading" class="package-annotation-state"><span class="package-annotation-spinner"></span><p>{{ text('正在加载包级标注工作台…', 'Loading package annotation workbench…') }}</p></div>
          <div v-else-if="state.loadError" class="package-annotation-state package-annotation-error"><p>{{ state.loadError }}</p><button v-if="state.historicalResultMissing" type="button" class="package-annotation-button secondary" @click="requestBack">{{ text('返回工作项', 'Back to work items') }}</button><button v-else type="button" class="package-annotation-button secondary" @click="reload">{{ text('重新加载', 'Reload') }}</button></div>
          <div v-else-if="state.packageSummary" class="package-annotation-layout">
            <aside class="package-annotation-episodes">
              <div class="package-annotation-panel-heading"><div><h2>{{ text('Episode', 'Episodes') }}</h2><span>{{ packageProgress.processed }}/{{ packageProgress.total }} {{ text('已处理', 'processed') }}</span></div><strong>{{ state.episodes.length }}</strong></div>
              <div v-if="episodePage.pages > 1" class="package-annotation-pagination"><button type="button" :disabled="episodePage.page === 1" @click="state.episodePage = episodePage.page - 1">{{ text('上一页', 'Previous') }}</button><span>{{ episodePage.page }} / {{ episodePage.pages }}</span><button type="button" :disabled="episodePage.page === episodePage.pages" @click="state.episodePage = episodePage.page + 1">{{ text('下一页', 'Next') }}</button></div>
              <nav class="package-annotation-episode-list" aria-label="Episode navigator"><button v-for="episode in episodePage.items" :key="episode.id" type="button" class="package-annotation-episode" :class="{ selected: Number(state.selectedEpisodeId) === Number(episode.id) }" @click="selectEpisode(episode)"><span class="package-annotation-episode-index">{{ episode.index + 1 }}</span><span class="package-annotation-episode-main"><strong>{{ episode.label }}</strong><small>{{ statusLabel(episode) }}</small></span><i :class="'dot-' + statusClass(episode)"></i></button></nav>
              <div v-if="!state.episodes.length" class="package-annotation-empty">{{ text('没有冻结的 Episode。', 'No frozen Episodes.') }}</div>
            </aside>

            <main class="package-annotation-center">
              <div class="package-annotation-episode-heading"><div><p>{{ text('当前 Episode', 'Current Episode') }}</p><h2>{{ selectedEpisodeSummary?.label || '—' }}</h2></div><div v-if="state.episodeData" class="package-annotation-episode-meta"><span>{{ state.episodeData.episode?.admission_attempt ? 'Attempt ' + state.episodeData.episode.admission_attempt : '' }}</span><span v-if="currentBounds">{{ formatDuration(currentBounds.duration_s) }}</span></div></div>
              <div class="package-annotation-video-shell"><div v-if="state.episodeLoading" class="package-annotation-video-placeholder"><span class="package-annotation-spinner"></span><p>{{ text('正在读取 Episode 预览…', 'Loading Episode preview…') }}</p></div><video v-else-if="currentPreview?.available && currentPreview.url" :ref="setVideoElement" class="package-annotation-video" :src="currentPreview.url" controls playsinline preload="metadata" @timeupdate="onVideoTimeUpdate" @loadedmetadata="onVideoTimeUpdate" @error="state.mediaError = text('视频播放失败。', 'The video could not be played.')"></video><div v-else class="package-annotation-video-placeholder"><span class="package-annotation-video-icon">▶</span><p>{{ state.mediaError || text('当前 Episode 没有可用视频。', 'No video is available for this Episode.') }}</p></div></div>
              <div class="package-annotation-playhead-label"><span>{{ text('当前映射时间', 'Mapped time') }}</span><code :title="exactTimestampTitle(state.playbackTimestampNs)">{{ sourceOffsetLabel(state.playbackTimestampNs) }}</code></div>
              <section class="package-annotation-timeline-card"><div class="package-annotation-timeline-heading"><div><h3>{{ text('有效片段时间轴', 'Valid segment timeline') }}</h3><span v-if="currentBounds" :title="exactRangeTitle(currentBounds)">{{ sourceOffsetLabel(currentBounds.start_ns, currentBounds) }} → {{ sourceOffsetLabel(currentBounds.end_ns, currentBounds) }}</span></div><span v-if="currentPreview && !currentPreview.mapping_available" class="package-annotation-mapping-badge">{{ text('不可精确编辑', 'Editing unavailable') }}</span></div><div class="package-annotation-timeline-track" :class="{ disabled: !currentPreview?.mapping_available }" @click="handleTimelineClick"><span v-for="segment in currentSegments" :key="segment.id" class="package-annotation-segment-bar" :class="{ selected: isSelectedSegment(segment), invalid: !segmentValidity(segment) }" :style="segmentStyle(segment)" @click.stop="selectSegment(segment.id, { seek: true })"></span><i class="package-annotation-playhead" :style="playheadStyle()"></i></div><p v-if="currentPreview && !currentPreview.mapping_available" class="package-annotation-mapping-warning">{{ currentPreview.disabled_reason || state.episodeData?.capabilities?.disabled_reason || text('精确预览映射不可用，暂时不能调整片段边界。', 'The exact preview mapping is unavailable, so segment boundaries cannot be edited.') }}</p><div v-else-if="canEditBounds" class="package-annotation-boundary-tools"><button type="button" class="package-annotation-button secondary" :class="{ 'is-armed': state.boundaryMode === 'new' || state.boundaryMode === 'new-out' }" @click="newSegment">{{ text('新建片段', 'New segment') }}</button><button type="button" class="package-annotation-button" :disabled="!state.pendingTimestampNs" @click="setInPoint">{{ text('设为入点', 'Set in point') }}</button><button type="button" class="package-annotation-button" :disabled="!canSetOutPoint" @click="setOutPoint">{{ text('设为出点', 'Set out point') }}</button><span class="package-annotation-boundary-status" :class="{ 'is-ready': state.pendingStartNs }">{{ boundaryPrompt() }}</span><span v-if="state.pendingStartNs" class="package-annotation-boundary-value" :title="exactTimestampTitle(state.pendingStartNs)">{{ text('入点', 'In') }} {{ sourceOffsetLabel(state.pendingStartNs) }}</span><span v-if="state.pendingTimestampNs" class="package-annotation-boundary-value" :title="exactTimestampTitle(state.pendingTimestampNs)">{{ text('当前选择', 'Selected') }} {{ sourceOffsetLabel(state.pendingTimestampNs) }}</span></div></section>
              <div v-if="state.episodeError" class="package-annotation-inline-error">{{ state.episodeError }}</div>
            </main>

            <aside class="package-annotation-right">
              <section class="package-annotation-card package-annotation-entry-card"><div class="package-annotation-panel-heading"><div><h2>{{ text('当前标注', 'Current annotation') }}</h2><span>{{ selectedEpisodeSummary?.label || '—' }}</span></div><span class="package-annotation-conclusion">{{ conclusionLabel(currentEntry.conclusion) }}</span></div>
                <template v-if="currentEntry.conclusion === 'no_valid_segments'"><div class="package-annotation-no-valid"><strong>{{ text('本条无有效片段', 'No valid segments') }}</strong><label><span>{{ text('原因', 'Reason') }} <em>*</em></span><textarea :value="currentEntry.reason || ''" :disabled="!canEditCurrent" maxlength="2000" :placeholder="text('说明为什么没有可用片段。', 'Explain why there are no usable segments.')" @input="updateNoValidReason($event.target.value)"></textarea></label></div></template>
                <template v-else><div class="package-annotation-conclusion-toggle" v-if="canEditCurrent"><button type="button" :class="{ active: currentEntry.conclusion === 'segments' }" :disabled="!canEditBounds" @click="setConclusion('segments')">{{ text('有效片段', 'Segments') }}</button><button type="button" @click="setConclusion('no_valid_segments')">{{ text('无有效片段', 'No valid segments') }}</button></div><div v-if="!currentSegments.length" class="package-annotation-empty-entry">{{ text('在时间轴点击位置后设置入点和出点。', 'Click the timeline, then set an in and out point.') }}</div><div v-if="segmentPage.pages > 1" class="package-annotation-pagination"><button type="button" :disabled="segmentPage.page === 1" @click="state.segmentPage = segmentPage.page - 1">{{ text('上一页', 'Previous') }}</button><span>{{ segmentPage.page }} / {{ segmentPage.pages }}</span><button type="button" :disabled="segmentPage.page === segmentPage.pages" @click="state.segmentPage = segmentPage.page + 1">{{ text('下一页', 'Next') }}</button></div><div v-for="(segment, index) in segmentPage.items" :key="segment.id" class="package-annotation-segment-row" :class="{ selected: isSelectedSegment(segment), invalid: !segmentValidity(segment) }"><button type="button" class="package-annotation-segment-select" @click="selectSegment(segment.id, { seek: true })"><span>#{{ segmentPage.offset + index + 1 }}</span><small :title="exactRangeTitle(segment)">{{ segmentRangeLabel(segment) }}</small></button><textarea :value="segment.description" :disabled="!canEditCurrent" maxlength="4000" :placeholder="text('输入语言描述。', 'Describe the action in natural language.')" @input="updateSegment(segment.id, 'description', $event.target.value)"></textarea><button v-if="canEditBounds" type="button" class="package-annotation-delete" @click="deleteSegment(segment.id)">×</button><small v-if="!segmentValidity(segment)" class="package-annotation-field-error">{{ reasonForIssue(currentValidity.code) }}</small></div></template>
              </section>

              <section class="package-annotation-card package-annotation-progress-card"><div class="package-annotation-panel-heading"><div><h2>{{ text('包级进度', 'Package progress') }}</h2><span>{{ text('所有冻结 Episode 都要有明确结论', 'Every frozen Episode needs an explicit conclusion') }}</span></div></div><div class="package-annotation-progress-grid"><div><strong>{{ packageProgress.processed }}</strong><span>{{ text('已完成', 'Complete') }}</span></div><div><strong>{{ packageProgress.pending }}</strong><span>{{ text('待处理', 'Pending') }}</span></div><div><strong>{{ packageProgress.segmentCount }}</strong><span>{{ text('片段', 'Segments') }}</span></div></div><div class="package-annotation-progress-bar"><span :style="{ width: packageProgress.total ? (packageProgress.processed / packageProgress.total * 100) + '%' : '0%' }"></span></div></section>

              <section class="package-annotation-card package-annotation-final-card">
                <div v-if="workItem.return_reason" class="package-annotation-return-note"><strong>{{ text('返工要求', 'Return reason') }}</strong><p>{{ workItem.return_reason }}</p></div>
                <div class="package-annotation-panel-heading"><div><h2>{{ isReview ? text('确认结果', 'Confirm result') : annotationFinalized ? text('标注状态', 'Annotation status') : text('提交标注', 'Submit annotation') }}</h2><span>{{ isReview ? text('固定提交版本只读展示。', 'The submitted version is fixed and read-only.') : annotationFinalized ? workItemStatusLabel() : text('提交前检查全部 Episode。', 'Check every Episode before submitting.') }}</span></div></div>
                <p v-if="finalDisabledReason" class="package-annotation-disabled-reason">{{ finalDisabledReason }}</p>
                <p v-if="state.finalError" class="package-annotation-inline-error">{{ state.finalError }}</p>
                <div v-if="state.conflict" class="package-annotation-conflict"><strong>{{ text('版本冲突', 'Version conflict') }}</strong><p>{{ text('当前编辑仍保留在页面中。重新加载会读取服务端版本。', 'Your edits remain on this page. Reloading reads the server version.') }}</p><button type="button" class="package-annotation-button secondary" @click="reload">{{ text('重新加载', 'Reload') }}</button></div>
                <template v-if="state.finalAction"><div class="package-annotation-confirm"><strong>{{ state.finalAction === 'submit' ? text('确认提交全部标注？', 'Submit all annotations?') : state.finalAction === 'approved' ? text('确认通过此提交？', 'Approve this submission?') : text('确认退回此提交？', 'Return this submission?') }}</strong><p>{{ isReview ? text('通过或退回会固定作用于当前提交版本。', 'Approval or return applies to this fixed submission.') : confirmationSummary() }}</p><label v-if="state.finalAction === 'rejected'" class="package-annotation-reason-field"><span>{{ text('退回原因', 'Return reason') }} <em>*</em></span><textarea v-model="state.returnReason" maxlength="2000" :placeholder="text('请输入退回原因。', 'Enter a return reason.')"></textarea></label><div class="package-annotation-confirm-actions"><button type="button" class="package-annotation-button secondary" :disabled="state.submitting" @click="state.finalAction = ''">{{ text('返回修改', 'Back to edits') }}</button><button type="button" class="package-annotation-button" :class="state.finalAction === 'approved' || state.finalAction === 'submit' ? 'primary' : 'danger'" :disabled="state.submitting" @click="confirmFinalAction">{{ state.submitting ? text('提交中…', 'Submitting…') : (state.finalAction === 'submit' ? text('确认提交', 'Confirm submit') : state.finalAction === 'approved' ? text('确认通过', 'Confirm approval') : text('确认退回', 'Confirm return')) }}</button></div></div></template>
                <div v-else-if="!isReview && !annotationFinalized" class="package-annotation-final-actions"><button type="button" class="package-annotation-button primary" :disabled="!canSubmit || !draftComplete.valid" @click="openSubmitConfirmation">{{ text('提交标注', 'Submit annotation') }}</button></div>
                <div v-else-if="isReview && !reviewFinalized" class="package-annotation-final-actions"><button type="button" class="package-annotation-button primary" :disabled="!canReviewApprove" @click="openReviewAction('approved')">{{ text('通过', 'Approve') }}</button><button type="button" class="package-annotation-button danger" :disabled="!canReviewReturn" @click="openReviewAction('rejected')">{{ text('退回', 'Return') }}</button></div>
                <div v-else class="package-annotation-final-status"><strong>{{ workItemStatusLabel() }}</strong><span>{{ isReview ? text('此提交已完成确认。', 'This submission has completed review.') : text('当前标注无需再次提交。', 'This annotation does not need another submission.') }}</span></div>
              </section>
            </aside>
          </div>
        </section>`,
    });
    return app;
  }

  return {
    DRAFT_SCHEMA,
    normalizeDraftJson,
    normalizeEntry,
    normalizePlaybackTimeline,
    sourceTimestampAtPlaybackSeconds,
    playbackSecondsAtSourceTimestamp,
    sourceTimestampAtPlaybackRatio,
    episodeBounds,
    percentageForNs,
    validateSegment,
    validateEntry,
    validateDraftForSubmit,
    normalizePackageSummary,
    normalizeEpisodeResponse,
    buildDraftEnvelope,
    buildSavePayload,
    buildSubmitPayload,
    buildReviewDecisionPayload,
    scopeMatches,
    createWorkbenchLoader,
    createEpisodeLoader,
    createAutosaveCoordinator,
    errorMessage,
    isConflict,
    install,
  };
})();
