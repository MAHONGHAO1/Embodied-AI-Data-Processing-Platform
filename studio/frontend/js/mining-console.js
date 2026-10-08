/** Local-preview data collection console store; pure projections live in mining-utils.js. */
const _miningPure = (typeof globalThis !== 'undefined' && globalThis.QuicDataMiningUtils)
  || (typeof module !== 'undefined' && module.exports && typeof require === 'function'
    ? require('./mining-utils.js')
    : null);
if (!_miningPure) throw new Error('mining-console.js requires mining-utils.js first');
const {
  FAILURE_REASONS,
  PIPELINE_STAGES,
  CLOUD_SOURCES,
  BATCH_STAGES,
  taskProgress,
  splitPlan,
  applyAssignment,
  aggregateFailures,
  failureReasonLabel,
  pipelineStageRows,
  transferSummary,
  cutSummary,
  filterCutEpisodes,
  activeCutSegment,
  formatCutRange,
  batchStageRows,
  batchStage,
  canRunStage,
} = _miningPure;

const QuicDataMiningDemo = (() => {

  const STAGE_PRESETS = {
    done: { integrity: 'succeeded', split: 'succeeded', quality: 'succeeded', desensitize: 'succeeded' },
    processing: { integrity: 'succeeded', split: 'succeeded', quality: 'running', desensitize: 'queued' },
    collecting: { integrity: 'succeeded', split: 'queued', quality: 'queued', desensitize: 'queued' },
    planned: { integrity: 'queued', split: 'queued', quality: 'queued', desensitize: 'queued' },
  };

  const STAGE_OVERRIDES = {
    '901-1': { split: 'queued' },
    '901-2': { split: 'running', split_progress: 35, quality: 'succeeded' },
    '901-3': { split: 'queued' },
    '901-5': { split: 'pending_review', quality: 'queued' },
    '901-6': { split: 'pending_review', quality: 'queued' },
    '901-7': { integrity: 'running' },
    '901-8': { integrity: 'queued', split: 'queued' },
    '901-10': { integrity: 'queued', split: 'queued' },
    '902-1': { split: 'manual', quality: 'queued' },
    '902-2': { split: 'queued' },
    '903-1': { integrity: 'failed' },
  };

  const DEFAULT_DICTIONARIES = {
    project: { label: '项目标签', items: ['星动 · 家居场景', '星动 · 商超配送', '擎仓 · 仓储分拣'] },
    purpose: { label: '任务用途', items: ['测试任务', '正式任务'] },
    scene: { label: '场景标签', items: ['家居 · 厨房', '家居 · 客厅', '商超 · 货架', '仓储 · 分拣台'] },
    train: { label: '训练用途', items: ['预训练', '后训练', '其它'] },
    software: { label: '软件版本', items: ['QRDF 1.4.2', '采集 App 2.6.0', 'Edge 1.1.0'] },
  };

  const COLLECTION_CONFIG_DEFAULTS = {
    min_valid_rate: 0.9,
    batch_size: 100,
    edge_sync_mode: 'auto',
    desensitize_on_edge: true,
  };

  function buildStages(batch) {
    const preset = STAGE_PRESETS[batch?.status] || STAGE_PRESETS.planned;
    const override = STAGE_OVERRIDES[batch?.id] || {};
    return BATCH_STAGES.map((stage) => {
      const status = override[stage.key] || preset[stage.key] || 'queued';
      const progressKey = `${stage.key}_progress`;
      const progress = typeof override[progressKey] === 'number'
        ? override[progressKey]
        : (status === 'succeeded' || status === 'pending_review' ? 100 : 0);
      return { key: stage.key, status, progress };
    });
  }

  function batchCreatedAt(baseIso, seq) {
    const base = new Date(baseIso || '2026-08-01T02:00:00Z');
    if (Number.isNaN(base.getTime())) return '';
    base.setDate(base.getDate() + Math.max(0, Number(seq || 1) - 1) * 2);
    base.setHours(10, 30, 0, 0);
    return base.toISOString();
  }

  function attachStages(list) {
    return (Array.isArray(list) ? list : []).map((task) => ({
      ...task,
      assign_mode: String(task.id) === '9001' ? 'task' : 'batch',
      batches: batchList(task).map((batch) => {
        const stages = buildStages(batch);
        const sourceCount = toNumber(batch.raw);
        return {
          ...batch,
          name: `${task.name} · 第 ${batch.seq} 批`,
          batch_type: task.modality,
          created_at: batchCreatedAt(task.created_at, batch.seq),
          source_episode_count: sourceCount,
          source_duration_s: sourceCount * 42,
          import_percent: sourceCount ? Math.min(100, Math.round(sourceCount / Math.max(1, toNumber(batch.target)) * 100)) : 0,
          task_label: task.name,
          stages,
          split_mode: stages.find((stage) => stage.key === 'split')?.status === 'manual' ? 'manual' : 'auto',
        };
      }),
    }));
  }

  function toNumber(value) {
    const parsed = Number(value);
    return Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
  }

  function batchList(task) {
    return Array.isArray(task?.batches) ? task.batches : [];
  }

  function seedTasks() {
    return attachStages([
      {
        id: 9001,
        name: '桌面物体整理 · 8 月采集',
        modality: 'ego',
        sop: '桌面场景，单臂抓取，每段 30–60 秒，采前校准相机',
        target_episodes: 1000,
        min_valid_rate: 0.9,
        due_date: '2026-08-31',
        status: 'active',
        workspace_id: 1,
        task_set_id: 12,
        owner: '张明',
        target_duration_hours: 500,
        valid_duration_hours: 326,
        tags: { project: '家庭服务', scene: '桌面', purpose: '正式任务', train: '预训练' },
        created_at: '2026-08-01T02:00:00Z',
        batches: [
          { id: '901-1', seq: 1, target: 100, raw: 108, checked: 108, valid: 96, status: 'done', assignees: [{ collector_id: 101, device_id: 201 }], window: '08-01 ~ 08-03', failures: [{ key: 'off_task', count: 5 }, { key: 'occlusion', count: 4 }] },
          { id: '901-2', seq: 2, target: 100, raw: 96, checked: 96, valid: 88, status: 'done', assignees: [{ collector_id: 102, device_id: 202 }], window: '08-03 ~ 08-05', failures: [{ key: 'incomplete', count: 5 }, { key: 'frame_drop', count: 3 }] },
          { id: '901-3', seq: 3, target: 100, raw: 102, checked: 100, valid: 91, status: 'done', assignees: [{ collector_id: 101, device_id: 201 }], window: '08-05 ~ 08-07', failures: [{ key: 'off_task', count: 6 }, { key: 'time_desync', count: 3 }] },
          { id: '901-4', seq: 4, target: 100, raw: 94, checked: 90, valid: 79, status: 'processing', assignees: [{ collector_id: 103, device_id: 201 }], window: '08-08 ~ 08-10', failures: [{ key: 'depth_abnormal', count: 7 }, { key: 'off_task', count: 4 }] },
          { id: '901-5', seq: 5, target: 100, raw: 88, checked: 80, valid: 68, status: 'processing', assignees: [{ collector_id: 103, device_id: 201 }], window: '08-10 ~ 08-12', failures: [{ key: 'depth_abnormal', count: 9 }, { key: 'occlusion', count: 3 }] },
          { id: '901-6', seq: 6, target: 100, raw: 76, checked: 70, valid: 61, status: 'collecting', assignees: [], window: '08-12 ~ 08-14', failures: [{ key: 'incomplete', count: 6 }, { key: 'off_task', count: 3 }] },
          { id: '901-7', seq: 7, target: 100, raw: 64, checked: 60, valid: 52, status: 'collecting', assignees: [], window: '08-14 ~ 08-16', failures: [{ key: 'occlusion', count: 5 }, { key: 'frame_drop', count: 3 }] },
          { id: '901-8', seq: 8, target: 100, raw: 40, checked: 36, valid: 32, status: 'collecting', assignees: [], window: '08-16 ~ 08-18', failures: [{ key: 'off_task', count: 3 }, { key: 'privacy_risk', count: 1 }] },
          { id: '901-9', seq: 9, target: 100, raw: 22, checked: 20, valid: 18, status: 'collecting', assignees: [], window: '08-18 ~ 08-20', failures: [{ key: 'incomplete', count: 2 }] },
          { id: '901-10', seq: 10, target: 100, raw: 10, checked: 10, valid: 9, status: 'planned', assignees: [], window: '08-20 ~ 08-22', failures: [{ key: 'off_task', count: 1 }] },
        ],
      },
      {
        id: 9002,
        name: '水杯与托盘动作补采',
        modality: 'ego',
        sop: '补采被判定为无效的动作段，重点是抓取稳定性',
        target_episodes: 300,
        min_valid_rate: 0.92,
        due_date: '2026-09-10',
        status: 'active',
        workspace_id: 1,
        task_set_id: 12,
        owner: '李芳',
        target_duration_hours: 120,
        valid_duration_hours: 85,
        tags: { project: '家庭服务', scene: '桌面', purpose: '正式任务', train: '预训练' },
        created_at: '2026-08-18T06:00:00Z',
        batches: [
          { id: '902-1', seq: 1, target: 100, raw: 72, checked: 72, valid: 66, status: 'collecting', assignees: [{ collector_id: 102, device_id: 202 }], window: '08-20 ~ 08-24', failures: [{ key: 'occlusion', count: 4 }, { key: 'off_task', count: 2 }] },
          { id: '902-2', seq: 2, target: 100, raw: 30, checked: 24, valid: 22, status: 'collecting', assignees: [], window: '08-24 ~ 08-28', failures: [{ key: 'incomplete', count: 2 }] },
          { id: '902-3', seq: 3, target: 100, raw: 0, checked: 0, valid: 0, status: 'planned', assignees: [], window: '08-28 ~ 09-02', failures: [] },
        ],
      },
      {
        id: 9003,
        name: '遥操作 · 双臂装配场景',
        modality: 'teleop',
        sop: '双臂遥操作，需保证主从时间戳同步',
        target_episodes: 500,
        min_valid_rate: 0.88,
        due_date: '2026-09-30',
        status: 'paused',
        workspace_id: 1,
        task_set_id: 11,
        owner: '王强',
        target_duration_hours: 80,
        valid_duration_hours: 46,
        tags: { project: '工业制造', scene: '装配车间', purpose: '测试任务', train: '后训练' },
        created_at: '2026-08-10T08:00:00Z',
        batches: [
          { id: '903-1', seq: 1, target: 250, raw: 180, checked: 176, valid: 150, status: 'processing', assignees: [{ collector_id: 103, device_id: 203 }], window: '08-12 ~ 08-26', failures: [{ key: 'time_desync', count: 14 }, { key: 'data_corrupted', count: 6 }] },
          { id: '903-2', seq: 2, target: 250, raw: 0, checked: 0, valid: 0, status: 'planned', assignees: [], window: '待排期', failures: [] },
        ],
      },
      {
        id: 9004,
        name: '厨房场景 · 双手协作采集',
        modality: 'ego',
        sop: '厨房场景双手协作，注意灶台区域脱敏',
        target_episodes: 200,
        min_valid_rate: 0.9,
        due_date: '2026-09-20',
        status: 'active',
        workspace_id: 1,
        task_set_id: 12,
        owner: '张明',
        target_duration_hours: 60,
        valid_duration_hours: 0,
        tags: { project: '家庭服务', scene: '厨房', purpose: '正式任务', train: '预训练' },
        created_at: '2026-09-01T02:00:00Z',
        batches: [],
      },
      {
        id: 9005,
        name: '仓储盘点 · 夜间补采',
        modality: 'ego',
        sop: '夜间仓储通道盘点，低光环境补采',
        target_episodes: 400,
        min_valid_rate: 0.85,
        due_date: '2026-09-25',
        status: 'active',
        workspace_id: 1,
        task_set_id: 12,
        owner: '赵敏',
        target_duration_hours: 40,
        valid_duration_hours: 0,
        tags: { project: '仓储物流', scene: '仓库', purpose: '测试任务', train: '后训练' },
        created_at: '2026-09-02T02:00:00Z',
        batches: [
          { id: '905-1', seq: 1, target: 200, raw: 60, checked: 0, valid: 0, status: 'collecting', assignees: [], window: '09-03 ~ 09-06', failures: [] },
          { id: '905-2', seq: 2, target: 200, raw: 0, checked: 0, valid: 0, status: 'planned', assignees: [], window: '09-07 ~ 09-10', failures: [] },
        ],
      },
    ]);
  }

  function seedPipelines() {
    return [
      {
        id: 'pl-ego',
        name: 'EGO 标准处理链',
        trigger: 'upload',
        task_id: 9001,
        updated_at: '2026-08-20T09:12:00Z',
        stages: [
          { key: 'split', status: 'succeeded', progress: 100 },
          { key: 'integrity', status: 'succeeded', progress: 100 },
          { key: 'quality', status: 'running', progress: 68 },
          { key: 'desensitize', status: 'queued', progress: 0 },
          { key: 'process', status: 'queued', progress: 0 },
        ],
      },
      {
        id: 'pl-teleop',
        name: '遥操作处理链（含时间同步）',
        trigger: 'upload',
        task_id: 9003,
        updated_at: '2026-08-20T08:40:00Z',
        stages: [
          { key: 'split', status: 'succeeded', progress: 100 },
          { key: 'integrity', status: 'succeeded', progress: 100 },
          { key: 'quality', status: 'failed', progress: 42 },
          { key: 'desensitize', status: 'queued', progress: 0 },
          { key: 'process', status: 'queued', progress: 0 },
        ],
      },
    ];
  }

  function seedTransfers() {
    return [
      { id: 'tr-1', source: 'edge', name: 'BJ-EDGE-01 · 批次 #4', size_bytes: 42_000_000_000, status: 'succeeded', progress: 100, checksum_verified: true, updated_at: '2026-08-20T09:00:00Z' },
      { id: 'tr-2', source: 'edge', name: 'BJ-EDGE-01 · 批次 #5', size_bytes: 38_500_000_000, status: 'running', progress: 62, checksum_verified: false, updated_at: '2026-08-20T09:14:00Z' },
      { id: 'tr-3', source: 'upload', name: 'cup_tray_reshoot.zip', size_bytes: 6_200_000_000, status: 'succeeded', progress: 100, checksum_verified: true, updated_at: '2026-08-19T17:20:00Z' },
      { id: 'tr-4', source: 'oss', name: 'oss://quicdata-ego/raw/2026-08-19', size_bytes: 12_800_000_000, status: 'failed', progress: 35, checksum_verified: false, updated_at: '2026-08-19T21:05:00Z' },
      { id: 'tr-5', source: 'edge', name: 'SH-EDGE-02 · 批次 #1', size_bytes: 21_400_000_000, status: 'queued', progress: 0, checksum_verified: false, updated_at: '2026-08-20T09:15:00Z' },
    ];
  }

  function seedCutEpisodes() {
    return attachCutTags([
      {
        id: 4200, episode_uid: 'EGO-2026-0811-0042', task_id: 9001, task_name: '桌面物体整理 · 8 月采集', batch_id: '901-1',
        collector: '张明', device: 'EGO 采集背包 A', batch: '批次 #1', duration_s: 125,
        preview_url: 'https://vjs.zencdn.net/v/oceans.mp4',
        segments: [
          { id: '4200-s1', start_s: 0, end_s: 35, description: '拿起桌面的水杯', valid: true },
          { id: '4200-s2', start_s: 35, end_s: 75, description: '将水杯平稳放入托盘', valid: true },
          { id: '4200-s3', start_s: 75, end_s: 125, description: '等待或准备动作', valid: false },
        ],
      },
      {
        id: 4201, episode_uid: 'DRV-2026-0811-0042-A', task_id: 9001, task_name: '桌面物体整理 · 8 月采集', batch_id: '901-1',
        collector: '张明', device: 'EGO 采集背包 A', batch: '批次 #1', duration_s: 40,
        preview_url: 'https://vjs.zencdn.net/v/oceans.mp4',
        segments: [
          { id: '4201-s1', start_s: 0, end_s: 18, description: '抓取水杯并抬起', valid: true },
          { id: '4201-s2', start_s: 18, end_s: 40, description: '水杯入托盘并松手', valid: true },
        ],
      },
      {
        id: 4202, episode_uid: 'EGO-2026-0813-0055', task_id: 9001, task_name: '桌面物体整理 · 8 月采集', batch_id: '901-2',
        collector: '李芳', device: 'EGO 采集背包 B', batch: '批次 #2', duration_s: 96,
        preview_url: 'https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.mp4',
        segments: [
          { id: '4202-s1', start_s: 2, end_s: 30, description: '整理桌面文具', valid: true },
          { id: '4202-s2', start_s: 30, end_s: 58, description: '将文具放入收纳盒', valid: true },
          { id: '4202-s3', start_s: 58, end_s: 96, description: '画面被手遮挡', valid: false },
        ],
      },
      {
        id: 4300, episode_uid: 'EGO-2026-0820-0071', task_id: 9002, task_name: '水杯与托盘动作补采', batch_id: '902-1',
        collector: '曾琳', device: 'EGO 采集背包 B', batch: '批次 #1', duration_s: 88,
        preview_url: 'https://vjs.zencdn.net/v/oceans.mp4',
        segments: [
          { id: '4300-s1', start_s: 0, end_s: 26, description: '双手握杯校准位置', valid: true },
          { id: '4300-s2', start_s: 26, end_s: 52, description: '水杯放入托盘（补采）', valid: true },
          { id: '4300-s3', start_s: 52, end_s: 88, description: '托盘归位', valid: true },
        ],
      },
      {
        id: 4400, episode_uid: 'TELEOP-2026-0816-0009', task_id: 9003, task_name: '遥操作 · 双臂装配场景', batch_id: '903-1',
        collector: '刘畅', device: '遥操作工作站 01', batch: '批次 #1', duration_s: 150,
        preview_url: 'https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.mp4',
        segments: [
          { id: '4400-s1', start_s: 0, end_s: 50, description: '左臂抓取零件', valid: true },
          { id: '4400-s2', start_s: 50, end_s: 105, description: '双臂协同装配', valid: true },
          { id: '4400-s3', start_s: 105, end_s: 150, description: '右臂位姿漂移，判无效', valid: false },
        ],
      },
    ]);
  }

  let dictionaries = JSON.parse(JSON.stringify(DEFAULT_DICTIONARIES));
  let collectionConfig = { ...COLLECTION_CONFIG_DEFAULTS };
  let cutEpisodes = attachCutTags(seedCutEpisodes());

  function attachCutTags(rows) {
    return (Array.isArray(rows) ? rows : []).map((row) => ({
      ...row,
      tags: buildTags(hashOf(`${row?.task_id}:${row?.id}`)),
    }));
  }

  function buildTags(hash) {
    const pick = (kind, salt) => {
      const items = dictionaries[kind]?.items || [];
      return items.length ? items[(hash + salt) % items.length] : '';
    };
    return {
      project: pick('project', 0),
      purpose: pick('purpose', 1),
      scene: pick('scene', 2),
      train: pick('train', 3),
      software: pick('software', 4),
      device_version: 'UM13.0 v2.3.1',
    };
  }

  let tasks = seedTasks();
  let pipelines = seedPipelines();
  let transfers = seedTransfers();
  let sequence = 9100;

  function setBatchStage(batches, batchId, key, patch) {
    return (Array.isArray(batches) ? batches : []).map((batch) => {
      if (String(batch?.id) !== String(batchId)) return batch;
      const stages = batchStageRows(batch).map((row) => (row.key === key ? { ...row, ...(patch || {}) } : row));
      return { ...batch, stages };
    });
  }

  function restoreBatchToRaw(batches, batchId) {
    return (Array.isArray(batches) ? batches : []).map((batch) => {
      if (String(batch?.id) !== String(batchId)) return batch;
      const stages = batchStageRows(batch).map((row) => {
        if (row.key === 'split') return { ...row, status: 'manual', progress: 0 };
        if (row.key === 'quality' || row.key === 'desensitize') return { ...row, status: 'queued', progress: 0 };
        return row;
      });
      return { ...batch, stages, split_mode: 'manual', restored_to_raw: true };
    });
  }

  function listCutEpisodes() {
    return cutEpisodes;
  }

  const CUT_PREVIEW_URLS = [
    'https://vjs.zencdn.net/v/oceans.mp4',
    'https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.mp4',
  ];
  const CUT_PREVIEW_DESCRIPTIONS = ['拿起目标物体', '移动到目标位置', '放置并松手', '回位与停顿', '修正抓取姿态'];

  function hashOf(value) {
    const text = String(value || '');
    let hash = 7;
    for (let i = 0; i < text.length; i += 1) hash = (hash * 31 + text.charCodeAt(i)) % 100000;
    return hash;
  }

  /* Design-preview projection: deterministic cut episodes for batches that have
     no seeded cut data, so review flows always show stable content. */
  function generatedBatchCutEpisodes(taskId, batchId, taskName) {
    const hash = hashOf(`${taskId}:${batchId}:episodes`);
    const count = 3 + (hash % 3);
    const url = CUT_PREVIEW_URLS[hash % CUT_PREVIEW_URLS.length];
    return Array.from({ length: count }, (_, index) => {
      const segCount = 2 + ((hash + index) % 3);
      let cursor = 0;
      const segments = Array.from({ length: segCount }, (_, segIndex) => {
        const duration = 8 + ((hash + index * 7 + segIndex * 13) % 25);
        const start = cursor;
        cursor += duration;
        const invalid = (hash + index + segIndex) % 6 === 0;
        return {
          id: `${batchId}-e${index + 1}-s${segIndex + 1}`,
          start_s: start,
          end_s: cursor,
          description: `${CUT_PREVIEW_DESCRIPTIONS[(hash + index + segIndex) % CUT_PREVIEW_DESCRIPTIONS.length]}${invalid ? '（异常）' : ''}`,
          valid: !invalid,
        };
      });
      return {
        id: `${batchId}-eps-${index + 1}`,
        episode_uid: `AUTO-${String(batchId).toUpperCase()}-E${index + 1}`,
        task_id: taskId,
        task_name: taskName || '',
        batch_id: String(batchId),
        collector: '—',
        device: '—',
        batch: '',
        duration_s: cursor,
        preview_url: url,
        tags: buildTags(hash + index),
        segments,
      };
    });
  }

  function listBatchCutEpisodes(taskId, batchId, taskName) {
    const seeded = cutEpisodes.filter((row) => String(row?.batch_id ?? '') === String(batchId));
    if (seeded.length) return seeded.map((row) => ({ ...row, task_name: taskName || row.task_name }));
    return generatedBatchCutEpisodes(taskId, batchId, taskName);
  }

  function findCutEpisode(episodeId) {
    return cutEpisodes.find((row) => String(row?.id) === String(episodeId)) || null;
  }



  function matchesScope(row, scope) {
    if (!scope || typeof scope !== 'object') return true;
    if (scope.workspace_id != null && String(row.workspace_id ?? '') !== String(scope.workspace_id)) return false;
    if (scope.task_set_id != null && String(row.task_set_id ?? '') !== String(scope.task_set_id)) return false;
    return true;
  }

  function listTasks(scope) {
    return tasks.filter((task) => matchesScope(task, scope));
  }

  function findTask(taskId) {
    return tasks.find((task) => String(task.id) === String(taskId)) || null;
  }

  function createTask(payload) {
    const body = payload && typeof payload === 'object' ? payload : {};
    sequence += 1;
    const task = {
      id: sequence,
      name: String(body.name || '').trim(),
      modality: String(body.modality || 'ego'),
      sop: String(body.sop || '').trim(),
      target_episodes: toNumber(body.target_episodes),
      min_valid_rate: Number(body.min_valid_rate) || 0,
      due_date: typeof body.due_date === 'string' ? body.due_date : '',
      tags: body.tags && typeof body.tags === 'object' ? { ...body.tags } : null,
      owner: String(body.owner || '').trim(),
      status: 'active',
      workspace_id: body.workspace_id ?? null,
      task_set_id: body.task_set_id ?? null,
      created_at: new Date().toISOString(),
      batches: [],
    };
    tasks = [task, ...tasks];
    return task;
  }

  function splitTaskBatches(taskId, options) {
    const task = findTask(taskId);
    if (!task) return null;
    const opts = options && typeof options === 'object' ? options : {};
    const plan = splitPlan(task.target_episodes, opts.batch_count, opts.per_batch);
    let counter = task.batches.length;
    const created = plan.map((item) => {
      counter += 1;
      return {
        id: `${task.id}-${counter}`,
        seq: counter,
        target: item.target,
        raw: 0,
        checked: 0,
        valid: 0,
        status: 'planned',
        assignees: [],
        window: '',
        failures: [],
      };
    });
    task.batches = [...task.batches, ...created];
    return created;
  }

  function assignTaskBatch(taskId, batchId, assignment) {
    const task = findTask(taskId);
    if (!task) return null;
    task.batches = applyAssignment(task.batches, batchId, assignment);
    return findTask(taskId);
  }

  function listPipelines(scope) {
    return pipelines.filter((pipeline) => {
      if (!scope || !scope.task_id) return true;
      return String(pipeline.task_id ?? '') === String(scope.task_id);
    });
  }

  function listTransfers() {
    return transfers;
  }

  function retryTransfer(transferId) {
    transfers = transfers.map((row) => (String(row.id) === String(transferId)
      ? { ...row, status: 'queued', progress: 0, updated_at: new Date().toISOString() }
      : row));
    return transfers;
  }

  function setTaskBatchStage(taskId, batchId, key, patch) {
    const task = findTask(taskId);
    if (!task) return null;
    task.batches = setBatchStage(task.batches, batchId, key, patch);
    return task;
  }

  function reviewTaskBatchSplit(taskId, batchId, approve) {
    const task = findTask(taskId);
    if (!task) return null;
    task.batches = approve
      ? setBatchStage(task.batches, batchId, 'split', { status: 'succeeded', progress: 100 })
      : setBatchStage(task.batches, batchId, 'split', { status: 'running', progress: 0 });
    return task;
  }

  function restoreTaskBatchToManual(taskId, batchId) {
    const task = findTask(taskId);
    if (!task) return null;
    task.batches = restoreBatchToRaw(task.batches, batchId);
    return task;
  }

  function setTaskAssignMode(taskId, mode) {
    const task = findTask(taskId);
    if (!task || !['task', 'batch'].includes(mode)) return null;
    task.assign_mode = mode;
    return task;
  }

  /* Design-preview only: simulates an async stage run with a single bounded timer.
     Replaced by realtime job events once the pipeline APIs are wired. */
  function simulateBatchStageRun(taskId, batchId, key, onDone) {
    setTaskBatchStage(taskId, batchId, key, { status: 'running', progress: 0 });
    window.setTimeout(() => {
      const status = key === 'split' ? 'pending_review' : 'succeeded';
      setTaskBatchStage(taskId, batchId, key, { status, progress: 100 });
      if (typeof onDone === 'function') onDone(status);
    }, 1200);
  }

  function reset() {
    tasks = seedTasks();
    pipelines = seedPipelines();
    transfers = seedTransfers();
    cutEpisodes = attachCutTags(seedCutEpisodes());
    dictionaries = JSON.parse(JSON.stringify(DEFAULT_DICTIONARIES));
    collectionConfig = { ...COLLECTION_CONFIG_DEFAULTS };
  }

  function listDictionaries() {
    return Object.entries(dictionaries).map(([kind, def]) => ({ kind, label: def.label, items: [...def.items] }));
  }

  function addDictItem(kind, value) {
    const bucket = dictionaries[kind];
    const text = String(value || '').trim();
    if (!bucket || !text) return false;
    if (bucket.items.some((item) => item === text)) return false;
    bucket.items.push(text);
    return true;
  }

  function removeDictItem(kind, value) {
    const bucket = dictionaries[kind];
    if (!bucket) return false;
    const next = bucket.items.filter((item) => item !== value);
    if (next.length === bucket.items.length) return false;
    bucket.items = next;
    return true;
  }

  function getCollectionConfig() {
    return { ...collectionConfig };
  }

  function updateCollectionConfig(patch) {
    collectionConfig = { ...collectionConfig, ...(patch && typeof patch === 'object' ? patch : {}) };
    return getCollectionConfig();
  }

  function capacityDashboard() {
    return {
      kpis: { plan_target: 1800, raw_total: 1560, valid_total: 1240, rate: 0.795, plan_completion: 0.689 },
      daily: [
        { date: '09-01', raw: 214, valid: 178 },
        { date: '09-02', raw: 196, valid: 171 },
        { date: '09-03', raw: 238, valid: 205 },
        { date: '09-04', raw: 205, valid: 189 },
        { date: '09-05', raw: 246, valid: 212 },
        { date: '09-06', raw: 189, valid: 141 },
        { date: '09-07', raw: 272, valid: 244 },
      ],
    };
  }

  function collectionDashboard() {
    return {
      stages: { collecting: 4, processing: 3, done: 5 },
      by_scene: [
        { label: '家居 · 厨房', count: 612 },
        { label: '家居 · 客厅', count: 388 },
        { label: '商超 · 货架', count: 244 },
        { label: '仓储 · 分拣台', count: 316 },
      ],
      by_purpose: [
        { label: '正式任务', count: 1346 },
        { label: '测试任务', count: 214 },
      ],
      by_project: [
        { label: '星动 · 家居场景', count: 742 },
        { label: '星动 · 商超配送', count: 244 },
        { label: '擎仓 · 仓储分拣', count: 574 },
      ],
    };
  }

  function efficiencyDashboard() {
    return {
      collectors: [
        { name: '曾琳', collected: 262, valid: 242, rate: 0.924, avg_per_day: 26 },
        { name: '张明', collected: 320, valid: 291, rate: 0.909, avg_per_day: 32 },
        { name: '李芳', collected: 286, valid: 254, rate: 0.888, avg_per_day: 29 },
        { name: '刘畅', collected: 240, valid: 198, rate: 0.825, avg_per_day: 24 },
      ],
      devices: [
        { name: 'EGO 采集背包 A', collected: 430, valid_rate: 0.881, abnormal: 12 },
        { name: 'EGO 采集背包 B', collected: 386, valid_rate: 0.902, abnormal: 5 },
        { name: '遥操作工作站 01', collected: 244, valid_rate: 0.832, abnormal: 21 },
      ],
    };
  }

  return {
    listTasks,
    findTask,
    createTask,
    splitTaskBatches,
    assignTaskBatch,
    listPipelines,
    listTransfers,
    retryTransfer,
    listCutEpisodes,
    findCutEpisode,
    listBatchCutEpisodes,
    setBatchStage,
    restoreBatchToRaw,
    setTaskBatchStage,
    reviewTaskBatchSplit,
    restoreTaskBatchToManual,
    simulateBatchStageRun,
    setTaskAssignMode,
    listDictionaries,
    addDictItem,
    removeDictItem,
    getCollectionConfig,
    updateCollectionConfig,
    capacityDashboard,
    collectionDashboard,
    efficiencyDashboard,
    reset,
  };
})();

const _quicDataMiningApi = Object.assign({}, _miningPure, QuicDataMiningDemo);
if (typeof globalThis !== 'undefined') globalThis.QuicDataMining = _quicDataMiningApi;
if (typeof module !== 'undefined' && module.exports) module.exports = _quicDataMiningApi;
