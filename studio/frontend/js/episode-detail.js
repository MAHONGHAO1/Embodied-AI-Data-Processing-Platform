/** Shared read-only Episode detail drawer pure function state helper, responsible for read-only field projection and source view tracking. */
const QuicDataEpisodeDetail = (() => {
  const SOURCES = new Set(['assets', 'work-queue', 'batch', 'overview', 'dataset-builder']);
  const QUEUE_STAGES = new Set(['cut', 'annotation', 'review', 'completed']);
  const EPISODE_FIELDS = [
    'id',
    'episode_uid',
    'kind',
    'parent_episode_id',
    'parent_episode_uid',
    'derived_version',
    'workspace_id',
    'task_set_id',
    'batch_id',
    'import_session_id',
    'embodiment_id',
    'task_label_id',
    'task_label_key',
    'task_label_name',
    'task_name',
    'task_language',
    'modality',
    'scene',
    'collector_profile_id',
    'collection_device_id',
    'source_start_ns',
    'source_end_ns',
    'duration_s',
    'reference_frame_count',
    'average_rgb_rate_hz',
    'quality_status',
    'annotation_status',
    'review_status',
    'publication_status',
    'published_at',
    'created_at',
    'updated_at',
  ];
  const WORK_ITEM_FIELDS = ['id', 'kind', 'status', 'updated_at', 'assignee_user_id'];
  const PUBLICATION_FIELDS = ['category', 'status', 'retryable', 'published_at'];

  function copyFields(value, fields) {
    if (!value || typeof value !== 'object') return {};
    const result = {};
    for (const field of fields) {
      if (value[field] !== undefined) result[field] = value[field];
    }
    return result;
  }

  function normalizeId(value) {
    if (value === null || value === undefined || value === '') return null;
    const number = Number(value);
    return Number.isSafeInteger(number) && number > 0 ? number : null;
  }

  function safeEpisodeSummary(value) {
    const summary = copyFields(value, EPISODE_FIELDS);
    const id = normalizeId(summary.id);
    if (id !== null) summary.id = id;
    else delete summary.id;
    if (value?.publication && typeof value.publication === 'object') {
      summary.publication = copyFields(value.publication, PUBLICATION_FIELDS);
    }
    return summary;
  }

  function safeWorkItemSummary(value) {
    if (!value || typeof value !== 'object') return null;
    const summary = copyFields(value, WORK_ITEM_FIELDS);
    const id = normalizeId(summary.id);
    if (id !== null) summary.id = id;
    else delete summary.id;
    return Object.keys(summary).length ? summary : null;
  }

  function normalizeContext(row, options = {}) {
    const episodeValue = row?.episode && typeof row.episode === 'object' ? row.episode : row;
    const episode = safeEpisodeSummary(episodeValue);
    const episodeId = normalizeId(episode.id ?? row?.episode_id);
    if (episodeId !== null && episode.id === undefined) episode.id = episodeId;
    const source = SOURCES.has(options.source) ? options.source : 'overview';
    const queueStage = QUEUE_STAGES.has(options.queueStage) ? options.queueStage : '';
    const previewStatus = options.previewStatus
      ?? row?.preview_status
      ?? row?.media_preview_status
      ?? row?.preview?.status
      ?? '';
    return {
      source,
      episodeId,
      episode,
      workItem: safeWorkItemSummary(row?.work_item),
      queueStage,
      previewStatus: typeof previewStatus === 'string' ? previewStatus : '',
    };
  }

  function mergeEpisodeDetail(context, detail) {
    const summary = context?.episode && typeof context.episode === 'object' ? context.episode : {};
    const response = detail && typeof detail === 'object' ? detail : {};
    const publication = {
      ...(summary.publication && typeof summary.publication === 'object' ? summary.publication : {}),
      ...(response.publication && typeof response.publication === 'object' ? response.publication : {}),
    };
    const merged = { ...summary, ...response };
    if (response.published_at === null || response.published_at === undefined || response.published_at === '') {
      merged.published_at = summary.published_at ?? publication.published_at ?? null;
    }
    if (Object.keys(publication).length) merged.publication = publication;
    return merged;
  }

  function createRequestGate() {
    let generation = 0;
    let currentEpisodeId = null;
    return {
      begin(episodeId) {
        generation += 1;
        currentEpisodeId = normalizeId(episodeId);
        return { generation, episodeId: currentEpisodeId };
      },
      isCurrent(token, episodeId) {
        const normalizedEpisodeId = normalizeId(episodeId);
        return Boolean(token)
          && token.generation === generation
          && token.episodeId === currentEpisodeId
          && normalizedEpisodeId === currentEpisodeId;
      },
      invalidate() {
        generation += 1;
        currentEpisodeId = null;
      },
    };
  }

  return {
    normalizeContext,
    mergeEpisodeDetail,
    createRequestGate,
  };
})();
