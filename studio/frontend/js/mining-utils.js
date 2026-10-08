/** Pure mining projections used by the production collection pages.
 *
 * This module deliberately owns no seeded rows, timers, dictionaries, or
 * mutable demo state.  The local preview's stateful store remains in
 * mining-console.js and is loaded only by the explicit demo entry point.
 */
const QuicDataMiningUtils = (() => {
  const FAILURE_REASONS = [
    { key: 'off_task', label: '非目标动作' },
    { key: 'occlusion', label: '遮挡' },
    { key: 'incomplete', label: '任务执行不完整' },
    { key: 'depth_abnormal', label: 'Depth 异常' },
    { key: 'frame_drop', label: '缺帧' },
    { key: 'time_desync', label: '时间戳不同步' },
    { key: 'data_corrupted', label: '数据损坏' },
    { key: 'privacy_risk', label: '隐私风险' },
  ];

  const PIPELINE_STAGES = [
    { key: 'split', label: '数据切分' },
    { key: 'integrity', label: '完整性检查' },
    { key: 'quality', label: '自动质检' },
    { key: 'desensitize', label: '脱敏' },
    { key: 'process', label: 'Process · 标准化入库' },
  ];

  const CLOUD_SOURCES = [
    { value: 'edge', label: '现场 Edge Node' },
    { value: 'upload', label: '本地上传' },
    { value: 'oss', label: 'OSS 扫描' },
  ];

  const BATCH_STAGES = [
    { key: 'integrity', label: '完整性检查' },
    { key: 'split', label: '自动切分' },
    { key: 'quality', label: '自动质检' },
    { key: 'desensitize', label: '脱敏' },
  ];

  function toNumber(value) {
    const parsed = Number(value);
    return Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
  }

  function batchList(task) {
    return Array.isArray(task?.batches) ? task.batches : [];
  }

  function taskProgress(task) {
    const batches = batchList(task);
    const target = toNumber(task?.target_episodes);
    const raw = batches.reduce((sum, batch) => sum + toNumber(batch?.raw), 0);
    const checked = batches.reduce((sum, batch) => sum + toNumber(batch?.checked), 0);
    const valid = batches.reduce((sum, batch) => sum + toNumber(batch?.valid), 0);
    const validRate = checked > 0 ? valid / checked : 0;
    return {
      target,
      raw,
      checked,
      valid,
      valid_rate: validRate,
      gap: Math.max(0, target - valid),
      progress: target > 0 ? Math.min(1, valid / target) : 0,
      completed_batches: batches.filter((batch) => toNumber(batch?.valid) >= toNumber(batch?.target)).length,
      batch_count: batches.length,
    };
  }

  function splitPlan(target, batchCount, perBatch) {
    const total = toNumber(target);
    if (!total) return [];
    const per = toNumber(perBatch);
    const count = per > 0 ? Math.max(1, Math.ceil(total / per)) : Math.max(1, Math.trunc(toNumber(batchCount)) || 1);
    const base = Math.floor(total / count);
    let remainder = total - base * count;
    return Array.from({ length: count }, (_, index) => {
      const extra = remainder > 0 ? 1 : 0;
      remainder -= extra;
      return { seq: index + 1, target: base + extra };
    });
  }

  function applyAssignment(batches, batchId, assignment) {
    const payload = assignment && typeof assignment === 'object' ? assignment : {};
    const applyAll = String(batchId) === '*';
    const assignees = Array.isArray(payload.assignees) ? payload.assignees : [];
    const onlyUnassigned = !!payload.only_unassigned;
    return (Array.isArray(batches) ? batches : []).map((batch) => {
      if (!applyAll && String(batch?.id) !== String(batchId)) return batch;
      if (applyAll && onlyUnassigned && (Array.isArray(batch?.assignees) ? batch.assignees : []).length) return batch;
      return {
        ...batch,
        assignees,
        window: typeof payload.window === 'string' && payload.window ? payload.window : (batch?.window || ''),
        status: batch?.status === 'planned' && assignees.length ? 'collecting' : batch?.status,
      };
    });
  }

  function aggregateFailures(batches) {
    const rows = Array.isArray(batches) ? batches : [];
    const totals = new Map();
    rows.forEach((batch) => {
      (Array.isArray(batch?.failures) ? batch.failures : []).forEach((item) => {
        const key = String(item?.key || '');
        if (!key) return;
        totals.set(key, (totals.get(key) || 0) + toNumber(item?.count));
      });
    });
    return FAILURE_REASONS
      .filter((reason) => totals.has(reason.key))
      .map((reason) => ({ ...reason, count: totals.get(reason.key) }))
      .sort((left, right) => right.count - left.count);
  }

  function failureReasonLabel(key) {
    return FAILURE_REASONS.find((reason) => reason.key === key)?.label || key;
  }

  function pipelineStageRows(pipelines) {
    return (Array.isArray(pipelines) ? pipelines : []).map((pipeline) => ({
      id: pipeline?.id,
      name: pipeline?.name || '',
      trigger: pipeline?.trigger || 'upload',
      updated_at: pipeline?.updated_at || '',
      stages: PIPELINE_STAGES.map((stage) => {
        const found = (Array.isArray(pipeline?.stages) ? pipeline.stages : []).find((item) => item?.key === stage.key);
        return { ...stage, status: found?.status || 'queued', progress: toNumber(found?.progress) };
      }),
    }));
  }

  function transferSummary(transfers) {
    const rows = Array.isArray(transfers) ? transfers : [];
    return {
      total: rows.length,
      running: rows.filter((row) => ['uploading', 'running'].includes(row?.status)).length,
      succeeded: rows.filter((row) => row?.status === 'succeeded').length,
      failed: rows.filter((row) => row?.status === 'failed').length,
      bytes: rows.reduce((sum, row) => sum + toNumber(row?.size_bytes), 0),
    };
  }

  function cutSummary(episode) {
    const segments = Array.isArray(episode?.segments) ? episode.segments : [];
    const valid = segments.filter((segment) => segment?.valid !== false);
    const total = segments.reduce(
      (sum, segment) => sum + Math.max(0, toNumber(segment?.end_s) - toNumber(segment?.start_s)),
      0,
    );
    return {
      segment_count: segments.length,
      valid_count: valid.length,
      invalid_count: segments.length - valid.length,
      total_s: total,
    };
  }

  function filterCutEpisodes(rows, filters) {
    const opts = filters && typeof filters === 'object' ? filters : {};
    return (Array.isArray(rows) ? rows : []).filter((row) => {
      if (opts.task_name && String(row?.task_name || '') !== String(opts.task_name)) return false;
      if (opts.batch && String(row?.batch || '') !== String(opts.batch)) return false;
      const tags = row?.tags || {};
      for (const key of ['project', 'scene', 'purpose', 'train']) {
        if (opts[key] && String(tags[key] || '') !== String(opts[key])) return false;
      }
      if (opts.keyword) {
        const keyword = String(opts.keyword).toLowerCase();
        const haystack = `${row?.episode_uid || ''} ${row?.task_name || ''} ${row?.collector || ''} ${row?.batch || ''}`.toLowerCase();
        if (!haystack.includes(keyword)) return false;
      }
      return true;
    });
  }

  function activeCutSegment(episode, timeS) {
    const time = toNumber(timeS);
    if (time <= 0) return null;
    const segments = Array.isArray(episode?.segments) ? episode.segments : [];
    return segments.find((segment) => time >= toNumber(segment?.start_s) && time < toNumber(segment?.end_s)) || null;
  }

  function formatCutRange(segment) {
    const format = (value) => {
      const total = Math.max(0, Math.floor(toNumber(value)));
      const minutes = String(Math.floor(total / 60)).padStart(2, '0');
      const seconds = String(total % 60).padStart(2, '0');
      return `${minutes}:${seconds}`;
    };
    return `${format(segment?.start_s)} - ${format(segment?.end_s)}`;
  }

  function batchStageRows(batch) {
    const stages = Array.isArray(batch?.stages) ? batch.stages : [];
    return BATCH_STAGES.map((stage) => {
      const found = stages.find((item) => item?.key === stage.key);
      return { ...stage, status: found?.status || 'queued', progress: toNumber(found?.progress) };
    });
  }

  function batchStage(batch, key) {
    return batchStageRows(batch).find((row) => row.key === key) || null;
  }

  function canRunStage(batch, key) {
    const rows = batchStageRows(batch);
    const index = rows.findIndex((row) => row.key === key);
    if (index < 0) return false;
    if (index > 0 && rows[index - 1].status !== 'succeeded') return false;
    return ['queued', 'failed'].includes(rows[index].status);
  }

  function canAssignPackage(pkg) {
    return Boolean(pkg) && (pkg.raw_status || pkg.status) === 'pending_assignment';
  }

  function taskAssignDone(task) {
    if (!task) return false;
    if (task.assign_mode === 'task') return true;
    const total = Number(task.package_count);
    const pending = Number(task.pending_assignment_count);
    return Number.isFinite(total) && total > 0 && Number.isFinite(pending) && pending === 0;
  }

  function packageCountPreview(targetHours, packageHours) {
    if (targetHours === '' || packageHours === '' || targetHours == null || packageHours == null) return null;
    const target = Math.round(Number(targetHours) * 100);
    const size = Math.round(Number(packageHours) * 100);
    if (!Number.isFinite(target) || !Number.isFinite(size) || target <= 0 || size <= 0) return null;
    return Math.floor(target / size) + (target % size > 0 ? 1 : 0);
  }

  return Object.freeze({
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
    canAssignPackage,
    taskAssignDone,
    packageCountPreview,
  });
})();

// The root component still calls the historical QuicDataMining name for these
// pure projections.  Install only the stateless surface; when the demo module
// is loaded it extends this object with its local stateful operations.
if (typeof globalThis !== 'undefined') {
  globalThis.QuicDataMiningUtils = QuicDataMiningUtils;
  globalThis.QuicDataMining = Object.assign({}, QuicDataMiningUtils, globalThis.QuicDataMining || {});
}

if (typeof module !== 'undefined' && module.exports) module.exports = QuicDataMiningUtils;
