/* Local-only projections for inspecting workflows without a backend. Writes stay in memory. */
const QuicDataDemo = (() => {
  const user = {
    id: 9000,
    email: 'demo@local.preview',
    nickname: '本地演示',
    role: 'admin',
    permissions: ['*'],
    must_change_password: false,
  };
  const timeline = {
    start_ns: '0',
    end_ns: '125000000000',
    duration_s: 125,
    reference_topic: '/camera/front/image_raw',
    boundary_suggestions: [
      { timestamp_ns: '33000000000', segment_id_hint: 'qr-001', segment_index: 1, protocol_version: 1 },
      { timestamp_ns: '75000000000', segment_id_hint: 'qr-002', segment_index: 2, protocol_version: 1 },
    ],
  };
  const demoPreviewUrl = 'https://vjs.zencdn.net/v/oceans.mp4';
  const playbackTimeline = {
    encoded_fps: 1,
    encoded_duration_s: 46.612,
    frame_timestamps_ns: Array.from({ length: 126 }, (_, index) => String(index * 1000000000)),
  };
  const episode = {
    id: 4200,
    episode_uid: 'EGO-DEMO-0042',
    workspace_id: 1,
    task_set_id: 12,
    task_label: { id: 3, name: '拿起水杯并放入托盘' },
    quality: { status: 'passed' },
    human_stage: { status: 'in_progress' },
    review: { status: 'pending' },
    publication: { status: 'not_started' },
  };
  const batches = [
    {
      id: 501, sequence_number: 18, name: '桌面物体整理 · 8 月第 2 批', batch_type: 'ego', status: 'processing',
      workspace_id: 1, task_set_id: 12, task_label: episode.task_label, created_at: '2026-08-11T02:30:00Z',
      source_episode_count: 12, source_duration_s: 4280, source_summary: { upload: 9, oss: 3 },
      quality_progress: { completed: 8, total: 12, failed: 1 },
    },
    {
      id: 502, sequence_number: 17, name: '水杯与托盘动作补采', batch_type: 'ego', status: 'ready',
      workspace_id: 1, task_set_id: 12, task_label: episode.task_label, created_at: '2026-08-09T07:15:00Z',
      source_episode_count: 24, source_duration_s: 8160, source_summary: { upload: 24 },
      quality_progress: { completed: 24, total: 24, failed: 0 },
    },
    {
      id: 503, sequence_number: 16, name: 'EGO 现场采集回传', batch_type: 'ego', status: 'importing',
      workspace_id: 1, task_set_id: 12, task_label: episode.task_label, created_at: '2026-08-08T04:20:00Z',
      source_episode_count: 5, source_duration_s: 1760, source_summary: { oss: 5 },
      quality_progress: { completed: 2, total: 5, failed: 0 },
    },
  ];
  const importSessions = {
    501: [
      { id: 'demo-import-501-a', batch_id: 501, import_type: 'chunked_upload', original_name: 'ego_station_a_0811.zip', task_label_id: 3, status: 'parsing', updated_at: '2026-08-11T03:18:00Z', upload_progress: { uploaded_chunks: 48, total_chunks: 48, percent: 100 }, available_actions: [] },
      { id: 'demo-import-501-b', batch_id: 501, import_type: 'oss_scan', original_name: '现场采集 OSS 扫描', task_label_id: 3, status: 'succeeded', updated_at: '2026-08-11T02:58:00Z', available_actions: [] },
    ],
    502: [
      { id: 'demo-import-502-a', batch_id: 502, import_type: 'chunked_upload', original_name: 'cup_tray_reshoot.zip', task_label_id: 3, status: 'succeeded', updated_at: '2026-08-09T08:40:00Z', available_actions: [] },
    ],
    503: [
      { id: 'demo-import-503-a', batch_id: 503, import_type: 'oss_scan', original_name: 'ego/raw/2026-08-08', task_label_id: 3, status: 'uploading', updated_at: '2026-08-08T05:02:00Z', upload_progress: { uploaded_chunks: 18, total_chunks: 30, percent: 60 }, available_actions: [] },
    ],
  };
  // Review batches for the mining task packages: sequence_number matches the
  // package seq, so the console can jump straight from a package to its review.
  const reviewBatchStatuses = ['ready', 'ready', 'processing', 'processing', 'processing', 'importing', 'importing', 'created', 'created', 'created'];
  const reviewBatchSeqs = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10];
  const reviewBatches = [];
  reviewBatchSeqs.forEach((seq, index) => {
    const batchId = 601 + index;
    reviewBatches.push({
      id: batchId,
      sequence_number: seq,
      name: `桌面物体整理 · 8 月采集 #${seq}`,
      batch_type: 'ego',
      status: reviewBatchStatuses[index],
      workspace_id: 1,
      task_set_id: 12,
      task_label: episode.task_label,
      created_at: `2026-08-${String(index + 1).padStart(2, '0')}T02:30:00Z`,
      source_episode_count: 12 + index,
      source_duration_s: 4200 + index * 180,
      source_summary: { upload: 8 + index, oss: 4 },
      quality_progress: { completed: 6 + index, total: 12 + index, failed: index % 4 },
    });
    importSessions[batchId] = [
      {
        id: `demo-import-${batchId}-a`,
        batch_id: batchId,
        import_type: 'chunked_upload',
        original_name: `ego_station_${seq}_part1.zip`,
        task_label_id: 3,
        status: index < 6 ? 'succeeded' : (index < 8 ? 'parsing' : 'uploading'),
        updated_at: `2026-08-${String(index + 1).padStart(2, '0')}T03:18:00Z`,
        upload_progress: { uploaded_chunks: 48, total_chunks: 48, percent: 100 },
        available_actions: [],
      },
      {
        id: `demo-import-${batchId}-b`,
        batch_id: batchId,
        import_type: 'oss_scan',
        original_name: `ego/raw/2026-08-${String(index + 1).padStart(2, '0')}`,
        task_label_id: 3,
        status: index % 3 === 0 ? 'parsing' : 'succeeded',
        updated_at: `2026-08-${String(index + 1).padStart(2, '0')}T02:58:00Z`,
        available_actions: [],
      },
    ];
  });
  // Keep the review batches first so the review view opens the package list head.
  batches.unshift(...reviewBatches);
  const cutSegments = [
    { id: 'cut-1', start_ns: '0', end_ns: '35000000000', eligibility: 'included', boundary_after: { origin: 'qr_event', suggested_timestamp_ns: '33000000000', adjusted: true, segment_id_hint: 'qr-001', segment_index: 1, protocol_version: 1 } },
    { id: 'cut-2', start_ns: '35000000000', end_ns: '75000000000', eligibility: 'included', boundary_after: { origin: 'human' } },
    { id: 'cut-3', start_ns: '75000000000', end_ns: '125000000000', eligibility: 'excluded', exclusion_reason: 'idle_or_setup' },
  ];
  const annotationSegments = [
    { id: 'ann-1', start_ns: '5000000000', end_ns: '30000000000', description: '拿起桌面的水杯' },
    { id: 'ann-2', start_ns: '42000000000', end_ns: '82000000000', description: '将水杯平稳放入托盘' },
  ];
  const managedUsers = [
    { id: 9000, email: 'demo@local.preview', nickname: '本地演示', role: 'admin', is_active: true, created_at: '2026-08-01T08:00:00Z' },
    { id: 9001, email: 'operator@quicdata.local', nickname: '数据运维', role: 'operator', is_active: true, created_at: '2026-07-18T08:00:00Z' },
    { id: 9002, email: 'annotator@quicdata.local', nickname: '标注员', role: 'annotator', is_active: true, created_at: '2026-07-21T08:00:00Z' },
    { id: 9003, email: 'reviewer@quicdata.local', nickname: '审核员', role: 'auditor', is_active: true, created_at: '2026-07-22T08:00:00Z' },
    { id: 9004, email: 'viewer@quicdata.local', nickname: '访客', role: 'viewer', is_active: false, created_at: '2026-07-26T08:00:00Z' },
  ];
  const roles = {
    roles: {
      admin: { label: '管理员' },
      operator: { label: '数据运维' },
      annotator: { label: '标注员' },
      auditor: { label: '审核员' },
      viewer: { label: '查看者' },
    },
  };
  const workspaceMembers = [
    { user_id: 9000, email: 'demo@local.preview', role: 'admin', access_level: 'owner' },
    { user_id: 9001, email: 'operator@quicdata.local', role: 'operator', access_level: 'member' },
    { user_id: 9002, email: 'annotator@quicdata.local', role: 'annotator', access_level: 'member' },
  ];
  const platformSettings = {
    ai: {
      enabled: true,
      outbound_enabled: false,
      signed_url_delivery_enabled: true,
      public_endpoint: 'https://oss.example.local',
      signed_url_ttl_seconds: 900,
      provider_user_id: 'local-demo-user',
      provider_device_uuid: 'local-demo-device',
      annotation_version: 'ego-v2',
      api_key_configured: true,
      app_id_configured: true,
      revision: 3,
      source: 'database',
    },
    oss_import_scopes: [
      { id: 301, workspace_id: 1, task_set_id: 11, bucket: 'quicdata-ego-demo', prefixes: ['raw/ego/', 'imports/verified/'], is_enabled: true, revision: 2 },
    ],
  };
  const datasets = [
    {
      id: 701,
      workspace_id: 1,
      name: '桌面整理训练集',
      description: '用于验证层级选择、预览和双格式导出的本地演示数据集。',
      revision_count: 1,
      created_at: '2026-08-10T08:00:00Z',
    },
  ];
  const datasetRevisions = [
    {
      id: 801,
      dataset_id: 701,
      workspace_id: 1,
      name: '桌面整理训练集',
      version: 1,
      status: 'active',
      episode_count: 3,
      items: [],
      exports: {},
    },
  ];
  const datasetCandidates = {
    dataset_id: 701,
    workspace_id: 1,
    task_set_id: 12,
    summary: { candidate_count: 5, source_count: 2, duration_s: 250 },
    groups: [
      {
        source: { id: 4200, episode_uid: 'EGO-DEMO-0042', modality: 'ego', duration_s: 125, timeline_available: true, timeline_start_ns: '0', timeline_end_ns: '125000000000', created_at: '2026-08-11T02:30:00Z' },
        candidates: [
          { episode_id: 4200, episode_uid: 'EGO-DEMO-0042', candidate_kind: 'full_source', kind: 'source', modality: 'ego', source_start_ns: '0', source_end_ns: '125000000000', duration_s: 125, task_label: episode.task_label, collector: { id: 101, display_label: '张明 #0007' }, device: { id: 201, name: 'EGO 采集背包 A', model: 'EGO Pack' }, preview_available: true, created_at: '2026-08-11T02:30:00Z' },
          { episode_id: 4201, episode_uid: 'DRV-DEMO-0042-A', candidate_kind: 'derived', kind: 'derived', modality: 'ego', source_start_ns: '5000000000', source_end_ns: '35000000000', duration_s: 30, task_label: episode.task_label, collector: { id: 101, display_label: '张明 #0007' }, device: { id: 201, name: 'EGO 采集背包 A', model: 'EGO Pack' }, preview_available: true, created_at: '2026-08-11T03:00:00Z' },
          { episode_id: 4202, episode_uid: 'DRV-DEMO-0042-B', candidate_kind: 'derived', kind: 'derived', modality: 'ego', source_start_ns: '42000000000', source_end_ns: '82000000000', duration_s: 40, task_label: episode.task_label, collector: { id: 101, display_label: '张明 #0007' }, device: { id: 201, name: 'EGO 采集背包 A', model: 'EGO Pack' }, preview_available: true, created_at: '2026-08-11T03:02:00Z' },
        ],
      },
      {
        source: { id: 4300, episode_uid: 'EGO-DEMO-0043', modality: 'ego', duration_s: 80, timeline_available: true, timeline_start_ns: '0', timeline_end_ns: '80000000000', created_at: '2026-08-10T04:20:00Z' },
        candidates: [
          { episode_id: 4300, episode_uid: 'EGO-DEMO-0043', candidate_kind: 'full_source', kind: 'source', modality: 'ego', source_start_ns: '0', source_end_ns: '80000000000', duration_s: 80, task_label: episode.task_label, collector: { id: 102, display_label: '李芳 #0008' }, device: { id: 202, name: 'EGO 采集背包 B', model: 'EGO Pack' }, preview_available: true, created_at: '2026-08-10T04:20:00Z' },
          { episode_id: 4301, episode_uid: 'DRV-DEMO-0043-A', candidate_kind: 'derived', kind: 'derived', modality: 'ego', source_start_ns: '10000000000', source_end_ns: '35000000000', duration_s: 25, task_label: episode.task_label, collector: { id: 102, display_label: '李芳 #0008' }, device: { id: 202, name: 'EGO 采集背包 B', model: 'EGO Pack' }, preview_available: true, created_at: '2026-08-10T04:32:00Z' },
        ],
      },
    ],
  };
  const dashboardOverview = {
    kpis: { total_episodes: 48, total_duration_s: 12640, collected_today: 7, annotation_completed: 29, annotation_completion_rate: 0.6042, qrdf_baseline_count: 48, deltas: { total_episodes: 0.12, total_duration_s: 0.08, collected_today: 0.4, annotation_completed: 0.16, annotation_completion_rate: 0.05 } },
    pipeline_funnel: { avg_duration_s: 263.3, stages: [
      { key: 'intake', label: '数据接入', buckets: { lt_30s: 2, bt_30_60s: 4, gt_60s: 2, unknown: 0 } },
      { key: 'collected', label: '采集数据', buckets: { lt_30s: 3, bt_30_60s: 5, gt_60s: 6, unknown: 0 } },
      { key: 'separated', label: '分离数据', buckets: { lt_30s: 2, bt_30_60s: 4, gt_60s: 6, unknown: 0 } },
      { key: 'annotated', label: '标注数据', buckets: { lt_30s: 2, bt_30_60s: 3, gt_60s: 1, unknown: 0 } },
      { key: 'stored', label: '入库数据', buckets: { lt_30s: 1, bt_30_60s: 2, gt_60s: 5, unknown: 0 } },
    ] },
    today_queues: [
      { key: 'intake', label: '待采集', count: 8, share: 0.1667, pending: 8 },
      { key: 'collected', label: '待处理', count: 14, share: 0.2917, pending: 14 },
      { key: 'separated', label: '待标注', count: 12, share: 0.25, pending: 12 },
      { key: 'annotated', label: '待标注审核', count: 6, share: 0.125, pending: 6 },
      { key: 'stored', label: '待入库', count: 8, share: 0.1667, pending: 8 },
    ],
    collect_trend_7d: ['08-07', '08-08', '08-09', '08-10', '08-11', '08-12', '08-13'].map((date, index) => ({ date: `2026-${date}`, count: [3, 6, 5, 8, 11, 8, 7][index] })),
    device_distribution: { total: 48, items: [{ device_id: 201, name: 'EGO 采集背包 A', count: 28, share: 0.5833 }, { device_id: 202, name: 'EGO 采集背包 B', count: 20, share: 0.4167 }] },
    computed_at: '2026-08-13T08:30:00Z',
    etl_job_id: 'demo-dashboard-etl',
    source: 'live',
    scope: { key: 'task-set:12', workspace_id: 1, task_set_id: 12 },
  };
  const common = {
    episode,
    timeline,
    media: {
      preview: {
        available: true,
        url: demoPreviewUrl,
        playback_timeline: playbackTimeline,
      },
    },
  };
  // Annotation work is performed on derived clips, cut work on the source episode.
  const derivedClip = {
    id: 4340,
    episode_uid: 'DRV-DEMO-0042-A',
    workspace_id: 1,
    parent_episode_id: 4200,
    kind: 'derived',
    modality: 'ego',
    task_label: episode.task_label,
    quality: { status: 'passed' },
    human_stage: { status: 'in_progress' },
    review: { status: 'pending' },
    publication: { status: 'not_started' },
    metrics: { reference_frame_count: 62, duration_s: 62 },
  };
  const derivedClipTwo = {
    ...derivedClip,
    id: 4341,
    episode_uid: 'DRV-DEMO-0042-B',
    metrics: { reference_frame_count: 54, duration_s: 53 },
  };
  const queueRow = (episode, workItem, metrics) => ({
    episode,
    quality: episode.quality,
    preview: { status: 'ready' },
    metrics: metrics || episode.metrics || { reference_frame_count: 126, duration_s: 125 },
    work_item: workItem,
  });
  // Attribution mirrors the online-verified projection used by the review panel.
  const demoAttribution = {
    collector_attribution: { collector: { id: 101, name: '张明', workspace_sequence: 7 }, source: 'online_verified' },
    device_attribution: { device: { id: 201, name: 'EGO 采集背包 A', serial_number: 'EGO-A-008', software_number: 'QV-2.6.0' }, source: 'online_verified' },
  };
  const queueCases = {
    cut: [
      queueRow(episode, { id: 9001, kind: 'cut', status: 'in_progress', available_actions: ['save_draft', 'submit', 'release'] }),
      queueRow({ ...episode, id: 4201, episode_uid: 'EGO-DEMO-0043' }, { id: 9005, kind: 'cut', status: 'assigned', available_actions: ['continue', 'release'] }),
    ],
    annotation: [
      queueRow(derivedClip, { id: 9002, kind: 'annotation', status: 'in_progress', available_actions: ['save_draft', 'submit', 'release'] }),
      queueRow(derivedClipTwo, { id: 9006, kind: 'annotation', status: 'assigned', available_actions: ['continue', 'release'] }),
    ],
    review: [
      queueRow(derivedClip, { id: 9003, kind: 'review', status: 'in_progress', available_actions: ['review'] }),
      queueRow(derivedClipTwo, { id: 9007, kind: 'review', status: 'assigned', available_actions: ['continue', 'release'] }),
    ],
    completed: [queueRow({ ...episode, human_stage: { status: 'accepted' }, review: { status: 'accepted' }, publication: { status: 'published' } }, { id: 9004, kind: 'annotation', status: 'completed', available_actions: [] }, undefined)],
  };

  function workbench(workItemId) {
    const target = Number(workItemId);
    const item = Object.values(queueCases).flat().find((row) => Number(row.work_item.id) === target)?.work_item;
    const kind = item?.kind || 'cut';
    if (kind === 'annotation') {
      return {
        ...common,
        episode: { ...common.episode, ...demoAttribution },
        work_item: { id: target, kind: 'annotation', status: 'in_progress', available_actions: [], version: 2 },
        capabilities: { annotation: true },
        draft: { version: 2, payload: { mode: 'partitioned', segments: annotationSegments, outcome: 'success', rating: 4, collector_profile_id: 101, collection_device_id: 201, note: '动作完整，画面稳定。' } },
      };
    }
    if (kind === 'review') {
      return {
        ...common,
        episode: { ...common.episode, ...demoAttribution },
        work_item: { id: target, kind: 'review', status: 'in_progress', available_actions: [], version: 3 },
        capabilities: { review: true },
        draft: { version: 3, payload: { mode: 'partitioned', segments: [] } },
        review_target: { kind: 'annotation', work_item_id: 9002, payload: { mode: 'partitioned', segments: annotationSegments, outcome: 'success', rating: 4, collector_profile_id: 101, collection_device_id: 201, note: '动作完整，画面稳定。' } },
      };
    }
    return {
      ...common,
      episode: { ...common.episode, ...demoAttribution },
      work_item: { id: target, kind: 'cut', status: 'in_progress', available_actions: [], version: 1 },
      capabilities: { cut: true },
      draft: { version: 1, payload: { mode: 'partitioned', segments: cutSegments, note: '二维码节点已人工复核。' } },
    };
  }

  function readCore(path, params = {}) {
    if (path === '/workspace/options') return { list: [{ id: 1, name: 'EGO 数据工作空间' }] };
    if (path === '/workspace/task-set/options') return { list: [{ id: 11, name: '厨房物体摆放' }, { id: 12, name: '桌面物体整理' }] };
    if (path === '/auth/users') return { items: managedUsers };
    if (path === '/auth/roles') return roles;
    if (path === '/workspace/1/members') return { list: workspaceMembers };
    if (path === '/platform-settings') return platformSettings;
    if (path === '/dashboard/overview') return dashboardOverview;
    if (path === '/datasets') return { items: datasets, total: datasets.length, limit: 100, offset: 0 };
    if (path === '/datasets/701') return datasets[0];
    if (path === '/datasets/701/revisions') return { items: datasetRevisions };
    if (path === '/datasets/701/revision-candidates') return datasetCandidates;
    if (path === '/batches') return { items: batches, total: batches.length };
    if (/^\/batches\/\d+$/.test(path)) return batches.find((item) => String(item.id) === path.split('/')[2]) || {};
    if (/^\/batches\/\d+\/imports$/.test(path)) {
      const batchId = Number(path.split('/')[2]);
      return { items: importSessions[batchId] || [], total: (importSessions[batchId] || []).length };
    }
    if (path === '/imports/capabilities') return {
      max_upload_bytes: 107374182400,
      max_api_upload_bytes: 268435456,
      max_chunks: 20000,
      max_chunk_bytes: 8388608,
      direct_upload: { enabled: true, threshold_bytes: 268435456, part_bytes: 67108864, max_parts: 10000, concurrency: 4 },
    };
    if (path === '/episodes') return { items: [episode], total: 1 };
    if (path === '/task-labels') return { items: [episode.task_label], total: 1 };
    if (path === '/work-queue') {
      const items = queueCases[params.stage] || queueCases.cut;
      return { items, total: items.length };
    }
    if (path === '/collector-profiles') return { items: [{ id: 101, workspace_sequence: 7, name: '张明', is_active: true }, { id: 102, workspace_sequence: 8, name: '李芳', is_active: true }] };
    if (path === '/collection-devices') return { items: [{ id: 201, name: 'EGO 采集背包 A', serial_number: 'EGO-A-008', software_number: 'QV-2.6.0', is_active: true }, { id: 202, name: 'EGO 采集背包 B', serial_number: 'EGO-B-012', software_number: 'QV-2.6.0', is_active: true }] };
    if (path === '/episodes/4200/workbench') return workbench(params.work_item_id);
    if (path === '/episodes/4200') return { ...episode, kind: 'source', modality: 'ego', metrics: { reference_frame_count: 126, duration_s: 125 }, artifacts: [] };
    if (path === '/episodes/4200/preview-url') return { available: true, url: demoPreviewUrl };
    if (/^\/episodes\/(4201|4202|4300|4301)\/preview-url$/.test(path)) return { available: true, url: demoPreviewUrl };
    if (path === '/episodes/4200/timeline') return timeline;
    if (path === '/episodes/4200/ai-suggestions') return { capability: { eligible: false, rgb_topics: [], default_rgb_topic: '' }, items: [] };
    return {};
  }

  /* ------------------------------------------------------------------ *
   * Extended projections: published assets, datasets, intake, governance
   * ------------------------------------------------------------------ */
  const taskLabels = [
    { id: 3, name: '拿起水杯并放入托盘' },
    { id: 4, name: '桌面物体归类' },
    { id: 5, name: '开关抽屉取放物品' },
  ];
  const collectorProfiles = [
    { id: 101, workspace_sequence: 7, name: '张明', phone_suffix: '4821', is_active: true, membership_id: 9001, user_id: 31 },
    { id: 102, workspace_sequence: 8, name: '李芳', phone_suffix: '7730', is_active: true, membership_id: 9002, user_id: 32 },
    { id: 103, workspace_sequence: 9, name: '王强', phone_suffix: '1129', is_active: true, membership_id: 9003, user_id: 33 },
    { id: 104, workspace_sequence: 10, name: '陈静', phone_suffix: '6602', is_active: false, membership_id: 9004, user_id: 34 },
  ];
  const collectionDevices = [
    { id: 201, workspace_sequence: 3, name: 'EGO 采集背包 A', serial_number: 'EGO-A-008', software_number: 'QV-2.6.0', model: { id: 1, name: 'EGO 背包 V2' }, is_active: true },
    { id: 202, workspace_sequence: 4, name: 'EGO 采集背包 B', serial_number: 'EGO-B-012', software_number: 'QV-2.6.0', model: { id: 1, name: 'EGO 背包 V2' }, is_active: true },
    { id: 203, workspace_sequence: 5, name: 'LeRobot 数采台 A', serial_number: 'LRT-A-004', software_number: 'QV-3.1.2', model: { id: 2, name: 'LeRobot SO-100' }, is_active: true },
  ];
  const collectionProjects = [
    { id: 11, workspace_id: 1, name: '桌面物体整理', status: 'active', created_at: '2026-07-02T02:00:00Z', task_count: 3, description: '厨房与桌面场景的抓放动作采集' },
    { id: 12, workspace_id: 1, name: '商超货架补货', status: 'active', created_at: '2026-07-18T05:30:00Z', task_count: 2, description: '货架取放与理货动作采集' },
    { id: 13, workspace_id: 1, name: '仓储分拣', status: 'archived', created_at: '2026-06-11T01:10:00Z', task_count: 1, description: '已归档的历史采集项目' },
  ];
  const collectionTasks = [
    { id: 101, workspace_id: 1, project_id: 11, name: '拿起水杯并放入托盘', owner: '张明', sop: '从桌面拿起水杯，平稳移动到右侧托盘后松手', target_episodes: 200, target_duration_hours: 12, status: 'active', created_at: '2026-07-03T02:20:00Z', tags: { purpose: '正式任务', scene: '家居 · 厨房', train: '预训练' } },
    { id: 102, workspace_id: 1, project_id: 11, name: '桌面物体归类', owner: '李芳', sop: '按颜色将物体分放入两个收纳盒', target_episodes: 150, target_duration_hours: 8, status: 'active', created_at: '2026-07-09T06:40:00Z', tags: { purpose: '正式任务', scene: '家居 · 客厅', train: '后训练' } },
    { id: 103, workspace_id: 1, project_id: 11, name: '开关抽屉取放物品', owner: '王强', sop: '打开抽屉取出目标物体后关闭抽屉', target_episodes: 120, target_duration_hours: 6, status: 'paused', created_at: '2026-07-21T08:05:00Z', tags: { purpose: '测试任务', scene: '家居 · 厨房', train: '其它' } },
    { id: 104, workspace_id: 1, project_id: 12, name: '货架补货取放', owner: '张明', sop: '从周转箱取出商品并摆放到货架指定层', target_episodes: 180, target_duration_hours: 10, status: 'active', created_at: '2026-07-19T03:00:00Z', tags: { purpose: '正式任务', scene: '商超 · 货架', train: '预训练' } },
  ];
  const dataPackages = [
    { id: 5101, workspace_id: 1, collection_task_id: 101, collection_project_id: 11, package_uid: 'pkg-desk-0001', seq: 1, status: 'intake_approved', target_duration_hours: '2.00', intake_valid_duration_hours: '1.86', governed_valid_duration_hours: '1.72', responsible_collector_id: 101, operator_collector_id: 101, device_id: 201, assigned_at: '2026-08-05T01:00:00Z', upload_completed_at: '2026-08-05T03:20:00Z', updated_at: '2026-08-11T03:20:00Z', intake_review: { verdict: 'approved', reviewer_user_id: 9000, reviewed_at: '2026-08-11T03:20:00Z', reason: '' } },
    { id: 5102, workspace_id: 1, collection_task_id: 101, collection_project_id: 11, package_uid: 'pkg-desk-0002', seq: 2, status: 'pending_intake_review', target_duration_hours: '2.00', intake_valid_duration_hours: '1.42', governed_valid_duration_hours: '1.30', responsible_collector_id: 102, operator_collector_id: 102, device_id: 202, assigned_at: '2026-08-06T01:00:00Z', upload_completed_at: '2026-08-06T04:45:00Z', updated_at: '2026-08-12T07:45:00Z' },
    { id: 5103, workspace_id: 1, collection_task_id: 102, collection_project_id: 11, package_uid: 'pkg-desk-0003', seq: 3, status: 'ingested', target_duration_hours: '2.50', intake_valid_duration_hours: '1.05', governed_valid_duration_hours: '0.98', responsible_collector_id: 103, operator_collector_id: 103, device_id: 203, assigned_at: '2026-08-09T02:30:00Z', upload_completed_at: '2026-08-09T05:10:00Z', updated_at: '2026-08-13T09:10:00Z' },
    { id: 5104, workspace_id: 1, collection_task_id: 104, collection_project_id: 12, package_uid: 'pkg-mart-0001', seq: 1, status: 'assigned', target_duration_hours: '3.00', intake_valid_duration_hours: '0.00', governed_valid_duration_hours: '0.00', responsible_collector_id: 101, operator_collector_id: 101, device_id: 201, assigned_at: '2026-08-14T00:40:00Z', upload_completed_at: null, updated_at: '2026-08-14T00:40:00Z' },
    { id: 5105, workspace_id: 1, collection_task_id: 104, collection_project_id: 12, package_uid: 'pkg-mart-0002', seq: 2, status: 'pending_intake_review', target_duration_hours: '3.00', intake_valid_duration_hours: '2.68', governed_valid_duration_hours: '2.51', responsible_collector_id: 102, operator_collector_id: 102, device_id: 202, assigned_at: '2026-08-15T01:20:00Z', upload_completed_at: '2026-08-15T06:05:00Z', updated_at: '2026-08-16T02:15:00Z' },
    { id: 5106, workspace_id: 1, collection_task_id: 103, collection_project_id: 11, package_uid: 'pkg-desk-0004', seq: 4, status: 'parse_failed', target_duration_hours: '2.00', intake_valid_duration_hours: '0.00', governed_valid_duration_hours: '0.00', responsible_collector_id: 103, operator_collector_id: 103, device_id: 203, assigned_at: '2026-08-17T02:00:00Z', upload_completed_at: '2026-08-17T04:30:00Z', parse_error_code: 'qrdf_manifest_missing', parse_error_message: '缺少 QRDF 清单', updated_at: '2026-08-17T04:35:00Z' },
    { id: 5107, workspace_id: 1, collection_task_id: 102, collection_project_id: 11, package_uid: 'pkg-desk-0005', seq: 5, status: 'batched', target_duration_hours: '2.00', intake_valid_duration_hours: '1.95', governed_valid_duration_hours: '1.88', responsible_collector_id: 101, operator_collector_id: 101, device_id: 201, assigned_at: '2026-08-18T01:10:00Z', upload_completed_at: '2026-08-18T05:40:00Z', updated_at: '2026-08-19T01:05:00Z' },
    { id: 5108, workspace_id: 1, collection_task_id: 101, collection_project_id: 11, package_uid: 'pkg-desk-0006', seq: 6, status: 'pending_assignment', target_duration_hours: '1.50', intake_valid_duration_hours: '0.00', governed_valid_duration_hours: '0.00', responsible_collector_id: null, operator_collector_id: null, device_id: null, assigned_at: null, upload_completed_at: null, updated_at: '2026-08-19T02:00:00Z' },
  ];
  const apiTokens = [
    { id: 9001, name: 'duance-运维机', key_id: 'k8f2x9', expires_at: null, rotated_at: null, last_used_at: '2026-09-21T08:00:00Z', revoked_at: null, created_at: '2026-06-15T02:00:00Z' },
  ];
  const collectionLabels = [
    { id: 301, workspace_id: 1, category: 'purpose', name: '正式任务', is_active: true, color: '#409eff' },
    { id: 302, workspace_id: 1, category: 'purpose', name: '测试任务', is_active: true, color: '#909399' },
    { id: 303, workspace_id: 1, category: 'scene', name: '家居 · 厨房', is_active: true, color: '#67c23a' },
    { id: 304, workspace_id: 1, category: 'scene', name: '家居 · 客厅', is_active: true, color: '#67c23a' },
    { id: 305, workspace_id: 1, category: 'scene', name: '商超 · 货架', is_active: true, color: '#e6a23c' },
    { id: 306, workspace_id: 1, category: 'train', name: '预训练', is_active: true, color: '#f56c6c' },
    { id: 307, workspace_id: 1, category: 'train', name: '后训练', is_active: true, color: '#f56c6c' },
    { id: 308, workspace_id: 1, category: 'modality', name: 'ego', is_active: true, color: '#909399' },
    { id: 309, workspace_id: 1, category: 'modality', name: 'lerobot', is_active: true, color: '#909399' },
  ];
  const publishedEpisodes = [
    { id: 4300, episode_uid: 'EGO-20260811-0007', workspace_id: 1, source_workspace_id: 1, kind: 'source', modality: 'ego', task_label: taskLabels[0], quality: { status: 'passed' }, review: { status: 'accepted' }, publication: { status: 'published' }, published_at: '2026-08-12T02:15:00Z', created_at: '2026-08-11T06:20:00Z', metrics: { reference_frame_count: 126, duration_s: 125 }, attribution: { collector: { collector: collectorProfiles[0] }, device: { device: collectionDevices[0] } }, asset_has_children: true, asset_group_start_ns: '0', source_start_ns: '0', source_end_ns: '125000000000' },
    { id: 4301, episode_uid: 'EGO-20260811-0008', workspace_id: 1, source_workspace_id: 1, kind: 'source', modality: 'ego', task_label: taskLabels[1], quality: { status: 'passed' }, review: { status: 'accepted' }, publication: { status: 'published' }, published_at: '2026-08-12T02:40:00Z', created_at: '2026-08-11T07:05:00Z', metrics: { reference_frame_count: 98, duration_s: 96 }, attribution: { collector: { collector: collectorProfiles[1] }, device: { device: collectionDevices[1] } }, asset_has_children: false, asset_group_start_ns: '0', source_start_ns: '0', source_end_ns: '96000000000' },
    { id: 4302, episode_uid: 'EGO-20260813-0011', workspace_id: 1, source_workspace_id: 1, kind: 'source', modality: 'ego', task_label: taskLabels[0], quality: { status: 'passed' }, review: { status: 'accepted' }, publication: { status: 'published' }, published_at: '2026-08-14T01:05:00Z', created_at: '2026-08-13T03:30:00Z', metrics: { reference_frame_count: 142, duration_s: 140 }, attribution: { collector: { collector: collectorProfiles[2] }, device: { device: collectionDevices[2] } }, asset_has_children: true, asset_group_start_ns: '0', source_start_ns: '0', source_end_ns: '140000000000' },
    { id: 4310, episode_uid: 'DRV-20260812-0001', workspace_id: 1, source_workspace_id: 1, kind: 'derived', modality: 'ego', task_label: taskLabels[0], quality: { status: 'passed' }, review: { status: 'accepted' }, publication: { status: 'published' }, published_at: '2026-08-13T05:20:00Z', created_at: '2026-08-12T05:00:00Z', metrics: { reference_frame_count: 63, duration_s: 62 }, attribution: { collector: { collector: collectorProfiles[0] }, device: { device: collectionDevices[0] } }, parent_episode_uid: 'EGO-20260811-0007', asset_has_children: false, asset_group_start_ns: '0', source_start_ns: '0', source_end_ns: '62000000000' },
    { id: 4311, episode_uid: 'DRV-20260813-0004', workspace_id: 1, source_workspace_id: 1, kind: 'derived', modality: 'ego', task_label: taskLabels[1], quality: { status: 'passed' }, review: { status: 'accepted' }, publication: { status: 'published' }, published_at: '2026-08-14T06:10:00Z', created_at: '2026-08-13T06:00:00Z', metrics: { reference_frame_count: 54, duration_s: 53 }, attribution: { collector: { collector: collectorProfiles[1] }, device: { device: collectionDevices[1] } }, parent_episode_uid: 'EGO-20260811-0008', asset_has_children: false, asset_group_start_ns: '0', source_start_ns: '0', source_end_ns: '53000000000' },
    { id: 4320, episode_uid: 'LRT-20260815-0002', workspace_id: 1, source_workspace_id: 1, kind: 'source', modality: 'lerobot', task_label: taskLabels[2], quality: { status: 'passed' }, review: { status: 'accepted' }, publication: { status: 'published' }, published_at: '2026-08-16T03:00:00Z', created_at: '2026-08-15T02:00:00Z', metrics: { reference_frame_count: 210, duration_s: 208 }, attribution: { collector: { collector: collectorProfiles[2] }, device: { device: collectionDevices[2] } }, asset_has_children: true, asset_group_start_ns: '0', source_start_ns: '0', source_end_ns: '208000000000' },
  ];
  const qrdfDatasets = [
    { id: 701, workspace_id: 1, name: 'desk-grasp-ego', type: 'qrdf', status: 'ready', episode_count: 186, duration_hours: 6.4, revision_count: 3, latest_revision: 'v3', created_at: '2026-08-20T06:10:00Z', updated_at: '2026-08-20T06:10:00Z' },
    { id: 702, workspace_id: 1, name: 'tray-sorting-ego', type: 'qrdf', status: 'building', episode_count: 96, duration_hours: 3.1, revision_count: 1, latest_revision: 'v1', created_at: '2026-08-16T02:00:00Z', updated_at: '2026-08-21T01:30:00Z' },
    { id: 703, workspace_id: 1, name: 'desk-grasp-lerobot', type: 'native_lerobot', status: 'ready', episode_count: 142, duration_hours: 4.9, revision_count: 2, latest_revision: 'v2', created_at: '2026-08-18T02:40:00Z', updated_at: '2026-08-18T02:40:00Z' },
  ];
  const datasetRevisionMap = {
    701: [
      { id: 9001, dataset_id: 701, version: 3, status: 'ready', episode_count: 186, size_bytes: 48103633715, created_at: '2026-08-20T06:10:00Z', filters: { scene: ['家居 · 厨房'], train: ['预训练'] } },
      { id: 9002, dataset_id: 701, version: 2, status: 'ready', episode_count: 154, size_bytes: 39728447488, created_at: '2026-08-15T03:20:00Z', filters: { scene: ['家居 · 厨房'] } },
      { id: 9003, dataset_id: 701, version: 1, status: 'superseded', episode_count: 96, size_bytes: 24696061952, created_at: '2026-08-09T01:05:00Z', filters: {} },
    ],
    702: [
      { id: 9004, dataset_id: 702, version: 1, status: 'building', episode_count: 96, size_bytes: 17179869184, created_at: '2026-08-21T01:30:00Z', filters: {} },
    ],
    703: [
      { id: 9005, dataset_id: 703, version: 2, status: 'ready', episode_count: 142, size_bytes: 34359738368, created_at: '2026-08-18T02:40:00Z', filters: {} },
      { id: 9006, dataset_id: 703, version: 1, status: 'superseded', episode_count: 88, size_bytes: 20384317440, created_at: '2026-08-12T04:00:00Z', filters: {} },
    ],
  };
  const datasetCandidateMap = {
    701: { items: [
      { id: 4301, episode_uid: 'EGO-20260811-0008', task_label: taskLabels[1], duration_s: 96, quality: { status: 'passed' }, published_at: '2026-08-12T02:40:00Z' },
      { id: 4302, episode_uid: 'EGO-20260813-0011', task_label: taskLabels[0], duration_s: 140, quality: { status: 'passed' }, published_at: '2026-08-14T01:05:00Z' },
      { id: 4320, episode_uid: 'LRT-20260815-0002', task_label: taskLabels[2], duration_s: 208, quality: { status: 'passed' }, published_at: '2026-08-16T03:00:00Z' },
    ], total: 3 },
  };
  const datasetExports = [
    { id: 8801, revision_id: 9001, format: 'lerobot_3_0', status: 'succeeded', size_bytes: 47103013683, created_at: '2026-08-20T07:00:00Z', finished_at: '2026-08-20T07:26:00Z', download_url: 'https://vjs.zencdn.net/v/oceans.mp4' },
    { id: 8802, revision_id: 9002, format: 'qrdf_0_2', status: 'running', size_bytes: 0, created_at: '2026-08-21T02:10:00Z', finished_at: null, progress: 0.44 },
    { id: 8803, revision_id: 9003, format: 'lerobot_3_0', status: 'failed', size_bytes: 0, created_at: '2026-08-19T08:00:00Z', finished_at: '2026-08-19T08:03:00Z', failure_reason: '对象存储写入超时' },
  ];
  const nativeLerobotDatasets = [
    { id: 601, workspace_id: 1, batch_id: 501, dataset_id: 'lerobot/desk_grasp_v2', robot_type: 'so100', file_count: 428, total_size: 34359738368, copy_status: 'succeeded', updated_at: '2026-08-18T02:40:00Z', oss_uri: 'oss://quicstudio-datasets/lerobot/desk_grasp_v2' },
    { id: 602, workspace_id: 1, batch_id: 502, dataset_id: 'lerobot/tray_sort_v1', robot_type: 'so100', file_count: 312, total_size: 25769803776, copy_status: 'copying', updated_at: '2026-08-21T01:20:00Z', oss_uri: 'oss://quicstudio-datasets/lerobot/tray_sort_v1' },
    { id: 603, workspace_id: 1, batch_id: 501, dataset_id: 'lerobot/kitchen_pick_v3', robot_type: 'koch', file_count: 265, total_size: 21474836480, copy_status: 'failed', updated_at: '2026-08-19T05:10:00Z', oss_uri: 'oss://quicstudio-datasets/lerobot/kitchen_pick_v3', failure_reason: '源站签名过期' },
  ];
  const nativeLerobotBundles = [
    { id: 701, dataset_id: 601, status: 'ready', size_bytes: 34359738368, created_at: '2026-08-18T03:00:00Z', download_url: 'https://vjs.zencdn.net/v/oceans.mp4' },
    { id: 702, dataset_id: 602, status: 'building', size_bytes: 0, created_at: '2026-08-21T01:30:00Z' },
  ];
  const candidateBusinessInfo = [
    { project: '桌面物体整理', task: '拿起水杯并放入托盘', owner: '张明', purpose: '正式任务', scene: '家居 · 厨房', train: '预训练', collector: '张明', device: 0 },
    { project: '桌面物体整理', task: '桌面物体归类', owner: '李芳', purpose: '正式任务', scene: '家居 · 客厅', train: '后训练', collector: '李芳', device: 1 },
    { project: '桌面物体整理', task: '拿起水杯并放入托盘', owner: '张明', purpose: '正式任务', scene: '家居 · 厨房', train: '预训练', collector: '王强', device: 2 },
    { project: '桌面物体整理', task: '拿起水杯并放入托盘', owner: '张明', purpose: '测试任务', scene: '家居 · 厨房', train: '其它', collector: '张明', device: 0 },
    { project: '桌面物体整理', task: '桌面物体归类', owner: '李芳', purpose: '测试任务', scene: '家居 · 客厅', train: '后训练', collector: '李芳', device: 1 },
    { project: '商超货架补货', task: '开关抽屉取放物品', owner: '王强', purpose: '正式任务', scene: '商超 · 货架', train: '预训练', collector: '王强', device: 2 },
  ];
  const dataBatchCandidates = publishedEpisodes.map((episode, index) => {
    const info = candidateBusinessInfo[index % candidateBusinessInfo.length];
    const device = collectionDevices[info.device] || collectionDevices[0];
    const durationS = Number(episode.metrics?.duration_s || 0);
    return {
      id: episode.id,
      package_uid: episode.episode_uid,
      episode_uid: episode.episode_uid,
      name: episode.episode_uid,
      intake_valid_duration_hours: (durationS / 3600).toFixed(2),
      governed_valid_duration_hours: ((durationS / 3600) * 0.92).toFixed(2),
      target_duration_hours: '2.00',
      status: episode.kind === 'derived' ? 'approved' : 'pending_review',
      collection_project_id: 11,
      collection_task_id: 101,
      workspace_id: 1,
      updated_at: episode.published_at,
      project_name: info.project,
      task_name: info.task,
      owner: info.owner,
      purpose: info.purpose,
      scene: info.scene,
      train: info.train,
      channel: 'data_package',
      modality: episode.modality,
      collector_name: info.collector,
      device: { name: device.name, serial: device.serial_number, version: device.software_number },
      reference_frame_count: episode.metrics?.reference_frame_count || 0,
      total_frames: episode.metrics?.reference_frame_count || 0,
      annotation_status: episode.kind === 'derived' ? '已完成' : '待标注',
      stage_snapshot_json: { integrity: true, quality: true, compliance: episode.kind !== 'derived' },
    };
  });
  const dataBatches = [
    { id: 6601, workspace_id: 1, name: 'desk-grasp-batch-0820', status: 'ready', created_at: '2026-08-20T06:05:00Z', updated_at: '2026-08-20T06:30:00Z', data_package_ids: [5101, 5102], integrity_check_enabled: true, quality_check_enabled: true, compliance_check_enabled: true, data_asset_count: 4, governed_valid_duration_hours: 3.02 },
    { id: 6602, workspace_id: 1, name: 'tray-sorting-batch-0821', status: 'building', created_at: '2026-08-21T01:10:00Z', updated_at: '2026-08-21T01:40:00Z', data_package_ids: [5103], integrity_check_enabled: true, quality_check_enabled: true, compliance_check_enabled: false, data_asset_count: 2, governed_valid_duration_hours: 0.98 },
  ];
  const annotationWorkItems = [
    { id: 7001, workspace_id: 1, data_batch_id: 6601, assignee_user_id: 31, status: 'in_progress', updated_at: '2026-08-20T08:20:00Z', episode_uid: 'EGO-20260811-0007', task_label: taskLabels[0], return_reason: null },
    { id: 7002, workspace_id: 1, data_batch_id: 6601, assignee_user_id: 32, status: 'submitted', updated_at: '2026-08-20T09:05:00Z', episode_uid: 'EGO-20260811-0008', task_label: taskLabels[1], return_reason: null },
    { id: 7003, workspace_id: 1, data_batch_id: 6602, assignee_user_id: 33, status: 'returned', updated_at: '2026-08-21T02:15:00Z', episode_uid: 'EGO-20260813-0011', task_label: taskLabels[0], return_reason: '动作边界不准确，请重新切分' },
  ];
  const reviewWorkItems = [
    { id: 7101, workspace_id: 1, data_batch_id: 6601, assignee_user_id: 34, status: 'pending_review', updated_at: '2026-08-20T09:10:00Z', episode_uid: 'EGO-20260811-0008', task_label: taskLabels[1], reason: null,
      source: { kind: 'algorithm', name: 'ego-vl', version: '1.3.0', run_id: 'run-001', confidence: 0.82 } },
    { id: 7102, workspace_id: 1, data_batch_id: 6602, assignee_user_id: 31, status: 'approved', updated_at: '2026-08-21T02:40:00Z', episode_uid: 'DRV-20260812-0001', task_label: taskLabels[0], reason: null },
    { id: 7103, workspace_id: 1, data_batch_id: 6601, assignee_user_id: 32, status: 'returned', updated_at: '2026-08-21T03:05:00Z', episode_uid: 'DRV-20260813-0004', task_label: taskLabels[1], reason: '标注标签与 SOP 不一致' },
  ];
  const dataAssets = [
    { id: 7501, data_batch_id: 6601, workspace_id: 1, source_workspace_id: 1, governed_valid_duration_hours: 1.72, stage_snapshot_json: { integrity: true, quality: true, compliance: true }, created_at: '2026-08-20T06:30:00Z', episode_uid: 'EGO-20260811-0007' },
    { id: 7502, data_batch_id: 6601, workspace_id: 1, source_workspace_id: 1, governed_valid_duration_hours: 1.30, stage_snapshot_json: { integrity: true, quality: true, compliance: false }, created_at: '2026-08-20T06:32:00Z', episode_uid: 'EGO-20260811-0008' },
    { id: 7503, data_batch_id: 6602, workspace_id: 1, source_workspace_id: 1, governed_valid_duration_hours: 0.98, stage_snapshot_json: { integrity: true, quality: false, compliance: false }, created_at: '2026-08-21T01:40:00Z', episode_uid: 'EGO-20260813-0011' },
  ];
  const catalogDatasets = [
    { id: 8001, name: 'desk-grasp-ego-catalog', source_kind: 'data_batches', status: 'active', description: '桌面抓放动作，含治理与合规校验', created_at: '2026-08-20T07:00:00Z', latest_version: 2 },
    { id: 8002, name: 'tray-sorting-catalog', source_kind: 'data_batches', status: 'active', description: '托盘整理动作集合', created_at: '2026-08-21T02:00:00Z', latest_version: 1 },
  ];
  const catalogVersions = {
    8001: [
      { id: 8101, dataset_id: 8001, version: 2, status: 'ready', data_asset_ids: [7501, 7502], created_at: '2026-08-20T07:30:00Z' },
      { id: 8102, dataset_id: 8001, version: 1, status: 'superseded', data_asset_ids: [7501], created_at: '2026-08-19T03:00:00Z' },
    ],
    8002: [
      { id: 8103, dataset_id: 8002, version: 1, status: 'exporting', data_asset_ids: [7503], created_at: '2026-08-21T02:10:00Z' },
    ],
  };
  const collectionOverview = {
    workspace_id: 1,
    target_duration_hours: 36,
    intake_valid_duration_hours: 4.33,
    governed_valid_duration_hours: 3.98,
    task_count: collectionTasks.filter((task) => task.status !== 'archived').length,
    project_count: collectionProjects.filter((project) => project.status === 'active').length,
    package_count: dataPackages.length,
    pending_review_count: dataPackages.filter((item) => item.status === 'pending_review').length,
    active_collectors: collectorProfiles.filter((item) => item.is_active).length,
    device_count: collectionDevices.filter((item) => item.is_active).length,
    updated_at: '2026-08-21T04:00:00Z',
  };
  const importCandidates = {
    'demo-import-501-a': [
      { id: 'cand-501-1', name: 'ego_station_a_0811_part1.mcap', size_bytes: 5368709120, duration_s: 1284, status: 'imported', selected: true, episode_uid: 'EGO-20260811-0007' },
      { id: 'cand-501-2', name: 'ego_station_a_0811_part2.mcap', size_bytes: 4831838208, duration_s: 1156, status: 'pending', selected: false },
    ],
    'demo-import-503-a': [
      { id: 'cand-503-1', name: 'ego_station_b_0808_part1.mcap', size_bytes: 3221225472, duration_s: 902, status: 'pending', selected: false },
    ],
  };
  const batchActivities = {
    501: [
      { id: 1, kind: 'import_created', message: '创建导入会话 ego_station_a_0811.zip', created_at: '2026-08-11T03:00:00Z', actor: '张明' },
      { id: 2, kind: 'parse_succeeded', message: '解析完成，识别 2 个 MCAP 片段', created_at: '2026-08-11T03:12:00Z', actor: 'system' },
      { id: 3, kind: 'quality_started', message: '质检任务已提交（8/12）', created_at: '2026-08-11T04:20:00Z', actor: 'system' },
    ],
    502: [
      { id: 4, kind: 'import_created', message: 'OSS 直读导入完成', created_at: '2026-08-09T07:35:00Z', actor: '李芳' },
      { id: 5, kind: 'quality_passed', message: '全部 24 条片段质检通过', created_at: '2026-08-09T09:10:00Z', actor: 'system' },
    ],
    503: [
      { id: 6, kind: 'import_created', message: '创建分片上传会话', created_at: '2026-08-08T04:30:00Z', actor: '王强' },
      { id: 7, kind: 'upload_progress', message: '分片上传完成（48/48）', created_at: '2026-08-08T05:02:00Z', actor: '王强' },
    ],
  };
  // Review batches mirror the mining packages, so give each one an activity feed.
  reviewBatches.forEach((batch, index) => {
    batchActivities[batch.id] = [
      { id: 100 + index * 3, kind: 'import_created', message: `创建导入会话 ego_station_${batch.sequence_number}_part1.zip`, created_at: `2026-08-${String(index + 1).padStart(2, '0')}T03:00:00Z`, actor: '张明' },
      { id: 101 + index * 3, kind: 'parse_succeeded', message: '解析完成，识别 2 个 MCAP 片段', created_at: `2026-08-${String(index + 1).padStart(2, '0')}T03:12:00Z`, actor: 'system' },
      { id: 102 + index * 3, kind: 'quality_started', message: '质检任务已提交', created_at: `2026-08-${String(index + 1).padStart(2, '0')}T04:20:00Z`, actor: 'system' },
    ];
  });
  const importJobs = {
    7701: { id: 7701, kind: 'import', status: 'running', progress: 0.68, batch_id: 501, created_at: '2026-08-11T03:18:00Z', updated_at: '2026-08-11T03:40:00Z', message: '解析 MCAP 片段 12/18' },
    7702: { id: 7702, kind: 'quality', status: 'succeeded', progress: 1, batch_id: 502, created_at: '2026-08-09T08:00:00Z', updated_at: '2026-08-09T09:10:00Z', message: '质检完成，24/24 通过' },
    7703: { id: 7703, kind: 'export', status: 'failed', progress: 0.31, batch_id: 503, created_at: '2026-08-19T08:00:00Z', updated_at: '2026-08-19T08:03:00Z', message: '导出失败：对象存储写入超时' },
  };

  function episodeDetailById(rawId) {
    const id = Number(rawId);
    const queued = Object.values(queueCases)
      .flat()
      .map((row) => row.episode)
      .find((item) => Number(item.id) === id);
    if (queued) {
      return {
        ...queued,
        ...demoAttribution,
        artifacts: [
          { type: 'mcap', role: 'processed', size: 1073741824, retention: 'keep', until: null },
          { type: 'mp4', role: 'preview', size: 134217728, retention: 'keep', until: null },
        ],
        multimodal: { session_count: 2, topics: ['/camera/front/image_raw', '/joint_states'] },
      };
    }
    const asset = publishedEpisodes.find((item) => Number(item.id) === id);
    if (asset) {
      return {
        ...asset,
        artifacts: [
          { type: 'mcap', role: 'processed', size: 5368709120, retention: 'keep', until: null },
          { type: 'mp4', role: 'preview', size: 268435456, retention: 'keep', until: null },
        ],
        multimodal: { session_count: 2, topics: ['/camera/front/image_raw', '/joint_states'] },
      };
    }
    return {
      ...episode,
      id: Number.isFinite(id) ? id : episode.id,
      artifacts: [],
      kind: 'source',
      modality: 'ego',
      metrics: { reference_frame_count: 126, duration_s: 125 },
    };
  }

  function readExtended(path, params = {}) {
    const enrichDataPackage = (pkg) => {
      const project = collectionProjects.find((row) => Number(row.id) === Number(pkg.collection_project_id));
      const task = collectionTasks.find((row) => Number(row.id) === Number(pkg.collection_task_id));
      const collector = collectorProfiles.find((row) => Number(row.id) === Number(pkg.responsible_collector_id));
      const device = collectionDevices.find((row) => Number(row.id) === Number(pkg.collection_device_id ?? pkg.device_id));
      const taskLabels = collectionLabels.filter((label) => (
        task?.label_ids?.includes(label.id)
        || task?.tags?.[label.category === 'training' ? 'train' : label.category] === label.name
      ));
      return {
        ...pkg,
        collection_project_name: project?.name || '',
        collection_task_name: task?.name || '',
        // Keep the demo projection aligned with the production
        // /data-packages response consumed by the data overview table.
        project_name: project?.name || '',
        task_name: task?.name || '',
        responsible_collector_name: collector?.name || '',
        collector_name: collector?.name || '',
        collection_device_name: device?.name || '',
        device_name: device?.name || '',
        labels: taskLabels,
      };
    };
    // Published data assets -------------------------------------------------
    const episodeProjectId = (item) => Number(item.collection_project_id) || 11;
    const matchesEpisodeScope = (item) => (
      (!params.collection_project_id || episodeProjectId(item) === Number(params.collection_project_id))
      && (!params.task_set_id || Number(item.task_set_id) === Number(params.task_set_id))
    );
    if (path === '/episodes') {
      const items = [
        { ...episode, kind: 'source', modality: 'ego', metrics: { reference_frame_count: 126, duration_s: 125 }, published_at: null },
        ...publishedEpisodes.map((item) => ({ ...item, collection_project_id: Number(item.id) === 4320 ? 12 : 11 })),
      ].filter(matchesEpisodeScope);
      return { items, total: items.length, limit: 100, offset: 0, page: 1, page_size: items.length };
    }
    if (path === '/episodes/assets') {
      const items = publishedEpisodes
        .map((item) => ({ ...item, collection_project_id: Number(item.id) === 4320 ? 12 : 11 }))
        .filter(matchesEpisodeScope);
      return { items, total: items.length, limit: 100, offset: 0, page: 1, page_size: items.length || 1 };
    }
    const episodeAssetMatch = path.match(/^\/episodes\/(\d+)\/assets$/);
    if (episodeAssetMatch) {
      const items = publishedEpisodes.filter((item) => Number(item.id) === Number(episodeAssetMatch[1]));
      return { items, total: items.length };
    }
    const rawSourceMatch = path.match(/^\/episodes\/(\d+)\/raw-source\/downloads$/);
    if (rawSourceMatch) {
      return {
        items: [
          { id: 1, type: 'mcap', role: 'raw', size: 5368709120, retention: 'keep', until: null, url: 'https://vjs.zencdn.net/v/oceans.mp4' },
          { id: 2, type: 'zip', role: 'raw', size: 2147483648, retention: '30d', until: '2026-09-20', url: 'https://vjs.zencdn.net/v/oceans.mp4' },
        ],
      };
    }
    const multimodalMatch = path.match(/^\/episodes\/(\d+)\/multimodal\/session$/);
    if (multimodalMatch) {
      return {
        session_id: `mms-${multimodalMatch[1]}`,
        status: 'ready',
        camera_topics: ['/camera/front/image_raw', '/camera/wrist/image_raw'],
        sensor_topics: ['/joint_states', '/imu/data'],
        reference_frame_count: 126,
        duration_s: 125,
      };
    }
    const episodeMatch = path.match(/^\/episodes\/(\d+)$/);
    if (episodeMatch && Number(episodeMatch[1]) !== 4200) return episodeDetailById(episodeMatch[1]);
    const workbenchMatch = path.match(/^\/episodes\/(\d+)\/workbench$/);
    if (workbenchMatch && Number(workbenchMatch[1]) !== 4200) {
      const base = workbench(params.work_item_id);
      return { ...base, episode: episodeDetailById(workbenchMatch[1]) };
    }
    const previewMatch = path.match(/^\/episodes\/(\d+)\/preview-url$/);
    if (previewMatch && Number(previewMatch[1]) !== 4200) return { available: true, url: demoPreviewUrl };
    const timelineMatch = path.match(/^\/episodes\/(\d+)\/timeline$/);
    if (timelineMatch && Number(timelineMatch[1]) !== 4200) {
      const detail = episodeDetailById(timelineMatch[1]);
      const durationS = Number(detail.metrics?.duration_s) || 125;
      const toNs = (seconds) => String(Math.round(seconds * 1000000000));
      return {
        start_ns: '0',
        end_ns: toNs(durationS),
        duration_s: durationS,
        reference_topic: '/camera/front/image_raw',
        boundary_suggestions: [
          { timestamp_ns: toNs(durationS * 0.3), segment_id_hint: 'qr-001', segment_index: 1, protocol_version: 1 },
          { timestamp_ns: toNs(durationS * 0.7), segment_id_hint: 'qr-002', segment_index: 2, protocol_version: 1 },
        ],
      };
    }
    const suggestionMatch = path.match(/^\/episodes\/(\d+)\/ai-suggestions$/);
    if (suggestionMatch && Number(suggestionMatch[1]) !== 4200) {
      return { capability: { eligible: false, rgb_topics: ['/camera/front/image_raw'], default_rgb_topic: '/camera/front/image_raw' }, items: [] };
    }

    // Collection intake -----------------------------------------------------
    if (path === '/task-labels') return { items: taskLabels, total: taskLabels.length };
    if (path === '/collector-profiles') {
      const items = collectorProfiles.filter((item) => params.include_inactive || item.is_active);
      return { items, total: items.length };
    }
    if (path === '/collection-devices') {
      const items = collectionDevices.filter((item) => params.include_inactive || item.is_active);
      return { items, total: items.length };
    }
    if (path === '/collection-overview') {
      const scoped = dataPackages.filter((pkg) => Number(pkg.workspace_id) === Number(params?.workspace_id || 1) && (!params?.collection_project_id || Number(pkg.collection_project_id) === Number(params.collection_project_id)));
      const counts = { pending_assignment: 0, assigned: 0, other: 0, voided: 0 };
      scoped.forEach((pkg) => { counts[['pending_assignment', 'assigned', 'voided'].includes(pkg.status) ? pkg.status : 'other'] += 1; });
      return { ...collectionOverview, workspace_id: Number(params?.workspace_id || 1), package_counts: counts };
    }
    if (path === '/collection-dashboard/data') {
      const end = params?.end_date || '2026-09-29';
      const start = params?.start_date || '2026-08-31';
      const buckets = [];
      for (let day = new Date(`${start}T00:00:00Z`); day <= new Date(`${end}T00:00:00Z`); day = new Date(day.getTime() + 86400000)) buckets.push(day.toISOString().slice(0, 10));
      const series = (scale) => buckets.map((bucket, index) => ({ bucket, value: Math.round(((index * 7) % 5) * scale) }));
      const payload = {
        workspace_id: Number(params?.workspace_id || 1),
        filters: { start_date: start, end_date: end, granularity: params?.granularity || 'day', basis: params?.basis || 'valid', tz: 'Asia/Shanghai', project_ids: [], task_ids: [], scene_label_ids: [], purpose_label_ids: [] },
        projects: { total: collectionProjects.length, today: 0, trend: series(0.2) }, tasks: { total: collectionTasks.length, today: 1, trend: series(0.4) },
        packages: { total: dataPackages.length, today: 2, trend: series(1), status_counts: { pending_assignment: 1, assigned: 1, other: dataPackages.length - 2, voided: 0 } },
        duration: { basis: params?.basis || 'valid', total_s: 15588, today_s: 3600, pending_review_duration_s: 5400, trend: series(1800) },
        size: { basis: params?.basis || 'valid', total_bytes: 90924000000, today_bytes: 2147483648, trend: series(1073741824) }, incomplete_packages: 0, computed_at: '2026-09-29T04:00:00Z',
      };
      if (params?.format === 'csv') return `﻿时间,项目数,任务数,数据包数,时长（秒）,大小（字节）\n${buckets.map((bucket, i) => [bucket, payload.projects.trend[i].value, payload.tasks.trend[i].value, payload.packages.trend[i].value, payload.duration.trend[i].value, payload.size.trend[i].value].join(',')).join('\n')}\n`;
      return payload;
    }
    if (path === '/collection-dashboard/capacity') {
      const end = params?.end_date || '2026-09-29';
      const start = params?.start_date || '2026-08-31';
      const projectRows = collectionProjects.map((project, index) => ({ id: Number(project.id), name: project.name, packages: index + 1, share: (index + 1) / Math.max(collectionProjects.length * (collectionProjects.length + 1) / 2, 1) }));
      const taskRows = collectionTasks.map((task, index) => ({ id: Number(task.id), name: task.name, packages: index + 1, share: (index + 1) / Math.max(collectionTasks.length * (collectionTasks.length + 1) / 2, 1) }));
      const completion = { projects: projectRows.map((row) => ({ id: row.id, target_s: 7200, valid_s: row.id === projectRows[0]?.id ? 3600 : 0, completion: row.id === projectRows[0]?.id ? 0.5 : 0 })), tasks: taskRows.map((row) => ({ id: row.id, target_s: 7200, valid_s: row.id === taskRows[0]?.id ? 3600 : 0, completion: row.id === taskRows[0]?.id ? 0.5 : 0 })) };
      return { workspace_id: Number(params?.workspace_id || 1), filters: { start_date: start, end_date: end, granularity: params?.granularity || 'day', basis: 'valid', tz: 'Asia/Shanghai', project_ids: [], task_ids: [], scene_label_ids: [], purpose_label_ids: [] }, projects: projectRows, tasks: taskRows, completion, duration: { raw_total_s: 18000, valid_total_s: 12600, raw_trend: [], valid_trend: [] }, personnel: [{ id: 1, name: '演示采集员', duration_s: 12600 }], devices: [{ id: 1, name: '演示设备', duration_s: 12600 }], computed_at: '2026-09-29T04:00:00Z' };
    }
    if (path === '/collection-dashboard/efficiency') {
      const output = [{ id: 1, name: '演示采集员', valid_duration_s: 12600 }, { id: null, name: '未归属', valid_duration_s: 1800 }];
      return { workspace_id: Number(params?.workspace_id || 1), filters: { start_date: params?.start_date || '2026-08-31', end_date: params?.end_date || '2026-09-29', granularity: params?.granularity || 'day', basis: 'valid', tz: 'Asia/Shanghai', project_ids: [], task_ids: [], scene_label_ids: [], purpose_label_ids: [] }, top5: output, bottom5: [...output].reverse(), trend: [], personnel: output, computed_at: '2026-09-29T04:00:00Z' };
    }
    if (path === '/collection-labels') {
      const items = collectionLabels.filter((item) => {
        if (!params.include_inactive && !item.is_active) return false;
        if (params.category && item.category !== params.category) return false;
        return true;
      });
      return { items, total: items.length };
    }
    if (path === '/collection-projects') {
      const items = collectionProjects.filter((project) => params.include_archived || project.status !== 'archived');
      return { items, total: items.length };
    }
    const projectMatch = path.match(/^\/collection-projects\/(\d+)$/);
    if (projectMatch) return collectionProjects.find((item) => Number(item.id) === Number(projectMatch[1])) || {};
    if (path === '/collection-tasks') {
      const items = collectionTasks.filter((task) => !params.project_id || Number(task.project_id) === Number(params.project_id)).map((task) => {
        const packages = dataPackages.filter((pkg) => Number(pkg.collection_task_id) === Number(task.id));
        return { ...task, package_count: packages.length, pending_assignment_count: packages.filter((pkg) => pkg.status === 'pending_assignment').length };
      });
      return { items, total: items.length };
    }
    const taskMatch = path.match(/^\/collection-tasks\/(\d+)$/);
    if (taskMatch) return collectionTasks.find((item) => Number(item.id) === Number(taskMatch[1])) || {};
    const taskPackagesMatch = path.match(/^\/collection-tasks\/(\d+)\/packages$/);
    if (taskPackagesMatch) {
      const items = dataPackages.filter((item) => Number(item.collection_task_id) === Number(taskPackagesMatch[1]));
      return { items, total: items.length };
    }
    if (path === '/tokens') return { items: apiTokens.map((item) => { const { secret, ...rest } = item; return rest; }), total: apiTokens.length };
    if (path === '/data-packages') {
      const items = dataPackages.filter((item) => {
        if (params.status && item.status !== params.status) return false;
        if (params.collection_project_id) {
          const projectIds = Array.isArray(params.collection_project_id) ? params.collection_project_id : [params.collection_project_id];
          if (!projectIds.some((projectId) => Number(item.collection_project_id) === Number(projectId))) return false;
        }
        if (params.collection_task_id && Number(item.collection_task_id) !== Number(params.collection_task_id)) return false;
        if (params.operator_collector_id && Number(item.operator_collector_id) !== Number(params.operator_collector_id)) return false;
        if (params.collection_device_id && Number(item.collection_device_id ?? item.device_id) !== Number(params.collection_device_id)) return false;
      const task = collectionTasks.find((candidate) => Number(candidate.id) === Number(item.collection_task_id));
      const taskLabelIds = new Set(task?.label_ids || []);
        for (const [param, category] of [['purpose_label_id', 'purpose'], ['scene_label_id', 'scene'], ['modality_label_id', 'modality'], ['training_label_id', 'training']]) {
          const taskTag = task?.tags?.[category === 'training' ? 'train' : category];
          const label = collectionLabels.find((candidate) => Number(candidate.id) === Number(params[param]));
          if (params[param] && !(label && (label.category === category || (category === 'training' && label.category === 'train')) && (taskLabelIds.has(Number(label.id)) || taskTag === label.name))) return false;
        }
        return true;
      }).map(enrichDataPackage);
      return { items, total: items.length };
    }
    const packageEpisodePreviewMatch = path.match(/^\/data-packages\/(\d+)\/episodes\/(\d+)\/preview-urls$/);
    if (packageEpisodePreviewMatch) {
      return {
        streams: [{ topic: '/camera/front/rgb', video_url: demoPreviewUrl }],
      };
    }
    const packageMatch = path.match(/^\/data-packages\/(\d+)$/);
    if (packageMatch) {
      const pkg = dataPackages.find((item) => Number(item.id) === Number(packageMatch[1]));
      if (!pkg) return {};
      const task = collectionTasks.find((item) => Number(item.id) === Number(pkg.collection_task_id)) || {};
      const project = collectionProjects.find((item) => Number(item.id) === Number(pkg.collection_project_id)) || {};
      const episodes = [
        { id: 4300, episode_uid: 'EGO-20260811-0007', modality: 'ego', duration_s: 125, admission_status: 'passed', validity_status: 'valid', preview_available: true, privacy_sensitive: false },
        { id: 4301, episode_uid: 'EGO-20260811-0008', modality: 'ego', duration_s: 96, admission_status: 'failed', validity_status: 'unverified', preview_available: false, privacy_sensitive: false, admission_reason: 'integrity_failed' },
        { id: 4302, episode_uid: 'EGO-20260813-0011', modality: 'ego', duration_s: 140, admission_status: 'running', validity_status: 'unverified', preview_available: false, privacy_sensitive: true },
        { id: 4310, episode_uid: 'DRV-20260812-0001', modality: 'ego', duration_s: 62, admission_status: 'passed', validity_status: 'valid', preview_available: true, privacy_sensitive: false },
      ];
      const counts = {
        ready: episodes.filter((item) => item.admission_status === 'passed').length,
        failed: episodes.filter((item) => item.admission_status === 'failed').length,
        running: episodes.filter((item) => item.admission_status === 'running').length,
        reviewed: pkg.intake_reviewed ? episodes.length : 0,
      };
      return {
        ...enrichDataPackage(pkg),
        collection_task: { id: task.id, name: task.name },
        collection_project: { id: project.id, name: project.name },
        responsible_collector: collectorProfiles.find((item) => Number(item.id) === Number(pkg.responsible_collector_id)) || null,
        device: collectionDevices.find((item) => Number(item.id) === Number(pkg.device_id)) || null,
        captured_duration_hours: '2.00',
        intake_valid_duration_hours: pkg.intake_valid_duration_hours || '0.00',
        admission_counts: counts,
        episodes: episodes.map((item) => ({ ...item, duration_hours: (item.duration_s / 3600).toFixed(2) })),
        intake_review: pkg.intake_review || null,
      };
    }
    const manifestMatch = path.match(/^\/data-packages\/(\d+)\/offline-manifest$/);
    if (manifestMatch) {
      const pkg = dataPackages.find((item) => Number(item.id) === Number(manifestMatch[1]));
      return {
        schema_version: 1,
        package_uid: pkg?.package_uid || `pkg-${manifestMatch[1]}`,
        workspace_id: 1,
        collection_project_id: pkg?.collection_project_id || 11,
        collection_task_id: pkg?.collection_task_id || 101,
        target_duration_hours: pkg?.target_duration_hours || '2.00',
        responsible_collector_id: pkg?.responsible_collector_id || 101,
        operator_collector_id: pkg?.operator_collector_id || 101,
        device_model_id: 1,
        assigned_at: pkg?.assigned_at || '2026-08-14T00:40:00Z',
      };
    }

    // Collected data packages (build data) ----------------------------------
    if (path === '/data-batches/candidates') {
      const items = dataBatchCandidates.slice();
      return { items, total: items.length };
    }
    if (path === '/data-batches') {
      const items = dataBatches.slice();
      return { items, total: items.length };
    }
    const dataBatchMatch = path.match(/^\/data-batches\/(\d+)$/);
    if (dataBatchMatch) {
      const batch = dataBatches.find((item) => Number(item.id) === Number(dataBatchMatch[1]));
      if (!batch) return {};
      return { ...batch, assets: dataAssets.filter((asset) => Number(asset.data_batch_id) === Number(batch.id)) };
    }
    if (path === '/annotation-work-items') {
      const items = annotationWorkItems.filter((item) => !params.status || item.status === params.status);
      return { items, total: items.length };
    }
    if (path === '/review-work-items') {
      const items = reviewWorkItems.filter((item) => !params.status || item.status === params.status);
      return { items, total: items.length };
    }
    if (path === '/data-assets') {
      const items = dataAssets.slice();
      return { items, total: items.length };
    }
    const dataAssetMatch = path.match(/^\/data-assets\/(\d+)$/);
    if (dataAssetMatch) return dataAssets.find((item) => Number(item.id) === Number(dataAssetMatch[1])) || {};
    if (path === '/catalog-datasets') return { items: catalogDatasets.map((item) => ({ ...item })), total: catalogDatasets.length };
    const catalogDatasetMatch = path.match(/^\/catalog-datasets\/(\d+)$/);
    if (catalogDatasetMatch) return catalogDatasets.find((item) => Number(item.id) === Number(catalogDatasetMatch[1])) || {};
    const catalogVersionMatch = path.match(/^\/catalog-datasets\/(\d+)\/versions$/);
    if (catalogVersionMatch) {
      const items = catalogVersions[Number(catalogVersionMatch[1])] || [];
      return { items: items.map((item) => ({ ...item })), total: items.length };
    }

    // QRDF datasets ---------------------------------------------------------
    if (path === '/datasets') {
      const keyword = String(params.keyword || '').toLowerCase();
      const items = qrdfDatasets.filter((item) => !keyword || item.name.toLowerCase().includes(keyword));
      return { items: items.map((item) => ({ ...item })), total: items.length, limit: params.limit || 100, offset: params.offset || 0 };
    }
    if (path === '/datasets/catalog') {
      return {
        items: qrdfDatasets.map((item) => ({ ...item, dataset_type: item.type, episode_total: item.episode_count })),
        total: qrdfDatasets.length,
      };
    }
    const revisionListMatch = path.match(/^\/datasets\/(\d+)\/revisions$/);
    if (revisionListMatch) {
      const items = datasetRevisionMap[Number(revisionListMatch[1])] || [];
      return { items: items.map((item) => ({ ...item })), total: items.length };
    }
    const revisionCandidateMatch = path.match(/^\/datasets\/(\d+)\/revision-candidates$/);
    if (revisionCandidateMatch) {
      const candidates = datasetCandidateMap[Number(revisionCandidateMatch[1])];
      return candidates ? { ...candidates, items: candidates.items.map((item) => ({ ...item })) } : { items: [], total: 0 };
    }
    const datasetMatch = path.match(/^\/datasets\/(\d+)$/);
    if (datasetMatch) {
      const dataset = qrdfDatasets.find((item) => Number(item.id) === Number(datasetMatch[1]));
      if (!dataset) return {};
      return { ...dataset, revisions: (datasetRevisionMap[dataset.id] || []).map((item) => ({ ...item })) };
    }
    const revisionMatch = path.match(/^\/dataset-revisions\/(\d+)$/);
    if (revisionMatch) {
      const all = Object.values(datasetRevisionMap).flat();
      return all.find((item) => Number(item.id) === Number(revisionMatch[1])) || {};
    }
    const exportMatch = path.match(/^\/exports\/(\d+)$/);
    if (exportMatch) return datasetExports.find((item) => Number(item.id) === Number(exportMatch[1])) || {};
    const exportDownloadMatch = path.match(/^\/exports\/(\d+)\/download-url$/);
    if (exportDownloadMatch) return { available: true, url: demoPreviewUrl, expires_at: '2026-08-21T12:00:00Z' };
    const exportDeliveryMatch = path.match(/^\/exports\/(\d+)\/delivery$/);
    if (exportDeliveryMatch) {
      return {
        id: Number(exportDeliveryMatch[1]),
        status: 'succeeded',
        destination: 'oss://quicstudio-export/desk-grasp-ego/v3',
        delivered_at: '2026-08-20T07:26:00Z',
        artifacts: [{ name: 'dataset.tar', size: 47103013683 }],
      };
    }

    // Native LeRobot datasets ----------------------------------------------
    if (path === '/native-lerobot-datasets') {
      const items = nativeLerobotDatasets.filter((item) => !params.copy_status || item.copy_status === params.copy_status);
      return { items: items.map((item) => ({ ...item })), total: items.length };
    }
    const nativeDatasetMatch = path.match(/^\/native-lerobot-datasets\/(\d+)$/);
    if (nativeDatasetMatch) {
      const dataset = nativeLerobotDatasets.find((item) => Number(item.id) === Number(nativeDatasetMatch[1]));
      if (!dataset) return {};
      return { ...dataset, bundles: nativeLerobotBundles.filter((item) => Number(item.dataset_id) === Number(dataset.id)) };
    }
    const nativeOssMatch = path.match(/^\/native-lerobot-datasets\/(\d+)\/oss-uri$/);
    if (nativeOssMatch) {
      const dataset = nativeLerobotDatasets.find((item) => Number(item.id) === Number(nativeOssMatch[1]));
      return { uri: dataset?.oss_uri || '', available: Boolean(dataset) };
    }
    const bundleMatch = path.match(/^\/native-lerobot-datasets\/(\d+)\/bundles\/(\d+)$/);
    if (bundleMatch) return nativeLerobotBundles.find((item) => Number(item.id) === Number(bundleMatch[2])) || {};
    const bundleDownloadMatch = path.match(/^\/native-lerobot-datasets\/(\d+)\/bundles\/(\d+)\/download$/);
    if (bundleDownloadMatch) return { available: true, url: demoPreviewUrl };

    // Batch detail extras ---------------------------------------------------
    if (path === '/batches/lerobot-candidates') {
      const items = nativeLerobotDatasets.map((item) => ({ id: item.id, dataset_id: item.dataset_id, robot_type: item.robot_type, file_count: item.file_count, total_size: item.total_size, selected: false }));
      return { items, total: items.length };
    }
    const activityMatch = path.match(/^\/batches\/(\d+)\/activity$/);
    if (activityMatch) {
      const items = batchActivities[Number(activityMatch[1])] || [];
      return { items: items.map((item) => ({ ...item })), total: items.length };
    }
    const nativeSessionsMatch = path.match(/^\/batches\/(\d+)\/native-lerobot-sessions$/);
    if (nativeSessionsMatch) {
      return {
        items: [
          { id: 'nls-501-a', batch_id: Number(nativeSessionsMatch[1]), status: 'submitted', selected_count: 2, registered_count: 2, skipped_count: 0, updated_at: '2026-08-18T03:00:00Z' },
          { id: 'nls-501-b', batch_id: Number(nativeSessionsMatch[1]), status: 'draft', selected_count: 1, registered_count: 0, skipped_count: 1, updated_at: '2026-08-19T01:20:00Z' },
        ],
        total: 2,
      };
    }
    if (path === '/batches/native-lerobot-scan-snapshots/current') {
      return { id: 'snap-current', status: 'ready', source: 'oss://quicstudio-raw/lerobot/', scanned_at: '2026-08-21T03:50:00Z', candidate_count: 3 };
    }
    const scanCandidateMatch = path.match(/^\/batches\/native-lerobot-scan-snapshots\/([^/]+)\/candidates$/);
    if (scanCandidateMatch) {
      const items = nativeLerobotDatasets.map((item) => ({ id: `cand-${item.id}`, dataset_id: item.dataset_id, robot_type: item.robot_type, file_count: item.file_count, total_size: item.total_size, status: 'pending' }));
      return { items, total: items.length };
    }
    if (path === '/jobs/7701') return { ...importJobs[7701] };
    const jobMatch = path.match(/^\/jobs\/(\d+)$/);
    if (jobMatch) return importJobs[Number(jobMatch[1])] ? { ...importJobs[Number(jobMatch[1])] } : {};

    // Import sessions -------------------------------------------------------
    const importMatch = path.match(/^\/imports\/([^/]+)$/);
    if (importMatch && importMatch[1] !== 'capabilities') {
      const session = Object.values(importSessions).flat().find((item) => String(item.id) === String(importMatch[1]));
      if (!session) return {};
      return {
        ...session,
        batch_name: batches.find((item) => Number(item.id) === Number(session.batch_id))?.name || '',
        candidates: (importCandidates[session.id] || []).map((item) => ({ ...item })),
      };
    }
    const candidateMatch = path.match(/^\/imports\/([^/]+)\/candidates$/);
    if (candidateMatch) {
      const items = (importCandidates[candidateMatch[1]] || []).map((item) => ({ ...item }));
      return { items, total: items.length };
    }
    if (path === '/collector-profiles/available') {
      const items = [
        { id: 501, name: '赵敏', phone_suffix: '3345', is_active: true },
        { id: 502, name: '孙磊', phone_suffix: '9087', is_active: true },
      ].filter((item) => !params.query || item.name.includes(params.query));
      return { items, total: items.length };
    }
    return null;
  }

  function read(path, params = {}) {
    const extended = readExtended(path, params);
    if (extended !== null) return extended;
    return readCore(path, params);
  }

  const demoDrafts = new Map();

  function writeCore(method, path, body = {}) {
    const draftMatch = path.match(/^\/work-queue\/items\/(\d+)\/draft$/);
    if (method === 'POST' && path === '/dashboard/refresh') {
      return {
        job_id: dashboardOverview.etl_job_id,
        status: 'succeeded',
        scope_key: dashboardOverview.scope.key,
        baseline_computed_at: dashboardOverview.computed_at,
        payload: dashboardOverview,
      };
    }
    if (method === 'PUT' && draftMatch) {
      const workItemId = Number(draftMatch[1]);
      const prior = demoDrafts.get(workItemId) || workbench(workItemId).draft || { version: 0, payload: {} };
      const draft = {
        version: Number(prior.version || 0) + 1,
        payload: structuredClone(body.payload || {}),
      };
      demoDrafts.set(workItemId, draft);
      return draft;
    }
    return { id: Date.now(), demo: true };
  }

  let demoSequence = 5000;
  function nextId() {
    demoSequence += 1;
    return demoSequence;
  }

  function nowIso() {
    return new Date().toISOString();
  }

  function findWorkQueueItem(workItemId) {
    const target = Number(workItemId);
    for (const stage of Object.keys(queueCases)) {
      const item = queueCases[stage].find((row) => Number(row.work_item.id) === target);
      if (item) return item;
    }
    return null;
  }

  function writeExtended(method, path, body = {}) {
    // Workspace and master data --------------------------------------------
    const authUserResetMatch = path.match(/^\/auth\/users\/(\d+)\/reset-password$/);
    if (method === 'POST' && authUserResetMatch) {
      const user = managedUsers.find((item) => Number(item.id) === Number(authUserResetMatch[1]));
      const temporaryPassword = `Tmp-${Math.random().toString(36).slice(2, 12)}`;
      if (user) user.must_change_password = true;
      return { id: Number(authUserResetMatch[1]), email: user?.email || '', temporary_password: temporaryPassword, must_change_password: true, revoked_sessions: 0 };
    }
    if (method === 'POST' && path === '/workspace/create') {
      const workspace = { id: nextId(), name: body.name || '新建数采工作空间', created_at: nowIso() };
      return workspace;
    }
    if (method === 'POST' && path === '/workspace/task-set/create') {
      return { id: nextId(), workspace_id: Number(body.workspace_id) || 1, name: body.name || '新建采集项目', created_at: nowIso() };
    }
    if (method === 'POST' && path === '/task-labels') {
      const label = { id: nextId(), name: body.name || '新建采集任务', is_active: true };
      taskLabels.push(label);
      return label;
    }
    if (method === 'POST' && path === '/collector-profiles') {
      const profile = {
        id: nextId(),
        workspace_sequence: collectorProfiles.length + 1,
        name: body.name || '新采集员',
        phone_suffix: String(body.phone || '').slice(-4) || '0000',
        is_active: true,
      };
      collectorProfiles.push(profile);
      return profile;
    }
    const profileMatch = path.match(/^\/collector-profiles\/(\d+)$/);
    if (method === 'PATCH' && profileMatch) {
      const profile = collectorProfiles.find((item) => Number(item.id) === Number(profileMatch[1]));
      if (profile) Object.assign(profile, body);
      return profile || { id: Number(profileMatch[1]) };
    }
    if (method === 'POST' && path === '/collector-profiles/memberships') {
      return { id: nextId(), workspace_id: body.workspace_id, personnel_profile_id: body.personnel_profile_id };
    }
    if (method === 'DELETE' && /^\/collector-profiles\/\d+\/memberships$/.test(path)) {
      return { ok: true };
    }
    if (method === 'POST' && path === '/collection-devices') {
      const device = {
        id: nextId(),
        workspace_sequence: collectionDevices.length + 1,
        name: body.name || '新采集设备',
        serial_number: body.serial_number || `SN-${nextId()}`,
        software_number: body.software_number || 'QV-2.6.0',
        is_active: true,
      };
      collectionDevices.push(device);
      return device;
    }
    const deviceMatch = path.match(/^\/collection-devices\/(\d+)$/);
    if (method === 'PATCH' && deviceMatch) {
      const device = collectionDevices.find((item) => Number(item.id) === Number(deviceMatch[1]));
      if (device) Object.assign(device, body);
      return device || { id: Number(deviceMatch[1]) };
    }

    // Batches and imports ---------------------------------------------------
    if (method === 'POST' && path === '/batches') {
      const batch = {
        id: nextId(),
        sequence_number: batches.length + 19,
        name: body.name || '新建采集批次',
        batch_type: body.batch_type || 'ego',
        status: 'created',
        workspace_id: Number(body.workspace_id) || 1,
        task_set_id: Number(body.task_set_id) || 12,
        task_label: body.task_label || taskLabels[0],
        created_at: nowIso(),
        source_episode_count: 0,
        source_duration_s: 0,
        quality_progress: { completed: 0, total: 0, failed: 0 },
      };
      batches.unshift(batch);
      return batch;
    }
    const batchImportMatch = path.match(/^\/batches\/(\d+)\/imports$/);
    if (method === 'POST' && batchImportMatch) {
      const batchId = Number(batchImportMatch[1]);
      const session = {
        id: `demo-import-${batchId}-${nextId()}`,
        batch_id: batchId,
        import_type: body.import_type || 'chunked_upload',
        original_name: body.original_name || 'demo_session.zip',
        task_label_id: body.task_label_id || 3,
        status: 'uploading',
        updated_at: nowIso(),
        upload_progress: { uploaded_chunks: 0, total_chunks: 12, percent: 0 },
        available_actions: [],
      };
      importSessions[batchId] = [...(importSessions[batchId] || []), session];
      return session;
    }
    const importActionMatch = path.match(/^\/imports\/([^/]+)\/(scan|cancel|retry|chunked\/init|chunked\/complete|multipart\/init|multipart\/complete)$/);
    if (method === 'POST' && importActionMatch) {
      const sessionId = importActionMatch[1];
      const action = importActionMatch[2];
      const session = Object.values(importSessions).flat().find((item) => String(item.id) === String(sessionId));
      if (action === 'scan') {
        importCandidates[sessionId] = importCandidates[sessionId] || [
          { id: `cand-${sessionId}-1`, name: `${session?.original_name || 'demo'}-part1.mcap`, size_bytes: 1073741824, duration_s: 640, status: 'pending', selected: false },
        ];
        if (session) { session.status = 'parsed'; session.updated_at = nowIso(); }
        return { session_id: sessionId, candidate_count: importCandidates[sessionId].length, status: 'parsed' };
      }
      if (!session) return { id: sessionId, demo: true };
      if (action === 'cancel') session.status = 'cancelled';
      else if (action === 'retry') session.status = 'parsing';
      else if (action === 'chunked/init' || action === 'multipart/init') {
        session.status = 'uploading';
        return { session_id: sessionId, upload_id: `up-${nextId()}`, part_size: 67108864, chunk_size: 8388608 };
      } else if (action === 'chunked/complete' || action === 'multipart/complete') session.status = 'parsing';
      session.updated_at = nowIso();
      return { ...session };
    }
    const importCandidateImportMatch = path.match(/^\/imports\/([^/]+)\/candidates\/([^/]+)\/import$/);
    if (method === 'POST' && importCandidateImportMatch) {
      const [, sessionId, candidateId] = importCandidateImportMatch;
      const candidate = (importCandidates[sessionId] || []).find((item) => String(item.id) === String(candidateId));
      if (candidate) { candidate.status = 'imported'; candidate.selected = true; }
      return { id: candidateId, status: 'imported', demo: true };
    }
    if (method === 'POST' && path === '/imports/candidates/import') {
      return { id: nextId(), imported_count: (body.candidate_ids || []).length, demo: true };
    }
    const jobRetryMatch = path.match(/^\/jobs\/(\d+)\/retry$/);
    if (method === 'POST' && jobRetryMatch) {
      const job = importJobs[Number(jobRetryMatch[1])];
      if (job) { job.status = 'running'; job.progress = 0; job.updated_at = nowIso(); }
      return job ? { ...job } : { id: Number(jobRetryMatch[1]), demo: true };
    }

    // Work queue ------------------------------------------------------------
    const queueActionMatch = path.match(/^\/work-queue\/items\/(\d+)\/([a-zA-Z_]+)$/);
    if (method === 'POST' && queueActionMatch) {
      const item = findWorkQueueItem(queueActionMatch[1]);
      const action = queueActionMatch[2];
      if (item) {
        // Mirrors the backend transitions: claim takes pending work, release
        // returns it, submit/review move the item into the review pipeline.
        const statusMap = { claim: 'assigned', release: 'pending', continue: 'in_progress', submit: 'submitted', review: 'accepted', approve: 'accepted', accept: 'accepted', return: 'rejected', reject: 'rejected', start: 'in_progress', publish: 'accepted' };
        item.work_item.status = statusMap[action] || item.work_item.status;
        item.work_item.available_actions = action === 'release' || action === 'return' || action === 'reject'
          ? ['claim']
          : (item.work_item.status === 'assigned' ? ['continue', 'release'] : item.work_item.available_actions);
        item.work_item.version = Number(item.work_item.version || 1) + 1;
      }
      return item ? { ...item.work_item } : { id: Number(queueActionMatch[1]), demo: true };
    }

    // Data governance -------------------------------------------------------
    if (method === 'POST' && path === '/data-batches') {
      const packageIds = body.data_package_ids || body.package_ids || [];
      const batch = {
        id: nextId(),
        workspace_id: Number(body.workspace_id) || 1,
        name: body.name || `data-batch-${nextId()}`,
        status: 'building',
        created_at: nowIso(),
        updated_at: nowIso(),
        data_package_ids: packageIds,
        integrity_check_enabled: body.integrity_check_enabled !== false,
        quality_check_enabled: body.quality_check_enabled !== false,
        compliance_check_enabled: body.compliance_check_enabled !== false,
        data_asset_count: packageIds.length,
        governed_valid_duration_hours: 0,
      };
      dataBatches.unshift(batch);
      return batch;
    }
    const annotationPatchMatch = path.match(/^\/annotation-work-items\/(\d+)$/);
    if (method === 'PATCH' && annotationPatchMatch) {
      const item = annotationWorkItems.find((row) => Number(row.id) === Number(annotationPatchMatch[1]));
      if (item) { item.updated_at = nowIso(); Object.assign(item, body); }
      return item || { id: Number(annotationPatchMatch[1]), demo: true };
    }
    const annotationSubmitMatch = path.match(/^\/annotation-work-items\/(\d+)\/(submit|reassign)$/);
    if (method === 'POST' && annotationSubmitMatch) {
      const item = annotationWorkItems.find((row) => Number(row.id) === Number(annotationSubmitMatch[1]));
      if (item) {
        item.status = annotationSubmitMatch[2] === 'submit' ? 'submitted' : 'reassigned';
        if (body.assignee_user_id) item.assignee_user_id = Number(body.assignee_user_id);
        if (body.reason) item.reassign_reason = body.reason;
        item.updated_at = nowIso();
      }
      return item ? { ...item } : { id: Number(annotationSubmitMatch[1]), demo: true };
    }
    const reviewActionMatch = path.match(/^\/review-work-items\/(\d+)\/(approve|return|reassign)$/);
    if (method === 'POST' && reviewActionMatch) {
      const item = reviewWorkItems.find((row) => Number(row.id) === Number(reviewActionMatch[1]));
      const action = reviewActionMatch[2];
      if (item) {
        item.status = action === 'approve' ? 'approved' : (action === 'return' ? 'returned' : 'reassigned');
        if (body.assignee_user_id) item.assignee_user_id = Number(body.assignee_user_id);
        if (body.reason) item.reason = body.reason;
        item.updated_at = nowIso();
      }
      return item ? { ...item } : { id: Number(reviewActionMatch[1]), demo: true };
    }
    if (method === 'POST' && path === '/catalog-datasets') {
      const dataset = { id: nextId(), name: body.name || '新建目录数据集', source_kind: body.source_kind || 'data_batches', status: 'active', description: body.description || '', created_at: nowIso(), latest_version: 0 };
      catalogDatasets.push(dataset);
      return dataset;
    }
    const catalogVersionMatch = path.match(/^\/catalog-datasets\/(\d+)\/versions$/);
    if (method === 'POST' && catalogVersionMatch) {
      const datasetId = Number(catalogVersionMatch[1]);
      const list = catalogVersions[datasetId] || [];
      const version = {
        id: nextId(),
        dataset_id: datasetId,
        version: list.length + 1,
        status: 'ready',
        data_asset_ids: body.data_asset_ids || [],
        created_at: nowIso(),
      };
      catalogVersions[datasetId] = [version, ...list];
      const dataset = catalogDatasets.find((item) => Number(item.id) === datasetId);
      if (dataset) dataset.latest_version = version.version;
      return version;
    }
    const catalogExportMatch = path.match(/^\/catalog-datasets\/versions\/(\d+)\/export$/);
    if (method === 'POST' && catalogExportMatch) {
      const versionId = Number(catalogExportMatch[1]);
      const allVersions = Object.values(catalogVersions).flat();
      const version = allVersions.find((item) => Number(item.id) === versionId);
      if (version) version.status = 'exporting';
      return { id: nextId(), version_id: versionId, format: body.format || 'lerobot_3_0', status: 'running', created_at: nowIso() };
    }
    if (method === 'POST' && path === '/catalog-datasets/lerobot-imports') {
      return { id: nextId(), status: 'importing', dataset_count: (body.datasets || []).length || 1 };
    }
    if (method === 'POST' && path === '/train/catalog-registrations') {
      return { id: nextId(), version_id: body.version_id, status: 'registered', registered_at: nowIso() };
    }

    // Datasets and exports --------------------------------------------------
    if (method === 'POST' && path === '/datasets') {
      const dataset = { id: nextId(), workspace_id: Number(body.workspace_id) || 1, name: body.name || '新建数据集', type: body.type || 'qrdf', status: 'draft', episode_count: 0, revision_count: 0, created_at: nowIso(), updated_at: nowIso() };
      qrdfDatasets.unshift(dataset);
      return dataset;
    }
    const revisionCreateMatch = path.match(/^\/datasets\/(\d+)\/revisions$/);
    if (method === 'POST' && revisionCreateMatch) {
      const datasetId = Number(revisionCreateMatch[1]);
      const list = datasetRevisionMap[datasetId] || [];
      const revision = { id: nextId(), dataset_id: datasetId, version: list.length + 1, status: 'building', episode_count: (body.episode_ids || []).length, size_bytes: 0, created_at: nowIso(), filters: body.filters || {} };
      datasetRevisionMap[datasetId] = [revision, ...list];
      return revision;
    }
    const revisionExportMatch = path.match(/^\/dataset-revisions\/(\d+)\/exports$/);
    if (method === 'POST' && revisionExportMatch) {
      const exportJob = { id: nextId(), revision_id: Number(revisionExportMatch[1]), format: body.format || 'lerobot_3_0', status: 'running', size_bytes: 0, created_at: nowIso(), progress: 0 };
      datasetExports.unshift(exportJob);
      return exportJob;
    }
    const exportRetryMatch = path.match(/^\/exports\/(\d+)\/retry$/);
    if (method === 'POST' && exportRetryMatch) {
      const job = datasetExports.find((item) => Number(item.id) === Number(exportRetryMatch[1]));
      if (job) { job.status = 'running'; job.progress = 0; job.failure_reason = null; }
      return job ? { ...job } : { id: Number(exportRetryMatch[1]), demo: true };
    }

    // Native LeRobot --------------------------------------------------------
    const nativeDatasetMatch = path.match(/^\/native-lerobot-datasets\/(\d+)$/);
    if (method === 'PATCH' && nativeDatasetMatch) {
      const dataset = nativeLerobotDatasets.find((item) => Number(item.id) === Number(nativeDatasetMatch[1]));
      if (dataset) Object.assign(dataset, body);
      return dataset || { id: Number(nativeDatasetMatch[1]), demo: true };
    }
    const nativeCopyMatch = path.match(/^\/native-lerobot-datasets\/(\d+)\/(copy\/retry|source-reauthorization)$/);
    if (method === 'POST' && nativeCopyMatch) {
      const dataset = nativeLerobotDatasets.find((item) => Number(item.id) === Number(nativeCopyMatch[1]));
      if (dataset) { dataset.copy_status = 'copying'; dataset.failure_reason = null; dataset.updated_at = nowIso(); }
      return dataset ? { ...dataset } : { id: Number(nativeCopyMatch[1]), demo: true };
    }
    const nativeBundleMatch = path.match(/^\/native-lerobot-datasets\/(\d+)\/bundles$/);
    if (method === 'POST' && nativeBundleMatch) {
      const bundle = { id: nextId(), dataset_id: Number(nativeBundleMatch[1]), status: 'building', size_bytes: 0, created_at: nowIso() };
      nativeLerobotBundles.unshift(bundle);
      return bundle;
    }

    // Collection projects / tasks / packages --------------------------------
    if (method === 'POST' && path === '/collection-projects') {
      const project = { id: nextId(), workspace_id: Number(body.workspace_id) || 1, name: body.name || '新建采集项目', status: 'active', created_at: nowIso(), task_count: 0, description: body.description || '' };
      collectionProjects.push(project);
      return project;
    }
    const collectionProjectMatch = path.match(/^\/collection-projects\/(\d+)$/);
    if (method === 'PATCH' && collectionProjectMatch) {
      const project = collectionProjects.find((item) => Number(item.id) === Number(collectionProjectMatch[1]));
      if (project) Object.assign(project, body);
      return project || { id: Number(collectionProjectMatch[1]), demo: true };
    }
    const collectionProjectArchiveMatch = path.match(/^\/collection-projects\/(\d+)\/archive$/);
    if (method === 'POST' && collectionProjectArchiveMatch) {
      const project = collectionProjects.find((item) => Number(item.id) === Number(collectionProjectArchiveMatch[1]));
      if (project) project.status = 'archived';
      return project ? { ...project } : { id: Number(collectionProjectArchiveMatch[1]), demo: true };
    }
    if (method === 'POST' && path === '/collection-tasks') {
      const task = {
        id: nextId(),
        workspace_id: Number(body.workspace_id) || 1,
        project_id: Number(body.project_id) || 11,
        name: body.name || '新建采集任务',
        owner: body.owner || '张明',
        sop: body.sop || '',
        target_episodes: Number(body.target_episodes) || 100,
        target_duration_hours: Number(body.target_duration_hours) || 4,
        status: 'active',
        created_at: nowIso(),
        tags: body.tags || {},
      };
      collectionTasks.push(task);
      return task;
    }
    if (method === 'POST' && path === '/data-packages') {
      const pkg = {
        id: nextId(),
        workspace_id: Number(body.workspace_id) || 1,
        collection_task_id: Number(body.collection_task_id) || 101,
        collection_project_id: Number(body.collection_project_id) || 11,
        package_uid: body.package_uid || `pkg-${nextId()}`,
        seq: dataPackages.length + 1,
        status: 'assigned',
        target_duration_hours: body.target_duration_hours || '2.00',
        intake_valid_duration_hours: '0.00',
        governed_valid_duration_hours: '0.00',
        responsible_collector_id: Number(body.responsible_collector_id) || 101,
        operator_collector_id: Number(body.operator_collector_id) || 101,
        device_id: Number(body.device_id) || 201,
        assigned_at: nowIso(),
        updated_at: nowIso(),
      };
      dataPackages.push(pkg);
      return pkg;
    }
    if (method === 'POST' && path === '/data-packages/adjust') {
      const items = body.packages || body.items || [];
      const adjusted = [];
      items.forEach((entry) => {
        const pkg = dataPackages.find((item) => Number(item.id) === Number(entry.id));
        if (!pkg) return;
        if (entry.target_duration_hours != null) pkg.target_duration_hours = String(entry.target_duration_hours);
        if (entry.responsible_collector_id != null) pkg.responsible_collector_id = Number(entry.responsible_collector_id);
        if (entry.operator_collector_id != null) pkg.operator_collector_id = Number(entry.operator_collector_id);
        if (entry.device_id != null) pkg.device_id = Number(entry.device_id);
        pkg.updated_at = nowIso();
        adjusted.push({ ...pkg });
      });
      return { items: adjusted, adjusted_count: adjusted.length };
    }
    const supplementMatch = path.match(/^\/data-packages\/(\d+)\/supplements$/);
    if (method === 'POST' && supplementMatch) {
      const source = dataPackages.find(p => Number(p.id) === Number(supplementMatch[1]));
      if (!source?.intake_review) throw new Error('请先完成原包入库审核');
      const id = nextId();
      const pkg = { id, package_uid: `pkg-${id}`, workspace_id: source.workspace_id,
        collection_project_id: source.collection_project_id, collection_task_id: source.collection_task_id,
        target_duration_hours: String(body.target_duration_hours), status: 'pending_assignment',
        supplement_for_package_id: source.id, supplement_reason: body.reason,
        responsible_collector_id: null, operator_collector_id: null, collector_id: null,
        intake_valid_duration_hours: null, created_at: nowIso() };
      dataPackages.push(pkg);
      return { ...pkg };
    }
    if (method === 'POST' && path === '/data-packages/batch-assign') {
      const assignments = body.assignments || [];
      const targets = assignments.map(a => dataPackages.find(p => Number(p.id) === Number(a.data_package_id)));
      if (targets.some(p => !p || p.status !== 'pending_assignment')) throw new Error('只能分配未分配包');
      targets.forEach((p,i) => { p.collector_id = p.responsible_collector_id = p.operator_collector_id = Number(assignments[i].collector_id); p.status = 'assigned'; p.assigned_at = nowIso(); });
      return { packages: targets.map(p => ({ ...p })) };
    }
    const packageActionMatch = path.match(/^\/data-packages\/(\d+)\/(assign|void|intake-review)$/);
    if (method === 'POST' && packageActionMatch) {
      const pkg = dataPackages.find((item) => Number(item.id) === Number(packageActionMatch[1]));
      if (!pkg) return { id: Number(packageActionMatch[1]), demo: true };
      const action = packageActionMatch[2];
      if (action === 'assign') {
        if (pkg.status !== 'pending_assignment') throw new Error('数据包分配已锁定');
        const collector = Number(body.collector_id || body.operator_collector_id || body.responsible_collector_id);
        if (!collector) throw new Error('请选择数采员');
        pkg.collector_id = pkg.responsible_collector_id = pkg.operator_collector_id = collector;
        pkg.status = 'assigned';
        if (body.responsible_collector_id != null) pkg.responsible_collector_id = Number(body.responsible_collector_id);
        if (body.operator_collector_id != null) pkg.operator_collector_id = Number(body.operator_collector_id);
        if (body.device_id != null) pkg.device_id = Number(body.device_id);
        if (body.target_duration_hours != null) pkg.target_duration_hours = String(body.target_duration_hours);
        pkg.assigned_at = nowIso();
      } else if (action === 'void') {
        pkg.status = 'void';
        pkg.void_reason = body.reason || '演示作废';
      } else {
        if (pkg.intake_review || pkg.status !== 'pending_intake_review') throw new Error('入库审核已结束或尚未就绪');
        const verdict = body.verdict || (body.approved === false || body.decision === 'reject' ? 'rejected' : 'approved');
        pkg.status = verdict === 'rejected' ? 'voided' : 'intake_approved';
        pkg.reviewed_at = nowIso();
        const rejectedEpisodeIds = Array.isArray(body.rejected_episode_ids) ? body.rejected_episode_ids.map(Number) : [];
        pkg.rejected_episode_ids = rejectedEpisodeIds;
        pkg.intake_review = {
          verdict,
          reviewer_user_id: 9000,
          accepted_episode_ids: [],
          rejected_episode_ids: rejectedEpisodeIds,
          excluded_episodes: [],
          reason: body.reason || '',
          reviewed_at: nowIso(),
        };
      }
      pkg.updated_at = nowIso();
      return { ...pkg };
    }
    if (method === 'POST' && path === '/data-packages/intake-review/bulk-approve') {
      const ids = (body.package_ids || []).map(Number);
      const approved = [];
      dataPackages.forEach((pkg) => {
        if (!ids.length || ids.includes(Number(pkg.id))) { pkg.status = 'intake_approved'; pkg.updated_at = nowIso(); approved.push(pkg.id); }
      });
      return { approved_count: approved.length, package_ids: approved };
    }
    if (method === 'POST' && path === '/tokens') {
      const id = nextId();
      const secret = `qs_${('t' + Date.now().toString(36)).slice(0, 8)}_${Math.random().toString(36).slice(2, 10)}`;
      const expiresInDays = body.expires_in_days ?? 90;
      const expiresAt = expiresInDays >= 3650 ? null : new Date(Date.now() + expiresInDays * 86400000).toISOString();
      const row = { id, name: body.name || '未命名令牌', key_id: secret.split('_')[1], expires_at: expiresAt, rotated_at: null, last_used_at: null, revoked_at: null, created_at: nowIso() };
      apiTokens.unshift(row);
      return { ...row, secret };
    }
    const tokenRevokeMatch = path.match(/^\/tokens\/(\d+)$/);
    if (method === 'DELETE' && tokenRevokeMatch) {
      const row = apiTokens.find((item) => Number(item.id) === Number(tokenRevokeMatch[1]));
      if (row) row.revoked_at = nowIso();
      const view = row ? { ...row } : { id: Number(tokenRevokeMatch[1]) };
      delete view.secret;
      return view;
    }
    const tokenRotateMatch = path.match(/^\/tokens\/(\d+)\/rotate$/);
    if (method === 'POST' && tokenRotateMatch) {
      const row = apiTokens.find((item) => Number(item.id) === Number(tokenRotateMatch[1]));
      if (row) { row.rotated_at = nowIso(); row.last_used_at = null; }
      const secret = `qs_${(row ? row.key_id : 'rot')}_${Math.random().toString(36).slice(2, 10)}`;
      return { ...(row ? { ...row } : { id: Number(tokenRotateMatch[1]) }), secret };
    }

    // Collection labels -----------------------------------------------------
    if (method === 'POST' && path === '/collection-labels') {
      const label = { id: nextId(), workspace_id: Number(body.workspace_id) || 1, category: body.category || 'scene', name: body.name || '新标签', is_active: true, color: body.color || '#409eff' };
      collectionLabels.push(label);
      return label;
    }
    const labelDeactivateMatch = path.match(/^\/collection-labels\/(\d+)\/deactivate$/);
    if (method === 'POST' && labelDeactivateMatch) {
      const label = collectionLabels.find((item) => Number(item.id) === Number(labelDeactivateMatch[1]));
      if (label) label.is_active = false;
      return label ? { ...label } : { id: Number(labelDeactivateMatch[1]), demo: true };
    }
    return null;
  }

  function write(method, path, body = {}) {
    const extended = writeExtended(method, path, body);
    if (extended !== null) return extended;
    return writeCore(method, path, body);
  }

  /* ------------------------------------------------------------------ *
   * Training control plane projection
   * ------------------------------------------------------------------ */
  const trainProfiles = [
    { id: 'rtx4090_x4', name: 'RTX 4090 · 4 卡', gpu_count: 4, provider_id: 'local', calibration_status: 'calibrated', calibrated_at: '2026-08-02T09:20:00Z', notes: '单机 4 卡，适合 ACT / Diffusion Policy' },
    { id: 'a100_x8', name: 'A100 80G · 8 卡', gpu_count: 8, provider_id: 'aliyun', calibration_status: 'calibrated', calibrated_at: '2026-08-05T03:00:00Z', notes: '大 batch 预训练' },
    { id: 'a800_x8', name: 'A800 80G · 8 卡', gpu_count: 8, provider_id: 'bmcpfs', calibration_status: 'pending', calibrated_at: null, notes: '待校准，暂不可选' },
  ];
  const trainModels = [
    {
      version_id: 'act_v1_4', id: 'act_v1_4', name: 'ACT', version: 'v1.4', maturity: 'stable',
      description: '动作分块 Transformer，单臂桌面操作基线',
      recipes: [
        { id: 'fine_tune', name: '微调 · 20k 步' },
        { id: 'finetune_lora', name: 'LoRA 微调' },
      ],
    },
    {
      version_id: 'pi05_v0_9', id: 'pi05_v0_9', name: 'π0.5', version: 'v0.9', maturity: 'beta',
      description: '视觉-语言-动作通用策略，支持多任务迁移',
      recipes: [
        { id: 'fine_tune', name: '全量微调' },
        { id: 'freeze_vision', name: '冻结视觉塔' },
      ],
    },
    {
      version_id: 'diffusion_policy_v2_1', id: 'diffusion_policy_v2_1', name: 'Diffusion Policy', version: 'v2.1', maturity: 'stable',
      description: '扩散策略基线，适合高频精细操作',
      recipes: [{ id: 'fine_tune', name: '标准微调' }],
    },
  ];
  const trainDatasets = [
    { id: 'dsv_desk_0820', name: 'desk-grasp-ego', version: 'v3', status: 'READY', uri: 'oss://quicstudio-train/desk-grasp-ego/v3', created_at: '2026-08-20T06:10:00Z', episode_count: 186, duration_hours: 6.4 },
    { id: 'dsv_pick_0818', name: 'pick-place-lerobot', version: 'v2', status: 'READY', uri: 'oss://quicstudio-train/pick-place/v2', created_at: '2026-08-18T02:40:00Z', episode_count: 142, duration_hours: 4.9 },
    { id: 'dsv_tray_0812', name: 'tray-sorting-ego', version: 'v1', status: 'REGISTERED', uri: 'bmcpfs://robot-train/tray-sorting/v1', created_at: '2026-08-12T08:05:00Z', episode_count: 96, duration_hours: 3.1 },
  ];
  const trainJobs = [
    {
      id: 'job_20260821_01', display_name: 'desk-grasp-act-ft', state: 'running', stage: 'TRAINING',
      dataset_version_id: 'dsv_desk_0820', model_version_id: 'act_v1_4', model: { model_id: 'act_v1_4' },
      recipe_id: 'fine_tune', progress: 0.62, created_at: '2026-08-21T01:12:00Z', updated_at: '2026-08-21T03:48:00Z',
      resource_selection: { mode: 'MANUAL', profile: 'rtx4090_x4' }, steps: 20000, batch_size: 64,
    },
    {
      id: 'job_20260821_02', display_name: 'tray-sorting-pi05', state: 'queued', stage: 'QUEUED',
      dataset_version_id: 'dsv_tray_0812', model_version_id: 'pi05_v0_9', model: { model_id: 'pi05_v0_9' },
      recipe_id: 'freeze_vision', progress: 0, created_at: '2026-08-21T03:20:00Z', updated_at: '2026-08-21T03:20:00Z',
      resource_selection: { mode: 'AUTO', profile: null }, steps: 30000, batch_size: 32,
    },
    {
      id: 'job_20260820_07', display_name: 'pick-place-dp-v2', state: 'succeeded', stage: 'FINISHED',
      dataset_version_id: 'dsv_pick_0818', model_version_id: 'diffusion_policy_v2_1', model: { model_id: 'diffusion_policy_v2_1' },
      recipe_id: 'fine_tune', progress: 1, created_at: '2026-08-20T07:05:00Z', updated_at: '2026-08-20T15:42:00Z',
      resource_selection: { mode: 'MANUAL', profile: 'a100_x8' }, steps: 15000, batch_size: 128,
      metrics: { train_loss: 0.0421, val_loss: 0.0588, success_rate: 0.87 },
    },
    {
      id: 'job_20260819_04', display_name: 'tray-sorting-act-retry', state: 'failed', stage: 'FAILED',
      dataset_version_id: 'dsv_tray_0812', model_version_id: 'act_v1_4', model: { model_id: 'act_v1_4' },
      recipe_id: 'fine_tune', progress: 0.18, created_at: '2026-08-19T09:30:00Z', updated_at: '2026-08-19T10:04:00Z',
      resource_selection: { mode: 'MANUAL', profile: 'rtx4090_x4' }, steps: 20000, batch_size: 64,
      failure_reason: 'CUDA out of memory during evaluation',
    },
  ];

  function trainJobById(jobId) {
    return trainJobs.find((job) => String(job.id) === String(jobId)) || null;
  }

  function trainJobLogs(job) {
    if (!job) return { lines: [] };
    const base = [
      `[INFO] job ${job.id} · ${job.display_name}`,
      `[INFO] dataset_version=${job.dataset_version_id} model=${job.model_version_id || '—'} recipe=${job.recipe_id || '—'}`,
      `[INFO] resource profile=${job.resource_selection?.profile || 'auto'}`,
      '[INFO] loading dataset shards ... 186/186',
      '[INFO] normalizing actions (mean/std) ...',
      `[INFO] training steps=${job.steps || '—'} batch_size=${job.batch_size || '—'}`,
    ];
    if (job.state === 'running') {
      base.push('[INFO] epoch 12/20 step 12400/20000 loss=0.0612 lr=3e-5', '[INFO] checkpoint saved to oss://quicstudio-train/ckpt/step-12000');
    } else if (job.state === 'succeeded') {
      base.push('[INFO] evaluation success_rate=0.87', '[INFO] exported model artifact to oss://quicstudio-train/models/act_v1_4/desk-grasp-act-ft');
    } else if (job.state === 'failed') {
      base.push('[ERROR] CUDA out of memory during evaluation', '[ERROR] reduce batch_size or pick a larger profile and retry');
    } else {
      base.push('[INFO] waiting for a free resource slot ...');
    }
    return { lines: base.map((message, index) => ({ seq: index + 1, message, recorded_at: job.updated_at })) };
  }

  function trainRead(path) {
    const route = String(path || '').split('?')[0];
    if (route === '/dashboard') {
      const counts = { running: 0, queued: 0, failed: 0, succeeded: 0 };
      trainJobs.forEach((job) => { counts[job.state] = (counts[job.state] || 0) + 1; });
      return {
        jobs: counts,
        recent_jobs: trainJobs.slice(0, 5).map((job) => ({ ...job })),
        resources: { profiles: trainProfiles.length, busy: 1 },
        updated_at: '2026-08-21T04:00:00Z',
      };
    }
    if (route === '/jobs') return { items: trainJobs.map((job) => ({ ...job })), total: trainJobs.length };
    const logMatch = route.match(/^\/jobs\/([^/]+)\/logs$/);
    if (logMatch) return trainJobLogs(trainJobById(decodeURIComponent(logMatch[1])));
    const jobMatch = route.match(/^\/jobs\/([^/]+)$/);
    if (jobMatch) return trainJobById(decodeURIComponent(jobMatch[1])) || {};
    if (route === '/datasets') return { items: trainDatasets.map((row) => ({ ...row })) };
    if (route === '/models') return { items: trainModels.map((row) => ({ ...row })) };
    if (route === '/resources') return { profiles: trainProfiles.map((row) => ({ ...row })) };
    if (route === '/ops/status') {
      return {
        status: 'healthy',
        region: 'cn-shanghai',
        scheduler: { state: 'running', queue_depth: 2, heartbeat_at: '2026-08-21T04:00:00Z' },
        storage: { provider: 'oss', bucket: 'quicstudio-train', usage_gb: 428.6, quota_gb: 2048 },
        gpu: { total: 20, allocated: 4, utilization: 0.41 },
        versions: { api: '1.6.0', scheduler: '1.6.0', worker_image: 'quicstudio-train:1.6.0' },
      };
    }
    return {};
  }

  function trainWrite(method, path, body = {}) {
    const route = String(path || '').split('?')[0];
    if (route === '/jobs/validate') {
      const issues = [];
      if (!body.dataset_version_id) issues.push({ severity: 'BLOCKER', message: '请选择数据集版本' });
      if (!body.model_version_id) issues.push({ severity: 'BLOCKER', message: '请选择模型版本' });
      if (!body.recipe_id) issues.push({ severity: 'WARNING', message: '未选择 Recipe，将使用默认微调配方' });
      return { ok: !issues.some((item) => item.severity === 'BLOCKER'), issues };
    }
    if (route === '/jobs' && method === 'POST') {
      const model = trainModels.find((item) => item.version_id === body.model_version_id);
      const job = {
        id: `job_${Date.now()}`,
        display_name: body.display_name || `${model ? model.name : 'train'}-run`,
        state: 'queued',
        stage: 'QUEUED',
        dataset_version_id: body.dataset_version_id || null,
        model_version_id: body.model_version_id || null,
        model: model ? { model_id: model.version_id } : null,
        recipe_id: body.recipe_id || 'fine_tune',
        progress: 0,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
        resource_selection: body.resource_selection || { mode: 'AUTO', profile: null },
        steps: body.config_overrides?.['training.steps'] || 20000,
        batch_size: body.config_overrides?.['training.batch_size'] || 32,
      };
      trainJobs.unshift(job);
      return { job_id: job.id, id: job.id, state: job.state, display_name: job.display_name };
    }
    const actionMatch = route.match(/^\/jobs\/([^/]+)\/(cancel|retry|clone)$/);
    if (actionMatch) {
      const job = trainJobById(decodeURIComponent(actionMatch[1]));
      const action = actionMatch[2];
      if (!job) return { id: decodeURIComponent(actionMatch[1]), demo: true };
      if (action === 'cancel') {
        job.state = 'cancelled';
        job.stage = 'CANCELLED';
      } else if (action === 'retry') {
        job.state = 'queued';
        job.stage = 'QUEUED';
        job.progress = 0;
      } else if (action === 'clone') {
        const clone = { ...job, id: `job_${Date.now()}`, display_name: `${job.display_name}-copy`, state: 'queued', stage: 'QUEUED', progress: 0 };
        trainJobs.unshift(clone);
        return clone;
      }
      job.updated_at = new Date().toISOString();
      return { ...job };
    }
    if (route === '/datasets' && method === 'POST') {
      const dataset = {
        id: `dsv_${Date.now()}`,
        name: body.display_name || 'registered-dataset',
        version: 'v1',
        status: body.status || 'REGISTERED',
        uri: body.uri || '',
        created_at: new Date().toISOString(),
      };
      trainDatasets.unshift(dataset);
      return dataset;
    }
    return { id: Date.now(), demo: true };
  }

  return { user, read, write, trainRead, trainWrite };
})();
